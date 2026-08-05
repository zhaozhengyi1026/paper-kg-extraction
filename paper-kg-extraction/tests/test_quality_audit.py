from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from quality_audit import run_quality_audit  # noqa: E402


def _write(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")


def test_audit_saves_all_artifacts(tmp_path):
    accepted = tmp_path / "paper_triples.jsonl"
    rejected = tmp_path / "paper_triples_rejects.jsonl"
    warnings = tmp_path / "paper_triples_warnings.jsonl"
    _write(accepted, [{
        "source_id": "pmid:1", "head": "Smoking", "head_type": "Exposure",
        "relation": "HAS_PHENOTYPE", "tail": "restenosis",
        "tail_type": "PhenotypicFeature", "evidence_sentence": "Smoking caused restenosis.",
    }])
    _write(rejected, [{"reject_reason": "ev_entity_mismatch", "head": "DMP1"}])
    _write(warnings, [{"warn_reason": "nli_neutral", "head": "A", "tail": "B"}])
    out_dir = tmp_path / "audit"
    summary = run_quality_audit(accepted, rejected, warnings, out_dir)
    assert summary["category_counts"] == {"accepted": 1, "rejected": 1, "warning": 1}
    assert summary["risk_flag_counts"]["causal_or_predictive_phenotype"] == 1
    assert (out_dir / "quality_audit.tsv").exists()
    assert (out_dir / "quality_audit.jsonl").exists()
    assert json.loads((out_dir / "quality_summary.json").read_text(encoding="utf-8"))["records"] == 3
