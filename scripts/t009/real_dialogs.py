"""T-009a B4 真实 DeepSeek 对话（工具传输 MCP）：两组闭环并写证据。

用法：``PYTHONPATH=src python3 scripts/t009/real_dialogs.py``
输出：``docs/L5-执行与验证/evidence/T-009/T-009a-dialogs.jsonl``
"""

import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pi_market import db  # noqa: E402
from pi_market.chat import ChatSession  # noqa: E402

DATASET_ID = "ds-61edff98759f"
CORPUS_ID = "t004-full-v2"
AS_OF = "2026-09-09T20:00:00+08:00"
OUT_PATH = PROJECT_ROOT / "docs" / "L5-执行与验证" / "evidence" / "T-009" / "T-009a-dialogs.jsonl"
SUMMARY_PATH = PROJECT_ROOT / "docs" / "L5-执行与验证" / "evidence" / "T-009" / "T-009a-dialogs-summary.json"


def _trace_check(trace):
    tool_records = [r for r in trace if r.get("tool")]
    connects = [r for r in trace if r.get("op") == "mcp_connect"]
    mcp_only = all(r.get("backend") == "mcp" for r in tool_records)
    routed = all(
        r.get("phase") == "host_preload"
        or r.get("cache_hit") is True
        or r.get("rpc") is True
        or r.get("error") is not None
        for r in tool_records
    )
    return {
        "tools": len(tool_records),
        "connects": len(connects),
        "all_mcp_backend": mcp_only,
        "all_routed_or_cached": routed,
    }


def run_dialog(name, start, turns):
    session = ChatSession(
        DATASET_ID,
        CORPUS_ID,
        order_id=start.get("order_id"),
        line_id=start.get("line_id"),
        as_of=AS_OF,
        transport="mcp",
    )
    rows = []
    try:
        for index, turn in enumerate(turns, start=1):
            started = time.perf_counter()
            if turn.startswith("/"):
                reply = session.handle_command(turn)
                result = {
                    "text": reply,
                    "trace": [],
                    "tool_requests": 0,
                    "planning_requests": 0,
                    "final_generations": 0,
                    "incomplete": False,
                    "model_usage": {"calls": 0, "total_tokens": None, "unknown_calls": 0},
                    "generation_usage": {"calls": 0, "total_tokens": None, "unknown_calls": 0},
                }
                kind = "command"
            else:
                result = session.run_turn(turn)
                kind = "user"
            rows.append(
                {
                    "dialog": name,
                    "turn": index,
                    "kind": kind,
                    "input": turn,
                    "elapsed_sec": round(time.perf_counter() - started, 1),
                    "scope_key": result.get("scope_key"),
                    "tool_requests": result.get("tool_requests"),
                    "planning_requests": result.get("planning_requests"),
                    "final_generations": result.get("final_generations"),
                    "incomplete": result.get("incomplete"),
                    "model_usage": result.get("model_usage"),
                    "generation_usage": result.get("generation_usage"),
                    "trace_checks": _trace_check(result.get("trace", [])),
                    "trace": [dict(record) for record in result.get("trace", [])],
                    "text": result.get("text", ""),
                }
            )
    finally:
        session.close()
    return rows


def main():
    db.init_db()
    dialogs = [
        (
            "MCP-A",
            {},
            [
                "找一下最近的待检或差异事件",
                "/order SO-1003 001",
                "这批的流水与事件记录了什么？",
            ],
        ),
        (
            "MCP-B",
            {"order_id": "SO-1011", "line_id": "001"},
            [
                "包装受潮应该按哪版规范处理？",
                "那要留什么记录？",
            ],
        ),
    ]
    all_rows = []
    for name, start, turns in dialogs:
        all_rows.extend(run_dialog(name, start, turns))
        print(f"{name} done")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as handle:
        for row in all_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    summary = {
        "dialogs": len(dialogs),
        "turns": len(all_rows),
        "model_tokens": sum((row["model_usage"] or {}).get("total_tokens") or 0 for row in all_rows),
        "generation_tokens": sum((row["generation_usage"] or {}).get("total_tokens") or 0 for row in all_rows),
        "elapsed_sec": round(sum(row["elapsed_sec"] for row in all_rows), 1),
        "trace_ok": all(
            row["trace_checks"]["all_mcp_backend"] and row["trace_checks"]["all_routed_or_cached"]
            for row in all_rows
            if row["kind"] == "user"
        ),
        "incomplete_turns": [f"{row['dialog']} T{row['turn']}" for row in all_rows if row["incomplete"]],
    }
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
