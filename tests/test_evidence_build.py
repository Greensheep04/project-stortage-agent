import pytest

from pi_market import evidence

DATASET_ID = "ds-61edff98759f"
MOCK_MODEL = "test-mock-vec"


@pytest.fixture(autouse=True)
def cleanup_mock_index():
    evidence.clear_index(DATASET_ID, MOCK_MODEL)
    yield
    evidence.clear_index(DATASET_ID, MOCK_MODEL)


def test_build_chunks_creates_rows_without_vectors():
    status = evidence.build_chunks(DATASET_ID, MOCK_MODEL)
    assert status["status"] == "chunk_ready"
    assert status["chunks"] == 218
    assert status["vector_ready"] == 0
    assert status["vector_built"] is False

    idx = evidence.get_index_status(DATASET_ID, MOCK_MODEL)
    assert idx["chunk_ready"] is True
    assert idx["vector_ready_all"] is False


def test_fill_embeddings_makes_vectors_ready():
    evidence.build_chunks(DATASET_ID, MOCK_MODEL)
    fill = evidence.fill_embeddings(DATASET_ID, MOCK_MODEL, evidence.mock_embedder())
    assert fill["status"] == "vector_ready"
    assert fill["filled"] == 218
    assert fill["failed"] == 0

    idx = evidence.get_index_status(DATASET_ID, MOCK_MODEL)
    assert idx["vector_ready"] == 218
    assert idx["vector_ready_all"] is True


def test_build_index_default_creates_chunks_and_vectors():
    status = evidence.build_index(DATASET_ID, MOCK_MODEL, evidence.mock_embedder())
    assert status["status"] == "ready"
    assert status["chunks"] == 218
    assert status["vector_ready"] == 218
    assert status["vector_built"] is True


def test_build_index_idempotent():
    r1 = evidence.build_index(DATASET_ID, MOCK_MODEL, evidence.mock_embedder())
    assert r1["status"] == "ready"
    r2 = evidence.build_index(DATASET_ID, MOCK_MODEL, evidence.mock_embedder())
    assert r2["chunks"] == r1["chunks"] == 218
    assert r2["vector_ready"] == 218
