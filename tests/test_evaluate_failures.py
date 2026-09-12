import pytest

from pi_market import evaluate, retrieval

READY = {"chunks": 10, "vector_ready": 10, "chunk_ready": True, "vector_ready_all": True}

QUERIES = [
    {"qid": "p1", "query": "正例一", "relevant_parent_ids": ["EV-1"]},
    {"qid": "p2", "query": "正例二", "relevant_parent_ids": ["EV-2", "EV-3"]},
    {"qid": "n1", "query": "无支持一", "relevant_parent_ids": []},
    {"qid": "n2", "query": "无支持二", "relevant_parent_ids": []},
]


@pytest.fixture(autouse=True)
def _ready_index(monkeypatch):
    monkeypatch.setattr(
        evaluate.evidence, "get_index_status", lambda *args, **kwargs: dict(READY)
    )


def _patch_search(monkeypatch, results_by_query=None, failures=None):
    results_by_query = results_by_query or {}
    failures = failures or {}

    def _search(
        dataset_id,
        query,
        mode="rrf",
        k=10,
        as_of=None,
        embed_fn=None,
        embedding_model=None,
    ):
        if query in failures:
            raise failures[query]
        parents = results_by_query.get(query, [])
        return {
            "results": [{"parent_id": p} for p in parents],
            "disclaimer": "Top-k 结果为相关样本，不代表完整统计。",
        }

    monkeypatch.setattr(retrieval, "search", _search)


def _run():
    return evaluate.evaluate_mode("ds-test", QUERIES, "phrase", 10, embedding_model="m")


def test_all_success_is_ok(monkeypatch):
    _patch_search(
        monkeypatch,
        {
            "正例一": ["EV-1"],
            "正例二": ["EV-2", "EV-3"],
            "无支持一": ["TX-1"],
            "无支持二": [],
        },
    )
    report = _run()
    assert report["status"] == "ok"
    assert report["total_queries"] == 4
    assert report["success_count"] == 4
    assert report["failure_count"] == 0
    assert report["positive"]["total"] == 2
    assert report["positive"]["success"] == 2
    assert report["macro_recall@k"] == 1.0
    assert report["errors"] == []


def test_positive_failure_counts_zero_and_status_partial(monkeypatch):
    _patch_search(
        monkeypatch,
        {"正例一": ["EV-1"], "无支持一": [], "无支持二": []},
        failures={"正例二": RuntimeError("服务异常")},
    )
    report = _run()
    assert report["status"] == "partial"
    assert report["failure_count"] == 1
    assert report["positive"]["failed"] == 1
    # 失败正例按 0 计入完整正例均值分母（1.0 与 0.0 的均值）。
    assert report["macro_recall@k"] == 0.5
    assert report["errors"][0]["qid"] == "p2"


def test_all_failed(monkeypatch):
    _patch_search(monkeypatch, failures={q["query"]: RuntimeError("服务异常") for q in QUERIES})
    report = _run()
    assert report["status"] == "failed"
    assert report["success_count"] == 0
    assert report["failure_count"] == 4
    assert report["macro_recall@k"] == 0.0
    assert len(report["errors"]) == 4


def test_precheck_failure_is_failed_with_reason(monkeypatch):
    monkeypatch.setattr(
        evaluate.evidence,
        "get_index_status",
        lambda *args, **kwargs: {
            "chunks": 0,
            "vector_ready": 0,
            "chunk_ready": False,
            "vector_ready_all": False,
        },
    )
    report = _run()
    assert report["status"] == "failed"
    assert report["precheck"]["status"] == "index_not_ready"
    assert report["failure_count"] == 4
    assert len(report["errors"]) == 4
    # 含正例时，预检查失败的正例按零计入完整正例集均值。
    assert report["macro_recall@k"] == 0.0


def test_precheck_failure_without_positives_keeps_recall_null(monkeypatch):
    monkeypatch.setattr(
        evaluate.evidence,
        "get_index_status",
        lambda *args, **kwargs: {
            "chunks": 0,
            "vector_ready": 0,
            "chunk_ready": False,
            "vector_ready_all": False,
        },
    )
    queries = [q for q in QUERIES if not q["relevant_parent_ids"]]
    report = evaluate.evaluate_mode("ds-test", queries, "phrase", 10, embedding_model="m")
    assert report["status"] == "failed"
    assert report["positive"]["total"] == 0
    assert report["macro_recall@k"] is None
    assert report["precheck"]["status"] == "index_not_ready"


def test_unsupported_failure_and_no_results_are_separate(monkeypatch):
    _patch_search(
        monkeypatch,
        {"正例一": ["EV-1"], "正例二": ["EV-2", "EV-3"], "无支持二": []},
        failures={"无支持一": RuntimeError("服务异常")},
    )
    report = _run()
    assert report["status"] == "partial"
    assert report["unsupported"]["failed"] == 1
    assert report["unsupported"]["success"] == 1
    assert report["unsupported"]["success_no_results"] == 1
    # 无支持查询不进入 Recall 分母。
    assert report["macro_recall@k"] == 1.0
    failed_items = [u for u in report["unsupported_queries"] if u["status"] == "error"]
    assert len(failed_items) == 1
    assert failed_items[0]["qid"] == "n1"


def test_cli_evaluate_nonzero_exit_on_partial(monkeypatch, tmp_path):
    from pi_market import cli

    report = {
        "dataset_id": "ds-x",
        "numeric": {},
        "retrieval": {"status": "partial", "modes": [{"mode": "phrase", "status": "partial"}]},
    }
    monkeypatch.setattr(evaluate, "run_evaluation", lambda *args, **kwargs: report)

    rc = cli.main(["evaluate", "ds-x", "a.json", "q.jsonl", "--output", str(tmp_path / "r.json")])
    assert rc == 2

    report["retrieval"]["modes"][0]["status"] = "ok"
    rc = cli.main(["evaluate", "ds-x", "a.json", "q.jsonl", "--output", str(tmp_path / "r2.json")])
    assert rc == 0
