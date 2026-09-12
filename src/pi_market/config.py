import os
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values, load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"


def _load_env() -> None:
    path = str(_ENV_PATH)
    try:
        load_dotenv(path, override=False)
    except Exception:
        load_dotenv(override=False)


def demo_database() -> Optional[str]:
    """Demo/evaluation database name declared in .env (file wins over process env)."""
    try:
        value = dotenv_values(str(_ENV_PATH)).get("PGDATABASE")
    except Exception:
        value = None
    return value or os.getenv("PGDATABASE")


def test_database() -> str:
    """Isolated test database name; stops when missing or identical to the demo DB."""
    _load_env()
    name = os.getenv("PGDATABASE_TEST")
    if not name:
        raise RuntimeError("未配置 PGDATABASE_TEST，测试无法在隔离数据库运行")
    demo = demo_database()
    if demo and name == demo:
        raise RuntimeError(f"PGDATABASE_TEST 不能与演示库相同: {name}")
    return name


def db_conn_kwargs(user_role: str = "admin") -> dict:
    """Return kwargs for psycopg.connect without exposing values in logs."""
    _load_env()
    if user_role == "reader":
        user = os.getenv("PGREADER_USER")
    elif user_role == "admin":
        user = os.getenv("PGUSER")
    else:
        raise ValueError(f"unknown user_role: {user_role}")
    return {
        "host": os.getenv("PGHOST"),
        "port": os.getenv("PGPORT"),
        "dbname": os.getenv("PGDATABASE"),
        "user": user,
    }
