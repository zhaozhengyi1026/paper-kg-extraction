# paper-kg-extraction

> 从生物医学论文中抽取中西医结合知识图谱三元组，v5.1。

## 架构

```
论文 (PubMed/PMC OA)
  → 语义单元切分 (1200 char/unit)
  → LLM 抽取三元组 (SenseNova 优先，deepseek-v4-pro fallback)
  → 10 道校验闸门
  → NLI 语义验证 (sensenova flash-lite, fallback deepseek)
  → ICS 交互可信度评分
  → 输出候选知识图谱
```

## 快速开始

```bash
# 批量采集：查询表 → 摘要 → PMCID → 可用 PMC 全文
bash run_pipeline.sh download-batch queries.tsv

# 1. 配置
cp .env.example .env
# 编辑 .env，填入 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL

# 2. 安装依赖
pip install -r requirements.txt

# 3. 冒烟测试（无需 API key）
python scripts/02_run_extraction.py --from json --input examples/sample_paper.json --mock

# 4. 真跑
python scripts/02_run_extraction.py --from json --input data/papers_json --limit 100
```

## 一键 Pipeline

```bash
# 全流程：下载 → 评分 → 抽取 → 合并
bash run_pipeline.sh all "astragalus membranaceus" 10 --tag my-run

# 仅抽取已有 JSON
bash run_pipeline.sh extract-all --tag my-run

# 查看结果
cat out/runs/my-run/paper_triples_merged.jsonl
```

## 全库知识图谱抽取

`queries.tsv` 只用于专题采集；全 PubMed 不应通过关键词枚举。先用 PubMed
baseline 和 daily updates 建立完整 SQLite 库，再由抽取器直接流式读取。抽取时有
PMC 全文就使用全文，否则使用 PubMed 摘要。`--limit 0` 表示不设论文数量上限。

单进程断点续跑：

```bash
cd /home/featurize/work/zhaozhengyi/paper-kg-extraction
nohup python scripts/02_run_extraction.py \
  --from db --db /home/featurize/work/zhaozhengyi/med-papers/pubmed_full.db \
  --limit 0 --workers 3 \
  --out out/runs/pubmed_all/shard-000.jsonl \
  --batch-id batch:pubmed:baseline:all \
  > out/runs/pubmed_all/shard-000.log 2>&1 < /dev/null &
```

推荐按 PMID 分片。下面示例启动 8 个互不重叠的后台任务；每个任务内部默认
SenseNova 优先、DeepSeek fallback。API 限流较严时减少 `--workers` 或分片数。

### 同一批 200 篇模型对照

DeepSeek 只使用 `LLM_API_KEY` 配置的单把 NVIDIA key。两次运行使用同一数据库排序和 `--limit 200`，但写入不同目录；NLI 均保持 SenseNova 优先、DeepSeek fallback。

```bash
cd /home/featurize/work/zhaozhengyi/paper-kg-extraction

EXTRACTION_PROVIDER=sensenova python scripts/02_run_extraction.py \
  --from db --db /home/featurize/work/zhaozhengyi/med-papers/pubmed_full.db \
  --limit 200 --workers 6 \
  --out out/runs/ab_200_sensenova/paper_triples.jsonl

EXTRACTION_PROVIDER=deepseek python scripts/02_run_extraction.py \
  --from db --db /home/featurize/work/zhaozhengyi/med-papers/pubmed_full.db \
  --limit 200 --workers 3 \
  --out out/runs/ab_200_deepseek/paper_triples.jsonl
```

SenseNova 的默认请求间隔为 0.2 秒，可用 `SENSENOVA_MIN_INTERVAL` 调整；频繁出现 429 时应降低 `--workers` 或增大间隔。两个命令应分别运行，避免模型间争用 SenseNova NLI 配额。

```bash
mkdir -p out/runs/pubmed_all
for i in $(seq 0 7); do
  nohup python scripts/02_run_extraction.py \
    --from db --db /home/featurize/work/zhaozhengyi/med-papers/pubmed_full.db \
    --limit 0 --shard-count 8 --shard-index "$i" --workers 2 \
    --out "out/runs/pubmed_all/shard-${i}.jsonl" \
    --batch-id batch:pubmed:baseline:all \
    > "out/runs/pubmed_all/shard-${i}.log" 2>&1 < /dev/null &
done
```

每个输出旁的 `*_processed.jsonl` 是论文级成功检查点；使用相同命令和输出路径会
自动跳过已完成论文，LLM 错误则重试。所有分片结束后再合并，不能边写边合并：

```bash
cat out/runs/pubmed_all/shard-*.jsonl > out/runs/pubmed_all/paper_triples.jsonl
python scripts/04_merge_interactions.py \
  out/runs/pubmed_all/paper_triples.jsonl \
  --out out/runs/pubmed_all/paper_triples_merged.jsonl
python scripts/03_validate.py out/runs/pubmed_all/paper_triples_merged.jsonl
```

抽取器和合并器均采用流式/磁盘暂存方式，不会一次把全部论文或三元组装入内存。
全库任务预计是长期、高成本作业；先用 `--limit 100` 验证吞吐、费用和限流。

## 校验闸门 (v5.1)

```
每条 LLM 返回的三元组:

  ── 类型覆盖 ──
  成分名强制覆盖 (polysaccharide → MolecularEntity)
  + 缩写展开 (APS → Astragalus polysaccharide)
  + 同论文类型一致性

  Gate 0: 完整性
  Gate 1: Schema domain/range
  Gate 2: Provenance 三闸 (evidence 存在于原文)
  Gate 2.5: Char span 精确对齐
  Gate 3: 实体-证据词边界匹配
  Gate 3.5: NLI 语义校验 ← NEW
    entailment   → 通过
    contradiction → 拒绝
    neutral      → 降级 (confidence × 0.6)
  Gate 3.6: HDI 重分类 (无 PK → COMPLEMENTS) ← NEW
  Gate 4: 否定检测 → warning
  Gate 4.5: 结构关系置信度加成 ← NEW
  ── 组装 → node_id/edge_id → 去重
```

## 双模型

| 模型 | 用途 | Provider |
|------|------|----------|
| `sensenova-6.7-flash-lite` | 三元组抽取（主） | SenseNova |
| `deepseek-v4-pro` | 三元组抽取（fallback） | NVIDIA NIM |
| `sensenova-6.7-flash-lite` | NLI 语义校验 | SenseNova |
| `deepseek-v4-pro` (fallback) | NLI 兜底 | NVIDIA NIM |

## 关系类型

| 优先级 | 关系 | 说明 |
|--------|------|------|
| P0 | DDI, HDI, HHI, INTERACTS_WITH | 药物相互作用（用药安全） |
| P0 | TREATS, WM_TREATS | 疗效声明 |
| P0 | HERB_CONTAINS_INGREDIENT | 草药-成分 |
| P1 | MOLECULARLY_TARGETS, GENE_ASSOCIATED_WITH_DISEASE | 机制链 |
| P1 | COMPLEMENTS_WM_THERAPY | 中西医联用 |
| P2 | HAS_PHENOTYPE, PARTICIPATES_IN_PATHWAY 等 | 辅助关系 |

## 输出格式

```json
{
  "edge_id": "a1b2c3d4e5f6a7b8",
  "head": "Astragalus membranaceus", "head_type": "Herb",
  "relation": "TREATS",
  "tail": "diabetic nephropathy", "tail_type": "DiseaseWM",
  "evidence_sentence": "Astragalus significantly improved renal function...",
  "confidence": 0.88,
  "evidence_tier": "A",
  "nli_label": "entailment",
  "ics_score": 72, "ics_tier": "B"
}
```

## 目录

```
paper-kg-extraction/
├── src/                    # 核心引擎
│   ├── extract.py           _make_edge 10 道闸门
│   ├── nli.py               NLI 语义校验模块
│   ├── llm_client.py        双模型客户端 + NLI 工厂
│   ├── prompts.py           v3 prompt（9 铁律 + 5 few-shot）
│   ├── provenance.py        溯源三闸 + 括号补全
│   ├── segment.py           语义单元切分
│   ├── schema_loader.py     本体加载 + 校验
│   ├── ids.py               node_id / edge_id
│   ├── interaction_credibility.py  ICS v2.0 评分
│   └── validate_output.py   验收预检
├── scripts/                入口脚本
│   ├── 02_run_extraction.py  主抽取
│   ├── 03_validate.py        验收
│   └── 04_merge_interactions.py  跨论文合并
├── schema/                 本体定义 (core/tcm/wm/bridge/lit)
├── tools/                  辅助工具
│   └── evidence_grader.py  论文权威度评分
├── run_pipeline.sh         一键 Pipeline
└── examples/               示例数据
```

## v5.1 改进

- **NLI 语义校验**: evidence 蕴含判断，弱证据降级 / 矛盾拒绝
- **类型强制覆盖**: 多糖/皂苷等不会被标为 Herb
- **HDI 重分类**: 无 PK 证据自动归为 COMPLEMENTS_WM_THERAPY
- **结构关系置信度**: CONTAINS +0.25 加成
- **括号补全**: 截断证据 `(n=5` → `(n=5)`
- **skip_nli 前置规则**: 含明确断言词直接跳过 NLI API
- **否定误杀修复**: "not only...but also" 不触发 negation_warning
- **耗时统计**: extraction_report 含 `"elapsed": "3h25m12s"`

## v5.2 质量控制改进

- NLI 按关系类型判断证据：关联不再误判为因果，配方作用和分子靶点使用独立规则。
- 在进入 NLI 前拒绝研究目的、假设、并列候选和不完整补语等弱证据。
- 预测或间接的分子靶点保留但降低置信度，并写入明确 warning，便于后续筛选。
- 否定检测改为关系感知的局部作用域规则；生化语境中的 `NO`、`not only...but also` 等不再误报。
- 缩写从论文全文解析，同时保留证据原文中的 Unicode 空格，确保 provenance span 可回溯。
- 本次仅收紧抽取与校验逻辑，未修改任何 schema 或输出字段。

## License

MIT
