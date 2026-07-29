# -*- coding: utf-8 -*-
"""离线冒烟：不需 API key。跑 segment + mock 抽取 + 三闸 + schema 预检 + 输出校验。

运行：  cd paper-kg-extraction && python -m pytest tests -q
或：    python tests/test_smoke.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from extract import extract_paper, new_run_id  # noqa: E402
from ids import edge_id, node_id, normalize_name  # noqa: E402
from llm_client import LLMClient  # noqa: E402
from provenance import verify_evidence  # noqa: E402
from schema_loader import Schema  # noqa: E402
from segment import segment_paper  # noqa: E402
from validate_output import check_edge  # noqa: E402

SAMPLE = json.loads((ROOT / "examples" / "sample_paper.json").read_text(encoding="utf-8"))


def test_schema_loads():
    s = Schema()
    assert s.schema_version and len(s.entity_types) > 40 and len(s.relation_types) > 60
    assert "HERB_CONTAINS_INGREDIENT" in s.relation_types
    assert "CORRESPONDS_TO_WM_DISEASE" in s.relation_types


def test_normalize_and_ids_stable():
    # 繁简/空格归一后 node_id 必须一致（融合前提）
    assert normalize_name("黃芪") == normalize_name("黄 芪") == "黄芪"
    assert node_id("黃芪", "Herb") == node_id("黄芪", "Herb")
    # 已知固定值（回归钉子；与 medkg node_id 逐字节一致，改了归一/哈希会炸）
    assert node_id("黄芪", "Herb") == "a332c06019b5b31b"


def test_segment_provenance_fields():
    units = segment_paper(SAMPLE)
    assert units, "应切出至少一个语义单元"
    u = units[0]
    for k in ("unit_id", "source_id", "section_type", "chapter_path", "text"):
        assert k in u
    assert u["source_id"] == "pmc:PMC10123456"


def test_evidence_gate_rejects_hallucination():
    unit_text = "Astragaloside IV inhibited TGF-β1 in renal cells."
    assert verify_evidence(unit_text, "Astragaloside IV inhibited TGF-β1")["ok"]
    # 不在原文的证据必须被拒
    assert not verify_evidence(unit_text, "Astragalus cures diabetes completely")["ok"]


def test_end_to_end_mock():
    schema = Schema()
    llm = LLMClient(mock=True)
    edges = extract_paper(SAMPLE, schema, llm, new_run_id(), {})
    assert len(edges) >= 2
    for e in edges:
        # ID 自洽
        assert e["head_id"] == node_id(e["head"], e["head_type"])
        assert e["edge_id"] == edge_id(e["relation"], e["head_id"], e["tail_id"])
        # 溯源块完整 + 证据是原文子串（char span 命中）
        prov = e["provenance"]
        assert prov["char_start"] < prov["char_end"]
        assert prov["evidence_sentence"] == e["evidence_sentence"]
        # 输出校验全绿
        assert check_edge(e, schema) == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✓ {name}")
    print("冒烟通过。")
