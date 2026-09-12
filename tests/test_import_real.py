import hashlib
from pathlib import Path

import pytest

from pi_market.db import drop_dataset, get_admin_conn
from pi_market.importer import import_excel

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_XLSX = PROJECT_ROOT / "deliverables" / "仓储异常测试数据.xlsx"
EXPECTED_SHA256 = "61edff98759f2697c5489d2dd2fd4962637ea38982601d219e6b40a37ddd1df1"
AS_OF = "2026-09-09 20:00:00"


def _dataset_id_for(path: Path) -> str:
    h = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"ds-{h[:12]}"


@pytest.fixture(scope="module")
def real_dataset():
    drop_dataset(_dataset_id_for(REAL_XLSX))
    result = import_excel(REAL_XLSX, AS_OF)
    yield result


def test_real_import_counts(real_dataset):
    assert real_dataset["status"] == "ready"
    assert real_dataset["file_sha256"] == EXPECTED_SHA256
    assert real_dataset["dataset_id"] == _dataset_id_for(REAL_XLSX)
    assert real_dataset["plan_rows"] == 150
    assert real_dataset["flow_rows"] == 170
    assert real_dataset["note_rows"] == 48
    assert real_dataset["issues"] == []


def test_real_import_idempotent(real_dataset):
    second = import_excel(REAL_XLSX, AS_OF)
    assert second["reused"] is True
    assert second["dataset_id"] == real_dataset["dataset_id"]
    assert second["status"] == "ready"
    assert second["plan_rows"] == 150
    assert second["flow_rows"] == 170
    assert second["note_rows"] == 48


def test_leading_zeros_preserved(real_dataset):
    dataset_id = real_dataset["dataset_id"]
    conn = get_admin_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT line_id FROM plan_line WHERE dataset_id = %s LIMIT 5",
                (dataset_id,),
            )
            rows = cur.fetchall()
        assert len(rows) == 5
        for (line_id,) in rows:
            assert isinstance(line_id, str)
            assert line_id in ("001", "002")
    finally:
        conn.close()
