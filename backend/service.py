"""采集编排:把 collector 拉到的页逐页落库,并维护游标与任务记录。

逐页落库(而不是全拉完再存)的原因:
  * 中途被风控掐断 / 网络断开时,已采的不丢
  * 游标随页推进,下次 resume 从断点继续,不必重采(重采才是风控风险)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
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


# saved_at 只对「我主动存下来的」有意义。post 是我自己发的作品,
# 它的时间就是 create_time,平台已经精确给了,不用游标去猜。
_SAVED_SCOPES = ("collection", "like", "collects")


def cursor_to_iso(cursor: Any) -> str | None:
    """翻页游标 → ISO 时间。抖音不给「你什么时候收藏的」,但**游标本身就是那个时间戳**。

    单位按量级判而不是按 scope 硬编码(实测收藏是微秒 16 位、点赞和作品是毫秒
    13 位,26 次历史观测一致)—— 哪天抖音改了单位,这里自己就跟上了。

    自校验:换算结果必须落在 2015 年之后、明天之前。落不进去就返回 None,
    宁可没有也不要写一个 1970 年或 2255 年的假时间进库。
    """
    try:
        n = int(cursor)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    upper = datetime.now(timezone.utc).timestamp() + 86400
    for div in (1_000_000, 1000, 1):        # 微秒 → 毫秒 → 秒
        t = n / div
        if 1_420_070_400 < t < upper:       # 2015-01-01 ~ 明天
            return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")
    return None


def _create_time_utc(text: Any) -> datetime | None:
    """把 `videos.create_time` 解成带时区的时间。

    ⚠️ 这个字段是**本机本地时间、且不带时区标记**(`2026-07-17 10:47:29`),
    而 `saved_at` / `collected_at` / `updated_at` 都是 UTC 带 `+00:00`。
    库里这两套格式并存是历史遗留 —— 直接拿去做字符串比较会差 8 小时(CST),
    而且日期相同时 `'T'`(0x54)和 `' '`(0x20)还会让比较结果整个反过来。
    所以任何跨这两个字段的比较都必须走这里,别用 SQL 直接比。
    """
    if not text or not isinstance(text, str):
        return None
    try:
        dt = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo is None else dt


def _stamp_saved_at(rows: list[dict[str, Any]], scope: str, max_cursor: Any) -> None:
    """给这一页打上收藏时间(下界)。

    游标是**本页最后一条**的收藏时间,而列表是按收藏时间倒序的,所以本页每条
    都满足 `收藏时间 >= 游标`。于是:
      * 最后一条  —— 时间精确,`saved_exact=1`
      * 其余      —— 只是下界,`saved_exact=0`

    再免费收紧一道:**你不可能收藏一个还没发布的作品**。所以作品发布时间比
    游标晚时,发布时间才是更紧的下界。实测这一页 20 条里就有几条能往后推好几天,
    对月级归属尤其要紧 —— 否则一条 7 月底发的会被算进 7 月初那个月。
    只收紧非精确行:最后一条的时间是确定的,不该被推走(真出现矛盾也宁可留着,
    好让一致性检查报出来)。

    精度就到这儿了,别指望更多:实测相邻两页游标间隔收藏中位 5.3 天、
    点赞中位 22.4 天。够做月级兴趣漂移,**做不了「几点刷的」**。
    """
    if scope not in _SAVED_SCOPES or not rows:
        return
    iso = cursor_to_iso(max_cursor)
    if not iso:
        return
    floor = datetime.fromisoformat(iso)
    for r in rows:
        pub = _create_time_utc(r.get("create_time"))
        best = pub if (pub and pub > floor) else floor
        r["saved_at"] = best.isoformat(timespec="seconds")
        r["saved_exact"] = 0
    rows[-1]["saved_at"] = iso          # 最后一条是精确值,不收紧
    rows[-1]["saved_exact"] = 1


def _persist_page(
    rows: list[dict[str, Any]], scope: str = "", max_cursor: Any = None
) -> tuple[int, int]:
    """落库一页:作品 + 文案 + 结构化话题 + 知识片段 + 收藏时间。

    片段在这里就地生成,而不是留给一个「记得跑」的脚本 —— 那种东西一定会忘,
    然后新采的作品在检索里永远查不到,而且没有任何报错提示。
    """
    from knowledge import fragments as frag

    _stamp_saved_at(rows, scope, max_cursor)
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
                f, new_here = await asyncio.to_thread(_persist_page, rows, scope, max_cursor)
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


# ── 关注的人 ────────────────────────────────────────────────

async def sync_following() -> dict[str, Any]:
    """刷新关注列表(不采他们的作品)。97 位只要 5 页,很轻。"""
    store.init_db()
    guard_single_run()
    users = await douyin.list_following(await own_sec_user_id())
    n, new = await asyncio.to_thread(store.upsert_following, users)
    return {"fetched": n, "new": new}


async def crawl_recent(
    sec_user_id: str,
    days: int = 3,
    hard_page_cap: int = 5,
    on_progress: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """抓**一位**博主最近 N 天的新作品。

    主页按发布时间倒序,所以从第一页往下翻,**碰到第一条超过 N 天的就停**。
    实测这些博主每天发 0.1–0.5 条,一页 20 条 → 绝大多数人第一页就够。

    为什么不用全量深挖:
      全量  4345 条 / 221 页 / 29 分钟,而且实测第 20 页就被 403
      三天    约 9 条 /  11 页 / 1.5 分钟,零 403 风险
    拿到的东西也不同 —— 全量给的是陈年老作品,这个给的是「今天他们讲了什么」。

    不碰任何游标:每天都从最新开始,本来就该这样。`hard_page_cap` 是兜底,
    防止某人时间戳异常导致一直翻下去。
    """
    store.init_db()
    guard_single_run()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    run_id = store.start_run("recent", origin=ORIGIN)
    fetched = inserted = pages = 0
    fresh_ids: list[str] = []
    try:
        agen = douyin.collect_creator_posts(sec_user_id=sec_user_id,
                                            max_items=None, start_cursor=0)
        async for rows, _cursor in agen:
            pages += 1
            keep, done = [], False
            for r in rows:
                pub = _create_time_utc(r.get("create_time"))
                # 拿不到发布时间就先留着 —— 宁可多存一条,也别因为解析失败漏掉新内容
                if pub is None or pub >= cutoff:
                    keep.append(r)
                else:
                    done = True     # 已经翻到 N 天之前了,这一页剩下的更旧
            if keep:
                f, new_here = await asyncio.to_thread(
                    _persist_page, keep, "following", None)
                fetched += f
                inserted += new_here
                fresh_ids.extend(r["aweme_id"] for r in keep)
            info = {"scope": "recent", "pages": pages,
                    "fetched": fetched, "inserted": inserted}
            await asyncio.to_thread(store.beat_run, run_id, info)
            if on_progress:
                on_progress(info)
            if done or pages >= hard_page_cap:
                break

        store.finish_run(run_id, "done", fetched, inserted)
        await _refresh_tags(inserted)
        return {"sec_user_id": sec_user_id, "days": days, "pages": pages,
                "fetched": fetched, "inserted": inserted,
                "aweme_ids": fresh_ids,
                "hit_cap": pages >= hard_page_cap and not done}
    except Exception as e:
        store.finish_run(run_id, "failed", fetched, inserted, f"{type(e).__name__}: {e}")
        await _refresh_tags(inserted)
        raise


async def daily_round(
    days: int = 3,
    role: str | None = None,
    on_creator: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """每日一轮:把所有已分类的博主的最近 N 天新作品抓回来。

    `role=None` 两类都抓;传 'info' 或 'rival' 就只抓那一类。
    **一个人 403 就整体停手** —— 但这里几乎不会发生,每人只翻 1 页。
    """
    store.init_db()
    picked = await asyncio.to_thread(store.list_following, True)
    if role:
        picked = [u for u in picked if u.get("role") == role]
    else:
        picked = [u for u in picked if u.get("role")]
    if not picked:
        return {"creators": [], "new_ids": [], "stopped_on_403": False,
                "note": "没有已分类的博主 —— 先在关注页给人打上「信息价值」或「竞品」"}

    out, new_ids, stopped = [], [], False
    for u in picked:
        try:
            r = await crawl_recent(u["sec_user_id"], days=days)
            new_ids.extend(r["aweme_ids"])
            item = {"nickname": u["nickname"], "role": u.get("role"),
                    "status": "ok", "found": r["fetched"],
                    "new": r["inserted"], "pages": r["pages"]}
        except Exception as e:
            msg = str(e)
            item = {"nickname": u["nickname"], "role": u.get("role"),
                    "status": "failed", "error": msg[:200]}
            out.append(item)
            if on_creator:
                on_creator(item)
            if "403" in msg:
                stopped = True
                break
            continue
        out.append(item)
        if on_creator:
            on_creator(item)

    return {"creators": out, "new_ids": new_ids, "days": days,
            "found": sum(x.get("found", 0) for x in out),
            "new": sum(x.get("new", 0) for x in out),
            "stopped_on_403": stopped}


# 这里原来有 crawl_creator / crawl_followed —— 「把关注者的全部历史爬下来」。
# **已删除,那是错的方向。** 实测:11 位共 4345 条要 221 页 / 29 分钟,
# 而且第 20 页就被 403;拿回来的还全是陈年老作品。
# 用户要的是「每天知道他们发了什么」,那是 crawl_recent / daily_round ——
# 每人 1 页、1.5 分钟、零风险。想补一点历史就把 days 调大(上限 30 天),
# 按他们 0.1–0.5 条/天的发布频率,30 天也就一两页。

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
