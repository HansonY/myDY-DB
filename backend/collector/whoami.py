"""解析「我自己」的 sec_user_id —— 采集点赞和我的作品都要它。

为什么要专门做这件事:
  * 抖音主页链接 `https://www.douyin.com/user/self` 是个别名,不含 sec_user_id,
    而且它**不会重定向**到真实地址。f2 的 SecUserIdFetcher 拿它只会返回字面量
    "self",拿去请求会静默失败 —— 这是个很容易踩的陷阱。
  * 页面 HTML 里能搜到多个 MS4wLjAB… 候选(侧边栏推荐用户等),挑错就会去采
    别人的数据。

做法:用已保存的浏览器登录态打开 /user/self,截获它自己请求的
`/aweme/v1/web/user/profile/self/`,从中取与自己 uid 成对的 sec_uid。
再用 f2 的 fetch_query_user() 拿到的 uid 交叉验证,确保拿到的是自己的。
"""

from __future__ import annotations

import re
from pathlib import Path

_SEC_RE = re.compile(r'"sec_uid"\s*:\s*"(MS4wLjAB[\w-]+)"')
_SELF_API = "/aweme/v1/web/user/profile/self/"


async def _own_uid() -> str:
    """经 f2 拿到自己的数字 uid(只靠 cookie,不需要 sec_user_id)。"""
    from collector.douyin import _make_handler

    user = await _make_handler().fetch_query_user()
    uid = str(user.user_uid or "").strip()
    if not uid:
        raise RuntimeError("拿不到自己的 uid,cookie 可能已失效。请重新 qrlogin。")
    return uid


async def resolve(profile_dir: Path, timeout_ms: int = 15000) -> tuple[str, str]:
    """返回 (sec_user_id, uid)。需要 playwright 与已保存的登录态。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise RuntimeError(
            "解析自己的 sec_user_id 需要 playwright:\n"
            "  pip install -r backend/requirements-login.txt && playwright install chromium"
        ) from e

    if not profile_dir.exists():
        raise RuntimeError(
            f"没有已保存的浏览器登录态({profile_dir})。\n"
            "先跑:python backend/cli.py qrlogin --keep-session"
        )

    uid = await _own_uid()
    bodies: list[str] = []

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(str(profile_dir), headless=True)
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()

            async def on_response(resp) -> None:
                try:
                    if _SELF_API not in resp.url:
                        return
                    bodies.append(await resp.text())
                except Exception:
                    pass  # 单个响应读失败不影响整体

            page.on("response", on_response)
            await page.goto("https://www.douyin.com/user/self", wait_until="domcontentloaded")
            await page.wait_for_timeout(timeout_ms)
        finally:
            await ctx.close()

    if not bodies:
        raise RuntimeError(
            "没抓到 /user/profile/self/ 响应。可能浏览器登录态已过期 —— "
            "重跑 qrlogin --keep-session 再试。"
        )

    # 只认与自己 uid 出现在同一对象里的 sec_uid
    for body in bodies:
        for m in _SEC_RE.finditer(body):
            seg = body[max(0, m.start() - 600) : m.end() + 600]
            if uid in seg:
                return m.group(1), uid

    raise RuntimeError(
        f"在 self 接口响应里没找到与 uid {uid} 成对的 sec_uid。抖音可能改了返回结构。"
    )


def write_to_env(sec_user_id: str, env_path: Path) -> None:
    """写入 .env 的 DOUYIN_SEC_USER_ID,其余内容保持原样。"""
    line = f"DOUYIN_SEC_USER_ID={sec_user_id}"
    if not env_path.exists():
        env_path.write_text(line + "\n", encoding="utf-8")
        return

    lines = env_path.read_text(encoding="utf-8").splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().startswith("DOUYIN_SEC_USER_ID="):
            lines[i] = line
            break
    else:
        lines.append(line)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
