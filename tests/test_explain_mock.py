import json

from pi_market.explain import GenerationClient, explain


class _MockClient(GenerationClient):
    def __init__(self, response: dict):
        self.response = response

    def generate(self, prompt: str) -> str:
        return json.dumps(self.response, ensure_ascii=False)


def _make_facts() -> dict:
    return {
        "dataset_id": "ds-test",
        "order_id": "SO-1003",
        "line_id": "001",
        "as_of": "2026-09-09T20:00:00+08:00",
        "lines": [
            {
                "order_id": "SO-1003",
                "line_id": "001",
                "warehouse": "A1",
                "sku": "SKU-A",
                "unit": "件",
                "planned_quantity": 10,
                "matched_net_shipped_quantity": 10,
                "difference_quantity": 0,
                "status_tags": ["split_matched"],
                "flow_ids": ["TX-1005", "TX-1006"],
                "evidence_ids": ["TX-1005", "TX-1006", "EV-1001"],
            }
        ],
    }


def _make_evidence() -> list:
    return [
        {
            "evidence_id": "TX-1005",
            "evidence_type": "flow",
            "order_id": "SO-1003",
            "line_id": "001",
            "content": "上午交接6件。",
            "source": {"filename": "test.xlsx", "sheet": "仓储流水", "row": 6},
        },
        {
            "evidence_id": "TX-1006",
            "evidence_type": "flow",
            "order_id": "SO-1003",
            "line_id": "001",
            "content": "下午交接4件。",
            "source": {"filename": "test.xlsx", "sheet": "仓储流水", "row": 7},
        },
        {
            "evidence_id": "EV-1001",
            "evidence_type": "note",
            "order_id": "SO-1003",
            "line_id": "001",
            "content": "分两批出库，合计10件。",
            "source": {"filename": "test.xlsx", "sheet": "业务说明", "row": 2},
        },
    ]


def test_mock_explanation_binds_citations():
    facts = _make_facts()
    evidence = _make_evidence()
    client = _MockClient(
        {
            "explanation": "分两批出库，合计10件，与计划一致。",
            "used_evidence_ids": ["EV-1001", "TX-1005", "TX-1006"],
            "open_questions": [],
        }
    )
    result = explain(facts, evidence, client=client)
    assert result["explanation_status"] == "ok"
    assert result["explanation"] == "分两批出库，合计10件，与计划一致。"
    assert set(result["used_evidence_ids"]) == {"EV-1001", "TX-1005", "TX-1006"}
    assert result["dropped_citations"] == []
    assert len(result["citations"]) == 3
    for c in result["citations"]:
        assert c["source"]["filename"] == "test.xlsx"
        assert c["source"]["sheet"] in ("仓储流水", "业务说明")


def test_mock_drops_fake_evidence_ids():
    facts = _make_facts()
    evidence = _make_evidence()
    client = _MockClient(
        {
            "explanation": "引用伪造证据。",
            "used_evidence_ids": ["EV-1001", "FAKE-1", "TX-9999"],
            "open_questions": [],
        }
    )
    result = explain(facts, evidence, client=client)
    assert result["explanation_status"] == "ok"
    assert result["used_evidence_ids"] == ["EV-1001"]
    assert set(result["dropped_citations"]) == {"FAKE-1", "TX-9999"}
    assert len(result["citations"]) == 1


def test_mock_wrong_numbers_do_not_override_host_facts():
    facts = _make_facts()
    evidence = _make_evidence()
    client = _MockClient(
        {
            "explanation": "实际发出99件，差异为负数。",
            "used_evidence_ids": ["EV-1001"],
            "open_questions": [],
        }
    )
    result = explain(facts, evidence, client=client)
    assert result["explanation_status"] == "ok"
    # Host facts must remain unchanged regardless of model text.
    line = facts["lines"][0]
    assert line["planned_quantity"] == 10
    assert line["matched_net_shipped_quantity"] == 10
    assert line["difference_quantity"] == 0
