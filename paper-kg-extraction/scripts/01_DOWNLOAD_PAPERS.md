# 步骤 1 · 下载论文（med-paper-collector）

抽取的输入是**已下载的 PubMed 元数据 + PMC-OA 全文**。采集器在
`paper-search-proxy_new_evidence/med-paper-collector/`，产出一个 SQLite 库（默认
`~/med-papers/metadata.db`），其中 `pmc_fulltext` 表已把 JATS 全文解析成
`sections = [{"title","text"}]`（正是本包 `--from db` 直接读的结构）。

## 1. 安装采集器
```bash
cd paper-search-proxy_new_evidence/med-paper-collector
python3.11 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env      # 填 NCBI_API_KEY（去 https://www.ncbi.nlm.nih.gov/account/ 免费申请，限速更高）
```

## 2. 按主题下载一批（中西医结合相关）
`--query` 用 PubMed 检索式；只有 PMC-OA 源才有全文，务必带 `pmc_oa`。
```bash
# 例：黄芪 / 中药网络药理 / 中西医结合 相关、近 5 年、限 500 篇
python cli.py full-sync \
  --source pubmed,pmc_oa \
  --query '("Astragalus"[MeSH] OR "Drugs, Chinese Herbal"[MeSH] OR "network pharmacology") AND ("2020"[PDAT]:"2026"[PDAT])' \
  --max-results 500 \
  --year-start 2020

# 补充：把有 DOI 但没入 OA 全文的，尝试发现 OA 链接后再下
python cli.py enrich-oa --max-total 500
python cli.py download-queue --limit 500
```

> 建议一次一个**明确主题**下载一批，并给这批起一个 `paper_batch_id`（见步骤 2 的 `--batch-id`），
> 这样每条三元组都能溯源到"哪批检索意图"。批次命名：`batch:pmc_oa:<yyyymmdd>:<主题缩写>`。

## 3. 给论文打证据等级（供 evidence_tier / 置信）
```bash
python cli.py grade          # 产出 evidence_level(Oxford CEBM 1a–5)/journal_tier，写回 papers 表
python cli.py status         # 看总量、全文数、分级分布
```

## 4. 记下库路径
默认 `~/med-papers/metadata.db`（`echo ~` 看家目录）。抽取时用
`--db /绝对/路径/metadata.db`，或写进本包 `.env` 的 `PAPERS_DB`。

---
### 只想先跑通、暂时不下载？
用自带示例即可（无需 API key，无需采集器）：
```bash
cd paper-kg-extraction
python scripts/02_run_extraction.py --from json --input examples/sample_paper.json --mock
```
把你自己的论文整理成 `examples/sample_paper.json` 的字段（`pmcid/title/abstract/sections[]/license/evidence_level`）
也能用 `--from json` 直接抽，不依赖采集器库。
