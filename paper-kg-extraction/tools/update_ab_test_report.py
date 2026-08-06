#!/usr/bin/env python3
"""Append an idempotent SenseNova/DeepSeek comparison to the A/B history."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def summarize(run: Path) -> dict:
    report = read_json(run / "extraction_report.json")
    edges = read_jsonl(run / "paper_triples_merged.jsonl")
    papers = int(report.get("papers", 0))
    source_ids = {edge.get("evidence_source_id") for edge in edges}
    literal = sum(
        compact(edge.get("head", "")) in compact(edge.get("evidence_sentence", "")) and
        compact(edge.get("tail", "")) in compact(edge.get("evidence_sentence", ""))
        for edge in edges
    )
    coverage = report.get("paper_evidence_coverage") or {}
    covered = int(coverage.get("papers_with_accepted_edges", len(source_ids)))
    return {
        "papers": papers, "edges": len(edges), "rejects": report.get("rejects_total", 0),
        "warnings": report.get("warnings_total", 0), "covered": covered,
        "paper_coverage": 100 * covered / papers if papers else 0,
        "literal": literal, "literal_rate": 100 * literal / len(edges) if edges else 0,
        "zero_reasons": coverage.get("zero_output_reasons", {}),
        "commit": report.get("reproducibility", {}).get("git_commit", "unknown"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sensenova-run", required=True, type=Path)
    parser.add_argument("--deepseek-run", required=True, type=Path)
    parser.add_argument("--label", required=True)
    parser.add_argument("--improvements", default="待分析")
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).resolve().parents[1] / "docs" / "AB_TEST_RESULTS.md")
    args = parser.parse_args()
    sense, deep = summarize(args.sensenova_run), summarize(args.deepseek_run)
    marker = f"<!-- ab-test:{args.label} -->"
    old = args.out.read_text(encoding="utf-8") if args.out.exists() else "# A/B Test Results\n"
    if marker in old:
        raise SystemExit(f"report already contains {args.label}")
    block = f"""

{marker}
## {args.label}

| 指标 | SenseNova | DeepSeek |
|---|---:|---:|
| 输入论文 | {sense['papers']} | {deep['papers']} |
| accepted 三元组 | {sense['edges']} | {deep['edges']} |
| rejects / warnings | {sense['rejects']} / {sense['warnings']} | {deep['rejects']} / {deep['warnings']} |
| 有 accepted 的论文 | {sense['covered']} | {deep['covered']} |
| 论文证据覆盖率 | {sense['paper_coverage']:.1f}% | {deep['paper_coverage']:.1f}% |
| 头尾实体证据覆盖率 | {sense['literal_rate']:.1f}% | {deep['literal_rate']:.1f}% |
| 代码版本 | `{sense['commit']}` | `{deep['commit']}` |

零产出原因：SenseNova `{json.dumps(sense['zero_reasons'], ensure_ascii=False)}`；DeepSeek `{json.dumps(deep['zero_reasons'], ensure_ascii=False)}`。

改进：{args.improvements}
"""
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(old.rstrip() + block, encoding="utf-8")
    print(args.out)


if __name__ == "__main__":
    main()
