"""T-009a B3/B5：CLI chat --transport mcp 的 /exit、EOF、Ctrl-C 均无遗留子进程。"""

import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pi_market import corpus
from test_corpus_common import DATASET_ID, REAL_CORPUS_DIR

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AS_OF = "2026-09-09T20:00:00+08:00"


@pytest.fixture(scope="module", autouse=True)
def _corpus_ready():
    corpus.import_corpus("t004-full-v2", DATASET_ID, files_dir=REAL_CORPUS_DIR)
    corpus.index_corpus("t004-full-v2", "mock-v1", no_embedding=True)
    yield


def _env():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _pids():
    out = subprocess.run(["pgrep", "-f", "pi_market.mcp_server"], capture_output=True, text=True)
    return {int(pid) for pid in out.stdout.split() if pid.strip()}


def _wait_gone(pids, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not (pids & _pids()):
            return True
        time.sleep(0.2)
    return not (pids & _pids())


def _start_cli():
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pi_market.cli",
            "chat",
            DATASET_ID,
            "t004-full-v2",
            "--order",
            "SO-1003",
            "--line-id",
            "001",
            "--as-of",
            AS_OF,
            "--transport",
            "mcp",
        ],
        cwd=str(PROJECT_ROOT),
        env=_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _read_until(proc, marker, timeout=60.0):
    buf = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if select.select([proc.stdout], [], [], 0.5)[0]:
            line = proc.stdout.readline()
            if not line:
                break
            buf += line
            if marker in buf:
                return buf
        elif proc.poll() is not None:
            break
    return buf


def test_exit_command_leaves_no_mcp_process():
    baseline = _pids()
    proc = _start_cli()
    try:
        out, _ = proc.communicate("/exit\n", timeout=60)
        assert proc.returncode == 0
        assert "工具传输：mcp" in out
        assert "已绑定 SO-1003/001" in out
    finally:
        if proc.poll() is None:
            proc.kill()
    assert _wait_gone(_pids() - baseline), "退出后存在遗留 MCP 子进程"


def test_eof_leaves_no_mcp_process():
    baseline = _pids()
    proc = _start_cli()
    try:
        out, _ = proc.communicate("", timeout=60)
        assert proc.returncode == 0
        assert "工具传输：mcp" in out
    finally:
        if proc.poll() is None:
            proc.kill()
    assert _wait_gone(_pids() - baseline), "EOF 退出后存在遗留 MCP 子进程"


def test_ctrl_c_leaves_no_mcp_process():
    baseline = _pids()
    proc = _start_cli()
    try:
        out = _read_until(proc, "已绑定 SO-1003/001")
        assert "已绑定 SO-1003/001" in out
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=30)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    assert _wait_gone(_pids() - baseline), "Ctrl-C 退出后存在遗留 MCP 子进程"
