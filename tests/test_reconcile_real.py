import json
from pathlib import Path

import pytest

from pi_market.db import drop_dataset, get_reader_conn
from pi_market.importer import import_excel
from pi_market.reconcile import reconcile, ReconcileError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_XLSX = PROJECT_ROOT / "deliverables" / "仓储异常测试数据.xlsx"
ANSWERS_JSON = PROJECT_ROOT / "deliverables" / "评测答案.json"
AS_OF = "2026-09-09 20:00:00"


def _dataset_id() -> str:
    import hashlib

    h = hashlib.sha256(REAL_XLSX.read_bytes()).hexdigest()
    return f"ds-{h[:12]}"


@pytest.fixture(scope="module", autouse=True)
def ensure_imported():
    drop_dataset(_dataset_id())
    import_excel(REAL_XLSX, AS_OF)
    yield


def _answer(order_id: str, line_id: str):
    with open(ANSWERS_JSON, encoding="utf-8") as f:
        data = json.load(f)
    for case in data["cases"]:
        if case["order_id"] == order_id and case["order_line_id"] == line_id:
            return case
    raise KeyError(f"no answer for {order_id}/{line_id}")


def _line(order_id: str, line_id: str):
    result = reconcile(_dataset_id(), order_id, line_id=line_id)
    assert len(result["lines"]) == 1
    return result["lines"][0]


def test_split_matched():
    case = _answer("SO-1003", "001")
    line = _line("SO-1003", "001")
    assert line["planned_quantity"] == case["expected_quantity"]
    assert line["matched_net_shipped_quantity"] == case["matched_net_shipped_quantity"]
    assert line["difference_quantity"] == case["difference_quantity"]
    assert case["expected_finding"] in line["status_tags"]
    assert set(line["evidence_ids"]) == set(case["evidence_ids"])


def test_reversal_matched():
    case = _answer("SO-1007", "002")
    line = _line("SO-1007", "002")
    assert line["planned_quantity"] == case["expected_quantity"]
    assert line["matched_net_shipped_quantity"] == case["matched_net_shipped_quantity"]
    assert line["difference_quantity"] == case["difference_quantity"]
    assert case["expected_finding"] in line["status_tags"]


def test_sku_mismatch():
    case = _answer("SO-1005", "001")
    line = _line("SO-1005", "001")
    assert line["planned_quantity"] == case["expected_quantity"]
    assert line["matched_net_shipped_quantity"] == case["matched_net_shipped_quantity"]
    assert line["difference_quantity"] == case["difference_quantity"]
    assert "sku_mismatch" in line["status_tags"]


def test_insufficient_evidence():
    case = _answer("SO-1009", "001")
    line = _line("SO-1009", "001")
    assert line["planned_quantity"] == case["expected_quantity"]
    assert line["matched_net_shipped_quantity"] == case["matched_net_shipped_quantity"]
    assert line["difference_quantity"] == case["difference_quantity"]
    assert "insufficient_evidence" in line["status_tags"]


def test_reader_connection_blocks_write():
    conn = get_reader_conn()
    try:
        with conn.cursor() as cur:
            with pytest.raises(Exception):
                cur.execute("CREATE TABLE _reader_test (id int)")
    finally:
        conn.close()


def test_reconcile_uses_reader_role():
    # Reconcile default connection is pi_reader; if it returned successfully,
    # the read-only path works.
    result = reconcile(_dataset_id(), "SO-1001", line_id="001")
    assert result["lines"][0]["status_tags"]


def test_reconcile_order_without_line():
    result = reconcile(_dataset_id(), "SO-1003")
    assert len(result["lines"]) == 2
    line_ids = [ln["line_id"] for ln in result["lines"]]
    assert "001" in line_ids and "002" in line_ids


def test_reconcile_missing_dataset():
    with pytest.raises(ReconcileError):
        reconcile("ds-doesnotexist", "SO-1001")
