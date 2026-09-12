import hashlib
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import psycopg

from . import db
from .importer import _parse_as_of


class EvidenceError(Exception):
    pass


def _make_chunk_id(dataset_id: str, embedding_model: str, parent_type: str, parent_id: str) -> str:
    return f"{dataset_id}:{embedding_model}:{parent_type}:{parent_id}"


def _fetch_source_chunks(cur: psycopg.Cursor, dataset_id: str) -> List[Dict[str, Any]]:
    chunks: List[Dict[str, Any]] = []

    cur.execute(
        """
        SELECT ev_id, order_id, line_id, record_time, content, source_sheet, source_row
        FROM evidence_note
        WHERE dataset_id = %s
        ORDER BY source_row
        """,
        (dataset_id,),
    )
    for row in cur.fetchall():
        ev_id, order_id, line_id, record_time, content, source_sheet, source_row = row
        chunks.append(
            {
                "parent_type": "note",
                "parent_id": ev_id,
                "order_id": order_id,
                "line_id": line_id,
                "tx_id": None,
                "record_time": record_time,
                "content": content,
                "source_sheet": source_sheet,
                "source_row": source_row,
            }
        )

    cur.execute(
        """
        SELECT tx_id, order_id, line_id, event_time, note, source_sheet, source_row
        FROM wms_flow
        WHERE dataset_id = %s AND note IS NOT NULL AND note <> ''
        ORDER BY source_row
        """,
        (dataset_id,),
    )
    for row in cur.fetchall():
        tx_id, order_id, line_id, event_time, note, source_sheet, source_row = row
        chunks.append(
            {
                "parent_type": "flow_memo",
                "parent_id": tx_id,
                "order_id": order_id,
                "line_id": line_id,
                "tx_id": tx_id,
                "record_time": event_time,
                "content": note,
                "source_sheet": source_sheet,
                "source_row": source_row,
            }
        )

    return chunks


def _normalize_embedding(v: List[float]) -> List[float]:
    norm = sum(x * x for x in v) ** 0.5
    if norm == 0:
        return v
    return [x / norm for x in v]


def mock_embedder(seed: str = "mock") -> Callable[[str], List[float]]:
    """Deterministic pseudo-embedding for unit tests.

    Produces a normalized 1024-dim vector from the SHA-256 hash of the input.
    This is NOT a semantic embedding and must only be used in tests labeled mock.
    """

    def _embed(text: str) -> List[float]:
        vec: List[float] = []
        digest = hashlib.sha256(f"{seed}:{text}".encode("utf-8")).digest()
        while len(vec) < 1024:
            digest = hashlib.sha256(digest).digest()
            for i in range(0, len(digest), 4):
                if len(vec) >= 1024:
                    break
                val = int.from_bytes(digest[i : i + 4], "big") / (2**32) * 2 - 1
                vec.append(val)
        return _normalize_embedding(vec)

    return _embed


def _assert_dataset_ready(cur: psycopg.Cursor, dataset_id: str) -> None:
    cur.execute("SELECT status FROM dataset WHERE dataset_id = %s", (dataset_id,))
    row = cur.fetchone()
    if not row:
        raise EvidenceError(f"数据集不存在: {dataset_id}")
    if row[0] != "ready":
        raise EvidenceError(f"数据集未就绪: {dataset_id} 状态={row[0]}")


def _index_counts(
    cur: psycopg.Cursor, dataset_id: str, embedding_model: str
) -> Tuple[int, int]:
    cur.execute(
        """
        SELECT COUNT(*), COUNT(embedding)
        FROM chunk
        WHERE dataset_id = %s AND embedding_model = %s
        """,
        (dataset_id, embedding_model),
    )
    total, with_embedding = cur.fetchone()
    return total or 0, with_embedding or 0


def build_chunks(
    dataset_id: str,
    embedding_model: str,
    conn: Optional[psycopg.Connection] = None,
) -> Dict[str, Any]:
    """Create or refresh chunk rows without fetching embeddings.

    Each piece of evidence and every non-empty flow note becomes one chunk.
    Re-running with the same (dataset_id, embedding_model) updates existing rows
    instead of creating duplicates.
    """
    own_conn = conn is None
    if own_conn:
        conn = db.get_admin_conn()

    try:
        with conn.cursor() as cur:
            _assert_dataset_ready(cur, dataset_id)
            source_chunks = _fetch_source_chunks(cur, dataset_id)
            indexed_at = datetime.now(timezone.utc)

            for chunk in source_chunks:
                chunk_id = _make_chunk_id(
                    dataset_id, embedding_model, chunk["parent_type"], chunk["parent_id"]
                )
                cur.execute(
                    """
                    INSERT INTO chunk (
                        chunk_id, dataset_id, embedding_model, parent_type, parent_id,
                        order_id, line_id, tx_id, record_time, content,
                        source_sheet, source_row, embedding, indexed_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, NULL, %s
                    )
                    ON CONFLICT (chunk_id) DO UPDATE SET
                        order_id = EXCLUDED.order_id,
                        line_id = EXCLUDED.line_id,
                        tx_id = EXCLUDED.tx_id,
                        record_time = EXCLUDED.record_time,
                        content = EXCLUDED.content,
                        source_sheet = EXCLUDED.source_sheet,
                        source_row = EXCLUDED.source_row,
                        indexed_at = EXCLUDED.indexed_at
                        -- embedding 由 fill_embeddings 单独更新，避免无向量时覆盖已有向量
                    """,
                    (
                        chunk_id,
                        dataset_id,
                        embedding_model,
                        chunk["parent_type"],
                        chunk["parent_id"],
                        chunk["order_id"],
                        chunk["line_id"],
                        chunk["tx_id"],
                        chunk["record_time"],
                        chunk["content"],
                        chunk["source_sheet"],
                        chunk["source_row"],
                        indexed_at,
                    ),
                )

            total, vector_ready = _index_counts(cur, dataset_id, embedding_model)

        if own_conn:
            conn.commit()

        return {
            "dataset_id": dataset_id,
            "embedding_model": embedding_model,
            "status": "chunk_ready",
            "chunks": total,
            "vector_ready": vector_ready,
            "vector_built": False,
            "indexed_at": indexed_at.isoformat(),
        }
    except Exception:
        if own_conn:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


def fill_embeddings(
    dataset_id: str,
    embedding_model: str,
    embed_fn: Callable[[str], List[float]],
    conn: Optional[psycopg.Connection] = None,
) -> Dict[str, Any]:
    """Fill NULL embeddings for an existing chunk set.

    Updates in a per-chunk best-effort fashion: successful embeddings are
    committed, failures leave the corresponding embedding NULL so the caller
    can observe a partial vector-ready state.
    """
    own_conn = conn is None
    if own_conn:
        conn = db.get_admin_conn()

    filled = 0
    failed = 0
    last_error: Optional[str] = None
    indexed_at = datetime.now(timezone.utc)

    try:
        with conn.cursor() as cur:
            _assert_dataset_ready(cur, dataset_id)
            cur.execute(
                """
                SELECT chunk_id, content
                FROM chunk
                WHERE dataset_id = %s AND embedding_model = %s AND embedding IS NULL
                ORDER BY chunk_id
                """,
                (dataset_id, embedding_model),
            )
            pending = cur.fetchall()

            for chunk_id, content in pending:
                try:
                    embedding = embed_fn(content)
                    if len(embedding) != 1024:
                        raise EvidenceError(f"向量维度错误: 期望 1024, 实际 {len(embedding)}")
                    cur.execute(
                        """
                        UPDATE chunk
                        SET embedding = %s, indexed_at = %s
                        WHERE chunk_id = %s
                        """,
                        (embedding, indexed_at, chunk_id),
                    )
                    filled += 1
                except Exception as e:
                    failed += 1
                    last_error = str(e)

            total, vector_ready = _index_counts(cur, dataset_id, embedding_model)

        if own_conn:
            conn.commit()

        return {
            "dataset_id": dataset_id,
            "embedding_model": embedding_model,
            "status": "vector_ready" if failed == 0 and total > 0 and vector_ready == total else "partial",
            "chunks": total,
            "vector_ready": vector_ready,
            "filled": filled,
            "failed": failed,
            "last_error": last_error,
            "indexed_at": indexed_at.isoformat(),
        }
    except Exception:
        if own_conn:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


def build_index(
    dataset_id: str,
    embedding_model: str,
    embed_fn: Optional[Callable[[str], List[float]]] = None,
    no_embedding: bool = False,
    conn: Optional[psycopg.Connection] = None,
) -> Dict[str, Any]:
    """Convenience: build chunks, then optionally fill embeddings.

    If ``no_embedding`` is True or ``embed_fn`` is None, only chunks are created.
    If embedding filling fails, chunk rows are preserved and the returned status
    clearly reports that chunks are ready while vectors are not.
    """
    chunk_status = build_chunks(dataset_id, embedding_model, conn=conn)

    if no_embedding or embed_fn is None:
        return chunk_status

    try:
        fill_status = fill_embeddings(dataset_id, embedding_model, embed_fn)
        return {
            "dataset_id": dataset_id,
            "embedding_model": embedding_model,
            "status": "ready" if fill_status["status"] == "vector_ready" else "partial",
            "chunks": fill_status["chunks"],
            "vector_ready": fill_status["vector_ready"],
            "vector_built": True,
            "filled": fill_status["filled"],
            "failed": fill_status["failed"],
            "last_error": fill_status["last_error"],
            "indexed_at": fill_status["indexed_at"],
        }
    except Exception as e:
        return {
            "dataset_id": dataset_id,
            "embedding_model": embedding_model,
            "status": "chunk_ready_embedding_failed",
            "chunks": chunk_status["chunks"],
            "vector_ready": chunk_status["vector_ready"],
            "vector_built": False,
            "last_error": str(e),
            "indexed_at": chunk_status["indexed_at"],
        }


def get_index_status(
    dataset_id: str,
    embedding_model: str,
    conn: Optional[psycopg.Connection] = None,
) -> Dict[str, Any]:
    own_conn = conn is None
    if own_conn:
        conn = db.get_reader_conn()
    try:
        with conn.cursor() as cur:
            total, vector_ready = _index_counts(cur, dataset_id, embedding_model)
            cur.execute(
                "SELECT MAX(indexed_at) FROM chunk WHERE dataset_id = %s AND embedding_model = %s",
                (dataset_id, embedding_model),
            )
            last_indexed = cur.fetchone()[0]
        return {
            "dataset_id": dataset_id,
            "embedding_model": embedding_model,
            "chunks": total,
            "vector_ready": vector_ready,
            "chunk_ready": total > 0,
            "vector_ready_all": total > 0 and vector_ready == total,
            "indexed_at": last_indexed.isoformat() if last_indexed else None,
        }
    finally:
        if own_conn:
            conn.close()


def clear_index(
    dataset_id: str,
    embedding_model: str,
    conn: Optional[psycopg.Connection] = None,
) -> None:
    """Remove indexed chunks for a specific (dataset_id, embedding_model).

    Intended for test cleanup only.
    """
    own_conn = conn is None
    if own_conn:
        conn = db.get_admin_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM chunk WHERE dataset_id = %s AND embedding_model = %s",
                (dataset_id, embedding_model),
            )
        if own_conn:
            conn.commit()
    finally:
        if own_conn:
            conn.close()
