import os
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from pi_market import config, db

DATASET_ID = "ds-61edff98759f"
REAL_XLSX = _PROJECT_ROOT / "deliverables" / "仓储异常测试数据.xlsx"
AS_OF = "2026-09-09 20:00:00"

# Route every test connection to the isolated test database before tests run.
_TEST_DB = config.test_database()
os.environ["PGDATABASE"] = _TEST_DB
db.enable_test_guard()
db.ensure_test_database()


@pytest.fixture(scope="session", autouse=True)
def _seed_real_dataset():
    """Some modules rely on the real dataset; import it once into the test DB."""
    from pi_market.importer import import_excel

    import_excel(REAL_XLSX, AS_OF)
    return DATASET_ID
