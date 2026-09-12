#!/usr/bin/env python3
"""Retrieval evaluation entry point (thin wrapper).

The shared implementation lives in ``pi_market.evaluate`` so the ``evaluate``
CLI subcommand and this script do not duplicate it.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

# Allow running the script from the repo root without an editable install.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from pi_market import evaluate


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eval_retrieval.py",
        description="评测 phrase / vector / rrf 三模式检索 Recall@k",
    )
    parser.add_argument("queries", help="查询集 JSONL 路径（tests/eval/*.jsonl）")
    parser.add_argument("dataset_id", help="数据集 ID")
    parser.add_argument(
        "--mode",
        nargs="+",
        default=["phrase", "vector", "rrf"],
        choices=["phrase", "vector", "rrf"],
        help="评测模式，可多个",
    )
    parser.add_argument("--k", type=int, default=10, help="Top-k")
    parser.add_argument(
        "--embedding-model",
        help="指定 embedding 模型标识（默认读取环境变量 EMBEDDING_MODEL）",
    )
    parser.add_argument("--as-of", help="自定义截止时间（覆盖数据集默认值）")
    parser.add_argument("--output", help="输出 JSON 文件路径，缺省打印到 stdout")
    args = parser.parse_args(argv)

    queries_path = Path(args.queries)
    try:
        report = evaluate.retrieval_report(
            args.dataset_id,
            queries_path,
            args.mode,
            k=args.k,
            embedding_model=args.embedding_model,
            as_of=args.as_of,
        )
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1

    output_text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(output_text, encoding="utf-8")
    else:
        print(output_text)

    # Report first, then mirror the evaluate CLI exit convention.
    return 0 if report.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
