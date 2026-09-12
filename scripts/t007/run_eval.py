"""T-007a 统一评测入口：phrase/vector/rrf 三组基线（真实向量、真实查询 embedding）。

用法：
    PYTHONPATH=src python3 scripts/t007/run_eval.py \
        --queries tests/eval/t007/dev_queries.jsonl \
        --output docs/L5-执行与验证/evidence/T-007/T-007a-dev-baselines.json
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

from dotenv import load_dotenv

from pi_market import config, corpus_eval, corpus_retrieval


class _RerankRecorder:
    """Wrap the rerank client to record calls, usage and real call latency."""

    def __init__(self, client):
        self.client = client
        self.calls = 0
        self.total_tokens = 0
        self.latencies_ms = []

    def __call__(self, query, documents):
        start = time.perf_counter()
        response = self.client.rerank(query, documents)
        self.latencies_ms.append((time.perf_counter() - start) * 1000)
        self.calls += 1
        usage = response.get("usage") or {}
        self.total_tokens += usage.get("total_tokens") or 0
        return response

    def stats(self):
        return {
            "calls": self.calls,
            "usage": {"total_tokens": self.total_tokens},
            "real_call_latency_ms": {
                "count": len(self.latencies_ms),
                "p50": corpus_eval._percentile(self.latencies_ms, 50),
                "p95": corpus_eval._percentile(self.latencies_ms, 95),
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queries", required=True, help="评测查询集 JSONL")
    parser.add_argument("--output", required=True, help="报告输出 JSON")
    parser.add_argument("--dataset-id", default="ds-61edff98759f")
    parser.add_argument("--corpus-id", default="t004-full-v2")
    parser.add_argument("--groups", default="phrase,vector,rrf")
    parser.add_argument("--candidate-depth", type=int, default=100)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--cache", default=".cache/t007/query_vectors.json")
    parser.add_argument(
        "--estimate-rerank",
        action="store_true",
        help="只预估 rerank 的调用数与 token 量（不调用 rerank）",
    )
    args = parser.parse_args()

    groups = [g.strip() for g in args.groups.split(",") if g.strip()]
    queries_path = Path(args.queries)
    queries = corpus_eval.load_query_set(queries_path)
    problems = corpus_eval.validate_query_set(queries)
    if problems:
        print(
            json.dumps({"error": "评测集校验失败", "problems": problems}, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        return 1

    load_dotenv(str(config._ENV_PATH), override=False)
    model = os.getenv("EMBEDDING_MODEL", "text-embedding-v4")
    cache = None
    if any(g in ("vector", "rrf", "rrf+rerank") for g in groups):
        api_key = os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            print(json.dumps({"error": "DASHSCOPE_API_KEY 未配置"}, ensure_ascii=False), file=sys.stderr)
            return 1
        from openai import OpenAI

        client = OpenAI(
            base_url=os.getenv("DASHSCOPE_BASE_URL"),
            api_key=api_key,
        )
        cache = corpus_eval.QueryVectorCache(Path(args.cache), model, client)

    if args.estimate_rerank:
        total_tokens = 0
        calls = 0
        per_query = []
        for q in queries:
            ranked = corpus_retrieval.rank_parents(
                args.dataset_id,
                args.corpus_id,
                q["query"],
                q["pool"],
                as_of=q["as_of"],
                mode="rrf",
                candidate_depth=100,
                parent_top=50,
                embed_fn=cache.embed if cache is not None else None,
                embedding_model=model if cache is not None else None,
                warehouse=(q.get("scope") or {}).get("warehouse_id"),
                sku=(q.get("scope") or {}).get("sku"),
            )
            documents = [p["content"] for p in ranked["parents"]]
            if not documents:
                continue
            tokens = len(q["query"]) + sum(len(doc) for doc in documents)
            total_tokens += tokens
            calls += 1
            per_query.append({"qid": q["qid"], "documents": len(documents), "tokens": tokens})
        estimate = {
            "queries_file": str(queries_path),
            "groups": groups,
            "rerank_calls": calls,
            "estimated_tokens": total_tokens,
            "per_query": per_query,
            "note": "保守估算（1 字符≈1 token 上界）；实际 usage 以调用返回为准",
        }
        print(json.dumps(estimate, ensure_ascii=False, indent=2))
        return 0

    rerank_recorder = None
    if "rrf+rerank" in groups:
        from pi_market.rerank import get_rerank_client

        rerank_recorder = _RerankRecorder(get_rerank_client())

    report = corpus_eval.run_groups(
        queries,
        groups,
        args.dataset_id,
        args.corpus_id,
        rerank_fn=rerank_recorder,
        embed_fn=cache.embed if cache is not None else None,
        embedding_model=model if cache is not None else None,
        candidate_depth=args.candidate_depth,
        top_k=args.top_k,
    )
    report["queries_file"] = str(queries_path)
    report["queries_sha256"] = corpus_eval.sha256_file(queries_path)
    report["query_latency_ms"] = corpus_eval.query_latency_stats(report)
    if cache is not None:
        cache.save()
        report["cache"] = cache.stats()
    if rerank_recorder is not None:
        report["rerank"] = rerank_recorder.stats()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {
        "queries": str(queries_path),
        "queries_sha256": report["queries_sha256"],
        "groups": {
            mode: {
                "status": group["status"],
                "overall": {
                    k: group["positive"]["overall"][k] for k in ("recall@10", "mrr@5", "ndcg@5")
                },
                "case": {
                    k: group["positive"]["case"][k] for k in ("recall@10", "mrr@5", "ndcg@5")
                },
                "sop": {
                    k: group["positive"]["sop"][k] for k in ("recall@10", "mrr@5", "ndcg@5")
                },
            }
            for mode, group in report["groups"].items()
        },
        "negative_total": {
            mode: group["negative"]["total"] for mode, group in report["groups"].items()
        },
        "query_latency_ms": report["query_latency_ms"],
        "cache": report.get("cache"),
        "rerank": report.get("rerank"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    failed = any(group["status"] != "ok" for group in report["groups"].values())
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
