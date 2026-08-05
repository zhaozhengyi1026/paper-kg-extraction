# -*- coding: utf-8 -*-
"""论文 → 候选三元组（medkg 可融合格式 + 细粒度溯源）。

v5.1 改进:
  - v4 修复 (code review #1-#15)
  - v5 实体名规范化: 缩写展开 + 疾病阶段前缀剥离
  - v5.1 NLI 语义校验闸门 (Gate 3.5): 判断 evidence 是否蕴含三元组声明
  - v5.1 成分类型强制覆盖: 多糖/皂苷等不会被标为 Herb
  - v5.1 同论文实体类型一致性: 同一实体全文类型统一
"""
from __future__ import annotations

import datetime as _dt
import re as _re
import sys
import logging

from ids import edge_id, node_id
from prompts import build_prompt
from provenance import verify_evidence
from segment import segment_paper
from llm_client import create_nli_client
from nli import (
    needs_nli,
    build_hypothesis,
    batch_check as nli_batch_check,
    skip_nli,
)

_log = logging.getLogger(__name__)

_BASE = {"teacher_llm": 0.7, "external_kg": 0.6, "seed": 0.9}
_TIER_FACTOR = {"A": 1.0, "B": 0.95, "C": 0.9, "D": 0.8, "": 0.85}
_LIT_TIER = {"1a": "A", "1b": "A", "2a": "B", "2b": "B", "3a": "B",
             "3b": "C", "4": "C", "5": "D"}

# NLI neutral 降级系数
_NLI_NEUTRAL_FACTOR = 0.6

# ── 结构/事实关系：置信度加权 ──
# 这类关系的证据通常是客观陈述（"A extracted from B"、"X is a component of Y"），
# 默认 base 0.7 偏低，加 boost 提升到合理水平
_RELATION_CONFIDENCE_BOOST = {
    # 成分关系：化学分析结果，不应低于 0.85
    "HERB_CONTAINS_INGREDIENT": 0.25,
    # 通路参与：生信/实验已验证，不应低于 0.80
    "PARTICIPATES_IN_PATHWAY": 0.15,
    "GENE_INTERACTS_GENE": 0.10,
    # 表型归因
    "HAS_PHENOTYPE": 0.10,
}

# ── HDI/COMPLEMENTS 判定 ──
# 如果 relation=HDI 但 evidence 不含 PK 交互词（仅含临床疗效词），
# 应重新分类为 COMPLEMENTS_WM_THERAPY
_HDI_PK_PATTERNS = [
    r"\bbioavailability\b", r"\bpharmacokinetic", r"\bdrug\s+metaboli[sz]",
    r"\bmetaboli[sz](?:e|ed|es|ing)\s+(?:of\s+)?(?:the\s+)?(?:co[- ]?administered\s+)?drug\b",
    r"\b(cyp\w*|cytochrome\s+p450)\b", r"\bauc\b", r"\bcmax\b", r"\btmax\b",
    r"\b(clearance|half[- ]life|absorption)\s+of\b",
    r"\b(plasma|serum|blood|intracellular|hepatocyte)\s+concentration\b",
    r"\bconcentration\s+of\s+[A-Za-z0-9-]+",
    r"\b(p-gp|p-glycoprotein|drug\s+transporter)\b",
]
_HDI_PK_RE = _re.compile("|".join(_HDI_PK_PATTERNS), _re.IGNORECASE)

# ── 肯定关系 ──
_AFFIRMATIVE_RELATIONS = {
    "TREATS", "WM_TREATS", "INDICATED_FOR", "WM_INDICATED_FOR",
    "POTENTIATES", "CAUSES", "LEADS_TO",
    "RECOMMENDED_FORMULA", "RECOMMENDED_BY_GUIDELINE",
    "TREATED_BY_PRINCIPLE", "REALIZED_BY", "CORRESPONDS_TO",
    "HERB_CONTAINS_INGREDIENT", "CONTAINS", "HAS_FUNCTION",
    "GENE_ASSOCIATED_WITH_DISEASE", "MOLECULARLY_TARGETS",
    "FORMULA_ACTS_VIA", "COMPLEMENTS_WM_THERAPY", "INTEGRATIVE_TREATS",
    "HAS_PHENOTYPE", "DDI", "HDI", "HHI", "INTERACTS_WITH",
}

_RELATION_POLARITY_CUES = {
    "TREATS": r"treat|therapeutic|efficacy|effective|effect|improv|ameliorat|alleviat|benefit|prevent|protect|difference|response",
    "WM_TREATS": r"treat|therapeutic|efficacy|effective|effect|improv|ameliorat|alleviat|benefit|prevent|protect|difference|response",
    "INTEGRATIVE_TREATS": r"treat|therapeutic|efficacy|effective|effect|improv|ameliorat|alleviat|benefit|difference|response",
    "COMPLEMENTS_WM_THERAPY": r"enhanc|additive|synerg|benefit|improv|effective|efficacy|protect",
    "MOLECULARLY_TARGETS": r"target|bind|interact|inhibit|suppress|regulat|activat|phosphorylat|expression",
    "FORMULA_ACTS_VIA": r"mechanism|via|pathway|target|inhibit|suppress|regulat|activat",
    "GENE_ASSOCIATED_WITH_DISEASE": r"associat|correlat|related|pathogenesis|risk|differentially\s+expressed",
    "HAS_PHENOTYPE": r"phenotype|feature|characteri[sz]|manifest|present|show|exhibit|level|expression",
    "HERB_CONTAINS_INGREDIENT": r"contain|component|constituent|ingredient|isolated|purified|derived",
    "CONTAINS": r"contain|component|constituent|ingredient",
    "HAS_FUNCTION": r"function|activity|effect|role",
    "DDI": r"interact|adverse|toxicity|pharmacokinetic|bioavailability|concentration|clearance",
    "HDI": r"interact|adverse|toxicity|pharmacokinetic|bioavailability|concentration|clearance",
    "HHI": r"interact|synerg|antagoni[sz]|compatib|combined\s+effect",
    "INTERACTS_WITH": r"interact|synerg|antagoni[sz]|compatib|combined\s+effect",
}

# ═══════════════════════════════════════════════════════════════
#  成分类型强制覆盖 — 这些词表明实体是 MolecularEntity，不是 Herb
# ═══════════════════════════════════════════════════════════════

# 匹配模式：实体名包含这些词 → 强制 MolecularEntity
_MOLECULAR_ENTITY_PATTERNS = [
    r'\bpolysaccharide\b', r'\bsaponin\b', r'\bflavonoid\b',
    r'\balkaloid\b', r'\bglycoside\b', r'\bisoflavonoid\b',
    r'\bpterocarpan\b', r'\bisoflavan\b', r'\bisoflavone\b',
    r'\bglucoside\b', r'\btannin\b', r'\bterpenoid\b',
    r'\bphenolic\b', r'\bquinone\b', r'\bcoumarin\b',
    r'\blignan\b', r'\bstilbene\b', r'\bcurcuminoid\b',
    r'\banthraquinone\b', r'\bsteroid\b',
]
_MOLECULAR_PAT_RE = _re.compile(
    '|'.join(_MOLECULAR_ENTITY_PATTERNS), _re.IGNORECASE
)


def _should_be_molecular_entity(name: str) -> bool:
    """检查实体名是否明显是化学成分（应归为 MolecularEntity）。"""
    if not name:
        return False
    return bool(_MOLECULAR_PAT_RE.search(name))


def _has_pk_evidence(evidence: str) -> bool:
    """检查 evidence 是否包含药代动力学(PK)交互证据。"""
    if not evidence:
        return False
    ev_lower = evidence.lower()
    return bool(_HDI_PK_RE.search(ev_lower))


_OUTCOME_RE = _re.compile(
    r"\b(improv\w*|reduc\w*|decreas\w*|ameliorat\w*|alleviat\w*|effective|"
    r"efficacy|therapeutic\s+effect|benefit\w*|protect\w*|prevent\w*|delay\w*|"
    r"treat(?:s|ed|ing)?|lower\w*|increas\w*)\b|治疗|改善|降低|缓解|有效|疗效",
    _re.IGNORECASE,
)
_PURPOSE_RE = _re.compile(
    r"^\s*(to\s+(study|investigate|evaluate|observe|assess|determine|explore|compare)|"
    r"we\s+(investigated|evaluated|examined|aimed\s+to)|objective\s*:|aim\s*:)",
    _re.IGNORECASE,
)
_HYPOTHESIS_RE = _re.compile(
    r"\b(we\s+(assumed|hypothesized|speculated)|it\s+was\s+hypothesized|"
    r"may\s+potentially\s+(treat|improve)|promising\s+therapeutic)\b",
    _re.IGNORECASE,
)


def _weak_relation_reason(relation: str, evidence: str) -> str:
    """Return a reason when the quote lacks a complete proposition for relation."""
    if not evidence or len(evidence.strip()) < 12:
        return "evidence fragment is too short"
    has_outcome = bool(_OUTCOME_RE.search(evidence))
    if relation in {"TREATS", "WM_TREATS", "INTEGRATIVE_TREATS"}:
        if _PURPOSE_RE.search(evidence) or _HYPOTHESIS_RE.search(evidence):
            return "study purpose or hypothesis does not establish treatment efficacy"
    if relation == "COMPLEMENTS_WM_THERAPY":
        if _re.search(r"\btreated\s+with\b.{0,100}\bor\b", evidence, _re.IGNORECASE):
            return "parallel treatments joined by 'or' do not establish combination therapy"
        combined = _re.search(
            r"\b(combin\w*|co[- ]?admin\w*|adjunct\w*|plus|together\s+with)\b",
            evidence, _re.IGNORECASE,
        )
        if not combined or not has_outcome:
            return "combination therapy requires both co-treatment and an outcome statement"
    return ""


def _mechanism_evidence_grade(evidence: str) -> str:
    """Classify target evidence without changing relation or schema."""
    low = evidence.lower()
    if _re.search(r"network pharmacology|molecular docking|predicted|prediction|screened|"
                  r"potential targets?|target network|enrichment analysis", low):
        return "predicted"
    if _re.search(r"\b(direct(?:ly)?\s+(bind|target|interact)|bind(?:s|ing)?\s+to|"
                  r"binding affinity|pull[- ]down|surface plasmon resonance|"
                  r"(?:is|was|are|were)\s+(?:a\s+|an\s+)?target\s+of|"
                  r"antibod(?:y|ies)\s+(?:against|to)|(?:selective\s+)?inhibitor\s+of)\b", low):
        return "direct"
    if _re.search(r"\b(inhibit|suppress|down[- ]?regulat|up[- ]?regulat|activat|"
                  r"knockdown|silenc|phosphorylat)\w*\b", low):
        return "regulatory"
    if _re.search(r"\b(expression|expressed|cleavage|co[- ]?express|abundance|level)\w*\b", low):
        return "indirect"
    return "indirect"


_CAUSAL_OR_PREDICTIVE_RE = _re.compile(
    r"\b(caus(?:e|ed|es|ing)|induc(?:e|ed|es|ing)|trigger(?:ed|s|ing)?|"
    r"lead(?:s|ing)?\s+to|result(?:s|ed|ing)?\s+in|predict(?:s|ed|ive|or)?|"
    r"risk\s+(?:factor|predictor)|associated\s+with\s+(?:an?\s+)?(?:increased|reduced)\s+risk)\b",
    _re.IGNORECASE,
)


def _phenotype_semantics_reason(head_type: str, evidence: str) -> str:
    """Reject causal/predictive claims that cannot truthfully mean HAS_PHENOTYPE."""
    if head_type in {"Exposure", "LabTest", "Procedure"} and _CAUSAL_OR_PREDICTIVE_RE.search(evidence):
        return "causal or predictive evidence does not establish an intrinsic phenotype"
    return ""


def _edge_evidence_score(edge: dict) -> tuple:
    """Rank duplicate same-paper edges without changing their output contract."""
    evidence = (edge.get("evidence_sentence") or "").strip()
    qualifiers = edge.get("qualifiers") or {}
    complete_sentence = int(bool(_re.search(r"[.!?。！？]$", evidence)))
    proposition = int(bool(_re.search(r"\b(?:is|are|was|were|has|have|had|"
                                      r"bind\w*|target\w*|inhibit\w*|treat\w*|"
                                      r"associat\w*|caus\w*|improv\w*)\b", evidence,
                                      _re.IGNORECASE)))
    return (float(edge.get("confidence") or 0), proposition, complete_sentence,
            len(qualifiers), min(len(evidence), 500))


_jieba = None


def _get_jieba():
    global _jieba
    if _jieba is None:
        try:
            import jieba
            _jieba = jieba
        except ImportError:
            _jieba = False
    return _jieba


def _tier_from_level(level: str) -> str:
    return _LIT_TIER.get((level or "").strip().lower(), "C")


# ═══════════════════════════════════════════════════════════════
#  实体名规范化（v5）
# ═══════════════════════════════════════════════════════════════

_DISEASE_STAGE_PREFIX = _re.compile(
    r"^(early[-\s]?(stage|onset)?\s+|advanced\s+|late[-\s]?(stage)?\s+|"
    r"early[-\s]?(and|to)\s+intermediate[-\s]?stage\s+|"
    r"intermediate[-\s]?stage\s+|"
    r"mild\s+|moderate\s+|severe\s+|"
    r"type\s+[12]\s+)",
    _re.IGNORECASE,
)


def _extract_abbreviations(paper: dict) -> dict[str, str]:
    text_parts = [(paper.get("title") or ""), (paper.get("abstract") or "")]
    text_parts.extend(section.get("text", "") for section in paper.get("sections", []))
    text = ". ".join(part for part in text_parts if part)
    abbrs: dict[str, str] = {}
    _stop = {"the", "a", "an", "of", "in", "on", "at", "with", "for", "by",
             "to", "and", "or", "was", "were", "is", "are", "effect", "effects",
             "study", "treatment", "expression", "patients", "disease", "gene",
             "protein", "level", "levels", "group", "groups", "using"}

    for m in _re.finditer(r"\(([A-Z][A-Za-z0-9-]{1,11})\)", text):
        raw_abbr = m.group(1)
        abbr = raw_abbr.upper()
        match_abbr = abbr[:-1] if raw_abbr.endswith("s") and len(abbr) > 2 else abbr
        before = text[:m.start()]
        prev_paren = max(before.rfind(")"), before.rfind(" ("))
        for sep in [". ", "; ", ": "]:
            pos = before.rfind(sep)
            if pos > prev_paren:
                prev_paren = pos
                break
        preceding = before[prev_paren + 1:].strip() if prev_paren >= 0 else before.strip()
        words = preceding.split()
        best = None
        best_n = 99
        for n in range(8, 1, -1):
            if len(words) < n:
                continue
            cand = words[-n:]
            while cand and cand[0].lower() in _stop:
                cand = cand[1:]
            if len(cand) < 2:
                continue
            first_letters = "".join(w[0] for w in cand if w[0].isalpha()).upper()
            if not first_letters or match_abbr[0] != first_letters[0]:
                continue
            if _is_abbrev_subseq(match_abbr, first_letters):
                if len(cand) < best_n:
                    best = " ".join(cand).lower()
                    best_n = len(cand)
        if best:
            abbrs[abbr] = best
        elif len(words) >= 2:
            fallback = " ".join(words[-2:]).strip(" ,.;:[]").lower()
            if all(w.lower().strip(" ,.;:[]") not in _stop for w in words[-2:]):
                abbrs[abbr] = fallback
    return abbrs


def _is_abbrev_subseq(short: str, long: str) -> bool:
    si = 0
    for c in long:
        if si < len(short) and c == short[si]:
            si += 1
    return si == len(short)


def _canonicalize_entity_name(name: str, entity_type: str,
                              paper_abbrs: dict[str, str] | None = None) -> str:
    if not name:
        return name
    if paper_abbrs:
        upper = name.strip().upper()
        if upper in paper_abbrs:
            return paper_abbrs[upper]
    if entity_type in ("Disease", "DiseaseWM"):
        stripped = _DISEASE_STAGE_PREFIX.sub("", name).strip()
        if stripped and len(stripped) >= 2:
            if paper_abbrs:
                upper = stripped.upper()
                if upper in paper_abbrs:
                    return paper_abbrs[upper]
            return stripped
    return name


# ═══════════════════════════════════════════════════════════════
#  主抽取逻辑
# ═══════════════════════════════════════════════════════════════

def extract_paper(paper: dict, schema, llm, run_id: str,
                  stats: dict | None = None,
                  reject_log: list | None = None,
                  warn_log: list | None = None) -> list[dict]:
    stats = stats if stats is not None else {}
    reject_log = reject_log if reject_log is not None else []
    warn_log = warn_log if warn_log is not None else []

    paper["_abbreviations"] = _extract_abbreviations(paper)

    units = segment_paper(paper)
    method = "mock" if getattr(llm, "mock", False) else "teacher_llm"
    tier = _tier_from_level(paper.get("evidence_level", ""))
    conf = round(_BASE.get("teacher_llm", 0.7) * _TIER_FACTOR.get(tier, 0.85), 3)

    edge_positions: dict[tuple[str, str], int] = {}
    out: list[dict] = []

    # ── NLI 客户端 + 缓存 + 类型一致性 ──
    # mock 模式下不创建 NLI 客户端（避免尝试连接 API）
    if getattr(llm, "mock", False):
        nli_llm = None
        nli_fallback_llm = None
    else:
        nli_llm = create_nli_client(fallback_llm=llm)
        nli_fallback_llm = getattr(llm, "fallback", llm)
    nli_cache: dict = {}           # (premise_hash, hyp) → label
    paper_type_map: dict = {}      # entity_name → type（同论文内统一类型）

    nunits = len(units)
    print(f"    分 {nunits} 个语义单元", file=sys.stderr, flush=True)
    for ui, unit in enumerate(units, 1):
        system, user = build_prompt(schema, unit)
        print(f"    [{ui}/{nunits}] LLM 调用中…", file=sys.stderr, flush=True)
        try:
            resp = llm.complete_json(system, user)
        except Exception as e:
            _bump(stats, "llm_error")
            unit["_error"] = str(e)
            err_msg = f"LLM 调用失败(重试后): {e}"
            print(f"    {err_msg}", file=sys.stderr, flush=True)
            reject_log.append({
                "paper_title": (paper.get("title") or "")[:120],
                "source_id": unit.get("source_id", ""),
                "chapter_path": unit.get("chapter_path", ""),
                "head": "", "head_type": "", "relation": "", "tail": "", "tail_type": "",
                "evidence_sentence": "", "qualifiers": {},
                "reject_reason": "llm_error",
                "reject_detail": err_msg,
            })
            continue

        if not isinstance(resp, dict):
            _bump(stats, "llm_bad_response")
            err_msg = f"LLM 返回非 dict 类型: {type(resp).__name__}"
            print(f"    {err_msg}", file=sys.stderr, flush=True)
            reject_log.append({
                "paper_title": (paper.get("title") or "")[:120],
                "source_id": unit.get("source_id", ""),
                "chapter_path": unit.get("chapter_path", ""),
                "head": "", "head_type": "", "relation": "", "tail": "", "tail_type": "",
                "evidence_sentence": "", "qualifiers": {},
                "reject_reason": "llm_bad_response",
                "reject_detail": err_msg,
            })
            continue

        if "triples" not in resp:
            print(f"    WARN: LLM 返回无 triples 字段 (got keys: {sorted(resp.keys())})",
                  file=sys.stderr, flush=True)
        triples_list = resp.get("triples", []) or []
        extraction_model = resp.get("_model_used", "")
        if not isinstance(triples_list, list):
            triples_list = []
        n_triples = len(triples_list)
        print(f"    [{ui}/{nunits}] LLM ✓ 返回 {n_triples} 条三元组",
              file=sys.stderr, flush=True)
        for tr in triples_list:
            if not isinstance(tr, dict):
                _bump(stats, "llm_bad_triple")
                continue
            edge = _make_edge(tr, unit, schema, run_id, method, tier, conf,
                              stats, reject_log, warn_log, paper,
                              nli_llm=nli_llm, nli_fallback=nli_fallback_llm,
                              nli_cache=nli_cache,
                              paper_type_map=paper_type_map)
            if edge is None:
                continue
            if extraction_model:
                edge["extraction_model"] = extraction_model
                edge["provenance"]["extraction_model"] = extraction_model
            # An edge ID intentionally excludes provenance. Deduplicate only
            # within this paper/source and retain the stronger evidence quote.
            dedup_key = (edge.get("evidence_source_id", ""), edge["edge_id"])
            if dedup_key in edge_positions:
                _bump(stats, "dup_in_run")
                pos = edge_positions[dedup_key]
                if _edge_evidence_score(edge) > _edge_evidence_score(out[pos]):
                    out[pos] = edge
                    _bump(stats, "dup_replaced_by_better_evidence")
                continue
            edge_positions[dedup_key] = len(out)
            out.append(edge)
    return out


# ═══════════════════════════════════════════════════════════════
#  否定检测
# ═══════════════════════════════════════════════════════════════

def _has_negation(evidence: str, relation: str,
                  head: str, tail: str) -> tuple[bool, str]:
    """Detect negation scoped to the asserted relation, not nearby tokens alone."""
    if not evidence:
        return False, ""
    if relation not in _AFFIRMATIVE_RELATIONS:
        return False, ""
    cleaned = _mask_non_negative_constructions(evidence)
    cue = _RELATION_POLARITY_CUES.get(relation, "effect|association|interaction")
    tail_pattern = _entity_scope_pattern(tail)
    cue_pattern = f"(?:{cue})\\w*"
    target = f"(?:{cue_pattern}|{tail_pattern})" if tail_pattern else cue_pattern
    patterns = [
        rf"\b(?:do|does|did|is|are|was|were|has|have|had|can|could|may|might|would)\s+not"
        rf"\s+(?:\w+[\s-]+){{0,5}}{target}\b",
        rf"\b(?:failed|fails?|unable)\s+to\s+(?:\w+[\s-]+){{0,4}}{target}\b",
        rf"\b(?:no|neither)\s+(?:significant(?:ly)?\s+)?(?:\w+[\s-]+){{0,3}}{target}\b",
        rf"\b(?:lack(?:ed|s|ing)?(?:\s+of)?|absence\s+of|without(?:\s+any)?)\s+"
        rf"(?:\w+[\s-]+){{0,3}}{target}\b",
    ]
    for pattern in patterns:
        match = _re.search(pattern, cleaned, _re.IGNORECASE)
        if match:
            return True, f"关系谓词被否定: {match.group(0)[:100]}"

    cn_target = _re.escape(tail.strip()) if tail and not tail.isascii() else ""
    cn_cues = r"治疗|改善|降低|缓解|有效|相关|关联|表达|调控|激活|抑制|含有|成分|相互作用|协同"
    cn_target = f"(?:{cn_cues}|{cn_target})" if cn_target else f"(?:{cn_cues})"
    cn_match = _re.search(
        rf"(?:不|未|无|没有|未见|缺乏|不能|无法)[^，。；;,.]{{0,12}}{cn_target}", cleaned
    )
    if cn_match:
        return True, f"关系谓词被否定: {cn_match.group(0)[:100]}"
    return False, ""


def _mask_non_negative_constructions(text: str) -> str:
    """Mask pseudo-negation and biochemical NO while preserving positions."""
    masked = text
    for pattern in (
        r"\bnot\s+only\b", r"\bbut\s+also\b", r"\bno\s+less\s+than\b",
        r"\bnot\s+limited\s+to\b",
    ):
        masked = _re.sub(pattern, lambda m: " " * len(m.group(0)), masked,
                         flags=_re.IGNORECASE)
    biochemical_context = _re.search(
        r"\b(nitric\s+oxide|iNOS|eNOS|nNOS|NO\s+(?:production|generation|release|level|synthesis)|"
        r"NO[- ]?(?:donor|releasing|mediated)|(?:produce|release|generate|synthesize)\w*\s+NO)\b",
        masked,
        _re.IGNORECASE,
    )
    if biochemical_context:
        masked = _re.sub(r"\bNO\b", "__NITRIC_OXIDE__", masked)
    return masked


def _entity_scope_pattern(entity: str) -> str:
    if not entity:
        return ""
    words = _re.findall(r"[A-Za-z0-9α-ωΑ-Ωβκ]+", entity)
    if not words:
        return ""
    full = r"[\s-]+".join(_re.escape(word) for word in words)
    last = _re.escape(words[-1])
    return full if full == last else f"(?:{full}|{last})"


# ═══════════════════════════════════════════════════════════════
#  实体词边界匹配
# ═══════════════════════════════════════════════════════════════

def _entity_mentioned(name: str, evidence: str,
                     paper_abbrs: dict[str, str] | None = None) -> bool:
    if not name or not evidence:
        return False
    name = name.strip()
    def _surface(value: str) -> str:
        value = value.casefold().replace("α", "alpha").replace("β", "beta").replace("γ", "gamma")
        value = _re.sub(r"(?<=\w)[‐‑‒–—−](?=\w)", " ", value)
        value = _re.sub(r"[^\w]+", " ", value, flags=_re.UNICODE).strip()
        words = value.split()
        if words and len(words[-1]) > 3 and words[-1].endswith("s") and not words[-1].endswith("ss"):
            words[-1] = words[-1][:-1]
        return " ".join(words)

    if name.lower() in evidence.lower():
        return True
    name_surface = _surface(name)
    evidence_surface = _surface(evidence)
    if name_surface and _re.search(r"(?:^|\s)" + _re.escape(name_surface) + r"(?:$|\s)", evidence_surface):
        return True
    name_compact = _re.sub(r'\s+', '', name)
    ev_compact = _re.sub(r'\s+', '', evidence)
    if len(name_compact) >= 2 and name_compact.lower() in ev_compact.lower():
        return True
    if paper_abbrs:
        name_lower = name.lower()
        for abbr, full in paper_abbrs.items():
            if full == name_lower or _surface(full) == name_surface:
                if _re.search(r'\b' + _re.escape(abbr) + r'\b', evidence):
                    return True
                if abbr.lower() in evidence.lower():
                    return True
        upper = name.upper()
        for alias_key in (upper, upper.rstrip("S")):
            if alias_key in paper_abbrs:
                full = paper_abbrs[alias_key]
                if full.lower() in evidence.lower() or _surface(full) in evidence_surface:
                    return True
                full_compact = _re.sub(r'\s+', '', full)
                if len(full_compact) >= 2 and full_compact.lower() in ev_compact.lower():
                    return True
    if name.isascii() and _re.search(r'[a-zA-Z]', name):
        try:
            escaped = _re.escape(name)
            if _re.search(r'\b' + escaped + r'\b', evidence, _re.IGNORECASE):
                return True
            if len(name_compact) >= 3:
                escaped_c = _re.escape(name_compact)
                if _re.search(r'\b' + escaped_c + r'\b', ev_compact, _re.IGNORECASE):
                    return True
        except _re.error as e:
            _log.debug("_entity_mentioned regex error for %r: %s", name, e)
        if 2 <= len(name) <= 6 and name.isupper() and name.isalpha():
            ev_words = set(_re.findall(r'[A-Za-z]+', evidence))
            if name.lower() in {w.lower() for w in ev_words}:
                return True
        return False
    jieba = _get_jieba()
    if jieba:
        try:
            ev_tokens = set(jieba.cut(evidence))
            ent_tokens = set(jieba.cut(name))
            meaningful = {t.strip() for t in ent_tokens if len(t.strip()) >= 1}
            if meaningful and meaningful.issubset(ev_tokens):
                return True
        except Exception as e:
            _log.debug("_entity_mentioned jieba error for %r: %s", name, e)
    return name in evidence or (len(name_compact) >= 2 and name_compact in ev_compact)


# ═══════════════════════════════════════════════════════════════
#  NLI 语义校验辅助
# ═══════════════════════════════════════════════════════════════

def _run_nli_check(premise: str, head: str, rel: str, tail: str,
                   nli_llm, fallback_llm,
                   nli_cache: dict,
                   stats: dict) -> tuple[str, str]:
    """执行单条 NLI 校验（带缓存 + 模型 fallback + 前置跳过）。返回 (label, hypothesis_str)。"""
    hyp = build_hypothesis(head, rel, tail)
    if hyp is None:
        return "entailment", ""  # 无模板，默认通过

    # 前置跳过：evidence 包含明确断言词 → 直接视为 entailment
    if skip_nli(premise, rel):
        _bump(stats, "nli:skipped_clear")
        return "entailment", hyp

    # 缓存 key：premise + hypothesis
    cache_key = (hash(premise), hyp)
    if cache_key in nli_cache:
        label = nli_cache[cache_key]
        _bump(stats, f"nli:cached:{label}")
        return label, hyp

    results = nli_batch_check(premise, [hyp], nli_llm, fallback_llm)
    label = results[0].get("label", "neutral") if results else "neutral"
    nli_cache[cache_key] = label
    _bump(stats, f"nli:{label}")
    return label, hyp


# ═══════════════════════════════════════════════════════════════
#  核心：构建三元组边
# ═══════════════════════════════════════════════════════════════

def _make_edge(tr, unit, schema, run_id, method, tier, conf,
               stats, reject_log, warn_log, paper,
               nli_llm=None, nli_fallback=None,
               nli_cache=None,
               paper_type_map=None) -> dict | None:
    head, tail = (tr.get("head") or "").strip(), (tr.get("tail") or "").strip()
    ht, tt = tr.get("head_type", ""), tr.get("tail_type", "")
    rel = tr.get("relation", "")

    # ── 类型覆盖规则 ──
    _abbrs = paper.get("_abbreviations", {})
    ht = _normalize_type(head, ht, _abbrs, paper_type_map)
    tt = _normalize_type(tail, tt, _abbrs, paper_type_map)

    def _reject(reason: str, detail: str):
        reject_log.append({
            "paper_title": (paper.get("title") or "")[:120],
            "source_id": unit.get("source_id", ""),
            "chapter_path": unit.get("chapter_path", ""),
            "head": head, "head_type": ht,
            "relation": rel,
            "tail": tail, "tail_type": tt,
            "evidence_sentence": (tr.get("evidence_sentence") or "")[:300],
            "qualifiers": tr.get("qualifiers", {}) or {},
            "reject_reason": reason,
            "reject_detail": detail,
        })

    def _warn(reason: str, detail: str):
        warn_log.append({
            "paper_title": (paper.get("title") or "")[:120],
            "source_id": unit.get("source_id", ""),
            "chapter_path": unit.get("chapter_path", ""),
            "head": head, "head_type": ht,
            "relation": rel,
            "tail": tail, "tail_type": tt,
            "evidence_sentence": (tr.get("evidence_sentence") or "")[:300],
            "qualifiers": tr.get("qualifiers", {}) or {},
            "warn_reason": reason,
            "warn_detail": detail,
        })

    # ── 0) 完整性 ──
    if not (head and tail and rel):
        _bump(stats, "incomplete")
        detail = f"h={repr(head)} t={repr(tail)} r={repr(rel)}"
        print("      SKIP incomplete: " + detail, file=sys.stderr, flush=True)
        _reject("incomplete", detail)
        return None

    # ── 1) Schema 预检 ──
    ok, why = schema.validate({"head": head, "head_type": ht, "relation": rel,
                               "tail": tail, "tail_type": tt})
    if not ok:
        _bump(stats, f"schema:{why.split(chr(65289))[0].split(':')[0]}")
        msg = f"{ht}:{head} -[{rel}]-> {tt}:{tail}"
        print("      REJ schema: " + why + " | " + msg, file=sys.stderr, flush=True)
        _reject("schema", why)
        return None

    # ── 2) 三闸溯源校验 ──
    unit_text = unit.get("text", "")
    ver = verify_evidence(unit_text, tr.get("evidence_sentence", ""))
    if not ver["ok"]:
        _bump(stats, f"prov:{ver['reason']}")
        ep = tr.get("evidence_sentence", "")
        print("      REJ prov(" + ver["reason"] + "): evidence=" + repr(ep[:100]),
              file=sys.stderr, flush=True)
        _reject(f"prov:{ver['reason']}", f"evidence={repr(ep[:100])}")
        return None

    # ── 2.5) Char span 真实性校验 ──
    evidence_text = ver.get("evidence_sentence", "") or tr.get("evidence_sentence", "")
    cs = ver.get("char_start")
    ce = ver.get("char_end")
    if cs is not None and ce is not None and isinstance(cs, int) and isinstance(ce, int):
        if unit_text and 0 <= cs < ce <= len(unit_text):
            actual_text = unit_text[cs:ce]
            if actual_text.strip() != evidence_text.strip():
                _bump(stats, "span_mismatch")
                detail = (f"char[{cs}:{ce}] 实际文本与 evidence_sentence 不符: "
                          f"expected={repr(actual_text[:60])}, got={repr(evidence_text[:60])}")
                print("      REJ span: " + detail, file=sys.stderr, flush=True)
                _reject("span_mismatch", detail)
                return None
        elif unit_text:
            _bump(stats, "span_oob")
            detail = f"char[{cs}:{ce}] 越界 (unit_text 长度={len(unit_text)})"
            print("      REJ span: " + detail, file=sys.stderr, flush=True)
            _reject("span_oob", detail)
            return None

    # ── 3) 实体-证据句匹配 ──
    head_ok = _entity_mentioned(head, evidence_text, _abbrs)
    tail_ok = _entity_mentioned(tail, evidence_text, _abbrs)
    if not head_ok or not tail_ok:
        missing = []
        if not head_ok:
            missing.append("head=" + repr(head[:60]))
        if not tail_ok:
            missing.append("tail=" + repr(tail[:60]))
        _bump(stats, "ev_entity_mismatch")
        reason = ", ".join(missing)
        print("      REJ ev_entity: " + reason + " not in evidence",
              file=sys.stderr, flush=True)
        _reject("ev_entity_mismatch", reason + " not in evidence")
        return None

    # ── 3.1) 命题完整性：目的/假设/平行组不能充当疗效证据 ──
    weak_reason = _weak_relation_reason(rel, evidence_text)
    if weak_reason:
        _bump(stats, "weak_relation_evidence")
        print("      REJ weak evidence: " + weak_reason, file=sys.stderr, flush=True)
        _reject("weak_relation_evidence", weak_reason)
        return None

    if rel == "HAS_PHENOTYPE":
        phenotype_reason = _phenotype_semantics_reason(ht, evidence_text)
        if phenotype_reason:
            _bump(stats, "causal_semantics_mismatch")
            _reject("causal_semantics_mismatch", phenotype_reason)
            return None

    # ── 3.5) NLI 语义校验 ──
    if nli_llm is not None and nli_cache is not None and needs_nli(rel):
        nli_label, nli_hyp = _run_nli_check(
            evidence_text, head, rel, tail, nli_llm, nli_fallback, nli_cache, stats
        )
        if nli_label == "contradiction":
            _bump(stats, "nli_reject")
            detail = f"NLI contradiction: evidence contradicts {nli_hyp}"
            print("      REJ nli: " + detail[:120], file=sys.stderr, flush=True)
            _reject("nli_contradiction", detail)
            return None
        elif nli_label == "neutral":
            conf = round(conf * _NLI_NEUTRAL_FACTOR, 3)
            detail = f"NLI neutral: evidence does not entail {nli_hyp} (confidence→{conf})"
            print("      ⚠ nli_neutral: " + detail[:120], file=sys.stderr, flush=True)
            _warn("nli_neutral", detail)

    # ── 3.6) HDI 重分类: 无 PK 证据 → COMPLEMENTS_WM_THERAPY ──
    if rel == "HDI" and not _has_pk_evidence(evidence_text):
        new_rel = "COMPLEMENTS_WM_THERAPY"
        # 复查 schema
        ok2, _ = schema.validate({"head": head, "head_type": ht, "relation": new_rel,
                                   "tail": tail, "tail_type": tt})
        if ok2:
            rel = new_rel
            weak_reason = _weak_relation_reason(rel, evidence_text)
            if weak_reason:
                _bump(stats, "weak_relation_evidence")
                print("      REJ weak evidence: " + weak_reason,
                      file=sys.stderr, flush=True)
                _reject("weak_relation_evidence", weak_reason)
                return None
            _bump(stats, "hdi_reclassified")
            detail = "HDI→COMPLEMENTS: no PK evidence"
            print("      ⚠ reclass: " + detail[:100], file=sys.stderr, flush=True)
            _warn("hdi_reclassified", detail)

    # ── 3.7) 机制证据分级（不改变 schema） ──
    if rel == "MOLECULARLY_TARGETS":
        mechanism_grade = _mechanism_evidence_grade(evidence_text)
        if mechanism_grade == "predicted":
            conf = round(conf * 0.75, 3)
            _bump(stats, "mechanism:predicted")
            _warn("predicted_mechanism",
                  f"MOLECULARLY_TARGETS is prediction-level evidence (confidence→{conf})")
        elif mechanism_grade == "indirect":
            conf = round(conf * 0.85, 3)
            _bump(stats, "mechanism:indirect")
            _warn("indirect_mechanism",
                  f"MOLECULARLY_TARGETS lacks direct binding/regulation evidence (confidence→{conf})")

    # ── 4) 否定检测 ──
    neg, neg_detail = _has_negation(evidence_text, rel, head, tail)
    if neg:
        _bump(stats, "negation_warning")
        print("      ⚠ negation: " + neg_detail[:100], file=sys.stderr, flush=True)
        _warn("negation_warning", neg_detail)

    # ── 4.5) 结构关系置信度加权 ──
    boost = _RELATION_CONFIDENCE_BOOST.get(rel, 0.0)
    if boost:
        conf = round(min(conf + boost, 0.98), 3)

    # ── 组装 ──
    _paper_abbrs = paper.get("_abbreviations", {})
    _canon_head = _canonicalize_entity_name(head, ht, _paper_abbrs)
    _canon_tail = _canonicalize_entity_name(tail, tt, _paper_abbrs)
    hid, tid = node_id(_canon_head, ht), node_id(_canon_tail, tt)
    eid = edge_id(rel, hid, tid)
    ver_cs = ver.get("char_start")
    ver_ce = ver.get("char_end")
    prov = {
        "paper_batch_id": unit.get("paper_batch_id", ""),
        "source_id": unit.get("source_id", "") or "",
        "section_index": unit.get("section_index"),
        "section_type": unit.get("section_type", ""),
        "chapter_path": unit.get("chapter_path", ""),
        "evidence_chunk_id": unit.get("unit_id", ""),
        "unit_hash": unit.get("unit_hash", ""),
        "para_index_range": unit.get("para_index_range"),
        "char_start": ver_cs,
        "char_end": ver_ce,
        "evidence_sentence": ver.get("evidence_sentence", ""),
        "evidence_level": unit.get("evidence_level", ""),
        "evidence_tier": tier,
        "extraction_method": method,
        "extraction_run_id": run_id,
        "license": unit.get("license", ""),
        "usage_scope": unit.get("usage_scope", "commercial"),
    }
    _bump(stats, "accepted")
    return {
        "edge_id": eid, "head_id": hid, "tail_id": tid,
        "head": head, "head_type": ht, "tail": tail, "tail_type": tt,
        "_head_canon": _canon_head, "_tail_canon": _canon_tail,
        "relation": rel, "qualifiers": tr.get("qualifiers", {}) or {},
        "evidence_sentence": ver.get("evidence_sentence", ""),
        "evidence_chunk_id": unit.get("unit_id", ""),
        "evidence_source_id": unit.get("source_id", ""),
        "chapter_path": unit.get("chapter_path", ""),
        "license": unit.get("license", ""),
        "usage_scope": unit.get("usage_scope", "commercial"),
        "allowed_use": unit.get("allowed_use", "kg"),
        "evidence_tier": tier,
        "extraction_method": method,
        "kg_version": schema.schema_version,
        "status": "candidate",
        "confidence": conf,
        "domain": schema.relation_domain_of(rel),
        "provenance": prov,
    }


# ═══════════════════════════════════════════════════════════════
#  实体类型规范化
# ═══════════════════════════════════════════════════════════════

def _normalize_type(name: str, entity_type: str,
                    paper_abbrs: dict | None = None,
                    paper_type_map: dict | None = None) -> str:
    """规范化实体类型。

    规则：
    1. 成分名强制覆盖（含缩写展开）：polysaccharide/saponin 等 → MolecularEntity
    2. 同论文内类型一致性：同一实体名首次出现的类型为准
    """
    if not name or not entity_type:
        return entity_type

    # 规则 1: 成分名强制覆盖
    if entity_type == "Herb":
        if _should_be_molecular_entity(name):
            return "MolecularEntity"
        # 缩写展开后再检查：APS → Astragalus polysaccharide → MolecularEntity
        if paper_abbrs:
            expanded = paper_abbrs.get(name.upper(), "")
            if expanded and _should_be_molecular_entity(expanded):
                return "MolecularEntity"

    # 规则 2: 同一论文内类型一致性（首次出现为准，但规则 1 的覆盖优先）
    if paper_type_map is not None:
        if name in paper_type_map:
            return paper_type_map[name]
        # 首次出现：注册
        paper_type_map[name] = entity_type

    return entity_type


def _bump(stats: dict, key: str) -> None:
    stats[key] = stats.get(key, 0) + 1


def new_run_id() -> str:
    return "run:" + _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%MZ")
