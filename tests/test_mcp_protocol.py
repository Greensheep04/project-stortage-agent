"""T-009a M1/B1–B3：真实子进程 MCP 协议、业务等价、边界与生命周期。

不调用付费模型；子进程连接 conftest 指向的隔离测试库。
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pi_market import config, corpus
from pi_market.agent_tools import Scope, ToolError, ToolHost
from pi_market.mcp_client import McpServerClient, McpToolHost
from test_corpus_common import DATASET_ID, REAL_CORPUS_DIR

AS_OF = "2026-09-09T20:00:00+08:00"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROTOCOL_FALLBACK = "2025-11-25"


@pytest.fixture(scope="module", autouse=True)
def _corpus_ready():
    corpus.import_corpus("t004-full-v2", DATASET_ID, files_dir=REAL_CORPUS_DIR)
    corpus.index_corpus("t004-full-v2", "mock-v1", no_embedding=True)
    yield


def _scope(order_id="SO-1003", line_id="001", as_of=AS_OF):
    return Scope(DATASET_ID, "t004-full-v2", order_id, line_id, as_of)


def _child_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    return env


def _mcp_server_pids():
    out = subprocess.run(["pgrep", "-f", "pi_market.mcp_server"], capture_output=True, text=True)
    return {int(pid) for pid in out.stdout.split() if pid.strip()}


def _wait_pids_gone(pids, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not (pids & _mcp_server_pids()):
            return True
        time.sleep(0.2)
    return not (pids & _mcp_server_pids())


# ---------------------------------------------------------------- B1 启动与四工具
def test_real_stdio_initialize_tools_list_and_four_calls():
    client = McpServerClient(_scope())
    try:
        client.start()
        assert client.protocol_version
        assert client.server_info and "pi-market-tools" in client.server_info
        names = sorted(tool.name for tool in client.tools)
        assert names == ["inspect_order", "read_document", "read_order_evidence", "search_knowledge"]

        host = McpToolHost(_scope(), client_factory=lambda: client)
        direct = ToolHost(_scope())

        inspect = host.call("inspect_order", {})
        assert inspect == direct.call("inspect_order", {})
        evidence = host.call("read_order_evidence", {})
        assert evidence == direct.call("read_order_evidence", {})

        query = {"query": "包装 受潮 规范", "type": "sop"}
        mcp_search = host.call("search_knowledge", dict(query))
        direct_search = direct.call("search_knowledge", dict(query))
        assert [r["doc_id"] for r in mcp_search["results"]] == [
            r["doc_id"] for r in direct_search["results"]
        ]
        assert mcp_search["results"], "SOP 候选不应为空"

        doc_id = mcp_search["results"][0]["doc_id"]
        mcp_doc = host.call("read_document", {"doc_id": doc_id})
        direct_doc = direct.call("read_document", {"doc_id": doc_id})
        assert mcp_doc == direct_doc
        assert mcp_doc["document"]["doc_id"] == doc_id
    finally:
        assert client.close() is True
        assert client.alive is False


def test_raw_stdio_protocol_has_no_log_pollution():
    import select

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pi_market.mcp_server",
            "--dataset-id",
            DATASET_ID,
            "--corpus-id",
            "t004-full-v2",
            "--order-id",
            "SO-1003",
            "--line-id",
            "001",
            "--as-of",
            AS_OF,
        ],
        cwd=str(PROJECT_ROOT),
        env=_child_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_FALLBACK,
                "capabilities": {},
                "clientInfo": {"name": "raw-probe", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "inspect_order", "arguments": {}},
        },
    ]
    replies = {}
    raw_lines = []
    try:
        for message in messages:
            proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        deadline = time.time() + 60
        while time.time() < deadline and 3 not in replies:
            if not select.select([proc.stdout], [], [], 0.5)[0]:
                if proc.poll() is not None:
                    break
                continue
            line = proc.stdout.readline()
            if not line:
                break
            raw_lines.append(line)
            parsed = json.loads(line)  # 非 JSON 即协议污染
            if "id" in parsed:
                replies[parsed["id"]] = parsed
        assert replies[1]["result"]["protocolVersion"]
        tools = replies[2]["result"]["tools"]
        assert sorted(t["name"] for t in tools) == [
            "inspect_order",
            "read_document",
            "read_order_evidence",
            "search_knowledge",
        ]
        assert replies[2]["result"]["tools"][0]["inputSchema"]["type"] == "object"
        call_text = replies[3]["result"]["content"][0]["text"]
        assert json.loads(call_text)["type"] == "ok"
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
        stderr = proc.stderr.read()
    assert raw_lines and all(json.loads(line) for line in raw_lines)
    # stdout 已逐行校验为 JSON-RPC；诊断日志由 SDK 走 stderr，可能为空


# ---------------------------------------------------------------- B2 边界拒绝
def test_unknown_tool_and_scope_arguments_rejected_before_rpc():
    client = McpServerClient(_scope())
    client.start()
    host = McpToolHost(_scope(), client_factory=lambda: client)
    try:
        with pytest.raises(ToolError) as unknown:
            host.call("drop_table", {})
        assert unknown.value.category == "unknown_tool"

        with pytest.raises(ToolError) as extra:
            host.call("search_knowledge", {"query": "包装", "type": "sop", "as_of": "2026-01-01T00:00:00+08:00"})
        assert extra.value.category == "invalid_argument"

        with pytest.raises(ToolError) as scoped:
            host.call("inspect_order", {"order_id": "SO-1007"})
        assert scoped.value.category == "invalid_argument"

        with pytest.raises(ToolError) as bad_doc:
            host.call("read_document", {"doc_id": "../etc/passwd"})
        assert bad_doc.value.category == "invalid_argument"
    finally:
        client.close()


def _call_raw(client, name, arguments):
    import asyncio

    future = asyncio.run_coroutine_threadsafe(client._session.call_tool(name, arguments), client._loop)
    return future.result(timeout=30)


def test_server_side_rejects_extra_arguments_unknown_tool_and_maps_errors():
    client = McpServerClient(_scope())
    client.start()
    try:
        extra = _call_raw(
            client,
            "read_document",
            {"doc_id": "SOP-NO-SUCH", "as_of": "2026-01-01T00:00:00+08:00"},
        )
        extra_payload = json.loads(extra.content[0].text)
        assert extra_payload["type"] == "error"
        assert extra_payload["error"]["category"] == "invalid_argument"

        unknown = _call_raw(client, "drop_table", {})
        unknown_payload = json.loads(unknown.content[0].text)
        assert unknown_payload["type"] == "error"
        assert unknown_payload["error"]["category"] == "unknown_tool"

        unseen = _call_raw(client, "read_document", {"doc_id": "SOP-NO-SUCH"})
        assert unseen.is_error is False
        payload = json.loads(unseen.content[0].text)
        assert payload["type"] == "error"
        assert payload["error"]["category"] == "not_seen"
    finally:
        client.close()


def test_host_maps_tool_error_categories():
    client = McpServerClient(_scope())
    client.start()
    host = McpToolHost(_scope(), client_factory=lambda: client)
    try:
        with pytest.raises(ToolError) as extra:
            host.call("read_document", {"doc_id": "X", "order_id": "SO-1"})
        assert extra.value.category == "invalid_argument"
        with pytest.raises(ToolError) as unseen:
            host.call("read_document", {"doc_id": "SOP-NO-SUCH"})
        assert unseen.value.category == "not_seen"
    finally:
        client.close()

    unbound_host = McpToolHost(
        Scope(DATASET_ID, "t004-full-v2"),
        client_factory=lambda: McpServerClient(Scope(DATASET_ID, "t004-full-v2")),
    )
    try:
        with pytest.raises(ToolError) as unbound:
            unbound_host.call("search_knowledge", {"query": "换货", "type": "case"})
        assert unbound.value.category == "unbound_restricted"
    finally:
        unbound_host.close()


# ---------------------------------------------------------------- B3 生命周期
def test_scope_change_closes_old_process_without_leftover():
    unbound = McpServerClient(Scope(DATASET_ID, "t004-full-v2"))
    unbound.start()
    pids = _mcp_server_pids()
    assert pids
    try:
        host = McpToolHost(Scope(DATASET_ID, "t004-full-v2"), client_factory=lambda: unbound)
        found = host.call("search_knowledge", {"query": "待检 事件", "type": "event"})
        assert found["results"]
    finally:
        assert unbound.close() is True
    assert _wait_pids_gone(pids), f"旧进程未退出: {pids}"


def test_dead_server_is_structured_failure_and_next_call_recovers():
    client = McpServerClient(_scope())
    client.start()
    host = McpToolHost(_scope(), client_factory=lambda: client)
    pids = _mcp_server_pids()
    assert pids
    os.kill(next(iter(pids)), signal.SIGKILL)
    deadline = time.time() + 10
    while client.alive and time.time() < deadline:
        time.sleep(0.2)
    with pytest.raises(ToolError) as dead:
        host.call("inspect_order", {})
    assert dead.value.category == "internal_error"

    recovered = host.call("inspect_order", {})
    assert recovered["tool"] == "inspect_order"
    assert not (pids & _mcp_server_pids())  # 旧进程已不在
    new_pids = _mcp_server_pids()
    assert new_pids
    finally_clean = host.close()
    assert finally_clean is True
    assert _wait_pids_gone(new_pids)


def test_slow_start_initialize_succeeds_and_call_timeout_mapped():
    """R1 配对：夹具启动慢于 call_timeout(0.5s)，但 initialize 不因此超时；调用超时正确映射。"""
    slow_script = PROJECT_ROOT / "scripts" / "t009" / "slow_tool_server.py"
    client = McpServerClient(
        _scope(),
        command=sys.executable,
        args=[str(slow_script)],
        call_timeout=0.5,
    )
    try:
        client.start()
        assert client.protocol_version  # 慢启动（sleep 1.5s）下 initialize 成功
        assert [tool.name for tool in client.tools] == ["inspect_order"]
        with pytest.raises(ToolError) as timeout:
            client.call_tool("inspect_order", {})
        assert timeout.value.category == "internal_error"
        assert "inspect_order" in str(timeout.value)
    finally:
        assert client.close() is True
