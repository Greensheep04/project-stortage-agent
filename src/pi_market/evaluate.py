"""Unified evaluation entry: numeric reconciliation + retrieval Recall@k.

Single implementation shared by the ``evaluate`` CLI subcommand and
``scripts/eval_retrieval.py``. Only this evaluation path may read the answer
file and query-set labels; business subcommands must not.
"""

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import db, evidence, retrieval
from .reconcile import reconcile


class EvaluateError(Exception):
    pass


EXPECTED_QUERYSET_HASHES = {
    "eval_queries.jsonl": "3baf6a833e1ffcf8fcc7deb67e77a45c4a105b43a01e385322eacf057603b95b",
    "dev_queries.jsonl": "e236741a88f8fdbdfcefc46f880d16d810d2faeea15dc2a0992e008f9fcc0542",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_queryset(path: Path) -> None:
    actual = sha256_file(path)
    if actual not in EXPECTED_QUERYSET_HASHES.values():
        raise EvaluateError(f"查询集哈希校验失败: {path.name} 实际 {actual}")


def load_queries(path: Path) -> List[Dict[str, Any]]:
    queries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            queries.append(json.loads(line))
    return queries


def _recall_at_k(results: List[Dict[str, Any]], relevant: List[str], k: int) -> float:
    if not relevant:
        return 0.0
    returned_parents = {r["parent_id"] for r in results[:k]}
    hits = len(set(relevant) & returned_parents)
    return hits / len(relevant)


def evaluate_mode(
    dataset_id: str,
    queries: List[Dict[str, Any]],
    mode: str,
    k: int,
    embedding_model: Optional[str] = None,
    as_of: Optional[str] = None,
) -> Dict[str, Any]:
    total_queries = len(queries)
    positive_total = sum(1 for q in queries if q.get("relevant_parent_ids"))
    unsupported_total = total_queries - positive_total

    def _precheck_failure(precheck: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "mode": mode,
            "k": k,
            "embedding_model": embedding_model,
            "status": "failed",
            "total_queries": total_queries,
            "success_count": 0,
            "failure_count": total_queries,
            "positive": {"total": positive_total, "success": 0, "failed": positive_total},
            "unsupported": {
                "total": unsupported_total,
                "success": 0,
                "failed": unsupported_total,
                "success_no_results": 0,
            },
            "macro_recall@k": 0.0 if positive_total > 0 else None,
            "avg_latency_ms": None,
            "embedding_call_count": 0,
            "precheck": precheck,
            "errors": [
                {"qid": q["qid"], "status": precheck["status"], "error": precheck["reason"]}
                for q in queries
            ],
            "per_query": [],
            "unsupported_queries": [],
        }

    # Resolve model identifier up front so readiness checks are unambiguous.
    if embedding_model is None:
        try:
            _, embedding_model = retrieval.get_embedder()
        except retrieval.RetrievalError:
            embedding_model = "text-embedding-v4"

    # Pre-flight index readiness check.
    index_status = evidence.get_index_status(dataset_id, embedding_model)
    if mode == "phrase" and not index_status["chunk_ready"]:
        return _precheck_failure(
            {
                "status": "index_not_ready",
                "reason": "索引未就绪：该数据集尚未建立 chunk 索引",
                "index_status": index_status,
            }
        )
    if mode in ("vector", "rrf") and not index_status["vector_ready_all"]:
        return _precheck_failure(
            {
                "status": "index_not_ready",
                "reason": "索引未就绪：向量嵌入尚未全部填充完成",
                "index_status": index_status,
            }
        )

    embed_fn: Optional[Callable[[str], List[float]]] = None
    if mode in ("vector", "rrf"):
        try:
            embed_fn, _ = retrieval.get_embedder(embedding_model)
        except retrieval.RetrievalError as e:
            return _precheck_failure({"status": "embedding_unavailable", "reason": str(e)})

    per_query = []
    unsupported_reports = []
    errors = []
    recalls = []
    positive_success = 0
    unsupported_success = 0
    unsupported_no_results = 0
    total_latencies = []
    call_count = 0

    for q in queries:
        qid = q["qid"]
        query_text = q["query"]
        relevant = q.get("relevant_parent_ids", [])

        start = time.perf_counter()
        try:
            result = retrieval.search(
                dataset_id,
                query_text,
                mode=mode,
                k=k,
                as_of=as_of,
                embed_fn=embed_fn,
                embedding_model=embedding_model,
            )
            latency = time.perf_counter() - start
            total_latencies.append(latency)
            if mode in ("vector", "rrf"):
                call_count += 1

            top = result["results"]
            returned_parent_ids = [r["parent_id"] for r in top]

            if relevant:
                positive_success += 1
                recall = _recall_at_k(top, relevant, k)
                recalls.append(recall)
                per_query.append(
                    {
                        "qid": qid,
                        "query": query_text,
                        "status": "ok",
                        "recall@k": recall,
                        "returned_parent_ids": returned_parent_ids,
                        "relevant_parent_ids": relevant,
                        "latency_ms": round(latency * 1000, 3),
                    }
                )
            else:
                unsupported_success += 1
                if not top:
                    unsupported_no_results += 1
                unsupported_reports.append(
                    {
                        "qid": qid,
                        "query": query_text,
                        "status": "ok",
                        "returned_count": len(top),
                        "returned_parent_ids": returned_parent_ids,
                        "disclaimer": result.get("disclaimer"),
                        "latency_ms": round(latency * 1000, 3),
                    }
                )
        except retrieval.IndexNotReadyError as e:
            latency = time.perf_counter() - start
            errors.append({"qid": qid, "status": "index_not_ready", "error": str(e)})
            if relevant:
                recalls.append(0.0)
                per_query.append(
                    {
                        "qid": qid,
                        "query": query_text,
                        "status": "index_not_ready",
                        "index_status": e.detail,
                        "latency_ms": round(latency * 1000, 3),
                    }
                )
            else:
                unsupported_reports.append(
                    {
                        "qid": qid,
                        "query": query_text,
                        "status": "index_not_ready",
                        "error": str(e),
                        "returned_count": 0,
                        "returned_parent_ids": [],
                        "latency_ms": round(latency * 1000, 3),
                    }
                )
        except Exception as e:
            latency = time.perf_counter() - start
            errors.append({"qid": qid, "status": "error", "error": str(e)})
            if relevant:
                recalls.append(0.0)
                per_query.append(
                    {
                        "qid": qid,
                        "query": query_text,
                        "status": "error",
                        "error": str(e),
                        "latency_ms": round(latency * 1000, 3),
                    }
                )
            else:
                unsupported_reports.append(
                    {
                        "qid": qid,
                        "query": query_text,
                        "status": "error",
                        "error": str(e),
                        "returned_count": 0,
                        "returned_parent_ids": [],
                        "latency_ms": round(latency * 1000, 3),
                    }
                )

    success_count = positive_success + unsupported_success
    failure_count = total_queries - success_count
    if failure_count == 0:
        status = "ok"
    elif success_count == 0:
        status = "failed"
    else:
        status = "partial"

    return {
        "mode": mode,
        "k": k,
        "embedding_model": embedding_model,
        "status": status,
        "total_queries": total_queries,
        "success_count": success_count,
        "failure_count": failure_count,
        "positive": {
            "total": positive_total,
            "success": positive_success,
            "failed": positive_total - positive_success,
        },
        "unsupported": {
            "total": unsupported_total,
            "success": unsupported_success,
            "failed": unsupported_total - unsupported_success,
            "success_no_results": unsupported_no_results,
        },
        "macro_recall@k": round(sum(recalls) / len(recalls), 4) if recalls else None,
        "avg_latency_ms": round(sum(total_latencies) / len(total_latencies) * 1000, 3)
        if total_latencies
        else None,
        "embedding_call_count": call_count,
        "errors": errors,
        "per_query": per_query,
        "unsupported_queries": unsupported_reports,
    }


def retrieval_report(
    dataset_id: str,
    queries_path: Path,
    modes: List[str],
    k: int = 10,
    embedding_model: Optional[str] = None,
    as_of: Optional[str] = None,
) -> Dict[str, Any]:
    verify_queryset(queries_path)
    queries = load_queries(queries_path)
    modes_report = [
        evaluate_mode(dataset_id, queries, mode, k, embedding_model, as_of)
        for mode in modes
    ]
    if all(m["status"] == "ok" for m in modes_report):
        overall = "ok"
    elif all(m["status"] == "failed" for m in modes_report):
        overall = "failed"
    else:
        overall = "partial"
    return {
        "dataset_id": dataset_id,
        "queries_file": str(queries_path),
        "queries_sha256": sha256_file(queries_path),
        "k": k,
        "embedding_model": embedding_model,
        "status": overall,
        "modes": modes_report,
    }


def numeric_comparison(
    dataset_id: str,
    answers_path: Path,
    as_of: Optional[Any] = None,
) -> Dict[str, Any]:
    answers = json.loads(Path(answers_path).read_text(encoding="utf-8"))
    cases = answers.get("cases", [])

    effective_as_of = as_of
    if effective_as_of is None and answers.get("as_of"):
        try:
            effective_as_of = datetime.fromisoformat(answers["as_of"])
        except ValueError:
            effective_as_of = None

    mismatches: List[Dict[str, Any]] = []
    conn = db.get_reader_conn()
    try:
        for case in cases:
            order_id = case.get("order_id")
            line_id = case.get("order_line_id")
            try:
                result = reconcile(
                    dataset_id, order_id, line_id=line_id, as_of=effective_as_of, conn=conn
                )
                line = result["lines"][0]
            except Exception as e:
                mismatches.append({"order_id": order_id, "line_id": line_id, "error": str(e)})
                continue

            differences: Dict[str, Any] = {}
            for field in (
                "expected_quantity",
                "matched_net_shipped_quantity",
                "difference_quantity",
            ):
                actual_field = "planned_quantity" if field == "expected_quantity" else field
                if case.get(field) != line[actual_field]:
                    differences[field] = {
                        "expected": case.get(field),
                        "actual": line[actual_field],
                    }

            expected_finding = case.get("expected_finding")
            if expected_finding not in line["status_tags"]:
                differences["expected_finding"] = {
                    "expected": expected_finding,
                    "actual": line["status_tags"],
                }

            if differences:
                mismatches.append(
                    {"order_id": order_id, "line_id": line_id, "differences": differences}
                )
    finally:
        conn.close()

    return {
        "answers_file": str(answers_path),
        "answers_sha256": sha256_file(Path(answers_path)),
        "cases": len(cases),
        "matched": len(cases) - len(mismatches),
        "mismatches": mismatches,
    }


def run_evaluation(
    dataset_id: str,
    answers_path: Path,
    queries_path: Path,
    k: int = 10,
    embedding_model: Optional[str] = None,
    as_of: Optional[str] = None,
) -> Dict[str, Any]:
    verify_queryset(Path(queries_path))
    return {
        "dataset_id": dataset_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "numeric": numeric_comparison(dataset_id, answers_path, as_of=as_of),
        "retrieval": retrieval_report(
            dataset_id,
            Path(queries_path),
            ["phrase", "vector", "rrf"],
            k=k,
            embedding_model=embedding_model,
            as_of=as_of,
        ),
    }
