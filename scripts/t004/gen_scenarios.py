"""T-004b：完整集场景审查表生成（新增 54 组，与试样 16 组合计 70 组）。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TMP = PROJECT_ROOT / ".tmp" / "t004"
PILOT = PROJECT_ROOT / "deliverables" / "t004" / "pilot-v1"
FORBIDDEN = [
    ("SO-1009", "001"), ("SO-1009", "002"), ("SO-1039", "002"), ("SO-1042", "001"),
    ("SO-1050", "001"), ("SO-1050", "002"), ("SO-1075", "001"), ("SO-1077", "001"),
    ("SO-1083", "001"), ("SO-1099", "001"),
]
PILOT_FORBIDDEN_DOCS = {
    "SO-1009/001": ["EVX-0022"], "SO-1050/002": ["EVX-0023"], "SO-1099/001": ["EVX-0024"],
}
NEW_POLICIES = ["SOP-DRAFT", "SOP-REVERSE", "SOP-SPLIT", "SOP-RECEIPT", "SOP-DIFF", "SOP-HANDOVER", "SOP-ESCALATE", "SOP-ARCHIVE"]
SOP_WINDOWS = {
    "SOP-DRAFT": ("SOP-DRAFT-v1", "SOP-DRAFT-v2", "2026-09-01T00:00:00+08:00"),
    "SOP-REVERSE": ("SOP-REVERSE-v1", "SOP-REVERSE-v2", "2026-08-15T00:00:00+08:00"),
    "SOP-SPLIT": ("SOP-SPLIT-v1", "SOP-SPLIT-v2", "2026-08-20T00:00:00+08:00"),
    "SOP-RECEIPT": ("SOP-RECEIPT-v1", "SOP-RECEIPT-v2", "2026-09-05T00:00:00+08:00"),
    "SOP-DIFF": ("SOP-DIFF-v1", "SOP-DIFF-v2", "2026-08-25T00:00:00+08:00"),
    "SOP-HANDOVER": ("SOP-HANDOVER-v1", "SOP-HANDOVER-v2", "2026-09-03T00:00:00+08:00"),
    "SOP-ESCALATE": ("SOP-ESCALATE-v1", "SOP-ESCALATE-v2", "2026-08-30T00:00:00+08:00"),
    "SOP-ARCHIVE": ("SOP-ARCHIVE-v1", "SOP-ARCHIVE-v2", "2026-09-06T00:00:00+08:00"),
}


def load_jsonl(path: Path) -> list:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    pilot_events = load_jsonl(PILOT / "events.jsonl")
    pilot_cases = load_jsonl(PILOT / "cases.jsonl")
    new_events = load_jsonl(TMP / "events_new.jsonl")
    new_cases = load_jsonl(TMP / "cases_new.jsonl")
    events = pilot_events + new_events
    cases = pilot_cases + new_cases

    by_line: dict[str, list] = {}
    for e in events:
        key = f"{e['scope']['order_id']}/{e['scope']['line_id']}"
        by_line.setdefault(key, []).append(e)
    events_by_title = {}
    for e in events:
        events_by_title.setdefault(e["title"].split()[-1], []).append(e)

    scenarios = []
    n = 17

    def add(scenario: dict):
        nonlocal n
        scenario["scenario_id"] = f"SCN-{n:03d}"
        n += 1
        scenarios.append(scenario)

    # 1) 证据不足 7 组（补足试样 3 组到 10 组）
    for order_id, line_id in FORBIDDEN:
        key = f"{order_id}/{line_id}"
        if key in PILOT_FORBIDDEN_DOCS:
            continue
        docs = [e["doc_id"] for e in by_line.get(key, [])]
        add({
            "scenario_type": "insufficient_evidence",
            "related_doc_ids": docs,
            "order_id": order_id, "line_id": line_id,
            "cutoff": "2026-09-09T20:00:00+08:00",
            "facts_to_check": f"{key} 的流水与说明仅支持数量差异登记，没有原因材料",
            "allowed_conclusions": ["报告差异数量并列入待查", "说明现有资料不足以确定原因"],
            "forbidden_conclusions": ["补写确定原因", "引用其他订单说明作为本单原因"],
            "basis": "模板 §2 时间与权威性；T-004a 审查冻结约束 1",
        })

    # 2) 同对象同时间相反说法 8 组（新增刻意冲突对）
    for i, line_key in enumerate([
        "SO-1008/001", "SO-1015/001", "SO-1016/001", "SO-1026/001",
        "SO-1031/001", "SO-1040/001", "SO-1053/001", "SO-1062/001",
    ]):
        pair = [e for e in events if f"{e['scope']['order_id']}/{e['scope']['line_id']}" == line_key
                and e["title"].split()[-1] in ("交接完成确认", "现场数量复核")]
        add({
            "scenario_type": "same_object_time_contradiction",
            "related_doc_ids": [e["doc_id"] for e in pair],
            "order_id": line_key.split("/")[0], "line_id": line_key.split("/")[1],
            "cutoff": "2026-09-09T20:00:00+08:00",
            "facts_to_check": f"{line_key} 的两条记录发生于同一时点，对完成数量说法相反",
            "allowed_conclusions": ["指出两说法相反、需要澄清", "分别列出两说法及依据"],
            "forbidden_conclusions": ["择一作为确定事实", "把两个数量相加或取平均"],
            "basis": "模板 §5；T-004a 审查冻结约束 3",
            "design_note": "刻意设计的工作人员相反说法：基础关联键与源坐标真实，冲突仅存在于新编叙述",
        })

    # 3) 相容状态变化 8 组（分批两批、冲销前后）
    compatible = []
    for line_key, docs in by_line.items():
        titles = {e["title"].split()[-1] for e in docs}
        if "首批装车登记" in titles and "第二批装车补充核对" in titles:
            pair = [e["doc_id"] for e in docs if e["title"].split()[-1] in ("首批装车登记", "第二批装车补充核对")]
            compatible.append((line_key, pair, "分批两批"))
        if "冲销复核登记" in titles and len(compatible) < 8:
            pair = [e["doc_id"] for e in docs if e["title"].split()[-1] == "冲销复核登记"]
            compatible.append((line_key, pair, "冲销更正"))
    for line_key, docs, kind in compatible[:8]:
        add({
            "scenario_type": "compatible_state_change",
            "related_doc_ids": docs,
            "order_id": line_key.split("/")[0], "line_id": line_key.split("/")[1],
            "cutoff": "2026-09-09T20:00:00+08:00",
            "facts_to_check": f"{line_key} 的先后记录构成{kind}，累计或更正后与计划一致",
            "allowed_conclusions": ["按时间顺序陈述前后记录", "说明前后相容、不构成冲突"],
            "forbidden_conclusions": ["把前后记录判为相反说法", "只用其中一条判断完成量"],
            "basis": "模板 §2 前后状态变化按时间展开",
        })

    # 4) 相似历史干扰 8 组
    sku_case = {}
    for c in cases:
        sku = (c.get("scope") or {}).get("sku")
        if sku:
            sku_case.setdefault(sku, []).append(c)
    used = 0
    for e in new_events:
        if used >= 8:
            break
        sku = e["scope"]["sku"]
        if e["title"].split()[-1] not in ("出库核对完成", "破损商品待检登记", "标签退回重贴登记", "扫描 SKU 不符登记"):
            continue
        candidates = [c for c in sku_case.get(sku, []) if c["doc_id"].startswith("CASE-")]
        if not candidates:
            continue
        c = candidates[used % len(candidates)]
        add({
            "scenario_type": "similar_history_interference",
            "related_doc_ids": [e["doc_id"], c["doc_id"]],
            "order_id": e["scope"]["order_id"], "line_id": e["scope"]["line_id"],
            "cutoff": "2026-09-09T20:00:00+08:00",
            "facts_to_check": f"当前事件与历史案例商品相近；历史案例只提供排查路径",
            "allowed_conclusions": ["参考历史案例提示的核查路径", "说明当前原因未确认"],
            "forbidden_conclusions": ["确认本单为历史案例中的原因", "把历史结论当作本单证据"],
            "basis": "模板 §2 历史结论不转移到当前订单",
        })
        used += 1

    # 5) SOP 版本边界 8 组（8 个新政策各一组）
    boundary_events = [e for e in new_events if e["title"].split()[-1] in
                       ("草稿状态跟踪", "冲销复核登记", "首批装车登记", "签收材料缺失",
                        "数量差异待查", "交班复核", "事项升级", "材料归档")]
    for i, policy in enumerate(NEW_POLICIES):
        v1, v2, boundary = SOP_WINDOWS[policy]
        cutoff_dt = datetime.fromisoformat(boundary).replace(tzinfo=None)
        visible = [
            e for e in boundary_events
            if datetime.fromisoformat(e["recorded_at"]).replace(tzinfo=None) <= cutoff_dt
        ]
        if visible:
            e = visible[i % len(visible)]
            related = [v1, v2, e["doc_id"]]
            order_id, line_id = e["scope"]["order_id"], e["scope"]["line_id"]
            facts = f"{policy} 的 v1 在 {boundary[:10]} 前有效、v2 自该时点起生效，需按当时版本处理"
        else:
            related = [v1, v2]
            order_id, line_id = None, None
            facts = f"{policy} 的 v1 在 {boundary[:10]} 前有效、v2 自该时点起生效；该时点尚无可见业务事件，按版本区间判断适用流程"
        add({
            "scenario_type": "sop_version_boundary",
            "related_doc_ids": related,
            "order_id": order_id, "line_id": line_id,
            "cutoff": boundary,
            "facts_to_check": facts,
            "allowed_conclusions": ["按当时有效版本处理", "说明另一版本在截止时点的有效性"],
            "forbidden_conclusions": ["在旧版本有效期内按新版本要求处理", "在新版本生效后沿用旧版条件"],
            "basis": "模板 §2 SOP 有效区间前闭后开；冻结约束 4",
        })

    # 6) 事后补录 8 组
    backfills = [e for e in new_events if "补录" in e["title"]]
    for e in backfills[:8]:
        add({
            "scenario_type": "backfill_time_visibility",
            "related_doc_ids": [e["doc_id"]],
            "order_id": e["scope"]["order_id"], "line_id": e["scope"]["line_id"],
            "cutoff": e["occurred_at"],
            "facts_to_check": f"{e['doc_id']} 事发于 {e['occurred_at'][:10]}，补录于 {e['recorded_at'][:10]}；事发时点该记录尚不存在",
            "allowed_conclusions": ["事发时点只能使用当时已存在的记录", "引用补录内容时注明补录时间"],
            "forbidden_conclusions": ["在事发时点引用补录内容", "把补录时间写成事发时间"],
            "basis": "模板 §2 晚补录不能出现在过去时点的回答中",
        })

    # 7) 同单不同明细 8 组
    order_lines: dict[str, list[str]] = {}
    for key in by_line:
        order_id, line_id = key.split("/")
        order_lines.setdefault(order_id, []).append(line_id)
    multi = [(o, ls) for o, ls in order_lines.items() if len(ls) > 1]
    for order_id, lines in multi[:8]:
        docs = []
        for line_id in lines[:2]:
            docs.extend(e["doc_id"] for e in by_line.get(f"{order_id}/{line_id}", [])[:2])
        add({
            "scenario_type": "same_order_different_line",
            "related_doc_ids": docs,
            "order_id": order_id, "line_id": None,
            "cutoff": "2026-09-09T20:00:00+08:00",
            "facts_to_check": f"{order_id} 含多个明细，需分别核对，不得合并累计",
            "allowed_conclusions": ["按明细分别报告", "同订单不同明细单独核对"],
            "forbidden_conclusions": ["合并两明细数量计算差异", "用另一明细的记录支持本明细结论"],
            "basis": "模板 §2 明细隔离；冻结约束 3",
        })

    TMP.mkdir(parents=True, exist_ok=True)
    out = TMP / "scenarios_new.jsonl"
    out.write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in scenarios) + "\n", encoding="utf-8")
    from collections import Counter
    print("new scenarios:", len(scenarios))
    print("type counts:", dict(Counter(s["scenario_type"] for s in scenarios)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
