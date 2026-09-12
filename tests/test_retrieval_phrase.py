import pytest

from pi_market import evidence, retrieval

DATASET_ID = "ds-61edff98759f"
MOCK_MODEL = "test-mock-vec"


@pytest.fixture(autouse=True)
def cleanup_mock_index():
    evidence.clear_index(DATASET_ID, MOCK_MODEL)
    yield
    evidence.clear_index(DATASET_ID, MOCK_MODEL)


def test_phrase_score_basic():
    assert retrieval.phrase_score("外包装破损", "外包装破损，移至待检区") > 0.5
    assert retrieval.phrase_score("完全无关", "标签贴错退回") == 0.0


def test_phrase_search_works_without_vectors():
    evidence.build_chunks(DATASET_ID, MOCK_MODEL)
    result = retrieval.search(
        DATASET_ID,
        "外包装破损移至待检区",
        mode="phrase",
        k=10,
        embedding_model=MOCK_MODEL,
    )
    assert result["mode"] == "phrase"
    assert result["disclaimer"] == "Top-k 结果为相关样本，不代表完整统计。"
    # Zero-score chunks are filtered out, so fewer than k results may return.
    assert 0 < len(result["results"]) <= 10
    assert all(r["score"] > 0 for r in result["results"])
    parent_ids = {r["parent_id"] for r in result["results"]}
    assert "EV-1006" in parent_ids
    assert "TX-1026" in parent_ids


def test_vector_rrf_require_vectors_before_ready():
    evidence.build_chunks(DATASET_ID, MOCK_MODEL)
    with pytest.raises(retrieval.IndexNotReadyError):
        retrieval.search(
            DATASET_ID,
            "外包装破损",
            mode="vector",
            k=5,
            embedding_model=MOCK_MODEL,
        )
    with pytest.raises(retrieval.IndexNotReadyError):
        retrieval.search(
            DATASET_ID,
            "外包装破损",
            mode="rrf",
            k=5,
            embedding_model=MOCK_MODEL,
        )


def test_as_of_filter_excludes_late_records():
    evidence.build_chunks(DATASET_ID, MOCK_MODEL)
    result = retrieval.search(
        DATASET_ID,
        "外包装破损",
        mode="phrase",
        k=10,
        as_of="2026-08-01 00:00:00",
        embedding_model=MOCK_MODEL,
    )
    assert result["results"] == []
