#!/usr/bin/env python
"""从本地 raw 拼出知识片段。**零网络请求**,可随时全量重建。

片段是派生数据 —— 源头永远是 videos.raw_z。所以改了拼装策略就重跑一遍,
不用重采、不冒 403。

用法:
    .venv/bin/python scripts/build_fragments.py
    .venv/bin/python scripts/build_fragments.py --limit 200    # 先小范围试
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import _pyversion
_pyversion.check()

from collector import douyin           # noqa: E402
from db import store                   # noqa: E402
from knowledge import fragments        # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    store.init_db()
    n = made = 0
    for aid, raw in store.iter_raw(only_full=True):
        video = store.get_video(aid) or {}
        content = douyin._content_from_raw(raw)
        tags = douyin._hashtags_from_raw(raw)
        made += store.save_fragments(aid, fragments.build(video, content, tags))
        n += 1
        if n % 200 == 0:
            print(f"  {n} 条 → {made} 段…", flush=True)
        if args.limit and n >= args.limit:
            break

    s = store.fragment_stats()
    print(f"\n✓ {n} 条作品 → {s['fragments']} 段(覆盖 {s['videos_covered']} 条)")
    print("  分段类型:", s["by_kind"])
    tot = s["thick"] + s["mid"] + s["thin"] or 1
    print(f"\n  按作品看片段总字数:")
    print(f"    够用(≥150字)  {s['thick']:>5}  {s['thick']*100/tot:.0f}%")
    print(f"    能用(60-149)  {s['mid']:>5}  {s['mid']*100/tot:.0f}%")
    print(f"    太薄(<60)     {s['thin']:>5}  {s['thin']*100/tot:.0f}%   ← 这批才是 ASR 的目标")

    c = store.coverage()
    left = c["total"] - c["digg_count"]
    if left:
        print(f"\n  另有 {left} 条还是早期 31 字段结构,拼不出片段 —— 先跑 `cli.py refill`。")


if __name__ == "__main__":
    main()
