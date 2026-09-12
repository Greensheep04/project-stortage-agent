"""T-006a A2：过滤先于排名、时间/范围边界、分池与父文档回读（D10）。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from pi_market import corpus, corpus_retrieval, evidence
from test_corpus_common import (
    DATASET_ID,
    REAL_CORPUS_DIR,
    case,
    drop_corpus,
    event,
    make_corpus_dir,
    sop,
)

TZ = ZoneInfo("Asia/Shanghai")
CID_REAL = "t004-full-v2"


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=TZ)


@pytest.fixture(scope="module")
def real_corpus():
    corpus.import_corpus(CID_REAL, DATASET_ID, files_dir=REAL_CORPUS_DIR)
    corpus.index_corpus(CID_REAL, "mock-v1", no_embedding=True)
    return CID_REAL


def _ids(result, doc_type):
    return [r["doc_id"] for r in result["pools"][doc_type]]


def _search(cid, query, **kwargs):
    kwargs.setdefault("mode", "phrase")
    kwargs.setdefault("k", 10)
    return corpus_retrieval.search_corpus(DATASET_ID, cid, query, **kwargs)


def test_backfill_visibility(real_corpus):
    early = _search(
        real_corpus,
        "待检 破损 补录",
        doc_types=["event"],
        as_of=dt("2026-09-03T12:00:00+08:00"),
        order_id="SO-1034",
        line_id="002",
    )
    assert "EVX-0006" not in _ids(early, "event")
    late = _search(
        real_corpus,
        "待检 破损 补录",
        doc_types=["event"],
        as_of=dt("2026-09-09T20:00:00+08:00"),
        order_id="SO-1034",
        line_id="002",
    )
    assert "EVX-0006" in _ids(late, "event")


def test_case_closed_time(real_corpus):
    early = _search(
        real_corpus,
        "帆布袋 受潮 复核",
        doc_types=["case"],
        as_of=dt("2026-07-01T12:00:00+08:00"),
    )
    assert "CASE-0013" not in _ids(early, "case")
    late = _search(
        real_corpus,
        "帆布袋 受潮 复核",
        doc_types=["case"],
        as_of=dt("2026-07-07T12:00:00+08:00"),
    )
    assert "CASE-0013" in _ids(late, "case")


def test_sop_publish_effective_expiry(real_corpus):
    before_effective = _search(
        real_corpus,
        "包装异常 复核 交接",
        doc_types=["sop"],
        as_of=dt("2026-06-26T12:00:00+08:00"),
    )
    assert "SOP-PACK-v1" not in _ids(before_effective, "sop")
    assert "SOP-PACK-v2" not in _ids(before_effective, "sop")

    v1_window = _search(
        real_corpus,
        "包装异常 复核 交接",
        doc_types=["sop"],
        as_of=dt("2026-07-15T12:00:00+08:00"),
    )
    assert "SOP-PACK-v1" in _ids(v1_window, "sop")
    assert "SOP-PACK-v2" not in _ids(v1_window, "sop")

    v2_window = _search(
        real_corpus,
        "包装异常 复核 交接",
        doc_types=["sop"],
        as_of=dt("2026-09-06T12:00:00+08:00"),
    )
    assert "SOP-PACK-v2" in _ids(v2_window, "sop")
    assert "SOP-PACK-v1" not in _ids(v2_window, "sop")


def test_published_but_not_yet_effective(tmp_path):
    cid = "t006a-test-publish-window"
    drop_corpus(cid)
    make_corpus_dir(
        tmp_path,
        sops=[
            sop(
                recorded="2026-09-06T09:00:00+08:00",
                effective_from="2026-09-08T00:00:00+08:00",
                effective_to=None,
            )
        ],
    )
    corpus.import_corpus(cid, DATASET_ID, files_dir=tmp_path)
    corpus.index_corpus(cid, "mock-v1", no_embedding=True)
    result = _search(cid, "包装异常 复核", doc_types=["sop"], as_of=dt("2026-09-07T12:00:00+08:00"))
    assert _ids(result, "sop") == []


def test_scope_null_general_and_other_warehouse(tmp_path):
    cid = "t006a-test-scope"
    drop_corpus(cid)
    make_corpus_dir(
        tmp_path,
        cases=[
            case(doc_id="CASE-ANY", warehouse=None, sku=None),
            case(doc_id="CASE-WH2", warehouse="WH-02", sku=None),
        ],
    )
    corpus.import_corpus(cid, DATASET_ID, files_dir=tmp_path)
    corpus.index_corpus(cid, "mock-v1", no_embedding=True)

    wh1 = _search(cid, "包装受潮 复核", doc_types=["case"], warehouse="WH-01")
    assert _ids(wh1, "case") == ["CASE-ANY"]

    wh2 = _search(cid, "包装受潮 复核", doc_types=["case"], warehouse="WH-02")
    assert set(_ids(wh2, "case")) == {"CASE-ANY", "CASE-WH2"}


def test_scope_sku_exact_for_events_general_for_cases(tmp_path):
    cid = "t006a-test-sku"
    drop_corpus(cid)
    make_corpus_dir(
        tmp_path,
        events=[
            event(doc_id="EVX-BAG", sku="BAG-KH"),
            event(doc_id="EVX-MUG", sku="MUG-BL"),
        ],
        cases=[case(doc_id="CASE-ANY", sku=None), case(doc_id="CASE-MUG", sku="MUG-BL")],
    )
    corpus.import_corpus(cid, DATASET_ID, files_dir=tmp_path)
    corpus.index_corpus(cid, "mock-v1", no_embedding=True)

    events = _search(cid, "包装复核", doc_types=["event"], sku="BAG-KH")
    assert _ids(events, "event") == ["EVX-BAG"]

    cases = _search(cid, "包装受潮 复核", doc_types=["case"], sku="BAG-KH")
    assert _ids(cases, "case") == ["CASE-ANY"]


def test_same_order_different_line(tmp_path):
    cid = "t006a-test-line"
    drop_corpus(cid)
    make_corpus_dir(
        tmp_path,
        events=[
            event(doc_id="EVX-L1", order_id="SO-1003", line_id="001"),
            event(doc_id="EVX-L2", order_id="SO-1003", line_id="002"),
        ],
    )
    corpus.import_corpus(cid, DATASET_ID, files_dir=tmp_path)
    corpus.index_corpus(cid, "mock-v1", no_embedding=True)

    line1 = _search(cid, "包装复核", doc_types=["event"], order_id="SO-1003", line_id="001")
    assert _ids(line1, "event") == ["EVX-L1"]
    both = _search(cid, "包装复核", doc_types=["event"], order_id="SO-1003")
    assert set(_ids(both, "event")) == {"EVX-L1", "EVX-L2"}


def test_filter_before_topk(tmp_path):
    cid = "t006a-test-filter-first"
    drop_corpus(cid)
    invisible = [
        event(
            doc_id=f"EVX-H{i:02d}",
            text="包装复核",
            recorded="2026-09-06T10:00:00+08:00",
            occurred="2026-09-06T09:50:00+08:00",
        )
        for i in range(30)
    ]
    visible = event(doc_id="EVX-VIS", text="包装复核记录补充说明", recorded="2026-09-05T10:00:00+08:00")
    make_corpus_dir(tmp_path, events=[*invisible, visible])
    corpus.import_corpus(cid, DATASET_ID, files_dir=tmp_path)
    corpus.index_corpus(cid, "mock-v1", no_embedding=True)

    as_of = dt("2026-09-05T12:00:00+08:00")
    top1 = _search(cid, "包装复核", doc_types=["event"], k=1, as_of=as_of)
    assert _ids(top1, "event") == ["EVX-VIS"]

    top10 = _search(cid, "包装复核", doc_types=["event"], k=10, as_of=as_of)
    assert _ids(top10, "event") == ["EVX-VIS"]


def test_pools_separate(tmp_path):
    cid = "t006a-test-pools"
    drop_corpus(cid)
    make_corpus_dir(
        tmp_path,
        cases=[case(text="包装复核经验记录")],
        sops=[sop(text="包装复核步骤说明", effective_to=None)],
    )
    corpus.import_corpus(cid, DATASET_ID, files_dir=tmp_path)
    corpus.index_corpus(cid, "mock-v1", no_embedding=True)
    result = _search(cid, "包装复核", doc_types=["case", "sop"])
    assert set(result["pools"]) == {"case", "sop"}
    assert all(r["doc_type"] == "case" for r in result["pools"]["case"])
    assert all(r["doc_type"] == "sop" for r in result["pools"]["sop"])
    assert _ids(result, "case") == ["CASE-T1"]
    assert _ids(result, "sop") == ["SOP-T-v1"]


def test_parent_readback_rechecks(tmp_path):
    cid = "t006a-test-readback"
    drop_corpus(cid)
    make_corpus_dir(
        tmp_path,
        events=[
            event(
                doc_id="EVX-BF",
                recorded="2026-09-08T20:02:00+08:00",
                occurred="2026-09-02T10:23:00+08:00",
            )
        ],
    )
    corpus.import_corpus(cid, DATASET_ID, files_dir=tmp_path)

    early = dt("2026-09-03T12:00:00+08:00")
    with pytest.raises(corpus.CorpusError):
        corpus_retrieval.read_corpus_doc(cid, "EVX-BF", as_of=early)

    late = dt("2026-09-09T20:00:00+08:00")
    doc = corpus_retrieval.read_corpus_doc(cid, "EVX-BF", as_of=late)
    assert doc["sections"][0]["text"] == "包装复核记录"

    with pytest.raises(corpus.CorpusError):
        corpus_retrieval.read_corpus_doc(cid, "EVX-BF", as_of=late, order_id="SO-OTHER")


def test_rrf_mock_vectors_and_stability(tmp_path):
    cid = "t006a-test-rrf"
    drop_corpus(cid)
    make_corpus_dir(tmp_path, events=[event()], cases=[case()], sops=[sop()])
    corpus.import_corpus(cid, DATASET_ID, files_dir=tmp_path)
    index = corpus.index_corpus(cid, "mock-v1", embed_fn=evidence.mock_embedder())
    assert index["status"] == "ready"
    assert index["vector_ready"] == 3

    kwargs = dict(
        mode="rrf",
        embed_fn=evidence.mock_embedder(),
        embedding_model="mock-v1",
        doc_types=["event", "case", "sop"],
    )
    first = corpus_retrieval.search_corpus(DATASET_ID, cid, "包装复核", **kwargs)
    second = corpus_retrieval.search_corpus(DATASET_ID, cid, "包装复核", **kwargs)
    assert first["pools"] == second["pools"]
    assert len(first["pools"]["event"]) == 1


def test_unindexed_and_unknown_corpus(tmp_path):
    cid = "t006a-test-noindex"
    drop_corpus(cid)
    make_corpus_dir(tmp_path, events=[event()])
    corpus.import_corpus(cid, DATASET_ID, files_dir=tmp_path)
    with pytest.raises(corpus_retrieval.CorpusIndexNotReadyError):
        _search(cid, "包装复核", doc_types=["event"])
    with pytest.raises(corpus_retrieval.CorpusIndexNotReadyError):
        corpus_retrieval.search_corpus(
            DATASET_ID,
            cid,
            "包装复核",
            mode="vector",
            embed_fn=evidence.mock_embedder(),
            embedding_model="mock-v1",
        )
    with pytest.raises(corpus.CorpusError):
        _search("t006a-not-exist", "包装复核")
