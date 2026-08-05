#!/usr/bin/env python3
"""Prepare a deterministic PMC Open Access full-text extraction sample."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
PARSER_DIR = ROOT / "med-paper-downloader"
sys.path.insert(0, str(PARSER_DIR))
from parsers.jats_xml import parse_jats_xml  # noqa: E402


ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
OAI = "https://www.ncbi.nlm.nih.gov/pmc/oai/oai.cgi"


def fetch(url: str, retries: int = 5) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            completed = subprocess.run(
                ["curl", "-L", "--fail", "--silent", "--show-error",
                 "--max-time", "90", "--user-agent", "medkg-fulltext-sampler/1.0", url],
                check=True, capture_output=True, timeout=100,
            )
            return completed.stdout
        except Exception as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"request failed after {retries} attempts: {last}")


def search_ids(count: int, query: str) -> list[str]:
    params = urllib.parse.urlencode({
        "db": "pmc", "term": query, "retmax": count,
        "sort": "pub date", "retmode": "json",
    })
    payload = json.loads(fetch(f"{ESEARCH}?{params}"))
    return payload["esearchresult"]["idlist"]


def get_article(pmc_numeric_id: str) -> dict | None:
    params = urllib.parse.urlencode({
        "verb": "GetRecord", "identifier": f"oai:pubmedcentral.nih.gov:{pmc_numeric_id}",
        "metadataPrefix": "pmc",
    })
    root = ET.fromstring(fetch(f"{OAI}?{params}"))
    article = next((element for element in root.iter()
                    if element.tag.rsplit("}", 1)[-1] == "article"), None)
    if article is None:
        return None
    # OAI wraps JATS in versioned namespaces; the package parser consumes the
    # namespace-free XML found inside downloaded PMC OA archives.
    for element in article.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]
    parsed = parse_jats_xml(ET.tostring(article, encoding="unicode"))
    if not parsed:
        return None
    parsed["source"] = "pmc_oa"
    parsed["source_id"] = f"pmc:{parsed.get('pmcid') or 'PMC' + pmc_numeric_id}"
    return parsed


def aggregate_hash(files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare N PMC OA papers with real sectioned full text")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--target", type=int, default=200)
    parser.add_argument("--candidate-count", type=int, default=600)
    parser.add_argument("--min-fulltext-chars", type=int, default=2000)
    parser.add_argument("--interval", type=float, default=0.34)
    parser.add_argument("--query", default="open access[filter]")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    accepted = sorted(args.out.glob("PMC*.json"))
    accepted_ids = {path.stem for path in accepted}
    rows = []
    for path in accepted:
        paper = json.loads(path.read_text(encoding="utf-8"))
        rows.append((paper.get("pmcid", path.stem), paper.get("pmid", ""),
                     len(paper.get("full_text", "")), len(paper.get("sections", [])),
                     paper.get("title", "")))

    print(f"searching PMC candidates: query={args.query!r} count={args.candidate_count}", flush=True)
    candidate_ids = search_ids(args.candidate_count, args.query)
    print(f"found {len(candidate_ids)} candidate IDs", flush=True)
    failures = 0
    for index, numeric_id in enumerate(candidate_ids, 1):
        if len(rows) >= args.target:
            break
        pmcid = f"PMC{numeric_id}"
        if pmcid in accepted_ids:
            continue
        try:
            paper = get_article(numeric_id)
            time.sleep(args.interval)
        except Exception as exc:
            failures += 1
            print(f"[{index}/{len(candidate_ids)}] {pmcid} error: {exc}", flush=True)
            continue
        if not paper:
            failures += 1
            continue
        full_text = (paper.get("full_text") or "").strip()
        sections = [section for section in paper.get("sections", []) if (section.get("text") or "").strip()]
        if len(full_text) < args.min_fulltext_chars or not sections:
            continue
        paper["sections"] = sections
        paper["full_text"] = full_text
        output = args.out / f"{paper.get('pmcid') or pmcid}.json"
        output.write_text(json.dumps(paper, ensure_ascii=False, indent=2), encoding="utf-8")
        accepted_ids.add(output.stem)
        rows.append((paper.get("pmcid", pmcid), paper.get("pmid", ""),
                     len(full_text), len(sections), paper.get("title", "")))
        print(f"accepted {len(rows)}/{args.target}: {output.stem} chars={len(full_text)}", flush=True)

    files = sorted(args.out.glob("PMC*.json"))[:args.target]
    if len(files) != args.target:
        raise RuntimeError(f"only prepared {len(files)}/{args.target} full-text papers; failures={failures}")
    keep = {path.name for path in files}
    if any(path.name not in keep for path in args.out.glob("PMC*.json")):
        raise RuntimeError("output contains more than target papers; use a fresh directory")

    rows_by_id = {row[0]: row for row in rows}
    with (args.out / "fulltext_manifest.tsv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, dialect="excel-tab")
        writer.writerow(["pmcid", "pmid", "fulltext_chars", "section_count", "title"])
        for path in files:
            writer.writerow(rows_by_id[path.stem])
    sample_hash = aggregate_hash(files)
    (args.out / "sample_sha256.txt").write_text(sample_hash + "\n", encoding="ascii")
    lengths = [rows_by_id[path.stem][2] for path in files]
    summary = {
        "papers": len(files), "all_have_sections": True,
        "min_fulltext_chars_required": args.min_fulltext_chars,
        "min_fulltext_chars": min(lengths), "avg_fulltext_chars": round(sum(lengths) / len(lengths), 1),
        "max_fulltext_chars": max(lengths), "sample_sha256": sample_hash,
        "query": args.query,
    }
    (args.out / "sample_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
