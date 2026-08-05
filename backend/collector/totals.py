"""从抖音拉取「平台侧总数」—— 采集完整度的分母。

为什么必须有分母:
  没有它就只能靠「生成器自然结束」推断采尽,而这个推断**实测会出错**。
  点赞被 403 打断后续采,生成器自然结束了,于是被标成已采尽 ——
  但抖音资料接口说有 1876 条,库里只有 1457,少了 419 条。

三个分母都能拿到,都在 **`/aweme/v1/web/user/profile/self/`**(自己专属端点):
  * 我的作品 → `user.aweme_count`
  * 点赞     → `user.favoriting_count`
  * 收藏     → `user_collect_count.collect_count_list[item_type=2].collect_count`

两个坑:
  1. f2 的 `fetch_user_profile` 走通用 `USER_DETAIL` 端点(别人也能看),
     那里**没有收藏数** —— 收藏是私密信息,只在 self 端点返回。
     所以这里直接请求 self 端点。
  2. self 端点**必须带 ABogus 签名**,裸请求会返回
     `status_code=8 用户未登录`(即使 cookie 正确)。

注意分母也不是硬指标:原作者删稿、作品被限制后,列表里就再也拉不到,
差额会永久存在。所以判定采尽要允许缺口(见 planner)。
"""

from __future__ import annotations

from typing import Any

SELF_PROFILE_URL = "https://www.douyin.com/aweme/v1/web/user/profile/self/"

# 抖音网页端请求必带的固定参数,少了会被判未登录
_BASE_PARAMS = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "publish_video_strategy_type": "2",
    "version_code": "170400",
    "version_name": "17.4.0",
}

# 收藏计数按内容类型分列,2 = 视频(还有音乐、商品等其它类型)
_COLLECT_ITEM_TYPE_VIDEO = 2


async def fetch() -> dict[str, int]:
    """返回 {scope: 平台总数}。只发一次请求。"""
    from f2.apps.douyin.crawler import DouyinCrawler

    from collector.douyin import build_kwargs

    kwargs = build_kwargs()
    async with DouyinCrawler(kwargs) as crawler:
        endpoint = crawler.bogus_manager.model_2_endpoint(
            kwargs["headers"].get("User-Agent"), SELF_PROFILE_URL, dict(_BASE_PARAMS)
        )
        data = await crawler._fetch_get_json(endpoint)

    data = data or {}
    user = data.get("user") or {}
    if not user:
        raise RuntimeError(
            f"self 端点没返回 user(status_code={data.get('status_code')}"
            f" / {data.get('status_msg')})。cookie 可能已失效,重新 qrlogin 试试。"
        )

    out: dict[str, int] = {}
    for scope, field in (("post", "aweme_count"), ("like", "favoriting_count")):
        v = user.get(field)
        if isinstance(v, int) and v >= 0:
            out[scope] = v

    # 收藏数在独立子对象里,且按内容类型分列
    for row in ((data.get("user_collect_count") or {}).get("collect_count_list") or []):
        if row.get("item_type") == _COLLECT_ITEM_TYPE_VIDEO:
            v = row.get("collect_count")
            if isinstance(v, int) and v >= 0:
                out["collection"] = v
            break

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
