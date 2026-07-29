# -*- coding: utf-8 -*-
"""统一 ID 生成 —— 与 medkg 逐字节一致（务必不要修改）。

融合前提：同一实体（如"黄芪"/"黃芪"/"黄 芪"）在本包与 medkg 里必须得到**相同的 node_id**，
两张图才能按 node_id 合并。因此 normalize_name / sha256_id / node_id / edge_id / unit_id
是从 medkg `src/medkg/common/{io,ids}.py` 原样搬来的，行为必须保持一致。

  node_id = sha256_16("node" + normalize_name(name) + type)
  edge_id = sha256_16(relation + head_id + tail_id)
  unit_id = sha256_16(source_id + str(ordinal) + text_head[:64])
"""
from __future__ import annotations

import hashlib
import re

# ── 繁→简 归一（实体名去碎片化：黃芪/黄芪 必须归到同一节点）──
# 优先 zhconv（纯 Python 依赖）；缺失时退回无歧义高频中医字表（与 medkg 同表）。
_T2S_FALLBACK = {
    "黃": "黄", "蘇": "苏", "龍": "龙", "風": "风", "葉": "叶", "蓮": "莲", "薑": "姜", "棗": "枣",
    "藥": "药", "蒼": "苍", "蔘": "参", "參": "参", "歸": "归", "術": "术", "澤": "泽", "瀉": "泻",
    "鮮": "鲜", "飲": "饮", "蓋": "盖", "蘆": "芦", "蓯": "苁", "連": "连", "殼": "壳", "蟬": "蝉",
    "蠶": "蚕", "膽": "胆", "腎": "肾", "脹": "胀", "癰": "痈", "瘡": "疮", "嘔": "呕", "滯": "滞",
    "溫": "温", "涼": "凉", "補": "补", "寫": "写", "點": "点", "經": "经", "絡": "络", "證": "证",
    "體": "体", "氣": "气", "陰": "阴", "陽": "阳", "虛": "虚", "實": "实", "熱": "热", "標": "标",
    "鬱": "郁", "癥": "症", "瀋": "沈", "瀝": "沥", "頭": "头", "腦": "脑", "腫": "肿", "癢": "痒",
    "髮": "发", "癇": "痫", "癆": "痨", "瘧": "疟", "痺": "痹", "蘗": "檗", "棶": "梾",
}
_ZHCONV = None  # 懒加载缓存（None=未试, False=不可用, callable=可用）


def to_simplified(s: str) -> str:
    """转简体并归一异体，避免同一实体因繁简/异体分裂成多个 KG 节点。"""
    if not s:
        return s
    global _ZHCONV
    if _ZHCONV is None:
        try:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                from zhconv import convert
            _ZHCONV = convert
        except Exception:
            _ZHCONV = False
    if _ZHCONV:
        try:
            s = _ZHCONV(s, "zh-cn")
        except Exception:
            pass
    if any(c in _T2S_FALLBACK for c in s):
        s = "".join(_T2S_FALLBACK.get(c, c) for c in s)
    return s


_NAME_STRIP = "。，、,.;；:：「」『』\"'()（） "
_NAME_WS = re.compile(r"\s+")


def normalize_name(s: str) -> str:
    """KG 实体名归一（唯一实现）：去首尾标点/空白 + 去内部空白 + 繁→简。

    注意：英文实体名（西药/基因/成分）大小写敏感——本函数不改大小写。
    团队约定：西药/基因/成分统一用**规范英文名**或权威编码作 canonical_name，
    避免 aspirin/Aspirin 分裂（见 docs/OUTPUT_CONTRACT.md §实体命名约定）。
    """
    s = (s or "").strip().strip(_NAME_STRIP)
    s = _NAME_WS.sub("", s)
    return to_simplified(s)


def sha256_id(*parts: str, n: int = 16) -> str:
    """对拼接做 sha256 取前 n 位 hex —— node_id / edge_id / unit_id 去重。"""
    h = hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()
    return h[:n]


def node_id(name: str, ntype: str) -> str:
    return sha256_id("node", normalize_name(name), ntype or "")


def edge_id(relation: str, head_id: str, tail_id: str) -> str:
    return sha256_id(relation or "", head_id or "", tail_id or "")


def unit_id(source_id: str, ordinal: int, text_head: str = "") -> str:
    """语义单元 id：来源 + 序号 + 文本头，稳定可复现。"""
    return sha256_id(source_id or "", str(ordinal), (text_head or "")[:64])
