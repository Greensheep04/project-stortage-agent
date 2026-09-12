import hashlib
from pathlib import Path

import pytest

from pi_market.db import drop_dataset, get_reader_conn
from pi_market.evidence import clear_index
from pi_market.importer import import_excel
from pi_market.investigate import investigate
from pi_market.retrieval import IndexNotReadyError, search

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_XLSX = PROJECT_ROOT / "deliverables" / "仓储异常测试数据.xlsx"
AS_OF = "2026-09-09 20:00:00"
EMBEDDING_MODEL = "mock-test-model"


def _dataset_id() -> str:
    h = hashlib.sha256(REAL_XLSX.read_bytes()).hexdigest()
    return f"ds-{h[:12]}"


@pytest.fixture(scope="module", autouse=True)
def ensure_imported():
    drop_dataset(_dataset_id())
    import_excel(REAL_XLSX, AS_OF)
    yield


@pytest.fixture(autouse=True)
def ensure_no_index():
    clear_index(_dataset_id(), EMBEDDING_MODEL)
    yield
    clear_index(_dataset_id(), EMBEDDING_MODEL)


def test_investigate_works_without_vector_index():
    result = investigate(_dataset_id(), "SO-1003", line_id="001")
    assert result["facts"]["lines"][0]["status_tags"]
    assert len(result["evidence"]) > 0


def test_search_fails_without_index():
    with pytest.raises(IndexNotReadyError):
        search(_dataset_id(), "分批出库", embedding_model=EMBEDDING_MODEL)


def test_investigate_uses_reader_role_without_index():
    conn = get_reader_conn()
    try:
        result = investigate(_dataset_id(), "SO-1005", line_id="001", conn=conn)
        assert "sku_mismatch" in result["facts"]["lines"][0]["status_tags"]
    finally:
        conn.close()
