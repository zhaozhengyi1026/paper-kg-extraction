# -*- coding: utf-8 -*-
"""候选 JSONL 预检（验收闸）——学生提交前必须全绿。

逐条检查：
  A. 必填字段齐全（medkg 扁平字段 + provenance 块）
  B. ID 自洽：head_id==node_id(head,head_type)，tail_id 同理，edge_id==edge_id(rel,hid,tid)
  C. schema 合规：relation/head_type/tail_type/闭类（复用 schema_loader.validate）
  D. 溯源自洽：provenance 有 source_id/evidence_chunk_id/chapter_path/char_start<char_end；
     provenance.evidence_sentence == 顶层 evidence_sentence；非空
  E. 元数据：0<confidence<=1；evidence_tier∈{A,B,C,D}；license 非空

用法：python src/validate_output.py out/candidate_kg/paper_triples.jsonl
最终权威校验仍以 `medkg build-graph`（Validator+license_gate+质量闸）为准，本预检只是提前拦低级错误。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ids import edge_id, node_id, normalize_name
from schema_loader import Schema

_REQUIRED_TOP = ["edge_id", "head_id", "tail_id", "head", "head_type", "tail", "tail_type",
                 "relation", "evidence_sentence", "evidence_chunk_id", "evidence_source_id",
                 "chapter_path", "license", "evidence_tier", "extraction_method",
                 "kg_version", "status", "confidence", "domain", "provenance"]
_REQUIRED_PROV = ["source_id", "section_index", "section_type", "chapter_path",
                  "evidence_chunk_id", "char_start", "char_end", "evidence_sentence",
                  "evidence_tier", "extraction_run_id"]
_TIERS = {"A", "B", "C", "D"}


def check_edge(e: dict, schema: Schema) -> list[str]:
    errs: list[str] = []
    # A
    for k in _REQUIRED_TOP:
        if k not in e or e[k] in (None, ""):
            if k == "chapter_path" and isinstance(e.get(k), list):
                continue  # 允许空 list（如 Abstract 单节）
            errs.append(f"缺字段 {k}")
    if errs:
        return errs
    # B: ID 自洽 — 使用与 _make_edge 相同的规范化逻辑
    from extract import _canonicalize_entity_name
    canon_head = _canonicalize_entity_name(e["head"], e["head_type"])
    canon_tail = _canonicalize_entity_name(e["tail"], e["tail_type"])
    hid = node_id(canon_head, e["head_type"])
    tid = node_id(canon_tail, e["tail_type"])
    if e["head_id"] != hid:
        errs.append(f"head_id 不符（应 {hid}）")
    if e["tail_id"] != tid:
        errs.append(f"tail_id 不符（应 {tid}）")
    if e["edge_id"] != edge_id(e["relation"], hid, tid):
        errs.append("edge_id 不符")
    # C
    ok, why = schema.validate(e)
    if not ok:
        errs.append(f"schema：{why}")
    # D
    prov = e.get("provenance") or {}
    for k in _REQUIRED_PROV:
        if k not in prov:
            errs.append(f"provenance 缺 {k}")
    cs, ce = prov.get("char_start"), prov.get("char_end")
    if not (isinstance(cs, int) and isinstance(ce, int) and 0 <= cs < ce):
        errs.append("char_start/char_end 非法")
    if prov.get("evidence_sentence") != e["evidence_sentence"]:
        errs.append("provenance.evidence_sentence 与顶层不一致")
    # E
    conf = e.get("confidence", 0)
    if not (isinstance(conf, (int, float)) and 0 < conf <= 1):
        errs.append(f"confidence 越界 {conf}")
    if e.get("evidence_tier") not in _TIERS:
        errs.append(f"evidence_tier 非法 {e.get('evidence_tier')}")
    return errs


def validate_file(path: str | Path) -> dict:
    schema = Schema()
    total, bad = 0, 0
    reasons: dict[str, int] = {}
    samples: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                reasons["malformed_json"] = reasons.get("malformed_json", 0) + 1
                continue
            errs = check_edge(e, schema)
            if errs:
                bad += 1
                for er in errs:
                    key = er.split("（")[0].split("：")[0]
                    reasons[key] = reasons.get(key, 0) + 1
                if len(samples) < 15:
                    samples.append({"line": lineno, "edge_id": e.get("edge_id"), "errors": errs})
    return {"file": str(path), "total": total, "passed": total - bad, "failed": bad,
            "pass_rate": round((total - bad) / total, 4) if total else 0.0,
            "reasons": dict(sorted(reasons.items(), key=lambda x: -x[1])), "samples": samples}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法：python src/validate_output.py <paper_triples.jsonl>")
        sys.exit(2)
    rep = validate_file(sys.argv[1])
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    sys.exit(0 if rep["failed"] == 0 else 1)
