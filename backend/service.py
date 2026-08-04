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
    stop_after_known_pages: int = 0,
) -> dict[str, Any]:
    """通用采集循环。page_iter_factory(start_cursor) → 异步页生成器。

    两种模式:
      * resume=True  —— 从游标继续,**往历史深处翻**。用于首次全量采集被
        风控中断后接着采。
      * resume=False —— 从最新开始扫。这是发现「新增收藏」的唯一方式,因为
        游标分页只会越翻越旧。配合 stop_after_known_pages 做增量同步:
        连续 N 页全是已知条目就停,不必扫完几千条。
    """
    store.init_db()

    if not resume:
        store.clear_cursor(scope)
    start_cursor = store.load_cursor(scope) if resume else 0

    run_id = store.start_run(scope)
    fetched = inserted = pages = 0
    known_streak = 0
    stopped_early = False

    try:
        async for rows, max_cursor in page_iter_factory(start_cursor):
            pages += 1
            new_here = 0
            if rows:
                f, new_here = await asyncio.to_thread(_persist_page, rows)
                fetched += f
                inserted += new_here
            # 增量同步:这一页没带来新条目就累计,连续多页如此说明追上了。
            # 空页也算 —— 它同样代表「没有新东西」。
            known_streak = known_streak + 1 if new_here == 0 else 0
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

            if stop_after_known_pages and known_streak >= stop_after_known_pages:
                stopped_early = True
                break

        store.finish_run(run_id, "done", fetched, inserted)
        await _refresh_tags(inserted)
        return {
            "scope": scope,
            "status": "done",
            "pages": pages,
            "fetched": fetched,
            "inserted": inserted,
            "resumed_from": start_cursor,
            "stopped_early": stopped_early,
        }

    except Exception as e:  # 失败也要把已采数据和游标留住
        store.finish_run(run_id, "failed", fetched, inserted, f"{type(e).__name__}: {e}")
        # 被风控掐断时也要把已采部分的标签补上,否则新条目在界面上没有分类
        await _refresh_tags(inserted)
        raise


async def _refresh_tags(inserted: int) -> None:
    """采到新东西就重抽标签。

    纯本地计算、无外部请求,成本可忽略;不自动做的话新采的作品在界面上
    没有话题分类,得手工跑一次 `cli.py tags`,很容易忘。
    """
    if inserted <= 0:
        return
    try:
        await asyncio.to_thread(store.rebuild_tags)
    except Exception:
        pass  # 标签是增强项,失败不该影响采集结果


# 增量同步时,连续几页全是已知条目就认为追上了
SYNC_KNOWN_PAGES = 3


def _sync_args(sync: bool, resume: bool) -> tuple[bool, int]:
    """sync 模式必须从最新开始扫(resume=False),否则只会越翻越旧。"""
    return (False, SYNC_KNOWN_PAGES) if sync else (resume, 0)


async def collect_favorites(
    max_items: int | None = None,
    resume: bool = True,
    on_progress: Callable[[dict], None] | None = None,
    sync: bool = False,
) -> dict[str, Any]:
    limit = max_items if max_items is not None else settings.max_items
    resume, stop = _sync_args(sync, resume)
    return await _run(
        "collection",
        lambda cur: douyin.collect_favorites(max_items=limit, start_cursor=cur),
        resume,
        on_progress,
        stop,
    )


async def own_sec_user_id() -> str:
    """拿到自己的 sec_user_id(点赞、我的作品都要)。

    优先用 .env 里由 `cli.py whoami` 写好的值;否则退回从主页 URL 解析。
    注意 `user/self` 这类别名解析不出来,会得到字面量 "self" —— 直接拒掉,
    否则拿它去请求只会静默返回空,极难排查。
    """
    sec = settings.douyin_sec_user_id.strip()
    if sec:
        return sec

    url = settings.douyin_profile_url.strip()
    if not url:
        raise RuntimeError(
            "需要自己的 sec_user_id。跑一次:python backend/cli.py whoami"
        )

    sec = (await douyin.resolve_sec_user_id(url) or "").strip()
    if not sec.startswith("MS4wLjAB"):
        raise RuntimeError(
            f"从主页 URL 解析出的不是有效 sec_user_id(得到 {sec!r})。"
            "`user/self` 这类别名解析不出来 —— 跑 `python backend/cli.py whoami` 自动获取。"
        )
    return sec


async def collect_likes(
    max_items: int | None = None,
    resume: bool = True,
    on_progress: Callable[[dict], None] | None = None,
    sync: bool = False,
) -> dict[str, Any]:
    sec_user_id = await own_sec_user_id()
    limit = max_items if max_items is not None else settings.max_items
    resume, stop = _sync_args(sync, resume)
    return await _run(
        "like",
        lambda cur: douyin.collect_likes(
            sec_user_id=sec_user_id, max_items=limit, start_cursor=cur
        ),
        resume,
        on_progress,
        stop,
    )


async def collect_posts(
    max_items: int | None = None,
    resume: bool = True,
    on_progress: Callable[[dict], None] | None = None,
    sync: bool = False,
) -> dict[str, Any]:
    sec_user_id = await own_sec_user_id()
    limit = max_items if max_items is not None else settings.max_items
    resume, stop = _sync_args(sync, resume)
    return await _run(
        "post",
        lambda cur: douyin.collect_posts(
            sec_user_id=sec_user_id, max_items=limit, start_cursor=cur
        ),
        resume,
        on_progress,
        stop,
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
