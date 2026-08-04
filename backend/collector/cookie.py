"""从本机浏览器自动读取抖音 cookie,免去手工 F12 复制。

依赖 browser-cookie3(f2 自带,无需额外安装)。

各平台注意:
  * macOS Safari 的 cookie 在沙盒容器里,需要给终端 / iTerm
    「系统设置 → 隐私与安全性 → 完全磁盘访问权限」,否则 PermissionError。
  * macOS Chrome 首次读取会弹钥匙串授权。
  * cookie 读出来只写进本机 .env,不经过任何网络。
"""

from __future__ import annotations

from pathlib import Path

# 判断是否「已登录」的会话键。
# 只认真正的 session cookie —— 实测 passport_csrf_token / passport_csrf_token_default
# / ttwid / odin_tt 这些**匿名访客也会有**,拿它们判定会在扫码前就误报成功,
# 然后写进一个无效 cookie,采集静默返回空,极难排查。
_AUTH_KEYS = {"sessionid", "sessionid_ss", "sid_tt", "sid_guard", "uid_tt"}

BROWSERS = ("chrome", "safari", "edge", "firefox", "brave", "chromium", "vivaldi", "opera")


def _to_header(jar: dict[str, str]) -> str:
    """{name: value} → 可直接放进 Cookie 请求头的字符串。"""
    return "; ".join(f"{k}={v}" for k, v in jar.items() if v)


def read_from_browser(browser: str) -> tuple[str, dict[str, object]]:
    """读单个浏览器。返回 (cookie 字符串, 诊断信息)。失败不抛,信息进 diag。"""
    from f2.utils.utils import get_cookie_from_browser

    diag: dict[str, object] = {"browser": browser}
    try:
        jar = get_cookie_from_browser(browser, "douyin.com") or {}
    except Exception as e:
        diag["error"] = f"{type(e).__name__}: {e}"
        return "", diag

    diag["count"] = len(jar)
    diag["logged_in"] = bool(_AUTH_KEYS & set(jar))
    return _to_header(jar), diag


def autodetect() -> tuple[str, list[dict[str, object]]]:
    """依次尝试各浏览器,返回第一个「已登录」的 cookie。

    只有含 sessionid 等登录态键才算成功 —— 未登录的匿名 cookie
    虽然能读到,拿去请求收藏接口只会返回空,不如早报错。
    """
    diags: list[dict[str, object]] = []
    for b in BROWSERS:
        cookie, diag = read_from_browser(b)
        diags.append(diag)
        if cookie and diag.get("logged_in"):
            return cookie, diags
    return "", diags


async def qr_login(timeout: int = 240, profile_dir: Path | None = None) -> str:
    """开一个真浏览器让用户自己扫码登录,然后收走 cookie。

    刻意**不自动化登录流程**(不点按钮、不找二维码元素):只打开抖音首页,
    由用户自己点登录并扫码,我们轮询 cookie 里是否出现登录态键。
    这样抖音怎么改登录页都不会挂 —— 点选择器的实现一改版就废。

    需要额外安装(可选依赖):
        pip install playwright && playwright install chromium
    """
    import asyncio
    import time

    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise RuntimeError(
            "扫码登录需要 playwright(可选依赖):\n"
            "  pip install playwright && playwright install chromium"
        ) from e

    async with async_playwright() as p:
        # headless=False 是必须的 —— 用户要看到二维码
        if profile_dir:
            profile_dir.mkdir(parents=True, exist_ok=True)
            ctx = await p.chromium.launch_persistent_context(
                str(profile_dir), headless=False
            )
            browser = None
        else:
            browser = await p.chromium.launch(headless=False)
            ctx = await browser.new_context()

        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("https://www.douyin.com", wait_until="domcontentloaded")

            print("浏览器已打开。请在里面点「登录」并用抖音 App 扫码。")
            print(f"等待登录完成(最多 {timeout} 秒)…")

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                jar = {
                    c["name"]: c["value"]
                    for c in await ctx.cookies()
                    if c.get("domain", "").endswith("douyin.com")
                }
                if _AUTH_KEYS & set(jar):
                    print("✓ 检测到登录态")
                    return _to_header(jar)
                await asyncio.sleep(2)

            raise RuntimeError(f"{timeout} 秒内没有检测到登录。可以重跑,或改用手工填 cookie。")
        finally:
            await ctx.close()
            if browser:
                await browser.close()


def write_to_env(cookie: str, env_path: Path) -> None:
    """把 cookie 写进 .env 的 DOUYIN_COOKIE 行,其余内容保持原样。"""
    line = f"DOUYIN_COOKIE={cookie}"

    if not env_path.exists():
        env_path.write_text(line + "\n", encoding="utf-8")
        return

    lines = env_path.read_text(encoding="utf-8").splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().startswith("DOUYIN_COOKIE="):
            lines[i] = line
            break
    else:
        lines.append(line)

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
