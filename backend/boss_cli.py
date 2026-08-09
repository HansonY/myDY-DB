#!/usr/bin/env python
"""BOSS 直聘知识库的命令行入口。

    ./boss.sh login    扫码登录(首次跑一次)
    ./boss.sh fetch    抓我自己的数据

和抖音那边的关键区别:**浏览器全程留着**。
抖音是「扫码拿 cookie → 关浏览器 → f2 用 cookie 直接调接口」,因为 f2
实现了 ABogus 签名。BOSS 的 `__zp_stoken__` 是混淆 JS 现算的,光有 cookie
没用 —— 必须有 JS 运行时,所以浏览器本身就是采集器。

慢是特性:签名改了不用管,验证码出来你自己点一下,行为像人不容易被盯上。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _pyversion
_pyversion.check()

from db import boss_store as bs

LOGIN_URL = "https://www.zhipin.com/web/user/?ka=header-login"
HOME_URL = "https://www.zhipin.com/web/geek/job"


async def login(timeout: int = 300) -> int:
    """开浏览器让你自己登录,登录态存进本地 profile 目录。

    **不碰你的账号密码,也不读 cookie 的值** —— 登录全过程在你眼前的浏览器里
    由你完成,程序只负责把浏览器配置目录留在本地,下次免登。
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("需要 playwright:\n"
              "  .venv/bin/pip install -r backend/requirements-login.txt\n"
              "  .venv/bin/playwright install chromium")
        return 1

    prof = bs.profile_dir()
    print(f"登录态目录:{prof}")
    print("即将打开浏览器。请在浏览器里完成登录(扫码或短信都行)。")
    print("浏览器**不会自己关**,登录完回终端按回车。\n")

    async with async_playwright() as p:
        # 用 persistent_context:登录态直接落在这个目录,下次免登。
        # headless=False —— 扫码/验证码必须你能看见。
        ctx = await p.chromium.launch_persistent_context(
            str(prof), headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:      # noqa: BLE001
            print(f"打不开登录页:{type(e).__name__}: {str(e)[:120]}")
            await ctx.close()
            return 1

        # ⚠️ **不要靠 cookie 名字判断登录**。
        # 第一版就是这么写的,栽了:BOSS 给所有访客都下发 `__zp_stoken__`
        # (反爬 token),页面一加载就有;而我的匹配条件是「名字里含 token」,
        # 于是刚打开就判定登录成功、立刻关窗口 —— 用户看到的是「一闪而过」。
        # 我没实测过就猜名字,这是根因。
        #
        # 现在改成两步,都不依赖任何我没验证过的内部细节:
        #   1) 由你告诉我登录完了(回车)
        #   2) 程序去开一个**必须登录才能看的页面**,看是不是被弹回登录页
        print("─" * 56)
        print("浏览器已打开。请在里面完成登录。")
        print("登录好之后,回到这个终端按 [回车] —— 我再去验证。")
        print("(不想登了就按 Ctrl-C,已有的登录态不会被动)")
        print("─" * 56)
        try:
            await asyncio.wait_for(asyncio.to_thread(input, ""), timeout=timeout)
        except asyncio.TimeoutError:
            print(f"\n等了 {timeout // 60} 分钟没等到回车,先退出。登录态照常保留。")
            await ctx.close()
            return 1
        except (KeyboardInterrupt, EOFError):
            await ctx.close()
            return 1

        ok = await _verify(page)
        # 把 cookie 名字列出来 —— 只列名字不列值。下次要写自动判据就靠它,
        # 省得再猜一遍。
        names = sorted({c["name"] for c in await ctx.cookies()})
        print(f"\n当前 cookie 项(只列名字,不显示值,共 {len(names)} 个):")
        print("  " + ", ".join(names))

        if ok:
            print("\n✓ 登录有效,登录态已存在本地。下次不用再登。")
        else:
            print("\n✗ 看起来还没登上 —— 打开需要登录的页面时被弹回了登录页。")
            print("  再跑一次 ./boss.sh login 试试。")
        await ctx.close()
    return 0 if ok else 1


async def _verify(page) -> bool:
    """去开一个**必须登录才能看的页面**,看会不会被弹回登录页。

    这个判据不依赖任何内部实现:平台改 cookie 名字、改 token 机制都不影响,
    「没登录就看不到」这件事是不会变的。
    """
    try:
        await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(3)
    except Exception as e:      # noqa: BLE001
        print(f"验证时打不开页面:{type(e).__name__}: {str(e)[:100]}")
        return False
    url = page.url
    # 被弹到登录页 = 没登上
    if "/user/" in url or "login" in url.lower():
        return False
    return True


async def whoami() -> int:
    """看登录态还在不在(不打印任何 cookie 值)。"""
    from playwright.async_api import async_playwright
    prof = bs.profile_dir()
    if not prof.exists() or not any(prof.iterdir()):
        print("还没登录过。先跑:./boss.sh login")
        return 1
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(str(prof), headless=True)
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            ok = await _verify(page)          # 同一个判据:能不能进需登录的页面
            names = sorted({c["name"] for c in await ctx.cookies()})
            print(f"登录态目录:{prof}")
            print(f"登录{'有效 ✓' if ok else '已失效 ✗ —— 跑 ./boss.sh login 重登'}")
            print(f"cookie 项数:{len(names)}(只列名字,不显示值)")
            print("  " + ", ".join(names[:14]) + (" …" if len(names) > 14 else ""))
        finally:
            await ctx.close()
    return 0 if ok else 1


def fetch(_args: list[str]) -> int:
    """抓我自己的数据 —— **还没实现**。

    卡在一件事上:我不知道 BOSS 现在「我的投递 / 我的收藏 / 沟通过的」
    这三个列表页翻页时打的是哪个 XHR、响应里字段叫什么。
    凭记忆写必错,所以先不写。

    需要的信息(登录后 F12 → Network → 筛 Fetch/XHR,翻一页):
      · 三个页面各自的 URL
      · 翻页时那个请求的 URL(路径就行)
      · 响应里的关键字段名(如 encryptJobId / jobName / salaryDesc)
    **不要**给 cookie、token、请求头 —— 那些等同你的账号。
    """
    bs.init_db()
    print("采集器还没实现 —— 缺 BOSS 的接口形态。\n")
    print("登录后按 F12 → Network → 筛 Fetch/XHR,打开这三个页面各翻一页:")
    print("  · 我的投递")
    print("  · 我的收藏")
    print("  · 沟通过的")
    print("\n把每个页面的 URL + 翻页那个 XHR 的请求 URL + 响应字段名告诉我。")
    print("⚠️ 不要贴 cookie / token / 请求头 —— 那些等同你的账号。\n")
    print(f"库已就绪:{bs.db_file()}")
    return 2


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "login"
    if cmd == "login":
        return asyncio.run(login())
    if cmd == "whoami":
        return asyncio.run(whoami())
    if cmd == "fetch":
        return fetch(sys.argv[2:])
    print(f"未知命令 {cmd!r}。可用:login / whoami / fetch")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
