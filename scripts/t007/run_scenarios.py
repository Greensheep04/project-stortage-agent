"""T-007b 阶段B：20 个调查场景，原流程（rrf）vs 选定方案（phrase）。

每个场景每组至多一次真实生成；结果逐行写 JSONL，供中层语义复核。
用法：
    PYTHONPATH=src python3 scripts/t007/run_scenarios.py \
        --scenarios docs/L5-执行与验证/evidence/T-007/T-007b-investigation-scenarios.json \
        --output docs/L5-执行与验证/evidence/T-007/T-007b-investigation-results.jsonl
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

from pi_market.investigate import investigate

GROUP_MAP = {"rrf": ("rrf", "rrf"), "phrase": ("phrase", "rrf")}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenarios", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--dataset-id", default="ds-61edff98759f")
    parser.add_argument("--corpus-id", default="t004-full-v2")
    parser.add_argument("--groups", default="rrf,phrase")
    parser.add_argument("--scenario-ids", default=None, help="逗号分隔，只跑部分场景")
    args = parser.parse_args()

    doc = json.loads(Path(args.scenarios).read_text(encoding="utf-8"))
    scenarios = doc["scenarios"]
    if args.scenario_ids:
        wanted = {s.strip() for s in args.scenario_ids.split(",")}
        scenarios = [s for s in scenarios if s["scenario_id"] in wanted]
    groups = [g.strip() for g in args.groups.split(",") if g.strip()]

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    summary = {
        "scenarios": len(scenarios),
        "runs": 0,
        "ok": 0,
        "errors": 0,
        "extension_ok": 0,
        "explanation_ok": 0,
        "suggestion_runs": 0,
        "dropped_suggestion_refs": 0,
        "generation_tokens": 0,
    }
    started = time.time()
    with output_path.open("w", encoding="utf-8") as fh:
        for scenario in scenarios:
            for group in groups:
                search_mode, ranker = GROUP_MAP[group]
                record = {
                    "scenario_id": scenario["scenario_id"],
                    "source_qid": scenario["source_qid"],
                    "polarity": scenario["polarity"],
                    "scenario_group": scenario["scenario_group"],
                    "group": group,
                    "input": {
                        "dataset_id": args.dataset_id,
                        "corpus_id": args.corpus_id,
                        "order_id": scenario["order_id"],
                        "line_id": scenario["line_id"],
                        "as_of": scenario["as_of"],
                        "question": scenario["question"],
                        "search_mode": search_mode,
                        "ranker": ranker,
                    },
                }
                run_started = time.time()
                try:
                    result = investigate(
                        args.dataset_id,
                        scenario["order_id"],
                        line_id=scenario["line_id"],
                        as_of=scenario["as_of"],
                        corpus=args.corpus_id,
                        question=scenario["question"],
                        search_mode=search_mode,
                        ranker=ranker,
                    )
                    explanation = result["explanation"]
                    record["status"] = "ok"
                    record["result"] = {
                        "extension_status": result["extension_status"],
                        "extension_error": result["extension_error"],
                        "explanation_status": explanation["explanation_status"],
                        "explanation": explanation["explanation"],
                        "used_evidence_ids": explanation.get("used_evidence_ids", []),
                        "used_event_ids": explanation.get("used_event_ids", []),
                        "used_history_ids": explanation.get("used_history_ids", []),
                        "event_evidence": [e["doc_id"] for e in result["event_evidence"]],
                        "history_refs": [d["doc_id"] for d in result["history_refs"]],
                        "applicable_rules": [d["doc_id"] for d in result["applicable_rules"]],
                        "suggestions": result["suggestions"],
                        "dropped_suggestion_refs": explanation.get("dropped_suggestion_refs", []),
                        "open_questions": explanation.get("open_questions", []),
                        "call_record": result["call_record"],
                    }
                    summary["ok"] += 1
                    if result["extension_status"] == "ok":
                        summary["extension_ok"] += 1
                    if explanation["explanation_status"] == "ok":
                        summary["explanation_ok"] += 1
                    if result["suggestions"]:
                        summary["suggestion_runs"] += 1
                    summary["dropped_suggestion_refs"] += len(
                        explanation.get("dropped_suggestion_refs", [])
                    )
                    generation = result["call_record"].get("generation_usage") or {}
                    summary["generation_tokens"] += generation.get("total_tokens") or 0
                    print(
                        f"{scenario['scenario_id']} {group:6} ok "
                        f"ext={result['extension_status']} exp={explanation['explanation_status']} "
                        f"sugg={len(result['suggestions'])} "
                        f"rules={len(result['applicable_rules'])}",
                        file=sys.stderr,
                    )
                except Exception as e:
                    record["status"] = "error"
                    record["error"] = f"{type(e).__name__}: {e}"
                    summary["errors"] += 1
                    print(f"{scenario['scenario_id']} {group:6} ERROR {e}", file=sys.stderr)
                record["elapsed_sec"] = round(time.time() - run_started, 1)
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                summary["runs"] += 1

    summary["total_elapsed_sec"] = round(time.time() - started, 1)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 2 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
