import hashlib
import io
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import openpyxl
import psycopg
from openpyxl.worksheet.worksheet import Worksheet

from . import db

SHEET_PLAN = "发货计划"
SHEET_FLOW = "仓储流水"
SHEET_NOTE = "业务说明"

_TZ = "Asia/Shanghai"


@dataclass
class FieldSpec:
    name: str
    required: bool
    kind: str  # text, int, datetime, enum
    enum_values: Optional[set] = None


@dataclass
class SheetSpec:
    sheet_name: str
    fields: List[FieldSpec]
    pk_fields: List[str]


PLAN_SPEC = SheetSpec(
    sheet_name=SHEET_PLAN,
    fields=[
        FieldSpec("order_id", True, "text"),
        FieldSpec("line_id", True, "text"),
        FieldSpec("warehouse", True, "text"),
        FieldSpec("sku", True, "text"),
        FieldSpec("sku_name", True, "text"),
        FieldSpec("planned_qty", True, "int"),
        FieldSpec("unit", True, "text"),
        FieldSpec("order_time", True, "datetime"),
        FieldSpec("agreed_time", True, "datetime"),
        FieldSpec("order_status", True, "enum", {"生效", "已取消"}),
        FieldSpec("source", True, "text"),
        FieldSpec("note", False, "text"),
    ],
    pk_fields=["order_id", "line_id"],
)

FLOW_SPEC = SheetSpec(
    sheet_name=SHEET_FLOW,
    fields=[
        FieldSpec("tx_id", True, "text"),
        FieldSpec("wh_order_id", True, "text"),
        FieldSpec("wh_line_id", True, "text"),
        FieldSpec("order_id", True, "text"),
        FieldSpec("line_id", True, "text"),
        FieldSpec("warehouse", True, "text"),
        FieldSpec("sku", True, "text"),
        FieldSpec("sku_name", True, "text"),
        FieldSpec("biz_type", True, "enum", {"销售出库", "出库冲销"}),
        FieldSpec("quantity", True, "int"),
        FieldSpec("unit", True, "text"),
        FieldSpec("event_time", True, "datetime"),
        FieldSpec("status", True, "enum", {"草稿", "已过账", "已作废"}),
        FieldSpec("ref_tx_id", False, "text"),
        FieldSpec("source", True, "text"),
        FieldSpec("note", False, "text"),
    ],
    pk_fields=["tx_id"],
)

NOTE_SPEC = SheetSpec(
    sheet_name=SHEET_NOTE,
    fields=[
        FieldSpec("ev_id", True, "text"),
        FieldSpec("order_id", True, "text"),
        FieldSpec("line_id", True, "text"),
        FieldSpec("ref_tx_id", False, "text"),
        FieldSpec("record_time", True, "datetime"),
        FieldSpec("record_type", True, "text"),
        FieldSpec("recorder", True, "text"),
        FieldSpec("content", True, "text"),
    ],
    pk_fields=["ev_id"],
)

# Aliases per canonical field, per sheet context.
PLAN_ALIASES: Dict[str, List[str]] = {
    "order_id": ["订单号", "订单编号"],
    "line_id": ["订单明细号", "明细号"],
    "warehouse": ["计划仓库", "仓库"],
    "sku": ["SKU", "商品编码"],
    "sku_name": ["商品名称", "商品"],
    "planned_qty": ["应发数量", "应发件数", "数量"],
    "unit": ["单位"],
    "order_time": ["下单时间", "下单日期", "订单时间"],
    "agreed_time": ["约定出库时间", "计划出库时间"],
    "order_status": ["订单状态", "状态"],
    "source": ["来源系统"],
    "note": ["业务备注", "备注"],
}

FLOW_ALIASES: Dict[str, List[str]] = {
    "tx_id": ["流水号", "流水ID"],
    "wh_order_id": ["仓储单号", "出库单号"],
    "wh_line_id": ["仓储明细号", "明细号"],
    "order_id": ["订单号", "订单编号"],
    "line_id": ["订单明细号"],
    "warehouse": ["仓库"],
    "sku": ["SKU", "商品编码"],
    "sku_name": ["商品名称", "商品"],
    "biz_type": ["业务类型"],
    "quantity": ["数量"],
    "unit": ["单位"],
    "event_time": ["发生时间", "时间"],
    "status": ["记录状态", "状态"],
    "ref_tx_id": ["关联流水号", "关联流水"],
    "source": ["来源系统"],
    "note": ["仓储备注", "备注"],
}

NOTE_ALIASES: Dict[str, List[str]] = {
    "ev_id": ["证据编号", "证据ID"],
    "order_id": ["关联订单号", "订单号", "订单编号"],
    "line_id": ["关联订单明细号", "订单明细号", "明细号"],
    "ref_tx_id": ["关联流水号", "流水号"],
    "record_time": ["记录时间", "时间"],
    "record_type": ["记录类型"],
    "recorder": ["记录人"],
    "content": ["内容", "说明"],
}


def _alias_map(spec: SheetSpec, aliases: Dict[str, List[str]]) -> Dict[str, str]:
    """cell_value -> canonical_name."""
    mapping: Dict[str, str] = {}
    for canonical, alias_list in aliases.items():
        for alias in alias_list:
            mapping[alias] = canonical
    # Also map exact standard names (from a synthetic header row).
    for fld in spec.fields:
        mapping[fld.name] = fld.name
    return mapping


@dataclass
class HeaderMapping:
    header_row: int
    col_to_field: Dict[int, str]  # 1-based column -> canonical field name
    missing_required: List[str] = field(default_factory=list)
    duplicates: List[Tuple[str, int, int]] = field(default_factory=list)


@dataclass
class ValidationIssue:
    sheet: str
    row: int
    column: Optional[int]
    field: Optional[str]
    message: str


@dataclass
class ParsedSheet:
    spec: SheetSpec
    mapping: HeaderMapping
    rows: List[Dict[str, Any]]


def _cell_value(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        if v == "":
            return None
    return v


def _is_missing(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and v.strip() == "":
        return True
    if isinstance(v, (int, float)) and v == 0:
        # Identifiers must not be represented as zero; for non-identifier validation
        # this is handled by the caller via kind checks.
        return True
    return False


def _detect_header(ws: Worksheet, spec: SheetSpec, aliases: Dict[str, List[str]], max_scan: int = 10) -> HeaderMapping:
    alias_to_field = _alias_map(spec, aliases)
    required_names = {f.name for f in spec.fields if f.required}

    best: Optional[HeaderMapping] = None
    best_score = -1

    for row_idx in range(1, min(max_scan, ws.max_row) + 1):
        col_to_field: Dict[int, str] = {}
        seen: Dict[str, int] = {}
        duplicates: List[Tuple[str, int, int]] = []
        for col_idx in range(1, ws.max_column + 1):
            raw = ws.cell(row=row_idx, column=col_idx).value
            cell = _cell_value(raw)
            if cell is None:
                continue
            cell_str = str(cell)
            canonical = alias_to_field.get(cell_str)
            if canonical:
                if canonical in seen:
                    duplicates.append((canonical, seen[canonical], col_idx))
                else:
                    seen[canonical] = col_idx
                col_to_field[col_idx] = canonical
        found_required = required_names & set(col_to_field.values())
        score = len(found_required)
        missing = sorted(required_names - found_required)
        mapping = HeaderMapping(header_row=row_idx, col_to_field=col_to_field, missing_required=missing, duplicates=duplicates)
        if score > best_score:
            best_score = score
            best = mapping
        if not missing and not duplicates:
            return mapping

    if best is None:
        best = HeaderMapping(header_row=1, col_to_field={}, missing_required=sorted(required_names))
    return best


def _parse_datetime(v: Any) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(v.strip(), fmt)
            except ValueError:
                continue
    return None


def _localize(dt: datetime) -> datetime:
    import zoneinfo
    if dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=zoneinfo.ZoneInfo(_TZ))


def _convert_field(v: Any, spec: FieldSpec) -> Tuple[Any, Optional[str]]:
    v = _cell_value(v)
    if spec.required and _is_missing(v):
        return None, "必填字段缺失"
    if v is None:
        return None, None
    if spec.kind == "text":
        # Preserve leading zeros by treating identifiers as strings.
        return str(v), None
    if spec.kind == "int":
        if isinstance(v, bool):
            return None, "应为正整数"
        if isinstance(v, (int, float)):
            if float(v).is_integer() and int(v) > 0:
                return int(v), None
        return None, "应为正整数"
    if spec.kind == "datetime":
        dt = _parse_datetime(v)
        if dt is None:
            return None, "应为日期时间"
        return _localize(dt), None
    if spec.kind == "enum":
        if str(v) not in (spec.enum_values or set()):
            return None, f"枚举值错误: {v}"
        return str(v), None
    return v, None


def _read_sheet(ws: Worksheet, spec: SheetSpec, aliases: Dict[str, List[str]]) -> ParsedSheet:
    mapping = _detect_header(ws, spec, aliases)
    rows: List[Dict[str, Any]] = []
    for row_idx in range(mapping.header_row + 1, ws.max_row + 1):
        record: Dict[str, Any] = {"_source_row": row_idx}
        for col_idx, canonical in mapping.col_to_field.items():
            record[canonical] = ws.cell(row=row_idx, column=col_idx).value
        rows.append(record)
    return ParsedSheet(spec=spec, mapping=mapping, rows=rows)


def _validate_sheet(parsed: ParsedSheet) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    spec = parsed.spec

    # Header-level issues.
    for field in parsed.mapping.missing_required:
        issues.append(
            ValidationIssue(
                sheet=spec.sheet_name,
                row=parsed.mapping.header_row,
                column=None,
                field=field,
                message="缺少必填列",
            )
        )
    for canonical, col_a, col_b in parsed.mapping.duplicates:
        issues.append(
            ValidationIssue(
                sheet=spec.sheet_name,
                row=parsed.mapping.header_row,
                column=col_b,
                field=canonical,
                message=f"列重复（已出现在第 {col_a} 列）",
            )
        )
    if issues:
        return issues

    # Row-level issues.
    converted_rows: List[Dict[str, Any]] = []
    for record in parsed.rows:
        row_idx = record["_source_row"]
        converted: Dict[str, Any] = {"_source_row": row_idx}
        for fld in spec.fields:
            raw = record.get(fld.name)
            val, err = _convert_field(raw, fld)
            if err:
                col = None
                for c, f in parsed.mapping.col_to_field.items():
                    if f == fld.name:
                        col = c
                        break
                issues.append(
                    ValidationIssue(
                        sheet=spec.sheet_name,
                        row=row_idx,
                        column=col,
                        field=fld.name,
                        message=err,
                    )
                )
            converted[fld.name] = val
        converted_rows.append(converted)

    parsed.rows = converted_rows
    return issues


def _cross_validate(
    plan: ParsedSheet, flow: ParsedSheet, note: ParsedSheet
) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []

    plan_keys: Dict[Tuple[str, str], int] = {}
    for rec in plan.rows:
        key = (rec["order_id"], rec["line_id"])
        if key in plan_keys:
            issues.append(
                ValidationIssue(
                    sheet=plan.spec.sheet_name,
                    row=rec["_source_row"],
                    column=None,
                    field="order_id,line_id",
                    message=f"重复的发货计划键: {key}",
                )
            )
        else:
            plan_keys[key] = rec["_source_row"]

    tx_keys: Dict[str, Tuple[int, Dict[str, Any]]] = {}
    for rec in flow.rows:
        tx_id = rec["tx_id"]
        if tx_id in tx_keys:
            issues.append(
                ValidationIssue(
                    sheet=flow.spec.sheet_name,
                    row=rec["_source_row"],
                    column=None,
                    field="tx_id",
                    message=f"重复的流水号: {tx_id}",
                )
            )
        else:
            tx_keys[tx_id] = (rec["_source_row"], rec)

    ev_keys: Dict[str, int] = {}
    for rec in note.rows:
        ev_id = rec["ev_id"]
        if ev_id in ev_keys:
            issues.append(
                ValidationIssue(
                    sheet=note.spec.sheet_name,
                    row=rec["_source_row"],
                    column=None,
                    field="ev_id",
                    message=f"重复的证据编号: {ev_id}",
                )
            )
        else:
            ev_keys[ev_id] = rec["_source_row"]

    # Foreign keys: flow -> plan.
    for rec in flow.rows:
        key = (rec["order_id"], rec["line_id"])
        if key not in plan_keys:
            issues.append(
                ValidationIssue(
                    sheet=flow.spec.sheet_name,
                    row=rec["_source_row"],
                    column=None,
                    field="order_id,line_id",
                    message=f"流水对应的发货计划不存在: {key}",
                )
            )

    # Foreign keys: note -> plan and note -> flow.
    for rec in note.rows:
        key = (rec["order_id"], rec["line_id"])
        if key not in plan_keys:
            issues.append(
                ValidationIssue(
                    sheet=note.spec.sheet_name,
                    row=rec["_source_row"],
                    column=None,
                    field="order_id,line_id",
                    message=f"说明对应的发货计划不存在: {key}",
                )
            )
        ref = rec.get("ref_tx_id")
        if ref and ref not in tx_keys:
            issues.append(
                ValidationIssue(
                    sheet=note.spec.sheet_name,
                    row=rec["_source_row"],
                    column=None,
                    field="ref_tx_id",
                    message=f"说明关联的流水不存在: {ref}",
                )
            )

    # Reversal rules.
    reversal_sums: Dict[str, int] = {}
    for rec in flow.rows:
        if rec["biz_type"] != "出库冲销":
            continue
        ref = rec.get("ref_tx_id")
        if not ref:
            issues.append(
                ValidationIssue(
                    sheet=flow.spec.sheet_name,
                    row=rec["_source_row"],
                    column=None,
                    field="ref_tx_id",
                    message="出库冲销必须填写关联流水号",
                )
            )
            continue
        if ref not in tx_keys:
            continue  # already reported as FK error.
        _, orig = tx_keys[ref]
        if orig["status"] != "已过账":
            issues.append(
                ValidationIssue(
                    sheet=flow.spec.sheet_name,
                    row=rec["_source_row"],
                    column=None,
                    field="ref_tx_id",
                    message=f"冲销指向的记录状态不是已过账: {ref} 状态={orig['status']}",
                )
            )
        if orig["biz_type"] != "销售出库":
            issues.append(
                ValidationIssue(
                    sheet=flow.spec.sheet_name,
                    row=rec["_source_row"],
                    column=None,
                    field="ref_tx_id",
                    message=f"冲销指向的业务类型不是销售出库: {ref} 类型={orig['biz_type']}",
                )
            )
        same_fields = (
            rec["order_id"] == orig["order_id"]
            and rec["line_id"] == orig["line_id"]
            and rec["warehouse"] == orig["warehouse"]
            and rec["sku"] == orig["sku"]
            and rec["unit"] == orig["unit"]
        )
        if not same_fields:
            issues.append(
                ValidationIssue(
                    sheet=flow.spec.sheet_name,
                    row=rec["_source_row"],
                    column=None,
                    field="ref_tx_id",
                    message=f"冲销与原事件事务字段不一致: {ref}",
                )
            )
        if rec["event_time"] < orig["event_time"]:
            issues.append(
                ValidationIssue(
                    sheet=flow.spec.sheet_name,
                    row=rec["_source_row"],
                    column=None,
                    field="event_time",
                    message=f"冲销时间早于原事件: {ref}",
                )
            )
        reversal_sums[ref] = reversal_sums.get(ref, 0) + rec["quantity"]

    for ref, total in reversal_sums.items():
        _, orig = tx_keys[ref]
        if total > orig["quantity"]:
            issues.append(
                ValidationIssue(
                    sheet=flow.spec.sheet_name,
                    row=tx_keys[ref][0],
                    column=None,
                    field="quantity",
                    message=f"冲销累计超原数量: {ref} 原={orig['quantity']} 冲={total}",
                )
            )

    return issues


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_as_of(as_of: Any) -> datetime:
    import zoneinfo
    if isinstance(as_of, datetime):
        return as_of if as_of.tzinfo else as_of.replace(tzinfo=zoneinfo.ZoneInfo(_TZ))
    if isinstance(as_of, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(as_of.strip(), fmt)
                return dt.replace(tzinfo=zoneinfo.ZoneInfo(_TZ))
            except ValueError:
                continue
    raise ValueError(f"无法解析截止时间: {as_of}")


def _issue_to_dict(i: ValidationIssue) -> dict:
    return {
        "sheet": i.sheet,
        "row": i.row,
        "column": i.column,
        "field": i.field,
        "message": i.message,
    }


def import_excel(
    path: Path,
    as_of: Any,
    conn: Optional[psycopg.Connection] = None,
) -> dict:
    """Import an Excel workbook into the database.

    Returns a dict describing the outcome. On success the dataset is marked
    ``ready``; on validation failure it is marked ``failed`` and no child rows
    are inserted.
    """
    path = Path(path)
    as_of_dt = _parse_as_of(as_of)
    file_sha256 = _sha256_file(path)
    dataset_id = f"ds-{file_sha256[:12]}"
    source_filename = path.name

    own_conn = conn is None
    if own_conn:
        conn = db.get_admin_conn()
    try:
        # Idempotency: reuse existing record for the same file hash.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT dataset_id, status, plan_rows, flow_rows, note_rows FROM dataset WHERE file_sha256 = %s",
                (file_sha256,),
            )
            existing = cur.fetchone()
        if existing:
            old_id, old_status, plan_rows, flow_rows, note_rows = existing
            return {
                "dataset_id": old_id,
                "status": old_status,
                "source_filename": source_filename,
                "file_sha256": file_sha256,
                "plan_rows": plan_rows,
                "flow_rows": flow_rows,
                "note_rows": note_rows,
                "as_of": as_of_dt.isoformat(),
                "issues": [],
                "reused": True,
            }

        wb = openpyxl.load_workbook(path, data_only=False)
        plan_ws = wb[SHEET_PLAN]
        flow_ws = wb[SHEET_FLOW]
        note_ws = wb[SHEET_NOTE]

        plan = _read_sheet(plan_ws, PLAN_SPEC, PLAN_ALIASES)
        flow = _read_sheet(flow_ws, FLOW_SPEC, FLOW_ALIASES)
        note = _read_sheet(note_ws, NOTE_SPEC, NOTE_ALIASES)

        issues: List[ValidationIssue] = []
        issues.extend(_validate_sheet(plan))
        issues.extend(_validate_sheet(flow))
        issues.extend(_validate_sheet(note))
        if not issues:
            issues.extend(_cross_validate(plan, flow, note))

        if issues:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO dataset (dataset_id, source_filename, file_sha256,
                                         plan_rows, flow_rows, note_rows, as_of, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'failed')
                    ON CONFLICT (file_sha256) DO UPDATE SET
                        status = EXCLUDED.status,
                        plan_rows = EXCLUDED.plan_rows,
                        flow_rows = EXCLUDED.flow_rows,
                        note_rows = EXCLUDED.note_rows,
                        as_of = EXCLUDED.as_of
                    """,
                    (dataset_id, source_filename, file_sha256, len(plan.rows), len(flow.rows), len(note.rows), as_of_dt),
                )
            conn.commit()
            return {
                "dataset_id": dataset_id,
                "status": "failed",
                "source_filename": source_filename,
                "file_sha256": file_sha256,
                "plan_rows": len(plan.rows),
                "flow_rows": len(flow.rows),
                "note_rows": len(note.rows),
                "as_of": as_of_dt.isoformat(),
                "issues": [_issue_to_dict(i) for i in issues],
                "reused": False,
            }

        # Insert ready snapshot.
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dataset (dataset_id, source_filename, file_sha256,
                                     plan_rows, flow_rows, note_rows, as_of, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'ready')
                """,
                (dataset_id, source_filename, file_sha256, len(plan.rows), len(flow.rows), len(note.rows), as_of_dt),
            )
            for rec in plan.rows:
                cur.execute(
                    """
                    INSERT INTO plan_line (dataset_id, order_id, line_id, warehouse, sku, sku_name,
                                           planned_qty, unit, order_time, agreed_time, order_status,
                                           source, note, source_sheet, source_row)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        dataset_id,
                        rec["order_id"],
                        rec["line_id"],
                        rec["warehouse"],
                        rec["sku"],
                        rec["sku_name"],
                        rec["planned_qty"],
                        rec["unit"],
                        rec["order_time"],
                        rec["agreed_time"],
                        rec["order_status"],
                        rec["source"],
                        rec.get("note"),
                        SHEET_PLAN,
                        rec["_source_row"],
                    ),
                )
            for rec in flow.rows:
                cur.execute(
                    """
                    INSERT INTO wms_flow (dataset_id, tx_id, wh_order_id, wh_line_id, order_id, line_id,
                                          warehouse, sku, sku_name, biz_type, quantity, unit, event_time,
                                          status, ref_tx_id, source, note, source_sheet, source_row)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        dataset_id,
                        rec["tx_id"],
                        rec["wh_order_id"],
                        rec["wh_line_id"],
                        rec["order_id"],
                        rec["line_id"],
                        rec["warehouse"],
                        rec["sku"],
                        rec["sku_name"],
                        rec["biz_type"],
                        rec["quantity"],
                        rec["unit"],
                        rec["event_time"],
                        rec["status"],
                        rec.get("ref_tx_id"),
                        rec["source"],
                        rec.get("note"),
                        SHEET_FLOW,
                        rec["_source_row"],
                    ),
                )
            for rec in note.rows:
                cur.execute(
                    """
                    INSERT INTO evidence_note (dataset_id, ev_id, order_id, line_id, ref_tx_id,
                                               record_time, record_type, recorder, content,
                                               source_sheet, source_row)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        dataset_id,
                        rec["ev_id"],
                        rec["order_id"],
                        rec["line_id"],
                        rec.get("ref_tx_id"),
                        rec["record_time"],
                        rec["record_type"],
                        rec["recorder"],
                        rec["content"],
                        SHEET_NOTE,
                        rec["_source_row"],
                    ),
                )
        conn.commit()
        return {
            "dataset_id": dataset_id,
            "status": "ready",
            "source_filename": source_filename,
            "file_sha256": file_sha256,
            "plan_rows": len(plan.rows),
            "flow_rows": len(flow.rows),
            "note_rows": len(note.rows),
            "as_of": as_of_dt.isoformat(),
            "issues": [],
            "reused": False,
        }
    finally:
        if own_conn:
            conn.close()
