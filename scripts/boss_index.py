#!/usr/bin/env python3
"""建 / 同步**岗位库**的向量索引。零网络(本地 bge-m3)。

    BOSS_DB_PATH=data/boss.db .venv/bin/python scripts/boss_index.py            # 补新片段
    BOSS_DB_PATH=data/boss.db .venv/bin/python scripts/boss_index.py --rebuild  # 全量重建
    BOSS_DB_PATH=data/boss.db .venv/bin/python scripts/boss_index.py --status   # 只看现状

**为什么不给 scripts/build_index.py 加个 --space 参数。**
那个脚本是抖音正在用的(6235 条向量),加 flag 就动了它;更糟的是 flag 打错
会**静默跑到抖音库上**去重建 —— 而重建是要几十秒 + 覆盖 vec_meta 的。
两个脚本各管一个库,抖音那侧 diff 为零。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import kb                                       # noqa: E402
from knowledge.boss_space import BOSS_SPACE     # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="岗位库向量索引")
    ap.add_argument("--rebuild", action="store_true", help="全量重建(换模型/改分块后)")
    ap.add_argument("--status", action="store_true", help="只看现状,不加载模型")
    args = ap.parse_args()

    K = kb.bind(BOSS_SPACE)

    if args.status:
        s = K.status()
        print(f"索引模型   {s['model'] or '(还没建)'}"
              + (f"  {s['dim']} 维" if s["dim"] else ""))
        print(f"配置模型   {s['configured_model']}")
        print(f"向量 / 片段 {s['vectors']} / {s['fragments']}")
        print(f"待嵌入     {s['pending']}")
        if s["orphans"]:
            print(f"孤儿向量   {s['orphans']}  ← 片段重建过,下次 sync 会清掉")
        print("状态       " + ("✓ 已同步" if s["in_sync"] else "· 需要跑一次"))
        return

    def prog(done: int, total: int) -> None:
        pct = done * 100 // max(total, 1)
        print(f"\r  嵌入 {done}/{total}  {pct}%", end="", flush=True)

    r = K.sync(rebuild=args.rebuild, on_progress=prog)
    print()
    print(f"模型 {r['model']}({r['dim']} 维)")
    print(f"新嵌入 {r['embedded']} 段 · 清掉孤儿 {r['orphans_removed']} 条 "
          f"· 索引共 {r['total_vectors']} 条 · {r['seconds']}s")
    if r["embedded"] == 0 and r["total_vectors"] == 0:
        print("\n库里一段片段都没有。先提取岗位(网页「提取」按钮),"
              "提取时会顺手生成片段。")


if __name__ == "__main__":
    main()
