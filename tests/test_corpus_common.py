"""T-006a 测试共用工具：构造白名单语料目录与最小文档。"""

import json
from datetime import datetime
from pathlib import Path

from pi_market import db
from pi_market.corpus import CORPUS_FILES
from pi_market.importer import _sha256_file

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_XLSX = PROJECT_ROOT / "deliverables" / "仓储异常测试数据.xlsx"
REAL_CORPUS_DIR = PROJECT_ROOT / "deliverables" / "t004" / "full-v2"
DATASET_ID = "ds-61edff98759f"
SNAPSHOT_SHA = _sha256_file(REAL_XLSX)
XLSX_REL = "deliverables/仓储异常测试数据.xlsx"


def real_refs(order_id="SO-1003", line_id="001"):
    """Return (plan_row, warehouse, sku), [(tx_id, flow_row, event_time)], [(ev_id, note_row)]."""
    with db.get_reader_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_row, warehouse, sku FROM plan_line"
                " WHERE dataset_id = %s AND order_id = %s AND line_id = %s",
                (DATASET_ID, order_id, line_id),
            )
            plan = cur.fetchone()
            cur.execute(
                "SELECT tx_id, source_row, event_time FROM wms_flow"
                " WHERE dataset_id = %s AND order_id = %s AND line_id = %s ORDER BY source_row",
                (DATASET_ID, order_id, line_id),
            )
            flows = cur.fetchall()
            cur.execute(
                "SELECT ev_id, source_row FROM evidence_note"
                " WHERE dataset_id = %s AND order_id = %s AND line_id = %s ORDER BY source_row",
                (DATASET_ID, order_id, line_id),
            )
            notes = cur.fetchall()
    return plan, flows, notes


def other_flow_row(order_id="SO-1003", line_id="001") -> int:
    with db.get_reader_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_row FROM wms_flow"
                " WHERE dataset_id = %s AND NOT (order_id = %s AND line_id = %s)"
                " ORDER BY source_row LIMIT 1",
                (DATASET_ID, order_id, line_id),
            )
            return cur.fetchone()[0]


def make_corpus_dir(
    tmp_path, events=(), cases=(), sops=(), snapshot=SNAPSHOT_SHA, mutate_manifest=None
):
    files = {"events.jsonl": list(events), "cases.jsonl": list(cases), "sops.jsonl": list(sops)}
    for name, docs in files.items():
        text = "".join(json.dumps(d, ensure_ascii=False) + "\n" for d in docs)
        (tmp_path / name).write_text(text, encoding="utf-8")
    manifest = {
        "source_snapshot": {"sha256": snapshot},
        "files": [
            {"name": name, "count": len(files[name]), "sha256": _sha256_file(tmp_path / name)}
            for name in CORPUS_FILES
        ],
    }
    if mutate_manifest is not None:
        mutate_manifest(manifest)
    (tmp_path / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path


def event(
    doc_id="EVX-T1",
    text="包装复核记录",
    recorded="2026-09-09T10:00:00+08:00",
    occurred=None,
    order_id="SO-1003",
    line_id="001",
    warehouse=None,
    sku=None,
    transaction_ids=None,
    source_refs=None,
    **over,
):
    """Build an event bound to real snapshot rows of the seeded test dataset."""
    recorded_dt = datetime.fromisoformat(recorded)
    plan, flows, notes = real_refs(order_id, line_id)
    if warehouse is None:
        warehouse = plan[1] if plan else "WH-01"
    if sku is None:
        sku = plan[2] if plan else "BAG-KH"
    usable = [f for f in flows if f[2] <= recorded_dt]
    if transaction_ids is None:
        transaction_ids = [usable[0][0]] if usable else []
    if source_refs is None:
        source_refs = []
        if plan:
            source_refs.append({"file": XLSX_REL, "sheet": "发货计划", "row": plan[0]})
        if usable:
            source_refs.append({"file": XLSX_REL, "sheet": "仓储流水", "row": usable[0][1]})
    if occurred is None:
        occurred = recorded
    doc = {
        "doc_id": doc_id,
        "doc_type": "event",
        "title": f"{order_id}/{line_id} 事件记录",
        "recorded_at": recorded,
        "occurred_at": occurred,
        "author_role": "仓库交接员",
        "scope": {"warehouse_id": warehouse, "sku": sku, "order_id": order_id, "line_id": line_id},
        "transaction_ids": transaction_ids,
        "source_refs": source_refs,
        "sections": [{"section_id": "s1", "heading": "记录", "text": text}],
        "synthetic": True,
    }
    doc.update(over)
    return doc


def case(
    doc_id="CASE-T1",
    text="包装受潮复核经验",
    recorded="2026-08-02T10:00:00+08:00",
    closed="2026-08-01T17:00:00+08:00",
    warehouse="WH-01",
    sku="BAG-KH",
    **over,
):
    doc = {
        "doc_id": doc_id,
        "doc_type": "case",
        "title": f"{doc_id} 历史案例",
        "recorded_at": recorded,
        "closed_at": closed,
        "author_role": "仓储异常专员",
        "scope": {"warehouse_id": warehouse, "sku": sku, "order_id": None, "line_id": None},
        "history_case_id": f"HIST-{doc_id}",
        "rule_refs": [],
        "sections": [{"section_id": "s1", "heading": "经验", "text": text}],
        "synthetic": True,
    }
    doc.update(over)
    return doc


def sop(
    doc_id="SOP-T-v1",
    text="包装异常复核步骤",
    recorded="2026-06-25T09:00:00+08:00",
    effective_from="2026-07-01T00:00:00+08:00",
    effective_to="2026-08-01T00:00:00+08:00",
    policy_id="SOP-T",
    version="1",
    supersedes=None,
    warehouse="WH-01",
    sku=None,
    **over,
):
    doc = {
        "doc_id": doc_id,
        "doc_type": "sop",
        "title": f"{policy_id} v{version} 办法",
        "recorded_at": recorded,
        "author_role": "仓储作业主管",
        "scope": {"warehouse_id": warehouse, "sku": sku, "order_id": None, "line_id": None},
        "policy_id": policy_id,
        "version": version,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "supersedes": supersedes,
        "sections": [{"section_id": "s1", "heading": "步骤", "text": text}],
        "synthetic": True,
    }
    doc.update(over)
    return doc


def drop_corpus(corpus_id: str) -> None:
    with db.admin_cursor() as cur:
        cur.execute("DELETE FROM corpus WHERE corpus_id = %s", (corpus_id,))
