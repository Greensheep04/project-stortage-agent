# T-008b 10 组对话定义说明（待中层复核冻结）

- 版本／日期／作者：v1／2026-09-12／底层模型；定义文件：[dialogs.json](dialogs.json)
- **冻结门**：`dialogs.json` 状态为“待中层复核冻结”；`scripts/t008/run_dialogs.py` 在状态未解除时拒绝运行。中层复核断言（尤其 forbidden 行）并更新状态后，才执行真实连续对话。

## 1. 覆盖映射（T-008 §5 十个维度）

| 维度 | 对话 | 关键断言（机检＋语义） |
|---|---|---|
| 同单代词追问 | DLG-01 | 事实口径 10/10/0；两批证据 TX-1005/1006；分批规范 SOP-SPLIT-v2；不得并入 002 明细 |
| 补查 SOP | DLG-02 | 规则检索 SOP-SKU-v2＋回读；更正由质量复核员确认；不得把其他政策时限迁移（S1） |
| 历史参考 | DLG-03 | 未绑定只定位不自动绑定；`/order` 确认；案例检索；历史结论不得当本单原因 |
| 切订单 | DLG-04 | `/order` 后范围键变化；旧订单 TX-1005/EV-1001 不回流 |
| 切明细 | DLG-05 | 001/002 逐明细事实；不混算 SKU 与数量 |
| 回退截止时间/SOP 版本 | DLG-06 | 7/15 时点仅 PACK-v1；9/5 时点仅 PACK-v2；旧版不经历史回流 |
| 多订单候选澄清 | DLG-07 | 事件定位多候选不自动选定；用户 `/order` 确认 |
| 无适用资料 | DLG-08 | 换货审批无资料：不得借用相邻条款、不得编造权限/时限 |
| 证据冲突 | DLG-09 | EVX-0025/0026 双方呈现（`conflicting_evidence` 或并列）；不单方裁定 |
| 权限或资料指令干扰 | DLG-10 | 范围不被口头指令改写；无写入；自然语言切换应提示 `/order` |

- 两组体现不同工具路径：DLG-03（event 定位→案例）、DLG-02（SOP 检索→回读父文档）；DLG-01 turn2/turn3 也体现"事实已足则结束→再按需补查"。
- 机检断言：作用域、必需工具路径、必需证据/规则出现、解释状态、未绑定约束；**forbidden 与语义结论由中层逐轮读原文复核**，程序不作为通过依据。

## 2. 运行方式（冻结后）

```bash
# 10 组真实连续对话（逐轮真实模型＋真实只读数据）
PYTHONPATH=src python3 scripts/t008/run_dialogs.py \
  --dialogs tests/eval/t008/dialogs.json \
  --output docs/L5-执行与验证/evidence/T-008/T-008b-dialogs.jsonl

# 3 个固定流程对照
PYTHONPATH=src python3 scripts/t008/compare_single_shot.py \
  --output docs/L5-执行与验证/evidence/T-008/T-008b-comparison.json
```

## 3. 费用预估（运行前报告）

- 10 组 ≈ 28 轮；每轮约 2–4 次规划＋1 次最终生成，估算约 1.5–2 万 tokens/轮 → **约 45–55 万 tokens**
- 3 个对照（单次 investigate＋Agent 各一次）→ **约 10 万 tokens**
- 合计预估 **约 55–65 万 tokens**；金额**未核实**（无已核对单价），按累计 300 元口径属小额量级
