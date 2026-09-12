"""T-009b 受影响验证探针（真实 stdio + 离线映射），复现高层 F1–F3 反例。

用法：``PYTHONPATH=src python3 scripts/t009/rework_probe.py``
输出：``docs/L5-执行与验证/evidence/T-009/T-009b-probe.json``
"""

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pi_market import db  # noqa: E402
from pi_market.agent_tools import ToolError  # noqa: E402
from pi_market.chat import ChatSession  # noqa: E402
from pi_market.mcp_client import McpServerClient, McpToolHost  # noqa: E402

DATASET_ID = "ds-61edff98759f"
CORPUS_ID = "t004-full-v2"
AS_OF = "2026-09-09T20:00:00+08:00"
OUT = PROJECT_ROOT / "docs" / "L5-执行与验证" / "evidence" / "T-009" / "T-009b-probe.json"


class _ScriptedModel:
    def __init__(self, steps):
        self.steps = list(steps)
        self.last_usage = None

    def create(self, messages, tools=None):
        if not self.steps:
            return _response(content="（结束）")
        step = self.steps.pop(0)
        if isinstance(step, str):
            return _response(content=step)
        return _response(tool_calls=[_call(f"c{len(self.steps)}", step[0], step[1])])


class _MockGeneration:
    def __init__(self):
        self.last_usage = None
        self.prompts = []

    def generate(self, prompt):
        self.prompts.append(prompt)
        compact = prompt.split("四、有效规则")[-1]
        return json.dumps(
            {
                "explanation": "依据程序事实整理。",
                "used_evidence_ids": [],
                "suggestions": [],
                "open_questions": [],
            },
            ensure_ascii=False,
        )


def _call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(arguments, ensure_ascii=False))
    )


def _response(tool_calls=None, content=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls or []))]
    )


def f1_session_probe():
    query = {"query": "包装 受潮", "type": "sop"}
    session = ChatSession(
        DATASET_ID,
        CORPUS_ID,
        order_id="SO-1003",
        line_id="001",
        as_of=AS_OF,
        model_client=_ScriptedModel([("search_knowledge", dict(query)), "结束"]),
        generation_client=_MockGeneration(),
        transport="mcp",
    )
    try:
        first = session.run_turn("包装受潮按哪版规范？")
        search1 = [r for r in first["trace"] if r.get("tool") == "search_knowledge"]
        old_client = session._host._client
        old_close = old_client.close()  # 模拟连接生命周期结束
        session._client = _ScriptedModel(
            [("search_knowledge", dict(query)), "结束", ("read_document", {"doc_id": "SOP-PACK-v2"}), "结束"]
        )
        second = session.run_turn("再查一遍同样的规范")
        third = session.run_turn("回读刚才的规范")
    finally:
        clean = session.close()
    return {
        "first_search": {"cache_hit": search1[0]["cache_hit"], "rpc": search1[0].get("rpc")},
        "old_client_closed": old_close,
        "reconnect_events": [
            {k: v for k, v in r.items() if k in ("op", "generation", "cache_invalidated")}
            for r in second["trace"]
            if r.get("op") in ("mcp_reconnect", "mcp_connect")
        ],
        "second": {
            "search": [
                {"tool": r.get("tool"), "cache_hit": r.get("cache_hit"), "rpc": r.get("rpc")}
                for r in second["trace"]
                if r.get("tool") == "search_knowledge"
            ],
            "preload": [
                {"tool": r.get("tool"), "rpc": r.get("rpc"), "cache_hit": r.get("cache_hit")}
                for r in second["trace"]
                if r.get("phase") == "host_preload"
            ],
            "tool_requests": second["tool_requests"],
            "incomplete": second["incomplete"],
        },
        "readback_ok": bool(third["pack"]["facts"]) and not any(
            r.get("error") for r in third["trace"] if r.get("tool") == "read_document"
        ),
        "session_close_clean": clean,
    }


def f2_mapping_probe():
    def text(is_error, raw):
        return SimpleNamespace(
            is_error=is_error, content=[SimpleNamespace(type="text", text=raw)]
        )

    out = {}
    try:
        McpServerClient._parse_result("read_document", text(True, "SDK execution failed"))
        out["plain_native_error"] = "unexpected-success"
    except ToolError as e:
        out["plain_native_error"] = e.category
    try:
        McpServerClient._parse_result(
            "read_document", text(True, '{"type": "ok", "data": {"facts": {}}}')
        )
        out["contradictory_ok"] = "unexpected-success"
    except ToolError as e:
        out["contradictory_ok"] = e.category
    try:
        McpServerClient._parse_result(
            "read_document",
            text(False, '{"type": "error", "error": {"category": "not_seen", "message": "x"}}'),
        )
        out["business_rejection"] = "unexpected-success"
    except ToolError as e:
        out["business_rejection"] = e.category
    data = McpServerClient._parse_result(
        "inspect_order", text(False, '{"type": "ok", "data": {"tool": "inspect_order"}}')
    )
    out["normal_success"] = data["tool"]
    return out


class _InterruptAfterStart(McpToolHost):
    def __init__(self, scope):
        super().__init__(scope)
        self.started_client = None

    def inspect_order(self):
        self._ensure_connected()
        self.started_client = self._client
        raise KeyboardInterrupt


def f3_interrupt_probe():
    holder = {}

    def factory(scope):
        host = _InterruptAfterStart(scope)
        holder["host"] = host
        return host

    raised = False
    try:
        ChatSession(
            DATASET_ID,
            CORPUS_ID,
            order_id="SO-1003",
            line_id="001",
            as_of=AS_OF,
            model_client=_ScriptedModel([]),
            generation_client=_MockGeneration(),
            transport="mcp",
            host_factory=factory,
        )
    except KeyboardInterrupt:
        raised = True
    host = holder.get("host")
    return {
        "bind_interrupted": raised,
        "client_alive_after_interrupt": bool(
            host and host.started_client and host.started_client.alive
        ),
        "started_client_exists": bool(host and host.started_client),
    }


def main():
    db.init_db()
    evidence = {
        "dataset_id": DATASET_ID,
        "corpus_id": CORPUS_ID,
        "as_of": AS_OF,
        "F1_reconnect": f1_session_probe(),
        "F2_error_mapping": f2_mapping_probe(),
        "F3_interrupt": f3_interrupt_probe(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
