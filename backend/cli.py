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


async def cmd_qrlogin(args) -> None:
    """扫码登录:开真浏览器,用户扫码,自动收 cookie。"""
    from collector import cookie as ck
    from config import ROOT

    profile = (ROOT / "data" / "browser-profile") if args.keep_session else None
    cookie = await ck.qr_login(timeout=args.timeout, profile_dir=profile)

    ck.write_to_env(cookie, ROOT / ".env")
    print(f"\n✓ cookie 已写入 {ROOT / '.env'}(长度 {len(cookie)},内容不打印)")
    print("  下一步:python backend/cli.py probe")


def cmd_login(args) -> None:
    """从本机浏览器自动读取 cookie 写入 .env,免手工 F12 复制。"""
    from collector import cookie as ck
    from config import ROOT

    if args.browser:
        cookie, diag = ck.read_from_browser(args.browser)
        diags = [diag]
    else:
        cookie, diags = ck.autodetect()

    print("浏览器探测结果:")
    for d in diags:
        if "error" in d:
            print(f"  {d['browser']:<10} ✗ {d['error'][:70]}")
        else:
            state = "已登录" if d.get("logged_in") else "未登录(只有匿名 cookie)"
            print(f"  {d['browser']:<10} 读到 {d['count']} 条 · {state}")

    if not cookie:
        print(
            "\n✗ 没有找到已登录抖音的浏览器。三条出路:\n"
            "  1. 在 Chrome 里登录一次 www.douyin.com,再重跑本命令\n"
            "  2. 用 Safari 登录的话:系统设置 → 隐私与安全性 → 完全磁盘访问权限,\n"
            "     把你的终端(Terminal / iTerm)加进去,然后重跑\n"
            "  3. 手工填:把 cookie 粘进 .env 的 DOUYIN_COOKIE="
        )
        sys.exit(1)

    ck.write_to_env(cookie, ROOT / ".env")
    print(f"\n✓ cookie 已写入 {ROOT / '.env'}(长度 {len(cookie)},内容不打印)")
    print("  下一步:python backend/cli.py probe")


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

    sp = sub.add_parser("login", help="从本机浏览器自动读取 cookie 写入 .env")
    sp.add_argument(
        "--browser",
        choices=["chrome", "safari", "edge", "firefox", "brave", "chromium", "vivaldi", "opera"],
        help="指定浏览器;不指定则依次自动探测",
    )

    sp = sub.add_parser("qrlogin", help="扫码登录(开真浏览器,需 playwright)")
    sp.add_argument("--timeout", type=int, default=240, help="等待扫码的秒数")
    sp.add_argument(
        "--keep-session",
        action="store_true",
        help="保留浏览器登录态到 data/browser-profile,下次多半不用再扫",
    )

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
        "login": cmd_login,
        "qrlogin": cmd_qrlogin,
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
