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


async def cmd_whoami(args) -> None:
    """解析并保存自己的 sec_user_id(点赞、我的作品需要)。"""
    from collector import whoami
    from config import ROOT

    sec, uid = await whoami.resolve(ROOT / "data" / "browser-profile")
    whoami.write_to_env(sec, ROOT / ".env")
    print(f"✓ 自己的 uid          : {uid}")
    print(f"✓ 自己的 sec_user_id  : {sec}")
    print(f"  已写入 {ROOT / '.env'} 的 DOUYIN_SEC_USER_ID")
    print("  现在可以采:likes(点赞) / posts(我的作品)")


async def cmd_posts(args) -> None:
    from service import collect_posts

    print("采集我发布的作品…")
    result = await collect_posts(
        max_items=args.max, resume=not args.fresh, on_progress=_progress
    )
    print(f"\n✓ {result}")


async def cmd_smart(args) -> None:
    """智能采集:每类自己判断该做什么,自己处理限流退避。

    这是日常唯一需要的命令,适合挂定时任务。
    """
    import planner
    import service

    print("采集计划:")
    for s in planner.plan_all():
        icon = {"resume": "↻", "sync": "⇅", "skip": "⏸"}[s["action"]]
        print(f"  {icon} {s['label']:<8} {s['reason']}")

    if args.dry_run:
        print("\n(--dry-run,只看计划不执行)")
        return

    def step(o):
        st = o["status"]
        if st == "skipped":
            print(f"\n⏸ {o['label']}:{o['reason']}")
        elif st == "throttled":
            print(f"\n⚠️ {o['label']}:被限流,冷却 {o['cooldown_minutes']} 分钟后再试"
                  f"(已采部分与游标都保留)")
        elif st == "failed":
            print(f"\n✗ {o['label']}:{o['error'][:90]}")
        else:
            why = ("已采到历史尽头" if o.get("exhausted")
                   else "主动收手,下次继续" if o.get("hit_cap")
                   else "已追上最新" if o.get("stopped_early") else "")
            print(f"\n✓ {o['label']}:新增 {o['inserted']} 条 / {o['pages']} 页 {why}")

    print("\n开始采集…")
    r = await service.smart_collect(on_progress=_progress, on_step=step)
    print(f"\n═══ 本轮共新增 {r['inserted']} 条 ═══")
    cmd_stats(args)


async def cmd_state(args) -> None:
    """看各分类的采集进度:已采 / 平台总数 / 完成度。"""
    import planner

    if args.set:
        for pair in args.set:
            scope, _, val = pair.partition("=")
            planner.set_manual_total(scope.strip(), int(val) if val.strip() else None)
            print(f"已设置 {scope.strip()} 的平台总数 = {val.strip() or '(清除)'}")
        print()

    if args.refresh:
        from collector import totals
        try:
            t = await totals.fetch()
            planner.save_totals(t)
            print("已刷新平台计数:", t, "\n")
        except Exception as e:
            print(f"取平台计数失败({type(e).__name__}),用上次的值\n")

    print(f"{'分类':<10}{'已采':>7}{'平台':>8}{'完成度':>9}  {'状态':<8}说明")
    for s in planner.plan_all():
        st = store.get_state(s["scope"])
        flag = "冷却中" if planner.cooldown_left(s["scope"]) else (
            "已采尽" if st.get("exhausted") else "未采尽")
        total = s.get("total")
        src = {"api": "", "manual": "*"}.get(st.get("total_source") or "", "")
        tot_s = (str(total) + src) if total else "—"
        pct = f"{s['percent']}%" if s.get("percent") is not None else "—"
        print(f"{s['label']:<10}{s['collected']:>7}{tot_s:>8}{pct:>9}  {flag:<8}{s['reason']}")
        if st.get("last_error"):
            print(f"{'':<10}└ 上次错误:{st['last_error'][:70]}")
    print("\n注:* = 手填的总数(--set SCOPE=N)。三类分母都能自动取,")
    print("   来自抖音 self 端点:作品/点赞/收藏。")
    print("   缺口可能是原作者删稿造成的永久差额,连续确认两遍后就会接受现状。")


async def cmd_sync(args) -> None:
    """增量同步:从最新开始扫,连续几页无新增就停。

    日常更新用这个,不要用 favorites —— 后者是从游标往历史深处翻,
    发现不了你新收藏的东西。
    """
    import service

    jobs = [("收藏", service.collect_favorites), ("点赞", service.collect_likes)]
    if not args.skip_posts:
        jobs.append(("我的作品", service.collect_posts))

    for name, fn in jobs:
        print(f"\n── {name} ──")
        try:
            r = await fn(sync=True, on_progress=_progress)
            tail = "(连续多页无新增,已提前停止)" if r.get("stopped_early") else ""
            print(f"  新增 {r['inserted']} 条,扫了 {r['pages']} 页 {tail}")
        except Exception as e:
            print(f"  ✗ {type(e).__name__}: {str(e)[:90]}")
            print("    已采部分和游标都保留了。")


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


def cmd_tags(args) -> None:
    """从文案重抽 #话题标签(零成本,不发任何网络请求)。"""
    tagged, distinct = store.rebuild_tags()
    print(f"✓ {tagged} 条作品有标签,{distinct} 个不同标签\n")
    print(f"Top {args.top}:")
    for t in store.top_tags(args.top):
        print(f"  {t['n']:>4}  {t['tag']}")


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

    sub.add_parser("whoami", help="解析并保存自己的 sec_user_id(点赞/作品需要)")

    sp = sub.add_parser("smart", help="智能采集(推荐:自己判断+自己退避,适合挂定时)")
    sp.add_argument("--dry-run", action="store_true", help="只打印计划,不执行")

    sp = sub.add_parser("state", help="看采集进度:已采/平台总数/完成度")
    sp.add_argument("--refresh", action="store_true", help="重新向抖音取一次平台计数")
    sp.add_argument("--set", action="append", metavar="SCOPE=N",
                    help="手填总数,如 collection=1300(收藏只能手填,抖音不给这个数)")

    sp = sub.add_parser("sync", help="增量同步(只发现新增,不续采历史)")
    sp.add_argument("--skip-posts", action="store_true", help="不同步我的作品")

    for name, help_text in (
        ("favorites", "采集我的收藏"),
        ("likes", "采集我的点赞"),
        ("posts", "采集我发布的作品"),
    ):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--max", type=int, default=None, help="最多采集条数")
        sp.add_argument("--fresh", action="store_true", help="忽略游标,从头重采")

    sub.add_parser("folders", help="列出我的收藏夹")

    sp = sub.add_parser("folder", help="采集指定收藏夹")
    sp.add_argument("collects_id")
    sp.add_argument("--max", type=int, default=None)
    sp.add_argument("--fresh", action="store_true")

    sub.add_parser("stats", help="统计")

    sp = sub.add_parser("tags", help="从文案重抽 #话题标签(零成本)")
    sp.add_argument("--top", type=int, default=25)

    sp = sub.add_parser("search", help="关键词搜索")
    sp.add_argument("keyword")
    sp.add_argument("--limit", type=int, default=20)

    args = p.parse_args()

    handlers = {
        "init": cmd_init,
        "login": cmd_login,
        "qrlogin": cmd_qrlogin,
        "whoami": cmd_whoami,
        "smart": cmd_smart,
        "state": cmd_state,
        "sync": cmd_sync,
        "probe": cmd_probe,
        "favorites": cmd_favorites,
        "likes": cmd_likes,
        "posts": cmd_posts,
        "folders": cmd_folders,
        "folder": cmd_folder,
        "stats": cmd_stats,
        "tags": cmd_tags,
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
