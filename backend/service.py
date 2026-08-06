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


# 这个进程是谁:界面里要能说清「命令行在采」还是「网页在采」。
# main.py 启动时会改成 "web"。
ORIGIN = "cli"


class AlreadyCollecting(RuntimeError):
    """已有另一个采集在跑(可能是另一个进程)。"""

    # 三个入口都会写 origin,少一个就会把 MCP 误报成「网页」
    ORIGIN_LABEL = {"cli": "命令行", "web": "网页", "mcp": "MCP(AI)"}

    def __init__(self, run: dict[str, Any]):
        who = self.ORIGIN_LABEL.get(run.get("origin") or "", "另一个进程")
        p = run.get("progress") or {}
        detail = f",已到第 {p.get('pages')} 页" if p.get("pages") else ""
        super().__init__(
            f"{who}正在采集「{run.get('scope')}」{detail}。"
            "同时跑两个采集会让两个进程一起打抖音接口,这是风控的主要诱因 —— 请等它结束。"
        )
        self.run = run


def guard_single_run() -> None:
    """跨进程互斥:库里有活着的采集就拒绝新的。"""
    run = store.active_run()
    if run:
        raise AlreadyCollecting(run)


def _persist_page(rows: list[dict[str, Any]]) -> tuple[int, int]:
    """落库一页:作品 + 文案 + 结构化话题 + 知识片段。

    片段在这里就地生成,而不是留给一个「记得跑」的脚本 —— 那种东西一定会忘,
    然后新采的作品在检索里永远查不到,而且没有任何报错提示。
    """
    from knowledge import fragments as frag

    fetched, inserted = store.upsert_videos(rows)
    for r in rows:
        desc = (r.get("description") or "").strip()
        if desc:
            store.save_transcript(r["aweme_id"], "desc", desc, {"tier": 1})
        # 平台给的结构化话题比从文案正则抠更准(不会粘上标点)
        if r.get("hashtags"):
            store.save_hashtags(r["aweme_id"], r["hashtags"])

        # 抖音自己生成的 AI 内容总结 —— 这才是「视频内容」,不是作者写的文案
        ai = r.get("_ai") or {}
        if ai.get("summary"):
            store.save_transcript(
                r["aweme_id"], "summary", ai["summary"],
                {"source": "douyin_chapter_abstract", "tier": 0},
            )
        # 「大家都在搜」—— 真人写的查询语句,专门用来提升检索召回
        if ai.get("queries"):
            store.save_transcript(
                r["aweme_id"], "queries", " / ".join(ai["queries"]),
                {"source": "douyin_suggest_words", "tier": 0, "n": len(ai["queries"])},
            )
        if ai.get("chapters"):
            store.save_extraction(
                r["aweme_id"], category="chapters",
                fields=ai["chapters"], model="douyin_recommend_chapter",
                tier=0, summary=ai.get("summary") or None,
            )

        # 知识片段:只有拿到真实响应时才拼。旧结构那 31 个字段拼不出章节和
        # 搜索意图词,硬拼只会得到一个「只有文案」的假片段,还会盖掉
        # 以后回补出来的好片段。
        if ai:
            store.save_fragments(
                r["aweme_id"], frag.build(r, ai, r.get("hashtags") or [])
            )
    return fetched, inserted


async def _run(
    scope: str,
    page_iter_factory: Callable[[int], Any],
    resume: bool,
    on_progress: Callable[[dict], None] | None = None,
    stop_after_known_pages: int = 0,
    max_pages: int = 0,
    persist_cursor: bool = True,
    cursor_key: str | None = None,
) -> dict[str, Any]:
    """通用采集循环。page_iter_factory(start_cursor) → 异步页生成器。

    三种模式:
      * resume=True  —— 从游标继续,**往历史深处翻**。用于首次全量采集被
        风控中断后接着采。
      * resume=False —— 从最新开始扫。这是发现「新增收藏」的唯一方式,因为
        游标分页只会越翻越旧。配合 stop_after_known_pages 做增量同步:
        连续 N 页全是已知条目就停,不必扫完几千条。
      * cursor_key   —— 回补模式:用一套**独立游标**,既不从 resume 的深挖
        进度出发,也不覆盖它。没有它的话回补每轮都从最新重走 —— 被 403 打断
        后第二轮只会把同样的前几十页再刷一遍,永远走不到更深处。
    """
    store.init_db()
    guard_single_run()      # 无论从命令行还是网页进来,都先看库里有没有别人在跑

    # 游标记录的是「历史翻到哪了」,只属于 resume 模式。
    # sync 从最新开始扫,绝不能碰它 —— 否则会把深挖进度覆盖成「最新往下几页」,
    # 之后 resume 就一直在重走已知区域。实测踩过:收藏游标被 sync 重置到
    # 2026-01,而库里最早的收藏在 2020 年,续采抓了 470 条零新增。
    if cursor_key:
        start_cursor = store.load_cursor(cursor_key)
    elif resume:
        start_cursor = store.load_cursor(scope)
    else:
        start_cursor = 0
        if persist_cursor:          # 只有显式 --fresh 才真的清掉深挖进度
            store.clear_cursor(scope)

    run_id = store.start_run(scope, origin=ORIGIN)
    fetched = inserted = pages = 0
    known_streak = 0
    stopped_early = False   # 增量同步追上了
    hit_cap = False         # 主动收手(没到 403 就先停)

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
                if cursor_key:      # 回补自己的进度,和深挖游标互不干扰
                    await asyncio.to_thread(store.save_cursor, cursor_key, max_cursor)
                elif persist_cursor:
                    await asyncio.to_thread(store.save_cursor, scope, max_cursor)

            info = {
                "scope": scope,
                "pages": pages,
                "fetched": fetched,
                "inserted": inserted,
                "cursor": max_cursor,
            }
            # 心跳 + 进度落库:另一个进程(界面)只能从库里看到进度
            await asyncio.to_thread(store.beat_run, run_id, info)
            if on_progress:
                on_progress(info)

            if stop_after_known_pages and known_streak >= stop_after_known_pages:
                stopped_early = True
                break

            # 主动收手:实测被 403 拒过之后的冷却代价,远高于自己少采几页
            if max_pages and pages >= max_pages:
                hit_cap = True
                break

        # 回补走完了全程 → 清掉回补游标,下一轮重新从最新开始。
        # (顺带把互动数据刷新一遍 —— 赞/藏数是会变的。)
        if cursor_key and not stopped_early and not hit_cap:
            await asyncio.to_thread(store.clear_cursor, cursor_key)

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
            "hit_cap": hit_cap,
            # 生成器自然结束、既没撞上限也没提前停 → 说明翻到了历史尽头
            "exhausted": not stopped_early and not hit_cap,
        }

    except Exception as e:  # 失败也要把已采数据和游标留住
        store.finish_run(run_id, "failed", fetched, inserted, f"{type(e).__name__}: {e}")
        # 被风控掐断时也要把已采部分的标签补上,否则新条目在界面上没有分类
        await _refresh_tags(inserted)
        raise


async def smart_collect(
    on_progress: Callable[[dict], None] | None = None,
    on_step: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """智能采集:每类自己判断该续采、该增量同步、还是该跳过。

    规则来自实测(见 planner.py 顶部注释):
      * 每类独立状态与页数上限 —— 抖音对不同接口策略不同
      * 主动收手优于被 403 —— 后者的冷却代价高得多
      * 403 走指数退避,冷却期内直接跳过,不去试探
      * 历史采尽后自动切成增量同步,否则永远发现不了新增内容
      * 一类失败不影响其他类
    """
    import planner

    store.init_db()
    guard_single_run()      # 先整体拦一次,别等到逐类跑时才发现

    # 先取一次平台侧总数(1 个请求)。它是完整度的分母,也是纠正
    # 「生成器自然结束就算采尽」这个错误推断的唯一依据。
    try:
        from collector import totals
        planner.save_totals(await totals.fetch())
    except Exception:
        pass    # 拿不到分母不影响采集,只是没法判断完整度

    results: list[dict[str, Any]] = []

    for step in planner.plan_all():
        scope, label = step["scope"], step["label"]

        if step["action"] == "skip":
            out = {**step, "status": "skipped", "inserted": 0}
            results.append(out)
            if on_step:
                on_step(out)
            continue

        fn = _COLLECTORS[scope]
        is_sync = step["action"] == "sync"
        try:
            r = await fn(
                sync=is_sync,
                resume=not is_sync,
                on_progress=on_progress,
                max_pages=step.get("max_pages", 0),
            )
            planner.record_success(scope, r["pages"], r.get("exhausted", False))
            out = {
                **step, "status": "done",
                "inserted": r["inserted"], "pages": r["pages"],
                "exhausted": r.get("exhausted"), "hit_cap": r.get("hit_cap"),
                "stopped_early": r.get("stopped_early"),
            }
        except AlreadyCollecting:
            raise       # 别人抢在中途开始跑了 —— 整体中止,不要记成某一类失败
        except Exception as e:
            if planner.is_throttle_error(e):
                cd = planner.record_throttled(scope, 0, f"{type(e).__name__}: {e}")
                out = {**step, "status": "throttled", "inserted": 0, **cd}
            else:
                planner.record_failure(scope, 0, f"{type(e).__name__}: {e}")
                out = {**step, "status": "failed", "inserted": 0,
                       "error": f"{type(e).__name__}: {e}"}

        results.append(out)
        if on_step:
            on_step(out)

    return {"steps": results, "inserted": sum(r.get("inserted", 0) for r in results)}


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


async def refill_scope(
    scope: str,
    max_pages: int = 0,
    on_progress: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """回补:重走列表,把已有作品补上完整字段。

    为什么需要:早期采集时 raw_json 只存了 f2 提取的 31 个字段,而抖音真实
    响应有 787 个 —— 互动数据、视频/音轨地址、结构化话题、尺寸全丢了。
    这些只能重新请求才能拿到(媒体地址还带 x-expires,越晚越可能失效)。

    与其它模式的区别:
      * 从最新开始走全程(不是从游标续) —— 要覆盖所有已有作品
      * **不碰深挖游标** —— 那是 resume 的进度,不能被这次回补覆盖
      * 不提前停 —— 整页都是已知条目也要继续,因为目的就是更新它们
    """
    fn = _COLLECTORS[scope]
    return await fn(resume=False, max_pages=max_pages, refill=True,
                    on_progress=on_progress)


def _sync_args(sync: bool, resume: bool) -> tuple[bool, int, bool]:
    """返回 (resume, stop_after_known_pages, persist_cursor)。

    sync 必须从最新开始扫(resume=False),否则只会越翻越旧;
    并且**不能持久化游标** —— 游标是 resume 的深挖进度,被 sync 覆盖就丢了。
    """
    if sync:
        return (False, SYNC_KNOWN_PAGES, False)
    return (resume, 0, True)


async def collect_favorites(
    max_items: int | None = None,
    resume: bool = True,
    on_progress: Callable[[dict], None] | None = None,
    sync: bool = False,
    max_pages: int = 0,
    refill: bool = False,
) -> dict[str, Any]:
    limit = max_items if max_items is not None else settings.max_items
    resume, stop, keep = _sync_args(sync, resume)
    scope_name = "collection"
    ckey = None
    if refill:
        # 回补:走全程、不提前停、绝不动深挖游标,并用自己的游标记进度 ——
        # 点赞接口实测 6 页就 403,没有独立游标的话每轮都在重刷同样那几页。
        resume, stop, keep = False, 0, False
        ckey = f"refill:{scope_name}"
    return await _run(
        "collection",
        lambda cur: douyin.collect_favorites(max_items=limit, start_cursor=cur),
        resume,
        on_progress,
        stop,
        max_pages,
        keep,
        ckey,
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
    max_pages: int = 0,
    refill: bool = False,
) -> dict[str, Any]:
    sec_user_id = await own_sec_user_id()
    limit = max_items if max_items is not None else settings.max_items
    resume, stop, keep = _sync_args(sync, resume)
    scope_name = "like"
    ckey = None
    if refill:
        # 回补:走全程、不提前停、绝不动深挖游标,并用自己的游标记进度 ——
        # 点赞接口实测 6 页就 403,没有独立游标的话每轮都在重刷同样那几页。
        resume, stop, keep = False, 0, False
        ckey = f"refill:{scope_name}"
    return await _run(
        "like",
        lambda cur: douyin.collect_likes(
            sec_user_id=sec_user_id, max_items=limit, start_cursor=cur
        ),
        resume,
        on_progress,
        stop,
        max_pages,
        keep,
        ckey,
    )


async def collect_posts(
    max_items: int | None = None,
    resume: bool = True,
    on_progress: Callable[[dict], None] | None = None,
    sync: bool = False,
    max_pages: int = 0,
    refill: bool = False,
) -> dict[str, Any]:
    sec_user_id = await own_sec_user_id()
    limit = max_items if max_items is not None else settings.max_items
    resume, stop, keep = _sync_args(sync, resume)
    scope_name = "post"
    ckey = None
    if refill:
        # 回补:走全程、不提前停、绝不动深挖游标,并用自己的游标记进度 ——
        # 点赞接口实测 6 页就 403,没有独立游标的话每轮都在重刷同样那几页。
        resume, stop, keep = False, 0, False
        ckey = f"refill:{scope_name}"
    return await _run(
        "post",
        lambda cur: douyin.collect_posts(
            sec_user_id=sec_user_id, max_items=limit, start_cursor=cur
        ),
        resume,
        on_progress,
        stop,
        max_pages,
        keep,
        ckey,
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


# smart_collect 用:scope → 采集函数。放在文件末尾,等三个函数都已定义。
_COLLECTORS = {
    "collection": collect_favorites,
    "like": collect_likes,
    "post": collect_posts,
}
