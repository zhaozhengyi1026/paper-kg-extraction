# -*- coding: utf-8 -*-
"""Regression coverage for orchestration and cross-paper aggregation."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(REPO / "tools"))

from interaction_credibility import cross_source_merge  # noqa: E402
from bridge_papers_to_json import export_papers  # noqa: E402
from llm_client import FallbackLLMClient  # noqa: E402


class _FakeLLM:
    def __init__(self, model, result=None, error=None):
        self.model = model
        self.result = result
        self.error = error

    def complete_json(self, system, user, retries=5):
        if self.error:
            raise self.error
        return self.result


def _load_runner():
    path = ROOT / "scripts" / "02_run_extraction.py"
    spec = importlib.util.spec_from_file_location("run_extraction", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def _load_merger():
    path = ROOT / "scripts" / "04_merge_interactions.py"
    spec = importlib.util.spec_from_file_location("merge_interactions", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_checkpoint_includes_zero_edge_papers(tmp_path):
    runner = _load_runner()
    checkpoint = tmp_path / "processed.jsonl"
    checkpoint.write_text(
        json.dumps({"source_id": "pmid:1", "status": "completed", "edge_count": 0}) + "\n",
        encoding="utf-8",
    )
    assert runner.load_processed(checkpoint) == {"pmid:1"}


def test_extraction_prefers_sensenova():
    client = FallbackLLMClient(
        _FakeLLM("sensenova", {"triples": []}),
        _FakeLLM("deepseek", {"triples": ["fallback"]}),
    )
    assert client.complete_json("system", "user") == {"triples": []}
    assert client.usage_counts == {"sensenova": 1, "deepseek": 0}


def test_extraction_falls_back_to_deepseek():
    client = FallbackLLMClient(
        _FakeLLM("sensenova", error=RuntimeError("primary failed")),
        _FakeLLM("deepseek", {"triples": []}),
    )
    assert client.complete_json("system", "user") == {"triples": []}
    assert client.usage_counts == {"sensenova": 0, "deepseek": 1}


def test_cross_source_merge_counts_distinct_papers():
    base = {
        "head": "A", "tail": "B", "relation": "INTERACTS_WITH",
        "qualifiers": {}, "paper_authority_score": 50,
        "provenance": {"source_id": "pmid:1"},
    }
    second = {**base, "provenance": {"source_id": "pmid:2"}}
    merged = cross_source_merge({("A", "INTERACTS_WITH", "B", ""): [base, second]})[0]
    assert merged["source_count"] == 2
    assert merged["provenance"]["evidence_sources"] == ["pmid:1", "pmid:2"]


def test_merge_writes_report_next_to_output(tmp_path):
    merger = _load_merger()
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "merged.jsonl"
    input_path.write_text("", encoding="utf-8")
    report = merger.merge_interactions(str(input_path), str(output_path))
    report_path = tmp_path / "merge_report.json"
    assert report_path.exists()
    assert json.loads(report_path.read_text(encoding="utf-8")) == report


def test_bridge_cleans_stale_files_and_prefers_fulltext(tmp_path):
    db = tmp_path / "papers.db"
    con = sqlite3.connect(db)
    con.executescript("""
      CREATE TABLE papers (
        pmid TEXT, pmcid TEXT, doi TEXT, title TEXT, abstract TEXT, license TEXT,
        evidence_level TEXT, journal TEXT, pub_date TEXT,
        authority_tier TEXT, authority_score REAL
      );
      CREATE TABLE pmc_fulltext (pmcid TEXT, sections TEXT, license TEXT);
      INSERT INTO papers VALUES
        ('1','PMC1','','old','abstract old','pubmed','','','2020','','0'),
        ('2','PMC2','','new','abstract new','pubmed','','','2021','','0');
      INSERT INTO pmc_fulltext VALUES
        ('PMC2','[{"title":"Results","text":"full text"}]','cc-by');
    """)
    con.commit()
    con.close()
    out = tmp_path / "json"
    out.mkdir()
    (out / "stale.json").write_text("{}", encoding="utf-8")
    assert export_papers(str(db), str(out), 1, newest_first=True, clean=True) == 1
    assert not (out / "stale.json").exists()
    paper = json.loads((out / "2.json").read_text(encoding="utf-8"))
    assert paper["sections"][0]["text"] == "full text"
    assert paper["license"] == "cc-by"


def test_mock_keeps_same_edge_from_two_papers(tmp_path):
    sample = json.loads((ROOT / "examples" / "sample_paper.json").read_text(encoding="utf-8"))
    input_dir = tmp_path / "papers"
    input_dir.mkdir()
    for index in (1, 2):
        paper = dict(sample)
        paper["source_id"] = f"pmc:PMC{index}"
        (input_dir / f"{index}.json").write_text(json.dumps(paper), encoding="utf-8")
    output = tmp_path / "triples.jsonl"
    subprocess.run([
        sys.executable, str(ROOT / "scripts" / "02_run_extraction.py"),
        "--from", "json", "--input", str(input_dir), "--out", str(output), "--mock",
    ], check=True, cwd=ROOT, capture_output=True, text=True,
       encoding="utf-8", errors="replace")
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 4
    assert {row["provenance"]["source_id"] for row in rows} == {"pmc:PMC1", "pmc:PMC2"}
