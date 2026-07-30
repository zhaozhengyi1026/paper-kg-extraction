# -*- coding: utf-8 -*-
"""OpenAI 兼容 LLM 客户端（NVIDIA NIM / OpenRouter / OpenAI / 本地 vLLM / SenseNova 均可）。

通过环境变量配置，见 .env.example：
    LLM_BASE_URL   例：https://integrate.api.nvidia.com/v1
    LLM_API_KEY    你的 key
    LLM_MODEL      例：deepseek-ai/deepseek-v3 / qwen/qwen2.5-72b-instruct
    LLM_TEMPERATURE 默认 0（抽取要确定性）

也可通过构造函数参数覆盖（用于创建 NLI 等辅助模型客户端）：
    nli_llm = LLMClient(
        base_url="https://token.sensenova.cn/v1",
        api_key="sk-...",
        model="sensenova-6.7-flash-lite",
    )

提供 --mock 模式：不联网，用一个基于 schema 的确定性词典抽几条示例三元组，
供离线冒烟测试与示例复现（examples/ 用它生成，无需 API key）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time


# 跨调用的限流保护
_last_call = 0.0
_MIN_INTERVAL = 1.0  # 秒


def _rate_limit():
    global _last_call
    now = time.time()
    gap = _MIN_INTERVAL - (now - _last_call)
    if gap > 0:
        time.sleep(gap)
    _last_call = time.time()


class LLMClient:
    def __init__(self, mock: bool = False,
                 base_url: str | None = None,
                 api_key: str | None = None,
                 model: str | None = None,
                 temperature: float | None = None):
        self.mock = mock
        if mock:
            return
        # 构造参数优先，环境变量兜底
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "")
        self.model = model or os.environ.get("LLM_MODEL", "")
        self.temperature = (temperature if temperature is not None
                            else float(os.environ.get("LLM_TEMPERATURE", "0") or 0))
        if not (self.base_url and self.api_key and self.model):
            raise RuntimeError(
                "未配置 LLM。请在 .env 设 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL，或用 --mock 跑离线冒烟。")
        try:
            from openai import OpenAI
        except ImportError as e:
            raise RuntimeError("需要 openai 包：pip install openai>=1.0") from e
        self._client = OpenAI(base_url=self.base_url, api_key=self.api_key)

    def complete_json(self, system: str, user: str, retries: int = 5) -> dict:
        """调 chat.completions，返回 JSON 对象。

        失败自动重试：指数退避 + 随机抖动，最多 retries 次。
        可重试错误：网络超时、429 限流、5xx 服务端错误。
        不可重试：401 认证错误、400 参数错误（直接抛）。
        """
        if self.mock:
            return _mock_extract(user)
        # 限流保护：两次调用间隔 >= 1s
        _rate_limit()
        import random as _random
        use_response_format = os.environ.get("LLM_RESPONSE_FORMAT", "1") not in ("0", "no", "false")
        last = None
        for attempt in range(retries):
            try:
                kwargs = dict(
                    model=self.model,
                    temperature=self.temperature,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": user}],
                )
                if use_response_format:
                    kwargs["response_format"] = {"type": "json_object"}
                resp = self._client.chat.completions.create(**kwargs)
                txt = resp.choices[0].message.content or "{}"
                return _loads_lenient(txt)
            except Exception as e:
                last = e
                err_str = str(e).lower()
                if any(kw in err_str for kw in ("401", "unauthorized", "403", "forbidden",
                                                  "invalid_request_error", "invalid_api_key")):
                    raise RuntimeError(f"LLM 不可重试错误: {e}") from e

                if attempt < retries - 1:
                    delay = 2.0 * (attempt + 1) + _random.uniform(0, 3)
                    print(f"      ⚠ LLM 重试 {attempt+1}/{retries}，{delay:.0f}s 后: "
                          f"{str(e)[:80]}",
                          file=sys.stderr, flush=True)
                    time.sleep(delay)
        raise RuntimeError(f"LLM 调用失败（{retries} 次重试后）: {str(last)[:200]}")


def _loads_lenient(txt: str) -> dict:
    txt = txt.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", txt).strip()
    try:
        result = json.loads(txt)
    except json.JSONDecodeError:
        m = re.search(r"\{.*?\}", txt, re.S)
        if m:
            try:
                result = json.loads(m.group(0))
            except json.JSONDecodeError:
                result = _extract_balanced_json(txt)
                if result is None:
                    raise
        else:
            raise
    # NLI 响应有 "results" 而非 "triples"，不警告
    if "triples" not in result and "results" not in result:
        print(f"    WARN: LLM JSON 解析后无 triples/results 字段，raw={txt[:120]!r}",
              file=sys.stderr, flush=True)
    result.setdefault("triples", [])
    return result


def _extract_balanced_json(txt: str) -> dict | None:
    depth = 0
    start = -1
    for i, ch in enumerate(txt):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(txt[start:i + 1])
                except json.JSONDecodeError:
                    start = -1
                    continue
    return None


# ── mock ──
_MOCK_RULES = [
    (r"Astragalus(?:\s+membranaceus)?|黄芪|黃芪", "HERB_CONTAINS_INGREDIENT", "Herb",
     r"[Aa]stragaloside\s*IV|黄芪甲苷", "MolecularEntity"),
    (r"[Aa]stragaloside\s*IV|黄芪甲苷", "MOLECULARLY_TARGETS", "MolecularEntity",
     r"TGF-?β1", "Gene"),
]


def _mock_extract(user: str) -> dict:
    blocks = re.findall(r"<UNIT>\n(.*?)\n</UNIT>", user, re.S)
    text = blocks[-1] if blocks else user
    sentences = re.split(r"(?<=[.。;；])\s+", text)
    triples = []
    for h_pat, rel, h_type, t_pat, t_type in _MOCK_RULES:
        for sent in sentences:
            hm, tm = re.search(h_pat, sent), re.search(t_pat, sent)
            if hm and tm:
                triples.append({
                    "head": hm.group(0), "head_type": h_type,
                    "relation": rel,
                    "tail": tm.group(0), "tail_type": t_type,
                    "evidence_sentence": sent.strip(),
                    "qualifiers": {},
                })
                break
    return {"triples": triples}


# ── NLI 客户端工厂 ──

def create_nli_client(fallback_llm=None):
    """创建 NLI 专用 LLM 客户端。

    优先使用 NLI_* 环境变量，未设置时使用默认 SenseNova 轻量模型。

    环境变量（可选）：
        NLI_BASE_URL   NLI 模型 API 地址
        NLI_API_KEY    NLI 模型 API key
        NLI_MODEL      NLI 模型名称
    """
    nli_url = os.environ.get("NLI_BASE_URL", "https://token.sensenova.cn/v1")
    nli_key = os.environ.get("NLI_API_KEY", "sk-g49OmT4LIXNtgpvJndKL9CSO6zOkiTl5")
    nli_model = os.environ.get("NLI_MODEL", "sensenova-6.7-flash-lite")

    try:
        return LLMClient(
            base_url=nli_url,
            api_key=nli_key,
            model=nli_model,
            temperature=0,
        )
    except Exception as e:
        print(f"      ⚠ NLI 专用模型初始化失败 ({e})，回退到主模型",
              file=sys.stderr, flush=True)

    # 回退：复用主抽取 LLM
    if fallback_llm is not None:
        return fallback_llm
    return None
