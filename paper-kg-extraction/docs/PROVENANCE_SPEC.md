# 论文三元组 · 细粒度溯源规范（PROVENANCE_SPEC v1.0）

> 保证：知识图谱里**每一条**从论文抽取的三元组，都能被机器与人**逐层回链并字节校验**到
> 原始论文的具体段落与句子。设计与 medkg 教科书溯源模型同构，直接复用其反幻觉子串闸。

---

## 1. 溯源标识层级（哪批 → 哪篇 → 哪章 → 哪段 → 哪句 → 哪些字）

```
paper_batch_id            批：一次采集运行         batch:<source>:<yyyymmdd>:<query_hash8>
  └─ source_id            篇：论文规范 ID          pmc:PMC10123456 | pmid:38765432 | doi:10.x/y | nct:NCT01234567
       └─ section         章：JATS 章节            {section_index, section_type, title_path[]}
            └─ unit        段：语义单元(1~n 段)     unit_id = <source_id>#sec<si>#u<ui>
                 └─ span   句/字：单元内字符区间     [char_start, char_end)  → evidence_sentence
```

### 1.1 ID 构造规则（确定性、可复现）
- `source_id`：按优先级 `pmc: > pmid: > doi: > nct:`，全小写前缀 + 原生 ID。一篇论文一个规范 `source_id`
  （多 ID 互为 alias，存 JournalArticle 节点 key_props）。
- `section_index`：论文 body 内 `<sec>` 的先序序号（0 起）；`section_type` 归一到枚举（§2）；
  `title_path`：从顶层 sec 到当前 sec 的标题链（= 边的 `chapter_path`）。
- `unit_id = f"{source_id}#sec{section_index}#u{unit_ordinal}"`，`unit_ordinal` 为该 section 内单元序号。
  另存 `unit_hash = sha256(source_id + section_index + unit_text)[:12]` 抗 section 重排。
- `char_start/char_end`：**在该 unit 的归一文本（`clean_text` 后）内**的半开区间；跨单元的证据不允许（必须落在单一 unit）。
- `paper_batch_id`：采集批次；`query_hash8` = 检索式的 sha256 前 8 位，便于"哪批=哪个检索意图"。

> 与 medkg 现有字段对齐：`unit_id → edge.evidence_chunk_id`、`title_path → edge.chapter_path`、
> `source_id → edge.evidence_source_id`、`unit_text[char_start:char_end] → edge.evidence_sentence`。
> 即论文溯源**不新增机制**，只是把教科书的 `chapter_path` 换成论文 section 路径。

---

## 2. section_type 归一枚举（供分级与检索）

JATS `<sec sec-type>` 或标题正则归一到：
```
abstract, introduction, background, methods, results, discussion,
conclusion, case_report, limitations, funding, other
```
分级用途：`results/conclusion/abstract` 的结论性陈述可支持"结论边"；`introduction/background` 多为**引用他文**
（须降权或转记为 `lit.CITES`，避免把背景引述当本文证据）；`methods` 支持 `STUDIES`(PICO) 边。

---

## 3. 溯源记录（per-edge）JSON 契约

每条候选边在 `out/candidate_kg/paper_triples.jsonl` 里带如下溯源块（核心字段，其余同 core.edge_properties）：

```json
{
  "edge_id": "sha256(relation+head_id+tail_id)",
  "head": "Astragalus membranaceus", "head_type": "Herb",
  "relation": "HERB_CONTAINS_INGREDIENT",
  "tail": "Astragaloside IV", "tail_type": "MolecularEntity",
  "provenance": {
    "paper_batch_id": "batch:pmc_oa:20260703:q8f3a1c2",
    "source_id": "pmc:PMC10123456",
    "section_index": 3,
    "section_type": "results",
    "chapter_path": ["Results", "3.2 Chemical constituents"],
    "evidence_chunk_id": "pmc:PMC10123456#sec3#u07",
    "unit_hash": "9a1b7c2d4e6f",
    "para_index": 2,
    "sentence_index": 1,
    "char_start": 142,
    "char_end": 219,
    "evidence_sentence": "Astragaloside IV was identified as the major active constituent of Astragalus.",
    "evidence_level": "4",
    "evidence_tier": "C",
    "extraction_method": "teacher_llm",
    "extraction_run_id": "run:20260703T0930Z",
    "license": "cc-by",
    "usage_scope": "commercial"
  }
}
```

节点也可带 `SUPPORTED_BY_LITERATURE` 边指向 `JournalArticle` 节点，实现实体级 provenance 反查。

---

## 4. 校验规则（三闸，抽取与 build-graph 阶段各跑一次）

| 闸 | 规则 | 失败处理 |
|---|---|---|
| **子串闸**（复用 medkg）| `evidence_sentence` 必须是 `unit_text` 归一后的连续子串 | 拒边，记 `evidence_not_in_source` |
| **区间闸**（新增）| `unit_text[char_start:char_end]` 经同一 `clean_text` 归一后 == `evidence_sentence`；且 `0 ≤ char_start < char_end ≤ len(unit_text)` | 拒边，记 `char_span_mismatch` |
| **溯源存在闸**（新增）| `source_id` 在 `metadata.db`/`pmc_fulltext` 命中；该文 `license` 过 `license_gate` | 拒边，记 `source_not_found` / `license_blocked` |

> 与 medkg 已知不变量一致：`shortest_grounded_window` 已做繁简/去括号归一，`char_span` 须在**同一归一函数**
> 输出上计算，避免"原文能对上、区间对不上"。宽跨度（>80 字）仍按精度守卫拒，逼 LLM 给最短证据窗。

回链（serving 侧）：`edge → source_id → pmc_fulltext.sections[section_index] → unit(evidence_chunk_id) → [char_start:char_end]`
可直接高亮原句；`unit_hash` 兜底应对源文重排。

---

## 5. 批次与可复现（哪批论文）

- `paper_batch_id` 与 med-paper-collector 的一次 `sync` 运行绑定，落库到一张 `extraction_batches` 表：
  `{batch_id, source, query, query_hash, n_papers, started_at, finished_at, corpus_snapshot_sha}`。
- `extraction_run_id` 绑定一次 medkg 抽取运行（模型版本、prompt 版本、schema_version），支持"同批语料换模型重抽后对比/回滚"。
- 二者组合回答："这条边来自 2026-07-03 那批检索『黄芪 网络药理』的 PMC-OA 论文、PMC10123456 的 Results 3.2 第 2 段第 1 句"。

---

## 6. 与 evidence_grader 的衔接

`med-paper-collector/evidence_grader.py` 已对每篇论文产出 `evidence_level`(Oxford CEBM 1a–5) +
`journal_tier` + `pub_type_rank`。抽取时把论文级 `evidence_level` 落到该文所有边的 `provenance.evidence_level`，
再经 `lit.yaml.literature_tier_map` 映射到 `evidence_tier`(A–D)，driving core 的置信模型：
`confidence = base(extraction_method) × tier_factor(evidence_tier) × (1 + evidence_bonus·多源印证)`。
即**证据越强、印证越多、置信越高**，且分级全程可溯。
```
