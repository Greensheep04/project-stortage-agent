from datetime import datetime, timezone
from pathlib import Path

from pi_market import db, evaluate, evidence, retrieval

DATASET_ID = "ds-61edff98759f"
MOCK_MODEL = "test-mock-vec"


def test_phrase_filters_zero_score_and_sorts_stably(monkeypatch):
    chunks = [
        {"chunk_id": "c3", "content": "完全无关的内容"},
        {"chunk_id": "c2", "content": "外包装破损"},
        {"chunk_id": "c1", "content": "外包装破损"},
        {"chunk_id": "c0", "content": "外包装破损移库"},
    ]
    monkeypatch.setattr(
        retrieval,
        "_list_chunks_for_phrase",
        lambda *args, **kwargs: [dict(c) for c in chunks],
    )

    results = retrieval._search_phrase(None, "ds", "m", None, "外包装破损", 10)
    assert [c["chunk_id"] for c in results] == ["c1", "c2", "c0"]

    truncated = retrieval._search_phrase(None, "ds", "m", None, "外包装破损", 2)
    assert [c["chunk_id"] for c in truncated] == ["c1", "c2"]


def test_rrf_tie_break_by_chunk_id(monkeypatch):
    def fake_phrase(cur, dataset_id, model, as_of, query, k):
        return [{"chunk_id": "b", "content": "x"}, {"chunk_id": "a", "content": "y"}]

    def fake_vector(cur, dataset_id, model, as_of, query, k, embed_fn):
        return [{"chunk_id": "a", "content": "y"}, {"chunk_id": "b", "content": "x"}]

    monkeypatch.setattr(retrieval, "_search_phrase", fake_phrase)
    monkeypatch.setattr(retrieval, "_search_vector", fake_vector)

    results = retrieval._search_rrf(None, "ds", "m", None, "q", 5, lambda t: [0.0] * 1024)
    assert [c["chunk_id"] for c in results] == ["a", "b"]


def test_vector_tie_break_and_truncation():
    dataset_id = "ds-stability-test"
    model = "stability-model"
    vec = [0.0] * 1024
    vec[0] = 1.0
    now = datetime.now(timezone.utc)

    conn = db.get_admin_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM dataset WHERE dataset_id = %s", (dataset_id,))
            cur.execute(
                """
                INSERT INTO dataset (
                    dataset_id, source_filename, file_sha256,
                    plan_rows, flow_rows, note_rows, as_of, status
                ) VALUES (%s, 'synthetic.xlsx', 'stability-test-sha', 0, 0, 0, %s, 'ready')
                """,
                (dataset_id, now),
            )
            for cid in ("c-c", "c-a", "c-b"):
                cur.execute(
                    """
                    INSERT INTO chunk (
                        chunk_id, dataset_id, embedding_model, parent_type, parent_id,
                        order_id, line_id, tx_id, record_time, content,
                        source_sheet, source_row, embedding, indexed_at
                    ) VALUES (%s, %s, %s, 'note', %s, 'SO-X', '001', NULL, %s, 'same content', 'S', 1, %s, %s)
                    """,
                    (cid, dataset_id, model, cid, now, vec, now),
                )
        conn.commit()

        with conn.cursor() as cur:
            results = retrieval._search_vector(
                cur, dataset_id, model, now, "query", 2, lambda t: vec
            )
        assert [r["chunk_id"] for r in results] == ["c-a", "c-b"]
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM dataset WHERE dataset_id = %s", (dataset_id,))
        conn.commit()
        conn.close()


def test_evaluate_results_stable_across_runs(monkeypatch):
    monkeypatch.setattr(
        retrieval,
        "get_embedder",
        lambda model=None: (evidence.mock_embedder(), MOCK_MODEL),
    )
    evidence.build_index(DATASET_ID, MOCK_MODEL, evidence.mock_embedder())
    try:
        queries = evaluate.load_queries(
            Path(__file__).resolve().parent / "eval" / "dev_queries.jsonl"
        )
        for mode in ("phrase", "vector", "rrf"):
            first = evaluate.evaluate_mode(
                DATASET_ID, queries, mode, 10, embedding_model=MOCK_MODEL
            )
            second = evaluate.evaluate_mode(
                DATASET_ID, queries, mode, 10, embedding_model=MOCK_MODEL
            )
            first_ids = [(q["qid"], q.get("returned_parent_ids")) for q in first["per_query"]]
            second_ids = [(q["qid"], q.get("returned_parent_ids")) for q in second["per_query"]]
            assert first_ids == second_ids
    finally:
        evidence.clear_index(DATASET_ID, MOCK_MODEL)
