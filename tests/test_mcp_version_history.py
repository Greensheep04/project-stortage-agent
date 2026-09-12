"""T-010c R3：read_document include_superseded 版本沿革（默认兼容/两后端/越权/时间/链/引用）。"""

import json
import sys
from types import SimpleNamespace

import pytest

from pi_market import corpus, corpus_retrieval
from pi_market.agent_tools import Scope, ToolError, ToolHost, validate_tool_call
from pi_market.chat import ChatSession
from pi_market.mcp_client import McpServerClient, McpToolHost
from test_chat_answer_style import _FixedExplanation
from test_chat_bounds import _ScriptedClient, _call, _response
from test_corpus_common import DATASET_ID, REAL_CORPUS_DIR

AS_OF = "2026-09-09T20:00:00+08:00"
JULY = "2026-07-15T12:00:00+08:00"


@pytest.fixture(scope="module", autouse=True)
def _corpus_ready():
    corpus.import_corpus("t004-full-v2", DATASET_ID, files_dir=REAL_CORPUS_DIR)
    corpus.index_corpus("t004-full-v2", "mock-v1", no_embedding=True)
    yield


def _bound_scope(as_of=AS_OF, order_id="SO-1011"):
    return Scope(DATASET_ID, "t004-full-v2", order_id, "001", as_of)


def _sop_host(as_of=AS_OF):
    host = ToolHost(_bound_scope(as_of=as_of))
    host.search_knowledge({"query": "包装 受潮", "type": "sop"})
    return host


# ---------------------------------------------------------------- 默认兼容 / True 回读
def test_default_readback_structure_unchanged():
    host = _sop_host()
    default = host.read_document({"doc_id": "SOP-PACK-v2"})
    assert "version_history" not in default
    assert set(default.keys()) == {"tool", "source", "document"}
    assert default["source"] == "corpus_retrieval.read_corpus_doc（重查时间/范围）"

    host2 = _sop_host()
    explicit = host2.read_document({"doc_id": "SOP-PACK-v2", "include_superseded": False})
    assert explicit == default  # False 与默认逐字一致


def test_true_readback_returns_current_and_metadata_history():
    host = _sop_host()
    result = host.read_document({"doc_id": "SOP-PACK-v2", "include_superseded": True})
    assert result["document"]["doc_id"] == "SOP-PACK-v2"  # document 仍为当前版
    assert result["document"]["version"] == "2"
    history = result["version_history"]
    assert [v["doc_id"] for v in history] == ["SOP-PACK-v1", "SOP-PACK-v2"]  # 按 effective_from 排序
    assert result["version_history_complete"] is True
    first = history[0]
    for key in ("doc_id", "policy_id", "version", "title", "effective_from", "effective_to", "recorded_at", "scope", "source"):
        assert key in first
    assert first["policy_id"] == "SOP-PACK"
    assert first["effective_from"] == "2026-07-01T00:00:00+08:00"
    assert first["effective_to"] == "2026-08-01T00:00:00+08:00"
    assert first["source"]["file"] and first["source"]["row"]
    assert "sections" not in first  # 只返回元数据

    with pytest.raises(ToolError) as unseen:
        host.read_document({"doc_id": "SOP-PACK-v1"})  # 祖先不进入 seen_docs
    assert unseen.value.category == "not_seen"


def test_include_superseded_only_for_sop_and_arguments_strict():
    host = ToolHost(_bound_scope())
    host.seen_docs["CASE-X"] = {"title": "c", "doc_type": "case", "scope": (None, None, None, None)}
    host.seen_docs["EVX-X"] = {"title": "e", "doc_type": "event", "scope": (None, None, None, None)}
    for doc in ("CASE-X", "EVX-X"):
        with pytest.raises(ToolError) as wrong_type:
            host.read_document({"doc_id": doc, "include_superseded": True})
        assert wrong_type.value.category == "invalid_argument"

    with pytest.raises(ToolError) as non_bool:
        validate_tool_call("read_document", {"doc_id": "SOP-PACK-v2", "include_superseded": 1})
    assert non_bool.value.category == "invalid_argument"
    with pytest.raises(ToolError) as extra:
        validate_tool_call(
            "read_document", {"doc_id": "SOP-PACK-v2", "include_superseded": True, "policy_id": "P"}
        )
    assert extra.value.category == "invalid_argument"
    with pytest.raises(ToolError) as unseen_true:
        host.read_document({"doc_id": "SOP-PACK-v1", "include_superseded": True})
    assert unseen_true.value.category == "not_seen"


# ---------------------------------------------------------------- 时间边界
def test_time_boundaries_july_and_september():
    july_host = _sop_host(as_of=JULY)
    july_results = july_host.search_knowledge({"query": "包装 受潮", "type": "sop"})["results"]
    assert july_results and july_results[0]["doc_id"] == "SOP-PACK-v1"  # 7 月只见 v1
    july_read = july_host.read_document({"doc_id": "SOP-PACK-v1", "include_superseded": True})
    assert [v["doc_id"] for v in july_read["version_history"]] == ["SOP-PACK-v1"]
    assert all(v["doc_id"] != "SOP-PACK-v2" for v in july_read["version_history"])  # 不读未来版

    sep_host = _sop_host()
    sep_results = sep_host.search_knowledge({"query": "包装 受潮", "type": "sop"})["results"]
    assert sep_results and sep_results[0]["doc_id"] == "SOP-PACK-v2"
    sep_read = sep_host.read_document({"doc_id": "SOP-PACK-v2", "include_superseded": True})
    assert [v["doc_id"] for v in sep_read["version_history"]] == ["SOP-PACK-v1", "SOP-PACK-v2"]
    assert sep_read["version_history"][0]["effective_to"] == "2026-08-01T00:00:00+08:00"  # 区间端点归 v2


# ---------------------------------------------------------------- 缓存键与范围清理
def test_false_then_true_use_distinct_cache_keys():
    session = ChatSession(
        DATASET_ID,
        "t004-full-v2",
        order_id="SO-1011",
        line_id="001",
        as_of=AS_OF,
        model_client=_ScriptedClient([]),
    )
    try:
        session._execute(_call("s", "search_knowledge", {"query": "包装 受潮", "type": "sop"}))
        false_result, _ = session._execute(_call("f", "read_document", {"doc_id": "SOP-PACK-v2"}))
        true_result, _ = session._execute(
            _call("t", "read_document", {"doc_id": "SOP-PACK-v2", "include_superseded": True})
        )
        assert "version_history" not in false_result
        assert true_result.get("version_history")
        keys = set(session._host.cache.keys())
        assert any("include_superseded" not in key for key in keys)
        assert any("include_superseded" in key for key in keys)
    finally:
        session.close()


def _synthetic_version_result(policy, doc_id, version, complete, reason=None):
    meta = {
        "doc_id": doc_id,
        "policy_id": policy,
        "version": version,
        "title": f"{policy} 制度",
        "effective_from": "2026-07-01T00:00:00+08:00",
        "effective_to": "2026-08-01T00:00:00+08:00",
        "recorded_at": "2026-06-25T09:00:00+08:00",
        "scope": {},
        "source": {"file": "sops.jsonl", "row": 1},
    }
    return {
        "tool": "read_document",
        "source": "test",
        "document": {"doc_id": doc_id, "doc_type": "sop", "policy_id": policy, "title": meta["title"], "sections": []},
        "version_history": [meta],
        "version_history_complete": complete,
        "version_history_reason": reason,
    }


def _version_record(doc_id):
    return {"tool": "read_document", "params": json.dumps({"doc_id": doc_id, "include_superseded": True})}


def _plain_session():
    return ChatSession(DATASET_ID, "t004-full-v2", model_client=_ScriptedClient([]))


def test_version_completeness_persists_and_isolates_policies():
    session = _plain_session()
    try:
        a_incomplete = _synthetic_version_result("POL-A", "DOC-A", "2", False, "沿革链缺口: DOC-A1 不存在")
        b_complete = _synthetic_version_result("POL-B", "DOC-B", "1", True)
        pack = session._build_pack(
            [
                ("read_document", a_incomplete, _version_record("DOC-A")),
                ("read_document", b_complete, _version_record("DOC-B")),
            ]
        )
        assert pack["version_completeness"]["POL-A"]["complete"] is False
        assert pack["version_completeness"]["POL-B"]["complete"] is True  # 同轮互不覆盖

        later = session._build_pack([])  # 下一轮无调用仍保留
        assert later["version_completeness"]["POL-A"] == pack["version_completeness"]["POL-A"]
        assert later["version_completeness"]["POL-B"]["complete"] is True

        a_complete = _synthetic_version_result("POL-A", "DOC-A", "2", True)
        after = session._build_pack([("read_document", a_complete, _version_record("DOC-A"))])
        assert after["version_completeness"]["POL-A"]["complete"] is True  # 自身后续成功才关闭
        assert after["version_completeness"]["POL-B"]["complete"] is True
    finally:
        session.close()


def test_unrelated_policy_gap_not_shown_in_text_and_prompt():
    generation = _FixedExplanation("POL-B 现行版本为 v1。")
    session = ChatSession(
        DATASET_ID,
        "t004-full-v2",
        order_id="SO-1003",
        line_id="001",
        as_of=AS_OF,
        model_client=_ScriptedClient([]),
        generation_client=generation,
    )
    try:
        session.state.readback_versions = {
            "DOC-A": {
                "doc_id": "DOC-A",
                "policy_id": "POL-A",
                "title": "包装异常出库复核办法",
                "version": "2",
            },
            "DOC-B": {
                "doc_id": "DOC-B",
                "policy_id": "POL-B",
                "title": "分批交接核对办法",
                "version": "1",
            },
        }
        session.state.readback_version_status = {
            "POL-A": {"complete": False, "reason": "沿革链缺口"},
            "POL-B": {"complete": True, "reason": None},
        }
        result = session._final_answer("分批交接核对办法现在有哪些版本？", [], False)
        prompt = generation.prompts[-1]
        assert "POL-A" not in prompt  # 无关政策缺口不进入生成
        assert "POL-B" in prompt
        assert "版本沿革不完整" not in result["text"]
    finally:
        session.close()


def test_scope_change_and_reconnect_clear_version_cache():
    session = ChatSession(
        DATASET_ID,
        "t004-full-v2",
        order_id="SO-1011",
        line_id="001",
        as_of=AS_OF,
        model_client=_ScriptedClient([]),
    )
    try:
        session.state.readback_versions["SOP-PACK-v1"] = {"doc_id": "SOP-PACK-v1"}
        session.state.readback_version_status["SOP-PACK"] = {"complete": False, "reason": "缺口"}
        session._apply_host_events([{"op": "mcp_reconnect", "cache_invalidated": True}])
        assert session.state.readback_versions == {}
        assert session.state.readback_version_status == {}
        session.state.readback_versions["SOP-PACK-v1"] = {"doc_id": "SOP-PACK-v1"}
        session.state.readback_version_status["SOP-PACK"] = {"complete": False, "reason": "缺口"}
        session.handle_command("/reset")
        assert session.state.readback_versions == {}
        assert session.state.readback_version_status == {}
    finally:
        session.close()


# ---------------------------------------------------------------- 链与错误
def test_missing_chain_and_cycle_are_reported(monkeypatch):
    original = corpus_retrieval._fetch_corpus_doc

    def missing(cur, corpus_id, doc_id):
        if doc_id == "SOP-PACK-v1":
            return None
        return original(cur, corpus_id, doc_id)

    monkeypatch.setattr(corpus_retrieval, "_fetch_corpus_doc", missing)
    result = corpus_retrieval.read_corpus_doc_history(
        "t004-full-v2", "SOP-PACK-v2", as_of=AS_OF, warehouse="WH-01"
    )
    assert result["complete"] is False and "缺口" in result["reason"]
    assert [v["doc_id"] for v in result["versions"]] == ["SOP-PACK-v2"]

    def cyclic(cur, corpus_id, doc_id):
        row = original(cur, corpus_id, doc_id)
        if row is not None and doc_id == "SOP-PACK-v1":
            row = dict(row)
            row["raw"] = {**row["raw"], "supersedes": "SOP-PACK-v1"}
        return row

    monkeypatch.setattr(corpus_retrieval, "_fetch_corpus_doc", cyclic)
    result = corpus_retrieval.read_corpus_doc_history(
        "t004-full-v2", "SOP-PACK-v2", as_of=AS_OF, warehouse="WH-01"
    )
    assert result["complete"] is False and "循环" in result["reason"]


def _patched_ancestor(monkeypatch, **overrides):
    original = corpus_retrieval._fetch_corpus_doc

    def patched(cur, corpus_id, doc_id):
        row = original(cur, corpus_id, doc_id)
        if row is not None and doc_id == "SOP-PACK-v1":
            row = dict(row)
            row.update(overrides)
        return row

    monkeypatch.setattr(corpus_retrieval, "_fetch_corpus_doc", patched)


def test_ancestor_policy_scope_and_future_time_rejected(monkeypatch):
    import datetime

    from psycopg.types.datetime import TimestamptzLoader  # noqa: F401  (确保时区类型可用)

    # 异 policy：沿革链终止且不返回被拒祖先元数据
    _patched_ancestor(monkeypatch, policy_id="SOP-OTHER")
    result = corpus_retrieval.read_corpus_doc_history(
        "t004-full-v2", "SOP-PACK-v2", as_of=AS_OF, warehouse="WH-01"
    )
    assert result["complete"] is False
    assert [v["doc_id"] for v in result["versions"]] == ["SOP-PACK-v2"]

    # 范围不符：仓库不匹配
    _patched_ancestor(monkeypatch, scope_warehouse="WH-99")
    result = corpus_retrieval.read_corpus_doc_history(
        "t004-full-v2", "SOP-PACK-v2", as_of=AS_OF, warehouse="WH-01"
    )
    assert result["complete"] is False
    assert [v["doc_id"] for v in result["versions"]] == ["SOP-PACK-v2"]

    # 未来 recorded_at / effective_from 不得越权返回
    future = datetime.datetime(2027, 1, 1, tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
    _patched_ancestor(monkeypatch, recorded_at=future)
    result = corpus_retrieval.read_corpus_doc_history(
        "t004-full-v2", "SOP-PACK-v2", as_of=AS_OF, warehouse="WH-01"
    )
    assert result["complete"] is False
    assert [v["doc_id"] for v in result["versions"]] == ["SOP-PACK-v2"]

    _patched_ancestor(monkeypatch, effective_from=future)
    result = corpus_retrieval.read_corpus_doc_history(
        "t004-full-v2", "SOP-PACK-v2", as_of=AS_OF, warehouse="WH-01"
    )
    assert result["complete"] is False
    assert [v["doc_id"] for v in result["versions"]] == ["SOP-PACK-v2"]


def test_august_first_boundary_and_before_v1_start():
    boundary = "2026-08-01T00:00:00+08:00"
    host = ToolHost(_bound_scope(as_of=boundary))
    results = host.search_knowledge({"query": "包装 受潮", "type": "sop"})["results"]
    assert results and results[0]["doc_id"] == "SOP-PACK-v2"  # 8 月 1 日当前版为 v2
    history = host.read_document({"doc_id": "SOP-PACK-v2", "include_superseded": True})["version_history"]
    assert [v["doc_id"] for v in history] == ["SOP-PACK-v1", "SOP-PACK-v2"]

    just_before = "2026-07-31T23:59:59+08:00"
    host2 = ToolHost(_bound_scope(as_of=just_before))
    results2 = host2.search_knowledge({"query": "包装 受潮", "type": "sop"})["results"]
    assert results2 and results2[0]["doc_id"] == "SOP-PACK-v1"

    before_v1 = "2026-06-30T12:00:00+08:00"
    host3 = ToolHost(_bound_scope(as_of=before_v1))
    results3 = host3.search_knowledge({"query": "包装 受潮", "type": "sop"})["results"]
    assert all(row["doc_id"] != "SOP-PACK-v1" for row in results3)  # v1 生效前不得出现
    with pytest.raises(ToolError) as unseen:
        host3.read_document({"doc_id": "SOP-PACK-v1", "include_superseded": True})
    assert unseen.value.category == "not_seen"


def test_equivalent_timezone_representation_matches():
    shanghai = ToolHost(_bound_scope(as_of="2026-08-01T00:00:00+08:00"))
    utc = ToolHost(_bound_scope(as_of="2026-07-31T16:00:00+00:00"))
    s = shanghai.search_knowledge({"query": "包装 受潮", "type": "sop"})["results"]
    u = utc.search_knowledge({"query": "包装 受潮", "type": "sop"})["results"]
    assert [r["doc_id"] for r in s] == [r["doc_id"] for r in u]
    sh = shanghai.read_document({"doc_id": "SOP-PACK-v2", "include_superseded": True})
    uh = utc.read_document({"doc_id": "SOP-PACK-v2", "include_superseded": True})
    assert [v["doc_id"] for v in sh["version_history"]] == [v["doc_id"] for v in uh["version_history"]]


def test_database_failure_is_internal_error(monkeypatch):
    def broken(cur, corpus_id, doc_id):
        if doc_id == "SOP-PACK-v2":
            raise RuntimeError("数据库连接中断")
        raise AssertionError("不应读取祖先")

    monkeypatch.setattr(corpus_retrieval, "_fetch_corpus_doc", broken)
    host = ToolHost(_bound_scope())
    host.seen_docs["SOP-PACK-v2"] = {"title": "s", "doc_type": "sop", "scope": ("WH-01", None, None, None)}
    with pytest.raises(ToolError) as failure:
        host.read_document({"doc_id": "SOP-PACK-v2", "include_superseded": True})
    assert failure.value.category == "internal_error"


# ---------------------------------------------------------------- 生成与引用
class _VersionGeneration:
    def __init__(self, payload):
        self.payload = payload
        self.last_usage = {"total_tokens": 1}
        self.prompts = []

    def generate(self, prompt):
        self.prompts.append(prompt)
        return json.dumps(self.payload, ensure_ascii=False)


def _version_payload():
    return {
        "explanation": "2026-08-01 起为 v2。",
        "used_evidence_ids": [],
        "used_event_ids": [],
        "used_history_ids": [],
        "used_version_ids": ["SOP-PACK-v1", "SOP-NOPE-v9"],
        "suggestions": [{"suggestion": "按旧版步骤处理", "rule_ids": ["SOP-PACK-v1"]}],
        "open_questions": [],
    }


def test_version_prompt_separates_group_and_binds_citations():
    generation = _VersionGeneration(_version_payload())
    session = ChatSession(
        DATASET_ID,
        "t004-full-v2",
        order_id="SO-1011",
        line_id="001",
        as_of=AS_OF,
        model_client=_ScriptedClient(
            [
                _response(
                    tool_calls=[
                        _call("s", "search_knowledge", {"query": "包装 受潮", "type": "sop"}),
                        _call("t", "read_document", {"doc_id": "SOP-PACK-v2", "include_superseded": True}),
                    ]
                ),
                _response(content="结束"),
            ]
        ),
        generation_client=generation,
    )
    try:
        result = session.run_turn("包装受潮规范有哪些版本？")
        prompt = generation.prompts[-1]
        versions_section = prompt.split("五、版本沿革")[1].split("用户问题")[0]
        rules_section = prompt.split("四、有效规则")[1].split("五、版本沿革")[0]
        assert "SOP-PACK-v1" in versions_section and "SOP-PACK-v2" in versions_section
        assert "SOP-PACK-v1" not in rules_section  # 旧版不入当前规则组
        facts_section = prompt.split("一、程序事实")[1].split("二、本单证据")[0]
        assert "SO-1011" not in facts_section  # 版本问题不夹带账目
        assert "区间为 [effective_from, effective_to)" in prompt

        explanation = result["explanation"]
        assert explanation["used_version_ids"] == ["SOP-PACK-v1"]  # 伪造版本被丢弃
        assert "SOP-NOPE-v9" in explanation["dropped_citations"]
        assert explanation["version_citations"][0]["effective_to"] == "2026-08-01T00:00:00+08:00"
        assert explanation["suggestions"] == []  # 旧版不能作为当前建议依据
        assert result["context_selection"]["policy_versions"] is True
    finally:
        session.close()


# ---------------------------------------------------------------- 真实 MCP 后端一致性
def test_mcp_backend_version_history_parity():
    scope = _bound_scope()
    direct = ToolHost(scope)
    direct.search_knowledge({"query": "包装 受潮", "type": "sop"})
    expected = direct.read_document({"doc_id": "SOP-PACK-v2", "include_superseded": True})

    client = McpServerClient(scope)
    client.start()
    try:
        names = sorted(tool.name for tool in client.tools)
        assert names == ["inspect_order", "read_document", "read_order_evidence", "search_knowledge"]
        schema = next(t for t in client.tools if t.name == "read_document").input_schema
        assert schema["properties"]["include_superseded"]["type"] == "boolean"
        host = McpToolHost(scope, client_factory=lambda: client)
        host.call("search_knowledge", {"query": "包装 受潮", "type": "sop"})
        result = host.call("read_document", {"doc_id": "SOP-PACK-v2", "include_superseded": True})
        assert result["document"] == expected["document"]
        assert result["version_history"] == expected["version_history"]
        assert result["version_history_complete"] is True
        with pytest.raises(ToolError) as extra:
            host.call("read_document", {"doc_id": "SOP-PACK-v2", "policy_id": "SOP-PACK"})
        assert extra.value.category == "invalid_argument"
    finally:
        assert client.close() is True
