"""T-004b：事件扩量生成（EVX-0025 起，676 篇新事件）。

按明细事实与角度池轮转生成，含 8 对刻意冲突与 12 篇事后补录。
输出 .tmp/t004/events_new.jsonl 与批次记录 .tmp/t004/events_batches.json。
"""

from __future__ import annotations

import json
import re
import zlib
from datetime import datetime, timedelta
from pathlib import Path

from t004.full_facts import AS_OF, SKU_NAMES, Line, load_lines

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TMP = PROJECT_ROOT / ".tmp" / "t004"
PILOT_LINES = {
    "SO-1004/001", "SO-1029/002", "SO-1011/001", "SO-1024/001", "SO-1034/002",
    "SO-1012/002", "SO-1025/002", "SO-1041/001", "SO-1007/002", "SO-1005/001",
    "SO-1006/002", "SO-1038/001", "SO-1001/001", "SO-1055/001", "SO-1014/002",
    "SO-1038/002", "SO-1021/001", "SO-1019/001", "SO-1009/001", "SO-1050/002",
    "SO-1099/001",
}
FORBIDDEN = [
    ("SO-1009", "001"), ("SO-1009", "002"), ("SO-1039", "002"), ("SO-1042", "001"),
    ("SO-1050", "001"), ("SO-1050", "002"), ("SO-1075", "001"), ("SO-1077", "001"),
    ("SO-1083", "001"), ("SO-1099", "001"),
]
CONFLICT_LINES = [
    "SO-1008/001", "SO-1015/001", "SO-1016/001", "SO-1026/001",
    "SO-1031/001", "SO-1040/001", "SO-1053/001", "SO-1062/001",
]
BACKFILL_LINES = [
    "SO-1002/002", "SO-1006/001", "SO-1010/001", "SO-1013/001", "SO-1017/001",
    "SO-1018/001", "SO-1022/001", "SO-1023/002", "SO-1034/001", "SO-1037/001",
    "SO-1043/001", "SO-1048/001",
]
TOTAL_NEW = 676
MAX_PER_LINE = 10

PREAMBLES = [
    "本班次例行核对时记录如下。",
    "按交接流程登记本次情况。",
    "现场核对后整理本记录。",
    "值班期间完成核对，登记如下。",
    "依据当班可查材料整理本记录。",
]
CLOSINGS = [
    "以上为本次登记内容。",
    "后续如有新记录再行补充。",
    "记录供后续班次接续使用。",
    "本次登记以系统内已过账记录为准。",
    "如与其他记录不一致，以复核结论为准。",
]


EXTRA_SENTENCES = [
    "本记录只登记当班核对结果，不改变系统内的原始记录。",
    "相关材料按批次归档，后续如有新增记录再行补充。",
    "如与其他记录不一致，以复核岗位确认的口径为准。",
    "核对范围限于本次登记的对象，不扩展到其他明细。",
    "未完成事项在交班记录中继续跟踪，避免遗漏。",
    "本次登记以系统内已过账记录为准，口头说明只作线索。",
]


def vpick(key: str, n: int) -> int:
    return zlib.crc32(key.encode("utf-8")) % n


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:00+08:00")


def cap(dt: datetime) -> datetime:
    return min(dt, AS_OF)


def sku_name(sku: str) -> str:
    return SKU_NAMES.get(sku, sku)


def qty_word(qty: int) -> str:
    return str(qty)


def line_label(line: Line) -> str:
    return f"{line.order_id} 的 {line.line_id} 明细"


def flow_line(f) -> str:
    return f"{f.tx_id} 记录 {f.qty} 件已过账"


def summary(line: Line, upto: datetime | None = None) -> str:
    parts = []
    for f in line.sales:
        if upto is None or f.event_time <= upto:
            parts.append(f"{f.tx_id} 销售出库 {f.qty} 件已过账")
    for f in line.reversals:
        if upto is None or f.event_time <= upto:
            parts.append(f"{f.tx_id} 冲销 {f.qty} 件已过账")
    for f in line.drafts:
        if upto is None or f.event_time <= upto:
            parts.append(f"{f.tx_id} 草稿 {f.qty} 件未过账")
    if not parts:
        parts.append("系统内暂无流水")
    return "；".join(parts)


def refs(line: Line, *, plan=True, flows=(), notes=()) -> list:
    items = []
    if plan:
        items.append({"file": "deliverables/仓储异常测试数据.xlsx", "sheet": "发货计划", "row": line.plan_row})
    for f in flows:
        items.append({"file": "deliverables/仓储异常测试数据.xlsx", "sheet": "仓储流水", "row": f.row})
    for n in notes:
        items.append({"file": "deliverables/仓储异常测试数据.xlsx", "sheet": "业务说明", "row": n.row})
    return items


def base_time(line: Line) -> datetime:
    if line.flows:
        t = line.flows[-1].event_time + timedelta(minutes=45)
    elif line.agreed_time <= AS_OF:
        t = line.agreed_time - timedelta(hours=2)
    else:
        t = AS_OF - timedelta(hours=3)
    return min(t, AS_OF - timedelta(minutes=30))


def note_qty(content: str) -> int:
    m = re.search(r"发现(\d+)件", content)
    if not m:
        m = re.search(r"(\d+)件", content)
    return int(m.group(1)) if m else 0


def gen_obs(line: Line, angle: str, idx: int, upto: datetime) -> dict:
    key = f"{line.order_id}/{line.line_id}:{angle}:{idx}"
    pre = PREAMBLES[vpick(key + ":pre", len(PREAMBLES))]
    close = CLOSINGS[vpick(key + ":close", len(CLOSINGS))]
    sales = line.sales
    drafts = line.drafts
    revs = line.reversals
    notes = line.notes
    first = sales[0] if sales else None
    second = sales[1] if len(sales) > 1 else None

    if angle == "first_batch":
        obs = (f"{line_label(line)}，商品为{sku_name(line.sku)}，计划 {line.qty} 件。"
               f"当前查到 {flow_line(first)}，条码扫描完成。本记录只登记首批数量，"
               f"不表示整单只发 {first.qty} 件。")
        pend = (f"剩余 {line.qty - first.qty} 件是否安排在后续班次，本记录未取得确认。"
                f"请继续核对该明细的新增流水；同订单其他明细应单独核对，不要并入本明细累计。")
        return {"section_id": "observation", "heading": "本次核对", "text": pre + obs}, \
               {"section_id": "pending", "heading": "后续待核查", "text": pend}
    if angle == "second_batch":
        obs = (f"本次查到 {line_label(line)}的第二条流水 {flow_line(second)}。"
               f"连同此前 {first.tx_id} 的 {first.qty} 件，两批合计 {line.net} 件，与计划数量一致。"
               f"本记录是对首批登记的补充，不是对首批的更正。")
        bnd = ("核对范围限于系统内已过账的销售出库流水；承运签收与客户收货情况未纳入本记录，"
               "不能据此确认客户已收到全部商品。")
        return {"section_id": "followup", "heading": "追加核对", "text": pre + obs}, \
               {"section_id": "boundary", "heading": "核对边界", "text": bnd}
    if angle == "reversal_review":
        rev = revs[0]
        obs = (f"复核签收数量为 {line.net} 件。原流水 {first.tx_id} 初录 {first.qty} 件，"
               f"已通过 {rev.tx_id} 冲销多录的 {rev.qty} 件，原流水保留可查。"
               f"当前该明细有效出库量为 {line.net} 件，与计划一致。")
        bnd = ("冲销只更正原流水数量，不改变原记录。本记录不解释多录原因；"
               "后续如需追溯，请查原始复核材料。")
        return {"section_id": "observation", "heading": "本次复核", "text": pre + obs}, \
               {"section_id": "boundary", "heading": "记录边界", "text": bnd}
    if angle == "draft_track":
        draft = drafts[0]
        obs = (f"{line_label(line)}计划 {line.qty} 件，当前 {draft.tx_id} 仍为草稿状态，尚未过账。"
               f"草稿单已登记出库 {draft.qty} 件，但草稿与已过账不同，"
               f"不能作为已完成出库或库存已减少的依据。")
        pend = ("等待仓库复核并过账。在过账前，本明细的有效出库量按 0 处理；"
                "若下一班次仍未过账，交异常专员登记跟踪，不把草稿数量直接补进完成量。")
        return {"section_id": "observation", "heading": "本次核对", "text": pre + obs}, \
               {"section_id": "pending", "heading": "后续待办", "text": pend}
    if angle == "sku_scan_mismatch":
        flow = line.mismatching_flows[0]
        obs = (f"{line_label(line)}计划商品为{sku_name(line.sku)}（{line.sku}）{line.qty} 件。"
               f"{flow.tx_id} 记录的扫描出库商品为{sku_name(flow.sku)}（{flow.sku}）{flow.qty} 件，"
               f"与计划 SKU 不一致，该流水未计入本明细匹配出库。")
        pend = ("请拣货与复核岗位核对实物与单据。本记录不判断是错发、串单还是登记错误，"
                "也不把该流水数量计入本明细的完成数量；更正前保留原扫描记录。")
        return {"section_id": "observation", "heading": "本次核对", "text": pre + obs}, \
               {"section_id": "pending", "heading": "后续待办", "text": pend}
    if angle == "damage_inspection":
        note = notes[0]
        qty = note_qty(note.content)
        sent = first.qty if first else line.net
        obs = (f"拣货时发现 {qty} 件{sku_name(line.sku)}外包装破损，已移入待检区，与正常商品分开存放。"
               f"本批先发出 {sent} 件，{flow_line(first)}。破损的 {qty} 件尚未取得质量复核结论，"
               f"也未安排放行或替换。")
        pend = ("请质量复核员按现行包装异常流程记录检查结论，并注明适用对象与处理意见。"
                "在结论明确前，本明细剩余件数保持待查，不视为已发出，也不视为已报废。")
        return {"section_id": "observation", "heading": "本次观察", "text": pre + obs}, \
               {"section_id": "pending", "heading": "后续待办", "text": pend}
    if angle == "mislabel_return":
        note = notes[0]
        qty = note_qty(note.content)
        sent = first.qty if first else line.net
        obs = (f"复核发现 {qty} 件标签贴错，已退回重新贴标，与已发商品分开存放。"
               f"本批先发 {sent} 件，{flow_line(first)}。退回商品的更正结果尚未回传本班次。")
        pend = ("请标签岗位确认重贴结果并回传；更正完成前，退回数量不计入已发出，"
                "也不计入报废，原复核记录保留。")
        return {"section_id": "observation", "heading": "本次观察", "text": pre + obs}, \
               {"section_id": "pending", "heading": "后续待办", "text": pend}
    if angle == "misplace_recover":
        obs = (f"装车时发现部分商品被误放至相邻月台，已追回并重新清点。"
               f"本次实发 {first.qty} 件，{flow_line(first)}；追回过程未发现数量缺失。")
        bnd = "本记录只覆盖本次追回与清点，不解释误放原因；后续核对以已过账流水为准。"
        return {"section_id": "observation", "heading": "本次观察", "text": pre + obs}, \
               {"section_id": "boundary", "heading": "记录边界", "text": bnd}
    if angle == "normal_check":
        variants = [
            f"{line_label(line)}计划 {line.qty} 件，{flow_line(first)}，数量与 SKU 均与计划一致，没有待处理差异。",
            f"核对 {line_label(line)}：计划 {line.qty} 件，{flow_line(first)}。数量、SKU 均与计划一致。",
            f"{line_label(line)}的{qty_word(line.qty)}件已完成出库核对，{flow_line(first)}，与计划一致。",
        ]
        obs = variants[vpick(key + ":nc", len(variants))]
        bnd = "本记录只确认系统内已过账记录与计划一致；承运签收材料未纳入本次核对。"
        return {"section_id": "observation", "heading": "本次核对", "text": pre + obs}, \
               {"section_id": "boundary", "heading": "核对边界", "text": bnd}
    if angle == "not_due_wait":
        obs = (f"{line_label(line)}计划 {line.qty} 件，约定出库时间为 "
               f"{line.agreed_time.strftime('%m 月 %d 日 %H:%M')}。截至本记录时点，"
               f"该明细没有已过账流水，也没有待过账草稿。")
        pend = ("约定时间未到，暂不判逾期。请到期后再核对出库记录；"
                "本记录不把没有流水解释为缺货或漏发，也不作为上报短缺的依据。")
        return {"section_id": "observation", "heading": "本次核对", "text": pre + obs}, \
               {"section_id": "pending", "heading": "后续待办", "text": pend}
    if angle == "insufficient_pending":
        obs = (f"{line_label(line)}计划 {line.qty} 件，{flow_line(first)}，差异 {line.diff} 件。"
               f"系统内没有该明细的进一步说明记录，交接材料也未附其他说明。")
        pend = ("现有资料不足以确定差异原因，列入待查。请补充拣货、复核或交接材料后再判断，"
                "不预设缺货、破损或漏发等结论。")
        return {"section_id": "observation", "heading": "本次核对", "text": pre + obs}, \
               {"section_id": "pending", "heading": "待查事项", "text": pend}
    if angle == "missing_receipt":
        variants = [
            f"整理 {line.order_id}/{line.line_id} 批次材料时未找到对应的承运签收单，交接记录中未附签收凭证。系统内 {flow_line(first)}。",
            f"{line.order_id}/{line.line_id} 的承运签收材料缺失，现有材料只到交接环节；系统内 {flow_line(first)}。",
            f"归档检查发现 {line.order_id}/{line.line_id} 缺少承运签收单；系统内 {flow_line(first)}。",
        ]
        obs = variants[vpick(key + ":mr", len(variants))]
        pend = ("请承运对接岗位补充或确认签收材料。在签收材料补齐前，只能确认系统内已过账，"
                "不能据此确认客户已收到；若签收单确认遗失，应登记说明并明确责任岗位。")
        return {"section_id": "observation", "heading": "本次核对", "text": pre + obs}, \
               {"section_id": "pending", "heading": "后续待办", "text": pend}
    if angle == "shift_handover":
        s = summary(line, upto)
        variants = [
            f"交班前复核 {line.order_id}/{line.line_id}：计划 {line.qty} 件，{s}。本班次未发现新的差异记录。",
            f"本班次交接核对 {line.order_id}/{line.line_id}：计划 {line.qty} 件，{s}。班次内记录完整。",
            f"交接前查看 {line.order_id}/{line.line_id} 的完成情况：计划 {line.qty} 件，{s}。未见新增差异。",
        ]
        obs = variants[vpick(key + ":sh", len(variants))]
        pend = "下一班次请继续跟踪该明细的材料与流水，未完成的待办在交班记录中说明。"
        return {"section_id": "observation", "heading": "交班复核", "text": pre + obs}, \
               {"section_id": "pending", "heading": "交班事项", "text": pend}
    if angle == "customer_inquiry":
        variants = [
            f"客户询问 {line.order_id} 的出库进度。核对系统记录：{summary(line, upto)}。本记录只整理可提供的进度信息，不对未完成部分承诺时间。",
            f"为答复 {line.order_id} 的进度询问，先核对系统：{summary(line, upto)}。答复内容限于已记录事实。",
            f"{line.order_id} 进度准备：{summary(line, upto)}。可说明的部分以系统记录为准，未完成部分不承诺时间。",
        ]
        obs = variants[vpick(key + ":ci", len(variants))]
        bnd = "对外答复由主管统一说明；本记录不作为对客户的正式答复。"
        return {"section_id": "observation", "heading": "客户询问准备", "text": pre + obs}, \
               {"section_id": "boundary", "heading": "答复边界", "text": bnd}
    if angle == "stocktake_spot":
        variants = [
            f"本班次对 {line.warehouse} 相关货位做例行抽查，核对 {line.order_id}/{line.line_id} 的{sku_name(line.sku)}数量与系统记录。{summary(line, upto)}。抽查范围内未发现新的实物差异。",
            f"例行盘点抽查覆盖 {line.order_id}/{line.line_id}：{summary(line, upto)}，货位实物与记录一致。",
            f"盘点员抽查 {line.order_id}/{line.line_id} 对应货位：{summary(line, upto)}，未发现实物差异。",
        ]
        obs = variants[vpick(key + ":st", len(variants))]
        bnd = "抽查只覆盖当次货位与单据，不代表全仓盘点结论。"
        return {"section_id": "observation", "heading": "盘点抽查", "text": pre + obs}, \
               {"section_id": "boundary", "heading": "抽查边界", "text": bnd}
    if angle == "doc_review":
        s = summary(line, upto)
        variants = [
            f"复核 {line.order_id}/{line.line_id} 的出库单据与流水：{s}。单据要素齐全，数量与系统记录一致。",
            f"整理 {line.order_id}/{line.line_id} 单据时核对系统记录：{s}。单据与流水未发现不一致。",
            f"单据复核：{line.order_id}/{line.line_id} 计划 {line.qty} 件，{s}。要素与数量核对完成。",
        ]
        obs = variants[vpick(key + ":dr", len(variants))]
        bnd = "本记录只覆盖系统内单据与流水的一致性，不替代签收材料核对。"
        return {"section_id": "observation", "heading": "单据复核", "text": pre + obs}, \
               {"section_id": "boundary", "heading": "核对边界", "text": bnd}
    if angle == "carrier_confirm":
        s = summary(line, upto)
        variants = [
            f"与承运对接确认 {line.order_id}/{line.line_id} 的装车情况：{s}。承运方未反馈异常。",
            f"承运对接岗回复 {line.order_id}/{line.line_id}：{s}。装车信息与交接记录一致。",
            f"询问承运方 {line.order_id}/{line.line_id} 的运输状态：{s}。暂无异常反馈。",
        ]
        obs = variants[vpick(key + ":cc", len(variants))]
        bnd = "口头确认不作为签收凭证；正式签收材料仍需归档。"
        return {"section_id": "observation", "heading": "承运确认", "text": pre + obs}, \
               {"section_id": "boundary", "heading": "记录边界", "text": bnd}
    if angle == "picking_done":
        qty = first.qty if first else line.qty
        variants = [
            f"拣货完成登记：{line.order_id}/{line.line_id} 计划 {line.qty} 件，本次拣取 {qty} 件，货位与条码核对完成。",
            f"本次拣取 {line.order_id}/{line.line_id} 共 {qty} 件，货位数量与拣货单一致，待复核过账。",
            f"拣货作业完成：{line.order_id}/{line.line_id} 计划 {line.qty} 件，实拣 {qty} 件，条码扫描通过。",
        ]
        obs = variants[vpick(key + ":pd", len(variants))]
        pend = "待复核过账后完成本明细的出库确认；拣货数量不直接等于已出库数量。"
        return {"section_id": "observation", "heading": "拣货登记", "text": pre + obs}, \
               {"section_id": "pending", "heading": "后续待办", "text": pend}
    if angle == "loading_done":
        variants = [
            f"装车完成登记：{line.order_id}/{line.line_id} 本次装车 {first.qty} 件，{flow_line(first)}，车辆离仓。",
            f"{line.order_id}/{line.line_id} 装车完毕，{flow_line(first)}，交接单已签。",
            f"本次装车 {first.qty} 件（{line.order_id}/{line.line_id}），{flow_line(first)}，随后车辆离仓。",
        ]
        obs = variants[vpick(key + ":ld", len(variants))]
        bnd = "装车完成不代表客户已签收；签收材料由承运对接岗位归档。"
        return {"section_id": "observation", "heading": "装车登记", "text": pre + obs}, \
               {"section_id": "boundary", "heading": "记录边界", "text": bnd}
    if angle == "system_status":
        variants = [
            f"系统状态核对：{line.order_id}/{line.line_id} 计划 {line.qty} 件，{summary(line, upto)}。系统标记与实物记录未发现新的不一致。",
            f"查看 {line.order_id}/{line.line_id} 的系统状态：计划 {line.qty} 件，{summary(line, upto)}。标记正常。",
            f"系统与实物对照（{line.order_id}/{line.line_id}）：{summary(line, upto)}，未发现新的差异标记。",
        ]
        obs = variants[vpick(key + ":ss", len(variants))]
        bnd = "系统状态只反映账面记录，实物状态以现场复核为准。"
        return {"section_id": "observation", "heading": "系统核对", "text": pre + obs}, \
               {"section_id": "boundary", "heading": "核对边界", "text": bnd}
    if angle == "bin_recheck":
        variants = [
            f"货位复核：{line.order_id}/{line.line_id} 对应货位的{sku_name(line.sku)}数量与系统显示核对。{summary(line, upto)}。货位标签与实物对应正常。",
            f"复查 {line.order_id}/{line.line_id} 所在货位：实物数量与系统显示一致，{summary(line, upto)}。",
            f"货位与标签抽查（{line.order_id}/{line.line_id}）：数量核对完成，{summary(line, upto)}，未发现错位。",
        ]
        obs = variants[vpick(key + ":br", len(variants))]
        bnd = "本记录只覆盖当次货位，不扩展至其他库位。"
        return {"section_id": "observation", "heading": "货位复核", "text": pre + obs}, \
               {"section_id": "boundary", "heading": "复核边界", "text": bnd}
    if angle == "pre_shift_check":
        variants = [
            f"班前检查：查看 {line.order_id}/{line.line_id} 的未完成事项，{summary(line, upto)}。本班次优先处理待过账与待检记录。",
            f"接班后先核对 {line.order_id}/{line.line_id}：{summary(line, upto)}。待办事项列入班次计划。",
            f"班次开始前梳理 {line.order_id}/{line.line_id}：{summary(line, upto)}，未完成事项优先跟进。",
        ]
        obs = variants[vpick(key + ":ps", len(variants))]
        pend = "交班时同步当前状态，避免草稿或待检记录跨班次遗漏。"
        return {"section_id": "observation", "heading": "班前检查", "text": pre + obs}, \
               {"section_id": "pending", "heading": "班次事项", "text": pend}
    if angle == "escalation_note":
        variants = [
            f"本明细存在未完成事项：{summary(line, upto)}。按现行流程提交相应岗位跟进，未自行处理数量或状态。",
            f"{line.order_id}/{line.line_id} 有事项需要跟进：{summary(line, upto)}。已提交对应岗位，等待回填。",
            f"待办升级：{line.order_id}/{line.line_id}（{summary(line, upto)}）移交相应岗位处理。",
        ]
        obs = variants[vpick(key + ":en", len(variants))]
        pend = "跟进结果回填后更新记录；在结论前不改变账面数量，也不把待办写成已完成。"
        return {"section_id": "observation", "heading": "事项升级", "text": pre + obs}, \
               {"section_id": "pending", "heading": "后续待办", "text": pend}
    if angle == "archive_note":
        visible_ids = [f.tx_id for f in line.flows if f.event_time <= upto]
        visible_ids += [n.ev_id for n in line.notes if n.record_time <= upto]
        visible_ids = sorted(set(visible_ids))
        ids = "、".join(visible_ids[:4]) if visible_ids else "无"
        variants = [
            f"材料归档：{line.order_id}/{line.line_id} 的流水与说明已按记录保存要求归档，包含 {ids}。归档清单与系统记录一致。",
            f"{line.order_id}/{line.line_id} 本批材料整理完毕，{ids} 已归入批次档案，清单与系统一致。",
            f"归档登记：{line.order_id}/{line.line_id} 相关记录（{ids}）已按要求保存，未发现缺件。",
        ]
        obs = variants[vpick(key + ":an", len(variants))]
        bnd = "归档只证明材料已保存，不证明业务结论成立。"
        return {"section_id": "observation", "heading": "材料归档", "text": pre + obs}, \
               {"section_id": "boundary", "heading": "归档说明", "text": bnd}
    if angle == "cross_line_reminder":
        variants = [
            f"明细隔离提醒：{line.order_id} 含多个明细，本次只核对 {line.line_id}。其他明细的流水与说明不并入本明细统计。",
            f"{line.order_id} 汇总时注意：本次口径仅限 {line.line_id} 明细，其他明细单独核对。",
            f"核对范围说明：{line.order_id} 的 {line.line_id} 明细单独统计，不与同订单其他明细合并。",
        ]
        obs = variants[vpick(key + ":cl", len(variants))]
        pend = "汇总时按明细分别累计；商品相同也不能合并。"
        return {"section_id": "observation", "heading": "明细隔离", "text": pre + obs}, \
               {"section_id": "pending", "heading": "汇总要求", "text": pend}
    raise ValueError(angle)


def angles_for(line: Line) -> list[str]:
    tags = line.tags
    if "insufficient" in tags:
        # 冻结约束：禁补明细只允许仍待查记录，不生成其他运营记录。
        return ["insufficient_pending"]
    pool = []
    if "split" in tags:
        pool += ["first_batch", "second_batch", "shift_handover", "customer_inquiry",
                 "doc_review", "loading_done", "archive_note", "cross_line_reminder"]
    if "reversal" in tags:
        pool += ["reversal_review", "shift_handover", "doc_review", "archive_note", "customer_inquiry"]
    if "draft_only" in tags:
        pool += ["draft_track", "pre_shift_check", "escalation_note", "shift_handover", "doc_review"]
    if "sku_mismatch" in tags:
        pool += ["sku_scan_mismatch", "bin_recheck", "escalation_note", "shift_handover", "doc_review"]
    if "not_due" in tags:
        pool += ["not_due_wait", "shift_handover", "pre_shift_check", "customer_inquiry"]
    if line.notes:
        content = line.notes[0].content
        if "破损" in content:
            pool += ["damage_inspection", "shift_handover", "doc_review", "bin_recheck", "archive_note"]
        elif "标签贴错" in content:
            pool += ["mislabel_return", "bin_recheck", "shift_handover", "doc_review", "archive_note"]
        elif "误放" in content:
            pool += ["misplace_recover", "shift_handover", "doc_review", "loading_done", "archive_note"]
    if "matched" in tags:
        pool += ["normal_check", "shift_handover", "doc_review", "carrier_confirm", "picking_done",
                 "loading_done", "system_status", "bin_recheck", "pre_shift_check", "archive_note",
                 "missing_receipt", "stocktake_spot", "customer_inquiry", "cross_line_reminder",
                 "escalation_note"]
    # 去重且保持顺序
    seen, ordered = set(), []
    for a in pool:
        if a not in seen:
            seen.add(a)
            ordered.append(a)
    return ordered


def pad_sections(key: str, sections: list, minimum: int = 120) -> list:
    from t004.text_utils import sentence_dedupe

    for s in sections:
        s["text"] = sentence_dedupe(s["text"])
    total = sum(len(s["text"]) for s in sections)
    offset = vpick(key, len(EXTRA_SENTENCES))
    for step in range(len(EXTRA_SENTENCES)):
        if total >= minimum:
            break
        sentence = EXTRA_SENTENCES[(offset + step) % len(EXTRA_SENTENCES)]
        core = sentence.rstrip("。")
        if any(core in s["text"] for s in sections):
            continue
        sections[-1]["text"] += sentence
        total += len(sentence)
    return sections


def make_event(line: Line, angle: str, idx: int, occurred: datetime, recorded: datetime,
               sections: list, txs: list, refs_list: list, role: str) -> dict:
    sections = pad_sections(f"{line.order_id}/{line.line_id}:{angle}:{idx}", sections)
    return {
        "doc_id": "",  # 由主流程分配
        "doc_type": "event",
        "title": f"{line.order_id}/{line.line_id} {angle_title(angle)}",
        "recorded_at": fmt(cap(recorded)),
        "occurred_at": fmt(occurred),
        "author_role": role,
        "scope": {"warehouse_id": line.warehouse, "sku": line.sku,
                  "order_id": line.order_id, "line_id": line.line_id},
        "transaction_ids": txs,
        "source_refs": refs_list,
        "sections": sections,
        "synthetic": True,
    }


ANGLE_TITLES = {
    "first_batch": "首批装车登记", "second_batch": "第二批装车补充核对",
    "reversal_review": "冲销复核登记", "draft_track": "草稿状态跟踪",
    "sku_scan_mismatch": "扫描 SKU 不符登记", "damage_inspection": "破损商品待检登记",
    "mislabel_return": "标签退回重贴登记", "misplace_recover": "误放商品追回登记",
    "normal_check": "出库核对完成", "not_due_wait": "未到期待办",
    "insufficient_pending": "数量差异待查", "missing_receipt": "签收材料缺失",
    "shift_handover": "交班复核", "customer_inquiry": "客户询问准备",
    "stocktake_spot": "货位抽查", "doc_review": "单据复核",
    "carrier_confirm": "承运确认", "picking_done": "拣货登记",
    "loading_done": "装车登记", "system_status": "系统状态核对",
    "bin_recheck": "货位复核", "pre_shift_check": "班前检查",
    "escalation_note": "事项升级", "archive_note": "材料归档",
    "cross_line_reminder": "明细隔离提醒",
}


def angle_title(angle: str) -> str:
    return ANGLE_TITLES.get(angle, angle)


ROLES = {
    "first_batch": "仓库交接员", "second_batch": "仓库复核员", "reversal_review": "仓库复核员",
    "draft_track": "仓库复核员", "sku_scan_mismatch": "仓库复核员",
    "damage_inspection": "仓库交接员", "mislabel_return": "仓库复核员",
    "misplace_recover": "仓库交接员", "normal_check": "仓库复核员",
    "not_due_wait": "仓库复核员", "insufficient_pending": "仓储异常专员",
    "missing_receipt": "仓储异常专员", "shift_handover": "仓库交接员",
    "customer_inquiry": "仓储异常专员", "stocktake_spot": "仓库盘点员",
    "doc_review": "仓库复核员", "carrier_confirm": "承运对接岗",
    "picking_done": "仓库拣货员", "loading_done": "仓库交接员",
    "system_status": "仓库复核员", "bin_recheck": "仓库盘点员",
    "pre_shift_check": "班次组长", "escalation_note": "仓储异常专员",
    "archive_note": "仓储异常专员", "cross_line_reminder": "仓库复核员",
}


def angle_times(line: Line, angle: str, idx: int):
    key = f"{line.order_id}/{line.line_id}:{angle}:{idx}"
    first = line.sales[0] if line.sales else None
    if angle == "first_batch" and first:
        occurred = first.event_time + timedelta(minutes=25)
    elif angle == "second_batch" and len(line.sales) > 1:
        occurred = line.sales[1].event_time + timedelta(minutes=20)
    elif angle == "reversal_review" and line.reversals:
        occurred = line.reversals[0].event_time + timedelta(minutes=15)
    elif angle == "draft_track" and line.drafts:
        occurred = line.drafts[0].event_time + timedelta(minutes=30)
    elif angle == "sku_scan_mismatch" and line.mismatching_flows:
        occurred = line.mismatching_flows[0].event_time + timedelta(minutes=40)
    elif angle in ("damage_inspection", "mislabel_return", "misplace_recover") and line.flows:
        occurred = line.flows[0].event_time + timedelta(minutes=35)
    elif angle == "not_due_wait":
        occurred = AS_OF - timedelta(minutes=40)
    else:
        occurred = base_time(line) - timedelta(hours=3 * (idx % 4))
    recorded = occurred + timedelta(minutes=20 + (vpick(key, 70)))
    return occurred, recorded


def essential_time(line: Line, angle: str):
    """事件正文必须依赖的来源时间（用于把事件顺延到来源之后）。"""
    if angle == "first_batch" and line.sales:
        return line.sales[0].event_time
    if angle == "second_batch" and len(line.sales) > 1:
        return line.sales[1].event_time
    if angle == "reversal_review" and line.reversals:
        return line.reversals[0].event_time
    if angle == "draft_track" and line.drafts:
        return line.drafts[0].event_time
    if angle == "sku_scan_mismatch" and line.mismatching_flows:
        return line.mismatching_flows[0].event_time
    if angle in ("damage_inspection", "mislabel_return", "misplace_recover"):
        times = [n.record_time for n in line.notes] + [f.event_time for f in line.sales[:1]]
        return max(times) if times else None
    if angle == "insufficient_pending" and line.sales:
        return line.sales[0].event_time
    if angle in ("normal_check", "loading_done", "picking_done", "missing_receipt") and line.sales:
        return line.sales[0].event_time
    return None


def angle_txs(line: Line, angle: str, recorded: datetime | None = None) -> list:
    def vis(flows):
        return [f for f in flows if recorded is None or f.event_time <= recorded]

    if angle == "first_batch" and line.sales:
        return [f.tx_id for f in vis(line.sales[:1])]
    if angle == "second_batch" and len(line.sales) > 1:
        return [f.tx_id for f in vis(line.sales[:2])]
    if angle == "reversal_review":
        return [f.tx_id for f in vis(line.sales + line.reversals)]
    if angle == "draft_track" and line.drafts:
        return [f.tx_id for f in vis(line.drafts)]
    if angle == "sku_scan_mismatch" and line.mismatching_flows:
        return [f.tx_id for f in vis(line.mismatching_flows)]
    if angle in ("normal_check", "loading_done", "carrier_confirm") and line.sales:
        return [f.tx_id for f in vis(line.sales[:1])]
    return [f.tx_id for f in vis(line.sales + line.drafts + line.reversals)]


def angle_refs(line: Line, angle: str, recorded: datetime | None = None) -> list:
    def vf(flows):
        return [f for f in flows if recorded is None or f.event_time <= recorded]

    def vn(notes):
        return [n for n in notes if recorded is None or n.record_time <= recorded]

    if angle == "first_batch" and line.sales:
        return refs(line, flows=vf(line.sales[:1]))
    if angle == "second_batch" and len(line.sales) > 1:
        return refs(line, flows=vf(line.sales[:2]))
    if angle == "reversal_review":
        return refs(line, flows=vf(line.sales + line.reversals), notes=vn(line.notes))
    if angle == "draft_track" and line.drafts:
        return refs(line, flows=vf(line.drafts), notes=vn(line.notes))
    if angle == "sku_scan_mismatch" and line.mismatching_flows:
        return refs(line, flows=vf(line.mismatching_flows))
    if angle in ("damage_inspection", "mislabel_return", "misplace_recover"):
        return refs(line, flows=vf(line.sales[:1]), notes=vn(line.notes))
    if angle == "insufficient_pending" and line.sales:
        return refs(line, flows=vf(line.sales[:1]))
    if angle == "archive_note":
        return refs(line, flows=vf(line.flows), notes=vn(line.notes))
    return refs(line, flows=vf(line.flows), notes=vn(line.notes[:1]))


def conflict_events(line: Line, pair_index: int) -> list:
    """两篇同对象同时点相反说法（刻意设计）。"""
    if line.sales:
        tx = line.sales[0]
        q = line.qty
        t = tx.event_time + timedelta(minutes=20)
        obs_a = (f"{line_label(line)}计划 {q} 件，{tx.tx_id} 记录 {q} 件已装车交接，单据已签收。"
                 f"本班次确认该明细全部 {q} 件完成交接，未发现数量差异。")
        bnd_a = "本记录只依据本班次交接情况填写，未复核其他班次记录。"
        b_variants = [
            f"现场复核时，货位仍有部分商品未装车，本班次实际只交接了 {max(q - 3, 1)} 件，与交接记录填写的 {q} 件不一致。该明细尚未全部完成交接。",
            f"清点现场后确认，本班次只装车 {max(q - 3, 1)} 件，其余仍在货位；交接单上的 {q} 件与实物不符。",
            f"按实物清点，本班次完成 {max(q - 3, 1)} 件，另有部分未装车；系统记录的 {q} 件尚未核实。",
        ]
        obs_b = b_variants[pair_index % len(b_variants)]
        pend_b = "两处说法不一致，暂不合并统计，也不以任一方数量作为完成量，待复核结论。"
    else:
        t = AS_OF - timedelta(hours=2)
        obs_a = (f"{line_label(line)}已按计划完成出库，系统记录与交接单据一致，"
                 f"本班次未发现差异。")
        bnd_a = "本记录依据当班可查记录填写。"
        obs_b = (f"{line_label(line)}仍有部分商品未完成交接，现场数量与系统记录不一致，"
                 f"暂不能按完成处理。")
        pend_b = "两处说法不一致，暂不合并统计，待复核结论。"
    return [
        {
            "doc_id": "",
            "doc_type": "event",
            "title": f"{line.order_id}/{line.line_id} 交接完成确认",
            "recorded_at": fmt(cap(t + timedelta(minutes=10))),
            "occurred_at": fmt(t),
            "author_role": "仓库交接员",
            "scope": {"warehouse_id": line.warehouse, "sku": line.sku,
                      "order_id": line.order_id, "line_id": line.line_id},
            "transaction_ids": [f.tx_id for f in line.sales[:1]],
            "source_refs": refs(line, flows=line.sales[:1]),
            "sections": [
                {"section_id": "observation", "heading": "本次交接", "text": obs_a},
                {"section_id": "boundary", "heading": "记录边界", "text": bnd_a},
            ],
            "synthetic": True,
        },
        {
            "doc_id": "",
            "doc_type": "event",
            "title": f"{line.order_id}/{line.line_id} 现场数量复核",
            "recorded_at": fmt(cap(t + timedelta(minutes=20))),
            "occurred_at": fmt(t),
            "author_role": "仓库复核员",
            "scope": {"warehouse_id": line.warehouse, "sku": line.sku,
                      "order_id": line.order_id, "line_id": line.line_id},
            "transaction_ids": [f.tx_id for f in line.sales[:1]],
            "source_refs": refs(line, flows=line.sales[:1]),
            "sections": [
                {"section_id": "observation", "heading": "本次复核", "text": obs_b},
                {"section_id": "pending", "heading": "后续待办", "text": pend_b},
            ],
            "synthetic": True,
        },
    ]


def backfill_event(line: Line, idx: int) -> dict:
    if line.sales:
        flow = line.sales[0]
        occurred = flow.event_time + timedelta(minutes=15)
    else:
        occurred = AS_OF - timedelta(days=6)
    recorded = cap(occurred + timedelta(days=3 + idx % 3, hours=2))
    if recorded <= occurred:
        occurred = recorded - timedelta(days=2)
    obs = (f"本记录补录 {occurred.month} 月 {occurred.day} 日发生的情况：{summary(line, recorded)}。"
           f"该情况当时已口头交接，本记录于 {recorded.month} 月 {recorded.day} 日补录归档。")
    pend = ("补录仅覆盖当时记录的事实；原因与后续结论未取得，请按现行流程核查。"
            "因本记录晚于事发时间，追溯事发附近时点的调查不应引用本记录。")
    return {
        "doc_id": "",
        "doc_type": "event",
        "title": f"{line.order_id}/{line.line_id} 情况补录",
        "recorded_at": fmt(recorded),
        "occurred_at": fmt(occurred),
        "author_role": "仓储异常专员",
        "scope": {"warehouse_id": line.warehouse, "sku": line.sku,
                  "order_id": line.order_id, "line_id": line.line_id},
        "transaction_ids": [f.tx_id for f in line.flows if f.event_time <= recorded][:3],
        "source_refs": refs(
            line,
            flows=[f for f in line.flows if f.event_time <= recorded],
            notes=[n for n in line.notes if n.record_time <= recorded][:1],
        ),
        "sections": [
            {"section_id": "observation", "heading": "补录内容", "text": obs},
            {"section_id": "pending", "heading": "补录说明与待办", "text": pend},
        ],
        "synthetic": True,
    }


def main() -> int:
    lines = load_lines()
    new_lines = {k: v for k, v in lines.items() if k not in PILOT_LINES}
    conflict_set = set(CONFLICT_LINES)
    backfill_set = set(BACKFILL_LINES) - conflict_set

    events: list = []

    # 1) 刻意冲突对（8 对 = 16 篇）
    for i, key in enumerate(CONFLICT_LINES):
        events.extend(conflict_events(lines[key], i))

    # 2) 事后补录（12 篇）
    for i, key in enumerate(BACKFILL_LINES):
        if key in lines:
            events.append(backfill_event(lines[key], i))

    # 3) 轮转生成其余事件
    pool_lines = [k for k in new_lines if k not in conflict_set]
    pool_lines.sort()
    remaining = TOTAL_NEW - len(events)
    cursor = {k: 0 for k in pool_lines}
    allocated = 0
    pass_index = 0
    while allocated < remaining and pass_index < MAX_PER_LINE:
        progressed = False
        for key in pool_lines:
            if allocated >= remaining:
                break
            line = new_lines[key]
            candidates = angles_for(line)
            if candidates:
                offset = vpick(key + ":rot", len(candidates))
                candidates = candidates[offset:] + candidates[:offset]
            if pass_index >= len(candidates):
                continue
            angle = candidates[pass_index]
            idx = cursor[key]
            cursor[key] = idx + 1
            occurred, recorded = angle_times(line, angle, idx)
            if angle == "not_due_wait":
                occurred = min(occurred, AS_OF - timedelta(minutes=30))
                recorded = min(recorded, AS_OF)
            essential = essential_time(line, angle)
            if essential:
                occurred = max(occurred, essential + timedelta(minutes=15))
                recorded = max(recorded, occurred + timedelta(minutes=10))
            recorded = min(recorded, AS_OF)
            obs, second = gen_obs(line, angle, idx, recorded)
            events.append(
                make_event(
                    line, angle, idx, occurred, recorded, [obs, second],
                    angle_txs(line, angle, recorded), angle_refs(line, angle, recorded), ROLES[angle],
                )
            )
            allocated += 1
            progressed = True
        if not progressed:
            break
        pass_index += 1

    # 4) 统一补足短文本（冲突对与补录事件同样适用）
    for e in events:
        e["sections"] = pad_sections(e["title"] + e["occurred_at"], e["sections"], 120)

    # 5) 分配 ID（先冲突对与补录按生成顺序，其余随后）
    for i, event in enumerate(events, start=25):
        event["doc_id"] = f"EVX-{i:04d}"

    TMP.mkdir(parents=True, exist_ok=True)
    out = TMP / "events_new.jsonl"
    out.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")

    batches = []
    for b, start in enumerate(range(0, len(events), 100)):
        chunk = events[start:start + 100]
        batches.append({
            "batch": f"E{b + 1}",
            "doc_id_range": [chunk[0]["doc_id"], chunk[-1]["doc_id"]],
            "count": len(chunk),
            "method": "会话内模型生成（模板+事实槽位）",
        })
    (TMP / "events_batches.json").write_text(json.dumps(batches, ensure_ascii=False, indent=2), encoding="utf-8")
    print("new events:", len(events))
    covered = len({(e["scope"]["order_id"], e["scope"]["line_id"]) for e in events})
    print("covered lines (new):", covered)
    from collections import Counter
    print("angle distribution:", Counter(e["title"].split()[-1] for e in events).most_common(8))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
