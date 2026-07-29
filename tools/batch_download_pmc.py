#!/usr/bin/env python3
"""
批量下载 PMC OA 全文：自动查找 DB 中有 PMCID 但尚未下载全文的论文，
逐个尝试下载，有全文则存 pmc_fulltext，没有则跳过。

用法:
  python batch_download_pmc.py              # 下载全部
  python batch_download_pmc.py --limit 10   # 只下载 10 篇
  python batch_download_pmc.py --dry-run    # 只检查，不下载
"""
import sqlite3, os, sys, json, urllib.request, time, argparse
from lxml import etree

BASE = os.path.expanduser("~/work/zhaozhengyi")
DB = os.path.join(BASE, "med-papers/metadata.db")
JSON_OUT = os.path.join(BASE, "paper-kg-extraction/data/papers_json")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="最多下载篇数 (0=全部)")
    ap.add_argument("--dry-run", action="store_true", help="只检查可下载数，不实际下载")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    # 找有 PMCID 但没下载全文的论文
    # 已下载的: pmc_fulltext 表中已有的
    rows = con.execute("""
        SELECT p.pmid, p.pmcid, p.title
        FROM papers p
        WHERE p.pmcid IS NOT NULL AND p.pmcid != ''
          AND p.pmcid NOT IN (SELECT pmcid FROM pmc_fulltext)
        ORDER BY p.authority_score DESC NULLS LAST
    """).fetchall()

    total = len(rows)
    print(f"共 {total} 篇论文有 PMCID 但尚未下载全文")

    if args.dry_run:
        print("(dry-run，不下载)")
        con.close()
        return

    if args.limit > 0:
        rows = rows[:args.limit]
        print(f"本次最多下载 {args.limit} 篇")

    success = 0
    skipped = 0
    failed = 0

    for i, r in enumerate(rows, 1):
        pmcid = r["pmcid"]
        title = (r["title"] or "")[:60]
        pmc_num = pmcid.replace("PMC", "")
        print(f"\n[{i}/{len(rows)}] {pmcid}: {title}")

        # Step 1: 尝试下载
        efetch_url = (
            f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
            f"?db=pmc&id={pmc_num}&retmode=xml"
        )
        req = urllib.request.Request(efetch_url)
        req.add_header("User-Agent", "med-paper-downloader/1.0 (1292589632@qq.com)")
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            xml_text = resp.read().decode("utf-8")
        except Exception as e:
            print(f"  SKIP: 下载失败 ({e})")
            skipped += 1
            continue

        # Step 2: 验证是否有真实 <body>
        try:
            parser_xml = etree.XMLParser(recover=True)
            root = etree.fromstring(xml_text.encode("utf-8"), parser_xml)
        except Exception:
            print("  SKIP: XML 解析失败")
            skipped += 1
            continue

        body = root.find(".//body")
        if body is None:
            print("  SKIP: 无 <body>（非 OA 全文）")
            skipped += 1
            continue
        body_text = etree.tostring(body, method="text", encoding="unicode")
        if len(body_text.strip()) < 500:
            print(f"  SKIP: body 仅 {len(body_text.strip())} 字符（占位引用）")
            skipped += 1
            continue

        # Step 3: 提取真实 PMID
        real_pmid = None
        for aid in root.findall(".//article-id"):
            if aid.get("pub-id-type") == "pmid":
                real_pmid = aid.text
                break

        # Step 4: 解析 JATS XML
        sys.path.insert(0, os.path.join(BASE, "med-paper-downloader"))
        from parsers.jats_xml import parse_jats_xml
        paper = parse_jats_xml(xml_text)
        if not paper.get("pmcid"):
            paper["pmcid"] = pmcid
        if real_pmid:
            paper["pmid"] = real_pmid

        sections = paper.get("sections", [])
        total_chars = sum(len(s.get("text", "")) for s in sections)
        print(f"  下载成功: {len(sections)} 章节, {total_chars} 字")

        # Step 5: 写入 DB
        try:
            con.execute("""INSERT OR REPLACE INTO papers
                (pmid, pmcid, doi, title, abstract, license, source)
                VALUES (?,?,?,?,?,?,?)""",
                (paper.get("pmid"), paper["pmcid"], paper.get("doi"),
                 paper.get("title"), paper.get("abstract"),
                 paper.get("license", "cc-by"), "pmc_oa"))

            con.execute("""INSERT OR REPLACE INTO pmc_fulltext
                (pmcid, pmid, doi, title, abstract, sections, license, source, created_at)
                VALUES (?,?,?,?,?,?,?,?,datetime('now'))""",
                (paper["pmcid"], paper.get("pmid"), paper.get("doi"),
                 paper.get("title"), paper.get("abstract"),
                 json.dumps(sections, ensure_ascii=False),
                 paper.get("license", "cc-by"), "pmc_oa"))
            con.commit()
        except Exception as e:
            print(f"  WARN: DB 写入失败 ({e})")
            failed += 1
            continue

        # Step 6: 导出 JSON 到 papers_json 目录
        paper_json = {
            "pmcid": paper["pmcid"],
            "pmid": paper.get("pmid", "") or "",
            "doi": paper.get("doi", "") or "",
            "title": paper.get("title", "") or "",
            "abstract": paper.get("abstract", "") or "",
            "sections": sections,
            "license": (paper.get("license", "cc-by") or "cc-by").lower(),
            "evidence_level": "",
            "journal": paper.get("journal", "") or "",
            "paper_batch_id": "batch:pmc_oa:batch_download",
            "source_id": f"pmc:{paper['pmcid']}",
        }
        os.makedirs(JSON_OUT, exist_ok=True)
        json_path = os.path.join(JSON_OUT, f"{paper.get('pmid') or paper['pmcid']}.json")  # overwrites bridge abstract JSON)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(paper_json, f, ensure_ascii=False, indent=2)

        success += 1

        # 礼貌延迟
        time.sleep(0.5)

    con.close()
    print(f"\n{'='*50}")
    print(f"批量 PMC 下载完成:")
    print(f"  成功: {success} 篇 (全文已存入 pmc_fulltext)")
    print(f"  跳过: {skipped} 篇 (非 OA 或占位引用)")
    print(f"  失败: {failed} 篇")
    print(f"\nJSON 导出到: {JSON_OUT}/")
    print(f"下一步: bash run_pipeline.sh extract-fulltext-batch")


if __name__ == "__main__":
    main()
