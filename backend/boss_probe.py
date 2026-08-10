"""探针:测「光有 cookie 能不能拿到 BOSS 的数据」。

**为什么先做这个。** 整个采集方案取决于一件我没验证过的事:
BOSS 的 `__zp_stoken__` 是不是每次请求都要 JS 现算。
  够用 → 和抖音一样,几十行 HTTP 采集器,快、稳、不用开浏览器
  不够 → 才需要浏览器全程常驻(慢,但签名改了不用管)
在没验证的假设上盖房子,前面已经栽过一次(猜 cookie 名字导致窗口一闪而过)。

⚠️ 这个脚本**绝不打印 cookie 的值**,只报状态码和「看起来登没登上」。
   cookie 从 .env 的 BOSS_COOKIE 读 —— 你自己填进去,我看不到。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _pyversion
_pyversion.check()

import httpx

from config import ROOT

# 要登录才看得到的页面。用它当判据:没登录会被弹回登录页。
TARGETS = [
    ("我的投递", "https://www.zhipin.com/web/geek/chat"),
    ("首页(需登录态才显示个人信息)", "https://www.zhipin.com/web/geek/job"),
]

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _cookie() -> str:
    """从 .env 读 BOSS_COOKIE。**不打印,也不返回给任何会显示的地方。**"""
    v = os.environ.get("BOSS_COOKIE", "").strip()
    if v:
        return v
    env = ROOT / ".env"
    if env.exists():
        for ln in env.read_text(encoding="utf-8").splitlines():
            if ln.strip().startswith("BOSS_COOKIE="):
                return ln.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main() -> int:
    ck = _cookie()
    if not ck:
        print("没找到 BOSS_COOKIE。\n")
        print("怎么填(全程在你自己机器上,我看不到值):")
        print("  1. Chrome 打开 zhipin.com 并确认已登录")
        print("  2. F12 → Application → Cookies → https://www.zhipin.com")
        print("     或者更快:F12 → Network,随便点个请求 → 右键")
        print("     Copy → Copy as cURL,从里面把 -H 'cookie: …' 那一串取出来")
        print(f"  3. 写进 {ROOT / '.env'}:")
        print("       BOSS_COOKIE=这里粘贴整串")
        print("\n  ⚠️ 别把它贴到聊天里 —— 那等同你的账号。填进 .env 就行。")
        return 1

    # 只报特征,不报内容
    names = sorted({p.split("=", 1)[0].strip() for p in ck.split(";") if "=" in p})
    print(f"读到 cookie:{len(names)} 项(只列名字)")
    print(f"  {', '.join(names[:18])}" + (" …" if len(names) > 18 else ""))
    print()

    headers = {
        "user-agent": UA,
        "cookie": ck,
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "zh-CN,zh;q=0.9",
        "referer": "https://www.zhipin.com/",
    }

    ok_any = False
    # trust_env=False:绕开系统代理。抖音那边实测走代理必 SSL EOF,
    # 国内站点直连才通。
    with httpx.Client(timeout=25, follow_redirects=True, trust_env=False) as cl:
        for label, url in TARGETS:
            try:
                r = cl.get(url, headers=headers)
            except Exception as e:      # noqa: BLE001
                print(f"  ✗ {label}: 请求失败 {type(e).__name__}: {str(e)[:80]}")
                continue

            body = r.text
            final = str(r.url)
            # 判据不依赖任何我没验证过的内部细节:
            #   被弹回登录页 / 页面里出现登录表单 = 没登上
            bounced = "/user/" in final or "login" in final.lower()
            has_login_form = bool(re.search(r"(扫码登录|手机号登录|请登录|立即登录)", body))
            looks_in = not bounced and not has_login_form and len(body) > 2000

            print(f"  {'✓' if looks_in else '✗'} {label}")
            print(f"      HTTP {r.status_code} · 正文 {len(body)} 字节")
            if final != url:
                print(f"      跳转到 {final[:80]}")
            if has_login_form:
                print("      页面里有登录表单 —— 说明这份 cookie 没被认")
            ok_any = ok_any or looks_in

    print()
    if ok_any:
        print("结论:**cookie 直连可行**。")
        print("  那就不用开浏览器了 —— 走和抖音一样的 HTTP 采集器,快且稳。")
        print("  下一步:告诉我三个列表页的 XHR 路径,我照着写。")
    else:
        print("结论:**cookie 单独不够**(或者这份 cookie 不全/已过期)。")
        print("  那就走浏览器常驻:./boss.sh record —— 你正常操作,程序在后台记录。")
        print("  注意 cookie 要从**已登录**的标签页取,而且要取完整的一串。")
    return 0 if ok_any else 2


if __name__ == "__main__":
    raise SystemExit(main())
