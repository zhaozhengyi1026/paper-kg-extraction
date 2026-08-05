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
from llm_client import (FallbackLLMClient, create_extraction_client,
                        create_nli_client, load_env_file)  # noqa: E402


class _FakeLLM:
    def __init__(self, model, result=None, error=None):
        self.model = model
        self.result = result
        self.error = error
        self.calls = 0
        self.metrics = {}

    def complete_json(self, system, user, retries=5):
        self.calls += 1
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


def test_db_stream_uses_fulltext_then_abstract_and_shards(tmp_path):
    runner = _load_runner()
    db = tmp_path / "corpus.db"
    con = sqlite3.connect(db)
    con.executescript("""
      CREATE TABLE papers (
        pmid TEXT, pmcid TEXT, doi TEXT, title TEXT, abstract TEXT, license TEXT,
        evidence_level TEXT, journal TEXT, authority_tier TEXT, authority_score REAL
      );
      CREATE TABLE pmc_fulltext (
        pmcid TEXT, sections TEXT, license TEXT
      );
      INSERT INTO papers VALUES
        ('10','PMC10','','full','abstract 10','pubmed','','','','0'),
        ('11','','','abstract','abstract 11','pubmed','','','','0');
      INSERT INTO pmc_fulltext VALUES
        ('PMC10','[{"title":"Results","text":"full text 10"}]','cc-by');
    """)
    con.commit()
    con.close()
    shard0 = list(runner.papers_from_db(str(db), 0, "batch", 2, 0))
    shard1 = list(runner.papers_from_db(str(db), 0, "batch", 2, 1))
    assert [p["pmid"] for p in shard0] == ["10"]
    assert [p["pmid"] for p in shard1] == ["11"]
    assert shard0[0]["sections"][0]["text"] == "full text 10"
    assert shard1[0]["sections"] == [{"title": "Abstract", "text": "abstract 11"}]


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


def test_extraction_provider_can_force_deepseek(monkeypatch):
    monkeypatch.setenv("EXTRACTION_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    captured = {}
    def fake_client(**kwargs):
        captured.update(kwargs)
        return _FakeLLM(kwargs["model"])
    monkeypatch.setattr("llm_client.LLMClient", fake_client)
    assert create_extraction_client().model == "deepseek-v4-flash"
    assert captured["base_url"] == "https://api.deepseek.com"
    assert captured["model"] == "deepseek-v4-flash"
    assert captured["reasoning_effort"] == "high"
    assert captured["thinking_enabled"] is True


def test_extraction_circuit_breaker_skips_failed_primary(monkeypatch):
    monkeypatch.setenv("LLM_CIRCUIT_FAILURES", "1")
    primary = _FakeLLM("sensenova", error=RuntimeError("down"))
    fallback = _FakeLLM("deepseek", {"triples": []})
    client = FallbackLLMClient(primary, fallback)
    client.complete_json("system", "first")
    client.complete_json("system", "second")
    assert primary.calls == 1
    assert fallback.calls == 2
    assert client.fallback_reasons["circuit_open"] == 1


def test_env_loader_supports_missing_python_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("SENSENOVA_TEST_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text('SENSENOVA_TEST_KEY="configured"\n', encoding="utf-8")
    load_env_file(env_file)
    assert __import__("os").environ["SENSENOVA_TEST_KEY"] == "configured"


def test_nli_defaults_to_sensenova(monkeypatch):
    monkeypatch.delenv("NLI_MODEL", raising=False)
    monkeypatch.delenv("NLI_API_KEY", raising=False)
    monkeypatch.setenv("SENSENOVA_API_KEY", "test-key")
    monkeypatch.setenv("SENSENOVA_MODEL", "sensenova-test")
    client = create_nli_client()
    assert client.model == "sensenova-test"


def test_nli_falls_back_to_deepseek(monkeypatch):
    monkeypatch.setenv("SENSENOVA_API_KEY", "test-key")
    deepseek = _FakeLLM("deepseek", {"label": "entailment"})
    extraction_chain = FallbackLLMClient(_FakeLLM("sensenova"), deepseek)
    client = create_nli_client(extraction_chain)
    client.primary.complete_json = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("SenseNova unavailable")
    )
    assert client.complete_json("system", "user") == {"label": "entailment"}
    assert deepseek.calls == 1


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
    assert len(merged["evidence_records"]) == 2


def test_symmetric_interactions_merge_when_reversed(tmp_path):
    merger = _load_merger()
    rows = [
        {"head": "A", "head_id": "1", "tail": "B", "tail_id": "2",
         "relation": "INTERACTS_WITH", "qualifiers": {},
         "evidence_source_id": "pmid:1", "provenance": {"source_id": "pmid:1"}},
        {"head": "B", "head_id": "2", "tail": "A", "tail_id": "1",
         "relation": "INTERACTS_WITH", "qualifiers": {},
         "evidence_source_id": "pmid:2", "provenance": {"source_id": "pmid:2"}},
    ]
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    input_path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    report = merger.merge_interactions(str(input_path), str(output_path))
    merged = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["unique_pairs"] == 1
    assert merged["source_count"] == 2


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


def test_bridge_supports_pubmed_full_sync_schema(tmp_path):
    db = tmp_path / "pubmed_full.db"
    con = sqlite3.connect(db)
    con.executescript("""
      CREATE TABLE papers (
        pmid TEXT, pmcid TEXT, doi TEXT, title TEXT, authors TEXT,
        journal TEXT, pub_date TEXT, abstract TEXT, mesh_terms TEXT,
        keywords TEXT, pub_types TEXT, source TEXT,
        created_at TEXT, updated_at TEXT
      );
      CREATE TABLE pmc_fulltext (
        pmcid TEXT, pmid TEXT, doi TEXT, title TEXT, authors TEXT,
        journal TEXT, pub_date TEXT, abstract TEXT, full_text TEXT,
        sections TEXT, "references" TEXT, license TEXT, source TEXT,
        created_at TEXT
      );
      INSERT INTO papers VALUES
        ('1','PMC1','','title','','journal','2026','abstract','','','','pubmed','','');
      INSERT INTO pmc_fulltext VALUES
        ('PMC1','1','','title','','journal','2026','abstract','full',
         '[{"title":"Results","text":"full text"}]','','cc-by','pmc','');
    """)
    con.commit()
    con.close()

    out = tmp_path / "papers"
    assert export_papers(str(db), str(out), limit=200) == 1
    paper = json.loads((out / "1.json").read_text(encoding="utf-8"))
    assert paper["license"] == "cc-by"
    assert paper["evidence_level"] == ""
    assert paper["authority_score"] == 0
    assert paper["sections"] == [{"title": "Results", "text": "full text"}]


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
        "--from", "json", "--input", str(input_dir), "--out", str(output),
        "--mock", "--workers", "2",
    ], check=True, cwd=ROOT, capture_output=True, text=True,
       encoding="utf-8", errors="replace")
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 4
    assert {row["provenance"]["source_id"] for row in rows} == {"pmc:PMC1", "pmc:PMC2"}
    report = json.loads((tmp_path / "extraction_report.json").read_text(encoding="utf-8"))
    assert report["workers"] == 2
    assert report["reproducibility"]["input_manifest_sha256"]
