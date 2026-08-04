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
import threading
import time
from pathlib import Path


def load_env_file(path: str | Path) -> None:
    """Minimal python-dotenv fallback; never overrides process environment."""
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


# The package declares python-dotenv, but production environments may be
# partially provisioned. Keep model configuration usable without that extra.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    load_env_file(Path(__file__).resolve().parent.parent / ".env")


# 跨调用的限流保护
_MIN_INTERVAL = 1.0  # 秒
_rate_lock = threading.Lock()
_last_calls: dict[str, float] = {}


def _rate_limit(provider_key: str, min_interval: float = _MIN_INTERVAL):
    with _rate_lock:
        now = time.time()
        last_call = _last_calls.get(provider_key, 0.0)
        gap = min_interval - (now - last_call)
        if gap > 0:
            time.sleep(gap)
        _last_calls[provider_key] = time.time()


def _retry_after(error) -> float | None:
    headers = getattr(getattr(error, "response", None), "headers", None)
    if headers:
        try:
            value = headers.get("retry-after") or headers.get("Retry-After")
            return max(0.0, float(value)) if value is not None else None
        except (TypeError, ValueError):
            return None
    return None


class LLMClient:
    def __init__(self, mock: bool = False,
                 base_url: str | None = None,
                 api_key: str | None = None,
                 model: str | None = None,
                 temperature: float | None = None,
                 min_interval: float | None = None):
        self.mock = mock
        self.model = "mock" if mock else (model or os.environ.get("LLM_MODEL", ""))
        self.metrics = {"requests": 0, "successes": 0, "failures": 0,
                        "retries": 0, "rate_limits": 0, "latency_seconds": 0.0}
        self.metrics.update({"prompt_tokens": 0, "completion_tokens": 0,
                             "total_tokens": 0})
        self._metrics_lock = threading.Lock()
        if mock:
            return
        # 构造参数优先，环境变量兜底
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "")
        self.min_interval = (min_interval if min_interval is not None else
                             float(os.environ.get("LLM_MIN_INTERVAL", str(_MIN_INTERVAL))))
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
            result = _mock_extract(user)
            result["_model_used"] = self.model
            return result
        import random as _random
        use_response_format = os.environ.get("LLM_RESPONSE_FORMAT", "1") not in ("0", "no", "false")
        last = None
        for attempt in range(retries):
            # Per-provider throttle is shared across concurrent paper workers.
            _rate_limit(self.base_url, self.min_interval)
            started = time.monotonic()
            with self._metrics_lock:
                self.metrics["requests"] += 1
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
                result = _loads_lenient(txt)
                result["_model_used"] = self.model
                usage = getattr(resp, "usage", None)
                with self._metrics_lock:
                    self.metrics["successes"] += 1
                    self.metrics["latency_seconds"] += time.monotonic() - started
                    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        self.metrics[field] += int(getattr(usage, field, 0) or 0)
                return result
            except Exception as e:
                last = e
                err_str = str(e).lower()
                with self._metrics_lock:
                    self.metrics["failures"] += 1
                    self.metrics["latency_seconds"] += time.monotonic() - started
                    if "429" in err_str or "rate limit" in err_str:
                        self.metrics["rate_limits"] += 1
                if any(kw in err_str for kw in
                       ("401", "unauthorized", "403", "forbidden",
                        "invalid_request_error", "invalid_api_key")):
                    raise RuntimeError(f"LLM 不可重试错误: {e}") from e

                # Connection failures rarely benefit from five long attempts;
                # switch to the configured fallback after two.
                if attempt >= 1 and any(kw in err_str for kw in
                                        ("timeout", "timed out", "connection", "dns")):
                    break

                if attempt < retries - 1:
                    delay = _retry_after(e)
                    if delay is None:
                        delay = 2.0 * (attempt + 1) + _random.uniform(0, 3)
                    with self._metrics_lock:
                        self.metrics["retries"] += 1
                    print(f"      ⚠ LLM 重试 {attempt+1}/{retries}，{delay:.0f}s 后: "
                          f"{str(e)[:80]}",
                          file=sys.stderr, flush=True)
                    time.sleep(delay)
        raise RuntimeError(f"LLM 调用失败（{retries} 次重试后）: {str(last)[:200]}")


class FallbackLLMClient:
    """Try the primary extraction model, then a fallback model on failure."""

    def __init__(self, primary: LLMClient, fallback: LLMClient):
        self.primary = primary
        self.fallback = fallback
        self.model = primary.model
        self.model_chain = [primary.model, fallback.model]
        self.usage_counts = {primary.model: 0, fallback.model: 0}
        self.fallback_reasons: dict[str, int] = {}
        self._consecutive_primary_failures = 0
        self._circuit_open_until = 0.0
        self._lock = threading.Lock()
        self._failure_threshold = int(os.environ.get("LLM_CIRCUIT_FAILURES", "3"))
        self._cooldown = float(os.environ.get("LLM_CIRCUIT_COOLDOWN", "300"))

    def complete_json(self, system: str, user: str, retries: int = 5) -> dict:
        with self._lock:
            circuit_open = time.monotonic() < self._circuit_open_until
        primary_error_text = ""
        if not circuit_open:
            try:
                result = self.primary.complete_json(system, user, retries=retries)
                with self._lock:
                    self.usage_counts[self.primary.model] += 1
                    self._consecutive_primary_failures = 0
                return result
            except Exception as primary_error:
                primary_error_text = str(primary_error)
                reason = type(primary_error).__name__
                with self._lock:
                    self._consecutive_primary_failures += 1
                    self.fallback_reasons[reason] = self.fallback_reasons.get(reason, 0) + 1
                    if self._consecutive_primary_failures >= self._failure_threshold:
                        self._circuit_open_until = time.monotonic() + self._cooldown
        else:
            primary_error_text = "SenseNova circuit breaker is open"
            with self._lock:
                self.fallback_reasons["circuit_open"] = self.fallback_reasons.get("circuit_open", 0) + 1
        print(
            f"      ⚠ 主抽取模型 {self.primary.model} 失败，切换到 {self.fallback.model}: "
            f"{primary_error_text[:120]}",
            file=sys.stderr,
            flush=True,
        )
        result = self.fallback.complete_json(system, user, retries=retries)
        with self._lock:
            self.usage_counts[self.fallback.model] += 1
        return result

    def metrics_snapshot(self) -> dict:
        return {
            "usage": dict(self.usage_counts),
            "fallback_reasons": dict(self.fallback_reasons),
            "primary": dict(self.primary.metrics),
            "fallback": dict(self.fallback.metrics),
            "circuit_open": time.monotonic() < self._circuit_open_until,
        }


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


# ── 抽取与 NLI 客户端工厂 ──

def create_extraction_client(mock: bool = False):
    """Create SenseNova-primary extraction with the legacy LLM as fallback.

    ``SENSENOVA_*`` configures the primary model. Existing ``LLM_*`` variables
    remain the fallback configuration, so current DeepSeek deployments continue
    to work without renaming secrets.
    """
    if mock:
        return LLMClient(mock=True)

    fallback = LLMClient()
    provider = os.environ.get("EXTRACTION_PROVIDER", "sensenova").strip().lower()
    if provider == "deepseek":
        return fallback
    if provider not in ("sensenova", "auto"):
        raise RuntimeError("EXTRACTION_PROVIDER 只能是 sensenova、auto 或 deepseek")
    sense_key = os.environ.get("SENSENOVA_API_KEY") or os.environ.get("NLI_API_KEY", "")
    if not sense_key:
        print("      ⚠ 未配置 SENSENOVA_API_KEY，抽取仅使用 LLM_* fallback",
              file=sys.stderr, flush=True)
        return fallback

    primary = LLMClient(
        base_url=os.environ.get("SENSENOVA_BASE_URL", "https://token.sensenova.cn/v1"),
        api_key=sense_key,
        model=os.environ.get("SENSENOVA_MODEL", "sensenova-6.7-flash-lite"),
        temperature=0,
        min_interval=float(os.environ.get("SENSENOVA_MIN_INTERVAL", "0.2")),
    )
    return FallbackLLMClient(primary, fallback)

def create_nli_client(fallback_llm=None):
    """创建 NLI 专用 LLM 客户端。

    优先使用 NLI_* 环境变量，未设置时使用默认 SenseNova 轻量模型。

    环境变量（可选）：
        NLI_BASE_URL   NLI 模型 API 地址
        NLI_API_KEY    NLI 模型 API key
        NLI_MODEL      NLI 模型名称
    """
    nli_url = os.environ.get(
        "NLI_BASE_URL", os.environ.get("SENSENOVA_BASE_URL", "https://token.sensenova.cn/v1")
    )
    nli_key = os.environ.get("NLI_API_KEY") or os.environ.get("SENSENOVA_API_KEY", "")
    nli_model = os.environ.get(
        "NLI_MODEL", os.environ.get("SENSENOVA_MODEL", "sensenova-6.7-flash-lite")
    )

    try:
        if not nli_key:
            raise RuntimeError("未配置 NLI_API_KEY 或 SENSENOVA_API_KEY")
        primary = LLMClient(
            base_url=nli_url,
            api_key=nli_key,
            model=nli_model,
            temperature=0,
        )
        if fallback_llm is not None:
            deepseek = getattr(fallback_llm, "fallback", fallback_llm)
            if deepseek is not primary:
                return FallbackLLMClient(primary, deepseek)
        return primary
    except Exception as e:
        print(f"      ⚠ NLI 专用模型初始化失败 ({e})，回退到主模型",
              file=sys.stderr, flush=True)

    # 回退：复用主抽取 LLM
    if fallback_llm is not None:
        # Extraction may itself be a SenseNova→DeepSeek chain. NLI fallback
        # should go directly to DeepSeek instead of retrying SenseNova again.
        return getattr(fallback_llm, "fallback", fallback_llm)
    return None
