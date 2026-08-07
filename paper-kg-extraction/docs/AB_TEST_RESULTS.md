# A/B Test Results

本文件固定记录每轮同批论文的 SenseNova/DeepSeek 对比。论文证据覆盖率指“至少产生一条 accepted 三元组的论文数 / 输入论文数”；头尾实体证据覆盖率指 accepted 三元组中，head 和 tail 均可在证据原文中定位的比例。自动覆盖率不是医学正确率。

<!-- ab-test:2026-08-05 PMC 全文 200 篇 v1 -->
## 2026-08-05 PMC 全文 200 篇 v1

| 指标 | SenseNova | DeepSeek |
|---|---:|---:|
| 输入论文 | 200 | 200 |
| accepted 三元组 | 570 | 532 |
| rejects / warnings | 255 / 29 | 42 / 48 |
| 有 accepted 的论文 | 114 | 121 |
| 论文证据覆盖率 | 57.0% | 60.5% |
| 头尾实体证据覆盖率 | 99.3% | 100.0% |
| 代码版本 | `4023c62` | `4023c62` |

说明：SenseNova 运行中有 51 条 accepted 来自官方 DeepSeek fallback。两组规范化事实重合 188 条，Jaccard 0.208。

改进：修复 NLI 单层 fallback 和专用响应契约；增加原文证据相邻句扩窗、关系约束下的实体类型修复、逐论文零产出原因、`MOLECULARLY_TARGETS` 证据分级；保留 schema 不变。

<!-- ab-test:2026-08-06 PMC 全文 200 篇 v2 -->
## 2026-08-06 PMC 全文 200 篇 v2

| 指标 | SenseNova | DeepSeek |
|---|---:|---:|
| 输入论文 | 200 | 200（仅 199 完成检查点） |
| accepted / merged 三元组 | 833 | 1074 |
| rejects / warnings | 124 / 21 | 40 / 24 |
| 有 accepted 的论文 | 145 | 147 |
| 论文证据覆盖率 | 72.5% | 73.5%（待补跑后定稿） |
| 严格原文 evidence 覆盖 | 832/833 | 1074/1074 |
| 规范化事实重合 / Jaccard | 286 / 0.178 | 286 / 0.178 |
| 代码版本 | `ee5bcb1` | `ee5bcb1` |

审计结论：自动闸门不能当作正确率。已确认的误杀包括否定词跨分句污染（“no difference ... both effective”、“not T1DM but IM-diabetes”）、低发生率被 NLI 当成 contradiction，以及 `occurs/coexist/observed prevalence` 等谓词未被完整句检测器识别。已确认的误收包括 Exposure 头实体误用 `HAS_PHENOTYPE`、仅表示“使用/给药”却输出 `WM_TREATS`、预测/相关性被升级为 `MEASURES` 或 `DIAGNOSED_BY`，以及实验造模部位被当作疾病自然定位。

工程问题：DeepSeek 的 `pmc:PMC13431288` 因 LLM 错误未写 checkpoint，但流程仍写入 `PIPELINE_COMPLETED`；完成标记需增加 manifest/checkpoint/LLM-error 一致性验收。SenseNova 有 2658 次空对象、125 次限流，DeepSeek 有 1398 次空对象；当前每个 unit 都携带完整实体和 90 个关系菜单，需在不改 schema 的前提下按内容路由 schema 子集，并对整篇零候选论文做一次精简 prompt 补抽。
