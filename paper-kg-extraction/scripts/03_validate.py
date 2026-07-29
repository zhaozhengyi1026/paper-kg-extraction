# -*- coding: utf-8 -*-
"""验收：对候选 JSONL 跑预检，打印通过率与失败样例，非零退出码表示未通过。

用法：python scripts/03_validate.py [out/candidate_kg/paper_triples.jsonl]
验收标准：failed == 0（100% 通过预检）才可提交。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

for _s in (sys.stdout, sys.stderr):        # Windows 控制台默认 GBK，强制 UTF-8
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

from validate_output import validate_file  # noqa: E402


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "out" / "candidate_kg" / "paper_triples.jsonl")
    if not Path(target).exists():
        print(f"✗ 找不到 {target}，先跑 02_run_extraction.py")
        sys.exit(2)
    rep = validate_file(target)
    print(f"文件：{rep['file']}")
    print(f"总计 {rep['total']} 条 | 通过 {rep['passed']} | 失败 {rep['failed']} | 通过率 {rep['pass_rate']:.2%}")
    if rep["reasons"]:
        print("\n失败原因分布：")
        for k, v in rep["reasons"].items():
            print(f"  {v:>6}  {k}")
    if rep["samples"]:
        print("\n失败样例（前 15）：")
        for s in rep["samples"]:
            print(f"  L{s['line']} {s['edge_id']}: {'; '.join(s['errors'])}")
    if rep["failed"] == 0 and rep["total"] > 0:
        print("\n✓ 全部通过预检，可提交。")
        sys.exit(0)
    print("\n✗ 存在未通过的边，修复后重跑。")
    sys.exit(1)


if __name__ == "__main__":
    main()
