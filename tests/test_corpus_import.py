"""T-006a A1：导入、校验、幂等与冲突（D9）。"""

import json

import pytest

from pi_market import corpus, db
from test_corpus_common import (
    DATASET_ID,
    REAL_CORPUS_DIR,
    XLSX_REL,
    case,
    drop_corpus,
    event,
    make_corpus_dir,
    other_flow_row,
    real_refs,
    sop,
)


def _counts(corpus_id):
    with db.admin_cursor() as cur:
        cur.execute(
            "SELECT doc_type, COUNT(*) FROM corpus_doc WHERE corpus_id = %s GROUP BY 1",
            (corpus_id,),
        )
        return dict(cur.fetchall())


def test_import_idempotent_and_readback(tmp_path):
    cid = "t006a-test-basic"
    drop_corpus(cid)
    make_corpus_dir(
        tmp_path,
        events=[event(text="待检区包装破损登记")],
        cases=[case()],
        sops=[sop()],
    )
    result = corpus.import_corpus(cid, DATASET_ID, files_dir=tmp_path)
    assert result["status"] == "ready"
    assert result["docs"] == {"event": 1, "case": 1, "sop": 1}
    assert result["sections"] == 3

    again = corpus.import_corpus(cid, DATASET_ID, files_dir=tmp_path)
    assert again["status"] == "reused"
    assert _counts(cid) == {"event": 1, "case": 1, "sop": 1}

    with db.admin_cursor() as cur:
        cur.execute(
            "SELECT jsonl_file, jsonl_row, raw FROM corpus_doc WHERE corpus_id = %s AND doc_id = %s",
            (cid, "EVX-T1"),
        )
        jsonl_file, jsonl_row, raw = cur.fetchone()
    assert jsonl_file == "events.jsonl"
    assert jsonl_row == 1
    assert raw["sections"][0]["text"] == "待检区包装破损登记"
    assert raw["scope"]["order_id"] == "SO-1003"


def test_index_builds_one_chunk_per_section(tmp_path):
    cid = "t006a-test-index"
    drop_corpus(cid)
    make_corpus_dir(tmp_path, events=[event(), event(doc_id="EVX-T2")], sops=[sop()])
    corpus.import_corpus(cid, DATASET_ID, files_dir=tmp_path)
    result = corpus.index_corpus(cid, "mock-v1", no_embedding=True)
    assert result["status"] == "chunk_ready"
    assert result["chunks"] == 3
    assert result["vector_ready"] == 0
    with db.admin_cursor() as cur:
        cur.execute(
            "SELECT content FROM corpus_chunk WHERE chunk_id = %s",
            (f"{cid}:EVX-T1:s1",),
        )
        (content,) = cur.fetchone()
    assert content == "SO-1003/001 事件记录\n记录\n包装复核记录"


def test_hash_mismatch_rejected_and_not_published(tmp_path):
    cid = "t006a-test-hash"
    drop_corpus(cid)
    make_corpus_dir(tmp_path, events=[event()])
    (tmp_path / "events.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(corpus.CorpusValidationError) as exc:
        corpus.import_corpus(cid, DATASET_ID, files_dir=tmp_path)
    assert any(i["field"] == "sha256" for i in exc.value.issues)
    with db.admin_cursor() as cur:
        cur.execute("SELECT status FROM corpus WHERE corpus_id = %s", (cid,))
        assert cur.fetchone()[0] == "failed"
        cur.execute("SELECT COUNT(*) FROM corpus_doc WHERE corpus_id = %s", (cid,))
        assert cur.fetchone()[0] == 0


def test_missing_field_rejected(tmp_path):
    cid = "t006a-test-field"
    drop_corpus(cid)
    bad = event()
    del bad["occurred_at"]
    make_corpus_dir(tmp_path, events=[bad])
    with pytest.raises(corpus.CorpusValidationError) as exc:
        corpus.import_corpus(cid, DATASET_ID, files_dir=tmp_path)
    assert any(i["field"] == "occurred_at" for i in exc.value.issues)


def test_duplicate_doc_id_rejected(tmp_path):
    cid = "t006a-test-dup"
    drop_corpus(cid)
    make_corpus_dir(tmp_path, events=[event(doc_id="DUP-1")], cases=[case(doc_id="DUP-1")])
    with pytest.raises(corpus.CorpusValidationError) as exc:
        corpus.import_corpus(cid, DATASET_ID, files_dir=tmp_path)
    assert any("重复" in i["message"] for i in exc.value.issues)


def test_snapshot_mismatch_rejected(tmp_path):
    cid = "t006a-test-snapshot"
    drop_corpus(cid)
    make_corpus_dir(tmp_path, events=[event()], snapshot="0" * 64)
    with pytest.raises(corpus.CorpusValidationError) as exc:
        corpus.import_corpus(cid, DATASET_ID, files_dir=tmp_path)
    assert any(i["field"] == "source_snapshot" for i in exc.value.issues)


def test_conflict_not_overwritten(tmp_path):
    cid = "t006a-test-conflict"
    drop_corpus(cid)
    make_corpus_dir(tmp_path, events=[event()])
    corpus.import_corpus(cid, DATASET_ID, files_dir=tmp_path)

    other = tmp_path / "v2"
    other.mkdir()
    make_corpus_dir(other, events=[event(text="不同内容")])
    with pytest.raises(corpus.CorpusConflictError):
        corpus.import_corpus(cid, DATASET_ID, files_dir=other)

    with db.admin_cursor() as cur:
        cur.execute(
            "SELECT raw->'sections'->0->>'text' FROM corpus_doc WHERE corpus_id = %s AND doc_id = %s",
            (cid, "EVX-T1"),
        )
        assert cur.fetchone()[0] == "包装复核记录"


def test_null_scope_preserved(tmp_path):
    cid = "t006a-test-null-scope"
    drop_corpus(cid)
    make_corpus_dir(tmp_path, cases=[case()], sops=[sop()])
    corpus.import_corpus(cid, DATASET_ID, files_dir=tmp_path)
    with db.admin_cursor() as cur:
        cur.execute(
            "SELECT scope_order_id, scope_line_id FROM corpus_doc"
            " WHERE corpus_id = %s AND doc_type IN ('case', 'sop')",
            (cid,),
        )
        assert cur.fetchall() == [(None, None), (None, None)]


def test_real_full_v2_import():
    cid = "t004-full-v2"
    drop_corpus(cid)
    result = corpus.import_corpus(cid, DATASET_ID, files_dir=REAL_CORPUS_DIR)
    assert result["status"] in {"ready", "reused"}
    assert result["docs"] == {"event": 700, "case": 280, "sop": 20}
    assert result["sections"] == 2320
    assert _counts(cid) == {"event": 700, "case": 280, "sop": 20}

    indexed = corpus.index_corpus(cid, "mock-v1", no_embedding=True)
    assert indexed["chunks"] == 2320

    status = corpus.corpus_status(cid)
    assert status["status"] == "ready"
    assert status["docs"] == {"event": 700, "case": 280, "sop": 20}
    assert status["chunks"] == 2320

    with db.admin_cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM corpus_doc WHERE corpus_id = %s", (cid,))
        assert cur.fetchone()[0] == 1000


def _import_expect_rejected(tmp_path, cid, expect_field):
    drop_corpus(cid)
    with pytest.raises(corpus.CorpusValidationError) as exc:
        corpus.import_corpus(cid, DATASET_ID, files_dir=tmp_path)
    assert any(expect_field in i["field"] for i in exc.value.issues), exc.value.issues
    with db.admin_cursor() as cur:
        cur.execute("SELECT status FROM corpus WHERE corpus_id = %s", (cid,))
        assert cur.fetchone()[0] == "failed"
        cur.execute("SELECT COUNT(*) FROM corpus_doc WHERE corpus_id = %s", (cid,))
        assert cur.fetchone()[0] == 0
    return exc.value.issues


def test_nonexistent_transaction_rejected(tmp_path):
    make_corpus_dir(tmp_path, events=[event(transaction_ids=["TX-NOT-EXIST"])])
    _import_expect_rejected(tmp_path, "t006c-test-tx", "transaction_ids[0]")


def test_nonexistent_source_row_rejected(tmp_path):
    make_corpus_dir(
        tmp_path,
        events=[
            event(
                transaction_ids=[],
                source_refs=[{"file": XLSX_REL, "sheet": "仓储流水", "row": 999999}],
            )
        ],
    )
    _import_expect_rejected(tmp_path, "t006c-test-row", "source_refs[0]")


def test_wrong_order_source_row_rejected(tmp_path):
    row = other_flow_row()
    make_corpus_dir(
        tmp_path,
        events=[
            event(
                transaction_ids=[],
                source_refs=[{"file": XLSX_REL, "sheet": "仓储流水", "row": row}],
            )
        ],
    )
    issues = _import_expect_rejected(tmp_path, "t006c-test-wrong-order", "source_refs[0]")
    assert any("不属于本单" in i["message"] for i in issues)


def test_future_source_time_rejected(tmp_path):
    _, flows, _ = real_refs()
    flow_row = flows[0][1]
    make_corpus_dir(
        tmp_path,
        events=[
            event(
                recorded="2026-09-01T00:00:00+08:00",
                occurred="2026-09-01T00:00:00+08:00",
                transaction_ids=[],
                source_refs=[{"file": XLSX_REL, "sheet": "仓储流水", "row": flow_row}],
            )
        ],
    )
    issues = _import_expect_rejected(tmp_path, "t006c-test-future", "source_refs[0]")
    assert any("晚于事件记录时间" in i["message"] for i in issues)


def test_sop_overlap_rejected(tmp_path):
    make_corpus_dir(
        tmp_path,
        sops=[
            sop(doc_id="SOP-OV-v1", policy_id="SOP-OV"),
            sop(
                doc_id="SOP-OV-v2",
                policy_id="SOP-OV",
                version="2",
                recorded="2026-07-01T09:00:00+08:00",
                effective_from="2026-07-15T00:00:00+08:00",
                effective_to=None,
                supersedes="SOP-OV-v1",
            ),
        ],
    )
    issues = _import_expect_rejected(tmp_path, "t006c-test-overlap", "effective_from")
    assert any("重叠" in i["message"] for i in issues)


def test_supersedes_self_reference_rejected(tmp_path):
    make_corpus_dir(
        tmp_path,
        sops=[sop(doc_id="SOP-SELF-v1", policy_id="SOP-SELF", supersedes="SOP-SELF-v1")],
    )
    issues = _import_expect_rejected(tmp_path, "t006c-test-self", "supersedes")
    assert any("自引用" in i["message"] for i in issues)


def test_supersedes_wrong_policy_rejected(tmp_path):
    make_corpus_dir(
        tmp_path,
        sops=[
            sop(doc_id="SOP-P1-v1", policy_id="SOP-P1"),
            sop(doc_id="SOP-P2-v1", policy_id="SOP-P2", supersedes="SOP-P1-v1"),
        ],
    )
    issues = _import_expect_rejected(tmp_path, "t006c-test-sup-policy", "supersedes")
    assert any("不一致" in i["message"] for i in issues)


def test_rule_ref_nonexistent_rejected(tmp_path):
    make_corpus_dir(tmp_path, cases=[case(rule_refs=["SOP-NOPE"])])
    issues = _import_expect_rejected(tmp_path, "t006c-test-ruleref", "rule_refs[0]")
    assert any("不存在" in i["message"] for i in issues)


def test_rule_ref_not_effective_rejected(tmp_path):
    make_corpus_dir(
        tmp_path,
        sops=[sop(doc_id="SOP-C-v1", policy_id="SOP-C")],
        cases=[
            case(
                rule_refs=["SOP-C-v1"],
                closed="2026-09-01T17:00:00+08:00",
                recorded="2026-09-02T10:00:00+08:00",
            )
        ],
    )
    issues = _import_expect_rejected(tmp_path, "t006c-test-ruleref-time", "rule_refs[0]")
    assert any("无效" in i["message"] for i in issues)


def test_legal_boundaries_pass(tmp_path):
    cid = "t006c-test-boundary"
    drop_corpus(cid)
    make_corpus_dir(
        tmp_path,
        sops=[
            sop(doc_id="SOP-A-v1", policy_id="SOP-A"),
            sop(
                doc_id="SOP-B-v1",
                policy_id="SOP-B",
                recorded="2026-07-01T09:00:00+08:00",
                effective_from="2026-07-15T00:00:00+08:00",
                effective_to=None,
            ),
        ],
        cases=[case(rule_refs=[])],
    )
    result = corpus.import_corpus(cid, DATASET_ID, files_dir=tmp_path)
    assert result["status"] == "ready"


def test_case_rule_scope_mismatch_rejected(tmp_path):
    make_corpus_dir(
        tmp_path,
        sops=[sop(doc_id="SOP-SC-v1", policy_id="SOP-SC", warehouse="WH-01")],
        cases=[case(warehouse="WH-OTHER", rule_refs=["SOP-SC-v1"])],
    )
    issues = _import_expect_rejected(tmp_path, "t006c-test-case-scope", "rule_refs[0]")
    assert any("范围不适配" in i["message"] for i in issues)


def test_case_null_scope_cannot_satisfy_concrete_sop(tmp_path):
    make_corpus_dir(
        tmp_path,
        sops=[sop(doc_id="SOP-SC-v1", policy_id="SOP-SC", warehouse="WH-01")],
        cases=[case(warehouse=None, rule_refs=["SOP-SC-v1"])],
    )
    issues = _import_expect_rejected(tmp_path, "t006c-test-case-null", "rule_refs[0]")
    assert any("范围不适配" in i["message"] for i in issues)


def test_general_sop_matches_any_case_scope(tmp_path):
    cid = "t006c-test-general-sop"
    drop_corpus(cid)
    make_corpus_dir(
        tmp_path,
        sops=[
            sop(
                doc_id="SOP-GEN-v1",
                policy_id="SOP-GEN",
                warehouse=None,
                sku=None,
                effective_to=None,
            )
        ],
        cases=[case(warehouse="WH-OTHER", sku="MUG-X", rule_refs=["SOP-GEN-v1"])],
    )
    result = corpus.import_corpus(cid, DATASET_ID, files_dir=tmp_path)
    assert result["status"] == "ready"


def test_supersedes_time_direction_rejected(tmp_path):
    make_corpus_dir(
        tmp_path,
        sops=[
            sop(doc_id="SOP-TD-v1", policy_id="SOP-TD", supersedes="SOP-TD-v2"),
            sop(
                doc_id="SOP-TD-v2",
                policy_id="SOP-TD",
                version="2",
                recorded="2026-07-25T09:00:00+08:00",
                effective_from="2026-08-01T00:00:00+08:00",
                effective_to=None,
            ),
        ],
    )
    issues = _import_expect_rejected(tmp_path, "t006c-test-sup-time", "supersedes")
    assert any("开始时间不早于" in i["message"] for i in issues)


def test_supersedes_target_not_ended_rejected(tmp_path):
    make_corpus_dir(
        tmp_path,
        sops=[
            sop(doc_id="SOP-NE-v1", policy_id="SOP-NE", effective_to=None),
            sop(
                doc_id="SOP-NE-v2",
                policy_id="SOP-NE",
                version="2",
                recorded="2026-07-25T09:00:00+08:00",
                effective_from="2026-08-01T00:00:00+08:00",
                effective_to=None,
                supersedes="SOP-NE-v1",
            ),
        ],
    )
    issues = _import_expect_rejected(tmp_path, "t006c-test-sup-open", "supersedes")
    assert any("未在当前版本开始前结束" in i["message"] for i in issues)


def test_supersedes_adjacent_chain_passes(tmp_path):
    cid = "t006c-test-sup-chain"
    drop_corpus(cid)
    make_corpus_dir(
        tmp_path,
        sops=[
            sop(doc_id="SOP-CH-v1", policy_id="SOP-CH"),
            sop(
                doc_id="SOP-CH-v2",
                policy_id="SOP-CH",
                version="2",
                recorded="2026-07-25T09:00:00+08:00",
                effective_from="2026-08-01T00:00:00+08:00",
                effective_to=None,
                supersedes="SOP-CH-v1",
            ),
        ],
    )
    result = corpus.import_corpus(cid, DATASET_ID, files_dir=tmp_path)
    assert result["status"] == "ready"
