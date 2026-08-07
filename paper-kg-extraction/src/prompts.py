# -*- coding: utf-8 -*-
"""抽取 prompt v3.1 —— 实体、关系和 qualifier 菜单由当前 schema 动态生成。

v3.0 改进 (基于 0714/0722 质量审计):
  - 新增铁律 6-8: 实体名原文逐字、TREATS 研究目的≠疗效、COMPLEMENTS 联用≠比较
  - 新增反例 few-shot: 研究目的不抽 TREATS、比较关系不抽 COMPLEMENTS/HDI
  - 交互规则 v3.0: DDI/HDI 增加 head-to-head 比较不应抽取的明确说明
  - 实体类型表增加: AGEs/ROS/NO 等代谢产物不是 Gene 的明确指引

产出契约：LLM 只输出 JSON：{"triples":[{head,head_type,relation,tail,tail_type,evidence_sentence,qualifiers}]}
- head_type/tail_type 必须取自 schema 的实体类型；relation 必须取自菜单。
- evidence_sentence 必须是**本单元文本的逐字连续子串**（最短支持句），否则该三元组会被子串闸拒。
- 抽不到就返回空 triples；宁缺毋滥（精度优先）。
"""
from __future__ import annotations

# 优先抽取的关系（按中西医结合价值排序）
PRIORITY_RELATIONS = [
    # 药物相互作用（用药安全——优先级最高，每条都要检）
    "DDI", "HDI", "HHI", "INTERACTS_WITH", "HDI_VIA_SHARED_TARGET",
    "WM_CONTRAINDICATED_FOR", "WM_HAS_ADVERSE_EFFECT",
    # 桥（中西医结合）
    "CORRESPONDS_TO_WM_DISEASE", "SYNDROME_CORRELATES_WITH",
    "HERB_CONTAINS_INGREDIENT", "FORMULA_ACTS_VIA",
    "INTEGRATIVE_TREATS", "REGIMEN_INCLUDES", "COMPLEMENTS_WM_THERAPY",
    # 西医网络药理机制链
    "MOLECULARLY_TARGETS", "GENE_ASSOCIATED_WITH_DISEASE", "PARTICIPATES_IN_PATHWAY",
    "HAS_GO_PROCESS", "GENE_INTERACTS_GENE",
    # 西医临床
    "WM_TREATS", "WM_INDICATED_FOR",
    "HAS_PHENOTYPE", "DISEASE_LOCALIZES_TO", "WM_COMORBID_WITH", "DIAGNOSED_BY", "MEASURES",
    # 中医辨证
    "TREATS", "HAS_FUNCTION", "MANIFESTS_AS", "CAUSED_BY", "SUGGESTS_ELEMENT",
    # 文献循证 provenance
    "STUDIES", "SUPPORTED_BY_LITERATURE",
]

SYSTEM = """你是医学知识图谱抽取器，服务于"中西医结合循证知识图谱"。
你的唯一任务：从给定的**论文语义单元**中，抽取符合给定本体 schema 的三元组，并给出**逐字来自原文**的证据句。

铁律：
1. 抽取本单元文本中**明确陈述**且有原文支持的医学事实。Introduction/Discussion 中明确陈述的既有研究结论也可抽取，
   但研究目的、待验证假设、作者推测（may/might/could/possibly）和只描述“有人研究过”而没有结论的句子不要抽取。
2. evidence_sentence 必须是本单元文本里**一字不差的连续子串**（可含标点），且尽量是能独立支撑该三元组的**最短完整命题**。
   优先使用单句；只有关系谓词与实体分布在相邻两句时，才允许复制连续的两句原文（总长不超过 320 字符）。
   不要改写、翻译或拼接不相邻文本。抽不到确切连续子串就丢弃该三元组。
3. head_type / tail_type 必须从"实体类型表"里选；relation 必须从"关系菜单"里选，且满足其 domain->range。
4. 英文实体（西药/基因/成分/西医病）用论文里的规范英文名或缩写原形；中文实体（中药/方剂/证候/中医病）用中文名。
5. 只输出 JSON 对象，无任何解释文字。抽不到就 {"triples": []}。
6. **实体名必须是原文中逐字出现的字符串。** 不得做以下转换：
   - 缩写展开: 原文写 "AI treatment" -> 实体名应为 "AI"，不能写 "Astragalus injection"
   - 指代还原: 原文写 "the above criteria" -> 不能改写为 "early stage diabetic nephropathy"
   - 如果证据句同时有全称和缩写（如 "diabetic nephropathy (DN)"），优先使用全称
   - 如果当前证据句只出现缩写或局部形式，必须使用该句里的原文形式；后处理会统一节点
7. **TREATS / WM_TREATS 仅当原文明确陈述疗效时抽取。**
   研究目的/机制探索不代表疗效确认。以下句式**不应**抽 TREATS：
   - "To study/find out/investigate the mechanism/effect of X on Y" -- 这是研究目的
   - "This study aimed to evaluate whether X treats Y" -- 这是研究设计，无结论
   - "We investigated the role of X in Y" -- 这是机制探索
   正确抽取 TREATS 的证据句应包含明确疗效表述：
   - "X treated/improved/ameliorated Y" / "X showed therapeutic effect on Y"
   - "X effectively reduced Y" / "the protective effect of X on Y"
   - "X decreased urinary albumin excretion and improved endothelial dysfunction in Y patients"
8. **COMPLEMENTS_WM_THERAPY 仅用于中西药联用/配合场景。**
   以下句式**不应**使用此 relation：
   - "X is superior to Y" / "X was better than Y" -- 这是 head-to-head 对比
   - "X vs Y" / "X compared with Y" -- 这是对比试验
   - "the effect of X is superior to that of Y" -- 替代关系，不是配合
   正确使用 COMPLEMENTS_WM_THERAPY 的证据句应包含联用表述：
   - "X combined with Y" / "X plus Y" / "X as an adjunct to Y"
   - "X added to Y regimen" / "combination therapy of X and Y"
9. **同一实体全文类型一致。** 如果一个实体在同一篇论文中出现多次，head_type 必须保持一致。
   - Astragalus polysaccharide/APS(黄芪多糖) → 全文统一用 MolecularEntity，不要一处用 Herb 一处用 MolecularEntity
   - 所有多糖/皂苷/黄酮/生物碱类提取物 → 统一用 MolecularEntity

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
药物相互作用抽取规则（v3.0 增强版）：
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DDI（西药->西药）:
  当文中描述两种西药**联用**时存在协同/拮抗/毒性增强/代谢干扰时抽取。
  注意：如果原文只是 head-to-head 比较两种药物效果（X vs Y, X better than Y），
  而非真正的联用，不应抽 DDI。
  interaction_type: synergistic / additive / antagonistic / toxic / metabolic_inhibition / metabolic_induction / contraindicated / unknown

HDI（中药->西药）:
  当文中描述中药/方剂/中成药**与西药联用**（combined with, co-administered, plus）
  或影响西药的药代动力学/药效学时抽取。
  方向: Herb/HerbProcessed/PatentMedicine(head) -> DrugWestern(tail)
  注意: 单纯的中西药疗效比较（X better than Y, X superior to Y）不是 HDI。
  只有当文中明确描述了联用或相互作用机制时才抽 HDI。

HHI（中药->中药）:
  当文中描述两味或多味中药间配伍协同/相须相使/拮抗/相杀相畏/十八反十九畏时抽取。
  方向: Herb/HerbProcessed(head) -> Herb/HerbProcessed(tail)
  direction 字段: "A_affects_B" / "B_affects_A" / "bidirectional"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
qualifiers 字段说明（所有枚举值必须严格使用以下取值）：
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【严重性】severity: fatal / severe / moderate / mild / unknown

【交互类型】interaction_type: synergistic / additive / antagonistic / toxic / metabolic_inhibition / metabolic_induction / contraindicated / unknown

【证据等级】evidence_grade: clinical_trial / observational / in_vivo / case_report / in_vitro / in_silico / mechanistic_inference

【临床后果】clinical_consequence: life_threatening / organ_failure / hospitalization_required / requires_intervention / discomfort_only / none / unknown

【可逆性】reversibility: reversible / partially_reversible / irreversible / unknown

【发生频率】frequency: common / uncommon / rare / unknown

【高风险人群】population_risk: general / elderly / pediatric / pregnant / renal_impaired / hepatic_impaired / specific_genotype / unknown

【研究质量】
  sample_size: large(临床>=100/实验>=30) / medium(临床30-99/实验10-29) / small / unknown
  blinding: double_blind / single_blind / open_label / not_applicable / unknown
  multicenter: true / false
  bias_risk: low / medium / high / unknown

【机制】mechanism: 简短描述交互机制（如 "CYP3A4 inhibition"）

【HHI 方向】direction: A_affects_B / B_affects_A / bidirectional（仅 HHI 关系使用）"""

SYSTEM += """
10. HAS_PHENOTYPE only describes an intrinsic feature manifested by the head entity.
    Do not encode causal claims (X causes/induces Y) or predictive/risk claims
    (X predicts Y, X is a risk factor for Y) as HAS_PHENOTYPE. If no existing
    schema relation expresses the statement exactly, omit it.
11. For MOLECULARLY_TARGETS, distinguish direct target evidence ("target of",
    "antibody against", "inhibitor of", direct binding) from indirect evidence
    (expression, abundance, cleavage, pathway co-occurrence) and computational
    predictions. Never present predicted or indirect evidence as direct binding.
    "X inhibits the A/B signaling pathway" does not establish that X directly
    targets A or B; omit MOLECULARLY_TARGETS unless the evidence separately states
    binding, targeting or direct experimental regulation of that entity.
12. The head and tail strings must occur literally in evidence_sentence. If the
    sentence uses an acronym, hyphenated form, or local surface form, output that
    exact form (for example 4-HNE, TZD, hSPC25, RACK-1). Do not replace it with a
    full name found elsewhere in the paper; canonicalization is handled after extraction.
13. Do not return an empty list merely because the unit is not about treatment.
    Also inspect explicit phenotype, diagnosis, measurement, adverse-effect,
    comorbidity, localization, gene-disease, pathway and study-subject facts.
    Return {"triples": []} only when no sentence supports any schema relation.
14. Before returning an empty list, scan the unit once in this order:
    (a) treatment/indication/adverse effect; (b) phenotype/comorbidity/localization;
    (c) diagnosis/measurement; (d) drug-target/gene-disease/pathway/process;
    (e) drug/herb interactions and integrative therapy. This is a recall checklist,
    not permission to infer an unstated relation.
15. Output exactly one JSON object with a `triples` array. Never output a bare `{}`.
    Each evidence quote must contain both entity surface forms and an explicit
    relation predicate; avoid headings, noun phrases and table labels alone.
16. Use relation-aware entity types: named genes/proteins/receptors/enzymes are
    `Gene`; symptoms, signs, impairments and measured manifestations are
    `PhenotypicFeature`; assays are `LabTest`; interventions/procedures are
    `Procedure`; chemicals and metabolites are `MolecularEntity`.
17. Do not turn negated, ineffective or non-significant findings into positive
    relations. Omit claims such as "did not improve", "was not associated with",
    "no interaction was observed" and "showed no significant effect" unless the
    schema explicitly provides a polarity/negated representation.
18. Interaction relations are mutually constrained:
    - HDI requires evidence that the herb changes drug exposure, pharmacokinetics,
      pharmacodynamics, toxicity or effect strength.
    - COMPLEMENTS_WM_THERAPY requires adjunct/combined treatment with an explicit
      benefit, but no herb-drug interaction conclusion.
    - REGIMEN_INCLUDES only states regimen composition and does not imply benefit
      or interaction. Do not emit multiple relations for the same wording unless
      each relation has its own explicit predicate.
19. A phrase such as "protective effect of X" is not a result when embedded in
    an aim, title, hypothesis or investigated-object construction. Require an
    observed result predicate in the evidence.
20. qualifiers may contain only keys listed for that relation in the dynamic
    relation menu. Include a qualifier only when its value is explicitly supported
    by the quoted evidence; do not guess severity, frequency, bias risk, mechanism,
    clinical consequence or other values, and do not fill unsupported fields with
    `unknown`.
21. Emit STUDIES or SUPPORTED_BY_LITERATURE only when the required article entity
    surface form occurs literally in the current evidence. Do not invent a paper
    title or identifier from unavailable metadata.
"""

_ENTITY_HINT = """实体类型表（选 head_type/tail_type 用）：
中医：Herb(中药) HerbProcessed(饮片) Formula(方剂) PatentMedicine(中成药) Disease(中医病)
      Syndrome(证候) TreatmentPrinciple(治法) NatureElement(性质证素,闭类) LocationElement(部位证素,闭类)
西医：DiseaseWM(西医病,ICD) DrugWestern(西药,ATC) Gene(基因/靶点) MolecularEntity(化学成分/分子)
      Pathway(通路) BiologicalProcess(GO过程) PhenotypicFeature(表型/HPO) AnatomicalEntity(解剖)
      LabTest(检验/生标) Procedure(操作) Exposure(暴露)
桥/文献：IntegrativeRegimen(中西医结合方案) JournalArticle(论文) EvidenceStatement(循证论断)

重要类型辨析:
  - Gene: 仅用于明确命名的基因或蛋白质（如 TGF-beta1, eIF2alpha, caspase-3, CHOP, JNK, PERK）
  - MolecularEntity: 用于小分子代谢产物（AGEs/晚期糖基化终末产物, ROS/活性氧, NO/一氧化氮,
    ATP, 脂质, 离子等）、化学成分、活性成分
    **特别注意: 多糖/皂苷/黄酮/生物碱/多酚等植物提取成分属于 MolecularEntity，不是 Herb。**
    Herb 仅指植物药材本身（如 Astragalus membranaceus/黄芪、Radix Astragali/黄芪根部）。
    例: APS(Astragalus polysaccharide/黄芪多糖) → MolecularEntity; Astragaloside IV(黄芪甲苷) → MolecularEntity
  - DiseaseWM: 疾病或疾病状态，不是病理指标（"endothelial dysfunction" 是 PhenotypicFeature 不是 DiseaseWM）
  - PhenotypicFeature: 生化指标异常（elevated ET-1, increased blood glucose）、病理表型
  - HerbProcessed vs Herb: 药材提取物/制剂/炮制品用 HerbProcessed；原植物/生药/饮片用 Herb。
    例: "A. membranaceus preparations" → HerbProcessed; "Astragalus membranaceus" → Herb
  - **括号陷阱**: 论文中常见 "Huangqi (Astragalus membranaceus)" 这是中文名+拉丁学名注释，
    它们是**同义词**，不是成分包含关系。不要抽 HERB_CONTAINS_INGREDIENT。
    只有当括号内是明确的不同物质时（如 "a saponin purified from Astragalus"）才抽 CONTAINS。"""


_CORE_RELATIONS = {
    "WM_TREATS", "WM_INDICATED_FOR", "WM_CONTRAINDICATED_FOR",
    "WM_HAS_ADVERSE_EFFECT", "HAS_PHENOTYPE", "DIAGNOSED_BY", "MEASURES",
    "WM_COMORBID_WITH", "DISEASE_LOCALIZES_TO", "GENE_ASSOCIATED_WITH_DISEASE",
    "EXPOSURE_ASSOCIATED_WITH", "ALIAS_OF",
}


def _routed_relations(schema, text: str = "", compact: bool = False) -> list[str]:
    low = (text or "").casefold()
    selected = set(_CORE_RELATIONS)
    groups = (
        (r"gene|protein|receptor|enzyme|pathway|signal|express|bind|inhibit|activat|molecular",
         {"MOLECULARLY_TARGETS", "FORMULA_ACTS_VIA", "PARTICIPATES_IN_PATHWAY",
          "HAS_GO_PROCESS", "HAS_GO_FUNCTION", "GENE_EXPRESSED_IN", "GENE_INTERACTS_GENE"}),
        (r"co-admin|combination|interaction|pharmacokinetic|auc|cyp|toxicity",
         {"DDI", "HDI", "HHI", "INTERACTS_WITH", "COMPLEMENTS_WM_THERAPY", "REGIMEN_INCLUDES"}),
        (r"herb|formula|decoction|extract|traditional chinese|中医|中药|方剂",
         {"HERB_CONTAINS_INGREDIENT", "FORMULA_CONTAINS_HERB", "TREATS", "INTEGRATIVE_TREATS"}),
        (r"trial|study|article|literature|cohort|randomi[sz]",
         {"STUDIES", "SUPPORTED_BY_LITERATURE"}),
    )
    if not compact:
        for pattern, relations in groups:
            if __import__("re").search(pattern, low):
                selected.update(relations)
    return [rel for rel in PRIORITY_RELATIONS if rel in selected and rel in schema.relation_types] + [
        rel for rel in schema.relation_types if rel in selected and rel not in PRIORITY_RELATIONS]


def _relation_menu(schema, text: str = "", compact: bool = False) -> str:
    lines = []
    ordered = (_routed_relations(schema, text, compact) if text or compact else
               [rel for rel in PRIORITY_RELATIONS if rel in schema.relation_types] +
               [rel for rel in schema.relation_types if rel not in PRIORITY_RELATIONS])
    for rel in ordered:
        spec = schema.relation_types.get(rel)
        if not spec:
            continue
        dom = "/".join(spec.get("domain", []) or ["*"])
        rng = "/".join(spec.get("range", []) or ["*"])
        desc = (spec.get("desc", "") or "").replace("\n", " ")[:80]
        qualifiers = ", ".join(spec.get("qualifiers", []) or []) or "none"
        lines.append(f"- {rel}: {dom} -> {rng}; qualifiers=[{qualifiers}]; {desc}")
    return "关系菜单（relation: domain -> range；qualifiers 只能取该行白名单）：\n" + "\n".join(lines)


def _entity_menu(schema, relations: list[str] | None = None) -> str:
    lines = []
    allowed = None
    if relations is not None:
        allowed = set()
        for relation in relations:
            spec = schema.relation_types.get(relation, {})
            allowed.update(spec.get("domain", []) or [])
            allowed.update(spec.get("range", []) or [])
    for name, spec in schema.entity_types.items():
        if allowed is not None and name not in allowed:
            continue
        desc = (spec.get("desc", "") or "").replace("\n", " ")[:80]
        enum_values = spec.get("enum", []) or []
        enum_text = f"; enum={enum_values}" if enum_values else ""
        lines.append(f"- {name}{enum_text}; {desc}")
    return "当前 schema 实体类型（只能逐项选择，不能用斜杠组合）：\n" + "\n".join(lines)


_FEWSHOT = """示例 1——成分关系；通路抑制不升级为直接靶点：
<UNIT>
Astragaloside IV, the major active constituent of Astragalus membranaceus, ameliorated renal fibrosis in diabetic nephropathy by inhibiting the TGF-beta1/Smad3 signaling pathway.
</UNIT>
正确输出：
{"triples":[
 {"head":"Astragalus membranaceus","head_type":"Herb","relation":"HERB_CONTAINS_INGREDIENT",
  "tail":"Astragaloside IV","tail_type":"MolecularEntity",
  "evidence_sentence":"Astragaloside IV, the major active constituent of Astragalus membranaceus","qualifiers":{}}
]}

示例 2——有确定实验结果的中药-西药相互作用：
<UNIT>
Co-administration of Astragalus membranaceus extract with metformin significantly enhanced the hypoglycemic effect in diabetic rats. The area under the curve (AUC) of metformin increased by 45% when combined with the herbal extract, suggesting a potential herb-drug pharmacokinetic interaction via CYP3A4 inhibition.
</UNIT>
正确输出：
{"triples":[
 {"head":"Astragalus membranaceus extract","head_type":"HerbProcessed","relation":"HDI",
  "tail":"metformin","tail_type":"DrugWestern",
  "evidence_sentence":"Co-administration of Astragalus membranaceus extract with metformin significantly enhanced the hypoglycemic effect",
  "qualifiers":{"interaction_type":"synergistic","effect_description":"enhanced hypoglycemic effect","evidence_grade":"in_vivo"}}
]}

示例 3——研究目的不是疗效：
<UNIT>
To find out the mechanism of Tangfukang on the diabetic nephropathy (DN).
</UNIT>
正确输出：
{"triples": []}

示例 4——比较不是联用：
<UNIT>
Both Tangfukang and aminoguanidine significantly decreased the expression of AGEs, but Tangfukang was greatly superior to aminoguanidine in suppressing the expression of cytokine like ICAM-1, IL-1 beta, TNF-alpha.
</UNIT>
正确输出：
{"triples": []}

示例 5——括号内拉丁学名是同义词，不是成分：
<UNIT>
Huangqi (Astragalus membranaceus) and Dang Gui (Angelica sinensis) are two of the most widely used herbs in traditional Chinese medicine.
</UNIT>
正确输出：
{"triples": []}"""


def build_prompt(schema, unit: dict,
                 entity_type_memory: dict[str, str] | None = None,
                 compact: bool = False) -> tuple[str, str]:
    """返回 (system, user)。user 内嵌 <UNIT>...</UNIT>，供 mock 与真实模型统一解析。"""
    ctx = f"论文章节：{' > '.join(unit.get('chapter_path', []))}（section_type={unit.get('section_type')}）"
    memory = entity_type_memory or {}
    memory_text = "\n".join(
        f"- {name} => {typ}" for name, typ in sorted(memory.items())
    ) or "(none yet; post-processing will enforce consistency)"
    relations = _routed_relations(schema, unit.get("text", ""), compact)
    entity_hint = "" if compact else _ENTITY_HINT
    fewshot = "" if compact else _FEWSHOT
    user = f"""{entity_hint}

{_entity_menu(schema, relations)}

{_relation_menu(schema, unit.get('text', ''), compact)}

<ENTITY_TYPE_MEMORY>
{memory_text}
</ENTITY_TYPE_MEMORY>

{fewshot}

现在抽取下面这个单元。{ctx}
提示：全文明确陈述且非推测的医学事实均可抽；研究目的、假设和无显著结果不抽。
<UNIT>
{unit['text']}
</UNIT>
只输出 JSON。"""
    return SYSTEM, user
