"""T-007 统一检索评测入口（D13/D14）。

同一实现编排 phrase/vector/rrf（以及后续 rrf+rerank）四组同口径比较：
硬过滤 → 分池候选（各至多 candidate_depth 块）→ 父文档去重排名 → Top-k。
指标：Recall@10（等级 2 目标）、MRR@5、nDCG@5（gain 2^g−1，理想排名来自完整标签）；
候选 Recall@50 位置留给 T-007b。技术失败的正向题记 0 并保留在分母。
"""

import hashlib
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import corpus_retrieval
from .retrieval import RetrievalError

GROUP_MODES = ("phrase", "vector", "rrf", "rrf+rerank")
CANDIDATE_RECALL_NOTE = "rerank 前候选（本组父文档排名前 50）"


class CorpusEvalError(Exception):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def load_query_set(path: Path) -> List[Dict[str, Any]]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def validate_query_set(queries: List[Dict[str, Any]]) -> List[str]:
    problems: List[str] = []
    for q in queries:
        qid = q.get("qid", "?")
        for field in ("query", "pool", "as_of", "scenario_group", "polarity", "labels"):
            if field not in q:
                problems.append(f"{qid}: 缺字段 {field}")
        if q.get("pool") not in ("case", "sop"):
            problems.append(f"{qid}: pool 必须为 case/sop")
        if q.get("polarity") not in ("positive", "negative"):
            problems.append(f"{qid}: polarity 必须为 positive/negative")
        for label in q.get("labels", []):
            if label.get("grade") not in (0, 1, 2):
                problems.append(f"{qid}: 等级必须为 0/1/2")
            if not label.get("reason"):
                problems.append(f"{qid}: 标签缺理由")
        grade2 = [l for l in q.get("labels", []) if l.get("grade") == 2]
        if q.get("polarity") == "positive":
            if not grade2:
                problems.append(f"{qid}: 正向题缺等级 2")
            if len(grade2) > 10:
                problems.append(f"{qid}: 等级 2 超过 10 篇")
        elif grade2:
            problems.append(f"{qid}: 负向题不应有等级 2")
        if q.get("polarity") == "negative" and not q.get("no_answer_reason"):
            problems.append(f"{qid}: 负向题缺不适用理由")
    return problems


def score_query(
    labels: List[Dict[str, Any]],
    ranked_doc_ids: List[str],
    k_recall: int = 10,
    k_mrr: int = 5,
    k_ndcg: int = 5,
) -> Dict[str, Any]:
    grades = {l["doc_id"]: l["grade"] for l in labels}
    grade2 = [doc for doc, g in grades.items() if g == 2]
    top_recall = ranked_doc_ids[:k_recall]
    hits = [doc for doc in top_recall if grades.get(doc) == 2]
    recall = len(hits) / len(grade2) if grade2 else None

    mrr = 0.0
    for rank, doc in enumerate(ranked_doc_ids[:k_mrr], start=1):
        if grades.get(doc) == 2:
            mrr = 1.0 / rank
            break

    dcg = 0.0
    for rank, doc in enumerate(ranked_doc_ids[:k_ndcg], start=1):
        gain = 2 ** grades.get(doc, 0) - 1
        dcg += gain / math.log2(rank + 1)
    ideal_grades = sorted((g for g in grades.values() if g > 0), reverse=True)[:k_ndcg]
    idcg = sum((2**g - 1) / math.log2(i + 1) for i, g in enumerate(ideal_grades, start=1))
    ndcg = dcg / idcg if idcg > 0 else None

    return {
        "recall@10": recall,
        "mrr@5": mrr,
        "ndcg@5": ndcg,
        "grade2_total": len(grade2),
        "grade2_hits": len(hits),
    }


def _macro(entries: List[Dict[str, Any]], key: str) -> Optional[float]:
    values = [e[key] for e in entries if e.get(key) is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def candidate_recall(labels: List[Dict[str, Any]], ranked_doc_ids: List[str], k: int = 50) -> Optional[float]:
    grades = {l["doc_id"]: l["grade"] for l in labels}
    grade2 = [doc for doc, g in grades.items() if g == 2]
    if not grade2:
        return None
    hits = sum(1 for doc in ranked_doc_ids[:k] if grades.get(doc) == 2)
    return hits / len(grade2)


def _pool_macros(positives: List[Dict[str, Any]]) -> Dict[str, Any]:
    result = {}
    for pool in ("case", "sop", "overall"):
        entries = positives if pool == "overall" else [e for e in positives if e["pool"] == pool]
        result[pool] = {
            "total": len(entries),
            "failed": sum(1 for e in entries if e["status"] != "ok"),
            "recall@10": _macro(entries, "recall@10"),
            "mrr@5": _macro(entries, "mrr@5"),
            "ndcg@5": _macro(entries, "ndcg@5"),
            "candidate_recall@50": _macro(entries, "candidate_recall@50"),
            "candidate_recall@50_note": CANDIDATE_RECALL_NOTE,
        }
    return result


def run_group(
    queries: List[Dict[str, Any]],
    mode: str,
    dataset_id: str,
    corpus_id: str,
    rank_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    rerank_fn: Optional[Callable[[str, List[str]], Dict[str, Any]]] = None,
    embed_fn: Optional[Callable[[str], List[float]]] = None,
    embedding_model: Optional[str] = None,
    candidate_depth: int = 100,
    top_k: int = 10,
) -> Dict[str, Any]:
    if mode not in GROUP_MODES:
        raise CorpusEvalError(f"不支持的组: {mode}")
    rank_fn = rank_fn or corpus_retrieval.rank_parents

    per_query: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for q in queries:
        qid = q["qid"]
        start = time.perf_counter()
        try:
            result = rank_fn(
                dataset_id,
                corpus_id,
                q["query"],
                q["pool"],
                as_of=q["as_of"],
                mode=mode,
                candidate_depth=candidate_depth,
                parent_top=max(top_k, 50),
                embed_fn=embed_fn,
                embedding_model=embedding_model,
                warehouse=(q.get("scope") or {}).get("warehouse_id"),
                sku=(q.get("scope") or {}).get("sku"),
                rerank_fn=rerank_fn,
            )
            latency_ms = round((time.perf_counter() - start) * 1000, 3)
            ranked = [p["doc_id"] for p in result["parents"]]
            label_ids = {l["doc_id"] for l in q["labels"]}
            unjudged = [d for d in ranked if d not in label_ids]
            base_entry = {
                "qid": qid,
                "pool": q["pool"],
                "polarity": q["polarity"],
                "scenario_group": q["scenario_group"],
                "query": q["query"],
                "latency_ms": latency_ms,
                "ranked_doc_ids": ranked[:top_k],
                "candidate_doc_ids": ranked,
                "candidate_counts": result.get("candidate_counts", {}),
                "rerank": result.get("rerank"),
            }
            if unjudged:
                # E1: 未判定的返回候选不得默认当 0；标签不完整时拒绝出成功成绩
                errors.append(
                    {"qid": qid, "status": "labels_incomplete", "unjudged_doc_ids": unjudged}
                )
                per_query.append(
                    {**base_entry, "status": "labels_incomplete", "unjudged_doc_ids": unjudged}
                )
                continue
            entry = {
                **base_entry,
                "status": "ok",
                **score_query(q["labels"], ranked),
                "candidate_recall@50": candidate_recall(q["labels"], ranked),
            }
            if q["polarity"] == "negative":
                entry["no_answer_reason"] = q.get("no_answer_reason")
            per_query.append(entry)
        except Exception as e:
            latency_ms = round((time.perf_counter() - start) * 1000, 3)
            errors.append({"qid": qid, "status": "error", "error": str(e)})
            entry = {
                "qid": qid,
                "pool": q["pool"],
                "polarity": q["polarity"],
                "scenario_group": q["scenario_group"],
                "query": q["query"],
                "status": "error",
                "error": str(e),
                "latency_ms": latency_ms,
                "ranked_doc_ids": [],
            }
            if q["polarity"] == "positive":
                grade2 = [l for l in q["labels"] if l.get("grade") == 2]
                entry.update(
                    {
                        "recall@10": 0.0,
                        "mrr@5": 0.0,
                        "ndcg@5": 0.0,
                        "grade2_total": len(grade2),
                        "grade2_hits": 0,
                    }
                )
            per_query.append(entry)

    return _summarize_group(mode, per_query, errors)


def _summarize_group(
    mode: str, per_query: List[Dict[str, Any]], errors: List[Dict[str, Any]]
) -> Dict[str, Any]:
    positives = [e for e in per_query if e["polarity"] == "positive"]
    negatives = [e for e in per_query if e["polarity"] == "negative"]
    failures = [e for e in per_query if e["status"] != "ok"]
    labels_incomplete = [e for e in per_query if e["status"] == "labels_incomplete"]
    macros = _pool_macros(positives)
    if labels_incomplete:
        status = "labels_incomplete"
        # 标签不完整：拒绝输出可用于验收的成功成绩
        for pool in macros.values():
            for key in ("recall@10", "mrr@5", "ndcg@5", "candidate_recall@50"):
                pool[key] = None
    elif not failures:
        status = "ok"
    elif len(failures) == len(per_query):
        status = "failed"
    else:
        status = "partial"

    return {
        "mode": mode,
        "status": status,
        "positive": macros,
        "negative": {
            "total": len(negatives),
            "failed": sum(1 for e in negatives if e["status"] != "ok"),
            "returned": [
                {
                    "qid": e["qid"],
                    "status": e["status"],
                    "returned_doc_ids": e.get("ranked_doc_ids", []),
                    "no_answer_reason": e.get("no_answer_reason"),
                    "error": e.get("error"),
                }
                for e in negatives
            ],
        },
        "per_query": per_query,
        "errors": errors,
    }


def rescore_report(
    report: Dict[str, Any], queries: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Offline re-score a saved report with (possibly updated) labels.

    Uses the saved candidate rankings; no retrieval or model calls. Entries with
    unjudged candidates become ``labels_incomplete`` and the group refuses to
    output success metrics.
    """
    queries_by_qid = {q["qid"]: q for q in queries}
    new_report = {k: v for k, v in report.items() if k != "groups"}
    new_groups = {}
    for mode, group in report["groups"].items():
        per_query = []
        errors = []
        for entry in group["per_query"]:
            q = queries_by_qid.get(entry["qid"])
            if q is None:
                per_query.append(entry)
                continue
            if entry.get("status") != "ok":
                per_query.append(entry)
                errors.append(
                    {
                        "qid": entry["qid"],
                        "status": entry.get("status"),
                        "error": entry.get("error"),
                        "unjudged_doc_ids": entry.get("unjudged_doc_ids"),
                    }
                )
                continue
            ranked = entry.get("candidate_doc_ids") or entry.get("ranked_doc_ids") or []
            label_ids = {l["doc_id"] for l in q["labels"]}
            unjudged = [d for d in ranked if d not in label_ids]
            base_entry = {k: v for k, v in entry.items() if k not in (
                "recall@10", "mrr@5", "ndcg@5", "grade2_total", "grade2_hits",
                "candidate_recall@50", "status",
            )}
            if unjudged:
                errors.append(
                    {"qid": entry["qid"], "status": "labels_incomplete", "unjudged_doc_ids": unjudged}
                )
                per_query.append(
                    {**base_entry, "status": "labels_incomplete", "unjudged_doc_ids": unjudged}
                )
                continue
            per_query.append(
                {
                    **base_entry,
                    "status": "ok",
                    **score_query(q["labels"], ranked),
                    "candidate_recall@50": candidate_recall(q["labels"], ranked),
                }
            )
        new_groups[mode] = _summarize_group(mode, per_query, errors)
    new_report["groups"] = new_groups
    return new_report


def _percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(math.ceil(pct / 100.0 * len(ordered))) - 1)
    return round(ordered[max(index, 0)], 3)


class QueryVectorCache:
    """Query-vector cache keyed by model + query text; records hits and real usage."""

    def __init__(self, path: Path, model: str, client):
        self.path = Path(path)
        self.model = model
        self.client = client
        self.data: Dict[str, List[float]] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.data = {}
        self.hits = 0
        self.misses = 0
        self.usage = {"calls": 0, "prompt_tokens": 0, "total_tokens": 0}
        self.call_latencies_ms: List[float] = []

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model}:{text}".encode("utf-8")).hexdigest()

    def embed(self, text: str) -> List[float]:
        key = self._key(text)
        if key in self.data:
            self.hits += 1
            return self.data[key]
        self.misses += 1
        start = time.perf_counter()
        resp = self.client.embeddings.create(input=[text], model=self.model)
        self.call_latencies_ms.append((time.perf_counter() - start) * 1000)
        self.usage["calls"] += 1
        if resp.usage is not None:
            self.usage["prompt_tokens"] += resp.usage.prompt_tokens or 0
            self.usage["total_tokens"] += resp.usage.total_tokens or 0
        vector = resp.data[0].embedding
        self.data[key] = vector
        return vector

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data), encoding="utf-8")

    def stats(self) -> Dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "usage": dict(self.usage),
            "real_call_latency_ms": {
                "count": len(self.call_latencies_ms),
                "p50": _percentile(self.call_latencies_ms, 50),
                "p95": _percentile(self.call_latencies_ms, 95),
            },
        }


def run_groups(
    queries: List[Dict[str, Any]],
    groups: List[str],
    dataset_id: str,
    corpus_id: str,
    rank_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    rerank_fn: Optional[Callable[[str, List[str]], Dict[str, Any]]] = None,
    embed_fn: Optional[Callable[[str], List[float]]] = None,
    embedding_model: Optional[str] = None,
    candidate_depth: int = 100,
    top_k: int = 10,
) -> Dict[str, Any]:
    report = {
        "dataset_id": dataset_id,
        "corpus_id": corpus_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidate_depth": candidate_depth,
        "top_k": top_k,
        "groups": {},
    }
    for mode in groups:
        report["groups"][mode] = run_group(
            queries,
            mode,
            dataset_id,
            corpus_id,
            rank_fn=rank_fn,
            rerank_fn=rerank_fn,
            embed_fn=embed_fn,
            embedding_model=embedding_model,
            candidate_depth=candidate_depth,
            top_k=top_k,
        )
    return report


def query_latency_stats(report: Dict[str, Any]) -> Dict[str, Any]:
    latencies = [
        e["latency_ms"]
        for group in report["groups"].values()
        for e in group["per_query"]
        if e["status"] == "ok"
    ]
    return {
        "count": len(latencies),
        "p50": _percentile(latencies, 50),
        "p95": _percentile(latencies, 95),
    }
