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


def test_mock_vector_exact_match_zero_distance():
    _ensure_vector_ready()
    target = "扫描复核后装车发出。"
    result = retrieval.search(
        DATASET_ID,
        target,
        mode="vector",
        k=5,
        embed_fn=evidence.mock_embedder(),
        embedding_model=MOCK_MODEL,
    )
    assert result["mode"] == "vector"
    top = result["results"][0]
    assert top["content"] == target
    assert top["distance"] == pytest.approx(0.0, abs=1e-9)


def test_mock_vector_as_of_filter():
    _ensure_vector_ready()
    result = retrieval.search(
        DATASET_ID,
        "扫描复核后装车发出。",
        mode="vector",
        k=5,
        as_of="2026-08-01 00:00:00",
        embed_fn=evidence.mock_embedder(),
        embedding_model=MOCK_MODEL,
    )
    assert result["results"] == []


def test_vector_index_not_ready_when_chunks_have_no_embeddings():
    evidence.build_chunks(DATASET_ID, MOCK_MODEL)
    with pytest.raises(retrieval.IndexNotReadyError) as exc:
        retrieval.search(
            DATASET_ID,
            "扫描复核",
            mode="vector",
            k=5,
            embed_fn=evidence.mock_embedder(),
            embedding_model=MOCK_MODEL,
        )
    assert exc.value.detail["chunks"] == 218
    assert exc.value.detail["vector_ready"] == 0
