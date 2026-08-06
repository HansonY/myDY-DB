#!/usr/bin/env python
"""Douyin-DB 命令行。

日常只需要一条命令(可反复跑,每步都会跳过已完成的):
    python backend/cli.py go

它按顺序做:建库 → 扫码登录 → 认自己的 sec_user_id → 收藏夹清单 →
智能采集(收藏/点赞/我的作品,自己处理限流退避)→ 报告覆盖率。

下面那些是它内部用到的单步命令,想单独控制时才需要。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _pyversion
_pyversion.check()   # Python 版本不对就早失败,别让人撞 Rust 编译错误

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


async def cmd_go(args) -> None:
    """一条命令跑完全流程:建库 → 登录 → 认人 → 采全部 → 报告。

    clone 下来之后**只需要记住这一个命令**,而且可以反复跑 ——
    每一步都会先看「是不是已经做过了」,做过就跳过。所以它既是首次安装,
    也是日常更新(采集本身是断点续跑的)。
    """
    import config
    import service
    from collector import cookie as ck
    from config import ROOT

    store.init_db()
    print(f"① 数据库  {settings.db_file}")

    # ── 登录 ──────────────────────────────────────────────
    # cookie 等同账号控制权,只写本机 .env(已 gitignore),不打印内容。
    config.reload()
    if settings.has_cookie:
        print("② 登录    已有 cookie,跳过")
    else:
        print("② 登录    没有 cookie,开浏览器扫码(只有你自己能扫)")
        profile = ROOT / "data" / "browser-profile"
        cookie = await ck.qr_login(timeout=args.timeout, profile_dir=profile)
        ck.write_to_env(cookie, ROOT / ".env")
        config.reload()
        print(f"          ✓ 已写入 .env(长度 {len(cookie)},内容不打印)")

    # ── 认人:点赞和「我的作品」都需要自己的 sec_user_id ──
    if settings.douyin_sec_user_id.strip():
        print("③ 认人    已有 sec_user_id,跳过")
    else:
        from collector import whoami
        sec, uid = await whoami.resolve(ROOT / "data" / "browser-profile")
        whoami.write_to_env(sec, ROOT / ".env")
        config.reload()
        print(f"③ 认人    ✓ uid={uid}")

    # ── 收藏夹清单:1 个请求,顺手拿 ──────────────────────
    try:
        folders = await service.sync_folders()
        print(f"④ 收藏夹  {len(folders)} 个")
    except Exception as e:
        print(f"④ 收藏夹  跳过({type(e).__name__})")

    # ── 采集:三类各自判断续采/增量/跳过,自己处理限流退避 ──
    print("\n⑤ 采集(收藏 / 点赞 / 我的作品)")
    print("   注:抖音没有「转发」接口 —— f2 的 21 个方法里没有,")
    print("       实测「我的作品」里 is_share_post 也全是 0,采不到。\n")

    def step(o):
        st = o["status"]
        if st == "skipped":
            print(f"   ⏸ {o['label']}:{o['reason']}")
        elif st == "throttled":
            print(f"   ⚠️ {o['label']}:被限流,冷却 {o['cooldown_minutes']} 分钟"
                  f"(已采部分与游标都保留,重跑本命令即可接着走)")
        elif st == "failed":
            print(f"   ✗ {o['label']}:{o['error'][:80]}")
        else:
            why = ("已到历史尽头" if o.get("exhausted")
                   else "主动收手,下次继续" if o.get("hit_cap")
                   else "已追上最新" if o.get("stopped_early") else "")
            print(f"   ✓ {o['label']}:新增 {o['inserted']} 条 / {o['pages']} 页 {why}")

    try:
        r = await service.smart_collect(on_progress=_progress, on_step=step)
        print(f"\n   本轮新增 {r['inserted']} 条")
    except Exception as e:
        print(f"\n   ✗ {type(e).__name__}: {str(e)[:90]}")
        print("   已采部分和游标都保留了,重跑本命令接着走。")

    print("\n" + "─" * 60)
    cmd_stats(args)

    c = store.coverage()
    if c["content_unknown"]:
        print(f"\n下一步:{c['content_unknown']} 条还没采到完整响应,"
              "「有没有视频内容总结」是未知的。")
        print("        跑 `cli.py refill` 去确定(会被 403 打断,分多轮跑)。")
    print("\n看数据:.venv/bin/python -m uvicorn main:app --app-dir backend --port 8000")


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
        gap = (f"{s['gap']} 条已失效" if s.get("gap_permanent")
               else f"还差 {s['gap']}" if s.get("short") else "")
        print(f"{s['label']:<10}{s['collected']:>7}{tot_s:>8}{pct:>9}  {flag:<8}{gap:<14}{s['reason'][:34]}")
        if st.get("last_error"):
            print(f"{'':<10}└ 上次错误:{st['last_error'][:70]}")
    print("\n注:* = 手填的总数(--set SCOPE=N)。三类分母都能自动取,")
    print("   来自抖音 self 端点:作品/点赞/收藏。")
    print("   缺口可能是原作者删稿造成的永久差额,连续确认两遍后就会接受现状。")


async def cmd_refill(args) -> None:
    """回补完整字段:重走列表,把已有作品的 raw 与新字段补上。"""
    import service

    scopes = [args.scope] if args.scope else ["collection", "like", "post"]
    for sc in scopes:
        label = {"collection": "收藏", "like": "点赞", "post": "我的作品"}[sc]
        print(f"\n── 回补 {label} ──")
        try:
            r = await service.refill_scope(sc, max_pages=args.max_pages,
                                           on_progress=_progress)
            print(f"  走了 {r['pages']} 页,更新 {r['fetched']} 条"
                  f"{'(采够上限主动收手)' if r.get('hit_cap') else ''}")
        except Exception as e:
            print(f"  ✗ {type(e).__name__}: {str(e)[:80]}")
            print("    已更新的保留了,再跑一次会接着走。")


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
    import service
    from collector import douyin

    # 不写库,但**照样在打抖音接口** —— 所以必须过跨进程锁。
    # 否则网页/MCP 正在采时跑 probe,两个进程一起请求,正是风控的头号诱因。
    # (这里是架构复查时发现的漏洞:probe 是唯一绕过 service 直接翻页的入口。)
    service.guard_single_run()

    print(f"探测中(最多 {args.max} 条,不写库)…\n")
    async for rows, cursor in douyin.collect_favorites(max_items=args.max):
        if not rows:
            print("⚠️  这一页没有条目。cookie 可能已失效,或该账号收藏为空。")
            break

        raw = json.loads(rows[0]["raw_json"])
        print(f"── 抖音真实响应的顶层字段({len(raw)} 个,全部会压缩存进 raw_z)──")
        print("  " + ", ".join(sorted(raw.keys())))
        print("\n── 归一化后的首条(这一轮提升为列的字段)──")
        for k, v in rows[0].items():
            if k in ("raw_json", "_ai"):     # 一个太长、一个是落库中间态
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

    c = store.coverage()
    t = c["total"] or 1
    # 视频内容总结必须三态报。混成「有/没有」的话,「抖音确认没给」和
    # 「还没采全所以不知道」会被当成同一件事 —— 而前者该认命,后者该去补采。
    print("\n视频内容总结(抖音自己生成的,不是文案):")
    print(f"  有            {c['content_have']:>5}  {c['content_have']*100/t:.0f}%")
    print(f"  抖音确认没给   {c['content_none']:>5}  {c['content_none']*100/t:.0f}%"
          "   ← 它只给长视频/知识类生成")
    print(f"  还不知道       {c['content_unknown']:>5}  {c['content_unknown']*100/t:.0f}%"
          "   ← 没采到完整响应,跑 refill 才能确定")

    print("\n字段覆盖:")
    for key, label in (
        ("full_raw", "完整原始响应"), ("digg_count", "互动数据"),
        ("cat1", "官方分类"), ("music_url", "音轨地址"),
        ("chapters", "章节大纲"),
    ):
        print(f"  {label:<12} {c[key]:>5}/{c['total']}  {c[key] * 100 / t:.0f}%")
    if c["legacy_raw"]:
        print(f"  ⚠️ 还有 {c['legacy_raw']} 条未压缩,跑 scripts/compress_raw.py")
    if c["db_bytes"]:
        print(f"  库文件       {c['db_bytes'] / 1024 / 1024:.1f} MB")

    f = store.fragment_stats()
    if f["fragments"]:
        tot = f["thick"] + f["mid"] + f["thin"] or 1
        print(f"\n知识片段(检索的最小单位,scripts/build_fragments.py 重建):")
        print(f"  {f['fragments']} 段 / 覆盖 {f['videos_covered']} 条作品 · {f['by_kind']}")
        print(f"  够用(≥150字) {f['thick']:>5}  {f['thick']*100/tot:.0f}%")
        print(f"  能用(60-149) {f['mid']:>5}  {f['mid']*100/tot:.0f}%")
        print(f"  太薄(<60)    {f['thin']:>5}  {f['thin']*100/tot:.0f}%   ← ASR 唯一该做的目标")

    cats = store.top_categories(8)
    if cats:
        print("\n官方分类 Top:")
        for x in cats:
            print(f"  {x['n']:>4}  {x['cat']}")
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
    rows = store.list_videos(
        q=args.keyword, limit=args.limit, sort=args.sort,
        cat1=args.cat, content=args.content,
    )
    total = store.count_videos(
        q=args.keyword, cat1=args.cat, content=args.content,
    )
    print(f"命中 {total} 条,显示前 {len(rows)}:\n")
    for r in rows:
        desc = (r["description"] or "").replace("\n", " ")
        meta = " · ".join(
            x for x in (
                r.get("cat1"),
                f"赞 {r['digg_count']}" if r.get("digg_count") is not None else None,
            ) if x
        )
        print(f"  [{r['nickname']}] {desc[:70]}")
        if meta:
            print(f"    {meta}")
        # 平台 AI 总结才是「视频讲了什么」,比作者文案有用
        if r.get("ai_summary"):
            print(f"    内容:{r['ai_summary'][:100].replace(chr(10), ' ')}…")
        print(f"    {r['share_url']}")


def cmd_raw(args) -> None:
    """打印一条作品的完整原始响应。

    「存什么」和「用什么」是两回事 —— 库里存了全部 787 个字段,
    想加新维度先来这里看有什么可用,不用重采。
    """
    raw = store.get_raw(args.aweme_id)
    if raw is None:
        print("没有这条,或它还没有完整响应(旧数据需要 refill)。")
        return
    if args.keys:
        print(f"顶层 {len(raw)} 个字段:")
        print("  " + ", ".join(sorted(raw.keys())))
        return
    print(json.dumps(raw, ensure_ascii=False, indent=2)[: args.chars])


# ── 入口 ────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(prog="douyin-db", description="抖音收藏夹 → 个人知识库")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("go", help="★ 一条命令跑完:建库+登录+认人+采全部(可反复跑)")
    sp.add_argument("--timeout", type=int, default=180, help="扫码等待秒数")

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

    sp = sub.add_parser("refill", help="回补完整字段(早期采的只存了 31/787 个字段)")
    sp.add_argument("--scope", choices=["collection", "like", "post"])
    sp.add_argument("--max-pages", type=int, default=0, help="单次页数上限,0=不限")

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

    sp = sub.add_parser("search", help="关键词搜索(也搜平台 AI 总结)")
    sp.add_argument("keyword")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--sort", default="collected",
                    choices=["collected", "published", "duration", "author",
                             "digg", "collect", "comment"],
                    help="digg/collect/comment = 按互动数据排,挑优质内容用")
    sp.add_argument("--cat", help="按抖音官方一级分类筛选")
    sp.add_argument("--content", choices=["have", "none", "unknown"],
                    help="按内容总结状态筛:have=有 · none=抖音确认没给 · "
                         "unknown=还没采全(该去 refill,不是真没有)")

    sp = sub.add_parser("raw", help="看一条作品的完整原始响应(787 个字段都在库里)")
    sp.add_argument("aweme_id")
    sp.add_argument("--keys", action="store_true", help="只列顶层字段名")
    sp.add_argument("--chars", type=int, default=4000, help="最多打印多少字符")

    args = p.parse_args()

    handlers = {
        "go": cmd_go,
        "init": cmd_init,
        "login": cmd_login,
        "qrlogin": cmd_qrlogin,
        "whoami": cmd_whoami,
        "smart": cmd_smart,
        "state": cmd_state,
        "refill": cmd_refill,
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
        "raw": cmd_raw,
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
