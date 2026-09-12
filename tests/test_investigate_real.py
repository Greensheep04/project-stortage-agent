import hashlib
from pathlib import Path

import pytest

from pi_market.db import drop_dataset, get_reader_conn
from pi_market.importer import import_excel
from pi_market.investigate import investigate

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


def _line_facts(order_id: str, line_id: str):
    result = investigate(_dataset_id(), order_id, line_id=line_id)
    assert len(result["facts"]["lines"]) == 1
    return result["facts"]["lines"][0]


def _line_evidence(order_id: str, line_id: str):
    result = investigate(_dataset_id(), order_id, line_id=line_id)
    assert len(result["facts"]["lines"]) == 1
    return result


def test_split_batches_so_1003():
    result = _line_evidence("SO-1003", "001")
    line = result["facts"]["lines"][0]
    assert line["planned_quantity"] == 10
    assert line["matched_net_shipped_quantity"] == 10
    assert line["difference_quantity"] == 0
    assert "split_matched" in line["status_tags"]
    assert set(line["evidence_ids"]) == {"TX-1005", "TX-1006", "EV-1001"}

    evidence_ids = [ev["evidence_id"] for ev in result["evidence"]]
    assert set(evidence_ids) == set(line["evidence_ids"])
    for ev in result["evidence"]:
        assert ev["source"]["filename"] == "仓储异常测试数据.xlsx"
        assert ev["source"]["sheet"] in ("业务说明", "仓储流水")
        assert isinstance(ev["source"]["row"], int)


def test_reversal_so_1007_002():
    result = _line_evidence("SO-1007", "002")
    line = result["facts"]["lines"][0]
    assert line["planned_quantity"] == 5
    assert line["matched_net_shipped_quantity"] == 5
    assert line["difference_quantity"] == 0
    assert "reversal_matched" in line["status_tags"]
    assert "has_reversal" in line["status_tags"]
    assert set(line["evidence_ids"]) == {"TX-1018", "TX-1019", "EV-1005"}


def test_sku_mismatch_so_1005_001():
    result = _line_evidence("SO-1005", "001")
    line = result["facts"]["lines"][0]
    assert line["planned_quantity"] == 13
    assert line["matched_net_shipped_quantity"] == 0
    assert line["difference_quantity"] == 13
    assert "sku_mismatch" in line["status_tags"]
    assert set(line["evidence_ids"]) == {"TX-1012"}


def test_insufficient_evidence_so_1009_001():
    result = _line_evidence("SO-1009", "001")
    line = result["facts"]["lines"][0]
    assert line["planned_quantity"] == 13
    assert line["matched_net_shipped_quantity"] == 12
    assert line["difference_quantity"] == 1
    assert "insufficient_evidence" in line["status_tags"]
    assert set(line["evidence_ids"]) == {"TX-1022"}


def test_draft_only_so_1012_002():
    result = _line_evidence("SO-1012", "002")
    line = result["facts"]["lines"][0]
    assert line["planned_quantity"] == 7
    assert line["matched_net_shipped_quantity"] == 0
    assert line["difference_quantity"] == 7
    assert "draft_only" in line["status_tags"]
    assert set(line["evidence_ids"]) == {"TX-1030", "EV-1008"}


def test_not_due_so_1014_002():
    result = _line_evidence("SO-1014", "002")
    line = result["facts"]["lines"][0]
    assert line["planned_quantity"] == 5
    assert line["matched_net_shipped_quantity"] == 0
    assert line["difference_quantity"] == 5
    assert "not_due" in line["status_tags"]
    assert line["evidence_ids"] == []


def test_investigate_order_without_line():
    result = investigate(_dataset_id(), "SO-1003")
    assert len(result["facts"]["lines"]) == 2
    line_ids = [ln["line_id"] for ln in result["facts"]["lines"]]
    assert "001" in line_ids and "002" in line_ids
    assert len(result["evidence"]) > 0


def test_investigate_uses_reader_role():
    conn = get_reader_conn()
    try:
        result = investigate(_dataset_id(), "SO-1001", line_id="001", conn=conn)
        assert result["facts"]["lines"]
    finally:
        conn.close()


def test_evidence_includes_source_coordinates():
    result = _line_evidence("SO-1003", "001")
    for ev in result["evidence"]:
        assert "source" in ev
        src = ev["source"]
        assert "filename" in src
        assert "sheet" in src
        assert "row" in src
        assert isinstance(src["row"], int)
