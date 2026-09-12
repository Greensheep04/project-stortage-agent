"""T-009a D21/D22：McpToolHost 适配与 chat MCP 后端的离线测试（不启动真实子进程）。"""

from types import SimpleNamespace

import pytest

from pi_market import corpus
from pi_market.agent_tools import TOOL_NAMES, Scope, ToolError
from pi_market.chat import MAX_TOOL_REQUESTS, ChatSession
from pi_market.mcp_client import McpToolHost, to_openai_schemas
from test_chat_bounds import (
    _MockGeneration,
    _ScriptedClient,
    _call,
    _facts_fixture,
    _response,
)
from test_corpus_common import DATASET_ID, REAL_CORPUS_DIR

AS_OF = "2026-09-09T20:00:00+08:00"


@pytest.fixture(scope="module", autouse=True)
def _corpus_ready():
    corpus.import_corpus("t004-full-v2", DATASET_ID, files_dir=REAL_CORPUS_DIR)
    corpus.index_corpus("t004-full-v2", "mock-v1", no_embedding=True)
    yield


def _scope(order_id="SO-1003", line_id="001"):
    return Scope(DATASET_ID, "t004-full-v2", order_id, line_id, AS_OF)


def _fake_tools():
    return [
        SimpleNamespace(
            name=name, input_schema={"type": "object", "properties": {}}, description=""
        )
        for name in TOOL_NAMES
    ]


class _FakeMcpClient:
    def __init__(self, *, start_error=None, tools=None, fail_tools=None):
        self.tools = tools if tools is not None else _fake_tools()
        self.protocol_version = "2025-11-25"
        self.server_info = "fake-server 0.1.0"
        self.calls = []
        self.alive = False
        self.closed = False
        self._start_error = start_error
        self._fail_tools = set(fail_tools or ())

    def start(self):
        if self._start_error is not None:
            raise self._start_error
        self.alive = True

    def call_tool(self, name, args):
        if name in self._fail_tools:
            raise ToolError(f"{name} 服务失败", "internal_error")
        self.calls.append((name, dict(args)))
        if name == "inspect_order":
            return {"tool": name, "source": "fake-mcp", "facts": _facts_fixture()}
        if name == "read_order_evidence":
            return {"tool": name, "source": "fake-mcp", "evidence": [], "events": []}
        return {
            "tool": name,
            "source": "fake-mcp",
            "type": args.get("type"),
            "results": [],
        }

    def close(self):
        self.closed = True
        self.alive = False
        return True


def test_validate_rejects_before_any_rpc():
    client = _FakeMcpClient()
    host = McpToolHost(_scope(), client_factory=lambda: client)
    with pytest.raises(ToolError) as unknown:
        host.call("drop_table", {})
    assert unknown.value.category == "unknown_tool"
    with pytest.raises(ToolError) as extra:
        host.call("search_knowledge", {"query": "x", "type": "sop", "order_id": "SO-1"})
    assert extra.value.category == "invalid_argument"
    with pytest.raises(ToolError) as bad_doc:
        host.call("read_document", {"doc_id": "../secret"})
    assert bad_doc.value.category == "invalid_argument"
    assert client.calls == []
    assert client.alive is False  # 校验失败不触发连接


def test_connect_and_close_events_recorded():
    client = _FakeMcpClient()
    host = McpToolHost(_scope(), client_factory=lambda: client)
    schemas = host.tool_schemas()
    assert schemas == to_openai_schemas(client.tools)
    events = host.take_events()
    assert events and events[0]["op"] == "mcp_connect" and events[0]["status"] == "ok"
    assert host.take_events() == []
    assert host.close() is True
    assert host.take_events()[-1]["op"] == "mcp_close"
    assert client.closed is True


def test_schema_whitelist_mismatch_rejected():
    client = _FakeMcpClient(tools=_fake_tools()[:-1])
    host = McpToolHost(_scope(), client_factory=lambda: client)
    with pytest.raises(ToolError) as mismatch:
        host.tool_schemas()
    assert mismatch.value.category == "internal_error"


def _mcp_session(client, responses):
    return ChatSession(
        DATASET_ID,
        "t004-full-v2",
        order_id="SO-1003",
        line_id="001",
        model_client=_ScriptedClient(list(responses)),
        generation_client=_MockGeneration(),
        transport="mcp",
        host_factory=lambda scope: McpToolHost(scope, client_factory=lambda: client),
    )


def test_chat_bind_and_preload_go_through_call_tool():
    client = _FakeMcpClient()
    session = _mcp_session(client, [_response(content="结束")])
    try:
        assert [name for name, _ in client.calls] == ["inspect_order", "read_order_evidence"]
        assert session.state.preload_pending == ["inspect_order", "read_order_evidence"]

        result = session.run_turn("再核对一下")
        assert result["tool_requests"] == 2
        preload = [r for r in result["trace"] if r.get("phase") == "host_preload"]
        assert len(preload) == 2 and all(r["backend"] == "mcp" for r in preload)
        assert result["final_generations"] == 1
    finally:
        assert session.close() is True


def test_chat_mcp_rpc_record_and_cache_hit():
    client = _FakeMcpClient()
    query = {"query": "包装复核", "type": "sop"}
    session = _mcp_session(
        client,
        [
            _response(tool_calls=[_call("s1", "search_knowledge", dict(query))]),
            _response(content="结束"),
            _response(tool_calls=[_call("s2", "search_knowledge", dict(query))]),
            _response(content="结束"),
        ],
    )
    try:
        first = session.run_turn("包装应该怎么处理？")
        rpc = [r for r in first["trace"] if r.get("tool") == "search_knowledge"]
        assert rpc and rpc[0]["rpc"] is True and rpc[0]["backend"] == "mcp"
        assert rpc[0]["cache_hit"] is False

        second = session.run_turn("再确认一次")
        cached = [r for r in second["trace"] if r.get("tool") == "search_knowledge"]
        assert cached and cached[0]["cache_hit"] is True
        assert "rpc" not in cached[0]
        assert client.calls.count(("search_knowledge", query)) == 1
    finally:
        session.close()


def test_chat_mcp_start_failure_is_structured_then_recovers():
    failing = _FakeMcpClient(start_error=ToolError("连接失败", "internal_error"))
    session = _mcp_session(
        failing,
        [
            _response(tool_calls=[_call("s", "search_knowledge", {"query": "包装复核", "type": "sop"})]),
            _response(content="结束"),
        ],
    )
    try:
        result = session.run_turn("查一下")
        assert result["incomplete"] is True
        assert "MCP 工具服务不可用" in result["text"]
        assert result["planning_requests"] == 0
        assert result["final_generations"] == 0
        assert any(r.get("op") == "mcp_connect" and r.get("status") == "failed" for r in result["trace"])

        failing._start_error = None
        recovered = session.run_turn("恢复后的轮次")
        assert recovered["incomplete"] is False
        assert failing.calls  # 恢复后经过 MCP 调用
    finally:
        session.close()


def test_chat_mcp_tool_failure_marks_rule_group_failed():
    client = _FakeMcpClient(fail_tools={"search_knowledge"})
    session = _mcp_session(
        client,
        [
            _response(tool_calls=[_call("s", "search_knowledge", {"query": "包装复核", "type": "sop"})]),
            _response(content="结束"),
        ],
    )
    try:
        result = session.run_turn("包装应该怎么处理？")
        assert result["incomplete"] is True
        assert "规则检索失败" in result["text"]
        assert any(
            r.get("tool") == "search_knowledge" and r.get("error", {}).get("category") == "internal_error"
            for r in result["trace"]
        )
    finally:
        session.close()


def test_chat_mcp_budget_still_bounded():
    calls = [
        _call(f"c{i}", "search_knowledge", {"query": f"包装复核 {i}", "type": "sop"})
        for i in range(MAX_TOOL_REQUESTS + 1)
    ]
    client = _FakeMcpClient()
    session = _mcp_session(client, [_response(tool_calls=calls), _response(content="结束")])
    try:
        result = session.run_turn("一次问多个")
        assert result["tool_requests"] == MAX_TOOL_REQUESTS
        assert result["incomplete"] is True
        over = [r for r in result["trace"] if r.get("error", {}).get("category") == "budget_exhausted"]
        assert over and over[0]["backend"] == "mcp"
    finally:
        session.close()
