# -*- coding: utf-8 -*-
"""Offline post-run audit for accepted, rejected, and warning records."""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path


FIELDS = (
    "record_no", "category", "source_id", "head", "head_type", "relation",
    "tail", "tail_type", "evidence_sentence", "confidence", "reason",
    "detail", "risk_flags",
)
NEGATION_RE = re.compile(
    r"\b(?:not|without|neither|nor|failed to|lack(?:ing)?|absence of|"
    r"non-significant|insignificant)\b|不|无|未|没有|缺乏|阴性", re.I,
)
CAUSAL_RE = re.compile(r"\b(?:caus\w*|induc\w*|trigger\w*|predict\w*|risk factor)\b", re.I)
DIRECT_TARGET_RE = re.compile(
    r"\b(?:direct(?:ly)?\s+bind\w*|target of|antibod(?:y|ies) (?:against|to)|"
    r"inhibitor of|binding affinity)\b", re.I,
)
PREDICTED_TARGET_RE = re.compile(
    r"network pharmacology|molecular docking|predicted|potential targets?|target network", re.I,
)


def _records(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL {path}:{number}: {exc}") from exc


def _source_id(record: dict) -> str:
    return str(record.get("source_id") or record.get("evidence_source_id") or
               record.get("provenance", {}).get("source_id") or "")


def _entity_present(entity: str, evidence: str) -> bool:
    def norm(value: str) -> str:
        value = value.casefold().replace("α", "alpha").replace("β", "beta").replace("γ", "gamma")
        return re.sub(r"[^\w]+", "", value)
    return bool(entity and norm(entity) in norm(evidence))


def _flags(record: dict, category: str) -> list[str]:
    evidence = record.get("evidence_sentence", "") or ""
    relation = record.get("relation", "") or ""
    flags = []
    if record.get("head") and not _entity_present(record["head"], evidence):
        flags.append("head_not_literal_in_evidence")
    if record.get("tail") and not _entity_present(record["tail"], evidence):
        flags.append("tail_not_literal_in_evidence")
    scrubbed = re.sub(r"\bnot only\b|\bNO[- ]?(?:donor|releasing|mediated)\b", "", evidence, flags=re.I)
    if NEGATION_RE.search(scrubbed):
        flags.append("negated_evidence_review")
    if len(evidence.strip()) < 35 and category == "accepted":
        flags.append("weak_evidence_fragment")
    if relation == "HAS_PHENOTYPE" and CAUSAL_RE.search(evidence):
        flags.append("causal_or_predictive_phenotype")
    if relation == "MOLECULARLY_TARGETS":
        if PREDICTED_TARGET_RE.search(evidence):
            flags.append("predicted_target_claim")
        elif not DIRECT_TARGET_RE.search(evidence):
            flags.append("indirect_target_claim")
    if record.get("reject_reason") == "ev_entity_mismatch":
        flags.append("entity_mismatch_reject_review")
    if record.get("warn_reason") == "nli_neutral":
        flags.append("nli_neutral_review")
    return sorted(set(flags))


def run_quality_audit(accepted_path: Path, rejects_path: Path, warnings_path: Path,
                      out_dir: Path) -> dict:
    """Write compact review artifacts and return their machine-readable summary."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    categories = Counter()
    reasons = Counter()
    relations = Counter()
    flags = Counter()
    for category, path in (("accepted", accepted_path), ("rejected", rejects_path),
                           ("warning", warnings_path)):
        for record in _records(path):
            record_flags = _flags(record, category)
            row = {
                "record_no": len(rows) + 1,
                "category": category,
                "source_id": _source_id(record),
                "head": record.get("head", ""), "head_type": record.get("head_type", ""),
                "relation": record.get("relation", ""),
                "tail": record.get("tail", ""), "tail_type": record.get("tail_type", ""),
                "evidence_sentence": (record.get("evidence_sentence", "") or "").replace("\n", " "),
                "confidence": record.get("confidence", ""),
                "reason": record.get("reject_reason") or record.get("warn_reason") or "",
                "detail": record.get("reject_detail") or record.get("warn_detail") or "",
                "risk_flags": ";".join(record_flags),
            }
            rows.append(row)
            categories[category] += 1
            reasons[f"{category}:{row['reason'] or 'none'}"] += 1
            relations[f"{category}:{row['relation'] or 'none'}"] += 1
            flags.update(record_flags)

    with (out_dir / "quality_audit.tsv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, dialect="excel-tab")
        writer.writeheader()
        writer.writerows(rows)
    with (out_dir / "quality_audit.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = {
        "records": len(rows), "category_counts": dict(categories),
        "reason_counts": dict(reasons), "relation_counts": dict(relations),
        "risk_flag_counts": dict(flags),
        "important_note": "Risk flags are review candidates, not medical ground truth.",
    }
    (out_dir / "quality_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit one completed extraction run")
    parser.add_argument("--accepted", required=True, type=Path,
                        help="accepted or merged triples JSONL")
    parser.add_argument("--rejects", required=True, type=Path)
    parser.add_argument("--warnings", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    summary = run_quality_audit(args.accepted, args.rejects, args.warnings, args.out_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
