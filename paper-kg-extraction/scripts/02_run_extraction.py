# -*- coding: utf-8 -*-
"""端到端抽取：读论文语料 → 逐篇抽取 → 写 out/candidate_kg/paper_triples.jsonl。

两种输入源（--from）：
  db    ：med-paper-collector 的 SQLite（默认路径见 --db；读 pmc_fulltext + papers 元数据）
  json  ：一个 JSON/JSONL 文件或目录，每篇论文一条（examples/sample_paper.json 的格式）

用法示例：
  # 离线冒烟（无需 API key，用示例论文 + mock 抽取）
  python scripts/02_run_extraction.py --from json --input examples/sample_paper.json --mock

  # 真跑：从采集库取带全文的论文，用配置好的 LLM 抽取，限 200 篇
  python scripts/02_run_extraction.py --from db --db /path/to/papers.db --limit 200

  # 指定采集批次（写入每条边的 provenance.paper_batch_id）
  python scripts/02_run_extraction.py --from db --limit 500 --batch-id batch:pmc_oa:20260703:q8f3a1c2
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.metadata
import json
import os
import platform
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

from extract import extract_paper, new_run_id  # noqa: E402
from llm_client import create_extraction_client  # noqa: E402
from schema_loader import Schema  # noqa: E402
from interaction_credibility import batch_enrich  # noqa: E402

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def _fmt_elapsed(sec: float) -> str:
    """将秒数格式化为 3h25m12s 形式。"""
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    elif m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def papers_from_db(db_path: str, limit: int, batch_id: str):
    """从 med-paper-collector 库读带全文的论文，联 papers 元数据补 evidence_level/license/authority。"""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    q = """
      SELECT f.pmcid, f.pmid, f.doi, f.title, f.abstract, f.sections, f.license AS f_lic,
             p.evidence_level, p.journal_tier, p.license AS p_lic, p.pub_types, p.journal,
             p.authority_tier, p.authority_score
      FROM pmc_fulltext f
      LEFT JOIN papers p ON p.pmcid = f.pmcid
      WHERE f.sections IS NOT NULL AND f.sections != '' AND f.sections != '[]'
      LIMIT ?"""
    for r in con.execute(q, (limit,)):
        try:
            sections = json.loads(r["sections"]) if r["sections"] else []
        except json.JSONDecodeError:
            sections = []
        yield {
            "pmcid": r["pmcid"], "pmid": r["pmid"], "doi": r["doi"],
            "title": r["title"], "abstract": r["abstract"] or "",
            "sections": sections,
            "license": (r["f_lic"] or r["p_lic"] or "").lower(),
            "evidence_level": r["evidence_level"] or "",
            "journal": r["journal"] or "",
            "authority_tier": r["authority_tier"] or "",
            "authority_score": r["authority_score"] or 0,
            "paper_cebm_level": r["evidence_level"] or "",
            "paper_batch_id": batch_id,
        }
    con.close()


def papers_from_json(path: str, batch_id: str, limit: int = 0):
    p = Path(path)
    files = sorted(p.glob("*.json*")) if p.is_dir() else [p]
    for fp in files:
        txt = fp.read_text(encoding="utf-8").strip()
        docs = []
        if fp.suffix == ".jsonl" or "\n{" in txt:
            for line in txt.splitlines():
                line = line.strip()
                if line:
                    docs.append(json.loads(line))
        else:
            obj = json.loads(txt)
            docs = obj if isinstance(obj, list) else [obj]
        count = 0
        for d in docs:
            d.setdefault("paper_batch_id", batch_id)
            yield d
            count += 1
            if limit > 0 and count >= limit:
                return


def paper_source_id(paper: dict) -> str:
    """Return the same canonical source ID used by segment_paper()."""
    if paper.get("source_id"):
        return str(paper["source_id"])
    for key, prefix in (("pmcid", "pmc:"), ("pmid", "pmid:"),
                        ("doi", "doi:"), ("nct_id", "nct:")):
        if paper.get(key):
            return f"{prefix}{paper[key]}"
    return "unknown:0"


def load_processed(path: Path) -> set[str]:
    """Load successful paper checkpoints; malformed trailing lines are ignored."""
    processed = set()
    if not path.exists():
        return processed
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if item.get("status") == "completed" and item.get("source_id"):
            processed.add(item["source_id"])
    return processed


def extract_one_paper(paper: dict, schema, llm, run_id: str):
    """Extract one paper without writing shared output files."""
    paper_stats: dict = {}
    rejects: list = []
    warnings: list = []
    started = time.monotonic()
    edges = extract_paper(paper, schema, llm, run_id, paper_stats, rejects, warnings)
    edges = batch_enrich(
        edges,
        paper.get("authority_tier", "") or "",
        paper.get("authority_score", 0) or 0,
        paper.get("paper_cebm_level", "") or paper.get("evidence_level", "") or "",
    )
    had_llm_error = any(r.get("reject_reason") == "llm_error" for r in rejects)
    if had_llm_error:
        edges = []
    return paper, edges, rejects, warnings, paper_stats, had_llm_error, time.monotonic() - started


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reproducibility_metadata(input_manifest: Path) -> dict:
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT.parent,
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        commit = "unknown"
    dependencies = {}
    for package in ("openai", "PyYAML", "zhconv", "tqdm"):
        try:
            dependencies[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            dependencies[package] = "not-installed"
    schema_hashes = {
        path.name: _sha256_file(path) for path in sorted((ROOT / "schema").glob("*.yaml"))
    }
    return {
        "git_commit": commit,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "dependencies": dependencies,
        "prompt_sha256": _sha256_file(ROOT / "src" / "prompts.py"),
        "schema_sha256": schema_hashes,
        "input_manifest": str(input_manifest),
        "input_manifest_sha256": _sha256_file(input_manifest),
    }


def main():
    """Run extraction pipeline."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", choices=["db", "json"], required=True)
    ap.add_argument("--db", default=str(ROOT / "data" / "papers.db"),
                    help="med-paper-collector 的 SQLite 路径")
    ap.add_argument("--input", help="--from json 时的文件/目录")
    ap.add_argument("--out", default=str(ROOT / "out" / "candidate_kg" / "paper_triples.jsonl"))
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--batch-id", default="batch:local:00000000:manual")
    ap.add_argument("--mock", action="store_true", help="离线确定性抽取（无需 API key，仅冒烟）")
    ap.add_argument("--no-resume", action="store_true", help="不续跑：即使输出文件已存在也重新抽取全部")
    ap.add_argument("--workers", type=int, default=int(os.environ.get("EXTRACTION_WORKERS", "2")),
                    help="并发处理论文数；建议 2-3，默认 1")
    args = ap.parse_args()

    schema = Schema()
    llm = create_extraction_client(mock=args.mock)
    run_id = new_run_id()
    _t0 = time.time()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.src == "db":
        papers = papers_from_db(args.db, args.limit, args.batch_id)
    else:
        if not args.input:
            ap.error("--from json 需要 --input")
        papers = papers_from_json(args.input, args.batch_id, args.limit)

    stats: dict = {}
    accepted_by_model: Counter = Counter()
    paper_durations: list[float] = []
    n_papers = n_edges = n_rejects = n_warnings = 0
    all_rejects: list = []
    all_warnings: list = []
    rejects_path = out_path.parent / (out_path.stem + "_rejects.jsonl")
    warns_path = out_path.parent / (out_path.stem + "_warnings.jsonl")
    processed_path = out_path.parent / (out_path.stem + "_processed.jsonl")

    # Count papers for progress display
    if args.src == "json" and args.input:
        inp = Path(args.input)
        total = len(sorted(inp.glob("*.json*"))) if inp.is_dir() else 1
    else:
        total = args.limit

    # Resume: read existing output to skip already-processed papers
    seen_ids = set()
    errored_ids = set()
    if not args.no_resume:
        seen_ids.update(load_processed(processed_path))
    if not args.no_resume and out_path.exists():
        try:
            for line in out_path.read_text(encoding="utf-8").strip().splitlines():
                if not line.strip():
                    continue
                obj = json.loads(line)
                sid = obj.get("evidence_source_id") or obj.get("provenance", {}).get("source_id", "")
                if sid:
                    seen_ids.add(sid)
        except Exception:
            pass
        if rejects_path.exists():
            try:
                for line in rejects_path.read_text(encoding="utf-8").strip().splitlines():
                    if not line.strip():
                        continue
                    rj = json.loads(line)
                    if rj.get("reject_reason") == "llm_error":
                        sid = rj.get("source_id", "")
                        if sid:
                            errored_ids.add(sid)
            except Exception:
                pass

    force_retry = seen_ids & errored_ids
    if force_retry:
        seen_ids -= force_retry
        print(f"断点续跑: {len(force_retry)} 篇之前 LLM 出错，将重新处理",
              file=sys.stderr, flush=True)

    if seen_ids:
        print(f"断点续跑: 跳过已处理的 {len(seen_ids)} 篇论文", file=sys.stderr, flush=True)

    pending_papers = [paper for paper in papers if paper_source_id(paper) not in seen_ids]
    manifest_path = out_path.parent / "input_manifest.json"
    manifest_path.write_text(json.dumps({
        "batch_id": args.batch_id,
        "papers": [{
            "source_id": paper_source_id(p),
            "title": p.get("title", ""),
            "pmid": p.get("pmid", ""),
            "pmcid": p.get("pmcid", ""),
            "doi": p.get("doi", ""),
        } for p in pending_papers],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    total = len(pending_papers) + len(seen_ids)
    remaining = len(pending_papers)
    print()
    print(f"加载 {total} 篇论文，待抽取 {remaining} 篇，开始…", file=sys.stderr, flush=True)

    open_mode = "a" if (out_path.exists() and not args.no_resume) else "w"
    if open_mode == "w":
        for artifact in (rejects_path, warns_path, processed_path):
            if artifact.exists():
                artifact.unlink()

    if HAS_TQDM:
        pbar = tqdm(
            total=total,
            desc="抽取中",
            unit="篇",
            bar_format="{desc:35.35}{percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]",
            file=sys.stderr,
        )
    else:
        pbar = None

    with out_path.open(open_mode, encoding="utf-8") as fout:
        pi = 0
        workers = max(1, min(args.workers, 8))
        if workers == 1:
            results = (extract_one_paper(p, schema, llm, run_id) for p in pending_papers)
            executor = None
        else:
            executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="paper-extract")
            results = executor.map(lambda p: extract_one_paper(p, schema, llm, run_id), pending_papers)
        for paper, edges, paper_rejects, paper_warns, paper_stats, paper_had_llm_error, paper_elapsed in results:
            sid = paper_source_id(paper)

            n_papers += 1
            paper_durations.append(paper_elapsed)
            pi += 1
            sid = paper_source_id(paper)
            if sid:
                sid_short = sid[:15]
            else:
                sid_short = "?"

            raw_title = (paper.get("title") or "Unknown").strip()
            max_title_len = 30
            if len(raw_title) > max_title_len:
                cut = max_title_len - 3
                space_idx = raw_title.rfind(" ", 0, cut + 3)
                title = raw_title[:space_idx if space_idx > max_title_len - 8 else cut].rstrip(" ,;:.") + "..."
            else:
                title = raw_title

            if pbar:
                pbar.set_description_str(f"{sid_short} | {title}", refresh=True)

            for key, value in paper_stats.items():
                if isinstance(value, (int, float)):
                    stats[key] = stats.get(key, 0) + value

            if paper_rejects:
                n_rejects += len(paper_rejects)
                all_rejects.extend(paper_rejects)
                with rejects_path.open("a", encoding="utf-8") as fr:
                    for rj in paper_rejects:
                        fr.write(json.dumps(rj, ensure_ascii=False) + "\n")

            if paper_warns:
                n_warnings += len(paper_warns)
                all_warnings.extend(paper_warns)
                with warns_path.open("a", encoding="utf-8") as fw:
                    for w in paper_warns:
                        fw.write(json.dumps(w, ensure_ascii=False) + "\n")

            if paper_had_llm_error:
                # Do not persist a partial paper: retrying it would otherwise
                # append duplicate same-paper evidence on the next run.
                if edges:
                    print(f"      RETRY {sid}: LLM error; discard {len(edges)} partial edges",
                          file=sys.stderr, flush=True)
            # Keep equal edge IDs from different papers. Cross-source merge needs
            # every independent report to compute source_count and provenance.
            for e in edges:
                fout.write(json.dumps(e, ensure_ascii=False).replace(chr(10), " ") + "\n")
                accepted_by_model[e.get("extraction_model", "unknown")] += 1
            n_edges += len(edges)
            # Commit checkpoint only after durable edge output.
            fout.flush()
            if hasattr(os, "fsync"):
                os.fsync(fout.fileno())
            if not paper_had_llm_error:
                with processed_path.open("a", encoding="utf-8") as checkpoint:
                    checkpoint.write(json.dumps({
                        "source_id": sid, "status": "completed",
                        "edge_count": len(edges), "elapsed_seconds": round(paper_elapsed, 3),
                    }, ensure_ascii=False) + "\n")
                    checkpoint.flush()
                    if hasattr(os, "fsync"):
                        os.fsync(checkpoint.fileno())

            if pbar:
                pbar.update(1)
                pbar.set_postfix({
                    "边": n_edges,
                    "接受": stats.get("accepted", 0),
                    "拒": n_rejects,
                    "警": n_warnings,
                    "错": stats.get("llm_error", 0),
                }, refresh=True)
            else:
                suffix = "" if len(edges) > 0 else " (论文无有效三元组)"
                print(f"[{pi}/{remaining}] {title} -> {len(edges)} 边 (累计 {n_edges}){suffix}",
                      file=sys.stderr, flush=True)

            if n_papers % 20 == 0 and not pbar:
                print(f"  ...{n_papers} 篇 / {n_edges} 边（accepted={stats.get('accepted',0)}）",
                      file=sys.stderr)
        if executor is not None:
            executor.shutdown(wait=True)

    if pbar:
        pbar.close()

    reject_reasons = Counter(r["reject_reason"] for r in all_rejects)
    sorted_durations = sorted(paper_durations)
    p95_index = max(0, int(len(sorted_durations) * 0.95) - 1)

    report = {
        "run_id": run_id,
        "papers": n_papers,
        "edges_written": n_edges,
        "rejects_total": n_rejects,
        "warnings_total": n_warnings,
        "reject_reasons": dict(reject_reasons),
        "elapsed": _fmt_elapsed(time.time() - _t0),
        "schema_version": schema.schema_version,
        "checkpoint_file": str(processed_path),
        "extraction_models": getattr(llm, "model_chain", [getattr(llm, "model", "mock")]),
        "model_usage": getattr(llm, "usage_counts", {}),
        "accepted_edges_by_model": dict(accepted_by_model),
        "model_metrics": (llm.metrics_snapshot() if hasattr(llm, "metrics_snapshot")
                          else {"model": getattr(llm, "model", "mock"),
                                "metrics": getattr(llm, "metrics", {})}),
        "workers": max(1, min(args.workers, 8)),
        "paper_latency_seconds": {
            "avg": round(sum(paper_durations) / len(paper_durations), 3) if paper_durations else 0,
            "p95": round(sorted_durations[p95_index], 3) if sorted_durations else 0,
            "max": round(max(paper_durations), 3) if paper_durations else 0,
        },
        "reproducibility": reproducibility_metadata(manifest_path),
        "reject_stats": stats,
    }
    rep_path = out_path.parent / "extraction_report.json"
    rep_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))

    print(f"\n{'='*55}")
    print(f"√ 写出 {n_edges} 条候选边 -> {out_path}")
    print(f"x 拒绝 {n_rejects} 条三元组 -> {rejects_path}")
    if n_warnings:
        print(f"! 软警告 {n_warnings} 条 (三元组已接受，需审计) -> {warns_path}")
    if reject_reasons:
        print(f"\n  拒因分布:")
        for reason, count in reject_reasons.most_common():
            pct = count / n_rejects * 100 if n_rejects else 0
            print(f"    {reason}: {count} 条 ({pct:.1f}%)")
    print(f"  报告 -> {rep_path}")


if __name__ == "__main__":
    main()
