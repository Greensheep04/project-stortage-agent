import json

from pi_market.explain import GenerationClient, explain


class _JsonClient(GenerationClient):
    """Deterministic mock client returning a fixed JSON payload."""

    def __init__(self, payload: dict):
        self.payload = payload

    def generate(self, prompt: str) -> str:
        return json.dumps(self.payload, ensure_ascii=False)


def _facts(evidence_ids: list = None) -> dict:
    return {
        "dataset_id": "ds-test",
        "order_id": "SO-TEST",
        "line_id": "001",
        "as_of": "2026-09-09T20:00:00+08:00",
        "lines": [
            {
                "order_id": "SO-TEST",
                "line_id": "001",
                "planned_quantity": 10,
                "matched_net_shipped_quantity": 10,
                "difference_quantity": 0,
                "status_tags": ["matched"],
                "evidence_ids": evidence_ids or ["EV-1", "EV-2"],
            }
        ],
    }


def _notes(*contents: str) -> list:
    return [
        {
            "evidence_id": f"EV-{i + 1}",
            "evidence_type": "note",
            "order_id": "SO-TEST",
            "line_id": "001",
            "content": content,
            "source": {"filename": "t.xlsx", "sheet": "业务说明", "row": i + 2},
        }
        for i, content in enumerate(contents)
    ]


def test_compatible_notes_are_not_conflict():
    # 高层反例：计划 10 件与“首批已发 6 件，剩余 4 件待发”相容，不得判冲突。
    evidence = _notes("订单计划10件。", "首批已发6件，剩余4件待发。")
    client = _JsonClient(
        {
            "explanation": "计划10件，首批已发6件，剩余4件待发，属于分批执行。",
            "used_evidence_ids": ["EV-1", "EV-2"],
            "open_questions": [],
        }
    )
    result = explain(_facts(), evidence, client=client)
    assert result["explanation_status"] == "ok"
    assert "conflicting_evidence_ids" not in result


def test_same_time_contradiction_is_conflict():
    evidence = _notes("质检未通过，禁止放行。", "质检已通过，允许放行。")
    client = _JsonClient(
        {
            "explanation": "两条说明对同一明细的结论相反。",
            "used_evidence_ids": ["EV-1"],
            "open_questions": [],
            "conflict": {
                "evidence_ids": ["EV-1", "EV-2"],
                "reason": "同一时间、同一对象的结论相反。",
            },
        }
    )
    result = explain(_facts(), evidence, client=client)
    assert result["explanation_status"] == "conflicting_evidence"
    assert set(result["conflicting_evidence_ids"]) == {"EV-1", "EV-2"}
    cited = {c["evidence_id"] for c in result["citations"]}
    assert cited == {"EV-1", "EV-2"}
    for citation in result["citations"]:
        assert citation["source"]["filename"] == "t.xlsx"
        assert isinstance(citation["source"]["row"], int)


def test_state_change_over_time_is_not_conflict():
    evidence = _notes("9月1日：已发货6件。", "9月2日：客户退回，冲销6件。")
    client = _JsonClient(
        {
            "explanation": "先发货后冲销，属于时间上的明确状态变化。",
            "used_evidence_ids": ["EV-1", "EV-2"],
            "open_questions": [],
        }
    )
    result = explain(_facts(), evidence, client=client)
    assert result["explanation_status"] == "ok"


def test_unknown_time_or_object_stays_open_question():
    evidence = _notes("有一批货状态待确认。", "另一记录提到数量不一致。")
    client = _JsonClient(
        {
            "explanation": "现有资料不足以确定原因。",
            "used_evidence_ids": [],
            "open_questions": ["两条说明的对象与时间不明确，需要核实。"],
        }
    )
    result = explain(_facts(), evidence, client=client)
    assert result["explanation_status"] == "ok"
    assert result["open_questions"]


def test_conflict_with_fake_ids_only_is_not_conflict():
    evidence = _notes("说明一。", "说明二。")
    client = _JsonClient(
        {
            "explanation": "疑似冲突。",
            "used_evidence_ids": [],
            "open_questions": [],
            "conflict": {"evidence_ids": ["FAKE-1"], "reason": "引用不存在的证据。"},
        }
    )
    result = explain(_facts(), evidence, client=client)
    assert result["explanation_status"] == "ok"
    assert "FAKE-1" in result["dropped_citations"]


def test_conflict_mixed_ids_keeps_valid_drops_fake():
    evidence = _notes("说明一。", "说明二。")
    client = _JsonClient(
        {
            "explanation": "引用部分有效证据。",
            "used_evidence_ids": [],
            "open_questions": [],
            "conflict": {
                "evidence_ids": ["EV-1", "FAKE-1"],
                "reason": "同一时间结论相反。",
            },
        }
    )
    result = explain(_facts(), evidence, client=client)
    assert result["explanation_status"] == "conflicting_evidence"
    assert result["conflicting_evidence_ids"] == ["EV-1"]
    assert "FAKE-1" in result["dropped_citations"]
    assert [c["evidence_id"] for c in result["citations"]] == ["EV-1"]


def test_mock_sku_mismatch_uses_facts_with_citations():
    from pi_market.investigate import investigate

    dataset_id = "ds-61edff98759f"
    client = _JsonClient({})
    for order_id, line_id in (("SO-1005", "001"), ("SO-1006", "002")):
        inv = investigate(dataset_id, order_id, line_id=line_id)
        assert "sku_mismatch" in inv["facts"]["lines"][0]["status_tags"]
        evidence = inv["evidence"]
        flow_ids = [e["evidence_id"] for e in evidence if e["evidence_type"] == "flow"]
        assert flow_ids
        client.payload = {
            "explanation": "计划 SKU 与流水 SKU 不一致，流水不计入匹配出库。",
            "used_evidence_ids": flow_ids,
            "open_questions": [],
        }
        result = explain(inv["facts"], evidence, client=client)
        assert result["explanation_status"] == "ok"
        assert "conflicting_evidence_ids" not in result
        assert result["used_evidence_ids"] == flow_ids
        assert [c["evidence_id"] for c in result["citations"]] == flow_ids
        for citation in result["citations"]:
            assert citation["source"]["sheet"] == "仓储流水"
            assert isinstance(citation["source"]["row"], int)
