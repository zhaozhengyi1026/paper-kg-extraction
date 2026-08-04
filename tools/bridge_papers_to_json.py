"""
桥接脚本：从 med-paper-downloader 的 papers 表导出论文，
转为 paper-kg-extraction 的 --from json 格式。
"""
import sqlite3, json, argparse, os
from pathlib import Path

def export_papers(db_path: str, out_dir: str, limit: int = 0,
                  newest_first: bool = False, clean: bool = False):
    """从 papers 表读论文，输出为抽取器可用的 JSON 文件。"""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    paper_columns = {
        row["name"] for row in con.execute("PRAGMA table_info(papers)")
    }

    def paper_column(name: str, default_sql: str = "''") -> str:
        """Select an optional papers column or a compatible default."""
        return f"p.{name}" if name in paper_columns else f"{default_sql} AS {name}"

    has_fulltext = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pmc_fulltext'"
    ).fetchone() is not None
    fulltext_columns = ("f.sections AS fulltext_sections, f.license AS fulltext_license "
                        if has_fulltext else
                        "NULL AS fulltext_sections, NULL AS fulltext_license ")
    sql = (
        "SELECT p.pmid, p.pmcid, p.doi, p.title, p.abstract, "
        f"{paper_column('license')}, {paper_column('evidence_level')}, "
        f"{paper_column('journal')}, {paper_column('pub_date')}, "
        f"{paper_column('authority_tier')}, "
        f"{paper_column('authority_score', '0')}, "
        + fulltext_columns
        + "FROM papers p "
    )
    if has_fulltext:
        sql += "LEFT JOIN pmc_fulltext f ON f.pmcid = p.pmcid "
    sql += "ORDER BY p.rowid DESC, p.pmid DESC" if newest_first else "ORDER BY p.pmid"
    if limit > 0:
        sql += " LIMIT ?"

    rows = con.execute(sql, (limit,) if limit > 0 else ()).fetchall()
    con.close()

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if clean:
        for old_file in out.glob("*.json"):
            old_file.unlink()

    papers = []
    for r in rows:
        # 把 abstract 包装成单个 section（模拟全文结构）
        try:
            sections = json.loads(r["fulltext_sections"]) if r["fulltext_sections"] else []
        except (json.JSONDecodeError, TypeError):
            sections = []
        if not sections and r["abstract"]:
            sections = [{"title": "Abstract", "text": r["abstract"]}]

        paper = {
            "pmid": r["pmid"] or "",
            "pmcid": r["pmcid"] or "",
            "doi": r["doi"] or "",
            "title": r["title"] or "",
            "abstract": r["abstract"] or "",
            "sections": sections,
            "license": (r["fulltext_license"] or r["license"] or "pubmed").lower(),
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
    p.add_argument("--newest-first", action="store_true",
                   help="优先导出数据库中最近插入或更新的论文")
    p.add_argument("--clean", action="store_true",
                   help="导出前清理目标目录中的旧 JSON 文件")
    args = p.parse_args()
    export_papers(args.db, args.out, args.limit, args.newest_first, args.clean)
