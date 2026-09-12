"""T-004b：拼装 full-v1（试样原样并入 + 扩量内容）并生成 manifest。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PILOT = PROJECT_ROOT / "deliverables" / "t004" / "pilot-v1"
FULL = PROJECT_ROOT / "deliverables" / "t004" / "full-v1"
TMP = PROJECT_ROOT / ".tmp" / "t004"
XLSX = PROJECT_ROOT / "deliverables" / "仓储异常测试数据.xlsx"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def raw_lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_merged(pilot_path: Path, new_path: Path, out_path: Path) -> None:
    lines = raw_lines(pilot_path) + raw_lines(new_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    # 新 SOP 落盘
    from t004.sop_content import EXTENSIONS, EXTENSIONS2, EXTENSIONS3, EXTENSIONS4, NEW_SOPS
    from t004.text_utils import sentence_dedupe

    for sop in NEW_SOPS:
        for section in sop["sections"]:
            for source in (EXTENSIONS, EXTENSIONS2, EXTENSIONS3, EXTENSIONS4):
                extra = source.get(sop["doc_id"], {}).get(section["section_id"])
                if extra:
                    section["text"] += extra
            section["text"] = sentence_dedupe(section["text"], sim_threshold=2.0)  # SOP 仅做包含去重

    TMP.mkdir(parents=True, exist_ok=True)
    sops_new = TMP / "sops_new.jsonl"
    sops_new.write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in NEW_SOPS) + "\n", encoding="utf-8"
    )

    FULL.mkdir(parents=True, exist_ok=True)
    (FULL / "review").mkdir(parents=True, exist_ok=True)

    write_merged(PILOT / "events.jsonl", TMP / "events_new.jsonl", FULL / "events.jsonl")
    write_merged(PILOT / "cases.jsonl", TMP / "cases_new.jsonl", FULL / "cases.jsonl")
    write_merged(PILOT / "sops.jsonl", sops_new, FULL / "sops.jsonl")
    write_merged(PILOT / "review" / "scenarios.jsonl", TMP / "scenarios_new.jsonl", FULL / "review" / "scenarios.jsonl")

    now = datetime.now(timezone(timedelta(hours=8)))
    batches = []
    for path, prefix in (
        (TMP / "events_batches.json", "events"),
        (TMP / "cases_batches.json", "cases"),
    ):
        for batch in json.loads(path.read_text(encoding="utf-8")):
            batch["part"] = prefix
            batches.append(batch)
    batches.append({"part": "sops", "batch": "S1", "doc_id_range": ["SOP-DRAFT-v1", "SOP-ARCHIVE-v2"], "count": 16, "method": "会话内模型手写"})
    new_scenarios = json.loads("[" + ",".join(raw_lines(TMP / "scenarios_new.jsonl")) + "]")
    batches.append({
        "part": "scenarios", "batch": "N1",
        "doc_id_range": [new_scenarios[0]["scenario_id"], new_scenarios[-1]["scenario_id"]],
        "count": len(new_scenarios), "method": "会话内模型生成",
    })

    manifest = {
        "version": "full-v1",
        "created_at": now.isoformat(),
        "files": [
            {"name": name, "count": sum(1 for line in raw_lines(FULL / name)), "sha256": sha256_file(FULL / name)}
            for name in ("events.jsonl", "cases.jsonl", "sops.jsonl", "review/scenarios.jsonl")
        ],
        "source_snapshot": {
            "file": "deliverables/仓储异常测试数据.xlsx",
            "sha256": sha256_file(XLSX),
        },
        "generation": {
            "method": "会话内模型生成；试样 pilot-v1 原样并入；事件按角度+事实分批，案例按主题轴×MDS 变体（R2 重构）",
            "pilot_docs": 40,
            "new_docs": 960,
            "dashscope_api_calls": 0,
            "estimated_cost_cny": 0,
            "batches": batches,
            "failures_revisions": [
                {"part": "events", "issue": "禁补明细曾混入通用运营事件", "fix": "禁补明细只生成待查事件（10 个明细各 1 篇）"},
                {"part": "events", "issue": "补录事件记录时间被数据截止时间截断", "fix": "补录选行改为事发较早的 12 个明细"},
                {"part": "events", "issue": "标签数量提取把 3 件与 13 件连成 313", "fix": "改为按发现(\d+)件 正则提取"},
                {"part": "cases", "issue": "扩展句在同一文档内重复", "fix": "生成器对同文档句子去重"},
                {"part": "all", "issue": "R1：句内整句重复（events 120/700、cases 268/280、sops 9/20）", "fix": "统一句级去重（精确/包含/近义），句内重复=0；事件补句改为从未用句选取"},
                {"part": "cases", "issue": "R2：同题变体无实质差异、填充句 25% 复用", "fix": "改为三轴（背景/路径/结局）×MDS 码变体，标题带情形后缀，填充句按轴选项主题化；整句最高共用 21 篇（≤40）"},
            ],
            "scaling": {
                "case_variant_model": "每主题三轴（背景/核查路径/结局）各 3 个实质选项，用 (x,y,z=x+y mod 3) 的 MDS 码生成两两至少两维不同的变体；单主题容量 = 选项数²",
                "current_capacity": "35 主题 × 8 变体 = 280 篇（含试样 12）",
                "expansion_note": "扩到 1 万/5 万篇时，增补各轴选项（如每轴 10 项 → 单主题 100 变体）并增加主题数即可；生成器与质量校验（维度差异、句频上限、近似候选）无需改架构",
            },
        },
        "notes": "700 事件 + 280 案例 + 20 SOP（10 政策×2 版）；review/scenarios.jsonl 70 组，仅供审查。",
    }
    (FULL / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({f["name"]: f["count"] for f in manifest["files"]}, ensure_ascii=False))
    print("batches:", len(batches))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
