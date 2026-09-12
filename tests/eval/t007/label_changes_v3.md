# T-007 评测集 v3 标签更正与完整判定覆盖

- 版本／日期／作者：v3／2026-09-11／底层模型；依据 [T-007c 返工交接](../../../docs/L5-执行与验证/交接/T-007c-返工交接.md) 与高层复审-2
- 披露：冻结测试集已于 T-007b 曝光；v3 为**完整判定覆盖**的标签更正版本，成绩按已保存排名离线重算，不构成盲测
- 历史版本保留：v1（冻结）、v2（返回候选补标）文件与报告均未改动

## 1. 方法（组定义＋逐查询映射，取代“只补返回候选”）

- 为 280 案例＋20 SOP 建立 47 组的内容化归组清单：[doc_groups_v3.md](doc_groups_v3.md)（组定义按正文回答的问题与边界，含成员与代表正文）
- 每查询映射：**direct 组＝等级 2**、**related 组＝等级 1**、其余组＝等级 0；doc 级 **exceptions** 记录同组内的必要例外（如受潮组内“待查结案”子类、签收缺失组内“遗失/跟进”区别、升级组内“部分未闭环/等待回填”区别）
- 时间／范围排除由明确规则实现：案例 closed_at 与 recorded_at ≤ T；SOP recorded_at ≤ T 且 effective_from ≤ T < effective_to；仓库/SKU 精确或 null 通用
- 负向题覆盖全部可见范围（全组映射 0，逐篇写明不适用理由）
- 生成后校验：可见未判定=0；正向题等级 2 ≥1 且 ≤10；负向题无等级 2；v2 等级 2 无静默降级（0 例）

## 2. 完整覆盖统计（全体可见父文档）

| 集合 | 查询 | v3 标签总数 | 可见未判定 | 等级 2 单题上限 |
|---|---|---|---|---|
| dev_v3 | 20 | 1572 | 0 | 7 |
| test_v3 | 40 | 3918 | 0 | 8 |

（高层口径复核：dev_v2 余 822 项／7 问、test_v2 余 2,738 项／24 问，v3 全部覆盖，未判定归零。）

## 3. 全文审查发现并修正的标签问题

- CASE-0003（DEV-C01）：正文为“货位账面数量记录滞后导致短拣”，与 CASE-0050/0052 同组直接相关，v2 误标等级 1 → v3 等级 2
- CASE-0143（DEV-C07）：与 0141/0145/0147 同组（签收材料缺失），v2 组内不一致（0）→ v3 统一按 related 等级 1
- TEST-C15 的 CASE-0178：同类“差异材料不足待查”未进 v2 候选而未覆盖（高层点名）→ v3 经组映射等级 1
- 分批汇总组拆分：登记延迟（0119/0122）与汇总核对分成两组，避免“登记延迟”题把汇总案例误当直接相关
- 其余同组等级不一致按例外固化（DEV-C04 受潮待查子类、DEV-C05 受潮基础案例、TEST-C07 遗失/补件、TEST-C12 未闭环/等待回填）

## 4. 逐查询组映射（direct＝2 / related＝1 / exceptions）

### dev

| qid | direct（等级2） | related（等级1） | exceptions |
|---|---|---|---|
| DEV-C01 | CG-SHORT-NOREG, CG-SHORT-REC | CG-LINE-MIX, CG-STOCK-CHECK | — |
| DEV-C02 | CG-REV-OVER | CG-REV-LATE | — |
| DEV-C03 | CG-DRAFT-CROSS | CG-DRAFT-LATE | — |
| DEV-C04 | CG-PACK-DAMP | — | CASE-0014→2, CASE-0016→2, CASE-0018→2, CASE-0020→1 |
| DEV-C05 | CG-PACK-DAMP | — | CASE-0001→1 |
| DEV-C06 | CG-PACK-VERSION | CG-PACK-CLAIM | — |
| DEV-C07 | CG-RECEIPT-LATE | CG-RECEIPT-MISS | — |
| DEV-C08 | CG-HANDOVER-INCONSIST | CG-HANDOVER-PENDING | — |
| DEV-S01 | SG-PACK | SG-SKU | — |
| DEV-S02 | SG-SKU | SG-PACK | — |
| DEV-S03 | SG-DIFF | SG-SPLIT | — |
| DEV-S04 | SG-REVERSE | SG-DIFF | — |
| DEV-S05 | SG-DRAFT | SG-HANDOVER | — |
| DEV-S06 | SG-PACK | SG-DIFF | — |
| DEV-S07 | SG-RECEIPT | SG-HANDOVER | — |
| DEV-S08 | SG-ESCALATE | SG-HANDOVER | — |

负向题（4 问）：direct/related 均为空，全部可见组判 0（不适用理由逐篇写入标签）。

### test

| qid | direct（等级2） | related（等级1） | exceptions |
|---|---|---|---|
| TEST-C01 | CG-SHORT-MULTI | CG-SHORT-REC | — |
| TEST-C02 | CG-SHORT-NOREG | CG-DIFF-INSUFF | — |
| TEST-C03 | CG-DIFF-LATE | CG-DRAFT-LATE | — |
| TEST-C04 | CG-LINE-MIX | CG-COUNT-WRONG | — |
| TEST-C05 | CG-REV-LINK | — | — |
| TEST-C06 | CG-DRAFT-LATE | CG-DRAFT-CROSS | — |
| TEST-C07 | CG-RECEIPT-MISS | — | CASE-0141→1 |
| TEST-C08 | CG-RECEIPT-MISS | CG-RECEIPT-LATE | — |
| TEST-C09 | CG-SPLIT-CUSTOMER | CG-SPLIT-SUM | — |
| TEST-C10 | CG-SPLIT-LATE | CG-SPLIT-SUM, CG-SPLIT-WAVE | — |
| TEST-C11 | CG-HANDOVER-PENDING | CG-HANDOVER-INCONSIST | — |
| TEST-C12 | CG-ESCALATE-PATH | — | CASE-0204→1, CASE-0210→1 |
| TEST-C13 | CG-ARCHIVE-MISS | CG-ARCHIVE-DUE | — |
| TEST-C14 | CG-PACK-CLAIM | CG-PACK-VERSION | — |
| TEST-C15 | CG-SHORT-NOREG | CG-DIFF-INSUFF | — |
| TEST-C16 | CG-RECEIPT-LATE | CG-RECEIPT-MISS | — |
| TEST-S01 | SG-PACK | SG-DIFF | — |
| TEST-S02 | SG-PACK | SG-DIFF | — |
| TEST-S03 | SG-SKU | SG-PACK | — |
| TEST-S04 | SG-DRAFT | SG-DIFF | — |
| TEST-S05 | SG-REVERSE | SG-DIFF | — |
| TEST-S06 | SG-SPLIT | SG-DIFF | — |
| TEST-S07 | SG-SPLIT | SG-DIFF | — |
| TEST-S08 | SG-RECEIPT | SG-HANDOVER | — |
| TEST-S09 | SG-HANDOVER | SG-ESCALATE | — |
| TEST-S10 | SG-HANDOVER | SG-ESCALATE | — |
| TEST-S11 | SG-ESCALATE | SG-DIFF | — |
| TEST-S12 | SG-DIFF | SG-SPLIT | — |
| TEST-S13 | SG-ARCHIVE | SG-HANDOVER | — |
| TEST-S14 | SG-ARCHIVE | SG-HANDOVER | — |
| TEST-S15 | SG-DIFF | SG-DRAFT | — |
| TEST-S16 | SG-PACK | SG-SKU | — |

负向题（8 问）：direct/related 均为空，全部可见组判 0（不适用理由逐篇写入标签）。

## 5. 指标影响（已保存排名离线重算，未调用 API）

| 集合 | 指标 | v2 | v3 |
|---|---|---|---|
| test phrase | Recall@10 | 0.9325 | **0.9325** |
| test phrase | MRR@5 | 0.8568 | 0.8568 |
| test phrase | nDCG@5 | 0.8074 | **0.8144** |
| dev phrase | nDCG@5 | 0.9187 | **0.9242** |
| dev vector | nDCG@5 | 0.9044 | 0.9105 |
| dev rrf | nDCG@5 | 0.9083 | 0.9144 |
| dev rrf+rerank | nDCG@5 | 0.8496 | 0.8638 |

- A3 仍通过（test phrase Recall@10 ≥0.80、nDCG@5 ≥0.75）；rerank 相对 rrf 仍无增益（nDCG −0.0506、Recall −0.0982）
- **选型不受影响**：phrase nDCG 仍最高且与 rrf 差距 0.0098 <0.03，按 E3 仍为 phrase；无需上报重选

## 6. 文件与哈希（v3）

| 文件 | sha256 |
|---|---|
| `dev_queries_v3.jsonl` | `d3e845bef2bb679f0058c33eca5988164037564eb260957cb83f26c09766f637` |
| `test_queries_v3.jsonl` | `65e499b2442847eb54943e44ffdcd8ed5db2fbf379fa0fce6096a0a873380819` |

- 离线重算报告：`evidence/T-007/T-007c-dev-four-groups-v3.json`、`T-007c-test-phrase-v3.json`
- 如后续再更正：延续本流程（变更清单、版本号、披露曝光、离线重算并说明影响），不再改 v1/v2。
