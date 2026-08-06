# 论文 → 中西医结合知识图谱 · 抽取任务包

> **一句话任务**：把已下载的 PubMed / PMC-OA 医学论文，按我们设计的**中西医结合知识图谱 schema**
> 抽成三元组，每条三元组能**逐字溯源**到"哪批论文-哪篇-哪章-哪段-哪句-哪些字"，且产物能与
> 现有 `medkg` 知识图谱**直接融合**（同一实体自动并成同一节点）。

这个目录是自包含的：schema、抽取代码、校验器、示例、测试都在里面。你只需按下面步骤跑通、
抽检质量、通过验收，然后把 `out/candidate_kg/paper_triples.jsonl` 交回来。

---

## 0. 你要交付什么
1. `out/candidate_kg/paper_triples.jsonl` —— 候选三元组（格式见 `docs/OUTPUT_CONTRACT.md`）。
2. `out/candidate_kg/extraction_report.json` —— 抽取统计（自动生成）。
3. 一份**抽检记录**（随机 ≥30 条边人工核对准确率，Excel/Markdown 均可）。
4. 简短说明：用了哪个模型、下载了哪些主题/批次、准确率、遇到的问题。

**验收硬标准**：`python scripts/03_validate.py` 输出 **100% 通过**（`failed == 0`）。见 [§6 验收标准](#6-验收标准)。

---

## 1. 环境准备（约 5 分钟）
```bash
cd paper-kg-extraction
python -m venv .venv && .venv\Scripts\activate     # Windows；Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # 填 SenseNova 与 DeepSeek 官方 API 配置
```
> 要求 Python ≥ 3.10。默认 SenseNova 优先，DeepSeek 官方 API fallback；NLI 使用相同顺序。

## 2. 先跑通（无需 API key，1 分钟）
```bash
python tests/test_smoke.py                                             # 冒烟：全绿
python scripts/02_run_extraction.py --from json --input examples/sample_paper.json --mock
python scripts/03_validate.py                                          # 100% 通过
```
`--mock` 是离线确定性抽取（词典规则，仅演示流程，**不是**真实抽取质量）。跑通后进入真实抽取。

## 3. 完整流程
```
① 下载论文        →  ② 抽取           →  ③ 验收自检        →  ④ 抽检并交付
scripts/01_...md     scripts/02_...py     scripts/03_...py     out/*.jsonl + 抽检记录
```

### ① 下载论文
见 [`scripts/01_DOWNLOAD_PAPERS.md`](scripts/01_DOWNLOAD_PAPERS.md)。用采集器 `med-paper-collector`
按主题下载一批（务必含 `pmc_oa` 才有全文），跑 `grade` 打证据等级，记下 `metadata.db` 路径。
> 也可跳过采集器：把论文整理成 `examples/sample_paper.json` 的字段，用 `--from json` 直接抽。

### ② 抽取
```bash
# 从采集库抽（推荐）：给这批起 batch-id，便于"哪批检索意图"溯源
python scripts/02_run_extraction.py --from db \
  --db ~/med-papers/metadata.db --limit 200 \
  --batch-id batch:pmc_oa:20260703:astragalus
```
产物：`out/candidate_kg/paper_triples.jsonl` + `extraction_report.json`。每次正常结束后还会
自动生成 `out/candidate_kg/audit/quality_audit.tsv`、`quality_audit.jsonl` 和
`quality_summary.json`；这些风险标记用于筛选人工复核对象，不等同于医学正确率。仅在明确需要时
使用 `--no-audit` 跳过。

如果先执行了跨论文合并，可对合并结果重新生成最终审计：

```bash
python src/quality_audit.py \
  --accepted out/runs/RUN/paper_triples_merged.jsonl \
  --rejects out/runs/RUN/paper_triples_rejects.jsonl \
  --warnings out/runs/RUN/paper_triples_warnings.jsonl \
  --out-dir out/runs/RUN/audit_merged
```

每轮 SenseNova/DeepSeek 同批 A/B 完成后，将对比追加到
`docs/AB_TEST_RESULTS.md`：

```bash
python tools/update_ab_test_report.py \
  --sensenova-run out/runs/SENSENOVA_RUN \
  --deepseek-run out/runs/DEEPSEEK_RUN \
  --label "2026-08-06 PMC 全文 200 篇 v2" \
  --improvements "填写本轮发现及下一步改进"
```

抽取报告中的 `paper_evidence_coverage` 同时记录论文覆盖率和零产出原因，
区分模型无候选、候选全部被拒绝和 LLM 最终失败。

DeepSeek fallback 使用官方接口：

```dotenv
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_REASONING_EFFORT=high
DEEPSEEK_THINKING=enabled
DEEPSEEK_MAX_COMPLETION_TOKENS=4096
NLI_MAX_COMPLETION_TOKENS=512
```
先 `--limit 20` 小批试跑，看 report 的 `reject_stats` 调 prompt / 换模型（见 `docs/EXTRACTION_GUIDE.md` §4）。

### ③ 验收自检
```bash
python scripts/03_validate.py out/candidate_kg/paper_triples.jsonl     # 必须 100%
```

### ④ 抽检并交付
随机 ≥30 条，人工核对 `evidence_sentence` 是否真支持三元组、类型对不对，记准确率。达标即交付。

---

## 4. 目录结构
```
paper-kg-extraction/
├─ README.md                  ← 你在这
├─ requirements.txt  .env.example
├─ schema/                    ← 完整中西医结合本体（5 域，自包含可离线校验）
│   ├─ core.yaml  tcm.yaml    ← medkg 生产原文件（证据治理 + 辨证论治）
│   ├─ wm.yaml                ← 西医域（PrimeKG + Biolink 扩展）
│   ├─ bridge.yaml            ← 中西医桥（病证映射/网络药理/互作/结合治疗）
│   ├─ lit.yaml               ← 文献循证域（论文/试验/证据分级）
│   └─ registry.yaml          ← 域注册（active: core,tcm,wm,bridge,lit）
├─ docs/
│   ├─ PROVENANCE_SPEC.md     ← 细粒度溯源规范（哪批→哪篇→哪章→哪段→哪句→哪些字）
│   ├─ OUTPUT_CONTRACT.md     ← 候选边 JSONL 精确字段契约 + 与 medkg 融合说明
│   └─ EXTRACTION_GUIDE.md    ← 该抽什么、怎么抽对、命名约定、调优
├─ src/
│   ├─ ids.py                 ← node_id/edge_id/normalize_name（**与 medkg 逐字节一致**，勿改）
│   ├─ schema_loader.py       ← 轻量本体加载 + 校验（domain/range/闭类）
│   ├─ segment.py             ← 论文→章节→语义单元（chapter_path/unit_id）
│   ├─ provenance.py          ← 三闸溯源校验 + char-span 定位
│   ├─ llm_client.py          ← OpenAI 兼容客户端（+ --mock 离线）
│   ├─ prompts.py             ← 抽取 prompt（关系菜单由 schema 动态生成）
│   ├─ extract.py             ← 主抽取：单元→LLM→三闸→候选边
│   └─ validate_output.py     ← 输出预检（验收闸）
├─ scripts/
│   ├─ 01_DOWNLOAD_PAPERS.md  ← 下载说明
│   ├─ 02_run_extraction.py   ← 端到端抽取
│   └─ 03_validate.py         ← 验收
├─ examples/
│   ├─ sample_paper.json      ← 一篇示例论文（PMC-OA 结构）
│   └─ expected_triples.jsonl ← 对应的 gold 候选三元组（含完整溯源）
└─ tests/test_smoke.py        ← 离线冒烟（无需 API key）
```

## 5. schema 速览（详见 `docs/EXTRACTION_GUIDE.md`）
5 域：`core`(证据/治理) + `tcm`(辨证论治) + `wm`(西医·PrimeKG+Biolink) + `bridge`(中西医桥) + `lit`(文献循证)。
论文里最该抽的机制链：
```
中药 ─HERB_CONTAINS_INGREDIENT→ 成分 ─MOLECULARLY_TARGETS→ 基因 ─GENE_ASSOCIATED_WITH_DISEASE→ 西医病
中医病/证 ─CORRESPONDS_TO_WM_DISEASE→ 西医病        证候 ─SYNDROME_CORRELATES_WITH→ 生标/表型/靶基因
中药/方 ─COMPLEMENTS_WM_THERAPY→ 西药/操作          中药 ─INTERACTS_WITH→ 西药（用药安全）
```
全部 3–4 跳可溯源。关系菜单见 `src/prompts.py: PRIORITY_RELATIONS`，完整定义见 `schema/*.yaml`。

## 6. 验收标准
一条候选边**必须**同时满足（`scripts/03_validate.py` 全部自动检查）：
- ✅ **字段齐全**：medkg 扁平字段 + `provenance` 块（见 `docs/OUTPUT_CONTRACT.md`）。
- ✅ **ID 自洽**：`head_id==node_id(head,head_type)`，`edge_id==edge_id(rel,hid,tid)`（用本包 `src/ids.py` 算）。
- ✅ **schema 合规**：`relation` 存在，`head_type∈domain`、`tail_type∈range`，闭类取值在枚举内。
- ✅ **逐字溯源**：`evidence_sentence == unit_text[char_start:char_end]`，`0≤char_start<char_end`，非空；
  `provenance.evidence_sentence` 与顶层一致。
- ✅ **元数据合法**：`0<confidence≤1`，`evidence_tier∈{A,B,C,D}`，`license` 非空。

> 权威融合校验在老师侧的 `medkg build-graph`（含 Validator + license_gate + 7 道质量闸）；本包 `03_validate.py`
> 是提前拦低级错误的**预检**，通过预检不代表零人工抽检——质量仍以抽检准确率为准。

## 7. 常见问题
- **Q：Windows 控制台中文/✓ 报 GBK 编码错？** 脚本已强制 UTF-8；若仍报错，`set PYTHONIOENCODING=utf-8`。
- **Q：`node_id` 能自己算吗？** 不能。必须用 `src/ids.py`——它与 medkg 逐字节一致，否则同一实体分裂、无法融合。
- **Q：纯西医论文抽出的中医边很少，正常吗？** 正常。价值在 bridge/wm 机制链；纯西医论文贡献 wm 子图也有用。
- **Q：证据句必须英文？** 论文什么语言就照抄什么（逐字子串），中英混排也照抄；实体名按 §命名约定归一。
- **Q：能改 schema 吗？** 不要。schema 是团队统一契约；发现缺关系/类型，记录下来反馈，不要私自改 `schema/`。
```
