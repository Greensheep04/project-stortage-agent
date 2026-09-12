"""T-004c：时间一致性修正——生成 pilot-v2 与 full-v2。

- pilot-v2：pilot-v1 原样 + 两处定向修正（EVX-0020 去掉不可见引用、CASE-0001 去掉未来月份表述）
- full-v2：pilot-v2 + 重新生成的新事件/案例（可见性过滤）+ 原 SOP/场景
- pilot-v1 / full-v1 保留为历史，不修改
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PILOT_V1 = PROJECT_ROOT / "deliverables" / "t004" / "pilot-v1"
PILOT_V2 = PROJECT_ROOT / "deliverables" / "t004" / "pilot-v2"
FULL_V1 = PROJECT_ROOT / "deliverables" / "t004" / "full-v1"
FULL_V2 = PROJECT_ROOT / "deliverables" / "t004" / "full-v2"
TMP = PROJECT_ROOT / ".tmp" / "t004"
XLSX = PROJECT_ROOT / "deliverables" / "仓储异常测试数据.xlsx"

PILOT_EVENT_CORRECTIONS = {
    "EVX-0020": {
        "drop_refs": [("业务说明", 12)],
        "note": "去掉记录时点（9-08 17:05）之后才记录的业务说明第 12 行；正文事实由可见流水 TX-1042/TX-1043 支持",
    },
}
PILOT_SCENARIO_CORRECTIONS = {
    "SCN-011": {
        "related_doc_ids": ["EVX-0011", "SOP-SKU-v1", "SOP-SKU-v2"],
        "order_id": "SO-1005",
        "line_id": "001",
        "note": "原关联事件 EVX-0013 记录于 9-09，晚于场景 cutoff 9-07；换为 cutoff 前可见的 SKU 不符事件 EVX-0011（记录 9-05）",
    },
}
PILOT_CASE_CORRECTIONS = {
    "CASE-0001": {
        "replace": [("与九月订单没有交易关联", "与当前调查的其他批次没有交易关联")],
        "note": "结案为 7 月，原句出现未来月份“九月”；改为不指向未来批次的表述",
    },
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def raw_lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def count_lines(path: Path) -> int:
    return len(raw_lines(path))


def build_pilot_v2() -> list:
    corrections = []
    PILOT_V2.mkdir(parents=True, exist_ok=True)
    (PILOT_V2 / "review").mkdir(parents=True, exist_ok=True)

    event_lines = []
    for line in raw_lines(PILOT_V1 / "events.jsonl"):
        doc = json.loads(line)
        spec = PILOT_EVENT_CORRECTIONS.get(doc["doc_id"])
        if spec:
            drop = set(spec["drop_refs"])
            doc["source_refs"] = [
                r for r in doc["source_refs"] if (r["sheet"], r["row"]) not in drop
            ]
            corrections.append({"doc_id": doc["doc_id"], "issue": "未来来源引用", "fix": spec["note"]})
        event_lines.append(json.dumps(doc, ensure_ascii=False))
    (PILOT_V2 / "events.jsonl").write_text("\n".join(event_lines) + "\n", encoding="utf-8")

    case_lines = []
    for line in raw_lines(PILOT_V1 / "cases.jsonl"):
        doc = json.loads(line)
        spec = PILOT_CASE_CORRECTIONS.get(doc["doc_id"])
        if spec:
            for old, new in spec["replace"]:
                for section in doc["sections"]:
                    section["text"] = section["text"].replace(old, new)
            corrections.append({"doc_id": doc["doc_id"], "issue": "案例时间自洽", "fix": spec["note"]})
        case_lines.append(json.dumps(doc, ensure_ascii=False))
    (PILOT_V2 / "cases.jsonl").write_text("\n".join(case_lines) + "\n", encoding="utf-8")

    (PILOT_V2 / "sops.jsonl").write_text((PILOT_V1 / "sops.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
    scenario_lines = []
    for line in raw_lines(PILOT_V1 / "review" / "scenarios.jsonl"):
        doc = json.loads(line)
        spec = PILOT_SCENARIO_CORRECTIONS.get(doc["scenario_id"])
        if spec:
            for key in ("related_doc_ids", "order_id", "line_id"):
                doc[key] = spec[key]
            corrections.append({"doc_id": doc["scenario_id"], "issue": "场景关联事件可见性", "fix": spec["note"]})
        scenario_lines.append(json.dumps(doc, ensure_ascii=False))
    (PILOT_V2 / "review" / "scenarios.jsonl").write_text("\n".join(scenario_lines) + "\n", encoding="utf-8")

    now = datetime.now(timezone(timedelta(hours=8)))
    manifest = {
        "version": "pilot-v2",
        "based_on": "pilot-v1",
        "created_at": now.isoformat(),
        "files": [
            {"name": name, "count": count_lines(PILOT_V2 / name), "sha256": sha256_file(PILOT_V2 / name)}
            for name in ("events.jsonl", "cases.jsonl", "sops.jsonl", "review/scenarios.jsonl")
        ],
        "corrections": corrections,
        "notes": "T-004c 时间一致性修正；未受影响试样原样保留，pilot-v1 保留为历史。",
    }
    (PILOT_V2 / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return corrections


def assemble_sops() -> list[str]:
    from t004.sop_content import EXTENSIONS, EXTENSIONS2, EXTENSIONS3, EXTENSIONS4, NEW_SOPS
    from t004.text_utils import sentence_dedupe

    for sop in NEW_SOPS:
        for section in sop["sections"]:
            for source in (EXTENSIONS, EXTENSIONS2, EXTENSIONS3, EXTENSIONS4):
                extra = source.get(sop["doc_id"], {}).get(section["section_id"])
                if extra:
                    section["text"] += extra
            section["text"] = sentence_dedupe(section["text"], sim_threshold=2.0)
    return [json.dumps(s, ensure_ascii=False) for s in NEW_SOPS]


def build_full_v2() -> dict:
    FULL_V2.mkdir(parents=True, exist_ok=True)
    (FULL_V2 / "review").mkdir(parents=True, exist_ok=True)

    # 事件：pilot-v2（修正后）+ 新事件
    events = raw_lines(PILOT_V2 / "events.jsonl") + raw_lines(TMP / "events_new.jsonl")
    (FULL_V2 / "events.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")
    # 案例：pilot-v2（修正后）+ 新案例
    cases = raw_lines(PILOT_V2 / "cases.jsonl") + raw_lines(TMP / "cases_new.jsonl")
    (FULL_V2 / "cases.jsonl").write_text("\n".join(cases) + "\n", encoding="utf-8")
    # SOP：pilot + 组装后的新 SOP（与 full-v1 相同逻辑）
    sops = raw_lines(PILOT_V2 / "sops.jsonl") + assemble_sops()
    (FULL_V2 / "sops.jsonl").write_text("\n".join(sops) + "\n", encoding="utf-8")
    # 场景：pilot + 新场景（如需修正在此处理）
    scenarios = raw_lines(PILOT_V2 / "review" / "scenarios.jsonl") + raw_lines(TMP / "scenarios_new.jsonl")
    (FULL_V2 / "review" / "scenarios.jsonl").write_text("\n".join(scenarios) + "\n", encoding="utf-8")

    # 受影响清单（与 full-v1 逐篇比较）
    affected_events, affected_cases = [], []
    for name, out_list in (("events.jsonl", affected_events), ("cases.jsonl", affected_cases)):
        v1 = {json.loads(l)["doc_id"]: l for l in raw_lines(FULL_V1 / name)}
        for line in raw_lines(FULL_V2 / name):
            doc = json.loads(line)
            if v1.get(doc["doc_id"]) != line:
                out_list.append(doc["doc_id"])
    (TMP / "v2_affected.json").write_text(
        json.dumps({"events": affected_events, "cases": affected_cases}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    now = datetime.now(timezone(timedelta(hours=8)))
    manifest = {
        "version": "full-v2",
        "based_on": "full-v1",
        "created_at": now.isoformat(),
        "files": [
            {"name": name, "count": count_lines(FULL_V2 / name), "sha256": sha256_file(FULL_V2 / name)}
            for name in ("events.jsonl", "cases.jsonl", "sops.jsonl", "review/scenarios.jsonl")
        ],
        "source_snapshot": {"file": "deliverables/仓储异常测试数据.xlsx", "sha256": sha256_file(XLSX)},
        "generation": {
            "method": "T-004c 时间一致性修正版：事件生成按 recorded_at 过滤来源与正文，案例按正文月份定时间",
            "pilot_docs": 40,
            "new_docs": 960,
            "dashscope_api_calls": 0,
            "estimated_cost_cny": 0,
            "batches": [
                {"part": "pilot", "batch": "P1", "doc_id_range": ["EVX-0001", "EVX-0024"], "count": 24, "method": "pilot-v1 原样 + 2 处定向修正"},
                {"part": "events", "batch": "E1", "doc_id_range": ["EVX-0025", "EVX-0700"], "count": 676, "method": "T-004c 可见性过滤重生成"},
                {"part": "cases", "batch": "C1", "doc_id_range": ["CASE-0013", "CASE-0280"], "count": 268, "method": "T-004c 月份自洽重生成"},
                {"part": "sops", "batch": "S1", "doc_id_range": ["SOP-DRAFT-v1", "SOP-ARCHIVE-v2"], "count": 16, "method": "沿用 T-004b 组装（无时间问题）"},
                {"part": "scenarios", "batch": "N1", "doc_id_range": ["SCN-017", "SCN-071"], "count": 55, "method": "沿用 T-004b 场景（可见性已核对）"},
            ],
        },
        "corrections": {
            "scope": "full-v1 → full-v2",
            "pilot": PILOT_EVENT_CORRECTIONS["EVX-0020"]["note"] + "；" + PILOT_CASE_CORRECTIONS["CASE-0001"]["note"],
            "event_method": "按每篇 recorded_at 过滤 source_refs/transaction_ids/正文（summary 只列可见流水）；关键来源晚于事件时把事件发生/记录时间顺延到来源之后",
            "case_method": "正文出现月份时，结案/记录时间落在该月并重算 rule_refs；无月份表述的保持原分布",
            "affected_events": len(affected_events),
            "affected_cases": len(affected_cases),
            "affected_list_file": ".tmp/t004/v2_affected.json（由 L5 证据目录保存）",
            "scenarios_count_fixed": "scenarios 计数修正为 71（原 full-v1 manifest 备注写 70）",
        },
        "notes": "700 事件 + 280 案例 + 20 SOP + 71 场景；pilot-v1/full-v1 保留为历史。",
    }
    (FULL_V2 / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"affected_events": len(affected_events), "affected_cases": len(affected_cases)}


def main() -> int:
    corrections = build_pilot_v2()
    stats = build_full_v2()
    print("pilot-v2 corrections:", len(corrections))
    print("full-v2 affected:", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
