"""T-006a A3：CLI 入口（import-corpus / index-corpus / search --corpus）。"""

import json

from pi_market import cli
from test_corpus_common import DATASET_ID, drop_corpus, event, make_corpus_dir, sop


def test_cli_import_index_search(tmp_path, capsys):
    cid = "t006a-test-cli"
    drop_corpus(cid)
    make_corpus_dir(tmp_path, events=[event()], sops=[sop(effective_to=None)])

    assert (
        cli.main(["import-corpus", cid, "--dataset-id", DATASET_ID, "--dir", str(tmp_path)]) == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ready"
    assert result["docs"] == {"event": 1, "case": 0, "sop": 1}

    assert cli.main(["index-corpus", cid, "--no-embedding"]) == 0
    indexed = json.loads(capsys.readouterr().out)
    assert indexed["chunks"] == 2

    assert (
        cli.main(
            ["search", DATASET_ID, "包装复核", "--corpus", cid, "--type", "sop", "--mode", "phrase"]
        )
        == 0
    )
    found = json.loads(capsys.readouterr().out)
    assert found["corpus_id"] == cid
    assert found["pools"]["sop"][0]["doc_id"] == "SOP-T-v1"
    assert set(found["pools"]) == {"sop"}


def test_cli_type_requires_corpus(capsys):
    assert cli.main(["search", DATASET_ID, "包装复核", "--type", "sop"]) == 1
    assert "--corpus" in capsys.readouterr().err


def test_cli_validation_failure_exit_2(tmp_path, capsys):
    cid = "t006a-test-cli-bad"
    drop_corpus(cid)
    make_corpus_dir(tmp_path, events=[event()])
    (tmp_path / "events.jsonl").write_text("{}\n", encoding="utf-8")
    assert (
        cli.main(["import-corpus", cid, "--dataset-id", DATASET_ID, "--dir", str(tmp_path)]) == 2
    )
    err = json.loads(capsys.readouterr().err)
    assert err["issues"]


def test_cli_corpus_index_not_ready_exit_2(tmp_path, capsys):
    cid = "t006a-test-cli-noindex"
    drop_corpus(cid)
    make_corpus_dir(tmp_path, events=[event()])
    cli.main(["import-corpus", cid, "--dataset-id", DATASET_ID, "--dir", str(tmp_path)])
    capsys.readouterr()
    assert (
        cli.main(["search", DATASET_ID, "包装复核", "--corpus", cid, "--mode", "phrase"]) == 2
    )
    err = json.loads(capsys.readouterr().err)
    assert err["index_status"]["chunks"] == 0
