"""T-007b 第四组（rrf+rerank）顺序映射、失败不静默回退与集成选项。"""

import json

import pytest

from pi_market import corpus, corpus_eval, corpus_retrieval, evidence
from pi_market.explain import GenerationClient
from pi_market.investigate import investigate
from pi_market.retrieval import RetrievalError
from test_corpus_common import DATASET_ID, REAL_CORPUS_DIR, drop_corpus, event, make_corpus_dir, sop


def _base(docs, query="q"):
    parents = [
        {
            "doc_id": doc_id,
            "content": f"标题\n节\n{doc_id}",
            "rank": i + 1,
        }
        for i, doc_id in enumerate(docs)
    ]
    return {"query": query, "parents": parents}


def test_rerank_parents_maps_index_back_to_candidates():
    base = _base(["A", "B", "C"])

    def rerank_fn(query, documents):
        assert documents == ["标题\n节\nA", "标题\n节\nB", "标题\n节\nC"]
        return {
            "results": [
                {"index": 2, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.5},
                {"index": 1, "relevance_score": 0.3},
            ],
            "usage": {"total_tokens": 7},
        }

    order, meta = corpus_retrieval._rerank_parents(base, rerank_fn)
    assert [p["doc_id"] for p in order] == ["C", "A", "B"]
    assert order[0]["rerank_score"] == 0.9
    assert order[0]["rrf_rank"] == 3
    assert [p["rank"] for p in order] == [1, 2, 3]
    assert meta == {"called": True, "candidates": 3, "returned": 3, "usage": {"total_tokens": 7}}


def test_rerank_parents_missing_result_fails():
    base = _base(["A", "B"])

    def rerank_fn(query, documents):
        return {"results": [{"index": 0, "relevance_score": 0.5}], "usage": None}

    with pytest.raises(RetrievalError):
        corpus_retrieval._rerank_parents(base, rerank_fn)


def test_rerank_parents_requires_rerank_fn():
    with pytest.raises(RetrievalError):
        corpus_retrieval._rerank_parents(_base(["A"]), None)


def test_rerank_parents_empty_pool_no_call():
    order, meta = corpus_retrieval._rerank_parents(
        {"query": "q", "parents": []}, lambda q, d: pytest.fail("空池不应调用")
    )
    assert order == []
    assert meta["called"] is False


def test_run_group_rrf_rerank_failure_counts_as_zero():
    queries = [
        {
            "qid": "Q1",
            "query": "query-Q1",
            "pool": "case",
            "as_of": "2026-09-09T23:59:59+08:00",
            "scenario_group": "test",
            "polarity": "positive",
            "labels": [{"doc_id": "A", "section_id": "s", "grade": 2, "reason": "r"}],
        }
    ]

    def rank_fn(dataset_id, corpus_id, query, doc_type, **kwargs):
        if kwargs.get("mode") == "rrf+rerank":
            raise RetrievalError("rerank 失败不静默回退")
        return {"parents": [{"doc_id": "A"}]}

    group = corpus_eval.run_group(
        queries, "rrf+rerank", "ds", "corpus", rank_fn=rank_fn, rerank_fn=lambda q, d: None
    )
    assert group["status"] == "failed"
    assert group["positive"]["overall"]["recall@10"] == 0.0


def test_rerank_group_metrics_and_candidate_recall():
    queries = [
        {
            "qid": "Q1",
            "query": "query-Q1",
            "pool": "case",
            "as_of": "2026-09-09T23:59:59+08:00",
            "scenario_group": "test",
            "polarity": "positive",
            "labels": [
                {"doc_id": "A", "section_id": "s", "grade": 2, "reason": "r"},
                {"doc_id": "B", "section_id": "s", "grade": 2, "reason": "r"},
                {"doc_id": "X", "section_id": "s", "grade": 0, "reason": "r"},
            ],
        }
    ]

    def rank_fn(dataset_id, corpus_id, query, doc_type, **kwargs):
        assert kwargs["mode"] == "rrf+rerank"
        assert kwargs["rerank_fn"] is not None
        return {"parents": [{"doc_id": "B"}, {"doc_id": "X"}, {"doc_id": "A"}]}

    group = corpus_eval.run_group(
        queries, "rrf+rerank", "ds", "corpus", rank_fn=rank_fn, rerank_fn=lambda q, d: None
    )
    assert group["status"] == "ok"
    assert group["positive"]["overall"]["recall@10"] == 1.0
    assert group["positive"]["overall"]["candidate_recall@50"] == 1.0
    assert group["per_query"][0]["rerank"] is None


def _prepare_corpus(tmp_path, cid):
    drop_corpus(cid)
    make_corpus_dir(
        tmp_path,
        events=[event(doc_id="EVX-R1", order_id="SO-1003", line_id="001")],
        cases=[],
        sops=[sop(doc_id="SOP-R-v1", policy_id="SOP-R", effective_to=None)],
    )
    corpus.import_corpus(cid, DATASET_ID, files_dir=tmp_path)
    corpus.index_corpus(cid, "mock-v1", embed_fn=evidence.mock_embedder())
    return cid


def test_search_corpus_ranker_rerank_returns_parent_level(tmp_path):
    cid = _prepare_corpus(tmp_path, "t007b-test-rerank-search")

    def rerank_fn(query, documents):
        # 反转候选顺序，验证重排确实生效
        return {
            "results": [
                {"index": i, "relevance_score": float(i)}
                for i in range(len(documents))
            ][::-1],
            "usage": {"total_tokens": 5},
        }

    result = corpus_retrieval.search_corpus(
        DATASET_ID,
        cid,
        "包装复核",
        doc_types=["sop"],
        k=5,
        scheme="rrf+rerank",
        embed_fn=evidence.mock_embedder(),
        embedding_model="mock-v1",
        rerank_fn=rerank_fn,
    )
    assert result["scheme"] == "rrf+rerank"
    assert result["pools"]["sop"]
    assert "rerank_score" in result["pools"]["sop"][0]

    default = corpus_retrieval.search_corpus(
        DATASET_ID,
        cid,
        "包装复核",
        doc_types=["sop"],
        k=5,
        embed_fn=evidence.mock_embedder(),
        embedding_model="mock-v1",
    )
    assert default["scheme"] is None
    assert "rerank_score" not in default["pools"]["sop"][0]


def test_search_corpus_rejects_unknown_scheme(tmp_path):
    cid = _prepare_corpus(tmp_path, "t007b-test-rerank-mode")
    with pytest.raises(RetrievalError):
        corpus_retrieval.search_corpus(DATASET_ID, cid, "包装复核", scheme="no-such-scheme")


class _MockGeneration(GenerationClient):
    def generate(self, prompt: str) -> str:
        return json.dumps(
            {"explanation": "ok", "used_evidence_ids": [], "open_questions": []},
            ensure_ascii=False,
        )


def test_investigate_ranker_rerank_records_usage(tmp_path):
    cid = _prepare_corpus(tmp_path, "t007b-test-rerank-investigate")

    calls = {"n": 0}

    def rerank_fn(query, documents):
        calls["n"] += 1
        return {
            "results": [
                {"index": i, "relevance_score": 0.5} for i in range(len(documents))
            ],
            "usage": {"total_tokens": 11},
        }

    result = investigate(
        DATASET_ID,
        "SO-1003",
        line_id="001",
        as_of="2026-09-09T20:00:00+08:00",
        corpus=cid,
        generation_client=_MockGeneration(),
        search_mode="rrf",
        embed_fn=evidence.mock_embedder(),
        embedding_model="mock-v1",
        scheme="rrf+rerank",
        rerank_fn=rerank_fn,
    )
    assert result["call_record"]["scheme"] == "rrf+rerank"
    assert calls["n"] >= 1
    # 注入 rerank_fn 时无外部调用可计量，usage 记 None（真实客户端路径另行记录）
    assert result["call_record"]["rerank"] is None


def test_explicit_scheme_business_matches_eval_ranking():
    """R1 回归：TEST-C01 输入下，业务显式 phrase 与评测返回同一父文档序列。"""
    corpus.import_corpus("t004-full-v2", DATASET_ID, files_dir=REAL_CORPUS_DIR)
    corpus.index_corpus("t004-full-v2", "mock-v1", no_embedding=True)
    query = "多货位分布的商品，拣货路径只走了一个货位导致数量不足，应该怎么补齐？"
    as_of = "2026-09-09T23:59:59+08:00"

    eval_ranked = corpus_retrieval.rank_parents(
        DATASET_ID,
        "t004-full-v2",
        query,
        "case",
        as_of=as_of,
        mode="phrase",
        candidate_depth=100,
        parent_top=5,
        warehouse="WH-01",
        sku="BAG-KH",
    )
    business = corpus_retrieval.search_corpus(
        DATASET_ID,
        "t004-full-v2",
        query,
        doc_types=["case"],
        k=5,
        scheme="phrase",
        as_of=as_of,
        warehouse="WH-01",
        sku="BAG-KH",
    )
    eval_ids = [p["doc_id"] for p in eval_ranked["parents"]]
    business_ids = [r["doc_id"] for r in business["pools"]["case"]]
    assert eval_ids == business_ids
    assert len(set(business_ids)) == 5


def test_legacy_default_path_unchanged(tmp_path):
    """旧默认（不传 scheme）仍走 chunk 级路径，结果与显式方案解耦。"""
    cid = _prepare_corpus(tmp_path, "t007c-test-legacy-default")
    default = corpus_retrieval.search_corpus(
        DATASET_ID, cid, "包装复核", doc_types=["sop"], k=5, mode="phrase"
    )
    assert default["scheme"] is None
    assert default["pools"]["sop"]
