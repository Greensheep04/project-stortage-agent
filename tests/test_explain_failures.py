import json

from pi_market.explain import GenerationClient, explain


class _FailingClient(GenerationClient):
    def generate(self, prompt: str) -> str:
        raise TimeoutError("模型服务超时")


class _ConflictClient(GenerationClient):
    """Deterministic mock client reporting a same-time contradiction."""

    def generate(self, prompt: str) -> str:
        return json.dumps(
            {
                "explanation": "同一明细的两条说明结论相反。",
                "used_evidence_ids": ["EV-A", "EV-B"],
                "open_questions": [],
                "conflict": {
                    "evidence_ids": ["EV-A", "EV-B"],
                    "reason": "同一时间、同一明细的两条说明结论相反。",
                },
            },
            ensure_ascii=False,
        )


def _make_facts(evidence_ids: list = None) -> dict:
    return {
        "dataset_id": "ds-test",
        "order_id": "SO-TEST",
        "line_id": "001",
        "as_of": "2026-09-09T20:00:00+08:00",
        "lines": [
            {
                "order_id": "SO-TEST",
                "line_id": "001",
                "warehouse": "A1",
                "sku": "SKU-A",
                "unit": "件",
                "planned_quantity": 10,
                "matched_net_shipped_quantity": 8,
                "difference_quantity": 2,
                "status_tags": ["quantity_difference"],
                "flow_ids": ["TX-1"],
                "evidence_ids": evidence_ids or ["TX-1", "EV-1"],
            }
        ],
    }


def test_model_unavailable_status():
    facts = _make_facts()
    evidence = [
        {
            "evidence_id": "TX-1",
            "evidence_type": "flow",
            "order_id": "SO-TEST",
            "line_id": "001",
            "content": "出库8件。",
            "source": {"filename": "t.xlsx", "sheet": "仓储流水", "row": 2},
        }
    ]
    result = explain(facts, evidence, client=_FailingClient())
    assert result["explanation_status"] == "model_unavailable"
    assert "模型服务不可用" in result["explanation"]
    assert result["used_evidence_ids"] == []
    assert "模型服务超时" in result["model_error"]


def test_no_evidence_status():
    facts = _make_facts(evidence_ids=[])
    result = explain(facts, [], client=_FailingClient())
    assert result["explanation_status"] == "no_evidence"
    assert "现有资料不足以确定原因" in result["explanation"]
    assert result["used_evidence_ids"] == []


def test_conflicting_evidence_status():
    facts = _make_facts(evidence_ids=["EV-A", "EV-B"])
    evidence = [
        {
            "evidence_id": "EV-A",
            "evidence_type": "note",
            "order_id": "SO-TEST",
            "line_id": "001",
            "content": "分两批发出6件和4件，合计10件。",
            "source": {"filename": "t.xlsx", "sheet": "业务说明", "row": 2},
        },
        {
            "evidence_id": "EV-B",
            "evidence_type": "note",
            "order_id": "SO-TEST",
            "line_id": "001",
            "content": "实际只发出5件，剩余5件待处理。",
            "source": {"filename": "t.xlsx", "sheet": "业务说明", "row": 3},
        },
    ]
    result = explain(facts, evidence, client=_ConflictClient())
    assert result["explanation_status"] == "conflicting_evidence"
    assert "相反" in result["explanation"]
    assert set(result["conflicting_evidence_ids"]) == {"EV-A", "EV-B"}
    assert {c["evidence_id"] for c in result["citations"]} == {"EV-A", "EV-B"}


def test_failure_messages_are_distinguishable():
    facts_empty = _make_facts(evidence_ids=[])
    r_no_ev = explain(facts_empty, [])

    facts_conflict = _make_facts(evidence_ids=["EV-A", "EV-B"])
    ev_conflict = [
        {
            "evidence_id": "EV-A",
            "evidence_type": "note",
            "order_id": "SO-TEST",
            "line_id": "001",
            "content": "合计10件。",
            "source": {"filename": "t.xlsx", "sheet": "业务说明", "row": 2},
        },
        {
            "evidence_id": "EV-B",
            "evidence_type": "note",
            "order_id": "SO-TEST",
            "line_id": "001",
            "content": "只发出5件。",
            "source": {"filename": "t.xlsx", "sheet": "业务说明", "row": 3},
        },
    ]
    r_conflict = explain(facts_conflict, ev_conflict, client=_ConflictClient())

    facts_ok = _make_facts()
    ev_ok = [
        {
            "evidence_id": "TX-1",
            "evidence_type": "flow",
            "order_id": "SO-TEST",
            "line_id": "001",
            "content": "出库8件。",
            "source": {"filename": "t.xlsx", "sheet": "仓储流水", "row": 2},
        }
    ]
    r_model_down = explain(facts_ok, ev_ok, client=_FailingClient())

    statuses = {
        r_no_ev["explanation_status"],
        r_conflict["explanation_status"],
        r_model_down["explanation_status"],
    }
    assert statuses == {"no_evidence", "conflicting_evidence", "model_unavailable"}
