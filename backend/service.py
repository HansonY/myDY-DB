"""采集编排:把 collector 拉到的页逐页落库,并维护游标与任务记录。

逐页落库(而不是全拉完再存)的原因:
  * 中途被风控掐断 / 网络断开时,已采的不丢
  * 游标随页推进,下次 resume 从断点继续,不必重采(重采才是风控风险)
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from collector import douyin
from config import settings
from db import store


def _persist_page(rows: list[dict[str, Any]]) -> tuple[int, int]:
    """落库一页,并把文案写入文本层(Phase 1 的免费信息)。"""
    fetched, inserted = store.upsert_videos(rows)
    for r in rows:
        desc = (r.get("description") or "").strip()
        if desc:
            store.save_transcript(r["aweme_id"], "desc", desc, {"tier": 1})
    return fetched, inserted


async def _run(
    scope: str,
    page_iter_factory: Callable[[int], Any],
    resume: bool,
    on_progress: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """通用采集循环。page_iter_factory(start_cursor) → 异步页生成器。"""
    store.init_db()

    if not resume:
        store.clear_cursor(scope)
    start_cursor = store.load_cursor(scope) if resume else 0

    run_id = store.start_run(scope)
    fetched = inserted = pages = 0

    try:
        async for rows, max_cursor in page_iter_factory(start_cursor):
            pages += 1
            if rows:
                f, i = await asyncio.to_thread(_persist_page, rows)
                fetched += f
                inserted += i
            if max_cursor:
                await asyncio.to_thread(store.save_cursor, scope, max_cursor)

            if on_progress:
                on_progress(
                    {
                        "scope": scope,
                        "pages": pages,
                        "fetched": fetched,
                        "inserted": inserted,
                        "cursor": max_cursor,
                    }
                )

        store.finish_run(run_id, "done", fetched, inserted)
        return {
            "scope": scope,
            "status": "done",
            "pages": pages,
            "fetched": fetched,
            "inserted": inserted,
            "resumed_from": start_cursor,
        }

    except Exception as e:  # 失败也要把已采数据和游标留住
        store.finish_run(run_id, "failed", fetched, inserted, f"{type(e).__name__}: {e}")
        raise


async def collect_favorites(
    max_items: int | None = None,
    resume: bool = True,
    on_progress: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    limit = max_items if max_items is not None else settings.max_items
    return await _run(
        "collection",
        lambda cur: douyin.collect_favorites(max_items=limit, start_cursor=cur),
        resume,
        on_progress,
    )


async def collect_likes(
    max_items: int | None = None,
    resume: bool = True,
    on_progress: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    if not settings.douyin_profile_url.strip():
        raise RuntimeError(
            "采集点赞列表需要 DOUYIN_PROFILE_URL(你自己的主页地址)。"
            "只想采收藏的话用 favorites。"
        )
    sec_user_id = await douyin.resolve_sec_user_id(settings.douyin_profile_url.strip())
    limit = max_items if max_items is not None else settings.max_items
    return await _run(
        "like",
        lambda cur: douyin.collect_likes(
            sec_user_id=sec_user_id, max_items=limit, start_cursor=cur
        ),
        resume,
        on_progress,
    )


async def sync_folders() -> list[dict[str, Any]]:
    store.init_db()
    folders = await douyin.list_folders()
    await asyncio.to_thread(store.upsert_collects_folders, folders)
    return folders


async def collect_folder(
    collects_id: str,
    collects_name: str | None = None,
    max_items: int | None = None,
    resume: bool = True,
    on_progress: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    limit = max_items if max_items is not None else settings.max_items
    return await _run(
        f"collects:{collects_id}",
        lambda cur: douyin.collect_folder(
            collects_id=collects_id,
            collects_name=collects_name,
            max_items=limit,
            start_cursor=cur,
        ),
        resume,
        on_progress,
    )
