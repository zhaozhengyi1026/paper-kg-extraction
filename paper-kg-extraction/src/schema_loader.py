# -*- coding: utf-8 -*-
"""轻量本体加载器 + 校验器（离线可用，无需安装 medkg）。

- 按 schema/registry.yaml 合并 core+tcm+wm+bridge+lit（复刻 medkg loader 的合并语义：
  给每个类型打 _domain 标签；跨域重名且不一致时报错）。
- 派生：带 enum 的实体类型 → 取值表；关系 range/domain 指向 enum 类型即"闭类"，值必须落在表内。
- validate(triple)：校验 relation 存在、head_type∈domain、tail_type∈range、闭类值合法。

这是**预检**（pre-flight）。最终权威校验以 medkg 的 `medkg build-graph`（含 Validator + 质量闸）为准。
"""
from __future__ import annotations

from pathlib import Path

import yaml

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"


class Schema:
    def __init__(self, base: Path | None = None):
        base = Path(base) if base else SCHEMA_DIR
        reg = yaml.safe_load((base / "registry.yaml").read_text(encoding="utf-8"))
        active = reg["domains"]["active"]
        files = reg["domains"]["files"]
        on_dup = (reg.get("merge_policy") or {}).get("on_duplicate_type", "error")

        self.entity_types: dict = {}
        self.relation_types: dict = {}
        self.canonical_vocabularies: dict = {}
        self.schema_version = "0.0.0"
        self.domains = active

        def merge(bucket: dict, items: dict, domain: str, label: str):
            for name, spec in (items or {}).items():
                spec = dict(spec or {})
                if name in bucket:
                    prev = {k: v for k, v in bucket[name].items() if k != "_domain"}
                    if prev != spec and on_dup == "error":
                        raise ValueError(
                            f"领域 {domain} 与 {bucket[name].get('_domain')} 重复定义 {label} '{name}' 且不一致")
                spec["_domain"] = domain
                bucket[name] = spec

        for domain in active:
            d = yaml.safe_load((base / files[domain]).read_text(encoding="utf-8"))
            if domain == "core":
                self.schema_version = d.get("schema_version", self.schema_version)
                self.node_properties = d.get("node_properties", {})
                self.edge_properties = d.get("edge_properties", {})
                self.evidence_tier_map = d.get("evidence_tier_map", {})
                self.confidence_model = d.get("confidence_model", {})
            merge(self.entity_types, d.get("entity_types"), domain, "entity")
            merge(self.relation_types, d.get("relation_types"), domain, "relation")
            self.canonical_vocabularies.update(d.get("canonical_vocabularies") or {})

        # 派生闭类
        self.enums: dict[str, list] = {
            k: list(v["enum"]) for k, v in self.entity_types.items() if v.get("enum")
        }

    # ── 校验 ──
    def relation_domain(self, rel: str) -> list:
        return self.relation_types.get(rel, {}).get("domain", [])

    def relation_range(self, rel: str) -> list:
        return self.relation_types.get(rel, {}).get("range", [])

    def _slot_ok(self, allowed: list, typ: str, value: str) -> tuple[bool, str]:
        if not allowed or "*" in allowed:
            return True, ""
        if typ not in allowed:
            return False, f"类型 {typ} 不在允许集 {allowed}"
        # 闭类：若该 slot 的允许类型带 enum，则 value 必须在 enum 内
        if typ in self.enums and value not in self.enums[typ]:
            return False, f"{typ} 是闭类，'{value}' 不在枚举 {self.enums[typ][:6]}…"
        return True, ""

    def validate(self, tr: dict) -> tuple[bool, str]:
        """校验一条三元组（已含 head/head_type/relation/tail/tail_type）。返回 (ok, reason)。"""
        rel = tr.get("relation", "")
        if rel not in self.relation_types:
            return False, f"未知关系 {rel!r}"
        ht, tt = tr.get("head_type", ""), tr.get("tail_type", "")
        if ht not in self.entity_types:
            return False, f"未知 head_type {ht!r}"
        if tt not in self.entity_types:
            return False, f"未知 tail_type {tt!r}"
        ok, why = self._slot_ok(self.relation_domain(rel), ht, tr.get("head", ""))
        if not ok:
            return False, f"domain 不符：{why}"
        ok, why = self._slot_ok(self.relation_range(rel), tt, tr.get("tail", ""))
        if not ok:
            return False, f"range 不符：{why}"
        return True, ""

    def relation_domain_of(self, rel: str) -> str:
        return self.relation_types.get(rel, {}).get("_domain", "")


if __name__ == "__main__":
    s = Schema()
    print(f"schema v{s.schema_version} | 域={s.domains} | 实体={len(s.entity_types)} 关系={len(s.relation_types)} 闭类={len(s.enums)}")
