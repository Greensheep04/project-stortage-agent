"""T-008a 工具校验、权限与范围注入拒绝（离线，测试库真实实现连接）。"""

import json

import pytest

from pi_market import corpus
from pi_market.agent_tools import Scope, ToolError, ToolHost
from test_corpus_common import DATASET_ID, REAL_CORPUS_DIR


@pytest.fixture(scope="module", autouse=True)
def _corpus_ready():
    corpus.import_corpus("t004-full-v2", DATASET_ID, files_dir=REAL_CORPUS_DIR)
    corpus.index_corpus("t004-full-v2", "mock-v1", no_embedding=True)
    yield


def _bound_host():
    return ToolHost(Scope(DATASET_ID, "t004-full-v2", "SO-1003", "001", None))


def _unbound_host():
    return ToolHost(Scope(DATASET_ID, "t004-full-v2"))


def test_unknown_tool_rejected():
    with pytest.raises(ToolError) as exc:
        _bound_host().call("run_sql", {"sql": "select 1"})
    assert exc.value.category == "unknown_tool"


def test_inspect_and_evidence_reject_arguments():
    host = _bound_host()
    for name in ("inspect_order", "read_order_evidence"):
        with pytest.raises(ToolError) as exc:
            host.call(name, {"order_id": "SO-9999"})
        assert exc.value.category == "invalid_argument"


def test_unbound_order_tools_rejected():
    host = _unbound_host()
    for name in ("inspect_order", "read_order_evidence"):
        with pytest.raises(ToolError) as exc:
            host.call(name, {})
        assert exc.value.category == "no_scope"


def test_search_knowledge_validates_params_and_scope_injection():
    host = _bound_host()
    with pytest.raises(ToolError):
        host.call("search_knowledge", {"query": "", "type": "sop"})
    with pytest.raises(ToolError):
        host.call("search_knowledge", {"query": "x", "type": "all"})
    with pytest.raises(ToolError) as exc:
        host.call("search_knowledge", {"query": "x", "type": "sop", "order_id": "SO-9999"})
    assert exc.value.category == "invalid_argument"
    with pytest.raises(ToolError) as exc:
        host.call("search_knowledge", "not-json")
    assert exc.value.category == "invalid_argument"


def test_unbound_only_event_locating():
    host = _unbound_host()
    with pytest.raises(ToolError) as exc:
        host.call("search_knowledge", {"query": "包装复核", "type": "case"})
    assert exc.value.category == "unbound_restricted"
    result = host.call("search_knowledge", {"query": "包装复核", "type": "event"})
    assert result["type"] == "event"
    assert isinstance(result["results"], list)


def test_read_document_paths_and_unseen_rejected():
    host = _bound_host()
    for bad in ("../../.env", "tests/eval/t007/test_queries_v2.jsonl", "/etc/passwd", ""):
        with pytest.raises(ToolError) as exc:
            host.call("read_document", {"doc_id": bad})
        assert exc.value.category == "invalid_argument"
    with pytest.raises(ToolError) as exc:
        host.call("read_document", {"doc_id": "SOP-ARCHIVE-v2"})
    assert exc.value.category == "not_seen"


def test_bound_tools_return_real_reconcile_and_evidence():
    host = _bound_host()
    inspect = host.call("inspect_order", {})
    assert inspect["source"] == "reconcile.reconcile"
    line = inspect["facts"]["lines"][0]
    assert line["order_id"] == "SO-1003" and line["line_id"] == "001"
    evidence = host.call("read_order_evidence", {})
    evidence_ids = {e["evidence_id"] for e in evidence["evidence"]}
    assert {"TX-1005", "TX-1006", "EV-1001"} <= evidence_ids
    assert evidence["events"]  # 语料新事件按订单/明细精确读取


def test_search_and_readback_scoped():
    host = _bound_host()
    result = host.call("search_knowledge", {"query": "包装受潮 复核 待检", "type": "sop"})
    assert result["results"]
    doc_id = result["results"][0]["doc_id"]
    document = host.call("read_document", {"doc_id": doc_id})
    assert document["document"]["doc_id"] == doc_id
    assert document["document"]["sections"]
