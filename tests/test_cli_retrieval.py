import json

import pytest

from pi_market import cli, evidence, retrieval

DATASET_ID = "ds-61edff98759f"
MOCK_MODEL = "mock-cli"


@pytest.fixture
def mock_embedder(monkeypatch):
    def _fake(model=None):
        return evidence.mock_embedder(), MOCK_MODEL

    monkeypatch.setattr(retrieval, "get_embedder", _fake)


@pytest.fixture(autouse=True)
def cleanup_mock_index():
    evidence.clear_index(DATASET_ID, MOCK_MODEL)
    yield
    evidence.clear_index(DATASET_ID, MOCK_MODEL)


def test_cli_index_no_embedding_builds_chunks_only(mock_embedder, capsys):
    rc = cli.main(["index", DATASET_ID, "--no-embedding", "--embedding-model", MOCK_MODEL])
    captured = capsys.readouterr()
    assert rc == 0
    result = json.loads(captured.out)
    assert result["status"] == "chunk_ready"
    assert result["chunks"] == 218
    assert result["vector_ready"] == 0
    assert result["vector_built"] is False


def test_cli_search_phrase_without_vectors(mock_embedder, capsys):
    evidence.build_chunks(DATASET_ID, MOCK_MODEL)
    rc = cli.main(
        [
            "search",
            DATASET_ID,
            "标签贴错",
            "--mode",
            "phrase",
            "--k",
            "5",
            "--embedding-model",
            MOCK_MODEL,
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    result = json.loads(captured.out)
    assert result["mode"] == "phrase"
    assert result["disclaimer"] == "Top-k 结果为相关样本，不代表完整统计。"
    assert len(result["results"]) == 5


def test_cli_search_vector_without_vectors_reports_not_ready(mock_embedder, capsys):
    evidence.build_chunks(DATASET_ID, MOCK_MODEL)
    rc = cli.main(
        [
            "search",
            DATASET_ID,
            "标签贴错",
            "--mode",
            "vector",
            "--embedding-model",
            MOCK_MODEL,
        ]
    )
    captured = capsys.readouterr()
    assert rc == 2
    result = json.loads(captured.err)
    assert "索引未就绪" in result["error"]
    assert result["index_status"]["chunks"] == 218
    assert result["index_status"]["vector_ready"] == 0


def test_cli_index_default_with_mock_embedder(mock_embedder, capsys):
    rc = cli.main(["index", DATASET_ID, "--embedding-model", MOCK_MODEL])
    captured = capsys.readouterr()
    assert rc == 0
    result = json.loads(captured.out)
    assert result["status"] == "ready"
    assert result["chunks"] == 218
    assert result["vector_ready"] == 218


def test_cli_index_embedding_failure_keeps_chunks(monkeypatch, capsys):
    def _failing_embedder(model=None):
        def _fail(_text):
            raise retrieval.RetrievalError("模型服务不可用")
        return _fail, MOCK_MODEL

    monkeypatch.setattr(retrieval, "get_embedder", _failing_embedder)
    rc = cli.main(["index", DATASET_ID, "--embedding-model", MOCK_MODEL])
    captured = capsys.readouterr()
    # stdout has the chunk status; stderr reports vector failure
    assert rc == 2
    stdout = json.loads(captured.out)
    assert stdout["status"] != "ready"
    assert stdout["chunks"] == 218
    assert stdout["vector_ready"] < stdout["chunks"]
    stderr = json.loads(captured.err)
    assert "chunk 已就绪、向量未建立" in stderr["error"]
