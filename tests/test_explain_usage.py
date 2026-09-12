import json

from pi_market.explain import GenerationClient, explain


class _UsageClient(GenerationClient):
    """Deterministic mock client that reports token usage."""

    def __init__(self):
        self.last_usage = {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
            "model": "mock-model",
        }

    def generate(self, prompt: str) -> str:
        return json.dumps(
            {
                "explanation": "分批出库合计10件。",
                "used_evidence_ids": ["EV-1"],
                "open_questions": [],
            },
            ensure_ascii=False,
        )


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
                "matched_net_shipped_quantity": 10,
                "difference_quantity": 0,
                "status_tags": ["split_matched"],
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
            "content": "分两批出库，合计10件。",
            "source": {"filename": "t.xlsx", "sheet": "业务说明", "row": 2},
        }
    ]


def test_mock_usage_is_surfaced():
    result = explain(_facts(), _evidence(), client=_UsageClient())
    assert result["explanation_status"] == "ok"
    assert result["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "model": "mock-model",
    }
