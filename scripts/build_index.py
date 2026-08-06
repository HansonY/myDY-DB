#!/usr/bin/env python
"""建 / 同步知识库的向量索引。**零网络请求**(本地模型),没有 403 风险。

    .venv/bin/python scripts/build_index.py            # 只补新片段(日常)
    .venv/bin/python scripts/build_index.py --rebuild   # 全量重建(换模型/改分块后)
    .venv/bin/python scripts/build_index.py --status     # 只看现状,不加载模型
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import _pyversion
_pyversion.check()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="全量重建(换模型时必须)")
    ap.add_argument("--status", action="store_true", help="只看现状")
    args = ap.parse_args()

    from knowledge import index

    if args.status:
        s = index.status()
        print(f"索引模型   {s['model'] or '(还没建)'}"
              + (f"  {s['dim']} 维" if s['dim'] else ""))
        print(f"配置模型   {s['configured_model']}")
        print(f"向量 / 片段 {s['vectors']} / {s['fragments']}")
        print(f"待嵌入     {s['pending']}")
        if s["orphans"]:
            print(f"孤儿向量   {s['orphans']}  ← 片段重建过,下次 sync 会清掉")
        print(f"状态       {'✓ 已同步' if s['in_sync'] else '需要跑一次(见上面的待嵌入 / 模型不符)'}")
        return

    last = [0]
    def prog(done, total):
        if done - last[0] >= 512 or done == total:
            last[0] = done
            print(f"  {done}/{total} 段…", flush=True)

    r = index.sync(rebuild=args.rebuild, on_progress=prog)
    print(f"\n✓ {r['model']}({r['dim']} 维)")
    print(f"  本轮嵌入 {r['embedded']} 段 · 耗时 {r['seconds']}s")
    if r["orphans_removed"]:
        print(f"  清掉孤儿向量 {r['orphans_removed']} 条(片段被重建过)")
    print(f"  索引里共 {r['total_vectors']} 条向量")


if __name__ == "__main__":
    main()
