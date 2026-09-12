"""T-009a B1/B2 证据采集：真实 stdio 协议、四工具等价与残留进程检查。

用法：``PYTHONPATH=src python3 scripts/t009/protocol_evidence.py``
输出：``docs/L5-执行与验证/evidence/T-009/T-009a-protocol.json``
"""

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pi_market import db  # noqa: E402
from pi_market.agent_tools import Scope, ToolHost  # noqa: E402
from pi_market.mcp_client import McpServerClient  # noqa: E402

DATASET_ID = "ds-61edff98759f"
CORPUS_ID = "t004-full-v2"
AS_OF = "2026-09-09T20:00:00+08:00"
PROTOCOL_VERSION = "2025-11-25"
OUT_PATH = PROJECT_ROOT / "docs" / "L5-执行与验证" / "evidence" / "T-009" / "T-009a-protocol.json"


def _env():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    return env


def _pids():
    out = subprocess.run(["pgrep", "-f", "pi_market.mcp_server"], capture_output=True, text=True)
    return sorted(int(pid) for pid in out.stdout.split() if pid.strip())


def _server_args(scope):
    args = [sys.executable, "-m", "pi_market.mcp_server", "--dataset-id", scope.dataset_id, "--corpus-id", scope.corpus_id]
    if scope.order_id:
        args += ["--order-id", scope.order_id]
    if scope.line_id:
        args += ["--line-id", scope.line_id]
    if scope.as_of:
        args += ["--as-of", scope.as_of]
    return args


def raw_protocol(scope):
    proc = subprocess.Popen(
        _server_args(scope),
        cwd=str(PROJECT_ROOT),
        env=_env(),
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
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "t009-probe", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "inspect_order", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "read_order_evidence", "arguments": {}}},
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "search_knowledge", "arguments": {"query": "包装 受潮", "type": "sop"}},
        },
    ]
    request_lines = [json.dumps(message, ensure_ascii=False) for message in messages]
    responses = []
    try:
        for line in request_lines:
            proc.stdin.write(line + "\n")
        proc.stdin.flush()
        deadline = time.time() + 120
        while time.time() < deadline and len(responses) < 5:
            if not select.select([proc.stdout], [], [], 0.5)[0]:
                if proc.poll() is not None:
                    break
                continue
            line = proc.stdout.readline()
            if not line:
                break
            responses.append(json.loads(line))  # 非 JSON 即污染
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
    return {"requests": request_lines, "responses": responses}


def equivalence(scope):
    client = McpServerClient(scope)
    client.start()
    try:
        from pi_market.mcp_client import McpToolHost

        host = McpToolHost(scope, client_factory=lambda: client)
        direct = ToolHost(scope)

        results = {}
        inspect_mcp = host.call("inspect_order", {})
        inspect_direct = direct.call("inspect_order", {})
        results["inspect_order"] = {
            "equal": inspect_mcp == inspect_direct,
            "lines": len(inspect_mcp.get("facts", {}).get("lines", [])),
        }
        evidence_mcp = host.call("read_order_evidence", {})
        evidence_direct = direct.call("read_order_evidence", {})
        results["read_order_evidence"] = {
            "equal": evidence_mcp == evidence_direct,
            "evidence": len(evidence_mcp.get("evidence", [])),
            "events": len(evidence_mcp.get("events", [])),
        }
        query = {"query": "包装 受潮", "type": "sop"}
        search_mcp = host.call("search_knowledge", dict(query))
        search_direct = direct.call("search_knowledge", dict(query))
        results["search_knowledge"] = {
            "equal_ids": [r["doc_id"] for r in search_mcp["results"]] == [r["doc_id"] for r in search_direct["results"]],
            "hits": len(search_mcp["results"]),
        }
        doc_id = search_mcp["results"][0]["doc_id"]
        doc_mcp = host.call("read_document", {"doc_id": doc_id})
        doc_direct = direct.call("read_document", {"doc_id": doc_id})
        results["read_document"] = {
            "equal": doc_mcp == doc_direct,
            "doc_id": doc_id,
            "sections": len(doc_mcp.get("document", {}).get("sections", [])),
        }
        events = host.take_events()
        return {
            "protocol_version": client.protocol_version,
            "server_info": client.server_info,
            "tools": [
                {"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema}
                for tool in client.tools
            ],
            "results": results,
            "connect_events": events,
        }
    finally:
        client.close()


def main():
    import importlib.metadata as metadata

    db.init_db()
    scope = Scope(DATASET_ID, CORPUS_ID, "SO-1003", "001", AS_OF)

    before = _pids()
    raw = raw_protocol(scope)
    mcp = equivalence(scope)
    time.sleep(0.5)
    after = _pids()

    evidence = {
        "python": sys.version,
        "mcp_sdk_version": metadata.version("mcp"),
        "dataset_id": DATASET_ID,
        "corpus_id": CORPUS_ID,
        "scope": {"order_id": "SO-1003", "line_id": "001", "as_of": AS_OF},
        "raw_stdio": raw,
        "sdk": mcp,
        "leftover_processes": {"before": before, "after": after},
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written {OUT_PATH}")
    print(json.dumps({"results": mcp["results"], "leftover": evidence["leftover_processes"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
