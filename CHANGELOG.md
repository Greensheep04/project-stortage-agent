# Changelog

## 0.1.0（2026-09-13）

### 能力

- Excel 导入与精确对账：计划 vs 已过账净出库、差异与状态标签。
- 语料检索：词面 / 向量 / RRF 融合，时间与范围过滤先于 Top-k；父文档回读与引用绑定。
- 只读异常调查 RAG：`investigate` 输出程序事实、本单证据、历史参考、有效规则与模型解释（引用由程序校验）。
- 有限 Agent 会话（`chat`）：命令（`/order`、`/asof`、`/reset`、`/exit`）与自然语言追问；简洁回答、按问题筛选材料、失败/未完成如实提示；`--debug` 显示工具 trace。
- 本地 MCP stdio 工具服务：四个只读工具经官方 `mcp` SDK（2.2.0）提供，宿主内嵌 Client 管理子进程；范围变更重启进程、断管/超时结构化并显式重连、中断按归属清理。
- SOP 版本沿革：`read_document(..., include_superseded=true)` 返回同政策祖先版本元数据（默认关闭，旧版不入当前规则/回读权限）。

### 运行前提

- Python 3.14 与依赖：`pip install -e ".[dev]"`（或按 `requirements.lock`）；需 PostgreSQL + pgvector（`scripts/init_test_db.py` 初始化隔离测试库）。
- 配置 `.env`：`DASHSCOPE_API_KEY`（向量链）与 `DEEPSEEK_API_KEY`（生成链）；见 `.env.example`。
