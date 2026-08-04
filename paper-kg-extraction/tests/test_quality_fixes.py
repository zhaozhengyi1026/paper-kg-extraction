# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import extract  # noqa: E402
import nli  # noqa: E402
from provenance import verify_evidence  # noqa: E402


def test_nli_association_does_not_require_causation():
    hypothesis = nli.build_hypothesis("Tie-2", "GENE_ASSOCIATED_WITH_DISEASE", "DN")
    assert "不要求因果" in hypothesis
    assert "因果关联" not in hypothesis


def test_nli_skip_is_relation_specific():
    assert nli.skip_nli("AS-IV downregulated TLR4 expression", "MOLECULARLY_TARGETS")
    assert nli.skip_nli("The formula suppressed the MAPK pathway", "FORMULA_ACTS_VIA")
    assert not nli.skip_nli("in Group B MI plus Captopril", "COMPLEMENTS_WM_THERAPY")
    assert nli.skip_nli(
        "MI combined with Captopril significantly reduced albuminuria",
        "COMPLEMENTS_WM_THERAPY",
    )


def test_negation_ignores_biochemical_no_and_pseudo_negation():
    assert not extract._has_negation(
        "Polysaccharides induce cells to release NO and enhance iNOS activity",
        "MOLECULARLY_TARGETS", "Polysaccharides", "iNOS",
    )[0]
    assert not extract._has_negation(
        "Calycosin not only inhibits IKBα, but also inhibits NF-κB",
        "MOLECULARLY_TARGETS", "Calycosin", "IKBα",
    )[0]
    assert extract._has_negation(
        "The drug did not inhibit NF-κB", "MOLECULARLY_TARGETS", "drug", "NF-κB",
    )[0]


def test_negation_is_scoped_to_relation():
    evidence = (
        "Combination therapy with an ACE inhibitor and an ARB can produce "
        "adverse effects without additional benefits"
    )
    assert not extract._has_negation(evidence, "DDI", "ACE inhibitor", "ARB")[0]
    assert extract._has_negation(
        evidence, "COMPLEMENTS_WM_THERAPY", "ACE inhibitor", "ARB"
    )[0]
    assert extract._has_negation(
        "No significant association was found between Tie-2 and DN",
        "GENE_ASSOCIATED_WITH_DISEASE", "Tie-2", "DN",
    )[0]
    assert extract._has_negation(
        "该治疗未显著改善蛋白尿", "TREATS", "该治疗", "蛋白尿",
    )[0]


def test_hdi_requires_specific_pk_context():
    assert not extract._has_pk_evidence("The combination regulated lipid metabolism")
    assert extract._has_pk_evidence("Co-administration increased gliquidone bioavailability")
    assert extract._has_pk_evidence("The concentration of gliquidone in hepatocytes decreased")


def test_weak_treatment_and_combination_evidence_is_rejected():
    assert extract._weak_relation_reason(
        "TREATS", "To observe the effect of Compound FAM in treating diabetic nephropathy."
    )
    assert extract._weak_relation_reason(
        "COMPLEMENTS_WM_THERAPY", "Rats were treated with GQM or benazepril."
    )
    assert extract._weak_relation_reason(
        "COMPLEMENTS_WM_THERAPY", "in Group B MI plus Captopril"
    )
    assert not extract._weak_relation_reason(
        "COMPLEMENTS_WM_THERAPY",
        "MI combined with Captopril significantly reduced albuminuria.",
    )


def test_target_evidence_grading():
    assert extract._mechanism_evidence_grade("Docking predicted TP53 as a target") == "predicted"
    assert extract._mechanism_evidence_grade("The compound directly binds to TP53") == "direct"
    assert extract._mechanism_evidence_grade("The compound downregulated TP53 expression") == "regulatory"
    assert extract._mechanism_evidence_grade("TP53 was mentioned in the same pathway") == "indirect"


def test_parenthetical_aliases_include_non_initialism_and_plural():
    paper = {"title": "Yupingfeng San (YPFS) for renal disease", "abstract": "", "sections": []}
    assert extract._extract_abbreviations(paper)["YPFS"] == "yupingfeng san"
    plural = {"title": "angiotensin receptor blockers (ARBs) in DN",
              "abstract": "", "sections": []}
    assert extract._extract_abbreviations(plural)["ARBS"] == "angiotensin receptor blockers"


def test_provenance_preserves_nbsp_and_exact_span():
    unit = "Astragaloside\xa0IV is isolated from Astragalus."
    result = verify_evidence(unit, "Astragaloside IV is isolated from Astragalus.")
    assert result["ok"]
    assert result["evidence_sentence"] == unit
    assert unit[result["char_start"]:result["char_end"]] == result["evidence_sentence"]
