from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import psycopg

from . import db
from .importer import _parse_as_of


class ReconcileError(Exception):
    pass


def _fetch_dataset_as_of(cur, dataset_id: str, as_of: Optional[datetime]) -> datetime:
    cur.execute("SELECT as_of, status FROM dataset WHERE dataset_id = %s", (dataset_id,))
    row = cur.fetchone()
    if not row:
        raise ReconcileError(f"数据集不存在: {dataset_id}")
    dataset_as_of, status = row
    if status != "ready":
        raise ReconcileError(f"数据集未就绪: {dataset_id} 状态={status}")
    if as_of is None:
        return dataset_as_of
    return as_of


def _list_plan_lines(cur, dataset_id: str, order_id: str, line_id: Optional[str]) -> List[Dict[str, Any]]:
    sql = """
        SELECT order_id, line_id, warehouse, sku, unit, planned_qty, agreed_time
        FROM plan_line
        WHERE dataset_id = %s AND order_id = %s
    """
    params: List[Any] = [dataset_id, order_id]
    if line_id is not None:
        sql += " AND line_id = %s"
        params.append(line_id)
    sql += " ORDER BY line_id"
    cur.execute(sql, params)
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _fetch_flows(cur, dataset_id: str, order_id: str, line_id: str, as_of: datetime) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT tx_id, warehouse, sku, unit, biz_type, quantity, status, event_time, ref_tx_id
        FROM wms_flow
        WHERE dataset_id = %s AND order_id = %s AND line_id = %s AND event_time <= %s
        ORDER BY event_time, tx_id
        """,
        (dataset_id, order_id, line_id, as_of),
    )
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _fetch_notes(cur, dataset_id: str, order_id: str, line_id: str, as_of: datetime) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT ev_id, ref_tx_id
        FROM evidence_note
        WHERE dataset_id = %s AND order_id = %s AND line_id = %s AND record_time <= %s
        ORDER BY record_time, ev_id
        """,
        (dataset_id, order_id, line_id, as_of),
    )
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _reconcile_line(
    plan: Dict[str, Any],
    flows: List[Dict[str, Any]],
    notes: List[Dict[str, Any]],
    as_of: datetime,
) -> Dict[str, Any]:
    plan_key = (plan["warehouse"], plan["sku"], plan["unit"])
    planned = plan["planned_qty"]

    posted = [f for f in flows if f["status"] == "已过账"]
    drafts = [f for f in flows if f["status"] == "草稿"]

    # Group posted flows by SKU/warehouse/unit.
    groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for f in posted:
        key = (f["warehouse"], f["sku"], f["unit"])
        groups.setdefault(key, []).append(f)

    matching = groups.get(plan_key, [])
    mismatching = [f for key, grp in groups.items() for f in grp if key != plan_key]

    sales = sum(f["quantity"] for f in matching if f["biz_type"] == "销售出库")
    reversals = sum(f["quantity"] for f in matching if f["biz_type"] == "出库冲销")
    net = sales - reversals
    diff = planned - net

    status_tags: List[str] = []

    if plan["agreed_time"] > as_of and diff > 0 and net == 0 and not mismatching:
        status_tags.append("not_due")
    elif net == 0 and not matching and not mismatching:
        if drafts:
            status_tags.append("draft_only")
        else:
            status_tags.append("no_valid_flow")

    if mismatching:
        status_tags.append("sku_mismatch")

    if diff > 0 and not status_tags:
        # Either quantity_difference or insufficient_evidence.
        if notes:
            status_tags.append("quantity_difference")
        else:
            status_tags.append("insufficient_evidence")

    if diff == 0 and not status_tags:
        # Distinguish matched / split_matched / reversal_matched.
        has_reversal = any(f["biz_type"] == "出库冲销" for f in matching)
        sales_flows = [f for f in matching if f["biz_type"] == "销售出库"]
        if has_reversal:
            status_tags.append("reversal_matched")
        elif len(sales_flows) > 1:
            status_tags.append("split_matched")
        else:
            status_tags.append("matched")

    # Supplementary flags used by D3.2.
    if any(f["biz_type"] == "出库冲销" for f in matching):
        status_tags.append("has_reversal")
    if len([f for f in matching if f["biz_type"] == "销售出库"]) > 1:
        status_tags.append("split_batches")

    # Evidence IDs: all relevant flow IDs (posted or draft, on time) plus note IDs.
    relevant_flow_ids = [f["tx_id"] for f in flows if f["status"] in ("已过账", "草稿")]
    note_ids = [n["ev_id"] for n in notes]
    evidence_ids = sorted(set(relevant_flow_ids + note_ids))

    return {
        "order_id": plan["order_id"],
        "line_id": plan["line_id"],
        "warehouse": plan["warehouse"],
        "sku": plan["sku"],
        "unit": plan["unit"],
        "planned_quantity": planned,
        "matched_net_shipped_quantity": net,
        "difference_quantity": diff,
        "status_tags": sorted(set(status_tags)),
        "flow_ids": sorted(set(relevant_flow_ids)),
        "evidence_ids": evidence_ids,
    }


def reconcile(
    dataset_id: str,
    order_id: str,
    line_id: Optional[str] = None,
    as_of: Optional[Any] = None,
    conn: Optional[psycopg.Connection] = None,
) -> dict:
    """Run exact reconciliation for an order (or one of its lines).

    Uses a read-only database connection by default.
    """
    as_of_dt = _parse_as_of(as_of) if as_of else None

    own_conn = conn is None
    if own_conn:
        conn = db.get_reader_conn()
    try:
        with conn.cursor() as cur:
            effective_as_of = _fetch_dataset_as_of(cur, dataset_id, as_of_dt)
            plans = _list_plan_lines(cur, dataset_id, order_id, line_id)
            if not plans:
                raise ReconcileError(f"订单明细不存在: {order_id}/{line_id or '*'}")

            lines = []
            for plan in plans:
                flows = _fetch_flows(cur, dataset_id, order_id, plan["line_id"], effective_as_of)
                notes = _fetch_notes(cur, dataset_id, order_id, plan["line_id"], effective_as_of)
                lines.append(_reconcile_line(plan, flows, notes, effective_as_of))

        return {
            "dataset_id": dataset_id,
            "order_id": order_id,
            "line_id": line_id,
            "as_of": effective_as_of.isoformat(),
            "lines": lines,
        }
    finally:
        if own_conn:
            conn.close()
