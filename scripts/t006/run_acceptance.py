"""T-006b A5 配套：14 例冻结功能验收的批量运行入口。

评测入口读取 `tests/eval/t006_acceptance.jsonl`（业务进程不读该文件），
逐例调用扩展调查并把事实分组、建议与引用绑定结果写入 JSONL 供中层人工审查。

用法：
    PYTHONPATH=src python3 scripts/t006/run_acceptance.py \
        --output docs/L5-执行与验证/evidence/T-006/T-006b-acceptance-results.jsonl
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

from pi_market.investigate import investigate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--acceptance", default="tests/eval/t006_acceptance.jsonl")
    parser.add_argument("--corpus-id", default="t004-full-v2")
    parser.add_argument("--output", required=True)
    parser.add_argument("--question", default=None)
    args = parser.parse_args()

    cases = [
        json.loads(line)
        for line in Path(args.acceptance).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    summary = {
        "cases": 0,
        "extension_ok": 0,
        "explanation_ok": 0,
        "suggestion_cases": 0,
        "embedding_calls": 0,
        "embedding_tokens": 0,
        "generation_tokens": 0,
    }
    started = time.time()
    with output_path.open("w", encoding="utf-8") as fh:
        for case in cases:
            inp = case["input"]
            t0 = time.time()
            result = investigate(
                inp["dataset_id"],
                inp["order_id"],
                line_id=inp.get("line_id"),
                as_of=inp["as_of"],
                corpus=args.corpus_id,
                question=args.question,
            )
            record = {
                "acceptance_id": case["acceptance_id"],
                "source_scenario": case.get("source_scenario"),
                "scenario_type": case.get("scenario_type"),
                "input": inp,
                "expected": {
                    "related_doc_ids": case.get("related_doc_ids"),
                    "facts_to_check": case.get("facts_to_check"),
                    "allowed_conclusions": case.get("allowed_conclusions"),
                    "forbidden_conclusions": case.get("forbidden_conclusions"),
                },
                "result": {
                    "corpus_id": result["corpus_id"],
                    "extension_status": result["extension_status"],
                    "facts": result["facts"],
                    "evidence": result["evidence"],
                    "event_evidence": result["event_evidence"],
                    "history_refs": result["history_refs"],
                    "applicable_rules": result["applicable_rules"],
                    "explanation": result["explanation"],
                    "suggestions": result["suggestions"],
                    "call_record": result["call_record"],
                },
                "elapsed_sec": round(time.time() - t0, 1),
            }
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

            summary["cases"] += 1
            if result["extension_status"] == "ok":
                summary["extension_ok"] += 1
            if result["explanation"]["explanation_status"] == "ok":
                summary["explanation_ok"] += 1
            if result["suggestions"]:
                summary["suggestion_cases"] += 1
            usage = result["call_record"].get("embedding_usage") or {}
            summary["embedding_calls"] += usage.get("calls", 0)
            summary["embedding_tokens"] += usage.get("total_tokens", 0)
            gen = result["call_record"].get("generation_usage") or {}
            summary["generation_tokens"] += gen.get("total_tokens", 0) or 0

    summary["total_elapsed_sec"] = round(time.time() - started, 1)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["extension_ok"] == summary["cases"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
