"""从抖音拉取「平台侧总数」—— 采集完整度的分母。

为什么必须有分母:
  没有它就只能靠「生成器自然结束」推断采尽,而这个推断**实测会出错**。
  点赞被 403 打断后续采,生成器自然结束了,于是被标成已采尽 ——
  但抖音资料接口说有 1876 条,库里只有 1457,少了 419 条。

哪些有分母:
  * 我的作品 → profile.aweme_count      ✅ 官方计数
  * 点赞     → profile.favoriting_count ✅ 官方计数
  * 收藏     ❌ 抖音资料接口不提供收藏数,只能靠 App 里人工核对

注意分母也不是硬指标:原作者删稿、作品被限制后,列表里就再也拉不到,
差额会永久存在。所以判定采尽要允许缺口(见 planner)。
"""

from __future__ import annotations

from typing import Any

# scope → profile 字段名。没有对应字段的分类不在此表内。
TOTAL_FIELDS = {
    "post": "aweme_count",
    "like": "favoriting_count",
}


async def fetch() -> dict[str, int]:
    """返回 {scope: 平台总数}。只发一次请求。"""
    from collector.douyin import _make_handler
    from config import settings

    sec = settings.douyin_sec_user_id.strip()
    if not sec:
        raise RuntimeError("需要自己的 sec_user_id。先跑:python backend/cli.py whoami")

    profile = await _make_handler().fetch_user_profile(sec)

    out: dict[str, int] = {}
    for scope, field in TOTAL_FIELDS.items():
        v = getattr(profile, field, None)
        if isinstance(v, int) and v >= 0:
            out[scope] = v
    return out


def progress_of(scope: str) -> dict[str, Any]:
    """某一类的采集完整度。没有分母时 total 为 None。"""
    from db import store

    st = store.get_state(scope)
    got = store.count_by_source(scope)
    total = st.get("platform_total")
    gap = (total - got) if isinstance(total, int) else None
    return {
        "scope": scope,
        "collected": got,
        "total": total,
        "gap": gap,
        "percent": round(got * 100 / total, 1) if total else None,
        "total_at": st.get("platform_total_at"),
    }
