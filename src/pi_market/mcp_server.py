"""T-009a MCP stdio Server（D20）：四只读工具的标准协议入口。

用官方 ``mcp`` SDK 2.x（FastMCP 更名后的 ``MCPServer``）承载 stdio Server：
只暴露 inspect_order／read_order_evidence／search_knowledge／read_document，
业务实现直接复用 ``agent_tools.ToolHost``，不重写 SQL、检索或规则。范围经启动
参数注入（一个进程仅一个有效范围），不出现在模型可见工具参数里；stdout 仅
协议消息，日志走 stderr；结构化结果统一为 JSON text ``{"type": "ok|error", ...}``。
"""

import argparse
import json
from collections.abc import Mapping
from typing import Annotated, Any, Callable, Literal, Optional

from mcp import types as mcp_types
from mcp.server.mcpserver import MCPServer
from pydantic import Field

from .agent_tools import TOOL_SCHEMAS, Scope, ToolError, ToolHost, validate_tool_call

SERVER_NAME = "pi-market-tools"
SERVER_VERSION = "0.1.0"

_FUNCTIONS = {schema["function"]["name"]: schema["function"] for schema in TOOL_SCHEMAS}


class _ArgumentGuard:
    """服务端中间件：在 SDK 参数解析前按四工具白名单校验原始参数。

    SDK 的签名模型会忽略未声明的多余字段；这里拒绝未知工具、额外范围参数
    与非法取值，并返回与业务错误一致的 JSON text 结构化失败。
    """

    async def __call__(self, ctx: Any, call_next: Callable) -> Any:
        if getattr(ctx, "method", None) == "tools/call":
            params = getattr(ctx, "params", None)
            if isinstance(params, Mapping):
                try:
                    validate_tool_call(str(params.get("name")), params.get("arguments") or {})
                except ToolError as e:
                    return mcp_types.CallToolResult(
                        content=[
                            mcp_types.TextContent(
                                type="text", text=_payload(error=e.to_dict()["error"])
                            )
                        ]
                    )
                except Exception as e:  # noqa: BLE001
                    return mcp_types.CallToolResult(
                        content=[
                            mcp_types.TextContent(
                                type="text",
                                text=_payload(
                                    error={"category": "invalid_argument", "message": str(e)}
                                ),
                            )
                        ]
                    )
        return await call_next(ctx)


def _description(name: str) -> str:
    return str(_FUNCTIONS[name].get("description", ""))


def _param_description(name: str, param: str) -> str:
    parameters = _FUNCTIONS[name].get("parameters", {})
    return str((parameters.get("properties", {}) or {}).get(param, {}).get("description", ""))


def _payload(result: Optional[dict] = None, error: Optional[dict] = None) -> str:
    if error is not None:
        return json.dumps({"type": "error", "error": error}, ensure_ascii=False)
    return json.dumps({"type": "ok", "data": result}, ensure_ascii=False)


def build_server(scope: Scope) -> MCPServer:
    """Build a stdio server bound to one fixed scope; all tools reuse ToolHost."""

    host = ToolHost(scope)
    server = MCPServer(SERVER_NAME, version=SERVER_VERSION, middleware=[_ArgumentGuard()])

    def _run(name: str, arguments: dict) -> str:
        try:
            result = host.call(name, arguments)
        except ToolError as e:
            return _payload(error=e.to_dict()["error"])
        except Exception as e:  # 基础设施失败必须结构化可见，不静默为空结果
            return _payload(error={"category": "internal_error", "message": str(e)})
        return _payload(result=result)

    @server.tool(name="inspect_order", description=_description("inspect_order"), structured_output=False)
    def inspect_order() -> str:
        return _run("inspect_order", {})

    @server.tool(name="read_order_evidence", description=_description("read_order_evidence"), structured_output=False)
    def read_order_evidence() -> str:
        return _run("read_order_evidence", {})

    @server.tool(name="search_knowledge", description=_description("search_knowledge"), structured_output=False)
    def search_knowledge(
        query: Annotated[
            str, Field(description=_param_description("search_knowledge", "query"))
        ],
        type: Annotated[
            Literal["event", "case", "sop"],
            Field(description=_param_description("search_knowledge", "type")),
        ],
    ) -> str:
        return _run("search_knowledge", {"query": query, "type": type})

    @server.tool(name="read_document", description=_description("read_document"), structured_output=False)
    def read_document(
        doc_id: Annotated[
            str, Field(description=_param_description("read_document", "doc_id"))
        ],
        include_superseded: Annotated[
            bool, Field(description=_param_description("read_document", "include_superseded"))
        ] = False,
    ) -> str:
        return _run(
            "read_document",
            {"doc_id": doc_id, "include_superseded": include_superseded},
        )

    return server


def parse_args(argv: Optional[list] = None) -> Scope:
    parser = argparse.ArgumentParser(prog="pi-market-mcp-server", description="只读调查 MCP 工具服务")
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument("--order-id", default=None)
    parser.add_argument("--line-id", default=None)
    parser.add_argument("--as-of", default=None)
    args = parser.parse_args(argv)
    return Scope(
        dataset_id=args.dataset_id,
        corpus_id=args.corpus_id,
        order_id=args.order_id,
        line_id=args.line_id,
        as_of=args.as_of,
    )


def main(argv: Optional[list] = None) -> int:
    scope = parse_args(argv)
    build_server(scope).run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
