"""T-007a 评测计分与失败回归（人工可算，mock 只用于错误与指标回归）。"""

import pytest

from pi_market import corpus_eval


def _q(qid, pool="case", polarity="positive", labels=None, scenario="test"):
    return {
        "qid": qid,
        "query": f"query-{qid}",
        "pool": pool,
        "as_of": "2026-09-09T23:59:59+08:00",
        "scenario_group": scenario,
        "polarity": polarity,
        "labels": labels or [],
    }


def _l(doc, grade, reason="r"):
    return {"doc_id": doc, "section_id": "s", "grade": grade, "reason": reason}


def test_score_query_hand_computable():
    labels = [_l("D1", 2), _l("D2", 2), _l("D3", 2), _l("D4", 2), _l("D5", 1)]
    ranked = ["D1", "D5", "D2", "X", "D3"] + [f"X{i}" for i in range(6)] + ["D4"]
    scores = corpus_eval.score_query(labels, ranked)
    assert scores["grade2_total"] == 4 and scores["grade2_hits"] == 3
    assert scores["recall@10"] == 0.75
    assert scores["mrr@5"] == 1.0
    assert scores["ndcg@5"] == pytest.approx(6.2915 / 8.0717, abs=1e-3)


def test_score_query_zero_hit():
    labels = [_l("D1", 2), _l("D2", 1)]
    scores = corpus_eval.score_query(labels, ["X", "Y"])
    assert scores["recall@10"] == 0.0
    assert scores["mrr@5"] == 0.0
    assert scores["ndcg@5"] == 0.0


def test_validate_query_set_constraints():
    good = _q("Q1", labels=[_l("D1", 2)])
    assert corpus_eval.validate_query_set([good]) == []
    bad = [
        _q("Q2", labels=[_l("D1", 1)]),
        _q("Q3", labels=[_l(f"D{i}", 2) for i in range(11)]),
        _q("Q4", polarity="negative", labels=[_l("D1", 2)]),
        _q("Q5", polarity="negative", labels=[]),
    ]
    problems = corpus_eval.validate_query_set(bad)
    assert any("缺等级 2" in p for p in problems)
    assert any("超过 10" in p for p in problems)
    assert any("不应有等级 2" in p for p in problems)
    assert any("不适用理由" in p for p in problems)


def _fake_rank(rankings, fail_qids=()):
    def rank(dataset_id, corpus_id, query, doc_type, **kwargs):
        qid = query.replace("query-", "")
        if qid in fail_qids:
            raise RuntimeError(f"boom-{qid}")
        return {"parents": [{"doc_id": d} for d in rankings.get(qid, [])]}

    return rank


def test_run_group_partial_and_failed():
    queries = [_q("Q1", labels=[_l("A", 2)]), _q("Q2", labels=[_l("B", 2)])]
    partial = corpus_eval.run_group(
        queries,
        "phrase",
        "ds",
        "corpus",
        rank_fn=_fake_rank({"Q1": ["A"], "Q2": []}, fail_qids={"Q2"}),
    )
    assert partial["status"] == "partial"
    assert partial["positive"]["overall"]["recall@10"] == 0.5
    assert partial["errors"][0]["qid"] == "Q2"

    failed = corpus_eval.run_group(
        queries, "phrase", "ds", "corpus", rank_fn=_fake_rank({}, fail_qids={"Q1", "Q2"})
    )
    assert failed["status"] == "failed"
    assert failed["positive"]["overall"]["recall@10"] == 0.0


def test_run_group_pure_negative():
    queries = [
        _q("N1", polarity="negative", labels=[_l("X", 0), _l("Y", 0)], scenario="negative")
    ]
    queries[0]["no_answer_reason"] = "无适用资料"
    result = corpus_eval.run_group(
        queries, "phrase", "ds", "corpus", rank_fn=_fake_rank({"N1": ["X", "Y"]})
    )
    assert result["status"] == "ok"
    assert result["positive"]["overall"]["recall@10"] is None
    assert result["negative"]["returned"][0]["returned_doc_ids"] == ["X", "Y"]


def test_run_groups_macro_by_pool():
    queries = [
        _q("C1", pool="case", labels=[_l("A", 2)]),
        _q("C2", pool="case", labels=[_l("B", 2)]),
        _q("S1", pool="sop", labels=[_l("C", 2)]),
    ]
    rankings = {"C1": ["A"], "C2": [], "S1": ["C"]}
    report = corpus_eval.run_groups(
        queries, ["phrase"], "ds", "corpus", rank_fn=_fake_rank(rankings)
    )
    group = report["groups"]["phrase"]
    assert group["positive"]["case"]["recall@10"] == 0.5
    assert group["positive"]["sop"]["recall@10"] == 1.0
    assert group["positive"]["overall"]["recall@10"] == pytest.approx(2 / 3, abs=1e-3)


def test_query_vector_cache(tmp_path):
    class _Resp:
        def __init__(self, vec):
            self.data = [type("D", (), {"embedding": vec})()]
            self.usage = type("U", (), {"prompt_tokens": 3, "total_tokens": 3})()

    class _Client:
        def __init__(self):
            self.calls = 0
            self.embeddings = self

        def create(self, input, model):
            self.calls += 1
            return _Resp([float(self.calls)] * 4)

    client = _Client()
    cache = corpus_eval.QueryVectorCache(tmp_path / "cache.json", "mock", client)
    first = cache.embed("hello")
    second = cache.embed("hello")
    assert first == second
    assert cache.hits == 1 and cache.misses == 1
    cache.save()
    cache2 = corpus_eval.QueryVectorCache(tmp_path / "cache.json", "mock", client)
    assert cache2.embed("hello") == first
    assert cache2.hits == 1 and client.calls == 1


def test_unjudged_candidate_refuses_scores():
    queries = [_q("Q1", labels=[_l("A", 2)])]
    group = corpus_eval.run_group(
        queries, "phrase", "ds", "corpus", rank_fn=_fake_rank({"Q1": ["A", "X"]})
    )
    assert group["status"] == "labels_incomplete"
    assert group["positive"]["overall"]["recall@10"] is None
    assert group["errors"][0]["unjudged_doc_ids"] == ["X"]
    assert group["per_query"][0]["unjudged_doc_ids"] == ["X"]


def test_rescore_report_uses_saved_rankings():
    queries = [_q("Q1", labels=[_l("A", 2), _l("X", 0)])]
    report = corpus_eval.run_groups(
        queries, ["phrase"], "ds", "corpus", rank_fn=_fake_rank({"Q1": ["A", "X"]})
    )
    # 收紧标签（X 改判等级 1）后离线重算，排名沿用保存值
    tightened = [_q("Q1", labels=[_l("A", 2), _l("X", 1)])]
    rescored = corpus_eval.rescore_report(report, tightened)
    entry = rescored["groups"]["phrase"]["per_query"][0]
    assert entry["candidate_doc_ids"] == ["A", "X"]
    assert entry["status"] == "ok"
    assert rescored["groups"]["phrase"]["status"] == "ok"
