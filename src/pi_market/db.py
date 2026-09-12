import contextlib
from typing import Any, Dict, Optional

import psycopg
from psycopg import sql

from . import config

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dataset (
    dataset_id      TEXT PRIMARY KEY,
    source_filename TEXT NOT NULL,
    file_sha256     TEXT NOT NULL UNIQUE,
    plan_rows       INTEGER NOT NULL,
    flow_rows       INTEGER NOT NULL,
    note_rows       INTEGER NOT NULL,
    as_of           TIMESTAMPTZ NOT NULL,
    imported_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status          TEXT NOT NULL CHECK (status IN ('ready', 'failed'))
);

CREATE TABLE IF NOT EXISTS plan_line (
    dataset_id  TEXT NOT NULL REFERENCES dataset(dataset_id) ON DELETE CASCADE,
    order_id    TEXT NOT NULL,
    line_id     TEXT NOT NULL,
    warehouse   TEXT NOT NULL,
    sku         TEXT NOT NULL,
    sku_name    TEXT NOT NULL,
    planned_qty INTEGER NOT NULL,
    unit        TEXT NOT NULL,
    order_time  TIMESTAMPTZ NOT NULL,
    agreed_time TIMESTAMPTZ NOT NULL,
    order_status TEXT NOT NULL,
    source      TEXT NOT NULL,
    note        TEXT,
    source_sheet TEXT NOT NULL,
    source_row  INTEGER NOT NULL,
    PRIMARY KEY (dataset_id, order_id, line_id)
);

CREATE TABLE IF NOT EXISTS wms_flow (
    dataset_id  TEXT NOT NULL REFERENCES dataset(dataset_id) ON DELETE CASCADE,
    tx_id       TEXT NOT NULL,
    wh_order_id TEXT NOT NULL,
    wh_line_id  TEXT NOT NULL,
    order_id    TEXT NOT NULL,
    line_id     TEXT NOT NULL,
    warehouse   TEXT NOT NULL,
    sku         TEXT NOT NULL,
    sku_name    TEXT NOT NULL,
    biz_type    TEXT NOT NULL,
    quantity    INTEGER NOT NULL,
    unit        TEXT NOT NULL,
    event_time  TIMESTAMPTZ NOT NULL,
    status      TEXT NOT NULL,
    ref_tx_id   TEXT,
    source      TEXT NOT NULL,
    note        TEXT,
    source_sheet TEXT NOT NULL,
    source_row  INTEGER NOT NULL,
    PRIMARY KEY (dataset_id, tx_id)
);

CREATE TABLE IF NOT EXISTS evidence_note (
    dataset_id  TEXT NOT NULL REFERENCES dataset(dataset_id) ON DELETE CASCADE,
    ev_id       TEXT NOT NULL,
    order_id    TEXT NOT NULL,
    line_id     TEXT NOT NULL,
    ref_tx_id   TEXT,
    record_time TIMESTAMPTZ NOT NULL,
    record_type TEXT NOT NULL,
    recorder    TEXT NOT NULL,
    content     TEXT NOT NULL,
    source_sheet TEXT NOT NULL,
    source_row  INTEGER NOT NULL,
    PRIMARY KEY (dataset_id, ev_id)
);

CREATE TABLE IF NOT EXISTS chunk (
    chunk_id        TEXT PRIMARY KEY,
    dataset_id      TEXT NOT NULL REFERENCES dataset(dataset_id) ON DELETE CASCADE,
    embedding_model TEXT NOT NULL,
    parent_type     TEXT NOT NULL CHECK (parent_type IN ('note', 'flow_memo')),
    parent_id       TEXT NOT NULL,
    order_id        TEXT NOT NULL,
    line_id         TEXT NOT NULL,
    tx_id           TEXT,
    record_time     TIMESTAMPTZ,
    content         TEXT NOT NULL,
    source_sheet    TEXT NOT NULL,
    source_row      INTEGER NOT NULL,
    embedding       vector(1024),
    indexed_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_chunk_dataset_model
    ON chunk(dataset_id, embedding_model);

CREATE TABLE IF NOT EXISTS corpus (
    corpus_id       TEXT PRIMARY KEY,
    dataset_id      TEXT NOT NULL REFERENCES dataset(dataset_id) ON DELETE CASCADE,
    snapshot_sha256 TEXT NOT NULL,
    events_sha256   TEXT NOT NULL,
    cases_sha256    TEXT NOT NULL,
    sops_sha256     TEXT NOT NULL,
    parser_version  TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('ready', 'failed')),
    imported_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS corpus_doc (
    corpus_id       TEXT NOT NULL REFERENCES corpus(corpus_id) ON DELETE CASCADE,
    doc_id          TEXT NOT NULL,
    doc_type        TEXT NOT NULL CHECK (doc_type IN ('event', 'case', 'sop')),
    title           TEXT NOT NULL,
    recorded_at     TIMESTAMPTZ NOT NULL,
    occurred_at     TIMESTAMPTZ,
    closed_at       TIMESTAMPTZ,
    policy_id       TEXT,
    version         TEXT,
    effective_from  TIMESTAMPTZ,
    effective_to    TIMESTAMPTZ,
    supersedes      TEXT,
    author_role     TEXT NOT NULL,
    scope_warehouse TEXT,
    scope_sku       TEXT,
    scope_order_id  TEXT,
    scope_line_id   TEXT,
    history_case_id TEXT,
    jsonl_file      TEXT NOT NULL,
    jsonl_row       INTEGER NOT NULL,
    file_sha256     TEXT NOT NULL,
    raw             JSONB NOT NULL,
    PRIMARY KEY (corpus_id, doc_id)
);

CREATE INDEX IF NOT EXISTS idx_corpus_doc_type
    ON corpus_doc(corpus_id, doc_type);

CREATE TABLE IF NOT EXISTS corpus_chunk (
    chunk_id        TEXT PRIMARY KEY,
    corpus_id       TEXT NOT NULL,
    doc_id          TEXT NOT NULL,
    section_id      TEXT NOT NULL,
    heading         TEXT NOT NULL,
    content         TEXT NOT NULL,
    embedding       vector(1024),
    embedding_model TEXT NOT NULL,
    indexed_at      TIMESTAMPTZ,
    FOREIGN KEY (corpus_id, doc_id) REFERENCES corpus_doc(corpus_id, doc_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_corpus_chunk_doc
    ON corpus_chunk(corpus_id, doc_id);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pi_reader') THEN
        GRANT USAGE ON SCHEMA public TO pi_reader;
        GRANT SELECT ON dataset, plan_line, wms_flow, evidence_note, chunk TO pi_reader;
        GRANT SELECT ON corpus, corpus_doc, corpus_chunk TO pi_reader;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO pi_reader;
    END IF;
END
$$;
"""


class UnsafeDatabaseError(RuntimeError):
    """Raised when a destructive operation would run outside the test database."""


_test_guard_enabled = False


def enable_test_guard() -> None:
    """Require every connection opened afterwards to target the test database."""
    global _test_guard_enabled
    _test_guard_enabled = True


def assert_test_database(conn: psycopg.Connection) -> str:
    """Raise unless the open connection targets the configured test database."""
    with conn.cursor() as cur:
        return _assert_test_cursor(cur)


def _assert_test_cursor(cur: psycopg.Cursor) -> str:
    expected = config.test_database()
    cur.execute("SELECT current_database()")
    actual = cur.fetchone()[0]
    if actual != expected:
        raise UnsafeDatabaseError(
            f"破坏性操作被拒绝：当前连接库 {actual}，要求测试库 {expected}"
        )
    return actual


def _connect(user_role: str) -> psycopg.Connection:
    conn = psycopg.connect(**config.db_conn_kwargs(user_role))
    if _test_guard_enabled:
        try:
            assert_test_database(conn)
        except Exception:
            conn.close()
            raise
    return conn


def get_admin_conn() -> psycopg.Connection:
    return _connect("admin")


def get_reader_conn() -> psycopg.Connection:
    return _connect("reader")


def ensure_schema(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)


def init_db() -> None:
    with get_admin_conn() as conn:
        ensure_schema(conn)
        conn.commit()


def ensure_test_database() -> Dict[str, Any]:
    """Create the isolated test database when missing and apply the schema.

    Idempotent; connects to the demo database only to run CREATE DATABASE, then
    applies pgvector and the schema inside the test database.
    """
    test_db = config.test_database()

    demo_kwargs = config.db_conn_kwargs("admin")
    demo_kwargs["dbname"] = config.demo_database() or demo_kwargs["dbname"]
    demo_kwargs["autocommit"] = True
    with psycopg.connect(**demo_kwargs) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (test_db,))
            if cur.fetchone() is None:
                cur.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(test_db))
                )

    test_kwargs = config.db_conn_kwargs("admin")
    test_kwargs["dbname"] = test_db
    with psycopg.connect(**test_kwargs) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        ensure_schema(conn)
        conn.commit()

    return {"test_database": test_db, "status": "ready"}


@contextlib.contextmanager
def admin_cursor():
    conn = get_admin_conn()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def drop_dataset(dataset_id: str) -> None:
    """Remove a dataset and all its child rows. Test cleanup only."""
    with admin_cursor() as cur:
        _assert_test_cursor(cur)
        cur.execute("DELETE FROM dataset WHERE dataset_id = %s", (dataset_id,))
