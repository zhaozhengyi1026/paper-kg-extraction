"""下载一篇 PMC OA XML 全文（带 PMCID 真实性验证）"""
import sqlite3, os, sys, json, urllib.request, time

PMCID = sys.argv[1] if len(sys.argv) > 1 else "PMC8257355"
BASE = os.path.expanduser("~/work/zhaozhengyi")
DB = os.path.join(BASE, "med-papers/metadata.db")
pmc_num = PMCID.replace("PMC", "")

# ========== Step 0-1: 通过 E-utilities 下载并验证全文是否存在 ==========
print("下载并验证 PMCID %s ..." % PMCID)
efetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id=%s&retmode=xml" % pmc_num
req = urllib.request.Request(efetch_url)
req.add_header("User-Agent", "med-paper-downloader/1.0 (1292589632@qq.com)")
try:
    resp = urllib.request.urlopen(req, timeout=30)
    xml_text = resp.read().decode("utf-8")
except Exception as e:
    print("下载失败: %s" % e)
    sys.exit(1)

# Verify: check XML has actual article body (not just a citation stub)
from lxml import etree
parser_xml = etree.XMLParser(recover=True)
root_check = etree.fromstring(xml_text.encode("utf-8"), parser_xml)
body = root_check.find(".//body")
if body is None:
    print("ERROR: %s 没有 <body> 元素，不是真正的 OA 全文" % PMCID)
    sys.exit(1)
body_text = etree.tostring(body, method="text", encoding="unicode")
if len(body_text.strip()) < 500:
    print("ERROR: %s body 内容 < 500 字符，是引用占位而非全文" % PMCID)
    sys.exit(1)

# Extract real PMID from XML
real_pmid = None
for aid in root_check.findall(".//article-id"):
    if aid.get("pub-id-type") == "pmid":
        real_pmid = aid.text
        break

print("PMCID 验证通过: %s, XML=%d 字, body=%d 字, 真实 PMID=%s" % (
    PMCID, len(xml_text), len(body_text.strip()), real_pmid or "N/A"))

# ========== Step 2: 解析 ==========
sys.path.insert(0, os.path.join(BASE, "med-paper-downloader"))
from parsers.jats_xml import parse_jats_xml

paper = parse_jats_xml(xml_text)
if not paper.get("pmcid"):
    paper["pmcid"] = PMCID

# ========== Step 3: 纠正 DB 中的 PMID ==========
if real_pmid and real_pmid != paper.get("pmid"):
    print("PMID 修正: DB 原 %s → 真实 %s" % (paper.get("pmid"), real_pmid))
    paper["pmid"] = real_pmid

print("\n解析结果:")
print("  PMCID:", paper.get("pmcid"))
print("  真实 PMID:", paper.get("pmid"))
print("  Title:", (paper.get("title") or "")[:100])
print("  Sections:", len(paper.get("sections", [])))
for s in paper.get("sections", [])[:3]:
    print("    [%s] %s..." % (s.get("title", "?"), (s.get("text", "") or "")[:60]))

# ========== Step 4: 写入 DB（修正 PMID） ==========
con = sqlite3.connect(DB)
con.execute("""CREATE TABLE IF NOT EXISTS pmc_fulltext (
    pmcid TEXT PRIMARY KEY, pmid TEXT, doi TEXT, title TEXT, authors TEXT,
    journal TEXT, pub_date TEXT, abstract TEXT, full_text TEXT,
    sections TEXT, "references" TEXT, license TEXT, source TEXT, created_at TEXT
)""")

# Upsert into papers with verified data
con.execute("""INSERT OR REPLACE INTO papers
    (pmid, pmcid, doi, title, abstract, license, source)
    VALUES (?,?,?,?,?,?,?)""",
    (paper.get("pmid"), paper["pmcid"], paper.get("doi"),
     paper.get("title"), paper.get("abstract"),
     paper.get("license", "cc-by"), "pmc_oa"))

# Write full text
con.execute("""INSERT OR REPLACE INTO pmc_fulltext
    (pmcid, pmid, doi, title, abstract, sections, license, source, created_at)
    VALUES (?,?,?,?,?,?,?,?,datetime('now'))""",
    (paper["pmcid"], paper.get("pmid"), paper.get("doi"),
     paper.get("title"), paper.get("abstract"),
     json.dumps(paper.get("sections", []), ensure_ascii=False),
     paper.get("license", "cc-by"), "pmc_oa"))
con.commit()
con.close()
print("\nDB 已更新（PMID 已修正为真实值）")

# ========== Step 5: 导出 JSON 供抽取 ==========
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
r = con.execute("SELECT * FROM pmc_fulltext WHERE pmcid=?", (paper["pmcid"],)).fetchone()
sections = json.loads(r["sections"]) if r["sections"] else []

paper_json = {
    "pmcid": r["pmcid"], "pmid": r["pmid"] or "", "doi": r["doi"] or "",
    "title": r["title"] or "", "abstract": r["abstract"] or "",
    "sections": sections,
    "license": (r["license"] or "cc-by").lower(),
    "evidence_level": "", "journal": r["journal"] or "",
    "paper_batch_id": "batch:pmc_oa:verified",
    "source_id": "pmc:" + r["pmcid"],
}
json_path = os.path.join(BASE, "paper-kg-extraction/data/single_pmc.json")
with open(json_path, "w", encoding="utf-8") as f:
    json.dump(paper_json, f, ensure_ascii=False, indent=2)

total_chars = sum(len(s.get("text", "")) for s in sections)
print("论文 JSON: %s (%d 章节, %d 字)" % (json_path, len(sections), total_chars))
print("\n下一步:")
print("  cd ~/work/zhaozhengyi/paper-kg-extraction")
print("  rm -f out/candidate_kg/paper_triples.jsonl")
print("  conda run -n zhaozhengyi --no-capture-output python scripts/02_run_extraction.py \\")
print("    --from json --input data/single_pmc.json \\")
print("    --out out/candidate_kg/paper_triples.jsonl \\")
print("    --batch-id batch:pmc_oa:verified")
