# project-pi-market：只读异常调查 RAG（v0.1.0）

基于虚构仓储出库测试数据的最小只读调查程序：Excel 导入 → 精确对账 → 证据检索（词面/向量/RRF 融合）→
会话式有限 Agent（简洁回答、按问题筛选材料）→ 本地 MCP stdio 工具服务（含 SOP 版本沿革）→ 引用校验与隔离评测。

- 只读与范围隔离：调查走只读角色，订单/明细/截止时间由宿主注入，模型不能放宽。
- 有限 Agent：每轮 6 次工具、6 次规划、1 次最终生成上限；失败/未完成如实标记。
- 交互体验：默认简洁回答（先答所问、按需展开），`--debug` 才显示工具 trace。
- 工具服务：四个只读工具可经官方 `mcp` SDK 以本地子进程方式提供（`--transport mcp`）。

发布与变更：见 [CHANGELOG.md](CHANGELOG.md)。

## 环境准备

```bash
# 1. PostgreSQL + pgvector（conda，项目内）
conda create --prefix .local/pg-env -c conda-forge postgresql pgvector -y
.local/pg-env/bin/initdb -D .local/pgdata -U pi_admin --encoding=UTF-8 --locale=C
echo "port = 55432" >> .local/pgdata/postgresql.conf
.local/pg-env/bin/pg_ctl -D .local/pgdata -l .local/pg.log start

# 2. 建库与扩展（首次）
.local/pg-env/bin/psql -h 127.0.0.1 -p 55432 -U pi_admin -d postgres -c "CREATE DATABASE pi_market"
.local/pg-env/bin/psql -h 127.0.0.1 -p 55432 -U pi_admin -d pi_market -c "CREATE EXTENSION vector"

# 3. Python 依赖（Python 3.14；含 mcp SDK，requirements.lock 锁定）
pip install -e ".[dev]"   # 或 pip install openpyxl "psycopg[binary]" openai python-dotenv "mcp>=2.2.0" pytest

# 4. 配置：复制 .env.example 为 .env，填写 DASHSCOPE_API_KEY（向量链）与 DEEPSEEK_API_KEY（生成链）
cp .env.example .env

# 5. 初始化隔离测试库（幂等；pytest 启动时也会自动初始化）
python3 scripts/init_test_db.py
```

## 命令

```bash
# 命令均在项目根目录执行。已 `pip install -e .` 时可直接运行；若仅用源码运行，
# 下列 `python3 -m pi_market.*` 与 `scripts/*` 需要 PYTHONPATH=src（此处统一导出）。
export PYTHONPATH=src

# 导入（生成不可变快照，重复导入幂等）
python3 -m pi_market.cli import deliverables/仓储异常测试数据.xlsx --as-of "2026-09-09 20:00:00+08:00"

# 建立索引（真实向量；--no-embedding 只建 chunk）
python3 -m pi_market.cli index ds-61edff98759f

# 按订单号调查（真实生成模型解释；输出 facts/evidence/explanation 三段 JSON）
python3 -m pi_market.cli investigate ds-61edff98759f SO-1003 --line-id 001

# 按描述检索（模式：phrase / vector / rrf）
python3 -m pi_market.cli search ds-61edff98759f "找包装破损后暂留待检的记录" --mode rrf --k 10

# 导入 full-v2 语料（白名单三文件；manifest/Excel 快照校验；幂等）
python3 -m pi_market.cli import-corpus t004-full-v2 --dataset-id ds-61edff98759f

# 建立语料 section 索引（真实向量；--no-embedding 只建块）
python3 -m pi_market.cli index-corpus t004-full-v2

# 新语料过滤检索（时间/类型/范围过滤先于 Top-k；--type 可重复）
python3 -m pi_market.cli search ds-61edff98759f "包装破损后待检" --corpus t004-full-v2 --type sop --mode rrf

# 扩展调查（事实＋本单证据＋历史参考＋有效规则＋建议；建议引用由程序校验）
python3 -m pi_market.cli investigate ds-61edff98759f SO-1011 --line-id 001 --corpus t004-full-v2 \
  --as-of "2026-09-06T12:00:00+08:00"

# 显式检索方案（历史/规则池）：phrase|vector|rrf|rrf+rerank（与评测同一父文档排名口径）
# 不传 --ranker 保持旧默认路径（chunk 级 RRF），显式传才启用统一方案
python3 -m pi_market.cli investigate ds-61edff98759f SO-1011 --line-id 001 --corpus t004-full-v2 --ranker phrase
python3 -m pi_market.cli search ds-61edff98759f "包装复核" --corpus t004-full-v2 --ranker rrf+rerank

# 有限只读 Agent 终端会话：/order /asof /reset /exit /help
# 非交互（管道演示）：
printf '/order SO-1003 001\n请核对这笔订单的计划与已过账数量\n/exit\n' | \
  python3 -m pi_market.cli chat ds-61edff98759f t004-full-v2

# 交互式 MCP 会话（T-009a；默认仍为宿主内函数调用）。默认只打印回答与未完成提示，
# 加 --debug 显示内部工具 trace（审计用）：
# --transport mcp 需要 mcp SDK，已随 `pip install -e .` 安装并在 requirements.lock 锁定
# （mcp>=2.2.0），无单独安装步骤。直接运行下面的命令，在会话中逐行输入；示例：
#   /order SO-1003 001                  # 绑定订单/明细
#   这批的计划和已过账数量对得上吗？      # 自然语言追问（程序事实）
#   包装受潮按哪版规范处理？              # 规则检索＋回读后给有依据建议
#   /exit                               # 退出（Ctrl-C、EOF 同样关闭本地 MCP 子进程）
python3 -m pi_market.cli chat ds-61edff98759f t004-full-v2 --transport mcp

# MCP 服务也可独立接任意 MCP 客户端（固定范围经启动参数注入；stdout 仅协议消息）
python3 -m pi_market.mcp_server --dataset-id ds-61edff98759f --corpus-id t004-full-v2 \
  --order-id SO-1003 --line-id 001

# 14 例冻结验收批量运行（评测入口；输出供人工审查）
python3 scripts/t006/run_acceptance.py --output .tmp/t006/acceptance_results.jsonl

# 隔离评测（答案与查询集显式提供；业务命令不读这些文件）
python3 -m pi_market.cli evaluate ds-61edff98759f deliverables/评测答案.json tests/eval/eval_queries.jsonl --output report.json

# 测试
python3 -m pytest tests/ -q
```

注意：测试在独立测试库 `pi_market_test`（`.env` 的 `PGDATABASE_TEST`）中运行；破坏性操作前会校验实际连接库名，演示库 `pi_market` 的数据与真实向量索引不会被测试改动。

## 边界

- 调查与检索走只读角色 `pi_reader`（`default_transaction_read_only=on`）；导入/索引用 `pi_admin`
- 业务进程不读取 `deliverables/评测答案.json` 与数据生成说明；查询集标签在 `tests/eval/`（哈希冻结）
- 数值、订单标识、引用位置由程序生成；模型只解释所提供的证据
