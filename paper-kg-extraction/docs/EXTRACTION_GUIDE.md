# 抽取指南 · 该抽什么、怎么抽对

这份指南解释**中西医结合 schema 想要的知识**，以及抽取时的判断标准。抽取器（`src/extract.py`）
已经把流程和校验写好，你的主要工作是：**跑起来 → 抽检质量 → 按需调 prompt / 修数据 → 通过验收**。

## 1. schema 五层与优先抽取的关系
本体分 5 域（`schema/`）：`core`(证据/治理) + `tcm`(辨证论治) + `wm`(西医，PrimeKG+Biolink 扩展)
+ `bridge`(中西医桥) + `lit`(文献循证)。论文里**最有价值**的是 bridge 和 wm 的机制链。
`src/prompts.py` 的 `PRIORITY_RELATIONS` 已按价值排好，重点：

| 想抽的知识 | 关系 | 例（英文论文常见句式） |
|---|---|---|
| 中药含什么成分 | `HERB_CONTAINS_INGREDIENT` | "Astragaloside IV, the major constituent of *Astragalus*…" |
| 成分作用于什么靶点 | `MOLECULARLY_TARGETS` | "…inhibiting the TGF-β1/Smad3 pathway" |
| 靶点/基因与病的关联 | `GENE_ASSOCIATED_WITH_DISEASE` | "TGF-β1 is upregulated in diabetic nephropathy" |
| 整方经什么机制起效 | `FORMULA_ACTS_VIA` | "Buyang Huanwu decoction acts via the PI3K/Akt pathway" |
| 中医病证 ↔ 西医病 | `CORRESPONDS_TO_WM_DISEASE` | "消渴 corresponds to diabetes mellitus" |
| 证候的西医客观指标 | `SYNDROME_CORRELATES_WITH` | "血瘀证 correlates with elevated D-dimer" |
| 中药-西药互作 | `INTERACTS_WITH` / `HDI_VIA_SHARED_TARGET` | "danshen potentiates warfarin" |
| 中药增效/减毒西医治疗 | `COMPLEMENTS_WM_THERAPY` | "…reduces chemotherapy-induced toxicity" |
| 西药治病/不良反应 | `WM_TREATS` / `WM_HAS_ADVERSE_EFFECT` | 标准西医事实 |
| 这篇论文研究了什么 | `STUDIES`（PICO） | 干预×疾病，供证据检索 |

完整关系列表见各 `schema/*.yaml` 的 `relation_types`（含 `domain/range/desc`）。

## 2. 三条质量红线（抽取器会强制，但你要理解）
1. **逐字接地**：`evidence_sentence` 必须是单元原文的连续子串，且是能独立支撑该三元组的**最短句**。
   不允许改写/翻译/跨句拼接。→ 违者被"子串闸/区间闸"拒。
2. **本文事实 vs 背景引述**：`introduction/background` 段落大多在复述他人研究或常识，**不要**当作本文证据抽。
   优先抽 `results/conclusion/abstract` 的结论性陈述。（抽取器对 section_type 有提示，但判断仍靠 prompt+你抽检。）
3. **类型合规**：`head_type/tail_type` 必须落在 schema，`relation` 的 `domain→range` 必须满足；
   闭类实体（证素等）取值必须在枚举里。→ 违者被"schema 预检"拒。

## 3. 命名约定（决定能否与 medkg / 跨论文合并）
- **中文实体**（中药/方剂/证候/中医病）：用规范中文名（简体优先；繁体会自动归一）。如 `黄芪`、`银翘散`、`风热犯表`。
- **英文实体**（西药/基因/成分/西医病/通路）：用论文里的**规范英文名或标准缩写原形**，大小写保持惯例。
  如 `Astragaloside IV`、`TGF-β1`、`diabetic nephropathy`。**同一实体全批次统一写法**，否则会分裂成多个节点。
- 尽量向权威名靠拢：基因用 HGNC symbol（`TGFB1`/`NFE2L2`），西药用通用名，西医病可留英文病名（后续 `ingest` 挂 ICD/ATC 码）。
  > 提示：能附标准码更好，但**不是**本轮硬要求；本轮硬要求是"规范名一致 + 逐字溯源 + 类型合规"。

## 4. 调优抽取质量
- 先小批（`--limit 20`）跑，看 `out/candidate_kg/extraction_report.json` 的 `reject_stats`：
  - `prov:evidence_not_in_source` 高 → 模型在编证据，收紧 prompt「逐字子串」措辞，或换更强模型。
  - `schema:...` 高 → 模型给了错类型，检查 prompt 的实体类型表是否被模型忽略。
  - `accepted` 太低 → 可能论文本身中医内容少（纯西医论文正常），或 section 过滤太严。
- 抽检：随机取 10 条边，人工核对 `evidence_sentence` 是否真的支持该三元组、类型对不对。记录准确率。
- 换模型/改 prompt 后，用**同一批论文**重抽，对比 `accepted` 数与抽检准确率。

## 5. 交付前自检
```bash
python scripts/03_validate.py out/candidate_kg/paper_triples.jsonl   # 必须 100% 通过
```
详见 `README.md` 的「验收标准」。
