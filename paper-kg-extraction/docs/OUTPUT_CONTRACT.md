# 输出契约 · 候选三元组 JSONL（与 medkg 融合）

抽取产物是 `out/candidate_kg/paper_triples.jsonl`，**一行一条候选边**。它必须能被
medkg 的 `medkg build-graph` 直接读取融合，所以字段分两组：

1. **medkg 扁平字段**（`build-graph` 的 `fuse/graph.py` 直接读这些做节点/边聚合）——**必填、名字/类型不能改**。
2. **`provenance` 富溯源块**（论文特有：批次/章节/段落/字符区间等）——`build-graph` 用 `dict(e)` 拷贝会**原样保留**，供检索侧回链高亮。

## 1. 一条候选边（完整示例）
```json
{
  "edge_id": "sha256_16(relation + head_id + tail_id)",
  "head_id": "sha256_16('node' + normalize_name(head) + head_type)",
  "tail_id": "sha256_16('node' + normalize_name(tail) + tail_type)",
  "head": "Astragalus membranaceus", "head_type": "Herb",
  "relation": "HERB_CONTAINS_INGREDIENT",
  "tail": "Astragaloside IV", "tail_type": "MolecularEntity",
  "qualifiers": {},
  "evidence_sentence": "Astragaloside IV, the major active constituent of Astragalus membranaceus",
  "evidence_chunk_id": "pmc:PMC10123456#sec0#u00",
  "evidence_source_id": "pmc:PMC10123456",
  "chapter_path": ["Abstract"],
  "license": "cc-by", "usage_scope": "commercial", "allowed_use": "kg",
  "evidence_tier": "C", "extraction_method": "teacher_llm",
  "kg_version": "2.1.0", "status": "candidate", "confidence": 0.63,
  "domain": "bridge",
  "provenance": {
    "paper_batch_id": "batch:pmc_oa:20260703:astragalus",
    "source_id": "pmc:PMC10123456",
    "section_index": 0, "section_type": "abstract",
    "chapter_path": ["Abstract"],
    "evidence_chunk_id": "pmc:PMC10123456#sec0#u00", "unit_hash": "…",
    "para_index_range": [0, 0],
    "char_start": 68, "char_end": 243,
    "evidence_sentence": "…（== 顶层 evidence_sentence，逐字来自原文）",
    "evidence_level": "4", "evidence_tier": "C",
    "extraction_method": "teacher_llm", "extraction_run_id": "run:20260703T0930Z",
    "license": "cc-by", "usage_scope": "commercial"
  }
}
```

## 2. 字段来源与铁律
| 字段 | 谁算 | 铁律 |
|---|---|---|
| `head_id/tail_id` | `ids.node_id(name,type)` | **必须**用本包 `src/ids.py` 计算——它与 medkg 逐字节一致，`黄芪/黃芪/黄 芪` 会归一到**同一** node_id 才能与 medkg 图合并。不要自己拼哈希。 |
| `edge_id` | `ids.edge_id(rel,hid,tid)` | 同上。 |
| `head_type/tail_type` | LLM + schema 预检 | 必须是 `schema/` 里存在的实体类型，且满足该 relation 的 `domain→range`；闭类（NatureElement/LocationElement 等）取值必须在枚举内。 |
| `evidence_sentence` | 三闸校验后回填 | **逐字**是 `unit_text[char_start:char_end]`；改写/翻译/跨句拼接一律拒。 |
| `confidence` | `base(method)×tier_factor(tier)` | 由 `extract.py` 按 core.confidence_model 算，别手填。 |
| `provenance.*` | segment + 三闸 | 齐全且自洽（`char_start<char_end`、`provenance.evidence_sentence==顶层`）。 |

## 3. 融合怎么发生（交付后由老师侧执行，学生了解即可）
```bash
# 把学生产物放进 medkg 候选目录，然后融合（幂等，可反复重跑）
cp out/candidate_kg/paper_triples.jsonl  <medkg>/out/candidate_kg/paper_triples.jsonl
cd <medkg> && medkg build-graph          # fuse/graph.py 读所有 candidate_kg/*.jsonl → clean_kg
```
- 同名实体按 `node_id` 天然并节点（首见名为 canonical，其余入 aliases，合并 source_ids）。
- 同 `edge_id` 去重，多篇论文印证同一事实 → `evidence_count`↑、`confidence`↑（多源印证）。
- 与 medkg 教科书图共享 node_id，所以论文里的"黄芪"和教科书里的"黄芪"是**同一个节点**，
  论文证据边直接挂到既有辨证骨架上，实现"教科书 + 循证文献"双证据。

## 4. 与 medkg 已有字段的对齐（一句话）
`unit_id→evidence_chunk_id`、`title_path→chapter_path`、`source_id→evidence_source_id`、
`unit_text[char_start:char_end]→evidence_sentence`——论文溯源**不发明新机制**，只是把教科书的
章节路径换成论文 section 路径。详见 `docs/PROVENANCE_SPEC.md`。
