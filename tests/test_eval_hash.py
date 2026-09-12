import subprocess
import sys
import tempfile
from pathlib import Path

from pi_market import evidence

DATASET_ID = "ds-61edff98759f"
MOCK_MODEL = "test-mock-vec"


def test_eval_refuses_tampered_queryset():
    original = Path("tests/eval/dev_queries.jsonl").read_text(encoding="utf-8")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(original.replace("外包装", "外包装x"))
        tmp_path = Path(tmp.name)

    try:
        evidence.clear_index(DATASET_ID, MOCK_MODEL)
        evidence.build_chunks(DATASET_ID, MOCK_MODEL)
        result = subprocess.run(
            [
                sys.executable,
                "scripts/eval_retrieval.py",
                str(tmp_path),
                DATASET_ID,
                "--mode",
                "phrase",
                "--embedding-model",
                MOCK_MODEL,
            ],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parent.parent,
        )
        assert result.returncode == 1
        assert "哈希校验失败" in result.stderr
    finally:
        tmp_path.unlink(missing_ok=True)
        evidence.clear_index(DATASET_ID, MOCK_MODEL)
