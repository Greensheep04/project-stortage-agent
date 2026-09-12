"""T-007c R2：评测集标签完整性补全（子主题归组规则＋逐篇理由）。

读取已保存的检索报告与 v1 标签，对“全部返回候选”中未判定的文档补齐 0/1/2，
输出 v2 标签集（保留 v1 原文件）与变更清单。规则：
- 等级 2：与本题已判定等级 2 文档同子主题（直接满足，补标）
- 等级 1：与本题已判定等级 1 文档同子主题（有帮助但不能独立满足）
- 等级 0：其余返回候选（写明主题与不适用理由）
SOP 按政策（policy）为子主题；案例按标题主题（含分批登记延迟细分）。
"""

import hashlib
import json
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
BASE = PROJECT / "deliverables/t004/full-v2"
EV = PROJECT / "docs/L5-执行与验证/evidence/T-007"
OUT = PROJECT / "tests/eval/t007"

SUBTHEME_RULES = [
    ("分批装运的首末批", None),  # 特殊细分，见 sub_theme
    ("货位数量记录滞后", "货位记录滞后短拣"),
    ("补货未登记", "补货未登记短拣"),
    ("多货位分布", "多货位短拣"),
    ("盘点抽查", "盘点抽查"),
    ("统计口径", "统计口径误报"),
    ("同订单明细串记", "明细串记"),
    ("未到期订单", "未到期误报"),
    ("差异材料不足", "差异材料不足待查"),
    ("差异上报超时", "差异上报超时"),
    ("标签互换", "标签互换"),
    ("标签贴错", "标签贴错"),
    ("标签疑点", "标签疑点"),
    ("拣货取错货位", "拣货取错货位"),
    ("出库流水多录", "流水多录冲销"),
    ("冲销关联错误", "冲销关联错误"),
    ("冲销超时", "冲销超时"),
    ("草稿出库单跨班次", "草稿跨班次"),
    ("草稿误作废", "草稿误作废"),
    ("草稿处理超时", "草稿超时"),
    ("包装破损索赔", "破损索赔缺记录"),
    ("外包装受潮", "受潮复核"),
    ("沿用旧版包装流程", "包装版本误用"),
    ("承运签收材料缺失", "签收缺失"),
    ("签收材料延迟回收", "签收延迟补录"),
    ("签收材料归档不全", "签收归档不全"),
    ("客户催办", "客户催办分批"),
    ("分批发货与承运延迟", "承运延迟区分"),
    ("波次计划临时调整", "波次调整核对"),
    ("月台误放", "月台误放"),
    ("交接班记录缺少未完成事项", "交接缺待办"),
    ("交接记录与系统状态不一致", "交接状态不一致"),
    ("异常事项的升级路径", "升级路径"),
    ("升级结果回填", "升级结果回填"),
    ("记录保存期限", "保存期限"),
    ("归档材料缺件", "归档缺件"),
    ("短拣原因排查", "短拣原因未确认"),
]

cases = {
    d["doc_id"]: d
    for d in (
        json.loads(line)
        for line in (BASE / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    )
}
sops = {
    d["doc_id"]: d
    for d in (
        json.loads(line)
        for line in (BASE / "sops.jsonl").read_text(encoding="utf-8").splitlines()
    )
}
docs = {**cases, **sops}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sub_theme(doc):
    if doc["doc_type"] != "case":
        return doc["policy_id"]
    title = doc["title"]
    for keyword, theme in SUBTHEME_RULES:
        if keyword not in title:
            continue
        if keyword == "分批装运的首末批":
            return "分批登记延迟" if "登记延迟" in title else "分批汇总"
        return theme
    return "其他"


def query_themes(query):
    grade2, grade1 = set(), set()
    for label in query["labels"]:
        theme = sub_theme(docs[label["doc_id"]])
        if label["grade"] == 2:
            grade2.add(theme)
        elif label["grade"] == 1:
            grade1.add(theme)
    return grade2, grade1


def returned_candidates(report_path):
    report = json.loads(report_path.read_text(encoding="utf-8"))
    returns = {}
    for group in report["groups"].values():
        for entry in group["per_query"]:
            returns.setdefault(entry["qid"], set()).update(
                entry.get("candidate_doc_ids") or entry.get("ranked_doc_ids") or []
            )
    return returns


def complete(report_path, queries_path, v2_path, label, changes):
    queries = [
        json.loads(line)
        for line in queries_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    returns = returned_candidates(report_path)
    g2_added, g1_added, g0_added = [], [], 0
    for query in queries:
        query["label_version"] = "v2"
        labeled = {l["doc_id"] for l in query["labels"]}
        g2_themes, g1_themes = query_themes(query)
        for doc_id in sorted(returns.get(query["qid"], set()) - labeled):
            doc = docs[doc_id]
            theme = sub_theme(doc)
            title = doc["title"][:44]
            if theme in g2_themes:
                grade = 2
                reason = f"补标：与本题已判定等级 2 同子主题（{theme}），直接满足该问题（{title}）"
                g2_added.append((query["qid"], doc_id, theme))
            elif theme in g1_themes:
                grade = 1
                reason = f"补标：同属子主题 {theme}，有帮助但不能独立满足（{title}）"
                g1_added.append((query["qid"], doc_id, theme))
            else:
                grade = 0
                reason = (
                    f"补标：子主题 {theme} 与本题场景（{query['scenario_group']}）不同，"
                    f"不作为该问题依据（{title}）"
                )
                g0_added += 1
            query["labels"].append(
                {"doc_id": doc_id, "section_id": "result", "grade": grade, "reason": reason}
            )
        query["confusable_ids"] = [l["doc_id"] for l in query["labels"] if l["grade"] == 0]

    # 完整性自检：返回候选必须全部有判定
    unknown = []
    for query in queries:
        labeled = {l["doc_id"] for l in query["labels"]}
        unknown += [f"{query['qid']}:{d}" for d in sorted(returns.get(query["qid"], set()) - labeled)]
    assert not unknown, unknown[:5]

    v2_path.write_text(
        "".join(json.dumps(q, ensure_ascii=False) + "\n" for q in queries), encoding="utf-8"
    )
    changes[label] = {
        "queries": len(queries),
        "grade2_added": g2_added,
        "grade1_added": g1_added,
        "grade0_added": g0_added,
        "v1_sha256": sha256_file(queries_path),
        "v2_sha256": sha256_file(v2_path),
    }
    print(
        f"{label}: v2 写出；补标等级2 {len(g2_added)}、等级1 {len(g1_added)}、等级0 {g0_added}；"
        f"返回候选未判定 {len(unknown)}"
    )


if __name__ == "__main__":
    changes = {}
    complete(
        EV / "T-007b-dev-four-groups.json",
        OUT / "dev_queries.jsonl",
        OUT / "dev_queries_v2.jsonl",
        "dev",
        changes,
    )
    complete(
        EV / "T-007b-test-phrase.json",
        OUT / "test_queries.jsonl",
        OUT / "test_queries_v2.jsonl",
        "test",
        changes,
    )
    print(json.dumps(changes, ensure_ascii=False, indent=2))
