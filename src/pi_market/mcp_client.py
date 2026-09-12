"""T-009a 宿主内嵌 MCP Client 与 ToolHost 适配（D21/D22）。

用官方 ``mcp`` SDK 的 ``ClientSession`` 以 stdio 启动项目内固定 Server 入口
（``python -m pi_market.mcp_server``），一个进程仅承载一个范围。同步会话跑在
后台线程的事件循环上；initialize／tools/list 属连接操作，单独记录且不计入
业务工具请求；工具调用经 ``call_tool`` 顺序执行，参数先由共享校验拒绝，结果
按 JSON text 契约解包，错误沿用 out_of_scope／internal_error 分类。
"""

import asyncio
import concurrent.futures
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from .agent_tools import TOOL_NAMES, Scope, ToolError, validate_tool_call

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
CONNECT_TIMEOUT = 120.0  # 启动/初始化阶段（含冷启动导入），显著大于调用超时
CALL_TIMEOUT = 60.0  # 单次工具调用
CLOSE_TIMEOUT = 10.0


def to_openai_schemas(tools: List[Any]) -> List[Dict[str, Any]]:
    """Convert MCP tools/list into the model Tool Calls schema (D21.2 whitelist)."""
    names = sorted(getattr(tool, "name", "") for tool in tools)
    if names != sorted(TOOL_NAMES):
        raise ToolError(f"MCP 工具列表与白名单不符: {names}", "internal_error")
    schemas: List[Dict[str, Any]] = []
    for tool in tools:
        schema = getattr(tool, "input_schema", None) or {"type": "object", "properties": {}}
        if schema.get("type") != "object":
            raise ToolError(f"MCP 工具 {tool.name} 的参数 schema 不是 object", "internal_error")
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": getattr(tool, "description", "") or "",
                    "parameters": schema,
                },
            }
        )
    return schemas


class McpServerClient:
    """Sync wrapper around one MCP stdio ClientSession subprocess."""

    def __init__(
        self,
        scope: Scope,
        *,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        connect_timeout: float = CONNECT_TIMEOUT,
        call_timeout: float = CALL_TIMEOUT,
        close_timeout: float = CLOSE_TIMEOUT,
    ):
        self.scope = scope
        self.command = command or sys.executable
        if args is None:
            args = [
                "-m",
                "pi_market.mcp_server",
                "--dataset-id",
                scope.dataset_id,
                "--corpus-id",
                scope.corpus_id,
            ]
            if scope.order_id:
                args += ["--order-id", scope.order_id]
            if scope.line_id:
                args += ["--line-id", scope.line_id]
            if scope.as_of:
                args += ["--as-of", scope.as_of]
        self.args = list(args)
        self.cwd = str(cwd or PROJECT_ROOT)
        self.env = self._build_env(env)
        self.connect_timeout = connect_timeout
        self.call_timeout = call_timeout
        self.close_timeout = close_timeout

        self.protocol_version: Optional[str] = None
        self.server_info: Optional[str] = None
        self.tools: List[Any] = []
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._session: Optional[ClientSession] = None
        self._stop: Optional[asyncio.Event] = None
        self._ready = threading.Event()
        self._start_error: Optional[BaseException] = None
        self._broken = False
        self._pending: set = set()

    def _build_env(self, extra: Optional[Dict[str, str]]) -> Dict[str, str]:
        env = dict(os.environ)
        pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{pythonpath}" if pythonpath else str(SRC_DIR)
        if extra:
            env.update(extra)
        return env

    # ------------------------------------------------------------------ lifecycle
    @property
    def alive(self) -> bool:
        return bool(
            self._thread is not None
            and self._thread.is_alive()
            and self._session is not None
            and not self._broken
        )

    def start(self) -> None:
        if self.alive:
            return
        self._broken = False
        self._start_error = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pi-market-mcp", daemon=True)
        self._thread.start()
        try:
            if not self._ready.wait(self.connect_timeout + CLOSE_TIMEOUT):
                raise ToolError("MCP Server 启动超时（initialize/tools/list 超时）", "internal_error")
            if self._start_error is not None:
                raise ToolError(f"MCP Server 启动失败：{self._start_error}", "internal_error")
        except BaseException:
            # 线程/进程可能已建立：start 自身负责回收后再传播（含 KeyboardInterrupt/SystemExit）
            self.close()
            raise

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._serve())
        except BaseException as e:  # noqa: BLE001 - surfaced to the caller as structured failure
            self._start_error = self._start_error or e
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            self._loop = None
            self._ready.set()

    async def _serve(self) -> None:
        self._stop = asyncio.Event()
        params = StdioServerParameters(
            command=self.command,
            args=self.args,
            env=self.env,
            cwd=self.cwd,
        )
        try:
            async with stdio_client(params, errlog=sys.stderr) as (read, write):
                # read_timeout 不设会话级默认：initialize/list_tools 只受 connect_timeout
                # 约束，call_tool 才显式传入 call_timeout（R1）。
                async with ClientSession(read, write, read_timeout_seconds=None) as session:
                    init = await asyncio.wait_for(session.initialize(), self.connect_timeout)
                    listing = await asyncio.wait_for(session.list_tools(), self.connect_timeout)
                    self.protocol_version = getattr(init, "protocol_version", None)
                    info = getattr(init, "server_info", None)
                    self.server_info = (
                        f"{getattr(info, 'name', '?')} {getattr(info, 'version', '')}".strip()
                    )
                    self.tools = list(getattr(listing, "tools", []) or [])
                    self._session = session
                    self._ready.set()
                    await self._stop.wait()
        finally:
            self._session = None

    def close(self) -> bool:
        thread, loop, stop = self._thread, self._loop, self._stop
        if thread is None:
            return True
        for future in list(self._pending):  # 取消在途调用，帮助事件循环退出
            future.cancel()
        if loop is not None and stop is not None:
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass
        thread.join(timeout=self.close_timeout)
        if thread.is_alive():
            # 不能把未回收的进程/线程句柄当清理成功丢弃，保留以便重试
            return False
        self._thread = None
        self._session = None
        self._loop = None
        self._stop = None
        return True

    # ------------------------------------------------------------------ calls
    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name not in TOOL_NAMES:
            raise ToolError(f"未知工具: {name}", "unknown_tool", {"tool": name})
        session, loop, thread = self._session, self._loop, self._thread
        if session is None or loop is None or thread is None or not thread.is_alive():
            raise ToolError("MCP 连接不可用（服务可能已退出）", "internal_error")
        future = asyncio.run_coroutine_threadsafe(
            session.call_tool(name, arguments, read_timeout_seconds=self.call_timeout), loop
        )
        self._pending.add(future)
        try:
            result = future.result(timeout=self.call_timeout + 5)
        except concurrent.futures.TimeoutError:
            self._broken = True  # 下一页显式重连，不后台无限重试
            raise ToolError(f"MCP 工具调用超时（{name}）", "internal_error")
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001 - 断管/服务退出等统一结构化
            self._broken = True
            raise ToolError(f"MCP 工具调用失败（{name}）：{e}", "internal_error")
        finally:
            self._pending.discard(future)
        return self._parse_result(name, result)

    _ARGUMENT_ERROR_MARKERS = (
        "validation error",
        "field required",
        "unexpected keyword",
        "rejected arguments",
        "invalid arguments",
        "input validation",
    )

    @classmethod
    def _parse_result(cls, name: str, result: Any) -> Dict[str, Any]:
        is_error = bool(getattr(result, "is_error", False))
        texts = [
            getattr(item, "text", "")
            for item in (getattr(result, "content", None) or [])
            if getattr(item, "type", "") == "text"
        ]
        raw = texts[0] if texts else ""
        payload: Optional[Dict[str, Any]] = None
        if raw:
            try:
                loaded = json.loads(raw)
                payload = loaded if isinstance(loaded, dict) else None
            except json.JSONDecodeError:
                payload = None

        if is_error:
            # 协议层错误状态优先：任何包装都不得当成功返回
            if payload is not None and payload.get("type") == "error":
                error = payload.get("error") or {}
                raise ToolError(
                    str(error.get("message", "MCP 工具失败")),
                    str(error.get("category", "internal_error")),
                    error.get("detail"),
                )
            message = f"MCP 工具错误（{name}）：{raw[:300] or '无错误文本'}"
            lowered = raw.lower()
            if any(marker in lowered for marker in cls._ARGUMENT_ERROR_MARKERS):
                raise ToolError(message, "invalid_argument")  # 明确参数拒绝不算技术失败
            # 矛盾组合（is_error=True 却给 ok/未知包装）与未知执行错误都按技术失败处理
            raise ToolError(message, "internal_error")

        if payload is not None and payload.get("type") == "ok":
            data = payload.get("data")
            if isinstance(data, dict):
                return data
            raise ToolError(f"MCP 返回 data 不是对象（{name}）", "internal_error")
        if payload is not None and payload.get("type") == "error":
            error = payload.get("error") or {}
            raise ToolError(
                str(error.get("message", "MCP 工具失败")),
                str(error.get("category", "internal_error")),
                error.get("detail"),
            )
        raise ToolError(f"MCP 返回无法解析（{name}）", "internal_error")


class McpToolHost:
    """ToolHost-shaped MCP backend: every business call goes through call_tool."""

    def __init__(
        self,
        scope: Scope,
        *,
        client_factory: Optional[Callable[[], McpServerClient]] = None,
    ):
        self.scope = scope
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.events: List[Dict[str, Any]] = []
        self._client_factory = client_factory or (lambda: McpServerClient(scope))
        self._client: Optional[McpServerClient] = None
        self._schemas: Optional[List[Dict[str, Any]]] = None
        self._dangling: List[McpServerClient] = []
        self.generation = 0

    # ------------------------------------------------------------------ connection
    def _release_client(self, client: McpServerClient) -> bool:
        """Close a created client; keep an un-closed handle registered for retry."""
        try:
            clean = bool(client.close())
        except Exception:
            clean = False
        if not clean and client not in self._dangling:
            self._dangling.append(client)
        return clean

    def _ensure_connected(self) -> None:
        if self._client is not None and self._client.alive:
            return
        # 成功代次是生命周期事实：失败尝试不清零，后续成功仍属“重连”（F1.1）
        had_previous_connection = self.generation > 0
        if self._client is not None:
            if not self._client.alive:
                self.cache.clear()  # 断连立即失效；启动失败也不恢复旧缓存
            self._release_client(self._client)
            self._client = None
        started = time.perf_counter()
        # 创建即负责清理：start/工具发现/schema 检查全部成功后才提交（F3.1）
        client = self._client_factory()
        try:
            client.start()
            schemas = to_openai_schemas(client.tools)
        except BaseException as e:
            self._release_client(client)
            self.events.append(
                {
                    "op": "mcp_connect",
                    "status": "failed",
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                    "error": str(e),
                }
            )
            raise
        self._client = client
        self._schemas = schemas
        self.generation += 1
        if had_previous_connection:
            self.cache.clear()
        event = {
            "op": "mcp_reconnect" if had_previous_connection else "mcp_connect",
            "status": "ok",
            "transport": "stdio",
            "server": client.server_info,
            "protocol_version": client.protocol_version,
            "generation": self.generation,
            "tools": [schema["function"]["name"] for schema in schemas],
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }
        if had_previous_connection:
            event["cache_invalidated"] = True
        self.events.append(event)

    def tool_schemas(self) -> List[Dict[str, Any]]:
        self._ensure_connected()
        return list(self._schemas or [])

    def take_events(self) -> List[Dict[str, Any]]:
        events, self.events = self.events, []
        return events

    def close(self) -> bool:
        clean = True
        if self._client is not None:
            if self._client.close():
                self._client = None
            else:
                clean = False  # 未回收句柄保留在 self._client 供重试
            self.events.append({"op": "mcp_close", "clean": clean})
        else:
            self.events.append({"op": "mcp_close", "clean": True})
        for client in list(self._dangling):
            if client.close():
                self._dangling.remove(client)
            else:
                clean = False
        return clean

    # ------------------------------------------------------------------ tools
    def call(self, name: str, arguments: Any) -> Dict[str, Any]:
        args = validate_tool_call(name, arguments)
        self._ensure_connected()
        assert self._client is not None
        return self._client.call_tool(name, args)

    def inspect_order(self) -> Dict[str, Any]:
        return self.call("inspect_order", {})

    def read_order_evidence(self) -> Dict[str, Any]:
        return self.call("read_order_evidence", {})
