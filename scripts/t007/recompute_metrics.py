"""T-007c：用已保存排名与 v2 标签离线重算指标（不重复 embedding/rerank）。

用法：
    PYTHONPATH=src python3 scripts/t007/recompute_metrics.py \
        --report evidence/T-007/T-007b-test-phrase.json \
        --queries tests/eval/t007/test_queries_v2.jsonl \
        --output evidence/T-007/T-007c-test-phrase-v2.json
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, "src")

from pi_market import corpus_eval


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--queries", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    queries = corpus_eval.load_query_set(Path(args.queries))
    problems = corpus_eval.validate_query_set(queries)
    if problems:
        print(
            json.dumps({"error": "评测集校验失败", "problems": problems}, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        return 1

    rescored = corpus_eval.rescore_report(report, queries)
    rescored["queries_file"] = args.queries
    rescored["queries_sha256"] = corpus_eval.sha256_file(Path(args.queries))
    rescored["rescore_note"] = "离线重算：使用保存的候选排名与 v2 标签，不重复 embedding/rerank"

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(rescored, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {}
    for mode, group in rescored["groups"].items():
        summary[mode] = {
            "status": group["status"],
            **{
                key: group["positive"]["overall"][key]
                for key in ("recall@10", "mrr@5", "ndcg@5", "candidate_recall@50")
            },
        }
    print(
        json.dumps(
            {"queries": args.queries, "queries_sha256": rescored["queries_sha256"], "groups": summary},
            ensure_ascii=False,
            indent=2,
        )
    )
    failed = any(group["status"] != "ok" for group in rescored["groups"].values())
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
