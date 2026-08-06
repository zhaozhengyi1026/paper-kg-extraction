# -*- coding: utf-8 -*-
"""细粒度溯源三闸（PROVENANCE_SPEC §4）+ char-span 定位。

复用 medkg 反幻觉思路：证据句必须是单元文本的**连续子串**，且 [char_start,char_end) 精确对齐。
所有比较都在 segment.clean_text 归一后的文本上进行（单元文本在切段时已归一）。

v5.1 改进:
  - NBSP (\\xa0) 替换为普通空格
  - 截断括号自动扩展: evidence "(n=5" → "(n=5)"
"""
from __future__ import annotations

import re

from segment import clean_text

MAX_EVIDENCE_CHARS = 320
MIN_EVIDENCE_CHARS = 6

_QUOTE_STRIP = " \t\n\r\"'""''《》()（）[]【】"


def find_span(unit_text: str, evidence: str) -> tuple[int, int] | None:
    if not unit_text or not evidence:
        return None
    ev = clean_text(evidence).strip(_QUOTE_STRIP)
    if not ev:
        return None
    i = unit_text.find(ev)
    if i >= 0:
        return (i, i + len(ev))
    core = re.sub(r"\s+", "", ev)
    if len(core) < MIN_EVIDENCE_CHARS:
        return None
    pat = re.compile(r"\s*".join(re.escape(c) for c in core))
    m = pat.search(unit_text)
    if m:
        return (m.start(), m.end())
    # Models frequently normalize typographic quotes/dashes. Match those
    # variants while returning the exact original source span.
    pieces = []
    for ch in ev:
        if ch.isspace():
            pieces.append(r"\s+")
        elif ch in "'’‘`":
            pieces.append(r"['’‘`]")
        elif ch in '“”\"':
            pieces.append(r'[“”\"]')
        elif ch in "‐‑‒–—−-":
            pieces.append(r"[‐‑‒–—−-]")
        else:
            pieces.append(re.escape(ch))
    try:
        flexible = re.search("".join(pieces), unit_text, re.IGNORECASE)
    except re.error:
        flexible = None
    if flexible:
        return flexible.start(), flexible.end()
    return None


def expand_evidence_context(unit_text: str, char_start: int, char_end: int,
                            max_chars: int = MAX_EVIDENCE_CHARS) -> list[dict]:
    """Return exact-source sentence windows around an existing evidence span.

    Candidates grow from the containing sentence to one adjacent sentence on
    either side. Callers can select the shortest window containing both
    entities; offsets always refer to ``unit_text``.
    """
    if not (0 <= char_start < char_end <= len(unit_text)):
        return []
    boundaries = [0]
    boundaries.extend(m.end() for m in re.finditer(r"[.!?。！？]\s+", unit_text))
    boundaries.append(len(unit_text))
    boundaries = sorted(set(boundaries))
    left_index = max(i for i, pos in enumerate(boundaries) if pos <= char_start)
    right_index = min(i for i, pos in enumerate(boundaries) if pos >= char_end)
    windows = []
    for grow_left, grow_right in ((0, 0), (1, 0), (0, 1), (1, 1)):
        li = max(0, left_index - grow_left)
        ri = min(len(boundaries) - 1, right_index + grow_right)
        start, end = boundaries[li], boundaries[ri]
        while start < end and unit_text[start].isspace():
            start += 1
        while end > start and unit_text[end - 1].isspace():
            end -= 1
        if MIN_EVIDENCE_CHARS <= end - start <= max_chars:
            candidate = {"char_start": start, "char_end": end,
                         "evidence_sentence": unit_text[start:end]}
            if candidate not in windows:
                windows.append(candidate)
    return sorted(windows, key=lambda item: item["char_end"] - item["char_start"])


def verify_evidence(unit_text: str, evidence_sentence: str,
                    char_start: int | None = None, char_end: int | None = None) -> dict:
    if not evidence_sentence or not evidence_sentence.strip():
        return {"ok": False, "reason": "empty_evidence"}

    if char_start is not None and char_end is not None:
        if not (0 <= char_start < char_end <= len(unit_text)):
            return {"ok": False, "reason": "char_span_out_of_range"}
        sliced = clean_text(unit_text[char_start:char_end]).strip(_QUOTE_STRIP)
        want = clean_text(evidence_sentence).strip(_QUOTE_STRIP)
        if sliced != want:
            span = find_span(unit_text, evidence_sentence)
            if span is None:
                return {"ok": False, "reason": "char_span_mismatch"}
            char_start, char_end = span
    else:
        span = find_span(unit_text, evidence_sentence)
        if span is None:
            return {"ok": False, "reason": "evidence_not_in_source"}
        char_start, char_end = span

    width = char_end - char_start
    if width < MIN_EVIDENCE_CHARS:
        return {"ok": False, "reason": "evidence_too_short"}
    if width > MAX_EVIDENCE_CHARS:
        return {"ok": False, "reason": "evidence_too_wide"}

    # 保留 unit 原始子串。显示时替换 NBSP 但继续使用原始偏移会让
    # 后续 char-span 闸门把有效证据误判为不一致。
    evidence_text = unit_text[char_start:char_end]
    # 扩展截断括号
    evidence_text, char_end = _extend_incomplete_bracket(
        unit_text, char_start, char_end, evidence_text
    )

    return {
        "ok": True, "reason": "",
        "char_start": char_start, "char_end": char_end,
        "evidence_sentence": evidence_text,
    }


def _extend_incomplete_bracket(
    unit_text: str, cs: int, ce: int, evidence: str
) -> tuple[str, int]:
    """如果 evidence 以不完整括号结尾（如 '(n=5'），向后扩展到闭合。"""
    open_parens = evidence.count("(") - evidence.count(")")
    open_brackets = evidence.count("[") - evidence.count("]")
    if open_parens <= 0 and open_brackets <= 0:
        return evidence, ce

    for i in range(ce, min(len(unit_text), ce + 80)):
        ch = unit_text[i]
        if ch == ")" and open_parens > 0:
            open_parens -= 1
        elif ch == "]" and open_brackets > 0:
            open_brackets -= 1
        elif ch in ("(", "["):
            break

        if open_parens == 0 and open_brackets == 0:
            extended = unit_text[cs : i + 1]
            if len(extended) <= 400:
                return extended, i + 1
            break
    return evidence, ce
