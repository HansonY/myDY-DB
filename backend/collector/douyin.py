"""抖音采集层:封装 f2,把「收藏 / 点赞 / 收藏夹」拉成统一结构。

要点:
  * 收藏接口只靠 cookie,不需要 sec_user_id;点赞接口需要自己的 sec_user_id。
  * 文本取 `desc_raw` 而非 `desc` —— 后者被 f2 做过文件名安全替换(replaceT),
    会丢标点和特殊字符。做知识库要原文。
  * 按页 yield,调用方逐页落库 + 存游标,天然支持断点续跑。
  * f2 的 kwargs["timeout"] 被 HTTP 超时与翻页休眠共用(f2 的设计),
    所以它同时也是限速旋钮。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from f2.apps.douyin.handler import DouyinHandler
from f2.apps.douyin.utils import ClientConfManager, SecUserIdFetcher

from config import settings

# 一页里我们关心的字段 → 库表列名
_FIELD_MAP = {
    "aweme_id": "aweme_id",
    "aweme_type": "aweme_type",
    "sec_user_id": "sec_user_id",
    "uid": "uid",
    "create_time": "create_time",
    "video_duration": "video_duration",
    "cover": "cover",
    "is_prohibited": "is_prohibited",
    "author_deleted": "author_deleted",
}


def build_kwargs() -> dict[str, Any]:
    """构造 f2 handler 需要的最小 kwargs。

    headers 沿用 f2 自带的默认值(含 User-Agent,ABogus 签名依赖它),
    只把 cookie 换成我们自己的。
    """
    if not settings.has_cookie:
        raise RuntimeError(
            "DOUYIN_COOKIE 未配置。请复制 .env.example 为 .env 并填入你自己的 cookie。"
        )

    conf = ClientConfManager.client()
    headers = dict(conf.get("headers") or {})

    return {
        "cookie": settings.douyin_cookie.strip(),
        "headers": headers,
        # 不走系统代理:国内接口经代理常被掐断
        "proxies": {"http://": None, "https://": None},
        # 同时是 HTTP 超时与翻页间隔(f2 的设计)
        "timeout": settings.collect_page_delay,
        "max_retries": 3,
        "max_connections": 5,
        "max_tasks": 5,
    }


def _pick(item: dict[str, Any], *names: str) -> Any:
    """按优先级取第一个非空字段(用于 desc_raw → desc 这类回退)。"""
    for n in names:
        v = item.get(n)
        if v not in (None, "", []):
            return v
    return None


def _normalize(
    item: dict[str, Any],
    source: str,
    collects_id: str | None = None,
    collects_name: str | None = None,
) -> dict[str, Any] | None:
    """f2 的一条 → 我们的一行。拿不到 aweme_id 的条目直接丢弃。"""
    aweme_id = item.get("aweme_id")
    if not aweme_id:
        return None

    row: dict[str, Any] = {"source": source}
    for src, dst in _FIELD_MAP.items():
        row[dst] = item.get(src)

    # 原文优先:desc/nickname 被 f2 做过文件名安全替换
    row["description"] = _pick(item, "desc_raw", "desc")
    row["nickname"] = _pick(item, "nickname_raw", "nickname")
    row["music_title"] = _pick(item, "music_title_raw", "music_title")

    row["aweme_id"] = str(aweme_id)
    row["collects_id"] = collects_id
    row["collects_name"] = collects_name
    row["share_url"] = f"https://www.douyin.com/video/{aweme_id}"
    # 全量留档:以后要补字段不必重采(重采才是风控风险)
    row["raw_json"] = json.dumps(item, ensure_ascii=False, default=str)

    for flag in ("is_prohibited", "author_deleted"):
        row[flag] = 1 if row.get(flag) else 0

    return row


async def _iter_pages(
    agen, source: str, collects_id: str | None = None, collects_name: str | None = None
) -> AsyncIterator[tuple[list[dict[str, Any]], Any]]:
    """把 f2 的 filter 异步生成器转成 (归一化条目列表, max_cursor)。"""
    async for page in agen:
        try:
            raw_items = page._to_list() or []
        except Exception:
            raw_items = []

        rows = [
            r
            for r in (
                _normalize(it, source, collects_id, collects_name) for it in raw_items
            )
            if r
        ]
        yield rows, getattr(page, "max_cursor", None)


async def collect_favorites(
    max_items: int | None = None, start_cursor: int = 0
) -> AsyncIterator[tuple[list[dict[str, Any]], Any]]:
    """我收藏的作品。只需 cookie。"""
    handler = DouyinHandler(build_kwargs())
    agen = handler.fetch_user_collection_videos(
        max_cursor=start_cursor,
        page_counts=settings.collect_page_size,
        max_counts=max_items,
    )
    async for out in _iter_pages(agen, "collection"):
        yield out


async def collect_likes(
    sec_user_id: str, max_items: int | None = None, start_cursor: int = 0
) -> AsyncIterator[tuple[list[dict[str, Any]], Any]]:
    """我点赞的作品。需要自己的 sec_user_id,且账号点赞列表须对自己可见。"""
    handler = DouyinHandler(build_kwargs())
    agen = handler.fetch_user_like_videos(
        sec_user_id=sec_user_id,
        max_cursor=start_cursor,
        page_counts=settings.collect_page_size,
        max_counts=max_items,
    )
    async for out in _iter_pages(agen, "like"):
        yield out


async def list_folders() -> list[dict[str, Any]]:
    """我的收藏夹清单。只靠 cookie —— 即使填别人的 URL 也只能拿到自己的。"""
    handler = DouyinHandler(build_kwargs())
    folders: list[dict[str, Any]] = []

    async for page in handler.fetch_user_collects(
        page_counts=settings.collect_page_size
    ):
        ids = page.collects_id or []
        names = page.collects_name or []
        covers = page.collects_cover or []
        for i, cid in enumerate(ids):
            folders.append(
                {
                    "collects_id": str(cid),
                    "collects_name": names[i] if i < len(names) else None,
                    "cover": covers[i] if i < len(covers) else None,
                    "total_number": None,
                }
            )
    return folders


async def collect_folder(
    collects_id: str,
    collects_name: str | None = None,
    max_items: int | None = None,
    start_cursor: int = 0,
) -> AsyncIterator[tuple[list[dict[str, Any]], Any]]:
    """某个收藏夹内的作品。"""
    handler = DouyinHandler(build_kwargs())
    agen = handler.fetch_user_collects_videos(
        collects_id=collects_id,
        max_cursor=start_cursor,
        page_counts=settings.collect_page_size,
        max_counts=max_items,
    )
    async for out in _iter_pages(agen, "collects", collects_id, collects_name):
        yield out


async def resolve_sec_user_id(profile_url: str) -> str:
    """从主页 URL 解析 sec_user_id(点赞采集需要)。"""
    return await SecUserIdFetcher.get_sec_user_id(profile_url)
