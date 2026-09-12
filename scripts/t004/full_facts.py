"""T-004b：从只读工作簿提取每明细事实（不连接数据库）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import openpyxl

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
XLSX = PROJECT_ROOT / "deliverables" / "仓储异常测试数据.xlsx"
AS_OF = datetime(2026, 9, 9, 20, 0)
SKU_NAMES = {
    "MUG-BL": "蓝色陶瓷杯",
    "MUG-WH": "白色陶瓷杯",
    "BAG-KH": "卡其色帆布袋",
    "BAG-BK": "黑色帆布袋",
}


@dataclass
class Flow:
    row: int
    tx_id: str
    qty: int
    biz_type: str
    status: str
    event_time: datetime
    note: str
    ref_tx_id: Optional[str]
    sku: str = ""


@dataclass
class Note:
    row: int
    ev_id: str
    record_time: datetime
    content: str


@dataclass
class Line:
    plan_row: int
    order_id: str
    line_id: str
    warehouse: str
    sku: str
    qty: int
    agreed_time: datetime
    order_status: str
    flows: List[Flow] = field(default_factory=list)
    notes: List[Note] = field(default_factory=list)

    @property
    def sales(self) -> List[Flow]:
        return [f for f in self.flows if f.biz_type == "销售出库" and f.status == "已过账"]

    @property
    def drafts(self) -> List[Flow]:
        return [f for f in self.flows if f.status == "草稿"]

    @property
    def reversals(self) -> List[Flow]:
        return [f for f in self.flows if f.biz_type == "出库冲销" and f.status == "已过账"]

    @property
    def net(self) -> int:
        return sum(f.qty for f in self.sales) - sum(f.qty for f in self.reversals)

    @property
    def diff(self) -> int:
        return self.qty - self.net

    @property
    def mismatching_flows(self) -> List[Flow]:
        return [f for f in self.flows if f.status == "已过账" and f.sku != self.sku]

    @property
    def tags(self) -> List[str]:
        tags = []
        if self.drafts and not self.sales:
            tags.append("draft_only")
        if self.agreed_time > AS_OF and self.net == 0:
            tags.append("not_due")
        if self.mismatching_flows:
            tags.append("sku_mismatch")
        if self.reversals:
            tags.append("reversal")
        if len(self.sales) > 1:
            tags.append("split")
        if self.diff > 0 and not tags:
            tags.append("insufficient" if not self.notes else "quantity_difference")
        if self.diff == 0 and not tags:
            tags.append("matched")
        return tags

    @property
    def evidence_ids(self) -> List[str]:
        ids = [f.tx_id for f in self.flows if f.status in ("已过账", "草稿")]
        ids += [n.ev_id for n in self.notes]
        return sorted(set(ids))


def _dt(value) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def load_lines() -> Dict[str, Line]:
    wb = openpyxl.load_workbook(XLSX, read_only=True, data_only=True)
    lines: Dict[str, Line] = {}

    ws = wb["发货计划"]
    for row in ws.iter_rows(min_row=2, values_only=True):
        order, line_id, wh, sku, name, qty, unit, order_t, agreed, status, src, note = row
        if order is None:
            continue
        lines[f"{order}/{line_id}"] = Line(
            plan_row=0,
            order_id=str(order),
            line_id=str(line_id),
            warehouse=str(wh),
            sku=str(sku),
            qty=int(qty),
            agreed_time=_dt(agreed),
            order_status=str(status),
        )

    # 行号在 read_only 遍历中逐行记录，需要单独再读一遍拿行号
    ws = wb["发货计划"]
    for r, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if row[0] is None:
            continue
        lines[f"{row[0]}/{row[1]}"].plan_row = r

    ws = wb["仓储流水"]
    for r, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        tx, wh_order, wh_line, order, line_id, wh, sku, name, biz, qty, unit, event_t, status, ref, src, note = row
        if order is None:
            continue
        key = f"{order}/{line_id}"
        if key not in lines:
            continue
        lines[key].flows.append(
            Flow(
                row=r,
                tx_id=str(tx),
                qty=int(qty),
                biz_type=str(biz),
                status=str(status),
                event_time=_dt(event_t),
                note=str(note or ""),
                ref_tx_id=str(ref) if ref else None,
                sku=str(sku),
            )
        )

    ws = wb["业务说明"]
    for r, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        ev, order, line_id, tx, rec_t, rtype, recorder, content = row
        if order is None:
            continue
        key = f"{order}/{line_id}"
        if key not in lines:
            continue
        lines[key].notes.append(
            Note(row=r, ev_id=str(ev), record_time=_dt(rec_t), content=str(content))
        )

    for line in lines.values():
        line.flows.sort(key=lambda f: (f.event_time, f.tx_id))
        line.notes.sort(key=lambda n: (n.record_time, n.ev_id))
    return lines


def line_flows_summary(line: Line) -> str:
    return "；".join(f"{f.tx_id} {f.biz_type}{f.qty}件（{f.status}）" for f in line.flows)
