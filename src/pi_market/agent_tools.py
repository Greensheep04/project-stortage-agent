"""T-008a 四个只读工具与宿主范围注入（D18）。

工具白名单与 JSON 参数校验在程序侧；未知工具/非法参数/越界对象或来源返回结构化
错误且不执行。模型无法指定 dataset/corpus/仓库/SKU/时间等范围，也无法访问
SQL/文件/Shell/网络或任何写操作。四工具复用现有 reconcile/investigate/
corpus_retrieval 实现，不重复业务规则。
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import corpus_retrieval, db, investigate, reconcile
from .corpus import CorpusError

TOOL_NAMES = ("inspect_order", "read_order_evidence", "search_knowledge", "read_document")
TOP_K = 5
DOC_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+$")

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "inspect_order",
            "description": "核对当前绑定订单/明细的计划与已过账净出库（对象由宿主注入，无参数）",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_order_evidence",
            "description": "读取当前订单的原说明、流水与新事件（对象与截止时间由宿主注入，无参数）",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "按描述检索资料：event（未绑定时用于定位订单）/case（历史案例）/sop（有效规则），一次一个类型，Top-5",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索描述"},
                    "type": {"type": "string", "enum": ["event", "case", "sop"]},
                },
                "required": ["query", "type"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": "回读本会话检索结果中的父文档全文（时间与范围再次校验）；include_superseded=true 时仅对 SOP 额外返回同政策祖先版本元数据",
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string"},
                    "include_superseded": {
                        "type": "boolean",
                        "description": "仅 SOP：附带同政策版本沿革元数据（默认 false，不返回旧版名单）",
                    },
                },
                "required": ["doc_id"],
                "additionalProperties": False,
            },
        },
    },
]


class ToolError(Exception):
    """Structured tool failure; never executes the requested action."""

    def __init__(self, message: str, category: str = "invalid_argument", detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.category = category
        self.detail = detail or {}

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"category": self.category, "message": str(self)}
        if self.detail:
            payload["detail"] = self.detail
        return {"error": payload}


def parse_tool_arguments(arguments: Any) -> Dict[str, Any]:
    if arguments in (None, ""):
        return {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as e:
            raise ToolError(f"参数不是合法 JSON: {e}", "invalid_argument")
    if not isinstance(arguments, dict):
        raise ToolError("参数必须是 JSON 对象", "invalid_argument")
    return arguments


def validate_tool_call(name: str, arguments: Any) -> Dict[str, Any]:
    """Shared structural validation for function and MCP backends (D21.2).

    Rejects unknown tools, non-JSON payloads, extra/scope arguments, bad types
    and malformed doc ids before any business function or RPC executes.
    """
    if name not in TOOL_NAMES:
        raise ToolError(f"未知工具: {name}", "unknown_tool", {"tool": name})
    args = parse_tool_arguments(arguments)
    if name in ("inspect_order", "read_order_evidence"):
        if args:
            raise ToolError(f"{name} 不接受参数（对象由宿主注入）", "invalid_argument")
        return args
    if name == "search_knowledge":
        extra = sorted(set(args) - {"query", "type"})
        if extra:
            raise ToolError(f"不允许的参数: {', '.join(extra)}", "invalid_argument")
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolError("query 必须为非空字符串", "invalid_argument")
        if args.get("type") not in ("event", "case", "sop"):
            raise ToolError("type 必须是 event/case/sop", "invalid_argument")
        return args
    extra = sorted(set(args) - {"doc_id", "include_superseded"})
    if extra:
        raise ToolError(f"不允许的参数: {', '.join(extra)}", "invalid_argument")
    doc_id = args.get("doc_id")
    if not isinstance(doc_id, str) or not DOC_ID_PATTERN.match(doc_id):
        raise ToolError(f"非法 doc_id: {doc_id!r}（只接受文档 ID，不接受路径）", "invalid_argument")
    if "include_superseded" in args and not isinstance(args["include_superseded"], bool):
        raise ToolError("include_superseded 必须是布尔值", "invalid_argument")
    return args


@dataclass
class Scope:
    dataset_id: str
    corpus_id: str
    order_id: Optional[str] = None
    line_id: Optional[str] = None
    as_of: Optional[str] = None


@dataclass
class ToolHost:
    """Executes whitelisted tools for one session scope, with per-scope reuse."""

    scope: Scope
    facts: Optional[Dict[str, Any]] = None
    cache: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    seen_docs: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # ------------------------------------------------------------------ dispatch
    def call(self, name: str, arguments: Any) -> Dict[str, Any]:
        args = validate_tool_call(name, arguments)
        if name == "inspect_order":
            return self.inspect_order()
        if name == "read_order_evidence":
            return self.read_order_evidence()
        if name == "search_knowledge":
            return self.search_knowledge(args)
        return self.read_document(args)

    # ------------------------------------------------------------------ helpers
    def _parse_args(self, arguments: Any) -> Dict[str, Any]:
        return parse_tool_arguments(arguments)

    def _no_args(self, name: str, args: Dict[str, Any]) -> None:
        if args:
            raise ToolError(f"{name} 不接受参数（对象由宿主注入）", "invalid_argument")

    def _reject_extra(self, args: Dict[str, Any], allowed: set) -> None:
        extra = sorted(set(args) - allowed)
        if extra:
            raise ToolError(f"不允许的参数: {', '.join(extra)}", "invalid_argument")

    @property
    def bound(self) -> bool:
        return bool(self.scope.order_id)

    def _lines(self) -> List[Dict[str, Any]]:
        if self.facts is None:
            self.facts = reconcile.reconcile(
                self.scope.dataset_id,
                self.scope.order_id,
                line_id=self.scope.line_id,
                as_of=investigate._normalize_as_of(self.scope.as_of),
            )
        return self.facts.get("lines", [])

    def _line_ids(self) -> Optional[List[str]]:
        return [self.scope.line_id] if self.scope.line_id else None

    def _order_events(self) -> List[Dict[str, Any]]:
        with db.get_reader_conn() as conn:
            with conn.cursor() as cur:
                self._lines()
                return investigate._fetch_corpus_events(
                    cur,
                    self.scope.corpus_id,
                    self.scope.order_id,
                    self._line_ids(),
                    self.facts["as_of"],
                )

    def _remember(self, doc_id: str, title: str, doc_type: str, scope: Tuple[Any, ...]) -> None:
        self.seen_docs[doc_id] = {"title": title, "doc_type": doc_type, "scope": scope}

    # ------------------------------------------------------------------ tools
    def inspect_order(self) -> Dict[str, Any]:
        if not self.bound:
            raise ToolError("未绑定订单：请使用 /order <订单> [明细]", "no_scope")
        facts = reconcile.reconcile(
            self.scope.dataset_id,
            self.scope.order_id,
            line_id=self.scope.line_id,
            as_of=investigate._normalize_as_of(self.scope.as_of),
        )
        self.facts = facts
        return {
            "tool": "inspect_order",
            "source": "reconcile.reconcile",
            "order_id": facts["order_id"],
            "line_id": facts["line_id"],
            "as_of": facts["as_of"],
            "facts": facts,
        }

    def read_order_evidence(self) -> Dict[str, Any]:
        if not self.bound:
            raise ToolError("未绑定订单：请使用 /order <订单> [明细]", "no_scope")
        self._lines()
        facts = self.facts
        with db.get_reader_conn() as conn:
            with conn.cursor() as cur:
                source_filename = investigate._fetch_source_filename(cur, self.scope.dataset_id)
                evidence = investigate._collect_evidence(
                    cur, self.scope.dataset_id, facts, source_filename
                )
                events = investigate._fetch_corpus_events(
                    cur,
                    self.scope.corpus_id,
                    self.scope.order_id,
                    self._line_ids(),
                    facts["as_of"],
                )
        for event in events:
            self._remember(
                event["doc_id"],
                event["title"],
                "event",
                (event.get("scope_warehouse"), event.get("scope_sku"), self.scope.order_id, event.get("line_id")),
            )
        return {
            "tool": "read_order_evidence",
            "source": "investigate._collect_evidence + corpus_doc",
            "order_id": facts["order_id"],
            "as_of": facts["as_of"],
            "evidence": evidence,
            "events": events,
        }

    def search_knowledge(self, args: Dict[str, Any]) -> Dict[str, Any]:
        self._reject_extra(args, {"query", "type"})
        query = args.get("query")
        doc_type = args.get("type")
        if not isinstance(query, str) or not query.strip():
            raise ToolError("query 必须为非空字符串", "invalid_argument")
        if doc_type not in ("event", "case", "sop"):
            raise ToolError("type 必须是 event/case/sop", "invalid_argument")
        if not self.bound and doc_type != "event":
            raise ToolError(
                "未绑定订单时仅允许 event 定位搜索与 read_document 回读", "unbound_restricted"
            )

        if doc_type == "event" and self.bound:
            # 已绑定：精确限定对象（复用 investigate 的事件读取），过滤先于截断
            events = self._order_events()
            events = sorted(events, key=lambda e: (e["recorded_at"], e["doc_id"]), reverse=True)
            top = events[:TOP_K]
            for event in top:
                self._remember(
                    event["doc_id"],
                    event["title"],
                    "event",
                    (None, None, self.scope.order_id, event.get("line_id")),
                )
            return {
                "tool": "search_knowledge",
                "type": "event",
                "source": "corpus_doc（精确对象）",
                "results": top,
                "truncated": max(0, len(events) - TOP_K),
            }

        if doc_type == "event":
            ranked = corpus_retrieval.rank_parents(
                self.scope.dataset_id,
                self.scope.corpus_id,
                query,
                "event",
                as_of=self.scope.as_of,
                mode="phrase",
                candidate_depth=100,
                parent_top=TOP_K,
            )
            results = ranked["parents"]
            for row in results:
                self._remember(
                    row["doc_id"],
                    row["title"],
                    "event",
                    (row.get("scope_warehouse"), row.get("scope_sku"), row.get("scope_order_id"), row.get("scope_line_id")),
                )
            return {
                "tool": "search_knowledge",
                "type": "event",
                "source": "corpus_retrieval.rank_parents(phrase)",
                "results": results,
            }

        self._lines()
        facts = self.facts
        pool = investigate._search_pools(
            self.scope.dataset_id,
            self.scope.corpus_id,
            query,
            facts.get("lines", []),
            facts["as_of"],
            "phrase",
            None,
            None,
            scheme="phrase",
        )
        rows = sorted(pool[doc_type].values(), key=lambda r: r["rank"])[:TOP_K]
        for row in rows:
            warehouse, sku = row.get("_scope", (None, None))
            self._remember(
                row["doc_id"],
                row.get("title", ""),
                doc_type,
                (warehouse, sku, None, None),
            )
        return {
            "tool": "search_knowledge",
            "type": doc_type,
            "source": "corpus_retrieval.rank_parents(phrase，范围由宿主注入)",
            "results": rows,
        }

    def read_document(self, args: Dict[str, Any]) -> Dict[str, Any]:
        self._reject_extra(args, {"doc_id", "include_superseded"})
        doc_id = args.get("doc_id")
        if not isinstance(doc_id, str) or not DOC_ID_PATTERN.match(doc_id):
            raise ToolError(f"非法 doc_id: {doc_id!r}（只接受文档 ID，不接受路径）", "invalid_argument")
        meta = self.seen_docs.get(doc_id)
        if meta is None:
            raise ToolError(
                f"文档 {doc_id} 不在本会话的工具结果中，不能回读未检索到的文档", "not_seen"
            )
        include_superseded = bool(args.get("include_superseded", False))
        if include_superseded and meta.get("doc_type") != "sop":
            raise ToolError("include_superseded 仅支持 SOP 回读", "invalid_argument")
        warehouse, sku, order_id, line_id = meta["scope"]
        try:
            document = corpus_retrieval.read_corpus_doc(
                self.scope.corpus_id,
                doc_id,
                as_of=self.scope.as_of,
                warehouse=warehouse,
                sku=sku,
                order_id=order_id,
                line_id=line_id,
            )
        except CorpusError as e:
            # 权限/范围/不存在属合法拒绝，不是基础设施失败
            raise ToolError(f"回读被拒绝：{e}", "out_of_scope")
        except Exception as e:
            # 数据库超时、连接错误等基础设施失败必须可见
            raise ToolError(f"回读失败：{e}", "internal_error")
        result = {
            "tool": "read_document",
            "source": "corpus_retrieval.read_corpus_doc（重查时间/范围）",
            "document": document,
        }
        if include_superseded:
            try:
                history = corpus_retrieval.read_corpus_doc_history(
                    self.scope.corpus_id,
                    doc_id,
                    as_of=self.scope.as_of,
                    warehouse=warehouse,
                    sku=sku,
                    order_id=order_id,
                    line_id=line_id,
                )
            except CorpusError as e:
                raise ToolError(f"沿革读取被拒绝：{e}", "out_of_scope")
            except Exception as e:
                raise ToolError(f"沿革读取失败：{e}", "internal_error")
            result["source"] = "corpus_retrieval.read_corpus_doc + read_corpus_doc_history"
            result["version_history"] = history["versions"]
            result["version_history_complete"] = history["complete"]
            result["version_history_reason"] = history["reason"]
        return result
