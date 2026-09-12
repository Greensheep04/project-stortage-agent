"""T-008c/d 会话行为：R1 reset、R2 候选与序号、R3 失败四态与材料级恢复、R4 仅回读可建议且范围需证据、R5 请求计数与用量、R6 意图。"""

import json
import re
from types import SimpleNamespace

import httpx
import pytest

from pi_market import chat, corpus, explain
from pi_market.chat import MAX_PLANNING_REQUESTS, MAX_TOOL_REQUESTS, ChatSession
from test_corpus_common import DATASET_ID, REAL_CORPUS_DIR

AS_OF = "2026-09-09T20:00:00+08:00"


@pytest.fixture(scope="module", autouse=True)
def _corpus_ready():
    corpus.import_corpus("t004-full-v2", DATASET_ID, files_dir=REAL_CORPUS_DIR)
    corpus.index_corpus("t004-full-v2", "mock-v1", no_embedding=True)
    yield


def _call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments, ensure_ascii=False)),
    )


def _response(tool_calls=None, content=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls or []))]
    )


class _ScriptedClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.last_usage = None

    def create(self, messages, tools=None):
        self.calls += 1
        if not self.responses:
            return _response(content="（无更多工具）")
        return self.responses.pop(0)


class _MockGeneration:
    """离线最终生成替身：从提示词“四、有效规则”段取一个 chunk_id 作为建议依据。"""

    def __init__(self):
        self.last_usage = {"total_tokens": 3, "model": "mock-generation"}
        self.prompts = []

    def generate(self, prompt):
        self.prompts.append(prompt)
        rules_part = prompt.split("四、有效规则")[-1]
        match = re.search(r'"chunk_id": "([^"]+)"', rules_part)
        rule_ids = [match.group(1)] if match else []
        suggestions = [{"suggestion": "按所给规范处理并留存记录", "rule_ids": rule_ids}] if rule_ids else []
        return json.dumps(
            {
                "explanation": "依据程序事实与本单证据整理。",
                "used_evidence_ids": [],
                "suggestions": suggestions,
                "open_questions": [],
            },
            ensure_ascii=False,
        )


def _facts_fixture():
    return {
        "order_id": "SO-1003",
        "line_id": "001",
        "as_of": AS_OF,
        "lines": [
            {
                "order_id": "SO-1003",
                "line_id": "001",
                "warehouse": "WH-01",
                "sku": "BAG-KH",
                "planned_quantity": 10,
                "matched_net_shipped_quantity": 10,
                "difference_quantity": 0,
                "status_tags": ["split_matched"],
                "evidence_ids": ["TX-1005"],
            }
        ],
    }


def _event_row(doc_id="EVX-TEST-1", order_id="SO-1003", line_id="001", title="测试事件"):
    return {
        "doc_id": doc_id,
        "doc_type": "event",
        "title": title,
        "content": f"{title}：现场记录。",
        "recorded_at": "2026-09-07T10:00:00+08:00",
        "occurred_at": "2026-09-07T09:50:00+08:00",
        "scope_order_id": order_id,
        "scope_line_id": line_id,
        "scope_warehouse": "WH-01",
        "scope_sku": "BAG-KH",
        "jsonl_file": "events.jsonl",
        "jsonl_row": 1,
    }


class _FakeHost:
    """假宿主：可控的核对/检索/回读与失败注入（不出数据库与外部 API）。"""

    def __init__(
        self,
        scope,
        *,
        search_results=None,
        documents=None,
        fail_tools=None,
        valid_orders=("SO-1003", "SO-1007"),
    ):
        self.scope = scope
        self.cache = {}
        self.seen_docs = {}
        self.calls = []
        self._fail = set(fail_tools or ())
        self._search_results = search_results or {}
        self._documents = documents or {}
        self._valid_orders = set(valid_orders)

    def inspect_order(self):
        if "inspect_order" in self._fail:
            raise RuntimeError("inspect_order 服务失败")
        if self.scope.order_id not in self._valid_orders:
            raise RuntimeError(f"订单明细不存在: {self.scope.order_id}")
        facts = _facts_fixture()
        facts["order_id"] = self.scope.order_id
        facts["line_id"] = self.scope.line_id or "001"
        return {"tool": "inspect_order", "source": "fake", "facts": facts}

    def read_order_evidence(self):
        if "read_order_evidence" in self._fail:
            raise RuntimeError("read_order_evidence 服务失败")
        return {
            "tool": "read_order_evidence",
            "source": "fake",
            "evidence": [
                {
                    "evidence_id": "TX-1005",
                    "evidence_type": "flow",
                    "order_id": self.scope.order_id,
                    "line_id": self.scope.line_id or "001",
                    "content": "首批 6 件已过账。",
                    "source": {"filename": "fake.xlsx", "sheet": "仓储流水", "row": 6},
                }
            ],
            "events": [
                {
                    "doc_id": "EVX-FAKE-1",
                    "line_id": self.scope.line_id or "001",
                    "title": "本单测试事件",
                    "recorded_at": "2026-09-07T10:00:00+08:00",
                    "occurred_at": "2026-09-07T09:50:00+08:00",
                    "sections": [{"section_id": "s1", "heading": "记录", "text": "现场记录。"}],
                    "source": {"file": "events.jsonl", "row": 1},
                }
            ],
        }

    def call(self, name, arguments):
        from pi_market.agent_tools import ToolError

        self.calls.append((name, arguments))
        if name in self._fail:
            raise RuntimeError(f"{name} 服务失败")
        args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
        if name == "search_knowledge":
            if args["type"] != "event" and not self.scope.order_id:
                raise ToolError("未绑定订单时仅允许 event 定位搜索与 read_document 回读", "unbound_restricted")
            return {
                "tool": "search_knowledge",
                "type": args["type"],
                "source": "fake",
                "results": self._search_results.get(args["type"], []),
            }
        if name == "read_document":
            document = self._documents.get(args["doc_id"])
            if document is None:
                raise ToolError(f"文档 {args['doc_id']} 不在本会话的工具结果中", "not_seen")
            return {"tool": "read_document", "source": "fake", "document": document}
        raise AssertionError(f"unexpected tool {name}")


def _sop_row(doc_id="SOP-FAKE-v1", chunk_id=None):
    return {
        "doc_id": doc_id,
        "doc_type": "sop",
        "chunk_id": chunk_id or f"{DATASET_ID}:{doc_id}:scope",
        "title": f"{doc_id} 假制度",
        "heading": "适用范围",
        "content": f"{doc_id}\n适用范围\n仅用于测试的适用范围。",
        "recorded_at": "2026-07-01T09:00:00+08:00",
        "closed_at": None,
        "effective_from": "2026-07-01T00:00:00+08:00",
        "effective_to": None,
        "policy_id": "SOP-FAKE",
        "version": "1",
        "jsonl_file": "sops.jsonl",
        "jsonl_row": 1,
    }


def _sop_document(doc_id="SOP-FAKE-v1"):
    return {
        "corpus_id": "t004-full-v2",
        "dataset_id": DATASET_ID,
        "as_of": AS_OF,
        "doc_id": doc_id,
        "doc_type": "sop",
        "title": f"{doc_id} 假制度",
        "recorded_at": "2026-07-01T09:00:00+08:00",
        "closed_at": None,
        "effective_from": "2026-07-01T00:00:00+08:00",
        "effective_to": None,
        "policy_id": "SOP-FAKE",
        "version": "1",
        "jsonl_file": "sops.jsonl",
        "jsonl_row": 1,
        "sections": [
            {"section_id": "scope", "heading": "适用范围", "text": "仅用于测试的适用范围。"},
            {"section_id": "steps", "heading": "步骤", "text": "先回读再建议。"},
        ],
        "raw": {"scope": {"order_id": None, "line_id": None}},
    }


def _session(responses, *, host=None, fake_host_kwargs=None, **kwargs):
    client = _ScriptedClient(responses)
    generation = kwargs.pop("generation_client", None) or _MockGeneration()
    fake_host = host or _FakeHost(
        SimpleNamespace(), **(fake_host_kwargs or {})
    )

    def _factory(scope):
        fake_host.scope = scope
        return fake_host

    session = ChatSession(
        DATASET_ID,
        "t004-full-v2",
        model_client=client,
        generation_client=generation,
        host_factory=_factory,
        **kwargs,
    )
    session._mock_generation = generation
    session._fake_host = fake_host
    return session, client


# --------------------------------------------------------------------- R1 reset
def test_reset_clears_scope_and_does_not_rebind_startup_order():
    session, _ = _session([_response(content="结束")], order_id="SO-1003", line_id="001")
    session.run_turn("核对")
    reply = session.handle_command("/reset")
    assert "已重置" in reply and "数据集默认" in reply
    assert session.state.order_id is None
    assert session.state.line_id is None
    assert session.state.as_of is None
    assert session.state.facts_evidence is None
    assert session.state.recent_turns == []
    assert session.state.last_candidates == []


def test_reset_from_unbound_startup_with_asof_clears_override():
    session, _ = _session([_response(content="结束")], as_of="2024-01-01T00:00:00+08:00")
    assert session.state.as_of == "2024-01-01T00:00:00+08:00"
    session.handle_command("/reset")
    assert session.state.as_of is None
    assert session.state.order_id is None


# --------------------------------------------------------------------- R2 候选
def test_unbound_candidates_listed_with_orders_and_ordinal():
    session, _ = _session(
        [
            _response(tool_calls=[_call("e", "search_knowledge", {"query": "待检 事件", "type": "event"})]),
            _response(content="结束"),
        ],
        host=_FakeHost(SimpleNamespace(), search_results={"event": [_event_row(), _event_row("EVX-TEST-2", "SO-1007", "002", "第二批事件")]}),
    )
    first = session.run_turn("找待检事件")
    assert session.state.order_id is None
    assert "1." in first["text"] and "订单 SO-" in first["text"]
    candidates = session.state.last_candidates
    assert candidates and candidates[0]["order_id"]

    second = session.run_turn("第二条是哪个订单？")
    assert "第 2 条候选" in second["text"]
    assert session.state.order_id is None  # 不自动绑定


def test_unbound_no_candidates_message():
    session, _ = _session(
        [
            _response(tool_calls=[_call("e", "search_knowledge", {"query": "zzzz", "type": "event"})]),
            _response(content="结束"),
        ],
        host=_FakeHost(SimpleNamespace(), search_results={"event": []}),
    )
    result = session.run_turn("找不到的事件")
    assert "未绑定订单" in result["text"]
    assert "请用 `/order" in result["text"]


def test_reset_clears_candidates():
    session, _ = _session(
        [
            _response(tool_calls=[_call("e", "search_knowledge", {"query": "待检 事件", "type": "event"})]),
            _response(content="结束"),
        ],
        host=_FakeHost(SimpleNamespace(), search_results={"event": [_event_row()]}),
    )
    session.run_turn("找待检事件")
    assert session.state.last_candidates
    session.handle_command("/reset")
    assert session.state.last_candidates == []


# --------------------------------------------------------------------- R3 失败
def test_service_failure_marks_incomplete_and_reports_technical_failure():
    host = _FakeHost(SimpleNamespace(), fail_tools={"search_knowledge"})
    session, _ = _session(
        [
            _response(tool_calls=[_call("s", "search_knowledge", {"query": "包装复核", "type": "sop"})]),
            _response(content="结束"),
        ],
        host=host,
        order_id="SO-1003",
        line_id="001",
    )
    result = session.run_turn("包装应该怎么处理？")
    assert result["incomplete"] is True
    assert "技术失败" in result["text"]
    assert "规则检索失败" in result["text"]


def test_failed_then_successful_retry_recovers():
    class _FlakyHost(_FakeHost):
        def __init__(self):
            super().__init__(
                SimpleNamespace(),
                search_results={"sop": [_sop_row()]},
                documents={"SOP-FAKE-v1": _sop_document()},
            )
            self.attempts = 0

        def call(self, name, arguments):
            if name == "search_knowledge":
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("第一次失败")
            return super().call(name, arguments)

    host = _FlakyHost()
    session, _ = _session(
        [
            _response(
                tool_calls=[
                    _call("s1", "search_knowledge", {"query": "包装复核", "type": "sop"}),
                    _call("s2", "search_knowledge", {"query": "包装复核", "type": "sop"}),
                    _call("d", "read_document", {"doc_id": "SOP-FAKE-v1"}),
                ]
            ),
            _response(content="结束"),
        ],
        host=host,
        order_id="SO-1003",
        line_id="001",
    )
    result = session.run_turn("包装应该怎么处理？")
    assert result["incomplete"] is False
    assert "技术失败" not in result["text"]


# --------------------------------------------------------------------- R4 回读
def test_search_only_rules_do_not_create_suggestions():
    host = _FakeHost(SimpleNamespace(), search_results={"sop": [_sop_row()]})
    session, _ = _session(
        [
            _response(tool_calls=[_call("s", "search_knowledge", {"query": "包装复核", "type": "sop"})]),
            _response(content="结束"),
        ],
        host=host,
        order_id="SO-1003",
        line_id="001",
    )
    result = session.run_turn("包装应该怎么处理？")
    assert result["pack"]["counts"]["rules"] == 0
    assert result["explanation"]["suggestions"] == []
    assert "未回读" in result["text"]


def test_readback_rule_creates_suggestion_and_persists_in_scope():
    host = _FakeHost(
        SimpleNamespace(),
        search_results={"sop": [_sop_row()]},
        documents={"SOP-FAKE-v1": _sop_document()},
    )
    session, _ = _session(
        [
            _response(
                tool_calls=[
                    _call("s", "search_knowledge", {"query": "包装复核", "type": "sop"}),
                    _call("d", "read_document", {"doc_id": "SOP-FAKE-v1"}),
                ]
            ),
            _response(content="结束"),
        ],
        host=host,
        order_id="SO-1003",
        line_id="001",
    )
    first = session.run_turn("包装应该怎么处理？")
    assert first["pack"]["counts"]["rules"] == 1
    assert first["explanation"]["suggestions"]
    assert first["explanation"]["suggestions"][0]["rule_ids"]

    # 同范围下一轮无工具调用：复用已回读条款
    second = session.run_turn("再确认一下依据")
    assert second["pack"]["counts"]["rules"] == 1
    assert second["explanation"]["suggestions"]

    session.handle_command("/reset")
    assert session.state.readback_rules == {}


# --------------------------------------------------------------------- R5 计数
def test_tool_call_client_sends_once_without_hidden_retry():
    counter = {"n": 0}

    def handler(request):
        counter["n"] += 1
        return httpx.Response(429, json={"error": "rate limited"})

    client = explain.ToolCallClient(
        api_key="test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(Exception):
        client.create(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert counter["n"] == 1  # max_retries=0：无隐式重试


def test_generation_client_chat_chain_sends_once():
    counter = {"n": 0}

    def handler(request):
        counter["n"] += 1
        return httpx.Response(429, json={"error": "rate limited"})

    client = explain.DeepSeekGenerationClient(
        api_key="test-key",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(Exception):
        client.generate("hi")
    assert counter["n"] == 1


def test_planning_requests_counted_before_send_and_usage_aggregated():
    class _UsageClient(_ScriptedClient):
        def create(self, messages, tools=None):
            self.calls += 1
            self.last_usage = {"total_tokens": 10 if self.calls == 1 else 20, "model": "fake"}
            return super().create(messages, tools)

    client = _UsageClient(
        [
            _response(tool_calls=[_call("e", "search_knowledge", {"query": "待检 事件", "type": "event"})]),
            _response(content="结束"),
        ]
    )
    session = ChatSession(
        DATASET_ID, "t004-full-v2", model_client=client, generation_client=_MockGeneration()
    )
    result = session.run_turn("查一下事件")
    assert result["planning_requests"] == 2
    assert result["model_usage"]["calls"] == 2
    assert result["model_usage"]["unknown_calls"] == 0
    assert result["model_usage"]["total_tokens"] == 30  # 逐次合计，不取最后一次
    assert [r["usage"]["total_tokens"] for r in result["model_usage"]["per_call"]] == [10, 20]


def test_failed_planning_call_counted_with_unknown_usage():
    class _FailOnce(_ScriptedClient):
        def create(self, messages, tools=None):
            self.calls += 1
            self.last_usage = None
            raise RuntimeError("超时")

    session = ChatSession(
        DATASET_ID,
        "t004-full-v2",
        model_client=_FailOnce([]),
        generation_client=_MockGeneration(),
    )
    result = session.run_turn("会超时的轮次")
    assert result["planning_requests"] == 1  # 发送前计数，失败也计
    assert result["incomplete"] is True
    assert result["model_usage"]["unknown_calls"] == 1
    assert result["model_usage"]["total_tokens"] is None  # 未知不填零
    assert result["model_usage"]["per_call"][0]["status"] == "error"


def test_tool_budget_and_duplicates_still_count():
    calls = [_call(f"c{i}", "search_knowledge", {"query": f"待检事件 {i}", "type": "event"}) for i in range(7)]
    session, _ = _session([_response(tool_calls=calls), _response(content="结束")])
    result = session.run_turn("查资料")
    assert result["tool_requests"] == MAX_TOOL_REQUESTS
    assert result["incomplete"] is True

    same = {"query": "待检事件", "type": "event"}
    session2, _ = _session(
        [
            _response(
                tool_calls=[
                    _call("a", "search_knowledge", same),
                    _call("b", "search_knowledge", same),
                ]
            ),
            _response(content="结束"),
        ]
    )
    dup = session2.run_turn("查两次一样")
    assert dup["tool_requests"] == 2
    records = [r for r in dup["trace"] if r.get("tool") == "search_knowledge"]
    assert records[1]["cache_hit"] is True


# --------------------------------------------------------------------- R6 意图
def test_natural_language_switch_prompts_order_command():
    session, _ = _session([_response(content="结束")], order_id="SO-1003", line_id="001")
    result = session.run_turn("改查 SO-1007")
    assert "/order SO-1007" in result["text"]
    assert result["scope_key"][2] == "SO-1003"
    assert result["final_generations"] == 0


def test_natural_language_time_switch_prompts_asof_command():
    session, _ = _session([_response(content="结束")], order_id="SO-1003", line_id="001")
    result = session.run_turn("换一天再查")
    assert "/asof" in result["text"]
    assert result["scope_key"][4] is None


def test_pronoun_after_scope_clear_asks_clarification():
    session, _ = _session([_response(content="结束")], order_id="SO-1003", line_id="001")
    session.handle_command("/asof 2026-07-15 12:00")
    result = session.run_turn("现在呢？")
    assert "指代不明确" in result["text"]
    assert result["final_generations"] == 0


def test_same_scope_followup_carries_prior_question():
    host = _FakeHost(
        SimpleNamespace(),
        search_results={"sop": [_sop_row()]},
        documents={"SOP-FAKE-v1": _sop_document()},
    )
    session, _ = _session(
        [
            _response(
                tool_calls=[
                    _call("s", "search_knowledge", {"query": "包装复核", "type": "sop"}),
                    _call("d", "read_document", {"doc_id": "SOP-FAKE-v1"}),
                ]
            ),
            _response(content="结束"),
            _response(content="结束"),
        ],
        host=host,
        order_id="SO-1003",
        line_id="001",
    )
    session.run_turn("包装受潮按哪版流程？")
    session.run_turn("那要留什么记录？")
    last_prompt = session._mock_generation.prompts[-1]
    assert "结合上一轮问题：包装受潮按哪版流程？" in last_prompt


# --------------------------------------------------------------------- 回归（原有用例）
def test_scope_binding_preload_and_invalidation():
    session, _ = _session([_response(content="结束")], order_id="SO-1003", line_id="001")
    first = session.run_turn("核对一下")
    assert first["tool_requests"] == 2
    reply = session.handle_command("/asof 2026-09-08 12:00")
    assert "实际截止时间" in reply
    assert session.state.preload_pending == ["inspect_order", "read_order_evidence"]
    second = session.run_turn("再核对")
    assert second["tool_requests"] == 2
    assert second["scope_transition"]["action"] == "bind_order"


def test_asof_input_keeps_timezone():
    session, _ = _session([_response(content="结束")], order_id="SO-1003", line_id="001")
    session.handle_command("/asof 2026-08-15 12:00")
    assert session.state.as_of == "2026-08-15T12:00:00+08:00"


def test_final_answer_uses_current_scope_pack_on_later_turn():
    session, _ = _session([_response(content="结束")], order_id="SO-1003", line_id="001")
    first = session.run_turn("先核对")
    assert first["incomplete"] is False
    second = session.run_turn("再说一下状态")
    assert second["final_generations"] == 1
    assert second["explanation"]["explanation_status"] == "ok"


def test_unbound_turn_restricts_case_search():
    session, _ = _session(
        [
            _response(tool_calls=[_call("c", "search_knowledge", {"query": "换货", "type": "case"})]),
            _response(content="结束"),
        ]
    )
    result = session.run_turn("有换货案例吗")
    errors = [r for r in result["trace"] if r.get("error")]
    assert errors and errors[0]["error"]["category"] == "unbound_restricted"
    assert result["tool_requests"] == 1


def test_planning_budget_exhausted_marks_incomplete():
    responses = [
        _response(tool_calls=[_call(f"p{i}", "search_knowledge", {"query": f"待检事件 {i}", "type": "event"})])
        for i in range(MAX_PLANNING_REQUESTS)
    ]
    session, client = _session(responses)
    result = session.run_turn("一直请求工具")
    assert result["planning_requests"] == MAX_PLANNING_REQUESTS
    assert client.calls == MAX_PLANNING_REQUESTS
    assert result["incomplete"] is True


def test_model_call_failure_marks_incomplete_and_next_turn_recovers():
    class _Flaky(_ScriptedClient):
        def create(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("模拟模型超时")
            return super().create(messages, tools)

    client = _Flaky([_response(content="结束")])
    session = ChatSession(
        DATASET_ID, "t004-full-v2", model_client=client, generation_client=_MockGeneration()
    )
    first = session.run_turn("会超时的轮次")
    assert first["incomplete"] is True
    assert first["trace"][0].get("model_error")
    second = session.run_turn("恢复后的轮次")
    assert second["incomplete"] is False
    assert "未绑定订单" in second["text"]


def test_bind_invalid_order_keeps_unbound():
    session, _ = _session([_response(content="结束")])
    reply = session.bind_order("SO-NOT-EXIST")
    assert "绑定失败" in reply
    assert session.state.order_id is None


# --------------------------------------------------------------------- T-008d
def _event_document(doc_id="EVX-TEST-2", order_id="SO-1007", line_id="002"):
    return {
        "doc_id": doc_id,
        "doc_type": "event",
        "title": "第二批事件",
        "recorded_at": "2026-09-07T11:00:00+08:00",
        "sections": [{"section_id": "s1", "heading": "记录", "text": "回读内容。"}],
        "raw": {"scope": {"order_id": order_id, "line_id": line_id}},
    }


class _DocFailHost(_FakeHost):
    """按文档 ID 注入一次 internal_error 的宿主（验证恢复粒度不是工具名）。"""

    def __init__(self, failing_doc):
        super().__init__(
            SimpleNamespace(),
            search_results={"sop": [_sop_row("SOP-FAKE-A-v1"), _sop_row("SOP-FAKE-B-v1")]},
            documents={
                "SOP-FAKE-A-v1": _sop_document("SOP-FAKE-A-v1"),
                "SOP-FAKE-B-v1": _sop_document("SOP-FAKE-B-v1"),
            },
        )
        self.failing_doc = failing_doc
        self.failed_once = False

    def call(self, name, arguments):
        from pi_market.agent_tools import ToolError

        args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
        if name == "read_document" and args.get("doc_id") == self.failing_doc and not self.failed_once:
            self.failed_once = True
            raise ToolError("回读失败：数据库超时", "internal_error")
        return super().call(name, arguments)


def test_readback_does_not_renumber_seen_candidates():
    host = _FakeHost(
        SimpleNamespace(),
        search_results={
            "event": [_event_row(), _event_row("EVX-TEST-2", "SO-1007", "002", "第二批事件")]
        },
        documents={"EVX-TEST-2": _event_document()},
    )
    session, _ = _session(
        [
            _response(tool_calls=[_call("e", "search_knowledge", {"query": "待检 事件", "type": "event"})]),
            _response(content="结束"),
            _response(tool_calls=[_call("d", "read_document", {"doc_id": "EVX-TEST-2"})]),
            _response(content="结束"),
        ],
        host=host,
    )
    first = session.run_turn("找待检事件")
    assert "2. " in first["text"]

    second = session.run_turn("回读第二条候选")
    assert "第 2 条候选" in second["text"]
    candidates = session.state.last_candidates
    assert [c["seq"] for c in candidates] == [1, 2]
    assert candidates[1]["doc_id"] == "EVX-TEST-2"

    third = session.run_turn("第二条候选是哪个订单的？")
    assert "第 2 条候选" in third["text"]
    assert "SO-1007" in third["text"]


def test_new_event_search_resets_candidate_numbering():
    host = _FakeHost(
        SimpleNamespace(),
        search_results={
            "event": [_event_row(), _event_row("EVX-TEST-2", "SO-1007", "002", "第二批事件")]
        },
    )
    session, _ = _session(
        [
            _response(tool_calls=[_call("e1", "search_knowledge", {"query": "待检 事件", "type": "event"})]),
            _response(content="结束"),
            _response(tool_calls=[_call("e2", "search_knowledge", {"query": "换个说法找", "type": "event"})]),
            _response(content="结束"),
        ],
        host=host,
    )
    session.run_turn("找待检事件")
    assert len(session.state.last_candidates) == 2
    host._search_results = {"event": [_event_row()]}  # 新检索只返回一条
    second = session.run_turn("重新检索")
    assert len(session.state.last_candidates) == 1
    assert [c["seq"] for c in session.state.last_candidates] == [1]
    assert "2." not in second["text"]


def test_read_document_infrastructure_failure_is_internal_error():
    from unittest import mock

    from pi_market.agent_tools import Scope, ToolError, ToolHost

    host = ToolHost(Scope(DATASET_ID, "t004-full-v2"))
    host.seen_docs["SOP-FAKE-v1"] = {"scope": (None, None, None, None)}
    with mock.patch(
        "pi_market.corpus_retrieval.read_corpus_doc", side_effect=RuntimeError("数据库超时")
    ):
        with pytest.raises(ToolError) as exc:
            host.read_document({"doc_id": "SOP-FAKE-v1"})
    assert exc.value.to_dict()["error"]["category"] == "internal_error"

    with mock.patch(
        "pi_market.corpus_retrieval.read_corpus_doc", side_effect=corpus.CorpusError("文档不可见")
    ):
        with pytest.raises(ToolError) as exc:
            host.read_document({"doc_id": "SOP-FAKE-v1"})
    assert exc.value.to_dict()["error"]["category"] == "out_of_scope"


def test_document_failure_not_cleared_by_other_document_success():
    host = _DocFailHost("SOP-FAKE-A-v1")
    session, _ = _session(
        [
            _response(
                tool_calls=[
                    _call("a", "read_document", {"doc_id": "SOP-FAKE-A-v1"}),
                    _call("b", "read_document", {"doc_id": "SOP-FAKE-B-v1"}),
                ]
            ),
            _response(content="结束"),
        ],
        host=host,
        order_id="SO-1003",
        line_id="001",
    )
    result = session.run_turn("回读两份制度")
    assert result["incomplete"] is True
    failures = result["pack"]["unresolved_failures"]
    assert failures and failures[0]["tool"] == "read_document"
    assert "技术失败" in result["text"]


def test_same_document_retry_recovers():
    host = _DocFailHost("SOP-FAKE-A-v1")
    session, _ = _session(
        [
            _response(tool_calls=[_call("a1", "read_document", {"doc_id": "SOP-FAKE-A-v1"})]),
            _response(tool_calls=[_call("a2", "read_document", {"doc_id": "SOP-FAKE-A-v1"})]),
            _response(content="结束"),
        ],
        host=host,
        order_id="SO-1003",
        line_id="001",
    )
    result = session.run_turn("回读 A 制度")
    assert result["pack"]["unresolved_failures"] == []
    assert result["incomplete"] is False


def test_unbound_failure_is_reported_to_user():
    host = _FakeHost(SimpleNamespace(), fail_tools={"search_knowledge"})
    session, _ = _session(
        [
            _response(tool_calls=[_call("s", "search_knowledge", {"query": "待检 事件", "type": "event"})]),
            _response(content="结束"),
        ],
        host=host,
    )
    result = session.run_turn("查一下事件")
    assert result["incomplete"] is True
    assert "技术失败" in result["text"]
    assert "未绑定订单" in result["text"]


def _openai_body(usage_total=None):
    body = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "fake",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
    }
    if usage_total is not None:
        body["usage"] = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": usage_total}
    return body


def test_tool_call_client_usage_not_inherited_after_success():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=_openai_body(usage_total=10))
        return httpx.Response(429, json={"error": "rate limited"})

    client = explain.ToolCallClient(
        api_key="test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    client.create(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert client.last_usage and client.last_usage["total_tokens"] == 10
    with pytest.raises(Exception):
        client.create(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert client.last_usage is None  # 失败不继承上次成功值


def test_tool_call_client_usage_unknown_when_service_omits_it():
    def handler(request):
        return httpx.Response(200, json=_openai_body())

    client = explain.ToolCallClient(
        api_key="test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    client.create(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert client.last_usage is None


def test_generation_client_usage_not_inherited_after_success():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=_openai_body(usage_total=10))
        return httpx.Response(429, json={"error": "rate limited"})

    client = explain.DeepSeekGenerationClient(
        api_key="test-key", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    client.generate("hi")
    assert client.last_usage and client.last_usage["total_tokens"] == 10
    with pytest.raises(Exception):
        client.generate("hi")
    assert client.last_usage is None


def test_date_question_is_not_treated_as_switch():
    session, _ = _session([_response(content="结束")], order_id="SO-1003", line_id="001")
    result = session.run_turn("TX-1005 的过账日期是什么？")
    assert "/asof" not in result["text"]
    assert result["final_generations"] == 1


def test_date_in_plain_question_is_not_switch():
    session, _ = _session([_response(content="结束")], order_id="SO-1003", line_id="001")
    result = session.run_turn("2026-09-05 那条流水为什么没有计入？")
    assert "/asof" not in result["text"]
    assert result["final_generations"] == 1


def test_date_with_switch_verb_still_prompts_asof():
    session, _ = _session([_response(content="结束")], order_id="SO-1003", line_id="001")
    result = session.run_turn("改查 2026-09-05 的流水")
    assert "/asof" in result["text"]
    assert result["final_generations"] == 0


def test_rule_scope_evidence_constraint_in_generation_prompt():
    host = _FakeHost(
        SimpleNamespace(),
        search_results={"sop": [_sop_row()]},
        documents={"SOP-FAKE-v1": _sop_document()},
    )
    session, _ = _session(
        [
            _response(
                tool_calls=[
                    _call("s", "search_knowledge", {"query": "包装复核", "type": "sop"}),
                    _call("d", "read_document", {"doc_id": "SOP-FAKE-v1"}),
                ]
            ),
            _response(content="结束"),
        ],
        host=host,
        order_id="SO-1003",
        line_id="001",
    )
    session.run_turn("包装应该怎么处理？")
    prompt = session._mock_generation.prompts[-1]
    assert "证明当前事项属于该条款的适用范围" in prompt
