#!/usr/bin/env python3
"""生成 deliverables/t004/pilot-v1/manifest.json（T-004a 清单）。"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PILOT_DIR = PROJECT_ROOT / "deliverables" / "t004" / "pilot-v1"
XLSX = PROJECT_ROOT / "deliverables" / "仓储异常测试数据.xlsx"

FILES = ["events.jsonl", "cases.jsonl", "sops.jsonl", "review/scenarios.jsonl"]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def count_docs(path: Path) -> int:
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def main() -> int:
    now = datetime.now(timezone(timedelta(hours=8)))
    manifest = {
        "version": "pilot-v1",
        "created_at": now.isoformat(),
        "files": [
            {
                "name": name,
                "count": count_docs(PILOT_DIR / name),
                "sha256": sha256_file(PILOT_DIR / name),
            }
            for name in FILES
        ],
        "source_snapshot": {
            "file": "deliverables/仓储异常测试数据.xlsx",
            "sha256": sha256_file(XLSX),
        },
        "generation": {
            "method": "模型会话直接撰写（deepseek-v4-flash），无 DashScope API 调用",
            "model_docs": 40,
            "script_docs": 0,
            "manual_docs": 0,
            "dashscope_api_calls": 0,
            "estimated_cost_cny": 0,
        },
        "notes": "40 篇父文档：24 事件＋12 案例＋4 SOP；review/scenarios.jsonl 为审查辅助材料，不进入业务正文。",
    }
    out = PILOT_DIR / "manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
