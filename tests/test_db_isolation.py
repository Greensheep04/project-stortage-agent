import psycopg
import pytest

from pi_market import config, db


def test_current_connections_target_test_database():
    assert config.test_database() != config.demo_database()
    with db.get_admin_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            assert cur.fetchone()[0] == config.test_database()


def test_guard_rejects_demo_database():
    kwargs = config.db_conn_kwargs("admin")
    kwargs["dbname"] = config.demo_database()
    with psycopg.connect(**kwargs) as conn:
        with pytest.raises(db.UnsafeDatabaseError):
            db.assert_test_database(conn)


def test_drop_dataset_rejects_demo_database(monkeypatch):
    # The explicit check inside drop_dataset must hold even without the guard flag.
    monkeypatch.setattr(db, "_test_guard_enabled", False)
    real_kwargs = config.db_conn_kwargs

    def _demo_kwargs(role="admin"):
        kwargs = real_kwargs(role)
        kwargs["dbname"] = config.demo_database()
        return kwargs

    monkeypatch.setattr(config, "db_conn_kwargs", _demo_kwargs)
    with pytest.raises(db.UnsafeDatabaseError):
        db.drop_dataset("ds-does-not-exist")


def test_test_database_requires_config(monkeypatch):
    monkeypatch.delenv("PGDATABASE_TEST", raising=False)
    monkeypatch.setattr(config, "_load_env", lambda: None)
    with pytest.raises(RuntimeError):
        config.test_database()


def test_test_database_rejects_demo_name(monkeypatch):
    monkeypatch.setenv("PGDATABASE_TEST", config.demo_database())
    monkeypatch.setattr(config, "_load_env", lambda: None)
    with pytest.raises(RuntimeError):
        config.test_database()
