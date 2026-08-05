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


def test_no_donor_is_not_a_negation_cue():
    assert not extract._has_negation(
        "The NO donor significantly reduced restenosis",
        "WM_TREATS", "NO donor", "restenosis",
    )[0]


def test_target_evidence_grade_handles_direct_and_indirect_forms():
    assert extract._mechanism_evidence_grade("DAT is a target of amphetamine") == "direct"
    assert extract._mechanism_evidence_grade("mepolizumab is an antibody against IL-5") == "direct"
    assert extract._mechanism_evidence_grade("The treatment changed TP53 expression") == "indirect"
    assert extract._mechanism_evidence_grade("HSF-2 is a downstream target of b-FGF") == "direct"
    assert extract._mechanism_evidence_grade("ADP-ribose polymers target topoisomerase I") == "direct"


def test_causal_or_predictive_claim_is_not_a_phenotype():
    assert extract._phenotype_semantics_reason(
        "Exposure", "Smoking caused endothelial dysfunction."
    )
    assert extract._phenotype_semantics_reason(
        "LabTest", "Elevated CRP predicted mortality."
    )
    assert not extract._phenotype_semantics_reason(
        "DiseaseWM", "Diabetes is characterized by hyperglycemia."
    )


def test_entity_matching_handles_alias_surface_variants():
    aliases = {"DMP1": "dentin matrix protein 1", "PTSD": "post traumatic stress disorder"}
    assert extract._entity_mentioned("dentin matrix protein-1", "DMP1 expression increased", aliases)
    assert extract._entity_mentioned("post-traumatic stress disorders", "PTSD was diagnosed", aliases)
    assert extract._entity_mentioned("β-catenin", "alpha and beta catenin were measured")
    assert extract._entity_mentioned("RACK1", "a stable complex with RACK-1")
    assert extract._entity_mentioned("human SPC25", "hSPC25 interacted with HEC1")


def test_numeric_leading_parenthetical_abbreviation():
    paper = {"title": "4-hydroxy-2-nonenal (4-HNE) signaling", "abstract": "", "sections": []}
    aliases = extract._extract_abbreviations(paper)
    assert aliases["4-HNE"] == "4-hydroxy-2-nonenal"
    assert extract._entity_mentioned("4-hydroxy-2-nonenal", "4-HNE activated ERK1/2", aliases)


def test_absence_feature_is_affirmed_phenotype_not_relation_negation():
    evidence = "Cicatricial alopecias are characterized by a lack of follicular ostia and irreversible alopecia."
    assert not extract._has_negation(
        evidence, "HAS_PHENOTYPE", "Cicatricial alopecias", "lack of follicular ostia"
    )[0]
    assert not extract._has_negation(
        evidence, "HAS_PHENOTYPE", "Cicatricial alopecias", "irreversible alopecia"
    )[0]


def test_neutral_fragment_requires_complete_predicate():
    assert extract._neutral_evidence_is_fragment("PI3K with wortmannin")
    assert extract._neutral_evidence_is_fragment("employment status among persons with bipolar disorder")
    assert not extract._neutral_evidence_is_fragment("HSVdwi was detected in patients with recent CVT")


def test_duplicate_evidence_prefers_stronger_complete_quote():
    weak = {"confidence": 0.6, "evidence_sentence": "TP53 expression", "qualifiers": {}}
    strong = {"confidence": 0.7, "evidence_sentence": "The compound directly binds to TP53.",
              "qualifiers": {"evidence_grade": "in_vitro"}}
    assert extract._edge_evidence_score(strong) > extract._edge_evidence_score(weak)
