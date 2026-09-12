"""T-006a 语料导入、校验与索引（L3 v0.3 D9）。

只导入白名单三文件；manifest 与 Excel 快照仅用于校验；review/ 不进入业务索引。
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb

from . import db
from .importer import _sha256_file

CORPUS_PARSER_VERSION = "t006a-1"
CORPUS_FILES = ("events.jsonl", "cases.jsonl", "sops.jsonl")
DEFAULT_CORPUS_DIR = (
    Path(__file__).resolve().parent.parent.parent / "deliverables" / "t004" / "full-v2"
)
KNOWN_SHEETS = {"发货计划", "仓储流水", "业务说明"}
_SCOPE_KEYS = ("warehouse_id", "sku", "order_id", "line_id")
_MAX_ISSUES = 50


class CorpusError(Exception):
    pass


class CorpusValidationError(CorpusError):
    def __init__(self, message: str, issues: List[Dict[str, Any]]):
        super().__init__(message)
        self.issues = issues


class CorpusConflictError(CorpusError):
    pass


def _issue(
    issues: List[Dict[str, Any]],
    jsonl_file: str,
    row: int,
    doc_id: Optional[str],
    field: str,
    message: str,
) -> None:
    if len(issues) < _MAX_ISSUES:
        issues.append(
            {"file": jsonl_file, "row": row, "doc_id": doc_id, "field": field, "message": message}
        )


def _parse_ts(value: Any, issues, ctx, field: str) -> Optional[datetime]:
    if not isinstance(value, str):
        _issue(issues, *ctx, field, "必填且必须为 ISO 时间字符串")
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        _issue(issues, *ctx, field, f"无法解析时间: {value!r}")
        return None
    if dt.tzinfo is None:
        _issue(issues, *ctx, field, "时间必须带时区偏移")
        return None
    return dt


def _require_str(value: Any, issues, ctx, field: str, allow_null: bool = False) -> Optional[str]:
    if value is None and allow_null:
        return None
    if not isinstance(value, str) or not value.strip():
        _issue(issues, *ctx, field, "必填且必须为非空字符串")
        return None
    return value


def _validate_sections(sections: Any, issues, ctx) -> bool:
    if not isinstance(sections, list) or not sections:
        _issue(issues, *ctx, "sections", "必须为非空数组")
        return False
    seen = set()
    ok = True
    for i, section in enumerate(sections):
        if not isinstance(section, dict):
            _issue(issues, *ctx, f"sections[{i}]", "必须为对象")
            ok = False
            continue
        sid = section.get("section_id")
        if not isinstance(sid, str) or not sid.strip():
            _issue(issues, *ctx, f"sections[{i}].section_id", "必填且必须为非空字符串")
            ok = False
        elif sid in seen:
            _issue(issues, *ctx, f"sections[{i}].section_id", f"文档内重复: {sid}")
            ok = False
        else:
            seen.add(sid)
        for key in ("heading", "text"):
            if not isinstance(section.get(key), str) or not section[key].strip():
                _issue(issues, *ctx, f"sections[{i}].{key}", "必填且必须为非空字符串")
                ok = False
    return ok


def _validate_scope(doc: Dict[str, Any], doc_type: str, issues, ctx) -> Optional[Dict[str, Any]]:
    scope = doc.get("scope")
    if not isinstance(scope, dict) or any(k not in scope for k in _SCOPE_KEYS):
        _issue(issues, *ctx, "scope", "必须含 warehouse_id/sku/order_id/line_id")
        return None
    for key in _SCOPE_KEYS:
        value = scope.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            _issue(issues, *ctx, f"scope.{key}", "只能为非空字符串或 null")
    order_id, line_id = scope.get("order_id"), scope.get("line_id")
    if doc_type == "event":
        if not order_id or not line_id:
            _issue(issues, *ctx, "scope", "事件必须引用订单与明细")
    elif order_id is not None or line_id is not None:
        _issue(issues, *ctx, "scope", f"{doc_type} 的 order_id/line_id 必须为 null")
    return scope


def _validate_doc(
    doc: Any,
    expected_type: str,
    jsonl_file: str,
    row: int,
    file_sha256: str,
    issues: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    ctx = (jsonl_file, row, doc.get("doc_id") if isinstance(doc, dict) else None)
    if not isinstance(doc, dict):
        _issue(issues, *ctx, "doc", "必须为 JSON 对象")
        return None

    doc_id = _require_str(doc.get("doc_id"), issues, ctx, "doc_id")
    if doc_id is None:
        return None
    ctx = (jsonl_file, row, doc_id)

    if doc.get("doc_type") != expected_type:
        _issue(issues, *ctx, "doc_type", f"必须为 {expected_type}")
    title = _require_str(doc.get("title"), issues, ctx, "title")
    author_role = _require_str(doc.get("author_role"), issues, ctx, "author_role")
    recorded_at = _parse_ts(doc.get("recorded_at"), issues, ctx, "recorded_at")
    if doc.get("synthetic") is not True:
        _issue(issues, *ctx, "synthetic", "必须为 true")
    scope = _validate_scope(doc, expected_type, issues, ctx)
    _validate_sections(doc.get("sections"), issues, ctx)

    meta: Dict[str, Any] = {
        "doc_id": doc_id,
        "doc_type": expected_type,
        "title": title,
        "recorded_at": recorded_at,
        "occurred_at": None,
        "closed_at": None,
        "policy_id": None,
        "version": None,
        "effective_from": None,
        "effective_to": None,
        "supersedes": None,
        "author_role": author_role,
        "scope_warehouse": scope.get("warehouse_id") if scope else None,
        "scope_sku": scope.get("sku") if scope else None,
        "scope_order_id": scope.get("order_id") if scope else None,
        "scope_line_id": scope.get("line_id") if scope else None,
        "history_case_id": None,
        "jsonl_file": jsonl_file,
        "jsonl_row": row,
        "file_sha256": file_sha256,
        "raw": doc,
    }

    if expected_type == "event":
        occurred_at = _parse_ts(doc.get("occurred_at"), issues, ctx, "occurred_at")
        if occurred_at and recorded_at and occurred_at > recorded_at:
            _issue(issues, *ctx, "occurred_at", "发生时间不能晚于记录时间")
        meta["occurred_at"] = occurred_at
        tx_ids = doc.get("transaction_ids")
        if not isinstance(tx_ids, list) or not all(isinstance(t, str) and t for t in tx_ids):
            _issue(issues, *ctx, "transaction_ids", "必须为字符串数组")
        refs = doc.get("source_refs")
        if not isinstance(refs, list):
            _issue(issues, *ctx, "source_refs", "必须为数组")
        else:
            for i, ref in enumerate(refs):
                if not isinstance(ref, dict):
                    _issue(issues, *ctx, f"source_refs[{i}]", "必须为对象")
                    continue
                _require_str(ref.get("file"), issues, ctx, f"source_refs[{i}].file")
                if ref.get("sheet") not in KNOWN_SHEETS:
                    _issue(issues, *ctx, f"source_refs[{i}].sheet", "必须是已知工作表")
                if not isinstance(ref.get("row"), int) or ref["row"] < 2:
                    _issue(issues, *ctx, f"source_refs[{i}].row", "必须为不小于 2 的整数")
    elif expected_type == "case":
        closed_at = _parse_ts(doc.get("closed_at"), issues, ctx, "closed_at")
        if closed_at and recorded_at and closed_at > recorded_at:
            _issue(issues, *ctx, "closed_at", "结案时间不能晚于记录时间")
        meta["closed_at"] = closed_at
        meta["history_case_id"] = _require_str(
            doc.get("history_case_id"), issues, ctx, "history_case_id"
        )
        refs = doc.get("rule_refs")
        if not isinstance(refs, list) or not all(isinstance(r, str) and r for r in refs):
            _issue(issues, *ctx, "rule_refs", "必须为字符串数组")
    else:
        meta["policy_id"] = _require_str(doc.get("policy_id"), issues, ctx, "policy_id")
        meta["version"] = _require_str(doc.get("version"), issues, ctx, "version")
        effective_from = _parse_ts(doc.get("effective_from"), issues, ctx, "effective_from")
        meta["effective_from"] = effective_from
        effective_to = doc.get("effective_to")
        if effective_to is not None:
            meta["effective_to"] = _parse_ts(effective_to, issues, ctx, "effective_to")
        if effective_from and recorded_at and recorded_at > effective_from:
            _issue(issues, *ctx, "effective_from", "生效时间不能早于发布时间")
        if effective_from and meta["effective_to"] and effective_from >= meta["effective_to"]:
            _issue(issues, *ctx, "effective_to", "生效区间必须前闭后开")
        supersedes = doc.get("supersedes")
        if supersedes is not None:
            meta["supersedes"] = _require_str(supersedes, issues, ctx, "supersedes")
    return meta


def _load_corpus(
    base: Path, issues: List[Dict[str, Any]]
) -> Tuple[Optional[Dict[str, Any]], Dict[str, str], Optional[str], List[Dict[str, Any]]]:
    """Read manifest and whitelist files; return (manifest, hashes, snapshot, docs)."""
    manifest_path = base / "manifest.json"
    if not manifest_path.exists():
        raise CorpusError(f"manifest 不存在: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise CorpusError(f"manifest 无法解析: {e}")

    entries = {
        f.get("name"): f for f in manifest.get("files", []) if isinstance(f, dict)
    }
    hashes: Dict[str, str] = {}
    for name in CORPUS_FILES:
        entry = entries.get(name)
        if entry is None:
            raise CorpusError(f"manifest 缺少白名单文件记录: {name}")
        path = base / name
        if not path.exists():
            raise CorpusError(f"语料文件不存在: {path}")
        actual = _sha256_file(path)
        if actual != entry.get("sha256"):
            _issue(issues, name, 0, None, "sha256", "文件哈希与 manifest 不匹配")
            continue
        hashes[name] = actual
        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        if entry.get("count") != len(lines):
            _issue(issues, name, 0, None, "count", "行数与 manifest 不匹配")

    snapshot = (manifest.get("source_snapshot") or {}).get("sha256")
    if not isinstance(snapshot, str):
        _issue(issues, "manifest.json", 0, None, "source_snapshot", "缺少快照哈希")

    docs: List[Dict[str, Any]] = []
    seen_ids: Dict[str, str] = {}
    if len(hashes) == len(CORPUS_FILES):
        expected_type = {"events.jsonl": "event", "cases.jsonl": "case", "sops.jsonl": "sop"}
        for name in CORPUS_FILES:
            path = base / name
            for row, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as e:
                    _issue(issues, name, row, None, "json", f"无法解析: {e}")
                    continue
                meta = _validate_doc(raw, expected_type[name], name, row, hashes[name], issues)
                if meta is None:
                    continue
                doc_id = meta["doc_id"]
                if doc_id in seen_ids:
                    _issue(
                        issues,
                        name,
                        row,
                        doc_id,
                        "doc_id",
                        f"全局重复（已在 {seen_ids[doc_id]}）",
                    )
                    continue
                seen_ids[doc_id] = name
                docs.append(meta)
    return manifest, hashes, snapshot, docs


def _validate_relations(
    cur: psycopg.Cursor,
    dataset_id: str,
    docs: List[Dict[str, Any]],
    issues: List[Dict[str, Any]],
) -> None:
    """Cross-check corpus docs against the bound dataset snapshot before publish.

    Events: scope order/line, transaction ids and source rows must exist and
    belong to the same order/line; source fact times must not be later than the
    event's recorded_at. The plan's agreed_time is a pre-known plan, not a
    future fact, and real SKU mismatches are preserved (not checked here).
    SOPs: same policy/scope intervals must not overlap and supersedes must point
    to an existing same-policy/same-scope version without self-reference or
    cycles. Cases: rule_refs must exist and be effective at closed_at.
    """
    cur.execute("SELECT source_filename FROM dataset WHERE dataset_id = %s", (dataset_id,))
    row = cur.fetchone()
    if row is None:
        return
    snapshot_name = row[0]

    cur.execute(
        "SELECT order_id, line_id, source_row FROM plan_line WHERE dataset_id = %s",
        (dataset_id,),
    )
    plan_by_row = {r[2]: (r[0], r[1]) for r in cur.fetchall()}
    plan_keys = set(plan_by_row.values())

    cur.execute(
        "SELECT tx_id, order_id, line_id, event_time, source_row FROM wms_flow"
        " WHERE dataset_id = %s",
        (dataset_id,),
    )
    flow_by_id: Dict[str, Tuple[str, str]] = {}
    flow_by_row: Dict[int, Tuple[str, str, datetime]] = {}
    for tx_id, order_id, line_id, event_time, source_row in cur.fetchall():
        flow_by_id[tx_id] = (order_id, line_id)
        flow_by_row[source_row] = (order_id, line_id, event_time)

    cur.execute(
        "SELECT order_id, line_id, record_time, source_row FROM evidence_note"
        " WHERE dataset_id = %s",
        (dataset_id,),
    )
    note_by_row = {r[3]: (r[0], r[1], r[2]) for r in cur.fetchall()}

    sops = {m["doc_id"]: m for m in docs if m["doc_type"] == "sop"}

    for m in docs:
        raw = m["raw"]
        ctx = (m["jsonl_file"], m["jsonl_row"], m["doc_id"])
        if m["doc_type"] == "event":
            key = (m["scope_order_id"], m["scope_line_id"])
            if key not in plan_keys:
                _issue(issues, *ctx, "scope", f"订单/明细不在绑定数据集: {key[0]}/{key[1]}")
            for i, tx_id in enumerate(raw.get("transaction_ids", [])):
                owner = flow_by_id.get(tx_id)
                if owner is None:
                    _issue(issues, *ctx, f"transaction_ids[{i}]", f"事务不存在: {tx_id}")
                elif owner != key:
                    _issue(issues, *ctx, f"transaction_ids[{i}]", f"事务不属于本单: {tx_id}")
            for i, ref in enumerate(raw.get("source_refs", [])):
                field = f"source_refs[{i}]"
                if Path(str(ref.get("file", ""))).name != snapshot_name:
                    _issue(issues, *ctx, field, f"来源文件与绑定快照不符: {ref.get('file')}")
                    continue
                sheet, row_no = ref.get("sheet"), ref.get("row")
                if sheet == "发货计划":
                    owner = plan_by_row.get(row_no)
                    if owner is None:
                        _issue(issues, *ctx, field, f"发货计划行不存在: {row_no}")
                    elif owner != key:
                        _issue(issues, *ctx, field, f"发货计划行不属于本单: {row_no}")
                elif sheet == "仓储流水":
                    owner = flow_by_row.get(row_no)
                    if owner is None:
                        _issue(issues, *ctx, field, f"仓储流水行不存在: {row_no}")
                    else:
                        if owner[:2] != key:
                            _issue(issues, *ctx, field, f"仓储流水行不属于本单: {row_no}")
                        if owner[2] > m["recorded_at"]:
                            _issue(
                                issues, *ctx, field,
                                f"来源流水发生时间晚于事件记录时间: {row_no}",
                            )
                elif sheet == "业务说明":
                    owner = note_by_row.get(row_no)
                    if owner is None:
                        _issue(issues, *ctx, field, f"业务说明行不存在: {row_no}")
                    else:
                        if owner[:2] != key:
                            _issue(issues, *ctx, field, f"业务说明行不属于本单: {row_no}")
                        if owner[2] > m["recorded_at"]:
                            _issue(
                                issues, *ctx, field,
                                f"来源说明记录时间晚于事件记录时间: {row_no}",
                            )
        elif m["doc_type"] == "case":
            closed_at = m["closed_at"]
            for i, ref in enumerate(raw.get("rule_refs", [])):
                sop = sops.get(ref)
                if sop is None:
                    _issue(issues, *ctx, f"rule_refs[{i}]", f"引用的 SOP 不存在: {ref}")
                    continue
                if sop["effective_from"] > closed_at or (
                    sop["effective_to"] is not None and not closed_at < sop["effective_to"]
                ):
                    _issue(issues, *ctx, f"rule_refs[{i}]", f"引用 SOP 在结案时点无效: {ref}")
                # A null SOP scope is general; a concrete scope must match the
                # case scope, and an unknown (null) case scope cannot satisfy it.
                if sop["scope_warehouse"] is not None and sop["scope_warehouse"] != m["scope_warehouse"]:
                    _issue(issues, *ctx, f"rule_refs[{i}]", f"引用 SOP 仓库范围不适配: {ref}")
                if sop["scope_sku"] is not None and sop["scope_sku"] != m["scope_sku"]:
                    _issue(issues, *ctx, f"rule_refs[{i}]", f"引用 SOP SKU 范围不适配: {ref}")

    groups: Dict[Tuple[Any, Any, Any], List[Dict[str, Any]]] = {}
    for sop in sops.values():
        groups.setdefault(
            (sop["policy_id"], sop["scope_warehouse"], sop["scope_sku"]), []
        ).append(sop)
    for items in groups.values():
        items.sort(key=lambda s: s["effective_from"])
        for earlier, later in zip(items, items[1:]):
            if earlier["effective_to"] is None or later["effective_from"] < earlier["effective_to"]:
                _issue(
                    issues, later["jsonl_file"], later["jsonl_row"], later["doc_id"],
                    "effective_from", f"与 {earlier['doc_id']} 的生效区间重叠",
                )

    for sop in sops.values():
        sup = sop["supersedes"]
        if sup is None:
            continue
        ctx = (sop["jsonl_file"], sop["jsonl_row"], sop["doc_id"])
        if sup == sop["doc_id"]:
            _issue(issues, *ctx, "supersedes", "不能自引用")
            continue
        target = sops.get(sup)
        if target is None:
            _issue(issues, *ctx, "supersedes", f"被替代版本不存在: {sup}")
            continue
        if target["policy_id"] != sop["policy_id"] or (
            target["scope_warehouse"], target["scope_sku"]
        ) != (sop["scope_warehouse"], sop["scope_sku"]):
            _issue(issues, *ctx, "supersedes", f"被替代版本政策或范围不一致: {sup}")
            continue
        if target["effective_from"] >= sop["effective_from"]:
            _issue(issues, *ctx, "supersedes", f"被替代版本开始时间不早于当前版本: {sup}")
        elif target["effective_to"] is None or target["effective_to"] > sop["effective_from"]:
            _issue(issues, *ctx, "supersedes", f"被替代版本未在当前版本开始前结束: {sup}")

    for sop in sops.values():
        seen = set()
        current: Optional[str] = sop["doc_id"]
        while current is not None:
            if current in seen:
                _issue(
                    issues, sop["jsonl_file"], sop["jsonl_row"], sop["doc_id"],
                    "supersedes", "supersedes 形成循环",
                )
                break
            seen.add(current)
            nxt = sops.get(current)
            current = nxt["supersedes"] if nxt else None


def _corpus_row(cur: psycopg.Cursor, corpus_id: str) -> Optional[Tuple[Any, ...]]:
    cur.execute(
        """
        SELECT corpus_id, dataset_id, snapshot_sha256, events_sha256, cases_sha256,
               sops_sha256, parser_version, status
        FROM corpus WHERE corpus_id = %s
        """,
        (corpus_id,),
    )
    return cur.fetchone()


def _upsert_failed(
    cur: psycopg.Cursor,
    corpus_id: str,
    dataset_id: str,
    snapshot: Optional[str],
    hashes: Dict[str, str],
) -> None:
    cur.execute(
        """
        INSERT INTO corpus (corpus_id, dataset_id, snapshot_sha256, events_sha256,
                            cases_sha256, sops_sha256, parser_version, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'failed')
        ON CONFLICT (corpus_id) DO UPDATE SET status = 'failed', imported_at = NOW()
        """,
        (
            corpus_id,
            dataset_id,
            snapshot or "",
            hashes.get("events.jsonl", ""),
            hashes.get("cases.jsonl", ""),
            hashes.get("sops.jsonl", ""),
            CORPUS_PARSER_VERSION,
        ),
    )


def import_corpus(
    corpus_id: str,
    dataset_id: str,
    files_dir: Optional[Any] = None,
    conn: Optional[psycopg.Connection] = None,
) -> Dict[str, Any]:
    """Validate and publish a whitelisted corpus version. Idempotent per version."""
    base = Path(files_dir) if files_dir is not None else DEFAULT_CORPUS_DIR
    issues: List[Dict[str, Any]] = []
    _, hashes, snapshot, docs = _load_corpus(base, issues)

    own_conn = conn is None
    if own_conn:
        conn = db.get_admin_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT file_sha256, status FROM dataset WHERE dataset_id = %s", (dataset_id,)
            )
            ds = cur.fetchone()
            if ds is None:
                raise CorpusError(f"数据集不存在: {dataset_id}")
            if ds[1] != "ready":
                raise CorpusError(f"数据集未就绪: {dataset_id} 状态={ds[1]}")
            if snapshot and ds[0] != snapshot:
                _issue(issues, "manifest.json", 0, None, "source_snapshot", "与数据集快照哈希不匹配")

            existing = _corpus_row(cur, corpus_id)
            if existing is not None:
                same_version = (
                    existing[1] == dataset_id
                    and existing[2] == snapshot
                    and existing[3] == hashes.get("events.jsonl")
                    and existing[4] == hashes.get("cases.jsonl")
                    and existing[5] == hashes.get("sops.jsonl")
                    and existing[6] == CORPUS_PARSER_VERSION
                )
                if not same_version:
                    raise CorpusConflictError(
                        f"语料 {corpus_id} 已存在不同版本，拒绝覆盖；请使用新 corpus_id"
                    )

            if issues:
                _upsert_failed(cur, corpus_id, dataset_id, snapshot, hashes)
                conn.commit()
                raise CorpusValidationError("语料校验失败，未发布", issues)

            counts = {"event": 0, "case": 0, "sop": 0}
            for meta in docs:
                counts[meta["doc_type"]] += 1

            if existing is not None and existing[7] == "ready":
                if own_conn:
                    conn.commit()
                return {
                    "corpus_id": corpus_id,
                    "dataset_id": dataset_id,
                    "status": "reused",
                    "docs": counts,
                    "sections": sum(len(d["raw"]["sections"]) for d in docs),
                    "files": hashes,
                    "snapshot_sha256": snapshot,
                    "parser_version": CORPUS_PARSER_VERSION,
                }

            _validate_relations(cur, dataset_id, docs, issues)
            if issues:
                _upsert_failed(cur, corpus_id, dataset_id, snapshot, hashes)
                conn.commit()
                raise CorpusValidationError("语料关系校验失败，未发布", issues)

            if existing is not None:
                cur.execute("DELETE FROM corpus_doc WHERE corpus_id = %s", (corpus_id,))

            cur.execute(
                """
                INSERT INTO corpus (corpus_id, dataset_id, snapshot_sha256, events_sha256,
                                    cases_sha256, sops_sha256, parser_version, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'ready')
                """,
                (
                    corpus_id,
                    dataset_id,
                    snapshot,
                    hashes["events.jsonl"],
                    hashes["cases.jsonl"],
                    hashes["sops.jsonl"],
                    CORPUS_PARSER_VERSION,
                ),
            )
            cur.executemany(
                """
                INSERT INTO corpus_doc (
                    corpus_id, doc_id, doc_type, title, recorded_at, occurred_at, closed_at,
                    policy_id, version, effective_from, effective_to, supersedes, author_role,
                    scope_warehouse, scope_sku, scope_order_id, scope_line_id, history_case_id,
                    jsonl_file, jsonl_row, file_sha256, raw
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                [
                    (
                        corpus_id,
                        m["doc_id"],
                        m["doc_type"],
                        m["title"],
                        m["recorded_at"],
                        m["occurred_at"],
                        m["closed_at"],
                        m["policy_id"],
                        m["version"],
                        m["effective_from"],
                        m["effective_to"],
                        m["supersedes"],
                        m["author_role"],
                        m["scope_warehouse"],
                        m["scope_sku"],
                        m["scope_order_id"],
                        m["scope_line_id"],
                        m["history_case_id"],
                        m["jsonl_file"],
                        m["jsonl_row"],
                        m["file_sha256"],
                        Jsonb(m["raw"]),
                    )
                    for m in docs
                ],
            )

        if own_conn:
            conn.commit()
        return {
            "corpus_id": corpus_id,
            "dataset_id": dataset_id,
            "status": "ready",
            "docs": counts,
            "sections": sum(len(d["raw"]["sections"]) for d in docs),
            "files": hashes,
            "snapshot_sha256": snapshot,
            "parser_version": CORPUS_PARSER_VERSION,
        }
    except CorpusValidationError:
        raise
    except Exception:
        if own_conn:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


def _chunk_content(title: str, heading: str, text: str) -> str:
    return f"{title}\n{heading}\n{text}"


def index_corpus(
    corpus_id: str,
    embedding_model: str,
    embed_fn: Optional[Callable[[str], List[float]]] = None,
    no_embedding: bool = False,
    conn: Optional[psycopg.Connection] = None,
) -> Dict[str, Any]:
    """Build one chunk per section and fill embeddings (retryable, reusable)."""
    own_conn = conn is None
    if own_conn:
        conn = db.get_admin_conn()
    indexed_at = datetime.now(timezone.utc)
    try:
        with conn.cursor() as cur:
            row = _corpus_row(cur, corpus_id)
            if row is None:
                raise CorpusError(f"语料不存在: {corpus_id}")
            if row[7] != "ready":
                raise CorpusError(f"语料未发布: {corpus_id} 状态={row[7]}")

            cur.execute(
                "SELECT doc_id, title, raw FROM corpus_doc WHERE corpus_id = %s"
                " ORDER BY jsonl_file, jsonl_row",
                (corpus_id,),
            )
            expected: List[str] = []
            for doc_id, title, raw in cur.fetchall():
                for section in raw["sections"]:
                    chunk_id = f"{corpus_id}:{doc_id}:{section['section_id']}"
                    expected.append(chunk_id)
                    cur.execute(
                        """
                        INSERT INTO corpus_chunk (
                            chunk_id, corpus_id, doc_id, section_id, heading, content,
                            embedding, embedding_model, indexed_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, %s)
                        ON CONFLICT (chunk_id) DO UPDATE SET
                            heading = EXCLUDED.heading,
                            content = EXCLUDED.content,
                            indexed_at = EXCLUDED.indexed_at,
                            embedding = CASE
                                WHEN corpus_chunk.content = EXCLUDED.content
                                 AND corpus_chunk.embedding_model = EXCLUDED.embedding_model
                                THEN corpus_chunk.embedding
                                ELSE NULL END,
                            embedding_model = EXCLUDED.embedding_model
                        """,
                        (
                            chunk_id,
                            corpus_id,
                            doc_id,
                            section["section_id"],
                            section["heading"],
                            _chunk_content(title, section["heading"], section["text"]),
                            embedding_model,
                            indexed_at,
                        ),
                    )

            expected_set = set(expected)
            cur.execute("SELECT chunk_id FROM corpus_chunk WHERE corpus_id = %s", (corpus_id,))
            stale = [r[0] for r in cur.fetchall() if r[0] not in expected_set]
            if stale:
                cur.execute("DELETE FROM corpus_chunk WHERE chunk_id = ANY(%s)", (stale,))

            filled = failed = 0
            last_error: Optional[str] = None
            if embed_fn is not None and not no_embedding:
                cur.execute(
                    "SELECT chunk_id, content FROM corpus_chunk"
                    " WHERE corpus_id = %s AND embedding IS NULL ORDER BY chunk_id",
                    (corpus_id,),
                )
                for chunk_id, content in cur.fetchall():
                    try:
                        vector = embed_fn(content)
                        if len(vector) != 1024:
                            raise CorpusError(f"向量维度错误: 期望 1024, 实际 {len(vector)}")
                        cur.execute(
                            "UPDATE corpus_chunk SET embedding = %s, embedding_model = %s,"
                            " indexed_at = %s WHERE chunk_id = %s",
                            (vector, embedding_model, indexed_at, chunk_id),
                        )
                        filled += 1
                        if own_conn and filled % 200 == 0:
                            conn.commit()
                    except Exception as e:
                        failed += 1
                        last_error = str(e)

            cur.execute(
                "SELECT COUNT(*), COUNT(embedding) FROM corpus_chunk WHERE corpus_id = %s",
                (corpus_id,),
            )
            total, vector_ready = cur.fetchone()
            total, vector_ready = total or 0, vector_ready or 0

        if own_conn:
            conn.commit()

        if no_embedding or embed_fn is None:
            status = "chunk_ready"
        elif failed == 0 and total > 0 and vector_ready == total:
            status = "ready"
        else:
            status = "partial"
        return {
            "corpus_id": corpus_id,
            "embedding_model": embedding_model,
            "status": status,
            "chunks": total,
            "vector_ready": vector_ready,
            "filled": filled,
            "failed": failed,
            "last_error": last_error,
            "chunks_deleted": len(stale),
            "indexed_at": indexed_at.isoformat(),
        }
    except Exception:
        if own_conn:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


def corpus_status(corpus_id: str, conn: Optional[psycopg.Connection] = None) -> Dict[str, Any]:
    own_conn = conn is None
    if own_conn:
        conn = db.get_reader_conn()
    try:
        with conn.cursor() as cur:
            row = _corpus_row(cur, corpus_id)
            if row is None:
                raise CorpusError(f"语料不存在: {corpus_id}")
            cur.execute(
                "SELECT doc_type, COUNT(*) FROM corpus_doc WHERE corpus_id = %s GROUP BY 1",
                (corpus_id,),
            )
            docs = {t: n for t, n in cur.fetchall()}
            cur.execute(
                "SELECT COUNT(*), COUNT(embedding), MIN(indexed_at), MAX(indexed_at)"
                " FROM corpus_chunk WHERE corpus_id = %s",
                (corpus_id,),
            )
            chunks, vectors, first_at, last_at = cur.fetchone()
        return {
            "corpus_id": corpus_id,
            "dataset_id": row[1],
            "status": row[7],
            "parser_version": row[6],
            "docs": {
                "event": docs.get("event", 0),
                "case": docs.get("case", 0),
                "sop": docs.get("sop", 0),
            },
            "chunks": chunks or 0,
            "vector_ready": vectors or 0,
            "indexed_at": last_at.isoformat() if last_at else None,
        }
    finally:
        if own_conn:
            conn.close()
