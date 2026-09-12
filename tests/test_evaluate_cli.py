import json
from pathlib import Path

import pytest

from pi_market import cli, evidence, retrieval
from pi_market.db import drop_dataset
from pi_market.importer import import_excel

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_XLSX = PROJECT_ROOT / "deliverables" / "仓储异常测试数据.xlsx"
ANSWERS = PROJECT_ROOT / "deliverables" / "评测答案.json"
QUERIES = PROJECT_ROOT / "tests" / "eval" / "eval_queries.jsonl"
DATASET_ID = "ds-61edff98759f"
AS_OF = "2026-09-09 20:00:00"
MOCK_MODEL = "mock-evaluate"


@pytest.fixture(scope="module", autouse=True)
def ensure_imported():
    drop_dataset(DATASET_ID)
    import_excel(REAL_XLSX, AS_OF)
    yield


@pytest.fixture(autouse=True)
def mock_embedder(monkeypatch):
    def _fake(model=None):
        return evidence.mock_embedder(), MOCK_MODEL

    monkeypatch.setattr(retrieval, "get_embedder", _fake)


@pytest.fixture(autouse=True)
def mock_index():
    evidence.clear_index(DATASET_ID, MOCK_MODEL)
    evidence.build_index(DATASET_ID, MOCK_MODEL, evidence.mock_embedder())
    yield
    evidence.clear_index(DATASET_ID, MOCK_MODEL)


def test_cli_evaluate_writes_unified_report(tmp_path):
    out = tmp_path / "report.json"
    rc = cli.main(
        [
            "evaluate",
            DATASET_ID,
            str(ANSWERS),
            str(QUERIES),
            "--embedding-model",
            MOCK_MODEL,
            "--output",
            str(out),
        ]
    )
    assert rc == 0

    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["numeric"]["cases"] == 150
    assert report["numeric"]["mismatches"] == []

    modes = {m["mode"]: m for m in report["retrieval"]["modes"]}
    assert set(modes) == {"phrase", "vector", "rrf"}
    for mode_report in modes.values():
        assert mode_report["status"] == "ok"
        assert len(mode_report["unsupported_queries"]) == 5
        for item in mode_report["unsupported_queries"]:
            assert item["returned_count"] == len(item["returned_parent_ids"])
