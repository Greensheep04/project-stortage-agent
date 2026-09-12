"""T-008b：10 组对话真实运行器（须在定义被中层复核冻结后使用）。

每轮真实模型＋真实只读数据；逐轮记录完整 trace、程序侧可机检断言与语义字段
（forbidden/notes 留给中层复核，程序不作为通过依据）。
用法：
    PYTHONPATH=src python3 scripts/t008/run_dialogs.py \
        --dialogs tests/eval/t008/dialogs.json \
        --output docs/L5-执行与验证/evidence/T-008/T-008b-dialogs.jsonl
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

from pi_market.chat import ChatSession


def _tool_signature(record):
    name = record.get("tool") or ""
    params = record.get("params") or ""
    if name == "search_knowledge" and isinstance(params, str):
        match = re.search(r'"type"\s*:\s*"([^"]+)"', params)
        if match:
            return f"{name}:{match.group(1)}"
    return name


def _scope_ok(state, expected):
    if expected is None:
        return state.order_id is None
    if state.order_id != expected.get("order") or state.line_id != expected.get("line"):
        return False
    prefix = expected.get("asof_prefix")
    if prefix and not (state.as_of or "").startswith(prefix):
        return False
    return True


def _check_turn(session, result, spec, expected_scope):
    checks = []
    state = session.state
    checks.append(
        {
            "name": "expected_scope",
            "pass": _scope_ok(state, expected_scope),
            "detail": {"order": state.order_id, "line": state.line_id, "as_of": state.as_of},
        }
    )
    if spec.get("must_stay_unbound"):
        checks.append({"name": "must_stay_unbound", "pass": state.order_id is None})

    signatures = {_tool_signature(r) for r in result.get("trace", [])}
    if spec["required_tools_any"]:
        checks.append(
            {
                "name": "required_tools_any",
                "pass": bool(signatures & set(spec["required_tools_any"])),
                "detail": sorted(signatures),
            }
        )

    pack = result.get("pack") or {}
    explanation = result.get("explanation") or {}
    evidence_ids = {e.get("evidence_id") for e in pack.get("evidence", [])}
    evidence_ids |= {e.get("doc_id") for e in pack.get("events", [])}
    evidence_ids |= {c.get("evidence_id") for c in explanation.get("citations", []) if c.get("evidence_id")}
    if spec["required_evidence_any"]:
        checks.append(
            {
                "name": "required_evidence_any",
                "pass": bool(evidence_ids & set(spec["required_evidence_any"])),
                "detail": sorted(x for x in evidence_ids if x),
            }
        )

    rule_ids = {d.get("doc_id") for d in pack.get("rules", [])}
    rule_ids |= {c.get("doc_id") for c in explanation.get("citations", []) if c.get("doc_id")}
    for item in explanation.get("suggestions", []) or []:
        rule_ids |= {rid.split(":")[1] for rid in item.get("rule_ids", []) if rid.count(":") >= 2}
    if spec["expected_rules_any"]:
        checks.append(
            {
                "name": "expected_rules_any",
                "pass": bool(rule_ids & set(spec["expected_rules_any"])),
                "detail": sorted(rule_ids),
            }
        )

    allowed_status = set(spec["expected_explanation_status_in"])
    if spec["command"] or spec.get("must_stay_unbound"):
        allowed_status.add("no_generation")
    status = explanation.get("explanation_status", "no_generation" if result["final_generations"] == 0 else "unknown")
    checks.append({"name": "explanation_status", "pass": status in allowed_status, "detail": status})
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dialogs", default="tests/eval/t008/dialogs.json")
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-dialogs", type=int, default=None)
    parser.add_argument("--dialog-ids", default=None, help="逗号分隔，只跑指定对话")
    args = parser.parse_args()

    doc = json.loads(Path(args.dialogs).read_text(encoding="utf-8"))
    if doc.get("status", "").startswith("待中层"):
        print(json.dumps({"error": "对话定义尚未被中层冻结，拒绝运行", "status": doc["status"]}, ensure_ascii=False), file=sys.stderr)
        return 1

    dialogs = doc["dialogs"][: args.max_dialogs] if args.max_dialogs else doc["dialogs"]
    if args.dialog_ids:
        wanted = {item.strip() for item in args.dialog_ids.split(",")}
        dialogs = [dialog for dialog in dialogs if dialog["dialog_id"] in wanted]
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    summary = {"dialogs": len(dialogs), "turns": 0, "checks_pass": 0, "checks_fail": 0,
               "model_tokens": 0, "generation_tokens": 0, "errors": 0}
    started = time.time()
    with output_path.open("w", encoding="utf-8") as fh:
        for dialog in dialogs:
            startup = dialog.get("startup") or {}
            session = ChatSession(
                doc["dataset_id"], doc["corpus_id"],
                order_id=startup.get("order"), line_id=startup.get("line_id"), as_of=startup.get("as_of"),
            )
            if session.startup_message:
                print(session.startup_message, file=sys.stderr)
            expected_scope = (
                {"order": startup.get("order"), "line": startup.get("line_id")}
                if startup.get("order")
                else None
            )
            for spec in dialog["turns"]:
                turn_started = time.time()
                record = {
                    "dialog_id": dialog["dialog_id"],
                    "dimension": dialog["dimension"],
                    "turn": spec["turn"],
                    "input": spec["input"],
                    "command": spec["command"],
                    "forbidden": spec["forbidden"],
                    "notes": spec["notes"],
                }
                try:
                    if spec["command"]:
                        record["command_reply"] = session.handle_command(spec["command"])
                        result = {
                            "text": record["command_reply"] or "",
                            "trace": [],
                            "tool_requests": 0,
                            "planning_requests": 0,
                            "final_generations": 0,
                            "incomplete": False,
                            "scope_key": list(session.state.scope_key()),
                            "model_usage": None,
                            "pack": None,
                            "explanation": None,
                        }
                    else:
                        result = session.run_turn(spec["input"])
                    record["result"] = {
                        "text": result["text"],
                        "trace": result["trace"],
                        "tool_requests": result["tool_requests"],
                        "planning_requests": result["planning_requests"],
                        "final_generations": result["final_generations"],
                        "incomplete": result["incomplete"],
                        "scope_key": result["scope_key"],
                        "model_usage": result.get("model_usage"),
                        "generation_usage": result.get("generation_usage"),
                        "pack_counts": (result.get("pack") or {}).get("counts"),
                        "explanation": {
                            "status": (result.get("explanation") or {}).get("explanation_status"),
                            "used_evidence_ids": (result.get("explanation") or {}).get("used_evidence_ids"),
                            "used_event_ids": (result.get("explanation") or {}).get("used_event_ids"),
                            "used_history_ids": (result.get("explanation") or {}).get("used_history_ids"),
                            "suggestions": (result.get("explanation") or {}).get("suggestions"),
                            "open_questions": (result.get("explanation") or {}).get("open_questions"),
                        } if result.get("explanation") else None,
                    }
                    if spec["expected_scope"] is not None:
                        expected_scope = spec["expected_scope"]
                    checks = _check_turn(session, result, spec, expected_scope)
                    record["checks"] = checks
                    passed = sum(1 for c in checks if c["pass"])
                    summary["checks_pass"] += passed
                    summary["checks_fail"] += len(checks) - passed
                    summary["turns"] += 1
                    model_usage = result.get("model_usage") or {}
                    generation_usage = result.get("generation_usage") or {}
                    summary["model_tokens"] += model_usage.get("total_tokens") or 0
                    summary["generation_tokens"] += generation_usage.get("total_tokens") or 0
                    print(
                        f"{dialog['dialog_id']} turn {spec['turn']}: checks {passed}/{len(checks)} "
                        f"tools={result['tool_requests']} planning={result['planning_requests']} "
                        f"final={result['final_generations']}",
                        file=sys.stderr,
                    )
                except Exception as e:
                    record["error"] = f"{type(e).__name__}: {e}"
                    summary["errors"] += 1
                    print(f"{dialog['dialog_id']} turn {spec['turn']}: ERROR {e}", file=sys.stderr)
                record["elapsed_sec"] = round(time.time() - turn_started, 1)
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    summary["total_elapsed_sec"] = round(time.time() - started, 1)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 2 if summary["checks_fail"] or summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
