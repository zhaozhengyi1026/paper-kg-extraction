# -*- coding: utf-8 -*-
"""NLI 语义校验闸门 —— 判断 evidence 是否蕴含三元组声明。

设计原则：
  - 以 evidence_sentence 为 premise，三元组转自然语言为 hypothesis
  - 一个 unit 内所有需校验的三元组批量一次 API 调用
  - entailment 通过 / contradiction 拒绝 / neutral 降级
  - 复用抽取 LLM，不引入额外模型依赖
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
#  Relation → 自然语言 hypothesis 模板
# ═══════════════════════════════════════════════════════════════

_RELATION_TEMPLATES: Dict[str, str] = {
    "TREATS": "“{head}”对“{tail}”有明确的治疗或预防效果",
    "WM_TREATS": "“{head}”对“{tail}”有明确的治疗或预防效果",
    "HERB_CONTAINS_INGREDIENT": "“{head}”含有化学成分“{tail}”",
    "MOLECULARLY_TARGETS": "“{head}”靶向或调控“{tail}”",
    "COMPLEMENTS_WM_THERAPY": "“{head}”与“{tail}”联用可增强疗效",
    "HAS_PHENOTYPE": "“{head}”表现为“{tail}”这一表型特征",
    "GENE_ASSOCIATED_WITH_DISEASE": "“{head}”基因与“{tail}”疾病之间存在因果关联",
    "HAS_FUNCTION": "“{head}”具有“{tail}”这一功能",
    "FORMULA_ACTS_VIA": "“{head}”通过“{tail}”发挥作用",
}

# ═══════════════════════════════════════════════════════════════
#  需要 NLI 校验的 relation（P0 + P1）
# ═══════════════════════════════════════════════════════════════

# TREATS/WM_TREATS 不做 NLI：prompt 铁律7 + 反例 few-shot 已覆盖"研究目的"场景，
# sensenova 小模型过于保守，会把明确疗效陈述也标 neutral，过度降级。
# CONTAINS/TARGETS/COMPLEMENTS 是 NLI 真正能拦截的（括号陷阱、we investigated）
NLI_RELATIONS: set = {
    "HERB_CONTAINS_INGREDIENT",
    "MOLECULARLY_TARGETS",
    "COMPLEMENTS_WM_THERAPY",
    "HAS_PHENOTYPE", "GENE_ASSOCIATED_WITH_DISEASE",
    "HAS_FUNCTION", "FORMULA_ACTS_VIA",
}

# NLI label → 动作映射
_NLI_CONTRADICTION = "contradiction"
_NLI_NEUTRAL = "neutral"
_NLI_ENTAILMENT = "entailment"

# neutral 降级系数
_NEUTRAL_CONFIDENCE_FACTOR = 0.6


def needs_nli(relation: str) -> bool:
    """判断一个 relation 是否需要 NLI 语义校验。"""
    return relation in NLI_RELATIONS


# ═══════════════════════════════════════════════════════════════
#  NLI 前置跳过规则：evidence 已包含明确断言词 → 跳过 NLI，直接视为 entailment
# ═══════════════════════════════════════════════════════════════

# 明确疗效/靶向/包含关系的断言模式（大小写不敏感）
_CLEAR_CLAIM_PATTERNS = [
    # 疗效
    r"\bimproved?\b", r"\breduced?\b", r"\bdecreased?\b", r"\bameliorated?\b",
    r"\bsignificantly\b", r"\beffectively\b", r"\btherapeutic\s+effect\b",
    r"\bprotective\s+effect\b", r"\bbeneficial\s+effect\b",
    r"\btreated?\b", r"\bprevent(ed|s)?\b", r"\bdelayed?\b",
    r"\bcan\s+(improve|reduce|decrease|treat|prevent)\b",
    # 成分
    r"\b(constituent|component|extracted|purified|isolated)\s+(of|from)\b",
    r"\b(major|main|active|bioactive)\s+(component|constituent|substance)\b",
    r"\b(derived|obtained)\s+from\b",
    r"\bis\s+(a|the)\s+(major|main|active|bioactive)\b",
    r"\b(activity|effect|role)\s+of\s+component\b",
    r"\bcomponent\s+\w+\b",  # "component quercetin"
    # 靶向
    r"\b(inhibited|suppressed|down[-\s]?regulated|up[-\s]?regulated)\b",
    r"\b(expression|phosphorylation|activation)\s+(was|is|of)\b",
    r"\b(raised|induced|reduced|blocked)\s+by\b",
    r"\binteracts?\s+with\b", r"\boverexpressed?\s+in\b",
    # 成分（追加）
    r"\bcomponents?\s+(of|in)\b", r"\bcomponent\s+(of|in)\b",
    r"\bconstituents?\s+(of|in)\b",
    # 联用
    r"\bcombined\s+(with|use|therapy)\b", r"\bco[-\s]?admin(istration|istered)\b",
    r"\b(in\s+addition|as\s+an\s+adjunct)\s+to\b",
    r"\b(combination|adjunctive)\s+(therapy|use|treatment)\b",
]

_CLEAR_PAT_RE = None


def _get_clear_re():
    global _CLEAR_PAT_RE
    if _CLEAR_PAT_RE is None:
        _CLEAR_PAT_RE = __import__('re').compile(
            '|'.join(_CLEAR_CLAIM_PATTERNS), __import__('re').IGNORECASE
        )
    return _CLEAR_PAT_RE


def skip_nli(evidence: str, relation: str) -> bool:
    """判断是否应跳过 NLI（evidence 已包含明确断言）。

    如果 evidence 中明显包含疗效/成分/靶向的确定性表述，
    则直接视为 entailment，不浪费 API 调用。

    用于降低 sensenova 小模型过于保守导致的误降级。
    """
    if not evidence:
        return False
    return bool(_get_clear_re().search(evidence))


def build_hypothesis(head: str, relation: str, tail: str) -> str | None:
    """将三元组转换为 NLI hypothesis 自然语言文本。

    返回 None 表示该 relation 没有模板（不应调用此函数）。
    """
    tmpl = _RELATION_TEMPLATES.get(relation)
    if not tmpl:
        return None
    return tmpl.format(head=head, tail=tail)


def _nli_system_prompt() -> str:
    return (
        "你是自然语言推理 (NLI) 评估器。你的任务是判断：给定一个科学证据句（前提），"
        "它是否在语义上必然蕴含一个声明（假设）？\n\n"
        "规则：\n"
        "1. entailment: 前提中明确、直接地陈述了假设的内容。前提足够作为假设的证据。\n"
        "2. contradiction: 前提直接否定或与假设有明显矛盾。\n"
        "3. neutral: 前提不包含足够信息来确认或否定假设。这包括：\n"
        "   - 前提只是描述研究目的/方法（如 \"To study...\", \"we investigated...\"）\n"
        "   - 前提使用弱推测语言（如 \"may be...\", \"might be associated with...\"）\n"
        "   - 前提与假设相关但不足以证明假设\n"
        "   - 前提中括号内是名称注释/同义词说明（不是成分定义）\n\n"
        "只输出 JSON 对象，格式：{\"results\": [{\"label\": \"entailment|neutral|contradiction\"}]}"
    )


def batch_check(
    premise: str,
    hypotheses: list[str],
    llm,
    fallback_llm=None,
    retries: int = 2,
) -> list[dict]:
    """对同一个 premise 批量检查多条 hypothesis。

    支持双模型 fallback：先用 llm（NLI 专用模型），失败则切 fallback_llm。

    Args:
        premise: 证据句（论文原文）
        hypotheses: 假设列表（自然语言三元组声明）
        llm: 主 NLI LLMClient 实例
        fallback_llm: 备用 LLMClient（通常为主抽取模型），失败时自动切换
        retries: 每个模型的 NLI 重试次数

    Returns:
        list of dict: [{"label": "entailment"|"neutral"|"contradiction"}, ...]
        与 hypotheses 按索引一一对应。全部失败时返回 neutral 作为安全兜底。
    """
    if not hypotheses:
        return []
    if llm is None and fallback_llm is None:
        return [{"label": "neutral"} for _ in hypotheses]

    hypotheses_text = "\n".join(
        f"{i+1}. {h}" for i, h in enumerate(hypotheses)
    )
    user = (
        f"前提 (premise):\n\"{premise}\"\n\n"
        f"假设 (hypotheses):\n{hypotheses_text}\n\n"
        f"对每条假设，判断它是 entailment / neutral / contradiction。"
    )
    system = _nli_system_prompt()

    # 轮询多个 LLM：主模型 → fallback
    models_to_try = [m for m in (llm, fallback_llm) if m is not None]
    for model_idx, current_llm in enumerate(models_to_try):
        model_label = _model_name(current_llm)
        for attempt in range(retries):
            try:
                resp = current_llm.complete_json(system, user)
                if isinstance(resp, dict) and "results" in resp:
                    results = resp["results"]
                    if isinstance(results, list) and len(results) == len(hypotheses):
                        valid = {"entailment", "neutral", "contradiction"}
                        for r in results:
                            if not isinstance(r, dict) or r.get("label") not in valid:
                                r["label"] = "neutral"
                        if model_idx > 0:
                            _log.info("NLI fallback to %s succeeded", model_label)
                        return results
            except Exception:
                _log.warning("NLI %s attempt %d failed", model_label, attempt + 1)
        _log.warning("NLI model %s exhausted all retries", model_label)

    # 全部失败 → 安全兜底
    _log.error("NLI all models failed, defaulting to neutral")
    return [{"label": "neutral"} for _ in hypotheses]


def _model_name(llm) -> str:
    """获取 LLM 客户端的短名称用于日志。"""
    try:
        return getattr(llm, "model", "unknown") or "unknown"
    except Exception:
        return "unknown"


class NLICache:
    """NLI 结果缓存：按 unit_id 存储，同一 unit 内多条 triple 共享一次 NLI 调用。

    用法：
        cache = NLICache()
        # 在 _make_edge 中：
        result = cache.get_or_compute(
            unit_id, evidence_sentence, hyp_key, hypothesis, llm
        )
    """

    def __init__(self):
        # unit_id → {"premise": str, "results": {hyp_key: label}}
        self._cache: dict = {}

    def get_or_compute(
        self,
        unit_id: str,
        premise: str,
        hyp_key: str,
        hypothesis: str,
        llm,
    ) -> str:
        """获取 NLI 结果，若未缓存则加入待处理队列。返回 label 字符串。

        注意：实际批量调用发生在 precompute() 中，此方法只注册查询。
        因此需要先调用 register() 注册所有查询，再调用 precompute() 批量执行。
        """
        if unit_id not in self._cache:
            self._cache[unit_id] = {"premise": premise, "pending": {}}

        entry = self._cache[unit_id]
        if hyp_key in entry.get("results", {}):
            return entry["results"][hyp_key]

        # 注册为 pending
        entry.setdefault("pending", {})[hyp_key] = hypothesis
        return None  # 表示需要等待 precompute

    def register(self, unit_id: str, premise: str, hyp_key: str, hypothesis: str):
        """注册一条 NLI 查询，稍后 precompute() 批量执行。"""
        if unit_id not in self._cache:
            self._cache[unit_id] = {"premise": premise, "pending": {}, "results": {}}
        entry = self._cache[unit_id]
        entry["pending"][hyp_key] = hypothesis

    def precompute(self, unit_id: str, llm) -> dict:
        """批量执行 unit_id 下所有 pending 的 NLI 查询。

        调用后，该 unit 的 pending 清空，results 填充完毕。
        返回 results dict。
        """
        entry = self._cache.get(unit_id)
        if not entry or not entry.get("pending"):
            return {}

        premise = entry["premise"]
        pending = entry["pending"]
        keys = list(pending.keys())
        hypotheses = [pending[k] for k in keys]

        results_list = batch_check(premise, hypotheses, llm)
        for k, r in zip(keys, results_list):
            entry.setdefault("results", {})[k] = r.get("label", "neutral")

        entry["pending"] = {}
        return dict(entry["results"])

    def get(self, unit_id: str, hyp_key: str) -> str | None:
        """获取已计算的 NLI 结果。未计算则返回 None。"""
        entry = self._cache.get(unit_id, {})
        return entry.get("results", {}).get(hyp_key)


def apply_nli_result(
    label: str,
    stats: dict,
    reject_log: list,
    warn_log: list,
    reject_fn,
    warn_fn,
) -> tuple[bool, float]:
    """根据 NLI label 返回 (should_reject, confidence_factor)。

    - entailment:   (False, 1.0)    通过
    - contradiction: (True, 0.0)    拒绝
    - neutral:      (False, 0.6)    降级 + warning
    """
    if label == _NLI_CONTRADICTION:
        return True, 0.0
    elif label == _NLI_NEUTRAL:
        return False, _NEUTRAL_CONFIDENCE_FACTOR
    else:  # entailment
        return False, 1.0
