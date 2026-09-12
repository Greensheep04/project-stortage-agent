import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import psycopg

from . import corpus_retrieval, db, explain
from .corpus import CorpusError
from .corpus_retrieval import CorpusIndexNotReadyError
from .reconcile import reconcile
from .retrieval import RetrievalError

HISTORY_TOP = 5
RULE_TOP = 5
MAX_CONTEXT_DOCS = 8


class InvestigateError(Exception):
    pass


def _normalize_as_of(as_of: Any) -> Any:
    """Accept ISO-8601 strings in addition to the legacy as_of formats."""
    if isinstance(as_of, str):
        try:
            return datetime.fromisoformat(as_of.strip())
        except ValueError:
            return as_of
    return as_of


def _fetch_source_filename(cur: psycopg.Cursor, dataset_id: str) -> Optional[str]:
    cur.execute("SELECT source_filename FROM dataset WHERE dataset_id = %s", (dataset_id,))
    row = cur.fetchone()
    return row[0] if row else None


def _fetch_notes(cur: psycopg.Cursor, dataset_id: str, evidence_ids: List[str]) -> List[Dict[str, Any]]:
    if not evidence_ids:
        return []
    cur.execute(
        """
        SELECT
            ev_id, order_id, line_id, ref_tx_id, record_time, record_type,
            recorder, content, source_sheet, source_row
        FROM evidence_note
        WHERE dataset_id = %s AND ev_id = ANY(%s)
        ORDER BY source_row
        """,
        (dataset_id, evidence_ids),
    )
    cols = [desc[0] for desc in cur.description]
    items: List[Dict[str, Any]] = []
    for row in cur.fetchall():
        rec = dict(zip(cols, row))
        items.append(
            {
                "evidence_id": rec["ev_id"],
                "evidence_type": "note",
                "order_id": rec["order_id"],
                "line_id": rec["line_id"],
                "tx_id": rec["ref_tx_id"],
                "record_time": rec["record_time"].isoformat() if rec["record_time"] else None,
                "record_type": rec["record_type"],
                "recorder": rec["recorder"],
                "content": rec["content"],
                "source": {
                    "sheet": rec["source_sheet"],
                    "row": rec["source_row"],
                },
            }
        )
    return items


def _fetch_flows(cur: psycopg.Cursor, dataset_id: str, evidence_ids: List[str]) -> List[Dict[str, Any]]:
    if not evidence_ids:
        return []
    cur.execute(
        """
        SELECT
            tx_id, order_id, line_id, event_time, biz_type, quantity,
            status, ref_tx_id, note, source_sheet, source_row
        FROM wms_flow
        WHERE dataset_id = %s AND tx_id = ANY(%s)
        ORDER BY source_row
        """,
        (dataset_id, evidence_ids),
    )
    cols = [desc[0] for desc in cur.description]
    items: List[Dict[str, Any]] = []
    for row in cur.fetchall():
        rec = dict(zip(cols, row))
        note = rec["note"] or ""
        content = note if note else f"{rec['biz_type']} {rec['quantity']} 件（{rec['status']}）"
        items.append(
            {
                "evidence_id": rec["tx_id"],
                "evidence_type": "flow",
                "order_id": rec["order_id"],
                "line_id": rec["line_id"],
                "tx_id": rec["tx_id"],
                "event_time": rec["event_time"].isoformat() if rec["event_time"] else None,
                "biz_type": rec["biz_type"],
                "quantity": rec["quantity"],
                "status": rec["status"],
                "ref_tx_id": rec["ref_tx_id"],
                "note": note,
                "content": content,
                "source": {
                    "sheet": rec["source_sheet"],
                    "row": rec["source_row"],
                },
            }
        )
    return items


def _collect_evidence(
    cur: psycopg.Cursor,
    dataset_id: str,
    facts: Dict[str, Any],
    source_filename: Optional[str],
) -> List[Dict[str, Any]]:
    evidence_ids: List[str] = []
    for line in facts.get("lines", []):
        evidence_ids.extend(line.get("evidence_ids", []))
    evidence_ids = sorted(set(evidence_ids))

    note_ids = [eid for eid in evidence_ids if eid.startswith("EV-")]
    flow_ids = [eid for eid in evidence_ids if eid.startswith("TX-")]

    items: List[Dict[str, Any]] = []
    items.extend(_fetch_notes(cur, dataset_id, note_ids))
    items.extend(_fetch_flows(cur, dataset_id, flow_ids))

    # Preserve original evidence_id order while attaching source filename.
    by_id = {item["evidence_id"]: item for item in items}
    ordered: List[Dict[str, Any]] = []
    for eid in evidence_ids:
        if eid in by_id:
            item = by_id[eid]
            item["source"]["filename"] = source_filename
            ordered.append(item)
    return ordered


def _default_description(facts: Dict[str, Any]) -> str:
    """Build a fixed retrieval description from facts only (no guessed cause)."""
    parts: List[str] = []
    for line in facts.get("lines", []):
        tags = "/".join(line.get("status_tags", [])) or "无状态标记"
        parts.append(
            f"{line['order_id']}/{line['line_id']} 商品 {line['sku']}：计划 {line['planned_quantity']}，"
            f"净出库 {line['matched_net_shipped_quantity']}，差异 {line['difference_quantity']}，状态 {tags}"
        )
    return "仓储出库异常核查：" + "；".join(parts) + "。查找相似历史案例与适用的出库处理规范。"


def _get_embedder_with_usage(model: Optional[str] = None):
    """Build the DashScope embedder and capture per-call usage for call records."""
    import os

    from dotenv import load_dotenv
    from openai import OpenAI

    from . import config

    load_dotenv(str(config._ENV_PATH), override=False)
    api_key = os.getenv("DASHSCOPE_API_KEY")
    base_url = os.getenv(
        "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    model = model or os.getenv("EMBEDDING_MODEL", "text-embedding-v4")
    if not api_key:
        raise RetrievalError("环境变量 DASHSCOPE_API_KEY 未配置")

    client = OpenAI(base_url=base_url, api_key=api_key)
    usage = {"calls": 0, "total_tokens": 0}

    def embed(text: str) -> List[float]:
        resp = client.embeddings.create(input=[text], model=model)
        if resp.usage is not None:
            usage["calls"] += 1
            usage["total_tokens"] += resp.usage.total_tokens or 0
        return resp.data[0].embedding

    return embed, model, usage


def _fetch_corpus_events(
    cur: psycopg.Cursor,
    corpus_id: str,
    order_id: str,
    line_ids: Optional[List[str]],
    as_of: datetime,
) -> List[Dict[str, Any]]:
    sql = """
        SELECT doc_id, title, scope_line_id, recorded_at, occurred_at,
               jsonl_file, jsonl_row, raw
        FROM corpus_doc
        WHERE corpus_id = %s AND doc_type = 'event' AND scope_order_id = %s
          AND occurred_at <= %s AND recorded_at <= %s
    """
    params: List[Any] = [corpus_id, order_id, as_of, as_of]
    if line_ids is not None:
        sql += " AND scope_line_id = ANY(%s)"
        params.append(list(line_ids))
    sql += " ORDER BY scope_line_id, recorded_at, doc_id"
    cur.execute(sql, params)

    items: List[Dict[str, Any]] = []
    for doc_id, title, scope_line_id, recorded_at, occurred_at, jsonl_file, jsonl_row, raw in cur.fetchall():
        items.append(
            {
                "doc_id": doc_id,
                "line_id": scope_line_id,
                "title": title,
                "recorded_at": recorded_at.isoformat(),
                "occurred_at": occurred_at.isoformat() if occurred_at else None,
                "sections": raw["sections"],
                "source": {"file": jsonl_file, "row": jsonl_row},
            }
        )
    return items


def _search_pools(
    dataset_id: str,
    corpus_id: str,
    description: str,
    lines: List[Dict[str, Any]],
    as_of: str,
    search_mode: str,
    embed_fn,
    embedding_model: Optional[str],
    scheme: Optional[str] = None,
    rerank_fn=None,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    scopes: List[Tuple[Optional[str], Optional[str]]] = []
    if lines:
        scopes = [(line.get("warehouse"), line.get("sku")) for line in lines]
    else:
        scopes = [(None, None)]

    if scheme is not None:
        # Explicit scheme: same parent-level ranking as the evaluation.
        merged: Dict[str, Dict[str, Dict[str, Any]]] = {"case": {}, "sop": {}}
        for warehouse, sku in scopes:
            for doc_type in ("case", "sop"):
                ranked = corpus_retrieval.rank_parents(
                    dataset_id,
                    corpus_id,
                    description,
                    doc_type,
                    as_of=as_of,
                    mode=scheme,
                    candidate_depth=100,
                    parent_top=max(HISTORY_TOP, RULE_TOP),
                    embed_fn=embed_fn,
                    embedding_model=embedding_model,
                    warehouse=warehouse,
                    sku=sku,
                    rerank_fn=rerank_fn,
                )
                for row in ranked["parents"]:
                    key = row["doc_id"]
                    current = merged[doc_type].get(key)
                    if current is None or row["rank"] < current["rank"]:
                        merged[doc_type][key] = {**row, "_scope": (warehouse, sku)}
        return merged

    merged = {"case": {}, "sop": {}}
    for warehouse, sku in scopes:
        result = corpus_retrieval.search_corpus(
            dataset_id,
            corpus_id,
            description,
            doc_types=["case", "sop"],
            k=max(HISTORY_TOP, RULE_TOP),
            mode=search_mode,
            as_of=as_of,
            embed_fn=embed_fn,
            embedding_model=embedding_model,
            warehouse=warehouse,
            sku=sku,
        )
        for pool_name, rows in result["pools"].items():
            for row in rows:
                current = merged[pool_name].get(row["chunk_id"])
                if current is None or row["rank"] < current["rank"]:
                    row = {**row, "_scope": (warehouse, sku)}
                    merged[pool_name][row["chunk_id"]] = row
    return merged


def _readback_docs(
    corpus_id: str,
    rows: List[Dict[str, Any]],
    as_of: str,
) -> List[Dict[str, Any]]:
    docs: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        doc_id = row["doc_id"]
        if doc_id in docs:
            docs[doc_id]["matched_chunk_ids"].append(row["chunk_id"])
            continue
        warehouse, sku = row.get("_scope", (None, None))
        parent = corpus_retrieval.read_corpus_doc(
            corpus_id, doc_id, as_of=as_of, warehouse=warehouse, sku=sku
        )
        sections = [
            {
                "chunk_id": f"{corpus_id}:{doc_id}:{section['section_id']}",
                "section_id": section["section_id"],
                "heading": section["heading"],
                "text": section["text"],
            }
            for section in parent["sections"]
        ]
        docs[doc_id] = {
            "doc_id": doc_id,
            "doc_type": parent["doc_type"],
            "title": parent["title"],
            "recorded_at": parent["recorded_at"],
            "closed_at": parent.get("closed_at"),
            "policy_id": parent.get("policy_id"),
            "version": parent.get("version"),
            "effective_from": parent.get("effective_from"),
            "effective_to": parent.get("effective_to"),
            "jsonl_file": parent["jsonl_file"],
            "jsonl_row": parent["jsonl_row"],
            "matched_chunk_ids": [row["chunk_id"]],
            "sections": sections,
        }
    return list(docs.values())


def _collect_corpus_context(
    dataset_id: str,
    corpus_id: str,
    facts: Dict[str, Any],
    as_of: str,
    question: Optional[str],
    search_mode: str,
    embed_fn,
    embedding_model: Optional[str],
    scheme: Optional[str] = None,
    rerank_fn=None,
) -> Dict[str, Any]:
    lines = facts.get("lines", [])
    line_ids = [line["line_id"] for line in lines] or None
    description = question.strip() if question and question.strip() else _default_description(facts)

    reader = db.get_reader_conn()
    try:
        with reader.cursor() as cur:
            events = _fetch_corpus_events(cur, corpus_id, facts["order_id"], line_ids, as_of)
    finally:
        reader.close()

    merged = _search_pools(
        dataset_id, corpus_id, description, lines, as_of, search_mode, embed_fn, embedding_model,
        scheme=scheme, rerank_fn=rerank_fn,
    )
    history_rows = sorted(merged["case"].values(), key=lambda r: (r["rank"], r["chunk_id"]))
    rule_rows = sorted(merged["sop"].values(), key=lambda r: (r["rank"], r["chunk_id"]))
    history_candidates = len({r["doc_id"] for r in history_rows})
    rule_candidates = len({r["doc_id"] for r in rule_rows})

    history_docs = _readback_docs(corpus_id, history_rows[:HISTORY_TOP], as_of)
    rule_docs = _readback_docs(corpus_id, rule_rows[:RULE_TOP], as_of)
    rule_docs = [doc for doc in rule_docs if doc.get("doc_type") == "sop"]

    truncation: Dict[str, Any] = {}
    if history_candidates > len(history_docs):
        truncation["history_truncated"] = history_candidates - len(history_docs)
    if rule_candidates > len(rule_docs):
        truncation["rules_truncated"] = rule_candidates - len(rule_docs)
    if len(history_docs) + len(rule_docs) > MAX_CONTEXT_DOCS:
        overflow = len(history_docs) + len(rule_docs) - MAX_CONTEXT_DOCS
        drop_n = min(overflow, len(history_docs))
        truncation["history_dropped_for_budget"] = drop_n
        history_docs = history_docs[: len(history_docs) - drop_n]

    return {
        "corpus_id": corpus_id,
        "question": question,
        "retrieval_description": description,
        "events": events,
        "history_refs": history_docs,
        "applicable_rules": rule_docs,
        "candidates": {"history": history_candidates, "rules": rule_candidates},
        "readback": {
            "history_docs": [doc["doc_id"] for doc in history_docs],
            "rule_docs": [doc["doc_id"] for doc in rule_docs],
        },
        "truncation": truncation,
    }


def investigate(
    dataset_id: str,
    order_id: str,
    line_id: Optional[str] = None,
    as_of: Optional[Any] = None,
    conn: Optional[psycopg.Connection] = None,
    corpus: Optional[str] = None,
    question: Optional[str] = None,
    generation_client: Optional[explain.GenerationClient] = None,
    search_mode: str = "rrf",
    embed_fn=None,
    embedding_model: Optional[str] = None,
    ranker: str = "rrf",
    scheme: Optional[str] = None,
    rerank_fn=None,
) -> Dict[str, Any]:
    """Investigate an order (or one of its lines) and produce a fact/evidence package.

    Without ``corpus`` this is the original exact path (facts + direct evidence).
    With ``corpus`` it additionally reads this order's corpus events exactly,
    searches history cases and valid SOPs in separate pools, reads back parent
    documents, generates with the real model, and binds citations and suggestion
    rule references programmatically.
    """
    own_conn = conn is None
    if own_conn:
        conn = db.get_reader_conn()
    try:
        facts = reconcile(
            dataset_id, order_id, line_id=line_id, as_of=_normalize_as_of(as_of), conn=conn
        )

        with conn.cursor() as cur:
            source_filename = _fetch_source_filename(cur, dataset_id)
            evidence = _collect_evidence(cur, dataset_id, facts, source_filename)

        line_evidence_map: Dict[str, List[str]] = {}
        for line in facts.get("lines", []):
            key = f"{line['order_id']}/{line['line_id']}"
            line_evidence_map[key] = list(line.get("evidence_ids", []))

        if corpus is None:
            return {
                "dataset_id": dataset_id,
                "order_id": order_id,
                "line_id": line_id,
                "as_of": facts["as_of"],
                "source_filename": source_filename,
                "facts": facts,
                "evidence": evidence,
                "line_evidence_map": line_evidence_map,
            }

        started = time.time()
        effective_scheme = scheme if scheme is not None else (
            "rrf+rerank" if ranker == "rrf+rerank" else None
        )
        if effective_scheme is not None and effective_scheme not in (
            "phrase", "vector", "rrf", "rrf+rerank"
        ):
            raise InvestigateError(f"不支持的检索方案: {effective_scheme}")
        model_used = embedding_model
        embed_usage = None
        rerank_stats = None
        if effective_scheme == "rrf+rerank" and rerank_fn is None:
            from .rerank import get_rerank_client

            rerank_client = get_rerank_client()
            rerank_stats = {"calls": 0, "total_tokens": 0, "latency_ms": []}

            def rerank_fn(query: str, documents, _client=rerank_client, _stats=rerank_stats):
                call_started = time.perf_counter()
                response = _client.rerank(query, documents)
                _stats["calls"] += 1
                _stats["latency_ms"].append((time.perf_counter() - call_started) * 1000)
                usage = response.get("usage") or {}
                _stats["total_tokens"] += usage.get("total_tokens") or 0
                return response

        if search_mode in ("vector", "rrf") and embed_fn is None:
            embed_fn, model_used, embed_usage = _get_embedder_with_usage(embedding_model)

        retrieval_started = time.time()
        extension_status = "ok"
        extension_error = None
        context: Optional[Dict[str, Any]] = None
        try:
            context = _collect_corpus_context(
                dataset_id,
                corpus,
                facts,
                facts["as_of"],
                question,
                search_mode,
                embed_fn,
                model_used,
                scheme=effective_scheme,
                rerank_fn=rerank_fn,
            )
        except CorpusIndexNotReadyError as e:
            extension_status, extension_error = "not_indexed", str(e)
        except CorpusError as e:
            extension_status, extension_error = "not_available", str(e)
        except RetrievalError as e:
            extension_status, extension_error = "retrieval_failed", str(e)
        except Exception as e:
            extension_status, extension_error = "retrieval_failed", f"{type(e).__name__}: {e}"
        retrieval_elapsed = time.time() - retrieval_started

        explanation = explain.explain(
            facts,
            evidence,
            client=generation_client,
            as_of=facts["as_of"],
            corpus_context=context if extension_status == "ok" else None,
        )

        return {
            "dataset_id": dataset_id,
            "order_id": order_id,
            "line_id": line_id,
            "as_of": facts["as_of"],
            "source_filename": source_filename,
            "corpus_id": corpus,
            "facts": facts,
            "evidence": evidence,
            "line_evidence_map": line_evidence_map,
            "event_evidence": context["events"] if context else [],
            "history_refs": context["history_refs"] if context else [],
            "applicable_rules": context["applicable_rules"] if context else [],
            "retrieval_description": (
                context["retrieval_description"] if context else (question or "")
            ),
            "extension_status": extension_status,
            "extension_error": extension_error,
            "explanation": explanation,
            "suggestions": explanation.get("suggestions", []),
            "call_record": {
                "corpus_id": corpus,
                "dataset_id": dataset_id,
                "as_of": facts["as_of"],
                "search_mode": search_mode,
                "embedding_model": model_used,
                "embedding_usage": embed_usage,
                "scheme": effective_scheme,
                "rerank": (
                    {
                        "calls": rerank_stats["calls"],
                        "total_tokens": rerank_stats["total_tokens"],
                        "latency_ms": {
                            "count": len(rerank_stats["latency_ms"]),
                            "p50": round(sorted(rerank_stats["latency_ms"])[len(rerank_stats["latency_ms"]) // 2], 1)
                            if rerank_stats["latency_ms"]
                            else None,
                            "p95": round(
                                sorted(rerank_stats["latency_ms"])[
                                    min(len(rerank_stats["latency_ms"]) - 1, int(0.95 * len(rerank_stats["latency_ms"])))
                                ],
                                1,
                            )
                            if rerank_stats["latency_ms"]
                            else None,
                        },
                    }
                    if rerank_stats
                    else None
                ),
                "generation_usage": explanation.get("usage"),
                "retrieval_elapsed_sec": round(retrieval_elapsed, 1),
                "total_elapsed_sec": round(time.time() - started, 1),
                "candidates": context["candidates"] if context else {"history": 0, "rules": 0},
                "readback": (
                    context["readback"] if context else {"history_docs": [], "rule_docs": []}
                ),
                "truncation": context["truncation"] if context else {},
            },
        }
    finally:
        if own_conn:
            conn.close()
