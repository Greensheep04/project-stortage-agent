import pytest

from pi_market import evidence, retrieval

DATASET_ID = "ds-61edff98759f"
MOCK_MODEL = "test-mock-vec"


@pytest.fixture(autouse=True)
def cleanup_mock_index():
    evidence.clear_index(DATASET_ID, MOCK_MODEL)
    yield
    evidence.clear_index(DATASET_ID, MOCK_MODEL)


def _ensure_vector_ready():
    evidence.build_chunks(DATASET_ID, MOCK_MODEL)
    evidence.fill_embeddings(DATASET_ID, MOCK_MODEL, evidence.mock_embedder())


def test_rrf_deduplicates_same_chunk():
    _ensure_vector_ready()
    target = "扫描复核后装车发出。"
    result = retrieval.search(
        DATASET_ID,
        target,
        mode="rrf",
        k=5,
        embed_fn=evidence.mock_embedder(),
        embedding_model=MOCK_MODEL,
    )
    assert result["mode"] == "rrf"
    chunk_ids = [r["chunk_id"] for r in result["results"]]
    assert len(chunk_ids) == len(set(chunk_ids))
    assert result["results"][0]["content"] == target


def test_rrf_as_of_filter():
    _ensure_vector_ready()
    result = retrieval.search(
        DATASET_ID,
        "扫描复核",
        mode="rrf",
        k=5,
        as_of="2026-08-01 00:00:00",
        embed_fn=evidence.mock_embedder(),
        embedding_model=MOCK_MODEL,
    )
    assert result["results"] == []


def test_rrf_index_not_ready_without_vectors():
    evidence.build_chunks(DATASET_ID, MOCK_MODEL)
    with pytest.raises(retrieval.IndexNotReadyError):
        retrieval.search(
            DATASET_ID,
            "扫描复核",
            mode="rrf",
            k=5,
            embed_fn=evidence.mock_embedder(),
            embedding_model=MOCK_MODEL,
        )
