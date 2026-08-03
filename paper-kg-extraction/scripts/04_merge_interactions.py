# -*- coding: utf-8 -*-
"""跨论文合并交互三元组 v2.0：多篇论文独立报告同一药物对时，合并并重新计算 ICS v2.0。

v2.0 改进:
  - 使用 ICS v2.0 的 8 维度评分
  - 合并时正确处理新增的 clinical_consequence / study_quality 维度
  - paper_cebm_level 传递到合并后的 ICS 计算

用法:
  python scripts/04_merge_interactions.py paper_triples.jsonl --out paper_triples_merged.jsonl
"""
from __future__ import annotations

import argparse, json, sqlite3, sys, tempfile
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from interaction_credibility import (  # noqa: E402
    SYMMETRIC_INTERACTION_RELATIONS, cross_source_merge, is_interaction,
)


def iter_triples(path: str):
    """流式读取 JSONL，避免全库结果进入内存。"""
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def merge_interactions(in_path: str, out_path: str, report_path: str | None = None):
    input_count = interaction_count = non_interaction_count = 0
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(prefix="kg_merge_", suffix=".sqlite3", delete=False)
    tmp.close()
    db = sqlite3.connect(tmp.name)
    db.execute("CREATE TABLE interactions (k TEXT NOT NULL, payload TEXT NOT NULL)")
    with open(out_path, "w", encoding="utf-8") as output:
      for tr in iter_triples(in_path):
        input_count += 1
        if not is_interaction(tr.get("relation", "")):
            non_interaction_count += 1
            output.write(json.dumps(tr, ensure_ascii=False) + "\n")
            continue
        interaction_count += 1
        # 对 HHI，如果 direction 不同 (A_affects_B vs B_affects_A) 则分到不同组
        # 但 bidirectional 与任意方向都能合并
        rel = tr.get("relation", "")
        direction = tr.get("qualifiers", {}).get("direction", "")
        head_key = tr.get("head_id") or tr.get("head", "")
        tail_key = tr.get("tail_id") or tr.get("tail", "")
        if rel in SYMMETRIC_INTERACTION_RELATIONS:
            head_key, tail_key = sorted((head_key, tail_key))
            direction = ""
        key = json.dumps((head_key, rel, tail_key, direction), ensure_ascii=False)
        db.execute("INSERT INTO interactions VALUES (?, ?)",
                   (key, json.dumps(tr, ensure_ascii=False)))
        if input_count % 10000 == 0:
            db.commit()
    db.commit()
    db.execute("CREATE INDEX interactions_key ON interactions(k)")
    print(f"读入 {input_count} 条三元组", file=sys.stderr)
    print(f"  交互: {interaction_count} 条", file=sys.stderr)
    print(f"  非交互: {non_interaction_count} 条", file=sys.stderr)

    unique_pairs, single_source, multi_source = db.execute("""
      SELECT COUNT(*),
             SUM(CASE WHEN n = 1 THEN 1 ELSE 0 END),
             SUM(CASE WHEN n >= 2 THEN 1 ELSE 0 END)
      FROM (SELECT COUNT(*) AS n FROM interactions GROUP BY k)
    """).fetchone()
    unique_pairs = unique_pairs or 0
    single_source = single_source or 0
    multi_source = multi_source or 0
    print(f"  独立交互对: {unique_pairs} 个", file=sys.stderr)
    print(f"    单源: {single_source}", file=sys.stderr)
    print(f"    多源 (>=2篇论文): {multi_source}", file=sys.stderr)

    # 跨源合并 (使用 v2.0 cross_source_merge)
    tiers = {"A": 0, "B": 0, "C": 0, "D": 0}
    score_min = score_max = score_sum = None
    merged_count = 0
    with open(out_path, "a", encoding="utf-8") as output:
        for (key,) in db.execute("SELECT DISTINCT k FROM interactions ORDER BY k"):
            members = [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM interactions WHERE k = ?", (key,))]
            merged = cross_source_merge({tuple(json.loads(key)): members})[0]
            output.write(json.dumps(merged, ensure_ascii=False) + "\n")
            merged_count += 1
            tier = merged.get("ics_tier", "D")
            tiers[tier] = tiers.get(tier, 0) + 1
            score = merged.get("ics_score", 0)
            score_min = score if score_min is None else min(score_min, score)
            score_max = score if score_max is None else max(score_max, score)
            score_sum = score if score_sum is None else score_sum + score
    db.close()
    Path(tmp.name).unlink(missing_ok=True)

    print(f"\n  合并后交互: {merged_count} 条 (ICS v2.0)", file=sys.stderr)
    for t in ["A", "B", "C", "D"]:
        if tiers[t] > 0:
            print(f"    {t} 级: {tiers[t]} 条", file=sys.stderr)

    # 统计 ICS 分布
    if merged_count:
        print(f"    ICS 范围: {score_min}-{score_max}, 平均: {score_sum/merged_count:.1f}",
              file=sys.stderr)

    output_total = non_interaction_count + merged_count
    print(f"\n写出 {output_total} 条 -> {out_path}", file=sys.stderr)
    print(f"  (非交互 {non_interaction_count} + 合并交互 {merged_count})",
          file=sys.stderr)

    report = {
        "ics_version": "2.0",
        "input_triples": input_count,
        "input_interactions": interaction_count,
        "unique_pairs": unique_pairs,
        "multi_source_pairs": multi_source,
        "merged_interactions": merged_count,
        "tier_distribution": tiers,
        "ics_score_range": {"min": score_min or 0, "max": score_max or 0,
                            "avg": score_sum / merged_count if merged_count else 0},
        "output_total": output_total,
    }
    report_file = Path(report_path) if report_path else Path(out_path).with_name("merge_report.json")
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="跨论文合并交互三元组 v2.0")
    ap.add_argument("input", help="输入 JSONL 文件 (paper_triples.jsonl)")
    ap.add_argument("--out", required=True, help="输出 JSONL 路径")
    ap.add_argument("--report", help="报告 JSON 路径（默认与输出文件同目录）")
    args = ap.parse_args()

    report = merge_interactions(args.input, args.out, args.report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
