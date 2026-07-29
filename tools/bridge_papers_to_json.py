"""
桥接脚本：从 med-paper-downloader 的 papers 表导出论文，
转为 paper-kg-extraction 的 --from json 格式。
"""
import sqlite3, json, argparse, os
from pathlib import Path

def export_papers(db_path: str, out_dir: str, limit: int = 0):
    """从 papers 表读论文，输出为抽取器可用的 JSON 文件。"""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    sql = ("SELECT pmid, pmcid, doi, title, abstract, license, evidence_level, "
           "journal, pub_date, authority_tier, authority_score FROM papers")
    if limit > 0:
        sql += f" LIMIT {limit}"

    rows = con.execute(sql).fetchall()
    con.close()

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    papers = []
    for r in rows:
        # 把 abstract 包装成单个 section（模拟全文结构）
        sections = []
        if r["abstract"]:
            sections = [{"title": "Abstract", "text": r["abstract"]}]

        paper = {
            "pmid": r["pmid"] or "",
            "pmcid": r["pmcid"] or "",
            "doi": r["doi"] or "",
            "title": r["title"] or "",
            "abstract": r["abstract"] or "",
            "sections": sections,
            "license": (r["license"] or "pubmed").lower(),
            "evidence_level": r["evidence_level"] or "",
            "journal": r["journal"] or "",
            "authority_tier": r["authority_tier"] or "",
            "authority_score": r["authority_score"] or 0,
            "paper_cebm_level": r["evidence_level"] or "",
            "paper_batch_id": "batch:pubmed:bridge:demo",
        }
        papers.append(paper)

    # 每篇一个 JSON 文件
    for paper in papers:
        pmid = paper["pmid"] or paper["pmcid"] or str(id(paper))
        fpath = out / f"{pmid}.json"
        fpath.write_text(json.dumps(paper, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"导出 {len(papers)} 篇论文 → {out}/")
    return len(papers)

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=os.path.expanduser("~/work/zhaozhengyi/med-papers/metadata.db"))
    p.add_argument("--out", default=os.path.expanduser("~/work/zhaozhengyi/paper-kg-extraction/data/papers_json"))
    p.add_argument("--limit", type=int, default=10)
    args = p.parse_args()
    export_papers(args.db, args.out, args.limit)
