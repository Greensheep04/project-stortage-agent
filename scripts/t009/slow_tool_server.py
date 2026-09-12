"""T-009a/b 测试夹具：慢启动 + 工具调用挂起的 MCP Server。

- 启动阶段 sleep 1.5s：模拟冷启动（导入/初始化）慢于调用超时的环境（R1 配对验证）
- 工具调用挂起 30s：验证 call_tool 超时映射与 close 回收

仅用于 tests/test_mcp_protocol.py 的离线超时检查，不参与产品路径。
"""

import asyncio
import time

from mcp.server.mcpserver import MCPServer

time.sleep(1.5)  # 慢启动模拟：initialize 前耗时

server = MCPServer("slow-tool-server", version="0.1.0")


@server.tool(name="inspect_order", description="睡眠后返回，用于触发客户端调用超时")
async def inspect_order() -> str:
    await asyncio.sleep(30)
    return '{"type": "ok", "data": {}}'


if __name__ == "__main__":
    server.run(transport="stdio")
