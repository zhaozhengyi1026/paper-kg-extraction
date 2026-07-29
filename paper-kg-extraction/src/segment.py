# -*- coding: utf-8 -*-
"""论文 → 章节 → 语义单元（承载 chapter_path 段落级溯源）。

输入：一篇论文的结构化 dict（med-paper-collector 的 pmc_fulltext 行 / JATS 解析产物）：
    {"source_id": "pmc:PMC123", "title": ..., "abstract": ...,
     "sections": [{"title": "Results", "text": "para1\npara2..."}], "license": "cc-by", ...}
输出：语义单元列表，每个单元带 unit_id / chapter_path / section_type / char 定位所需的归一 text。

设计对齐 medkg structure/segment.py：单元是"节内相邻段落聚成的语义块"，上限 max_unit_chars，
保单元内跨句关系又不淹没抽取召回。char_span 一律在**单元归一 text**上计算（见 provenance.py）。
"""
from __future__ import annotations

import re

from ids import unit_id

MAX_UNIT_CHARS = 1200          # 单元上限（论文段落偏长，略高于教科书）
MIN_UNIT_CHARS = 40            # 过短单元并入相邻

_SECTION_TYPES = [
    ("abstract", r"abstract|summary|摘要"),
    ("introduction", r"introduction|背景|引言"),
    ("background", r"background"),
    ("methods", r"method|material|patients and|方法|材料"),
    ("results", r"result|finding|结果"),
    ("discussion", r"discussion|讨论"),
    ("conclusion", r"conclusion|结论"),
    ("case_report", r"case report|case presentation|病例|个案"),
    ("limitations", r"limitation|局限"),
    ("funding", r"funding|acknowledg|conflict|资助|致谢|利益"),
]

_WS = re.compile(r"[ \t]{2,}")
_NL = re.compile(r"\n{2,}")


def clean_text(t: str) -> str:
    """论文正文轻量归一：折叠多余空白/空行。char_span 基于本函数输出计算。"""
    if not t:
        return ""
    t = t.replace("\r", "\n")
    t = _WS.sub(" ", t)
    t = _NL.sub("\n", t)
    return t.strip()


def section_type(title: str) -> str:
    low = (title or "").strip().lower()
    for stype, pat in _SECTION_TYPES:
        if re.search(pat, low):
            return stype
    return "other"


def _split_paragraphs(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"\n", text) if p.strip()]
    return parts


def _pack(paragraphs: list[str]) -> list[tuple[str, list[int]]]:
    """相邻段落聚成单元；返回 [(unit_text, [para_index...])]。"""
    units, buf, idxs, size = [], [], [], 0
    for i, p in enumerate(paragraphs):
        if buf and size + len(p) > MAX_UNIT_CHARS:
            units.append(("\n".join(buf), list(idxs)))
            buf, idxs, size = [], [], 0
        buf.append(p)
        idxs.append(i)
        size += len(p) + 1
    if buf:
        units.append(("\n".join(buf), list(idxs)))
    # 合并过短单元到前一个
    merged: list[tuple[str, list[int]]] = []
    for txt, idxs in units:
        if merged and len(txt) < MIN_UNIT_CHARS:
            ptxt, pidx = merged[-1]
            merged[-1] = (ptxt + "\n" + txt, pidx + idxs)
        else:
            merged.append((txt, idxs))
    return merged


def segment_paper(paper: dict) -> list[dict]:
    """把一篇论文切成语义单元列表。"""
    source_id = paper.get("source_id") or _derive_source_id(paper)
    batch_id = paper.get("paper_batch_id", "")
    evidence_level = paper.get("evidence_level", "")
    license_ = paper.get("license", "")

    # 组装 [(section_index, title_path, section_type, text)]
    sections: list[tuple[int, list[str], str, str]] = []
    if paper.get("abstract"):
        sections.append((0, ["Abstract"], "abstract", paper["abstract"]))
    raw_secs = paper.get("sections") or []
    for si, sec in enumerate(raw_secs, start=1):
        title = (sec.get("title") or "").strip() or f"Section {si}"
        sections.append((si, [title], section_type(title), sec.get("text", "")))

    units: list[dict] = []
    ordinal = 0
    for section_index, title_path, stype, text in sections:
        text = clean_text(text)
        if not text:
            continue
        paras = _split_paragraphs(text)
        for unit_text, para_idxs in _pack(paras):
            unit_text = clean_text(unit_text)
            if not unit_text:
                continue
            uid = f"{source_id}#sec{section_index}#u{ordinal:02d}"
            units.append({
                "unit_id": uid,
                "unit_hash": unit_id(source_id, ordinal, unit_text[:64]),
                "source_id": source_id,
                "paper_batch_id": batch_id,
                "section_index": section_index,
                "section_type": stype,
                "chapter_path": title_path,
                "para_index_range": [para_idxs[0], para_idxs[-1]] if para_idxs else [0, 0],
                "text": unit_text,
                "evidence_level": evidence_level,
                "license": license_,
                "usage_scope": paper.get("usage_scope", "commercial"),
                "allowed_use": paper.get("allowed_use", "kg"),
            })
            ordinal += 1
    return units


def _derive_source_id(paper: dict) -> str:
    for key, pfx in (("pmcid", "pmc:"), ("pmid", "pmid:"), ("doi", "doi:"), ("nct_id", "nct:")):
        v = paper.get(key)
        if v:
            return f"{pfx}{v}"
    return "unknown:0"
