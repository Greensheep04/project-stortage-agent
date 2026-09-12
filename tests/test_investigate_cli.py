import hashlib
import json
from pathlib import Path

import pytest

from pi_market.cli import main
from pi_market.db import drop_dataset
from pi_market.importer import import_excel

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_XLSX = PROJECT_ROOT / "deliverables" / "仓储异常测试数据.xlsx"
AS_OF = "2026-09-09 20:00:00"


def _dataset_id() -> str:
    h = hashlib.sha256(REAL_XLSX.read_bytes()).hexdigest()
    return f"ds-{h[:12]}"


@pytest.fixture(scope="module", autouse=True)
def ensure_imported():
    drop_dataset(_dataset_id())
    import_excel(REAL_XLSX, AS_OF)
    yield


def test_cli_investigate_outputs_three_sections(capsys):
    code = main(["investigate", _dataset_id(), "SO-1003", "--line-id", "001"])
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert "facts" in output
    assert "evidence" in output
    assert "explanation" in output
    assert output["facts"]["lines"][0]["status_tags"]
    # In the current environment the real generation model is unavailable,
    # so the CLI may legitimately return 2 with explanation_status=model_unavailable.
    assert code in (0, 2)


def test_cli_investigate_no_evidence_status(capsys):
    # SO-1014/002 is not_due and has no evidence.
    code = main(["investigate", _dataset_id(), "SO-1014", "--line-id", "002"])
    captured = capsys.readouterr()
    assert code == 2
    output = json.loads(captured.out)
    assert output["explanation"]["explanation_status"] == "no_evidence"
