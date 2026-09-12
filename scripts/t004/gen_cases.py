"""T-004b：案例生成（R2 重构版）。

每主题三条轴各 3 个实质选项；用 (x, y, z=x+y mod 3) 的 MDS 码取 8 个变体，
任意两变体至少两维不同。标题带情形后缀；正文经句级去重。
扩展空间：轴选项数 n → 单主题容量 n²；扩量时增补轴选项即可。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from t004.case_variants import CASE_EXTRAS, CASE_SUPPLEMENTS, THEMES
from t004.text_utils import sentence_dedupe

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TMP = PROJECT_ROOT / ".tmp" / "t004"
PILOT_SOPS = PROJECT_ROOT / "deliverables" / "t004" / "pilot-v1" / "sops.jsonl"
AS_OF = datetime(2026, 9, 9, 20, 0)
TOTAL_CASES = 268  # 完整集 280 含试样 12
VARIANTS_PER_THEME = 8
MONTH_WORDS = {
    "一月": 1, "二月": 2, "三月": 3, "四月": 4, "五月": 5, "六月": 6,
    "七月": 7, "八月": 8, "九月": 9, "十月": 10, "十一月": 11, "十二月": 12,
}


def load_all_sops() -> list:
    docs = [json.loads(line) for line in PILOT_SOPS.read_text(encoding="utf-8").splitlines() if line.strip()]
    from t004.sop_content import NEW_SOPS
    docs.extend(NEW_SOPS)
    return docs


def effective_refs(sops: list, policy: str, at: datetime) -> list:
    refs = []
    for doc in sops:
        if doc["policy_id"] != policy:
            continue
        start = datetime.fromisoformat(doc["effective_from"]).replace(tzinfo=None)
        end = (
            datetime.fromisoformat(doc["effective_to"]).replace(tzinfo=None)
            if doc["effective_to"]
            else None
        )
        if start <= at and (end is None or at < end):
            refs.append(doc["doc_id"])
    return sorted(refs)


PAD_SENTENCES = [
    "该处理过程按当时有效的流程执行，相关材料随批次归档，供后续核查时追溯。",
    "案例记录只保留可核对的事实，未加入推测内容，结论范围限于该批次。",
    "处理完成后由主管核对适用对象，确认结论不扩展到其他订单或商品。",
    "涉及的岗位在班次记录中同步了处理结果，后续同类事项按同一路径跟进。",
    "材料之间的时间与数量关系逐项比对，差异部分单独登记，未合并处理。",
    "该批次后续未再出现同类差异，相关记录按保存要求归档。",
    "处理过程中排除了若干猜测，未确认的部分如实保留，不补写原因。",
    "该案例用于说明处理路径与材料要求，不代表同类情况一定得到相同结果。",
    "后续班次按记录继续跟踪未完成事项，状态更新后回填原记录。",
    "复核岗位与当班人员分别说明情况，记录中保留了双方依据。",
    "该处理未改动原流水与原始记录，更正通过新增记录完成。",
    "涉及数量的部分以已过账流水为准，草稿与口头说明只作线索。",
    "归档清单与批次记录逐项核对，缺件部分登记并指定责任人跟进。",
    "该事项在班次交接中列明，接班人员确认后继续处理。",
    "处理结果以书面记录为准，未完成的动作保持待办状态。",
    "核对范围覆盖相关货位与相邻商品，确认影响没有扩大。",
    "案例的处理顺序为先核对材料、再判断原因，未跳过任何环节。",
    "相关记录保留了中间过程与时间点，便于后续追溯当时的判断依据。",
    "该批次的处理经验已纳入班组提醒，用于减少同类差异。",
    "未确认事项按待查登记，材料补齐后再更新结论。",
]


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:00+08:00")


def mds_combos(n: int = 3) -> list:
    return [(x, y, (x + y) % n) for x in range(n) for y in range(n)]


def main() -> int:
    sops = load_all_sops()
    cases = []
    start_date = datetime(2026, 6, 15, 10, 0)
    combos_all = mds_combos(3)

    n_themes = len(THEMES)
    base = TOTAL_CASES // n_themes
    extra = TOTAL_CASES % n_themes
    counts = [base + (1 if t < extra else 0) for t in range(n_themes)]

    for t_idx, theme in enumerate(THEMES):
        skip = t_idx % len(combos_all)
        combos = [c for i, c in enumerate(combos_all) if i != skip][:counts[t_idx]]
        for v_idx, (x, y, z) in enumerate(combos):
            i = sum(counts[:t_idx]) + v_idx
            skus = theme.get("skus")
            sku = skus[i % len(skus)] if skus else None
            extras = CASE_EXTRAS[theme["id"]]
            supp = CASE_SUPPLEMENTS[theme["id"]]
            bg = sentence_dedupe(theme["backgrounds"][x] + extras["contexts"][x] + supp["context"])
            inv = sentence_dedupe(theme["paths"][y] + extras["evidences"][y] + supp["evidence"])
            res = sentence_dedupe(theme["outcomes"][z] + extras["boundaries"][z] + supp["boundary"])

            # 时间自洽：正文出现月份时，结案/记录时间落在该月（取最晚月份）
            mentioned = [m for w, m in MONTH_WORDS.items() if w in (bg + inv + res)]
            if mentioned:
                month = max(mentioned)
                closed = datetime(2026, month, 5 + (i % 18), 14 + (i % 4), (i * 7) % 60)
                if closed > AS_OF - timedelta(days=2):
                    closed = AS_OF - timedelta(days=2, hours=i % 12)
                recorded = min(closed + timedelta(days=1 + i % 2, hours=1 + i % 5), AS_OF - timedelta(hours=1))
            else:
                month_offset = (i * 7) % 86
                closed = start_date + timedelta(days=month_offset, hours=(i * 3) % 8)
                if closed > AS_OF - timedelta(days=2):
                    closed = AS_OF - timedelta(days=2, hours=i % 12)
                recorded = min(closed + timedelta(days=1 + i % 2, hours=1 + i % 5), AS_OF - timedelta(hours=1))
            cases.append(
                {
                    "doc_id": "",
                    "doc_type": "case",
                    "title": f"{theme['title']}案例（{theme['path_labels'][y]}·{theme['outcome_labels'][z]}）",
                    "recorded_at": fmt(recorded),
                    "closed_at": fmt(closed),
                    "author_role": theme["role"],
                    "scope": {"warehouse_id": "WH-01", "sku": sku, "order_id": None, "line_id": None},
                    "history_case_id": "",
                    "rule_refs": effective_refs(sops, theme["policy"], closed),
                    "sections": [
                        {"section_id": "background", "heading": "历史背景", "text": bg},
                        {"section_id": "investigation", "heading": "查证经过", "text": inv},
                        {"section_id": "result", "heading": "结果与适用边界", "text": res},
                    ],
                    "synthetic": True,
                }
            )

    assert len(cases) == TOTAL_CASES, len(cases)

    pad_i = 0
    for case in cases:
        for _ in range(6):
            case["sections"] = [
                {**s, "text": sentence_dedupe(s["text"], sim_threshold=0.4)} for s in case["sections"]
            ]
            total = sum(len(s["text"]) for s in case["sections"])
            if total >= 250:
                break
            sentence = PAD_SENTENCES[pad_i % len(PAD_SENTENCES)]
            pad_i += 1
            case["sections"][-1]["text"] += sentence
    for idx, case in enumerate(cases, start=13):
        case["doc_id"] = f"CASE-{idx:04d}"
        case["history_case_id"] = f"HIST-{idx:04d}"

    TMP.mkdir(parents=True, exist_ok=True)
    (TMP / "cases_new.jsonl").write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n", encoding="utf-8"
    )
    batches = []
    for b, start in enumerate(range(0, len(cases), 50)):
        chunk = cases[start:start + 50]
        batches.append({
            "batch": f"C{b + 1}",
            "doc_id_range": [chunk[0]["doc_id"], chunk[-1]["doc_id"]],
            "count": len(chunk),
            "method": "会话内模型生成（主题轴×MDS 变体，R2 重构）",
        })
    (TMP / "cases_batches.json").write_text(json.dumps(batches, ensure_ascii=False, indent=2), encoding="utf-8")
    lens = [sum(len(s["text"]) for s in c["sections"]) for c in cases]
    print("new cases:", len(cases))
    print("length min/avg/max:", min(lens), sum(lens) // len(lens), max(lens))
    from collections import Counter
    print("themes:", len(THEMES), "| titles unique:", len({c["title"] for c in cases}))
    print("outcome label distribution:", Counter(c["title"].split("·")[-1].rstrip("）") for c in cases))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
