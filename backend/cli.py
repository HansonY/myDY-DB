#!/usr/bin/env python
"""Douyin-DB 命令行。

首次使用顺序:
    python backend/cli.py init          # 建库
    python backend/cli.py probe         # 只拉 3 条,核对字段映射(不写库)
    python backend/cli.py favorites     # 全量采集收藏
    python backend/cli.py stats
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import settings  # noqa: E402
from db import store  # noqa: E402


def _progress(info: dict) -> None:
    print(
        f"  第 {info['pages']} 页 | 累计 {info['fetched']} 条"
        f"(新增 {info['inserted']})| cursor={info['cursor']}",
        flush=True,
    )


# ── 命令实现 ────────────────────────────────────────────────

def cmd_init(_args) -> None:
    store.init_db()
    print(f"✓ 数据库已就绪:{settings.db_file}")


async def cmd_probe(args) -> None:
    """只拉几条,核对字段映射是否正确。不写库。"""
    from collector import douyin

    print(f"探测中(最多 {args.max} 条,不写库)…\n")
    async for rows, cursor in douyin.collect_favorites(max_items=args.max):
        if not rows:
            print("⚠️  这一页没有条目。cookie 可能已失效,或该账号收藏为空。")
            break

        raw = json.loads(rows[0]["raw_json"])
        print("── f2 返回的原始字段 ──")
        print("  " + ", ".join(sorted(raw.keys())))
        print("\n── 归一化后的首条 ──")
        for k, v in rows[0].items():
            if k == "raw_json":
                continue
            s = str(v)
            print(f"  {k:<16} {s[:90]}{'…' if len(s) > 90 else ''}")

        empty = sum(1 for r in rows if not (r.get("description") or "").strip())
        print(f"\n共 {len(rows)} 条,其中 {empty} 条无文案,cursor={cursor}")
        print("\n✓ 字段映射看起来正常的话,就可以跑 favorites 了")
        break


async def cmd_favorites(args) -> None:
    from service import collect_favorites

    print(f"采集收藏(resume={not args.fresh},翻页间隔 {settings.collect_page_delay}s)…")
    result = await collect_favorites(
        max_items=args.max, resume=not args.fresh, on_progress=_progress
    )
    print(f"\n✓ {result}")


async def cmd_likes(args) -> None:
    from service import collect_likes

    print("采集点赞…")
    result = await collect_likes(
        max_items=args.max, resume=not args.fresh, on_progress=_progress
    )
    print(f"\n✓ {result}")


async def cmd_folders(_args) -> None:
    from service import sync_folders

    folders = await sync_folders()
    if not folders:
        print("没找到收藏夹。")
        return
    print(f"共 {len(folders)} 个收藏夹:\n")
    for f in folders:
        print(f"  {f['collects_id']}  {f['collects_name']}")
    print("\n采集其中一个:python backend/cli.py folder <collects_id>")


async def cmd_folder(args) -> None:
    from service import collect_folder

    result = await collect_folder(
        args.collects_id, max_items=args.max, resume=not args.fresh, on_progress=_progress
    )
    print(f"\n✓ {result}")


def cmd_stats(_args) -> None:
    s = store.stats()
    print(f"作品总数      {s['total']}")
    print(f"有文案的      {s['with_description']}")
    print(f"涉及作者      {s['authors']}")
    print(f"按来源        {s['by_source'] or '—'}")
    runs = store.latest_runs(5)
    if runs:
        print("\n最近采集:")
        for r in runs:
            print(
                f"  #{r['id']} {r['scope']:<24} {r['status']:<7} "
                f"抓取 {r['fetched']} 新增 {r['inserted']} {r['error'] or ''}"
            )


def cmd_search(args) -> None:
    rows = store.list_videos(q=args.keyword, limit=args.limit)
    total = store.count_videos(q=args.keyword)
    print(f"命中 {total} 条,显示前 {len(rows)}:\n")
    for r in rows:
        desc = (r["description"] or "").replace("\n", " ")
        print(f"  [{r['nickname']}] {desc[:70]}")
        print(f"    {r['share_url']}")


# ── 入口 ────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(prog="douyin-db", description="抖音收藏夹 → 个人知识库")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="建库")

    sp = sub.add_parser("probe", help="拉几条核对字段(不写库)")
    sp.add_argument("--max", type=int, default=3)

    for name, help_text in (("favorites", "采集我的收藏"), ("likes", "采集我的点赞")):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--max", type=int, default=None, help="最多采集条数")
        sp.add_argument("--fresh", action="store_true", help="忽略游标,从头重采")

    sub.add_parser("folders", help="列出我的收藏夹")

    sp = sub.add_parser("folder", help="采集指定收藏夹")
    sp.add_argument("collects_id")
    sp.add_argument("--max", type=int, default=None)
    sp.add_argument("--fresh", action="store_true")

    sub.add_parser("stats", help="统计")

    sp = sub.add_parser("search", help="关键词搜索")
    sp.add_argument("keyword")
    sp.add_argument("--limit", type=int, default=20)

    args = p.parse_args()

    handlers = {
        "init": cmd_init,
        "probe": cmd_probe,
        "favorites": cmd_favorites,
        "likes": cmd_likes,
        "folders": cmd_folders,
        "folder": cmd_folder,
        "stats": cmd_stats,
        "search": cmd_search,
    }
    fn = handlers[args.cmd]

    try:
        if asyncio.iscoroutinefunction(fn):
            asyncio.run(fn(args))
        else:
            fn(args)
    except RuntimeError as e:
        print(f"✗ {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n已中断。已采集的数据和游标都已保存,下次直接重跑即可续采。")
        sys.exit(130)


if __name__ == "__main__":
    main()
