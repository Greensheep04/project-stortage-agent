"""T-006a 语料过滤检索与父文档回读（L3 v0.3 D10）。

过滤（时间/类型/适用范围）在 SQL 中先于词面/向量排名与 Top-k 截断完成。
"""

import os
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import psycopg
from dotenv import load_dotenv

from . import config, db
from .corpus import CorpusError
from .retrieval import (
    RetrievalError,
    _parse_as_of,
    _resolve_as_of,
    get_embedder,
    phrase_score,
)

_TZ = "Asia/Shanghai"


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=ZoneInfo(_TZ))
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return _parse_as_of(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo(_TZ))
    raise RetrievalError(f"无法解析截止时间: {value}")


def _effective_as_of(dataset_id: str, as_of: Any) -> datetime:
    return _resolve_as_of(dataset_id, _parse_time(as_of) if as_of is not None else None)

DOC_TYPES = ("event", "case", "sop")

_SELECT_COLS = """
    c.chunk_id, c.doc_id, c.section_id, c.heading, c.content,
    d.doc_type, d.title, d.recorded_at, d.occurred_at, d.closed_at,
    d.effective_from, d.effective_to, d.policy_id, d.version,
    d.scope_warehouse, d.scope_sku, d.scope_order_id, d.scope_line_id,
    d.jsonl_file, d.jsonl_row
"""


class CorpusIndexNotReadyError(CorpusError):
    def __init__(self, message: str, detail: Dict[str, Any]):
        super().__init__(message)
        self.detail = detail


def _where(
    corpus_id: str,
    doc_type: str,
    as_of: datetime,
    scope: Tuple[Optional[str], Optional[str], Optional[str], Optional[str]],
) -> Tuple[str, List[Any]]:
    clauses = ["c.corpus_id = %s", "d.doc_type = %s"]
    params: List[Any] = [corpus_id, doc_type]
    if doc_type == "event":
        clauses.append("d.occurred_at <= %s AND d.recorded_at <= %s")
        params += [as_of, as_of]
    elif doc_type == "case":
        clauses.append("d.closed_at <= %s AND d.recorded_at <= %s")
        params += [as_of, as_of]
    else:
        clauses.append(
            "d.recorded_at <= %s AND d.effective_from <= %s"
            " AND (d.effective_to IS NULL OR %s < d.effective_to)"
        )
        params += [as_of, as_of, as_of]

    warehouse, sku, order_id, line_id = scope
    if doc_type == "event":
        for column, value in (
            ("d.scope_warehouse", warehouse),
            ("d.scope_sku", sku),
            ("d.scope_order_id", order_id),
            ("d.scope_line_id", line_id),
        ):
            if value:
                clauses.append(f"{column} = %s")
                params.append(value)
    else:
        if warehouse:
            clauses.append("(d.scope_warehouse = %s OR d.scope_warehouse IS NULL)")
            params.append(warehouse)
        if sku:
            clauses.append("(d.scope_sku = %s OR d.scope_sku IS NULL)")
            params.append(sku)
    return " AND ".join(clauses), params


def _rows_to_dicts(cur: psycopg.Cursor) -> List[Dict[str, Any]]:
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _phrase_pool(
    cur, corpus_id, doc_type, as_of, query, k, scope
) -> List[Dict[str, Any]]:
    where, params = _where(corpus_id, doc_type, as_of, scope)
    cur.execute(
        f"SELECT {_SELECT_COLS} FROM corpus_chunk c"
        f" JOIN corpus_doc d USING (corpus_id, doc_id)"
        f" WHERE {where} ORDER BY c.chunk_id",
        params,
    )
    rows = _rows_to_dicts(cur)
    for row in rows:
        row["score"] = phrase_score(query, row["content"])
    hits = [row for row in rows if row["score"] > 0]
    hits.sort(key=lambda r: (-r["score"], r["chunk_id"]))
    return hits[:k]


def _vector_pool(
    cur, corpus_id, doc_type, as_of, query, k, embed_fn, scope
) -> List[Dict[str, Any]]:
    qvec = embed_fn(query)
    if len(qvec) != 1024:
        raise RetrievalError(f"查询向量维度错误: 期望 1024, 实际 {len(qvec)}")
    where, params = _where(corpus_id, doc_type, as_of, scope)
    cur.execute(
        f"SELECT {_SELECT_COLS}, c.embedding <=> %s::vector AS distance"
        f" FROM corpus_chunk c JOIN corpus_doc d USING (corpus_id, doc_id)"
        f" WHERE {where} AND c.embedding IS NOT NULL"
        f" ORDER BY distance, c.chunk_id LIMIT %s",
        [qvec] + params + [k],
    )
    results = _rows_to_dicts(cur)
    for row in results:
        row["score"] = 1.0 - row["distance"]
    return results


def _rrf_pool(
    cur, corpus_id, doc_type, as_of, query, k, embed_fn, scope, rrf_k=60, candidate_factor=10
) -> List[Dict[str, Any]]:
    pool_size = max(k * candidate_factor, 100)
    phrase = _phrase_pool(cur, corpus_id, doc_type, as_of, query, pool_size, scope)
    vector = _vector_pool(cur, corpus_id, doc_type, as_of, query, pool_size, embed_fn, scope)

    scores: Dict[str, float] = {}
    info: Dict[str, Dict[str, Any]] = {}
    for rank, row in enumerate(phrase, start=1):
        cid = row["chunk_id"]
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (rank + rrf_k)
        info[cid] = row
    for rank, row in enumerate(vector, start=1):
        cid = row["chunk_id"]
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (rank + rrf_k)
        info.setdefault(cid, row)
    ranked = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
    return [{**info[cid], "rrf_score": score} for cid, score in ranked[:k]]


def _check_ready(
    cur,
    corpus_id: str,
    dataset_id: str,
    require_vectors: bool,
    embedding_model: Optional[str],
) -> None:
    cur.execute("SELECT dataset_id, status FROM corpus WHERE corpus_id = %s", (corpus_id,))
    row = cur.fetchone()
    if row is None:
        raise CorpusError(f"语料不存在: {corpus_id}")
    if row[1] != "ready":
        raise CorpusError(f"语料未发布: {corpus_id} 状态={row[1]}")
    if row[0] != dataset_id:
        raise CorpusError(f"语料 {corpus_id} 绑定数据集 {row[0]}，与请求 {dataset_id} 不匹配")

    if require_vectors:
        cur.execute(
            "SELECT COUNT(*), COUNT(embedding) FROM corpus_chunk"
            " WHERE corpus_id = %s AND embedding_model = %s",
            (corpus_id, embedding_model),
        )
        total, vectors = cur.fetchone()
        total, vectors = total or 0, vectors or 0
        detail = {
            "corpus_id": corpus_id,
            "embedding_model": embedding_model,
            "chunks": total,
            "vector_ready": vectors,
        }
        if total == 0:
            raise CorpusIndexNotReadyError("索引未就绪：语料尚未建立 chunk 索引", detail)
        if vectors < total:
            raise CorpusIndexNotReadyError("索引未就绪：向量嵌入尚未全部填充完成", detail)
    else:
        cur.execute("SELECT COUNT(*) FROM corpus_chunk WHERE corpus_id = %s", (corpus_id,))
        if (cur.fetchone()[0] or 0) == 0:
            raise CorpusIndexNotReadyError(
                "索引未就绪：语料尚未建立 chunk 索引",
                {"corpus_id": corpus_id, "chunks": 0},
            )


def _resolve_embedder(
    mode: str,
    embed_fn: Optional[Callable[[str], List[float]]],
    embedding_model: Optional[str],
) -> Tuple[Optional[Callable[[str], List[float]]], Optional[str]]:
    real_embed_fn = embed_fn
    if embedding_model is None:
        if mode == "phrase":
            load_dotenv(str(config._ENV_PATH), override=False)
            embedding_model = os.getenv("EMBEDDING_MODEL", "text-embedding-v4")
        elif embed_fn is not None:
            raise RetrievalError("显式提供 embed_fn 时也必须提供 embedding_model")
        else:
            real_embed_fn, embedding_model = get_embedder(embedding_model)
    if mode in {"vector", "rrf"} and real_embed_fn is None:
        real_embed_fn, detected_model = get_embedder(embedding_model)
        if embedding_model is None:
            embedding_model = detected_model
    return real_embed_fn, embedding_model


def rank_parents(
    dataset_id: str,
    corpus_id: str,
    query: str,
    doc_type: str,
    as_of: Optional[Any] = None,
    mode: str = "rrf",
    candidate_depth: int = 100,
    parent_top: int = 10,
    embed_fn: Optional[Callable[[str], List[float]]] = None,
    embedding_model: Optional[str] = None,
    warehouse: Optional[str] = None,
    sku: Optional[str] = None,
    rrf_k: int = 60,
    rerank_fn: Optional[Callable[[str, List[str]], Dict[str, Any]]] = None,
    rerank_candidates: int = 50,
) -> Dict[str, Any]:
    """Rank parent documents from filtered chunk candidates (eval/rerank shared).

    phrase/vector each take up to ``candidate_depth`` filtered chunks; rrf fuses
    the two rankings (1/(rank+rrf_k)) before parent dedup. ``rrf+rerank`` reranks
    the top ``rerank_candidates`` RRF parents with ``rerank_fn`` (index maps back
    to the original candidates; explicit failures never silently fall back).
    """
    if not query or not query.strip():
        raise RetrievalError("查询描述不能为空")
    if mode not in {"phrase", "vector", "rrf", "rrf+rerank"}:
        raise RetrievalError(f"不支持的检索模式: {mode}")
    if doc_type not in DOC_TYPES:
        raise RetrievalError(f"不支持的资料类型: {doc_type}")

    base_mode = "rrf" if mode == "rrf+rerank" else mode
    target_parent_top = (
        max(parent_top, rerank_candidates) if mode == "rrf+rerank" else parent_top
    )
    effective_as_of = _effective_as_of(dataset_id, as_of)
    real_embed_fn, embedding_model = _resolve_embedder(base_mode, embed_fn, embedding_model)
    scope = (warehouse, sku, None, None)

    chunks: List[Dict[str, Any]] = []
    ranks: Dict[str, Dict[str, Optional[int]]] = {}
    candidate_counts: Dict[str, int] = {}

    with db.get_reader_conn() as conn:
        with conn.cursor() as cur:
            _check_ready(
                cur, corpus_id, dataset_id, base_mode in {"vector", "rrf"}, embedding_model
            )
            if base_mode == "phrase":
                chunks = _phrase_pool(
                    cur, corpus_id, doc_type, effective_as_of, query, candidate_depth, scope
                )
                candidate_counts["phrase"] = len(chunks)
                ranks = {row["chunk_id"]: {"phrase": i} for i, row in enumerate(chunks, 1)}
            elif base_mode == "vector":
                chunks = _vector_pool(
                    cur, corpus_id, doc_type, effective_as_of, query, candidate_depth,
                    real_embed_fn, scope,
                )
                candidate_counts["vector"] = len(chunks)
                ranks = {row["chunk_id"]: {"vector": i} for i, row in enumerate(chunks, 1)}
            else:
                phrase = _phrase_pool(
                    cur, corpus_id, doc_type, effective_as_of, query, candidate_depth, scope
                )
                vector = _vector_pool(
                    cur, corpus_id, doc_type, effective_as_of, query, candidate_depth,
                    real_embed_fn, scope,
                )
                candidate_counts["phrase"] = len(phrase)
                candidate_counts["vector"] = len(vector)
                scores: Dict[str, float] = {}
                info: Dict[str, Dict[str, Any]] = {}
                for rank, row in enumerate(phrase, start=1):
                    cid = row["chunk_id"]
                    scores[cid] = scores.get(cid, 0.0) + 1.0 / (rank + rrf_k)
                    info[cid] = row
                    ranks.setdefault(cid, {})["phrase"] = rank
                for rank, row in enumerate(vector, start=1):
                    cid = row["chunk_id"]
                    scores[cid] = scores.get(cid, 0.0) + 1.0 / (rank + rrf_k)
                    info.setdefault(cid, row)
                    ranks.setdefault(cid, {})["vector"] = rank
                fused = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
                chunks = [{**info[cid], "rrf_score": score} for cid, score in fused]
                for i, (cid, _) in enumerate(fused, start=1):
                    ranks[cid]["rrf"] = i

    parents: List[Dict[str, Any]] = []
    seen = set()
    for row in chunks:
        doc_id = row["doc_id"]
        if doc_id in seen:
            continue
        seen.add(doc_id)
        parents.append(
            {
                **_serialize(row),
                "rank": len(parents) + 1,
                "representative_chunk_id": row["chunk_id"],
                "representative_section_id": row["section_id"],
                "chunk_rank": ranks.get(row["chunk_id"], {}),
            }
        )
        if len(parents) >= target_parent_top:
            break

    result = {
        "dataset_id": dataset_id,
        "corpus_id": corpus_id,
        "query": query,
        "doc_type": doc_type,
        "mode": mode,
        "as_of": effective_as_of.isoformat(),
        "embedding_model": embedding_model,
        "candidate_depth": candidate_depth,
        "candidate_counts": candidate_counts,
        "parents": parents,
    }
    if mode == "rrf+rerank":
        reranked, meta = _rerank_parents(result, rerank_fn)
        result["parents"] = reranked[:parent_top]
        result["rerank"] = meta
    return result


def _rerank_parents(
    base: Dict[str, Any],
    rerank_fn: Optional[Callable[[str, List[str]], Dict[str, Any]]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Rerank parent representative chunks; index maps back to the candidates."""
    parents = base["parents"]
    if not parents:
        return [], {"called": False, "reason": "empty_pool", "candidates": 0}
    if rerank_fn is None:
        raise RetrievalError("rrf+rerank 需要 rerank_fn（显式失败不静默回退 RRF）")
    documents = [p["content"] for p in parents]
    response = rerank_fn(base["query"], documents)
    results = response.get("results", []) if isinstance(response, dict) else response
    returned = {item["index"] for item in results}
    if len(returned) != len(parents):
        raise RetrievalError(
            f"rerank 返回结果缺失: {len(returned)}/{len(parents)}（不静默回退）"
        )
    order: List[Dict[str, Any]] = []
    for item in results:
        parent = parents[item["index"]]
        order.append(
            {
                **parent,
                "rerank_score": item["relevance_score"],
                "rrf_rank": parent["rank"],
            }
        )
    for i, parent in enumerate(order, start=1):
        parent["rank"] = i
    meta = {
        "called": True,
        "candidates": len(parents),
        "returned": len(results),
        "usage": response.get("usage") if isinstance(response, dict) else None,
    }
    return order, meta


def _serialize(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "chunk_id": row["chunk_id"],
        "doc_id": row["doc_id"],
        "doc_type": row["doc_type"],
        "title": row["title"],
        "section_id": row["section_id"],
        "heading": row["heading"],
        "content": row["content"],
        "recorded_at": row["recorded_at"].isoformat(),
        "occurred_at": row["occurred_at"].isoformat() if row["occurred_at"] else None,
        "closed_at": row["closed_at"].isoformat() if row["closed_at"] else None,
        "effective_from": row["effective_from"].isoformat() if row["effective_from"] else None,
        "effective_to": row["effective_to"].isoformat() if row["effective_to"] else None,
        "policy_id": row["policy_id"],
        "version": row["version"],
        "scope_warehouse": row["scope_warehouse"],
        "scope_sku": row["scope_sku"],
        "scope_order_id": row["scope_order_id"],
        "scope_line_id": row["scope_line_id"],
        "jsonl_file": row["jsonl_file"],
        "jsonl_row": row["jsonl_row"],
        "score": row.get("score"),
        "distance": row.get("distance"),
        "rrf_score": row.get("rrf_score"),
    }


def search_corpus(
    dataset_id: str,
    corpus_id: str,
    query: str,
    doc_types: Optional[Sequence[str]] = None,
    k: int = 10,
    mode: str = "rrf",
    as_of: Optional[Any] = None,
    embed_fn: Optional[Callable[[str], List[float]]] = None,
    embedding_model: Optional[str] = None,
    warehouse: Optional[str] = None,
    sku: Optional[str] = None,
    order_id: Optional[str] = None,
    line_id: Optional[str] = None,
    scheme: Optional[str] = None,
    rerank_fn: Optional[Callable[[str, List[str]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Search one corpus with filters applied before ranking and Top-k.

    Without ``scheme`` this is the legacy chunk-level path (``mode``).
    With an explicit ``scheme`` (phrase/vector/rrf/rrf+rerank) it uses the same
    parent-level ranking as the evaluation (100 chunks per route, parent dedup
    by first chunk, then Top-k); ``rrf+rerank`` reranks the top-50 RRF parents
    with ``rerank_fn`` and explicit failures never silently fall back to RRF.
    """
    if not query or not query.strip():
        raise RetrievalError("查询描述不能为空")
    if mode not in {"phrase", "vector", "rrf"}:
        raise RetrievalError(f"不支持的检索模式: {mode}")
    if scheme is not None and scheme not in {"phrase", "vector", "rrf", "rrf+rerank"}:
        raise RetrievalError(f"不支持的检索方案: {scheme}")
    types = tuple(doc_types) if doc_types else DOC_TYPES
    for t in types:
        if t not in DOC_TYPES:
            raise RetrievalError(f"不支持的资料类型: {t}")

    effective_as_of = _effective_as_of(dataset_id, as_of)

    if scheme is not None:
        pools: Dict[str, List[Dict[str, Any]]] = {}
        model_used = embedding_model
        for doc_type in types:
            ranked = rank_parents(
                dataset_id,
                corpus_id,
                query,
                doc_type,
                as_of=effective_as_of,
                mode=scheme,
                candidate_depth=100,
                parent_top=k,
                embed_fn=embed_fn,
                embedding_model=embedding_model,
                warehouse=warehouse,
                sku=sku,
                rerank_fn=rerank_fn,
            )
            model_used = ranked.get("embedding_model") or model_used
            pools[doc_type] = ranked["parents"]
        matched_orders = sorted(
            {r["scope_order_id"] for pool in pools.values() for r in pool if r["scope_order_id"]}
        )
        return {
            "dataset_id": dataset_id,
            "corpus_id": corpus_id,
            "query": query,
            "mode": mode,
            "scheme": scheme,
            "k": k,
            "as_of": effective_as_of.isoformat(),
            "embedding_model": model_used,
            "disclaimer": "Top-k 结果为相关样本，不代表完整统计。",
            "orders_matched": matched_orders,
            "needs_disambiguation": order_id is None and len(matched_orders) > 1,
            "pools": pools,
        }

    real_embed_fn, embedding_model = _resolve_embedder(mode, embed_fn, embedding_model)

    scope = (warehouse, sku, order_id, line_id)
    pools: Dict[str, List[Dict[str, Any]]] = {}
    with db.get_reader_conn() as conn:
        with conn.cursor() as cur:
            _check_ready(cur, corpus_id, dataset_id, mode in {"vector", "rrf"}, embedding_model)
            for doc_type in types:
                if mode == "phrase":
                    rows = _phrase_pool(cur, corpus_id, doc_type, effective_as_of, query, k, scope)
                elif mode == "vector":
                    rows = _vector_pool(
                        cur, corpus_id, doc_type, effective_as_of, query, k, real_embed_fn, scope
                    )
                else:
                    rows = _rrf_pool(
                        cur, corpus_id, doc_type, effective_as_of, query, k, real_embed_fn, scope
                    )
                pools[doc_type] = [
                    {"rank": i + 1, **_serialize(row)} for i, row in enumerate(rows)
                ]

    matched_orders = sorted(
        {r["scope_order_id"] for pool in pools.values() for r in pool if r["scope_order_id"]}
    )
    return {
        "dataset_id": dataset_id,
        "corpus_id": corpus_id,
        "query": query,
        "mode": mode,
        "scheme": None,
        "k": k,
        "as_of": effective_as_of.isoformat(),
        "embedding_model": embedding_model,
        "disclaimer": "Top-k 结果为相关样本，不代表完整统计。",
        "orders_matched": matched_orders,
        "needs_disambiguation": order_id is None and len(matched_orders) > 1,
        "pools": pools,
    }


def _scope_ok(
    doc: Dict[str, Any],
    scope: Tuple[Optional[str], Optional[str], Optional[str], Optional[str]],
) -> bool:
    warehouse, sku, order_id, line_id = scope
    if doc["doc_type"] == "event":
        if order_id and doc["scope_order_id"] != order_id:
            return False
        if line_id and doc["scope_line_id"] != line_id:
            return False
        if warehouse and doc["scope_warehouse"] != warehouse:
            return False
        if sku and doc["scope_sku"] != sku:
            return False
        return True
    if warehouse and doc["scope_warehouse"] not in (None, warehouse):
        return False
    if sku and doc["scope_sku"] not in (None, sku):
        return False
    return True


def _doc_visible(
    doc: Dict[str, Any],
    as_of: datetime,
    scope: Tuple[Optional[str], Optional[str], Optional[str], Optional[str]],
) -> bool:
    doc_type = doc["doc_type"]
    if doc_type == "event":
        if doc["occurred_at"] > as_of or doc["recorded_at"] > as_of:
            return False
    elif doc_type == "case":
        if doc["closed_at"] > as_of or doc["recorded_at"] > as_of:
            return False
    else:
        if doc["recorded_at"] > as_of or doc["effective_from"] > as_of:
            return False
        if doc["effective_to"] is not None and not as_of < doc["effective_to"]:
            return False
    return _scope_ok(doc, scope)


_DOC_READ_COLS = (
    "doc_id", "doc_type", "title", "recorded_at", "occurred_at", "closed_at",
    "effective_from", "effective_to", "policy_id", "version", "scope_warehouse",
    "scope_sku", "scope_order_id", "scope_line_id", "jsonl_file", "jsonl_row", "raw",
)


def _fetch_corpus_doc(cur: psycopg.Cursor, corpus_id: str, doc_id: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        SELECT doc_id, doc_type, title, recorded_at, occurred_at, closed_at,
               effective_from, effective_to, policy_id, version, scope_warehouse,
               scope_sku, scope_order_id, scope_line_id, jsonl_file, jsonl_row, raw
        FROM corpus_doc WHERE corpus_id = %s AND doc_id = %s
        """,
        (corpus_id, doc_id),
    )
    row = cur.fetchone()
    return dict(zip(_DOC_READ_COLS, row)) if row is not None else None


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _version_meta(doc: Dict[str, Any]) -> Dict[str, Any]:
    raw_scope = (doc.get("raw") or {}).get("scope") or {}
    return {
        "doc_id": doc["doc_id"],
        "policy_id": doc["policy_id"],
        "version": doc["version"],
        "title": doc["title"],
        "effective_from": _iso(doc["effective_from"]),
        "effective_to": _iso(doc["effective_to"]),
        "recorded_at": _iso(doc["recorded_at"]),
        "scope": {
            "warehouse": doc["scope_warehouse"],
            "sku": doc["scope_sku"],
            "order_id": doc["scope_order_id"],
            "line_id": doc["scope_line_id"],
        },
        "source": {"file": doc["jsonl_file"], "row": doc["jsonl_row"]},
        "raw_scope": raw_scope,
    }


def read_corpus_doc(
    corpus_id: str,
    doc_id: str,
    as_of: Optional[Any] = None,
    warehouse: Optional[str] = None,
    sku: Optional[str] = None,
    order_id: Optional[str] = None,
    line_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Read a parent document, re-checking time and scope filters at readback."""
    scope = (warehouse, sku, order_id, line_id)
    with db.get_reader_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT dataset_id, status FROM corpus WHERE corpus_id = %s", (corpus_id,))
            corpus = cur.fetchone()
            if corpus is None:
                raise CorpusError(f"语料不存在: {corpus_id}")
            if corpus[1] != "ready":
                raise CorpusError(f"语料未发布: {corpus_id} 状态={corpus[1]}")
            dataset_id = corpus[0]
            effective_as_of = _effective_as_of(dataset_id, as_of)
            doc = _fetch_corpus_doc(cur, corpus_id, doc_id)
    if doc is None:
        raise CorpusError(f"文档不存在: {doc_id}")
    if not _doc_visible(doc, effective_as_of, scope):
        raise CorpusError(f"文档 {doc_id} 在截止时间或适用范围内不可见，回读被拒绝")

    return {
        "corpus_id": corpus_id,
        "dataset_id": dataset_id,
        "as_of": effective_as_of.isoformat(),
        "doc_id": doc["doc_id"],
        "doc_type": doc["doc_type"],
        "title": doc["title"],
        "recorded_at": _iso(doc["recorded_at"]),
        "occurred_at": _iso(doc["occurred_at"]),
        "closed_at": _iso(doc["closed_at"]),
        "effective_from": _iso(doc["effective_from"]),
        "effective_to": _iso(doc["effective_to"]),
        "policy_id": doc["policy_id"],
        "version": doc["version"],
        "jsonl_file": doc["jsonl_file"],
        "jsonl_row": doc["jsonl_row"],
        "sections": doc["raw"]["sections"],
        "raw": doc["raw"],
    }


def read_corpus_doc_history(
    corpus_id: str,
    doc_id: str,
    as_of: Optional[Any] = None,
    warehouse: Optional[str] = None,
    sku: Optional[str] = None,
    order_id: Optional[str] = None,
    line_id: Optional[str] = None,
    max_versions: int = 50,
) -> Dict[str, Any]:
    """R3：读取当前 SOP 及其 ``supersedes`` 祖先链（仅版本元数据）。

    默认回读路径不调用本函数；祖先失效窗口检查仅在此专用路径豁免。
    缺链/循环/范围不符终止并返回 ``complete=false``＋结构化原因；
    数据库错误向调用方抛出。
    """
    scope = (warehouse, sku, order_id, line_id)
    with db.get_reader_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT dataset_id, status FROM corpus WHERE corpus_id = %s", (corpus_id,))
            corpus = cur.fetchone()
            if corpus is None:
                raise CorpusError(f"语料不存在: {corpus_id}")
            if corpus[1] != "ready":
                raise CorpusError(f"语料未发布: {corpus_id} 状态={corpus[1]}")
            dataset_id = corpus[0]
            effective_as_of = _effective_as_of(dataset_id, as_of)
            anchor = _fetch_corpus_doc(cur, corpus_id, doc_id)
            if anchor is None:
                raise CorpusError(f"文档不存在: {doc_id}")
            if not _doc_visible(anchor, effective_as_of, scope):
                raise CorpusError(f"文档 {doc_id} 在截止时间或适用范围内不可见，回读被拒绝")
            policy_id = anchor["policy_id"]
            versions = [_version_meta(anchor)]
            seen = {anchor["doc_id"]}
            current = anchor
            complete = True
            reason: Optional[str] = None
            while True:
                previous_id = (current.get("raw") or {}).get("supersedes")
                if previous_id is None:
                    break
                if previous_id in seen:
                    complete, reason = False, f"沿革链循环: {previous_id}"
                    break
                if len(versions) >= max_versions:
                    complete, reason = False, "沿革链超长"
                    break
                ancestor = _fetch_corpus_doc(cur, corpus_id, previous_id)
                if ancestor is None:
                    complete, reason = False, f"沿革链缺口: {previous_id} 不存在"
                    break
                if ancestor["doc_type"] != "sop" or ancestor["policy_id"] != policy_id:
                    complete, reason = False, "沿革链政策或文档类型不一致"
                    break
                if not _scope_ok(ancestor, scope):
                    complete, reason = False, "祖先版本适用范围不匹配"
                    break
                if ancestor["recorded_at"] > effective_as_of or ancestor["effective_from"] > effective_as_of:
                    complete, reason = False, "祖先版本超出截止时间"
                    break
                if ancestor["effective_to"] is None or not (ancestor["effective_to"] <= effective_as_of):
                    complete, reason = False, "祖先版本尚未失效"
                    break
                versions.append(_version_meta(ancestor))
                seen.add(ancestor["doc_id"])
                current = ancestor

    versions.sort(key=lambda meta: (meta["effective_from"] or "", meta["doc_id"]))
    return {
        "corpus_id": corpus_id,
        "as_of": effective_as_of.isoformat(),
        "policy_id": policy_id,
        "versions": versions,
        "complete": complete,
        "reason": reason,
    }
