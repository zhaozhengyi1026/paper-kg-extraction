"""
交互可信度评分 (Interaction Credibility Score, ICS) v2.0。

用于 DDI/HDI/HHI 三元组的可信度评估，综合 8 个维度 (满分 100):
  1. 论文权威度           (0-25) — 来自 evidence_grader_v2 的原始 0-100 分线性映射
  2. 严重性权重           (0-12) — 越严重的交互越受关注
  3. 临床后果             (0-8)  — 交互导致的临床结局严重度
  4. 证据直接性           (0-20) — 临床证据 > 动物实验 > 体外 > 计算预测
  5. 研究质量             (0-15) — 样本量/盲法/多中心/偏倚风险
  6. 多源一致性           (0-10) — 多个独立论文报告同一交互 (单源更审慎)
  7. 声明一致性           (0-5)  — 多源报告的交互类型是否一致
  8. 元数据-正文一致性    (0-5)  — CEBM 等级 vs LLM 推断证据等级是否匹配

v2.0 改进:
  - 论文原始 0-100 分线性映射，保留细粒度信息
  - 新增临床后果、研究质量、元数据一致性三个独立维度
  - 单源证据更审慎 (source_count=1 仅得 2/10 分)
  - 严重性细化为独立 severity + clinical_consequence 两维

ICS 等级映射:
  A (>=75): 高可信 — 可直接入库，用于临床决策支持
  B (50-74): 中可信 — 入候选库，需人工审核确认
  C (25-49): 低可信 — 入观察库，仅作研究参考
  D (<25):  极低可信 — 不入库
"""

from __future__ import annotations

# ════════════════════════════════════════════════════════════════
# 维度 1: 论文权威度 — 原始 0-100 分线性映射到 0-25
# ════════════════════════════════════════════════════════════════

def paper_authority_score(raw_score: float) -> tuple[int, str]:
    """
    论文原始 0-100 分 -> (0-25 ICS 分, 标签)
    v2.0: 使用线性映射保留细粒度，替代 v1.0 的三档离散映射。
    """
    s = max(0.0, min(100.0, raw_score))
    mapped = round(s * 0.25)  # 0-100 -> 0-25
    if s >= 85:
        label = "顶级权威"
    elif s >= 70:
        label = "高权威"
    elif s >= 50:
        label = "中等权威"
    elif s >= 30:
        label = "较低权威"
    else:
        label = "低权威"
    return mapped, label


# ════════════════════════════════════════════════════════════════
# 维度 2: 严重性权重 (0-12)
# ════════════════════════════════════════════════════════════════
SEVERITY_SCORE = {
    "fatal": 12,
    "severe": 10,
    "moderate": 7,
    "mild": 3,
    "unknown": 2,
}

# ════════════════════════════════════════════════════════════════
# 维度 3: 临床后果 (0-8) — 新增
# ════════════════════════════════════════════════════════════════
CLINICAL_CONSEQUENCE_SCORE = {
    "life_threatening": 8,
    "organ_failure": 7,
    "hospitalization_required": 6,
    "requires_intervention": 4,
    "discomfort_only": 2,
    "none": 0,
    "unknown": 1,
}

# 可逆性调整因子 (0.7-1.0)
REVERSIBILITY_FACTOR = {
    "irreversible": 1.0,
    "partially_reversible": 0.9,
    "reversible": 0.8,
    "unknown": 0.85,
}

# 高风险人群加成 (0-2 额外加分)
POPULATION_RISK_BONUS = {
    "general": 0,
    "elderly": 1,
    "pediatric": 1,
    "pregnant": 2,
    "renal_impaired": 1,
    "hepatic_impaired": 1,
    "specific_genotype": 1,
    "unknown": 0,
}

# 发生频率调整
FREQUENCY_FACTOR = {
    "common": 1.0,
    "uncommon": 0.85,
    "rare": 0.7,
    "unknown": 0.85,
}

# ════════════════════════════════════════════════════════════════
# 维度 4: 证据直接性 (0-20)
# ════════════════════════════════════════════════════════════════
EVIDENCE_GRADE_SCORE = {
    "clinical_trial": 20,
    "observational": 16,
    "in_vivo": 12,
    "case_report": 9,
    "in_vitro": 6,
    "in_silico": 4,
    "mechanistic_inference": 4,
}

# ════════════════════════════════════════════════════════════════
# 维度 5: 研究质量 (0-15) — 新增
# ════════════════════════════════════════════════════════════════
SAMPLE_SIZE_SCORE = {
    "large": 5,      # >=100 (clinical) / >=30 (experimental)
    "medium": 3,     # 30-99 / 10-29
    "small": 1,      # <30 / <10
    "unknown": 1,
}

BLINDING_SCORE = {
    "double_blind": 5,
    "single_blind": 3,
    "open_label": 1,
    "not_applicable": 2,  # in_vitro / in_silico
    "unknown": 1,
}

MULTICENTER_SCORE = {
    True: 3,
    False: 0,
}

BIAS_RISK_SCORE = {
    "low": 2,
    "medium": 1,
    "high": 0,
    "unknown": 0,
}


def compute_study_quality(
    sample_size: str = "unknown",
    blinding: str = "unknown",
    multicenter: bool = False,
    bias_risk: str = "unknown",
) -> tuple[int, dict]:
    """计算研究质量子分数 (0-15)。"""
    ss = SAMPLE_SIZE_SCORE.get(sample_size, 1)
    bs = BLINDING_SCORE.get(blinding, 1)
    ms = MULTICENTER_SCORE.get(multicenter, 0)
    br = BIAS_RISK_SCORE.get(bias_risk, 0)
    total = ss + bs + ms + br
    breakdown = {
        "sample_size_score": ss,
        "blinding_score": bs,
        "multicenter_score": ms,
        "bias_risk_score": br,
    }
    return total, breakdown


# ════════════════════════════════════════════════════════════════
# 维度 8: 元数据-正文一致性 (0-5) — 新增
# ════════════════════════════════════════════════════════════════

# CEBM 等级 -> 期望的 evidence_grade 范围
CEBM_TO_EXPECTED_EVIDENCE = {
    "1a": {"clinical_trial", "observational"},
    "1b": {"clinical_trial"},
    "2a": {"clinical_trial", "observational"},
    "2b": {"clinical_trial", "observational"},
    "3a": {"observational", "case_report"},
    "3b": {"observational", "case_report"},
    "4":  {"case_report", "observational"},
    "5":  set(),
}

EVIDENCE_GRADE_ORDER = {
    "clinical_trial": 6,
    "observational": 5,
    "in_vivo": 4,
    "case_report": 3,
    "in_vitro": 2,
    "in_silico": 1,
    "mechanistic_inference": 1,
}


def compute_metadata_consistency(
    paper_cebm_level: str = "",
    llm_evidence_grade: str = "",
) -> tuple[int, str, str]:
    """
    计算元数据-正文一致性 (0-5)。
    比较 PubMed 标注的 CEBM 等级与 LLM 从正文推断的证据等级。
    """
    if not paper_cebm_level or not llm_evidence_grade:
        return 3, "unknown", "缺少 CEBM 或 evidence_grade 信息，无法比较"

    expected = CEBM_TO_EXPECTED_EVIDENCE.get(paper_cebm_level, set())
    if not expected:
        return 5, "not_applicable", f"CEBM {paper_cebm_level} 无证据类型约束"

    if llm_evidence_grade in expected:
        return 5, "consistent", f"CEBM {paper_cebm_level} 与 {llm_evidence_grade} 一致"

    cebm_expected_max = max(
        (EVIDENCE_GRADE_ORDER.get(e, 0) for e in expected), default=0
    )
    llm_order = EVIDENCE_GRADE_ORDER.get(llm_evidence_grade, 0)
    gap = cebm_expected_max - llm_order

    if gap <= 1:
        return 3, "minor_gap", f"CEBM {paper_cebm_level} 期望 >=等级{cebm_expected_max}，实际 {llm_evidence_grade}({llm_order})，差距={gap}"
    elif gap <= 2:
        return 1, "major_gap", f"CEBM {paper_cebm_level} 期望 >=等级{cebm_expected_max}，实际 {llm_evidence_grade}({llm_order})，差距={gap}"
    else:
        return 0, "conflict", f"CEBM {paper_cebm_level} 与 {llm_evidence_grade} 严重矛盾，差距={gap}"


# ════════════════════════════════════════════════════════════════
# ICS 等级阈值
# ════════════════════════════════════════════════════════════════
ICS_TIER_THRESHOLDS = [
    (75, "A", "高可信 - 可直接入库"),
    (50, "B", "中可信 - 入候选库，需人工审核"),
    (25, "C", "低可信 - 入观察库，仅作研究参考"),
    (0,  "D", "极低可信 - 不入库"),
]

# 交互关系集合
INTERACTION_RELATIONS = {
    "DDI", "HDI", "HHI", "INTERACTS_WITH", "HDI_VIA_SHARED_TARGET",
    "INCOMPATIBLE_WITH", "ANTAGONIZES", "POTENTIATES", "REDUCES_TOXICITY",
    "WM_CONTRAINDICATED_FOR",
}

SYMMETRIC_INTERACTION_RELATIONS = {
    "DDI", "HHI", "INTERACTS_WITH", "INCOMPATIBLE_WITH", "ANTAGONIZES",
}


def is_interaction(relation: str) -> bool:
    """判断三元组是否属于药物交互关系。"""
    return relation in INTERACTION_RELATIONS


def compute_ics(
    # 论文权威度 (v2: 直接使用原始 0-100 分)
    paper_raw_score: float = 0.0,
    # 交互严重性
    severity: str = "unknown",
    # 临床后果 (新增)
    clinical_consequence: str = "unknown",
    reversibility: str = "unknown",
    frequency: str = "unknown",
    population_risk: str = "unknown",
    # 证据类型
    evidence_grade: str = "",
    # 研究质量 (新增)
    sample_size: str = "unknown",
    blinding: str = "unknown",
    multicenter: bool = False,
    bias_risk: str = "unknown",
    # 多源
    source_count: int = 1,
    type_consistency: bool = True,
    # 元数据一致性 (新增)
    paper_cebm_level: str = "",
) -> tuple[int, str, dict]:
    """
    计算单条交互的 ICS 分数 (v2.0, 8 维度, 满分 100)。
    返回: (ics_score, tier_label, full_breakdown_dict)
    """
    # 维度 1: 论文权威度 (0-25)
    pap_score, pap_label = paper_authority_score(paper_raw_score)

    # 维度 2: 严重性 (0-12)
    sev_score = SEVERITY_SCORE.get(severity, 2)

    # 维度 3: 临床后果 (0-8)，经可逆性/频率/人群调整
    cc_base = CLINICAL_CONSEQUENCE_SCORE.get(clinical_consequence, 1)
    rev_factor = REVERSIBILITY_FACTOR.get(reversibility, 0.85)
    freq_factor = FREQUENCY_FACTOR.get(frequency, 0.85)
    pop_bonus = POPULATION_RISK_BONUS.get(population_risk, 0)
    cc_score = min(8, round(cc_base * rev_factor * freq_factor) + pop_bonus)

    # 维度 4: 证据直接性 (0-20)
    ev_score = EVIDENCE_GRADE_SCORE.get(evidence_grade, 4)

    # 维度 5: 研究质量 (0-15)
    sq_score, sq_breakdown = compute_study_quality(
        sample_size, blinding, multicenter, bias_risk
    )

    # 维度 6: 多源一致性 (0-10) — v2 更审慎
    if source_count >= 5:
        source_score = 10
    elif source_count >= 3:
        source_score = 8
    elif source_count >= 2:
        source_score = 5
    else:
        source_score = 2   # 单源仅得 2/10，需靠质量弥补

    # 维度 7: 声明一致性 (0-5)
    if type_consistency and source_count >= 2:
        consistency_score = 5
    elif type_consistency or source_count >= 2:
        consistency_score = 3
    else:
        consistency_score = 0

    # 维度 8: 元数据-正文一致性 (0-5)
    mc_score, mc_label, mc_detail = compute_metadata_consistency(
        paper_cebm_level, evidence_grade
    )

    # 汇总
    total = (
        pap_score + sev_score + cc_score + ev_score
        + sq_score + source_score + consistency_score + mc_score
    )

    # 确定等级
    tier_label = "D"
    tier_desc = "极低可信 - 不入库"
    for threshold, label, desc in ICS_TIER_THRESHOLDS:
        if total >= threshold:
            tier_label = label
            tier_desc = desc
            break

    breakdown = {
        "ics_version": "2.0",
        "ics_total": total,
        "ics_tier": tier_label,
        "ics_tier_desc": tier_desc,
        "paper_authority_score": pap_score,
        "paper_authority_label": pap_label,
        "paper_raw_score": paper_raw_score,
        "severity_score": sev_score,
        "clinical_consequence_score": cc_score,
        "clinical_consequence_detail": {
            "base": cc_base,
            "reversibility_factor": rev_factor,
            "frequency_factor": freq_factor,
            "population_bonus": pop_bonus,
        },
        "evidence_directness_score": ev_score,
        "study_quality_score": sq_score,
        "study_quality_breakdown": sq_breakdown,
        "source_diversity_score": source_score,
        "consistency_score": consistency_score,
        "metadata_consistency_score": mc_score,
        "metadata_consistency_label": mc_label,
        "metadata_consistency_detail": mc_detail,
    }
    return total, tier_label, breakdown


def enrich_interaction_triple(
    triple: dict,
    paper_authority_tier: str = "",
    paper_authority_score: float = 0.0,
    paper_cebm_level: str = "",
) -> dict:
    """
    为一条交互三元组添加 ICS v2.0 评分 (单篇论文内调用)。

    参数:
      triple: 原始三元组 dict
      paper_authority_tier: 向后兼容 (A/B/C), v2 中仅作标签保留
      paper_authority_score: 论文原始 0-100 权威度分 (来自 evidence_grader_v2)
      paper_cebm_level: 论文的 CEBM 等级 (1a/1b/2a/.../5), 来自 PubMed 元数据
    """
    q = triple.get("qualifiers", {}) or {}

    severity = q.get("severity", "unknown")
    evidence_grade = q.get("evidence_grade", "mechanistic_inference")
    clinical_consequence = q.get("clinical_consequence", "unknown")
    reversibility = q.get("reversibility", "unknown")
    frequency = q.get("frequency", "unknown")
    population_risk = q.get("population_risk", "unknown")
    sample_size = q.get("sample_size", "unknown")
    blinding = q.get("blinding", "unknown")
    multicenter = q.get("multicenter", False)
    bias_risk = q.get("bias_risk", "unknown")

    ics_score, ics_tier, breakdown = compute_ics(
        paper_raw_score=paper_authority_score,
        severity=severity,
        clinical_consequence=clinical_consequence,
        reversibility=reversibility,
        frequency=frequency,
        population_risk=population_risk,
        evidence_grade=evidence_grade,
        sample_size=sample_size,
        blinding=blinding,
        multicenter=bool(multicenter),
        bias_risk=bias_risk,
        source_count=1,
        type_consistency=True,
        paper_cebm_level=paper_cebm_level,
    )

    enriched = dict(triple)
    enriched["paper_authority_tier"] = paper_authority_tier
    enriched["paper_authority_score"] = paper_authority_score
    enriched["paper_cebm_level"] = paper_cebm_level
    enriched["ics_score"] = ics_score
    enriched["ics_tier"] = ics_tier
    enriched["ics_breakdown"] = breakdown

    return enriched


def batch_enrich(
    triples: list[dict],
    paper_authority_tier: str = "",
    paper_authority_score: float = 0.0,
    paper_cebm_level: str = "",
) -> list[dict]:
    """批量增强：为一批三元组中的交互关系添加 ICS v2.0 评分。"""
    enriched = []
    for tr in triples:
        rel = tr.get("relation", "")
        if is_interaction(rel):
            enriched.append(
                enrich_interaction_triple(
                    tr, paper_authority_tier, paper_authority_score, paper_cebm_level
                )
            )
        else:
            enriched.append(dict(tr))
    return enriched


def cross_source_merge(
    interaction_groups: dict[str, list[dict]],
) -> list[dict]:
    """
    跨论文合并：将同一药物对的多个来源的交互报告合并，重新计算 ICS v2.0。
    """
    merged = []
    for pair_key, reports in interaction_groups.items():
        if not reports:
            continue

        base = dict(reports[0])
        base["provenance"] = dict(base.get("provenance", {}))
        evidence_sources = []
        evidence_records = []
        for report in reports:
            source_id = (report.get("evidence_source_id") or
                         report.get("provenance", {}).get("source_id", ""))
            if source_id and source_id not in evidence_sources:
                evidence_sources.append(source_id)
            evidence_records.append({
                "source_id": source_id,
                "edge_id": report.get("edge_id", ""),
                "evidence_sentence": report.get("evidence_sentence", ""),
                "confidence": report.get("confidence", 0),
                "evidence_tier": report.get("evidence_tier", ""),
                "extraction_model": report.get("extraction_model", ""),
                "paper_authority_score": report.get("paper_authority_score", 0),
                "provenance": dict(report.get("provenance", {})),
            })
        source_count = len(evidence_sources) or len(reports)

        # 多源类型一致性
        types = {r.get("qualifiers", {}).get("interaction_type", "") for r in reports}
        type_consistent = len(types) <= 1

        # 取最严重的 severity
        sev_order = {"fatal": 4, "severe": 3, "moderate": 2, "mild": 1, "unknown": 0}
        best_sev = max(reports, key=lambda r: sev_order.get(
            r.get("qualifiers", {}).get("severity", "unknown"), 0
        ))
        severity = best_sev.get("qualifiers", {}).get("severity", "unknown")

        # 取最高证据等级
        ev_order = {"clinical_trial": 6, "observational": 5, "in_vivo": 4,
                     "case_report": 3, "in_vitro": 2, "in_silico": 1, "mechanistic_inference": 0}
        best_ev = max(reports, key=lambda r: ev_order.get(
            r.get("qualifiers", {}).get("evidence_grade", "mechanistic_inference"), 0
        ))
        evidence_grade = best_ev.get("qualifiers", {}).get("evidence_grade", "mechanistic_inference")

        # 取最高论文原始分
        best_auth = max(reports, key=lambda r: r.get("paper_authority_score", 0.0))
        paper_raw_score = best_auth.get("paper_authority_score", 0.0)
        paper_tier = best_auth.get("paper_authority_tier", "C")
        paper_cebm = best_auth.get("paper_cebm_level", "")

        # 取最佳研究质量各维度
        ss_order = {"large": 3, "medium": 2, "small": 1, "unknown": 0}
        best_ss = max(reports, key=lambda r: ss_order.get(
            r.get("qualifiers", {}).get("sample_size", "unknown"), 0
        ))
        sample_size = best_ss.get("qualifiers", {}).get("sample_size", "unknown")

        bl_order = {"double_blind": 4, "single_blind": 3, "open_label": 2,
                     "not_applicable": 1, "unknown": 0}
        best_bl = max(reports, key=lambda r: bl_order.get(
            r.get("qualifiers", {}).get("blinding", "unknown"), 0
        ))
        blinding = best_bl.get("qualifiers", {}).get("blinding", "unknown")

        multicenter = any(
            r.get("qualifiers", {}).get("multicenter", False) for r in reports
        )

        br_order = {"low": 3, "medium": 2, "high": 1, "unknown": 0}
        best_br = max(reports, key=lambda r: br_order.get(
            r.get("qualifiers", {}).get("bias_risk", "unknown"), 0
        ))
        bias_risk = best_br.get("qualifiers", {}).get("bias_risk", "unknown")

        cc_order = {"life_threatening": 5, "organ_failure": 4,
                     "hospitalization_required": 3, "requires_intervention": 2,
                     "discomfort_only": 1, "none": 0, "unknown": 0}
        best_cc = max(reports, key=lambda r: cc_order.get(
            r.get("qualifiers", {}).get("clinical_consequence", "unknown"), 0
        ))
        clinical_consequence = best_cc.get("qualifiers", {}).get("clinical_consequence", "unknown")
        reversibility = best_cc.get("qualifiers", {}).get("reversibility", "unknown")
        frequency = best_cc.get("qualifiers", {}).get("frequency", "unknown")
        population_risk = best_cc.get("qualifiers", {}).get("population_risk", "unknown")

        # 重新计算 ICS v2.0
        ics_score, ics_tier, breakdown = compute_ics(
            paper_raw_score=paper_raw_score,
            severity=severity,
            clinical_consequence=clinical_consequence,
            reversibility=reversibility,
            frequency=frequency,
            population_risk=population_risk,
            evidence_grade=evidence_grade,
            sample_size=sample_size,
            blinding=blinding,
            multicenter=bool(multicenter),
            bias_risk=bias_risk,
            source_count=source_count,
            type_consistency=type_consistent,
            paper_cebm_level=paper_cebm,
        )

        base["ics_score"] = ics_score
        base["ics_tier"] = ics_tier
        base["ics_breakdown"] = breakdown
        base["source_count"] = source_count
        base["type_consistent"] = type_consistent
        base["paper_authority_tier"] = paper_tier
        base["paper_authority_score"] = paper_raw_score
        base["paper_cebm_level"] = paper_cebm
        base["evidence_sources"] = evidence_sources
        base["evidence_records"] = evidence_records
        base.setdefault("provenance", {})["evidence_sources"] = evidence_sources
        base["provenance"]["source_count"] = source_count

        merged.append(base)

    return merged
