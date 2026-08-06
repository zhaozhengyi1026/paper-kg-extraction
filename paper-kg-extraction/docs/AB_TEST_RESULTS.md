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
