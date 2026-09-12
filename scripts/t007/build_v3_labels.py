"""T-007c R2 完整判定覆盖：文档归组清单 + 逐查询组映射 → v3 全量可见标签。

- 组定义依据正文所回答的问题与边界（见 GROUP_DEFS）；
- 每查询：direct 组＝等级 2、related 组＝等级 1、其余＝等级 0；doc 级 exceptions 记必要例外；
- 为全部“可见父文档”（时间＋范围规则）生成标签；负向题同样覆盖可见范围（全 0）。
干跑：--dry-run 打印映射与冲突检查，不写文件。
"""

import argparse
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
BASE = PROJECT / "deliverables/t004/full-v2"
OUT = PROJECT / "tests/eval/t007"

BASE_TO_GROUP = {
    "货位数量记录滞后的短拣排查案例": "CG-SHORT-REC",
    "白色陶瓷杯短拣原因排查案例": "CG-SHORT-REC",
    "多货位分布的短拣补齐案例": "CG-SHORT-MULTI",
    "补货未登记导致的短拣案例": "CG-SHORT-NOREG",
    "差异上报超时的补登记案例": "CG-DIFF-LATE",
    "差异材料不足的待查处理案例": "CG-DIFF-INSUFF",
    "统计口径导致的差异误报案例": "CG-COUNT-WRONG",
    "同订单明细串记的更正案例": "CG-LINE-MIX",
    "同订单两个明细数量串记的更正案例": "CG-LINE-MIX",
    "未到期订单的误报撤销案例": "CG-NOT-DUE",
    "未到期订单提前催办的纠正案例": "CG-NOT-DUE",
    "盘点抽查差异的登记案例": "CG-STOCK-CHECK",
    "货位标签互换导致的扫描不符案例": "CG-TAG-SWAP",
    "扫描 SKU 不符的货位标签排查案例": "CG-TAG-SWAP",
    "标签贴错退回重贴的排查案例": "CG-TAG-SWAP",
    "拣货取错货位的 SKU 不符案例": "CG-WRONG-PICK",
    "标签疑点未确认的结案案例": "CG-TAG-DOUBT",
    "标签异常疑点未确认的结案记录": "CG-TAG-DOUBT",
    "出库流水多录的冲销更正案例": "CG-REV-OVER",
    "出库流水多录冲销的处理案例": "CG-REV-OVER",
    "冲销关联错误的更正案例": "CG-REV-LINK",
    "冲销超时处理的跟进案例": "CG-REV-LATE",
    "草稿出库单跨班次未过账案例": "CG-DRAFT-CROSS",
    "草稿处理超时的跟踪案例": "CG-DRAFT-LATE",
    "草稿误作废后的补登记案例": "CG-DRAFT-VOID",
    "帆布袋外包装受潮的复核与交接案例": "CG-PACK-DAMP",
    "包装破损索赔缺少检查记录案例": "CG-PACK-CLAIM",
    "包装破损索赔缺少检查记录的案例": "CG-PACK-CLAIM",
    "沿用旧版包装流程导致的复核缺口案例": "CG-PACK-VERSION",
    "承运签收材料缺失案例": "CG-RECEIPT-MISS",
    "签收材料延迟回收补录案例": "CG-RECEIPT-LATE",
    "签收材料归档不全案例": "CG-RECEIPT-INCOMPLETE",
    "交接班记录缺少未完成事项案例": "CG-HANDOVER-PENDING",
    "交接记录与系统状态不一致案例": "CG-HANDOVER-INCONSIST",
    "异常事项的升级路径案例": "CG-ESCALATE-PATH",
    "升级结果回填补录案例": "CG-ESCALATE-BACKFILL",
    "记录保存期限到期的复核案例": "CG-ARCHIVE-DUE",
    "归档材料缺件的补正案例": "CG-ARCHIVE-MISS",
    "分批装运的首末批汇总核对案例": "CG-SPLIT-SUM",
    "分批发货与承运延迟的区分案例": "CG-SPLIT-CARRIER",
    "客户催办下的分批进度说明案例": "CG-SPLIT-CUSTOMER",
    "客户进度询问的答复准备案例": "CG-SPLIT-ASK",
    "波次计划临时调整后的批次核对案例": "CG-SPLIT-WAVE",
    "月台误放商品的追回案例": "CG-SPLIT-STAGE",
}

GROUP_DEFS = {
    "CG-SHORT-REC": "货位账面数量记录滞后/不同步导致拣货短少：先按货位分别清点实物、修正记录后补齐发运",
    "CG-SHORT-MULTI": "商品分放多货位而拣货路径不完整导致短少：复查全部相关货位后补拣",
    "CG-SHORT-NOREG": "补货实物已到但未登记入账导致拣货短少：按记录问题补登记后发运",
    "CG-DIFF-LATE": "差异上报超过时限的补登记：补登记说明原因、事项转入待查并加跟踪",
    "CG-DIFF-INSUFF": "差异材料不足的待查处理：列明缺口登记事实、保持待查不提前定性",
    "CG-COUNT-WRONG": "汇总统计口径/合并统计导致差异误报：按明细重算、撤销上报并保留原过程",
    "CG-LINE-MIX": "同订单不同明细数量串记：按明细分别累计并撤销差异上报",
    "CG-NOT-DUE": "未到期订单的误报撤销：先核对约定出库时间，未到期不按逾期处理",
    "CG-STOCK-CHECK": "盘点抽查差异的登记：复盘货位后登记待查，未确认前不调整账面",
    "CG-TAG-SWAP": "货位标签互换/贴错导致扫描不符：核对标签与实物对应关系后更正重扫",
    "CG-WRONG-PICK": "拣货取错货位导致 SKU 不符：退回实物重新拣货，不改原扫描记录",
    "CG-TAG-DOUBT": "标签疑点未确认的结案：材料不足时如实保留未解决状态，不补写原因",
    "CG-REV-OVER": "出库流水多录的冲销更正：核对签收材料后登记冲销，原流水保留可查",
    "CG-REV-LINK": "冲销关联号错误：更正关联关系、保留更正记录，数量差额不受影响",
    "CG-REV-LATE": "冲销超时跟进：补办审批并登记超时原因，增加时限提醒",
    "CG-DRAFT-CROSS": "草稿出库单跨班次未过账：先核对实物与登记数量，逐单确认后过账或作废",
    "CG-DRAFT-LATE": "草稿处理超时跟踪：登记超时、班前检查跟踪、避免继续搁置",
    "CG-DRAFT-VOID": "草稿误作废后的补登记：确认实际出库后重新登记并保留原作废记录",
    "CG-PACK-DAMP": "外包装受潮的复核与交接：检查包装完整性、按复核记录分批放行并补检查留痕",
    "CG-PACK-CLAIM": "包装破损索赔缺少检查记录：证据不足不推定责任，补充后续检查要求",
    "CG-PACK-VERSION": "沿用旧版包装流程导致复核缺口：处理前确认版本有效区间，补充复核重新归档",
    "CG-RECEIPT-MISS": "承运签收材料缺失：登记缺失、跟进补件或确认遗失，不据此确认客户收货",
    "CG-RECEIPT-LATE": "签收材料延迟回收补录：登记延迟、跨班次跟踪并标注补录时间",
    "CG-RECEIPT-INCOMPLETE": "签收材料归档不全：逐项核对清单、补齐材料并更新归档",
    "CG-HANDOVER-PENDING": "交接班记录缺少未完成事项：补登记三类事项并双人确认跟踪",
    "CG-HANDOVER-INCONSIST": "交接记录与系统状态不一致：先逐项核对再更正记录、双人确认后交接",
    "CG-ESCALATE-PATH": "异常事项升级路径：登记事项与范围、交主管协调，建议与执行分开记录",
    "CG-ESCALATE-BACKFILL": "升级结果回填补录：补齐处理结果与时间、状态更新、提交与结果分开记录",
    "CG-ARCHIVE-DUE": "记录保存期限到期复核：期限内核对清单、登记缺件、不擅自销毁",
    "CG-ARCHIVE-MISS": "归档材料缺件补正：逐项核对、登记缺件、补正后双签确认",
    "CG-SPLIT-SUM": "分批装运首末批汇总核对：按明细累计两批流水，确认合计与计划一致",
    "CG-SPLIT-LATE": "分批装运第二批登记延迟：记录分批登记时限、补登并说明延迟",
    "CG-SPLIT-CARRIER": "分批发货与承运延迟的区分：累计已过账流水后判断，分批间隔本身不是异常",
    "CG-SPLIT-CUSTOMER": "客户催办下的分批进度说明：答复限于系统记录、未完成部分不承诺时间",
    "CG-SPLIT-ASK": "客户进度询问的答复准备：整理已出库与未出库记录，按记录答复",
    "CG-SPLIT-WAVE": "波次计划临时调整后的批次核对：按波次逐批核对、补充批次标识",
    "CG-SPLIT-STAGE": "月台误放商品的追回：追回后重新清点、误放原因另行核查",
}

# 逐查询修正（在 v2 标签推导的映射上覆盖；doc 级必要例外）
FIXES = {
    "DEV-C01": {"direct": ["CG-SHORT-REC", "CG-SHORT-NOREG"], "related": ["CG-LINE-MIX", "CG-STOCK-CHECK"]},
    "DEV-C04": {
        "direct": ["CG-PACK-DAMP"],
        "exceptions": {"CASE-0014": 2, "CASE-0016": 2, "CASE-0018": 2, "CASE-0020": 1},
    },
    "DEV-C05": {"direct": ["CG-PACK-DAMP"], "related": [], "exceptions": {"CASE-0001": 1}},
    "DEV-C07": {"direct": ["CG-RECEIPT-LATE"], "related": ["CG-RECEIPT-MISS"]},
    "TEST-C07": {"direct": ["CG-RECEIPT-MISS"], "related": [], "exceptions": {"CASE-0141": 1}},
    "TEST-C10": {
        "direct": ["CG-SPLIT-LATE"],
        "related": ["CG-SPLIT-SUM", "CG-SPLIT-WAVE"],
        "exceptions": {},
    },
    "TEST-C12": {"direct": ["CG-ESCALATE-PATH"], "related": [], "exceptions": {"CASE-0204": 1, "CASE-0210": 1}},
}

SOP_DEFS = {
    "SOP-PACK": "包装异常出库复核",
    "SOP-SKU": "SKU 与扫描不符处理",
    "SOP-DRAFT": "草稿出库单过账时限",
    "SOP-REVERSE": "出库流水冲销更正",
    "SOP-SPLIT": "分批交接核对",
    "SOP-RECEIPT": "签收材料归档",
    "SOP-DIFF": "差异上报时限",
    "SOP-HANDOVER": "交接班记录",
    "SOP-ESCALATE": "异常升级处理",
    "SOP-ARCHIVE": "记录保存期限",
}


def load_docs():
    cases = {
        d["doc_id"]: d
        for d in (json.loads(l) for l in (BASE / "cases.jsonl").read_text(encoding="utf-8").splitlines())
    }
    sops = {
        d["doc_id"]: d
        for d in (json.loads(l) for l in (BASE / "sops.jsonl").read_text(encoding="utf-8").splitlines())
    }
    return cases, sops


def group_of(doc, cases):
    if doc["doc_type"] == "case":
        base = re.sub(r"（[^）]*）", "", doc["title"])
        group = BASE_TO_GROUP[base]
        if group == "CG-SPLIT-SUM" and "登记延迟" in doc["title"]:
            return "CG-SPLIT-LATE"
        return group
    return "SG-" + doc["policy_id"].replace("SOP-", "")


def visible(doc, as_of, scope):
    t = datetime.fromisoformat(as_of)
    if doc["scope"]["warehouse_id"] not in (None, scope.get("warehouse_id")):
        return False
    if scope.get("sku") and doc["scope"]["sku"] not in (None, scope["sku"]):
        return False
    if doc["doc_type"] == "case":
        return (
            datetime.fromisoformat(doc["closed_at"]) <= t
            and datetime.fromisoformat(doc["recorded_at"]) <= t
        )
    if doc["doc_type"] == "sop":
        return (
            datetime.fromisoformat(doc["recorded_at"]) <= t
            and datetime.fromisoformat(doc["effective_from"]) <= t
            and (doc["effective_to"] is None or t < datetime.fromisoformat(doc["effective_to"]))
        )
    return True


def sop_effective(doc, as_of):
    t = datetime.fromisoformat(as_of)
    return datetime.fromisoformat(doc["effective_to"]) > t if doc["effective_to"] else True


def derive_map(query, doc_group, docs):
    direct, related = set(), set()
    for label in query["labels"]:
        doc = docs.get(label["doc_id"])
        if doc is None:
            continue
        group = doc_group(doc)
        if label["grade"] == 2:
            direct.add(group)
        elif label["grade"] == 1:
            related.add(group)
    return {"direct": sorted(direct), "related": sorted(related), "exceptions": {}}


def build(queries, doc_group, cases, sops, dry_run):
    docs = {**cases, **sops}
    effective = {}
    for q in queries:
        qmap = derive_map(q, doc_group, docs)
        fix = FIXES.get(q["qid"])
        if fix:
            for key in ("direct", "related"):
                if key in fix:
                    qmap[key] = sorted(fix[key])
            qmap["exceptions"] = dict(fix.get("exceptions", {}))
        effective[q["qid"]] = qmap

    coverage = {}
    for q in queries:
        pool = cases if q["pool"] == "case" else sops
        qmap = effective[q["qid"]]
        exceptions = qmap["exceptions"]
        labels = []
        for doc in sorted(pool.values(), key=lambda d: d["doc_id"]):
            if not visible(doc, q["as_of"], q["scope"]):
                continue
            group = doc_group(doc)
            if doc["doc_id"] in exceptions:
                grade = exceptions[doc["doc_id"]]
                reason = f"必要例外（{doc['title'][:24]}）：按原文判定等级 {grade}"
            elif q["pool"] == "sop":
                policy = doc["policy_id"]
                sop_group = "SG-" + policy.replace("SOP-", "")
                if sop_group in qmap["direct"]:
                    grade = 2 if sop_effective(doc, q["as_of"]) else 0
                    state = "当时有效" if grade == 2 else "当时不在有效区间"
                    reason = f"{policy}（{SOP_DEFS[policy]}）：{state}，按时间规则判等级 {grade}"
                elif sop_group in qmap["related"]:
                    grade = 1 if sop_effective(doc, q["as_of"]) else 0
                    state = "当时有效" if grade == 1 else "当时不在有效区间"
                    reason = f"{policy}（{SOP_DEFS[policy]}）：相邻流程，{state}，判等级 {grade}"
                else:
                    grade = 0
                    reason = f"{policy}（{SOP_DEFS[policy]}）：与本题流程无交集"
            elif group in qmap["direct"]:
                grade = 2
                reason = f"{group}（{GROUP_DEFS[group]}）：直接回答本题"
            elif group in qmap["related"]:
                grade = 1
                reason = f"{group}（{GROUP_DEFS[group]}）：相关有帮助，不能直接回答本题"
            else:
                grade = 0
                reason = f"{group}（{GROUP_DEFS[group]}）：与本题问题无交集"
            section = "scope" if doc["doc_type"] == "sop" else "result"
            labels.append({"doc_id": doc["doc_id"], "section_id": section, "grade": grade, "reason": reason})
        coverage[q["qid"]] = len(labels)
        q["_v3_labels"] = sorted(labels, key=lambda x: x["doc_id"])
        if not dry_run:
            q["label_version"] = "v3"
            q["labels"] = q["_v3_labels"]
            q["confusable_ids"] = [l["doc_id"] for l in q["labels"] if l["grade"] == 0]

    # 冲突与约束检查（对生成后的 v3 标签）
    problems = []
    for q in queries:
        g2 = [l for l in q["_v3_labels"] if l["grade"] == 2]
        if q["polarity"] == "positive":
            if not g2:
                problems.append(f"{q['qid']}: 正向题无等级 2")
            if len(g2) > 10:
                problems.append(f"{q['qid']}: 等级 2 超过 10 篇（{len(g2)}）")
        elif g2:
            problems.append(f"{q['qid']}: 负向题出现等级 2")
        v2_g2 = set()
        if q.get("_v2_labels"):
            v2_g2 = {l["doc_id"] for l in q["_v2_labels"] if l["grade"] == 2}
        v3_g2 = {l["doc_id"] for l in q["_v3_labels"] if l["grade"] == 2}
        lost = sorted(v2_g2 - v3_g2)
        if lost:
            problems.append(f"{q['qid']}: v2 等级 2 被降级 {lost}")
    return effective, coverage, problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cases, sops = load_docs()

    def doc_group(doc):
        return group_of(doc, cases)

    all_problems = []
    for name in ("dev", "test"):
        v2_queries = [
            json.loads(l)
            for l in (OUT / f"{name}_queries_v2.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        queries = [json.loads(json.dumps(q)) for q in v2_queries]
        for q, v2q in zip(queries, v2_queries):
            q["_v2_labels"] = v2q["labels"]
        effective, coverage, problems = build(
            queries, doc_group, cases, sops, args.dry_run
        )
        all_problems += problems
        total_visible = sum(coverage.values())
        print(f"== {name}: 查询 {len(queries)}，可见标签 {total_visible}，问题 {len(problems)}")
        for q in queries:
            qmap = effective[q["qid"]]
            print(
                f"  {q['qid']:9} direct={qmap['direct']} related={qmap['related']} "
                f"exceptions={qmap['exceptions']} visible={coverage[q['qid']]}"
            )
        if not args.dry_run:
            out_path = OUT / f"{name}_queries_v3.jsonl"
            with out_path.open("w", encoding="utf-8") as fh:
                for q in queries:
                    q.pop("_v2_labels", None)
                    q.pop("_v3_labels", None)
                    fh.write(json.dumps(q, ensure_ascii=False) + "\n")
            print(f"  写出 {out_path}")
    if all_problems:
        print("问题：")
        for p in all_problems:
            print("  -", p)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
