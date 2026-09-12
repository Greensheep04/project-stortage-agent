import importlib.util
from pathlib import Path

import pytest

from pi_market import evaluate

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEV_QUERIES = PROJECT_ROOT / "tests" / "eval" / "dev_queries.jsonl"


def _load_script():
    path = PROJECT_ROOT / "scripts" / "eval_retrieval.py"
    spec = importlib.util.spec_from_file_location("eval_retrieval_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load_script()


def _fake_mode(status):
    def _inner(dataset_id, queries, mode, k, embedding_model=None, as_of=None):
        return {"mode": mode, "status": status, "k": k}

    return _inner


def test_eval_script_ok_exits_zero(monkeypatch, capsys, script):
    monkeypatch.setattr(evaluate, "evaluate_mode", _fake_mode("ok"))
    rc = script.main([str(DEV_QUERIES), "ds-x", "--mode", "phrase"])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"status": "ok"' in out
    assert '"modes"' in out


def test_eval_script_failed_exits_nonzero_with_report(monkeypatch, capsys, script):
    monkeypatch.setattr(evaluate, "evaluate_mode", _fake_mode("failed"))
    rc = script.main([str(DEV_QUERIES), "ds-x", "--mode", "phrase"])
    out = capsys.readouterr().out
    assert rc != 0
    assert '"status": "failed"' in out
    assert '"modes"' in out


def test_eval_script_partial_exits_nonzero(monkeypatch, capsys, script):
    def _mixed(dataset_id, queries, mode, k, embedding_model=None, as_of=None):
        return {"mode": mode, "status": "ok" if mode == "phrase" else "failed", "k": k}

    monkeypatch.setattr(evaluate, "evaluate_mode", _mixed)
    rc = script.main([str(DEV_QUERIES), "ds-x", "--mode", "phrase", "vector"])
    out = capsys.readouterr().out
    assert rc != 0
    assert '"status": "partial"' in out


def test_eval_script_failed_writes_report_file(monkeypatch, tmp_path, script):
    monkeypatch.setattr(evaluate, "evaluate_mode", _fake_mode("failed"))
    out_file = tmp_path / "report.json"
    rc = script.main(
        [str(DEV_QUERIES), "ds-x", "--mode", "phrase", "--output", str(out_file)]
    )
    assert rc != 0
    text = out_file.read_text(encoding="utf-8")
    assert '"status": "failed"' in text
    assert '"modes"' in text
