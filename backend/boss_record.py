"""BOSS 直聘「录制器」—— 你正常用浏览器,程序在后台把接口响应存下来。

**为什么是这个设计。** 前面两版都想让程序自己导航、自己翻页,结果:
  · 登录判据靠猜 cookie 名字 → 一打开就误判成功、窗口一闪而过
  · 程序驱动的浏览器行为不像人 → 页面跳空白
  · 而且我根本不知道 BOSS 的接口长什么样,写解析只能猜

换成:**你开车,我记录**。你登录、你点「我的投递」、你自己滚 ——
所有动作都是真人做的,反爬没什么可挑剔的;程序只挂一个监听器,
把 zhipin 返回的 JSON 落到本地文件。

于是「侦察」和「采集」变成同一件事:你正常点一遍,
数据和接口结构一起就有了。之后再照着真实响应写解析,不用猜。

⚠️ 存下来的是**响应体**,不含请求头,所以不会把 cookie / token 写进文件。
   但响应里会有你的投递记录、HR 昵称这些个人信息 —— 目录已在 .gitignore 里。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _pyversion
_pyversion.check()

from config import ROOT
from db import boss_store as bs

# 只记 zhipin 自己的接口,不记图片/统计/第三方
_INTERESTING = re.compile(r"zhipin\.com/.*(api|wapi|json)", re.I)
# 明显和数据无关的排除掉,免得刷屏
_SKIP = re.compile(r"(log|track|report|stat|monitor|heartbeat|\.gif|\.png|\.js|\.css)", re.I)

CAPTURE_DIR = ROOT / "data" / "boss_capture"


def _safe_name(url: str, n: int) -> str:
    """用路径做文件名,**丢掉查询串** —— 那里面常带 token。"""
    path = re.sub(r"^https?://[^/]+", "", url.split("?")[0])
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_")[:70] or "root"
    return f"{n:03d}_{slug}.json"


async def record(minutes: int = 30) -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("需要 playwright:\n"
              "  .venv/bin/pip install -r backend/requirements-login.txt\n"
              "  .venv/bin/playwright install chromium")
        return 1

    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    prof = bs.profile_dir()
    seen: dict[str, int] = {}     # url 路径 → 抓到几次
    saved = 0

    print("=" * 60)
    print("BOSS 录制器")
    print("=" * 60)
    print(f"抓到的响应会存到:{CAPTURE_DIR}")
    print(f"登录态目录:      {prof}")
    print()
    print("接下来:")
    print("  1. 浏览器打开后,**像平时一样**登录(扫码/短信都行)")
    print("  2. 依次点开这三个页面,每个往下滚几屏:")
    print("       · 我的投递      · 我的收藏      · 沟通过的")
    print("  3. 想多抓点就随便点开几个岗位详情")
    print("  4. 完事回这个终端按 [回车] 结束")
    print()
    print("程序不会自己点任何东西 —— 全程你操作,我只在后台记录。")
    print("=" * 60)
    print()

    async with async_playwright() as p:
        launch = dict(headless=False, viewport=None,
                      args=["--disable-blink-features=AutomationControlled"])
        ctx = None
        for channel in ("chrome", None):
            try:
                ctx = await p.chromium.launch_persistent_context(
                    str(prof), **({**launch, "channel": channel} if channel else launch))
                print(f"浏览器:{'本机 Chrome' if channel else 'Playwright Chromium'}\n")
                break
            except Exception as e:      # noqa: BLE001
                if not channel:
                    print(f"浏览器起不来:{type(e).__name__}: {str(e)[:120]}")
                    return 1

        async def on_response(resp):
            nonlocal saved
            url = resp.url
            if not _INTERESTING.search(url) or _SKIP.search(url):
                return
            ct = (resp.headers or {}).get("content-type", "")
            if "json" not in ct.lower():
                return
            try:
                body = await resp.json()
            except Exception:       # noqa: BLE001 —— 响应体拿不到就跳过,不影响你浏览
                return
            key = url.split("?")[0]
            seen[key] = seen.get(key, 0) + 1
            saved += 1
            f = CAPTURE_DIR / _safe_name(url, saved)
            f.write_text(json.dumps(
                {"url": key, "captured_at": datetime.now().isoformat(timespec="seconds"),
                 "body": body},
                ensure_ascii=False, indent=1), encoding="utf-8")
            # 实时反馈:让你看得见「点这一下抓到了东西」
            short = key.replace("https://www.zhipin.com", "")
            print(f"  ✓ {short[:64]}")

        ctx.on("response", lambda r: asyncio.create_task(on_response(r)))

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto("https://www.zhipin.com", wait_until="domcontentloaded",
                            timeout=60000)
        except Exception as e:      # noqa: BLE001
            print(f"(打开首页失败,你自己在地址栏输 zhipin.com 也行:{type(e).__name__})")

        print("\n浏览器已就绪 —— 现在归你操作。抓到东西会在下面滚动。\n")
        try:
            await asyncio.wait_for(asyncio.to_thread(input, ""), timeout=minutes * 60)
        except asyncio.TimeoutError:
            print(f"\n到时间了({minutes} 分钟),先收工。")
        except (KeyboardInterrupt, EOFError):
            pass

        try:
            await ctx.close()
        except Exception:           # noqa: BLE001
            pass

    print("\n" + "=" * 60)
    print(f"共抓到 {saved} 个响应,存在 {CAPTURE_DIR}")
    if seen:
        print("\n按接口分组(路径 · 次数):")
        for k, n in sorted(seen.items(), key=lambda x: -x[1]):
            print(f"  {n:>3}×  {k.replace('https://www.zhipin.com', '')}")
        print("\n下一步:跑 ./boss.sh inspect 看这些响应里有什么字段。")
    else:
        print("\n一个都没抓到。可能是:没点开列表页,或者接口路径不匹配。")
        print("把浏览器 F12 → Network 里看到的接口路径告诉我,我调匹配规则。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(record()))
