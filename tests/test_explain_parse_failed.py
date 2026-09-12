from pi_market.explain import GenerationClient, explain


class _UnparsableClient(GenerationClient):
    """Deterministic mock client returning non-JSON text."""

    def generate(self, prompt: str) -> str:
        return "这不是 JSON，只是一段普通文本。"


def _facts() -> dict:
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
                "matched_net_shipped_quantity": 8,
                "difference_quantity": 2,
                "status_tags": ["quantity_difference"],
                "evidence_ids": ["EV-1"],
            }
        ],
    }


def _evidence() -> list:
    return [
        {
            "evidence_id": "EV-1",
            "evidence_type": "note",
            "order_id": "SO-TEST",
            "line_id": "001",
            "content": "交接记录记载2件包装破损。",
            "source": {"filename": "t.xlsx", "sheet": "业务说明", "row": 2},
        }
    ]


def test_parse_failed_status():
    result = explain(_facts(), _evidence(), client=_UnparsableClient())
    assert result["explanation_status"] == "explanation_parse_failed"
    assert result["explanation"] == "这不是 JSON，只是一段普通文本。"
    assert result["parse_error"]
    assert result["citations"] == []
    assert result["used_evidence_ids"] == []
    assert result["dropped_citations"] == []
