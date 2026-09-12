"""T-009b：F1 重连代次一致性、F2 协议 isError 优先级、F3 中断清理归属。"""

from types import SimpleNamespace

import pytest

from pi_market import corpus
from pi_market.agent_tools import Scope, ToolError
from pi_market.chat import ChatSession
from pi_market.mcp_client import McpServerClient, McpToolHost
from test_chat_bounds import (
    _MockGeneration,
    _ScriptedClient,
    _call,
    _response,
)
from test_corpus_common import DATASET_ID, REAL_CORPUS_DIR
from test_mcp_host import _FakeMcpClient
from test_mcp_protocol import _mcp_server_pids, _wait_pids_gone

AS_OF = "2026-09-09T20:00:00+08:00"


@pytest.fixture(scope="module", autouse=True)
def _corpus_ready():
    corpus.import_corpus("t004-full-v2", DATASET_ID, files_dir=REAL_CORPUS_DIR)
    corpus.index_corpus("t004-full-v2", "mock-v1", no_embedding=True)
    yield


def _scope(order_id="SO-1003", line_id="001"):
    return Scope(DATASET_ID, "t004-full-v2", order_id, line_id, AS_OF)


def _text_result(is_error, text):
    return SimpleNamespace(
        is_error=is_error,
        content=[SimpleNamespace(type="text", text=text)],
    )


def _session(responses, **kwargs):
    return ChatSession(
        DATASET_ID,
        "t004-full-v2",
        order_id="SO-1003",
        line_id="001",
        as_of=AS_OF,
        model_client=_ScriptedClient(list(responses)),
        generation_client=_MockGeneration(),
        transport="mcp",
        **kwargs,
    )


# ---------------------------------------------------------------- F2 协议错误优先级
def test_native_iserror_plain_text_is_internal_error():
    with pytest.raises(ToolError) as error:
        McpServerClient._parse_result("read_document", _text_result(True, "SDK execution failed"))
    assert error.value.category == "internal_error"


def test_native_iserror_validation_text_is_invalid_argument():
    raw = "Error executing tool read_document: 1 validation error for read_documentArguments\n  Field required"
    with pytest.raises(ToolError) as error:
        McpServerClient._parse_result("read_document", _text_result(True, raw))
    assert error.value.category == "invalid_argument"


def test_native_iserror_with_ok_wrapper_is_internal_error_not_success():
    with pytest.raises(ToolError) as error:
        McpServerClient._parse_result(
            "read_document", _text_result(True, '{"type": "ok", "data": {"facts": {}}}')
        )
    assert error.value.category == "internal_error"


def test_business_error_payload_category_preserved():
    raw = '{"type": "error", "error": {"category": "not_seen", "message": "文档不在本会话"}}'
    with pytest.raises(ToolError) as error:
        McpServerClient._parse_result("read_document", _text_result(False, raw))
    assert error.value.category == "not_seen"


def test_success_payload_returns_data():
    raw = '{"type": "ok", "data": {"tool": "inspect_order", "facts": {"lines": []}}}'
    data = McpServerClient._parse_result("inspect_order", _text_result(False, raw))
    assert data["tool"] == "inspect_order"


def test_native_iserror_through_chat_marks_incomplete_and_visible():
    class _NativeErrorClient(_FakeMcpClient):
        def call_tool(self, name, args):
            if name == "search_knowledge":
                return McpServerClient._parse_result(
                    name, _text_result(True, "SDK execution failed")
                )
            return super().call_tool(name, args)

    client = _NativeErrorClient()
    session = _session(
        [
            _response(tool_calls=[_call("s", "search_knowledge", {"query": "包装复核", "type": "sop"})]),
            _response(content="结束"),
        ],
        host_factory=lambda scope: McpToolHost(scope, client_factory=lambda: client),
    )
    try:
        result = session.run_turn("包装应该怎么处理？")
        assert result["incomplete"] is True
        assert "规则检索失败" in result["text"]
        assert any(
            r.get("error", {}).get("category") == "internal_error"
            for r in result["trace"]
            if r.get("tool") == "search_knowledge"
        )
    finally:
        session.close()


# ---------------------------------------------------------------- F1 重连代次与缓存
def test_reconnect_invalidates_cache_and_readback_succeeds():
    query = {"query": "包装 受潮", "type": "sop"}
    session = _session(
        [
            _response(tool_calls=[_call("s1", "search_knowledge", dict(query))]),
            _response(content="结束"),
            _response(tool_calls=[_call("s2", "search_knowledge", dict(query))]),
            _response(content="结束"),
            _response(tool_calls=[_call("d", "read_document", {"doc_id": "SOP-PACK-v2"})]),
            _response(content="结束"),
        ]
    )
    try:
        first = session.run_turn("包装受潮按哪版规范？")
        first_search = [r for r in first["trace"] if r.get("tool") == "search_knowledge"]
        assert first_search and first_search[0]["rpc"] is True and first_search[0]["cache_hit"] is False

        old_client = session._host._client
        assert old_client is not None and old_client.close() is True  # 模拟连接生命周期结束

        second = session.run_turn("再查一遍同样的规范并给出全部依据")
        reconnects = [r for r in second["trace"] if r.get("op") == "mcp_reconnect"]
        assert reconnects and reconnects[0]["cache_invalidated"] is True
        second_search = [r for r in second["trace"] if r.get("tool") == "search_knowledge"]
        assert second_search and second_search[0]["cache_hit"] is False
        assert second_search[0]["rpc"] is True  # 缓存失效后是真 RPC，不冒充命中
        preload = [r for r in second["trace"] if r.get("phase") == "host_preload"]
        assert len(preload) == 2 and all(r.get("rpc") is True for r in preload)
        assert second["tool_requests"] == 3  # 预载 2＋搜索 1，不越预算
        assert second["incomplete"] is False

        third = session.run_turn("回读刚才的规范")
        assert third["pack"]["facts"] is not None  # 重连后已重新核对当前范围
        read = [r for r in third["trace"] if r.get("tool") == "read_document"]
        assert read and not read[0].get("error")
        assert read[0]["cache_hit"] is False and read[0]["rpc"] is True
    finally:
        assert session.close() is True


# ---------------------------------------------------------------- F3 中断清理归属
class _InterruptAfterStart(McpToolHost):
    """真实启动一个 Client 后抛出 KeyboardInterrupt（模拟绑定期间 Ctrl-C）。"""

    def __init__(self, scope, **kwargs):
        super().__init__(scope, **kwargs)
        self.started_client = None

    def inspect_order(self):
        self._ensure_connected()
        self.started_client = self._client
        raise KeyboardInterrupt


def test_binding_interrupt_closes_new_host():
    pids_before = _mcp_server_pids()
    holder = {}

    def factory(scope):
        host = _InterruptAfterStart(scope)
        holder["host"] = host
        return host

    with pytest.raises(KeyboardInterrupt):
        ChatSession(
            DATASET_ID,
            "t004-full-v2",
            order_id="SO-1003",
            line_id="001",
            as_of=AS_OF,
            model_client=_ScriptedClient([]),
            generation_client=_MockGeneration(),
            transport="mcp",
            host_factory=factory,
        )
    host = holder["host"]
    assert host.started_client is not None
    assert host.started_client.alive is False  # 创建即负责清理
    assert _wait_pids_gone(_mcp_server_pids() - pids_before)


def test_order_interrupt_does_not_commit_new_scope_or_leak_client():
    holder = {}

    def factory(scope):
        if scope.order_id == "SO-1007":
            host = _InterruptAfterStart(scope)
            holder["host"] = host
            return host
        return McpToolHost(scope)

    session = _session(
        [_response(content="结束")],
        host_factory=factory,
    )
    try:
        with pytest.raises(KeyboardInterrupt):
            session.handle_command("/order SO-1007 002")
        assert session.state.order_id == "SO-1003"  # 失败不提交新范围
        assert session._host is not None and session._host.scope.order_id == "SO-1003"
        assert holder["host"].started_client.alive is False
    finally:
        assert session.close() is True


def test_connection_wait_interrupt_in_bind_is_cleaned():
    class _StartInterruptClient(_FakeMcpClient):
        def start(self):
            raise KeyboardInterrupt

    client = _StartInterruptClient()

    def factory(scope):
        return McpToolHost(scope, client_factory=lambda: client)

    with pytest.raises(KeyboardInterrupt):
        ChatSession(
            DATASET_ID,
            "t004-full-v2",
            order_id="SO-1003",
            line_id="001",
            as_of=AS_OF,
            model_client=_ScriptedClient([]),
            generation_client=_MockGeneration(),
            transport="mcp",
            host_factory=factory,
        )
    assert client.closed is True  # 已创建 Client 必须被关闭，而非仅断言异常


class _StubbornClient(_FakeMcpClient):
    def __init__(self):
        super().__init__()
        self._close_ok = False

    def close(self):
        self.closed = True
        if self._close_ok:
            self.alive = False
            return True
        return False


def test_close_false_keeps_handle_and_retries():
    client = _StubbornClient()
    host = McpToolHost(_scope(), client_factory=lambda: client)
    host.tool_schemas()
    assert host.close() is False
    assert host._client is client  # 未回收句柄不能当清理成功丢弃

    session = _session([_response(content="结束")], host_factory=lambda scope: host)
    assert session.close() is False
    assert session._dangling_hosts
    client._close_ok = True
    assert session.close() is True
    assert session._dangling_hosts == []


# ---------------------------------------------------------------- T-009c F1.1
class _TaggedClient(_FakeMcpClient):
    def __init__(self, tag, **kwargs):
        super().__init__(**kwargs)
        self.tag = tag

    def call_tool(self, name, args):
        result = super().call_tool(name, args)
        result["source"] = f"fake-mcp-{self.tag}"
        return result


def test_reconnect_after_failed_attempt_republishes_and_repreloads():
    attempts = {}

    def factory():
        number = len(attempts) + 1
        if number == 1:
            client = _TaggedClient(1)
        elif number == 2:
            client = _FakeMcpClient(start_error=ToolError("恢复失败", "internal_error"))
        else:
            client = _TaggedClient(3)
        attempts[number] = client
        return client

    session = _session(
        [
            _response(tool_calls=[_call("s1", "search_knowledge", {"query": "包装复核", "type": "sop"})]),
            _response(content="结束"),
            _response(
                tool_calls=[
                    _call("s2", "search_knowledge", {"query": "包装复核", "type": "sop"}),
                    _call("d", "read_document", {"doc_id": "SOP-FAKE-v1"}),
                ]
            ),
            _response(content="结束"),
        ],
        host_factory=lambda scope: McpToolHost(scope, client_factory=factory),
    )
    try:
        session.run_turn("查包装规范")
        assert session.state.facts_evidence["inspect"]["source"] == "fake-mcp-1"

        attempts[1].close()  # 第一个连接失效
        second = session.run_turn("再查一次")  # 恢复启动失败
        assert second["incomplete"] is True
        assert "MCP 工具服务不可用" in second["text"]

        third = session.run_turn("恢复后的调查")
        reconnects = [r for r in third["trace"] if r.get("op") == "mcp_reconnect"]
        assert reconnects and reconnects[0]["cache_invalidated"] is True
        assert session._host.generation == 2  # 失败尝试不清零，成功代次推进
        preload = [r for r in third["trace"] if r.get("phase") == "host_preload"]
        assert len(preload) == 2 and all(r.get("rpc") is True for r in preload)
        assert third["tool_requests"] == 4  # 两次必要预载＋搜索＋回读，计预算
        calls3 = [name for name, _ in attempts[3].calls]
        assert calls3 == ["inspect_order", "read_order_evidence", "search_knowledge", "read_document"]
        readback = [r for r in third["trace"] if r.get("tool") == "read_document"]
        assert readback and not readback[0].get("error")  # 搜索后回读成功
        assert session.state.facts_evidence["inspect"]["source"] == "fake-mcp-3"  # 证据已换新代次
        assert session._host._dangling == []
    finally:
        session.close()


def test_reconnect_clears_cached_candidates():
    clients = []

    class _CandidateClient(_TaggedClient):
        def call_tool(self, name, args):
            result = super().call_tool(name, args)
            if name == "search_knowledge" and args.get("type") == "event":
                result["results"] = [
                    {
                        "doc_id": "EVX-A",
                        "title": "候选 A",
                        "scope_order_id": "SO-1003",
                        "scope_line_id": "001",
                        "recorded_at": "2026-09-07T10:00:00+08:00",
                    }
                ]
            return result

    def factory():
        client = _CandidateClient(len(clients) + 1)
        clients.append(client)
        return client

    session = ChatSession(
        DATASET_ID,
        "t004-full-v2",
        as_of=AS_OF,
        model_client=_ScriptedClient(
            [
                _response(tool_calls=[_call("e1", "search_knowledge", {"query": "待检", "type": "event"})]),
                _response(content="结束"),
                _response(content="结束"),
            ]
        ),
        generation_client=_MockGeneration(),
        transport="mcp",
        host_factory=lambda scope: McpToolHost(scope, client_factory=factory),
    )
    try:
        session.run_turn("找事件")
        assert session.state.last_candidates
        clients[0].close()
        second = session.run_turn("还在吗")
        assert any(r.get("op") == "mcp_reconnect" for r in second["trace"])
        assert session.state.last_candidates == []  # 旧代次候选失效
    finally:
        session.close()


def test_first_start_failure_then_success_is_plain_connect():
    attempts = []

    def factory():
        if not attempts:
            client = _FakeMcpClient(start_error=ToolError("首次启动失败", "internal_error"))
        else:
            client = _TaggedClient(2)
        attempts.append(client)
        return client

    host = McpToolHost(_scope(), client_factory=factory)
    with pytest.raises(ToolError):
        host.tool_schemas()
    assert host.tool_schemas()
    events = host.take_events()
    assert [e["op"] for e in events if e.get("status") == "ok"] == ["mcp_connect"]
    assert host.generation == 1  # 首次成功不虚构旧代次

    session = ChatSession(
        DATASET_ID,
        "t004-full-v2",
        as_of=AS_OF,
        model_client=_ScriptedClient([]),
        generation_client=_MockGeneration(),
        transport="mcp",
        host_factory=lambda scope: host,
    )
    try:
        reply = session.handle_command("/order SO-1003 001")
        assert "已绑定" in reply
        assert session.state.facts_evidence["inspect"]["source"] == "fake-mcp-2"  # 必要事实仍正确预载
    finally:
        assert session.close() is True


# ---------------------------------------------------------------- T-009c F3.1
def test_start_interrupt_closes_created_client(monkeypatch):
    pids_before = _mcp_server_pids()
    original_start = McpServerClient.start
    holder = {}

    def interrupting_start(self):
        original_start(self)  # 真实建立线程/stdio 连接
        holder["client"] = self
        raise KeyboardInterrupt  # 返回 Host 之前中断

    monkeypatch.setattr(McpServerClient, "start", interrupting_start)

    with pytest.raises(KeyboardInterrupt):
        ChatSession(
            DATASET_ID,
            "t004-full-v2",
            order_id="SO-1003",
            line_id="001",
            as_of=AS_OF,
            model_client=_ScriptedClient([]),
            generation_client=_MockGeneration(),
            transport="mcp",
        )
    client = holder["client"]
    assert client.alive is False  # 已创建资源被真实关闭
    assert client._thread is None
    assert _wait_pids_gone(_mcp_server_pids() - pids_before)


def test_client_start_wait_interrupt_is_cleaned(monkeypatch):
    import threading
    import time as _time

    pids_before = _mcp_server_pids()
    client = McpServerClient(_scope())

    class _MainThreadInterruptEvent(threading.Event):
        def wait(self, timeout=None):
            if threading.current_thread() is threading.main_thread():
                _time.sleep(2.0)  # 让后台线程完成子进程与连接建立
                raise KeyboardInterrupt
            return super().wait(timeout)

    monkeypatch.setattr(threading, "Event", _MainThreadInterruptEvent)
    with pytest.raises(KeyboardInterrupt):
        client.start()
    monkeypatch.undo()
    if client._thread is not None:  # 若关闭暂未完成，保留句柄应可重试
        assert client.close() is True
    assert client.alive is False
    assert _wait_pids_gone(_mcp_server_pids() - pids_before)


class _InterruptCloseStubborn(_FakeMcpClient):
    def __init__(self):
        super().__init__()
        self.close_ok = False

    def start(self):
        self.alive = True
        raise KeyboardInterrupt

    def close(self):
        self.closed = True
        if self.close_ok:
            self.alive = False
            return True
        return False


def test_start_interrupt_close_false_kept_and_retried():
    client = _InterruptCloseStubborn()
    host = McpToolHost(_scope(), client_factory=lambda: client)
    with pytest.raises(KeyboardInterrupt):
        host.tool_schemas()
    assert host._dangling == [client]  # 唯一登记，可追踪
    client.close_ok = True
    assert host.close() is True
    assert host._dangling == []
    assert client.alive is False
