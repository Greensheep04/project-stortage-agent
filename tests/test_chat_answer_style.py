"""T-010b：concise 提示词注入、D24 生成视图筛选、CLI 展示分层（确定性测试）。"""

import argparse
import io
import json
import sys
from types import SimpleNamespace

import pytest

from pi_market import corpus, explain
from pi_market.chat import ChatSession
from test_chat_bounds import (
    _MockGeneration,
    _ScriptedClient,
    _call,
    _response,
    _session,
    _sop_document,
    _sop_row,
    _FakeHost,
)
from test_corpus_common import DATASET_ID, REAL_CORPUS_DIR

AS_OF = "2026-09-09T20:00:00+08:00"


@pytest.fixture(scope="module", autouse=True)
def _corpus_ready():
    corpus.import_corpus("t004-full-v2", DATASET_ID, files_dir=REAL_CORPUS_DIR)
    corpus.index_corpus("t004-full-v2", "mock-v1", no_embedding=True)
    yield


def _prompt_case():
    facts = {
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
            }
        ],
    }
    context = {
        "corpus_id": "t004-full-v2",
        "question": "这批数量是多少？",
        "retrieval_description": "这批数量是多少？",
        "events": [],
        "history_refs": [],
        "applicable_rules": [],
        "truncation": {},
    }
    return facts, context


def test_full_prompt_unchanged_and_concise_prompt_injected():
    facts, context = _prompt_case()
    default_prompt = explain._build_corpus_prompt(facts, [], context, AS_OF)
    explicit_full = explain._build_corpus_prompt(facts, [], context, AS_OF, answer_style="full")
    concise = explain._build_corpus_prompt(facts, [], context, AS_OF, answer_style="concise")

    assert default_prompt == explicit_full  # 单次入口 full 模式逐字不变
    for marker in ("要求（简洁回答模式", "先直接回答用户所问", "材料里有的内容不等于必须输出"):
        assert marker not in default_prompt
        assert marker in concise
    # concise 仍保留引用校验红线、适用范围红线与 JSON 输出格式
    assert "证明当前事项属于该条款的适用范围" in concise
    assert "不得编造制度依据" in concise
    assert '"explanation"' in concise and '"suggestions"' in concise


def _bound_session(search_results, documents, responses):
    host = _FakeHost(SimpleNamespace(), search_results=search_results, documents=documents)
    session, _ = _session(responses, host=host, order_id="SO-1003", line_id="001")
    return session


def test_generation_prompt_is_concise_with_readback_rule():
    session = _bound_session(
        {"sop": [_sop_row()]},
        {"SOP-FAKE-v1": _sop_document()},
        [
            _response(
                tool_calls=[
                    _call("s", "search_knowledge", {"query": "包装复核", "type": "sop"}),
                    _call("d", "read_document", {"doc_id": "SOP-FAKE-v1"}),
                ]
            ),
            _response(content="结束"),
        ],
    )
    result = session.run_turn("包装应该怎么处理？")
    prompt = session._mock_generation.prompts[-1]
    assert "要求（简洁回答模式" in prompt
    assert "先直接回答用户所问" in prompt
    assert "SOP-FAKE-v1" in prompt  # 已回读规则仍进入生成视图
    assert result["context_selection"]["rules"] is True


def test_context_selection_six_mappings():
    session = ChatSession(DATASET_ID, "t004-full-v2", model_client=_ScriptedClient([]))
    cases = {
        "这批的计划和已过账数量是多少？": {"facts": True, "evidence": False, "events": False, "rules": False},
        "为什么会有差异？": {"facts": True, "evidence": True, "events": True, "rules": False},
        "包装受潮按哪版规范处理？": {"rules": True, "facts": False, "evidence": False},  # R1：普通版本问不默认本单事实
        "v2 规范的有效期是什么？": {"rules": True, "facts": False, "evidence": False},
        "这笔用哪版规范？": {"rules": True, "facts": True},
        "怎么处理包装受潮？": {"rules": True, "facts": False},
    }
    for text, expected in cases.items():
        selection = session.relevant_context(text)
        assert selection["full"] is False, text
        for key, value in expected.items():
            assert selection[key] is value, (text, key)
    for text in ("详细说说全部依据", "请综合说明"):
        selection = session.relevant_context(text)
        assert selection["full"] is True, text


def test_quantity_view_excludes_rules_events_and_keeps_session_cache():
    session = _bound_session(
        {"sop": [_sop_row()]},
        {"SOP-FAKE-v1": _sop_document()},
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
    )
    session.run_turn("包装应该怎么处理？")
    second = session.run_turn("这批的计划和已过账数量是多少？")

    assert second["final_generations"] == 1  # 仅事实视图也允许生成（不再误判无材料）
    assert second["context_selection"]["rules"] is False
    assert second["pack"]["counts"]["rules"] == 1  # 会话缓存完整保留，裁剪的只是本轮视图

    prompt = session._mock_generation.prompts[-1]
    rules_section = prompt.split("四、有效规则")[1].split("用户问题")[0]
    assert "[]" in rules_section and "SOP-FAKE-v1" not in rules_section
    events_section = prompt.split("本单新事件")[1].split("三、历史参考案例")[0]
    assert events_section.strip().endswith("[]")  # 事件列表为空，未附无关事件
    assert "未向生成提供规则材料" in prompt  # not_requested 与“没有规则”区分
    assert "本次没有可引用的有效规则" not in prompt
    assert "SO-1003" in prompt  # facts 保留


def test_independent_question_does_not_carry_prior():
    session = _bound_session(
        {"sop": []},
        {},
        [_response(content="结束"), _response(content="结束")],
    )
    session.run_turn("包装受潮按哪版流程？")
    session.run_turn("这批的计划和已过账数量对得上吗？")
    prompt = session._mock_generation.prompts[-1]
    assert "这批的计划和已过账数量对得上吗？" in prompt
    assert "结合上一轮问题" not in prompt


def test_r1_reference_probes_use_actual_prompt():
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
            _response(content="结束"),
            _response(content="结束"),
        ],
        host=host,
        order_id="SO-1003",
        line_id="001",
    )
    session.run_turn("包装受潮按哪版规范处理？")

    # 反例一：普通版本问 → rules（＋版本），不带本单账目
    first = session.run_turn("包装受潮按哪版规范处理？")
    prompt1 = session._mock_generation.prompts[-1]
    facts1 = prompt1.split("一、程序事实")[1].split("二、本单证据")[0]
    assert "SO-1003" not in facts1
    assert first["context_selection"]["full"] is False

    # 反例二：指代本单 → 并入上一轮，rules＋facts/evidence
    second = session.run_turn("那这笔订单应该怎么处理？")
    prompt2 = session._mock_generation.prompts[-1]
    facts2 = prompt2.split("一、程序事实")[1].split("二、本单证据")[0]
    assert "结合上一轮问题" in prompt2
    assert "SO-1003" in facts2
    assert second["context_selection"]["referenced"] is True

    # 反例三：指代＋“具体” → 不跳全量、不丢指代，规则＋本单事实
    third = session.run_turn("按刚才那版具体该怎么处理？")
    prompt3 = session._mock_generation.prompts[-1]
    assert "结合上一轮问题" in prompt3
    assert third["context_selection"]["full"] is False
    assert third["context_selection"]["referenced"] is True
    assert "SO-1003" in prompt3.split("一、程序事实")[1].split("二、本单证据")[0]


def test_r2_failure_hint_appears_exactly_once():
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
    assert result["text"].count("未完成") == 1
    assert result["text"].count("技术失败") == 1
    assert "规则检索失败" in result["text"]


def test_clarification_supplement_keeps_order_evidence():
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
    session.run_turn("现在应该怎么处理？")  # 笼统轮
    second = session.run_turn("目前卡在质量复核环节。")
    prompt = session._mock_generation.prompts[-1]
    assert "结合上一轮问题" in prompt
    evidence_section = prompt.split("二、本单证据")[1].split("三、历史参考案例")[0]
    assert "TX-1005" in evidence_section  # 补充环节后保留本单证据
    assert "EVX-FAKE-1" in evidence_section
    assert second["context_selection"]["evidence"] is True
    assert second["context_selection"]["events"] is True


class _FixedExplanation:
    def __init__(self, explanation):
        self.explanation = explanation
        self.last_usage = {"total_tokens": 1}
        self.prompts = []

    def generate(self, prompt):
        self.prompts.append(prompt)
        return json.dumps(
            {
                "explanation": self.explanation,
                "used_evidence_ids": [],
                "used_event_ids": [],
                "used_history_ids": [],
                "used_version_ids": [],
                "suggestions": [],
                "open_questions": [],
            },
            ensure_ascii=False,
        )


def test_user_text_preserves_multiline_display():
    explanation = (
        "2026-07-01 至 2026-07-31：v1。\n"
        "2026-08-01 起：v2。\n\n"
        "SO-1003/001：计划 10 件，已过账 10 件，差额 0 件。\n"
        "SO-1003/002：计划 5 件，已过账 5 件，差额 0 件。"
    )
    generation = _FixedExplanation(explanation)
    session = ChatSession(
        DATASET_ID,
        "t004-full-v2",
        order_id="SO-1003",
        line_id="001",
        as_of=AS_OF,
        model_client=_ScriptedClient([_response(content="结束")]),
        generation_client=generation,
    )
    try:
        result = session.run_turn("版本和各明细数量是什么？")
        lines = result["text"].splitlines()
        assert "2026-07-01 至 2026-07-31：v1。" in lines
        assert "2026-08-01 起：v2。" in lines
        assert sum(1 for line in lines if line.startswith("SO-1003/")) == 2  # 多明细各自成行
        assert "每个版本各占一行" in generation.prompts[-1]  # 排版要求注入实际生成
    finally:
        session.close()


def test_cli_chat_hides_trace_unless_debug(monkeypatch, capsys):
    from pi_market import cli

    canned = {
        "text": "回答文本",
        "trace": [{"tool": "inspect_order", "backend": "function", "elapsed_ms": 1.0}],
        "incomplete": True,
    }
    monkeypatch.setattr(ChatSession, "run_turn", lambda self, text: dict(canned))

    def run(debug):
        args = argparse.Namespace(
            dataset_id=DATASET_ID,
            corpus_id="t004-full-v2",
            order=None,
            line_id=None,
            as_of=None,
            transport="function",
            debug=debug,
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO("你好\n/exit\n"))
        code = cli.cmd_chat(args)
        return code, capsys.readouterr().out

    code, out = run(False)
    assert code == 0
    assert "回答文本" in out
    assert "[tool]" not in out
    assert "本轮调查未完成（原因见上）" in out

    code_debug, out_debug = run(True)
    assert code_debug == 0
    assert "[tool]" in out_debug
    assert "inspect_order" in out_debug

    # R2：回答文本已含未完成提示时，CLI 不叠加第二条
    canned["text"] = "（规则检索失败（技术失败）；本轮调查未完成，未给出的结论不能视为完成。）"
    code_hint, out_hint = run(False)
    assert code_hint == 0
    assert out_hint.count("未完成") == 1
