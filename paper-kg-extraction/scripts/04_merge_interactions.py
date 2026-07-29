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

import argparse, json, sys
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from interaction_credibility import is_interaction, cross_source_merge  # noqa: E402


def load_triples(path: str) -> list[dict]:
    """读 JSONL，每行一条三元组。"""
    triples = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            triples.append(json.loads(line))
    return triples


def merge_interactions(in_path: str, out_path: str):
    triples = load_triples(in_path)
    print(f"读入 {len(triples)} 条三元组", file=sys.stderr)

    # 分离交互和非交互
    interactions = []
    non_interactions = []
    for tr in triples:
        if is_interaction(tr.get("relation", "")):
            interactions.append(tr)
        else:
            non_interactions.append(tr)

    print(f"  交互: {len(interactions)} 条", file=sys.stderr)
    print(f"  非交互: {len(non_interactions)} 条", file=sys.stderr)

    # 按 (head, relation, tail) 分组
    groups: dict[str, list[dict]] = defaultdict(list)
    for tr in interactions:
        # 对 HHI，如果 direction 不同 (A_affects_B vs B_affects_A) 则分到不同组
        # 但 bidirectional 与任意方向都能合并
        rel = tr.get("relation", "")
        direction = tr.get("qualifiers", {}).get("direction", "")
        key = (tr.get("head", ""), rel, tr.get("tail", ""), direction)
        groups[key].append(tr)

    single_source = sum(1 for v in groups.values() if len(v) == 1)
    multi_source = sum(1 for v in groups.values() if len(v) >= 2)
    print(f"  独立交互对: {len(groups)} 个", file=sys.stderr)
    print(f"    单源: {single_source}", file=sys.stderr)
    print(f"    多源 (>=2篇论文): {multi_source}", file=sys.stderr)

    # 跨源合并 (使用 v2.0 cross_source_merge)
    merged_interactions = cross_source_merge(groups)

    # 分级统计
    tiers = {"A": 0, "B": 0, "C": 0, "D": 0}
    for m in merged_interactions:
        t = m.get("ics_tier", "D")
        tiers[t] = tiers.get(t, 0) + 1

    print(f"\n  合并后交互: {len(merged_interactions)} 条 (ICS v2.0)", file=sys.stderr)
    for t in ["A", "B", "C", "D"]:
        if tiers[t] > 0:
            print(f"    {t} 级: {tiers[t]} 条", file=sys.stderr)

    # 统计 ICS 分布
    scores = [m.get("ics_score", 0) for m in merged_interactions]
    if scores:
        print(f"    ICS 范围: {min(scores)}-{max(scores)}, 平均: {sum(scores)/len(scores):.1f}",
              file=sys.stderr)

    # 输出
    all_out = non_interactions + merged_interactions
    with open(out_path, "w", encoding="utf-8") as f:
        for tr in all_out:
            f.write(json.dumps(tr, ensure_ascii=False) + "\n")

    print(f"\n写出 {len(all_out)} 条 -> {out_path}", file=sys.stderr)
    print(f"  (非交互 {len(non_interactions)} + 合并交互 {len(merged_interactions)})",
          file=sys.stderr)

    return {
        "ics_version": "2.0",
        "input_triples": len(triples),
        "input_interactions": len(interactions),
        "unique_pairs": len(groups),
        "multi_source_pairs": multi_source,
        "merged_interactions": len(merged_interactions),
        "tier_distribution": tiers,
        "ics_score_range": {"min": min(scores) if scores else 0, "max": max(scores) if scores else 0, "avg": sum(scores)/len(scores) if scores else 0},
        "output_total": len(all_out),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="跨论文合并交互三元组 v2.0")
    ap.add_argument("input", help="输入 JSONL 文件 (paper_triples.jsonl)")
    ap.add_argument("--out", required=True, help="输出 JSONL 路径")
    args = ap.parse_args()

    report = merge_interactions(args.input, args.out)
    print(json.dumps(report, ensure_ascii=False, indent=2))
