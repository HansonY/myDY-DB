#!/usr/bin/env python
"""从本地完整 raw 重新投影出所有派生字段。**零网络请求。**

这是「存完整 raw」的回报,也是「存什么」和「用什么」分开的意义所在:

    采集时把 787 个字段一个不少地存下来 →
    以后想用哪个字段,遍历本地 raw 重投影一遍就有 →
    不用重采、不冒 403、媒体地址也不会因为过期而拿不回来。

实际就靠它救了一次:官方分类 / 平台 AI 总结 / 章节大纲这三个提取器是
在最后一轮采集**之后**才写的,所以那 1400 条完整数据的这些列全是空的。
如果当初为了省体积把 raw 剪枝掉,这三样就只能重采 —— 而重采会被 403 打断。
现在跑一遍这个脚本就补齐了。

投影逻辑直接复用采集层的函数(collector.douyin),保证「采集时写的」和
「回头补的」永远是同一套规则,不会分叉。

用法:
    .venv/bin/python scripts/reproject.py
    .venv/bin/python scripts/reproject.py --limit 100     # 先小范围试
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import _pyversion
_pyversion.check()

from collector import douyin  # noqa: E402
from db import store  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    store.init_db()
    n = skipped = 0
    got = {"cat1": 0, "summary": 0, "chapters": 0, "hashtags": 0,
           "digg": 0, "queries": 0, "title": 0}

    for aid, raw in store.iter_raw(only_full=True):
        fields = douyin._from_raw(raw)
        ai = douyin._content_from_raw(raw)
        # 这里读的是**完整 raw**,所以结论是确定的:有就 have,没有就 none。
        # 绝不会写 unknown —— iter_raw(only_full=True) 已经把旧结构过滤掉了。
        fields.update(
            cat1=ai["cat1"], cat2=ai["cat2"], cat3=ai["cat3"],
            cat_conf=ai["cat_conf"], item_title=ai["item_title"],
            content_state="have" if ai["summary"] else "none",
        )
        store.update_derived(aid, fields)

        if ai["summary"]:
            store.save_transcript(
                aid, "summary", ai["summary"],
                {"source": "douyin_chapter_abstract", "tier": 0},
            )
            got["summary"] += 1
        if ai["chapters"]:
            store.save_extraction(
                aid, category="chapters", fields=ai["chapters"],
                model="douyin_recommend_chapter", tier=0,
                summary=ai.get("summary") or None,
            )
            got["chapters"] += 1

        if ai["queries"]:
            store.save_transcript(
                aid, "queries", " / ".join(ai["queries"]),
                {"source": "douyin_suggest_words", "tier": 0, "n": len(ai["queries"])},
            )
            got["queries"] += 1
        if ai["item_title"]:
            got["title"] += 1

        tags = douyin._hashtags_from_raw(raw)
        if tags:
            store.save_hashtags(aid, tags)
            got["hashtags"] += 1

        if ai["cat1"]:
            got["cat1"] += 1
        if fields.get("digg_count") is not None:
            got["digg"] += 1

        n += 1
        if n % 200 == 0:
            print(f"  {n} 条…", flush=True)
        if args.limit and n >= args.limit:
            break

    print(f"\n✓ 重投影 {n} 条(完整 raw)")
    print(f"  官方分类   {got['cat1']}")
    print(f"  互动数据   {got['digg']}")
    print(f"  平台AI总结 {got['summary']}")
    print(f"  章节大纲   {got['chapters']}")
    print(f"  结构化话题 {got['hashtags']}")
    print(f"  搜索意图词 {got['queries']}   ← 真人写的查询语句,用来提升召回")
    print(f"  干净标题   {got['title']}")

    # 早期 31 字段结构里没有 statistics,所以 digg_count 缺失就等于「旧结构」
    c = store.coverage()
    legacy = c["total"] - c["digg_count"]
    if legacy:
        print(f"\n库内 {c['total']} 条,其中 {legacy} 条还是早期 31 字段结构 —— "
              "这些维度推不出来,得用 `cli.py refill` 重采(会被 403 打断,分多轮跑)。")


if __name__ == "__main__":
    main()
