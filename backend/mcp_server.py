#!/usr/bin/env python
"""Douyin-DB 的 MCP 服务 —— 让 AI 直接查你的收藏知识库,也能驱动采集。

和市面上那些抖音 MCP 的区别:它们都是**无状态的单链接解析**(给个 URL 返回
无水印地址或文案)。这个背后有库,所以能回答跨作品的问题:
  「我收藏过的做菜视频里,关于牛排回温的说法有哪些?」
  「我点赞的 AI 相关内容,最近三个月有哪些?」

三个入口(网页 / 命令行 / MCP)共用同一套逻辑、同一个数据库,并共用
**跨进程采集锁** —— MCP 触发采集时,命令行和网页会被正确拦住,
不会两个进程一起打抖音接口(那是风控的头号诱因)。

跑法(stdio):
    .venv/bin/python backend/mcp_server.py
接进 Claude Code / Cursor 的配置见 README 的 MCP 一节。

注:本文件针对 mcp SDK **1.x**。为什么不用 2.x ——
    2.x 会把 pydantic 升到 2.13,而 f2 钉死 pydantic==2.9.*,
    等于发布一套已知冲突的依赖。1.x 与 f2 的钉子完全兼容,`pip check` 干净。
    (踩过:requirements 写 `mcp>=1.2` 时,我的环境解析到 2.0、干净克隆解析到
     1.12.4,同一份代码一边能跑一边 ModuleNotFoundError。所以必须钉 <2。)
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _pyversion
_pyversion.check()   # Python 版本不对就早失败,别让人撞 Rust 编译错误

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

import planner
import service
from db import store

Scope = Literal["collection", "like", "post"]
SRC_LABEL = {"collection": "收藏", "like": "点赞", "post": "我的作品", "collects": "收藏夹"}

SERVER_INSTRUCTIONS = (
        "我的抖音收藏知识库(本地 SQLite)。可检索收藏/点赞/我发布的作品,"
        "按关键词、话题标签、作者、来源、抖音官方分类筛选,按互动数据排序;"
        "也能查看采集完整度并触发采集。\n"
        "两种检索分工:search_videos 字面匹配(找专名/作者/按分类筛选排序);"
        "search_library 语义匹配(问「关于怎么…的内容」用它,换句话说也找得到)。"
        "search_library 默认只搜用户主动收藏的;问「我关注的人讲过吗」才传 scope。"
        "回答「我收藏过的关于X」用 search_library,它返回的分数一定要看 —— "
        "verdict=nothing 就是库里真没有,verdict=only_maybe 是只有猜测 —— "
        "两种都别拿低分结果硬答、也别用自己的知识补。\n"
        "注意区分两种文本:text 是作者写的文案/标题,content 是抖音自己生成的"
        "视频内容总结(实测约 17% 的作品有,多为长视频/知识类)—— "
        "问「视频里讲了什么」只有 content 算。\n"
        "库里存了每条作品的完整响应(787 字段),常用字段之外的用 video_raw 取。\n"
    "采集有抖音风控约束:同一时刻只允许一个采集任务,403 会自动指数退避。"
)

app = Server("douyin-db", instructions=SERVER_INSTRUCTIONS)


def _slim(v: dict[str, Any]) -> dict[str, Any]:
    """给 AI 的精简视图:不带 17 KB 的压缩 raw,只留可推理字段。

    要看完整响应(787 个字段)用 video_raw 工具。
    """
    cats = [v.get("cat1"), v.get("cat2"), v.get("cat3")]

    def as_list(x: Any) -> list[str]:
        # list_videos 用 GROUP_CONCAT 给逗号串,get_video 给真列表 —— 都兜住
        if isinstance(x, list):
            return [s for s in x if s]
        return [s for s in (x or "").split(",") if s]

    out = {
        "id": v["aweme_id"],
        "author": v.get("nickname"),
        # text 是作者写的文案/标题;content 才是视频讲了什么
        "text": v.get("description"),
        "content": v.get("ai_summary"),
        "category": " > ".join(c for c in cats if c) or None,
        "published": (v.get("create_time") or "")[:10],
        "duration_sec": round((v.get("video_duration") or 0) / 1000) or None,
        "likes": v.get("digg_count"),
        "collects": v.get("collect_count"),
        "sources": [SRC_LABEL.get(s, s) for s in as_list(v.get("sources"))],
        "tags": as_list(v.get("tags")),
        "url": v.get("share_url"),
    }
    return {k: val for k, val in out.items() if val not in (None, [], "")}


# ── 检索 ────────────────────────────────────────────────────

async def search_videos(
    query: str | None = None,
    source: Scope | None = None,
    tag: str | None = None,
    author: str | None = None,
    category: str | None = None,
    only_with_content: bool = False,
    sort: Literal["collected", "published", "duration", "author",
                  "digg", "collect", "comment"] = "collected",
    limit: int = 20,
) -> dict[str, Any]:
    """在我的抖音知识库里检索作品。

    这是回答「我收藏过的关于 X 的内容」这类问题的主入口。

    query:    关键词,匹配文案 / 作者 / 音乐名 / 平台 AI 总结
    source:   collection=收藏, like=点赞, post=我发布的
    tag:      话题标签(不带 #),大小写不敏感
    author:   作者昵称,需完全匹配
    category: 抖音官方一级分类(如 科技、人文社科),用 library_stats 看有哪些
    only_with_content: 只要有 content(平台 AI 总结)的。想读「视频讲了什么」
              而不是作者写的标题时打开它 —— 实测约 17% 的作品有。
    sort:     collected=按存入时间(默认) · published=按发布时间 ·
              digg/collect/comment=按互动数据,用来挑真正优质的内容
    """
    store.init_db()
    limit = max(1, min(limit, 100))
    content = 'have' if only_with_content else None
    rows = await asyncio.to_thread(
        store.list_videos, query, source, limit, 0, None, author, sort, tag,
        category, content,
    )
    total = await asyncio.to_thread(
        store.count_videos, query, source, None, author, tag, category, content
    )
    return {"matched": total, "returned": len(rows), "items": [_slim(r) for r in rows]}


async def search_library(query: str, limit: int = 10,
                         include_maybe: bool = True,
                         scope: str = "mine") -> dict[str, Any]:
    """**语义**检索我的抖音收藏 —— 换句话说也找得到。

    和 search_videos 的分工:
      search_videos   字面匹配。找专名、找作者、按分类/互动数筛选排序时用。
      search_library  语义匹配。用户问「关于怎么…的内容」「讲…的视频」时用这个。

    先看 **verdict**,它只有三个值,不会误读:
      relevant     有确定相关的,在 good 里
      only_maybe   **只有「可能相关」** —— 不要当成答案,要告诉用户这是猜的
      nothing      库里真没有。**不要**用你自己的知识补,直接说没有。

    每条都带 score。英文查询比中文弱(实测会出硬错),分数低的更要谨慎。

    每条带 at_sec(命中的是哪一章、第几秒),引用时给出来。

    **scope** 决定搜哪一侧,默认 mine:
      mine       只搜用户**主动收藏/点赞**的 —— 默认,几乎总是对的
      following  只搜从关注者主页爬来的、用户自己**没存过**的作品
      all        两边一起

    什么时候该用 following/all:用户明确说「我关注的人有没有讲过…」
    「不限于我收藏的」时。**不要**自己擅自换成 all 来凑答案 ——
    关注者的全量产出比用户的收藏多一个数量级,混进来就等于拿别人的内容农场
    冒充「我的收藏」,而用户分辨不出来。返回体里带 scope,答的时候说清是哪一侧。
    """
    from knowledge import search as ks, vecdb
    store.init_db()
    try:
        if scope not in ("mine", "following", "all"):
            return {"error": f"scope 只能是 mine / following / all,收到 {scope!r}"}
        return await asyncio.to_thread(
            ks.search, query, max(1, min(limit, 50)), include_maybe, scope)
    except (vecdb.IndexMismatch, RuntimeError) as e:
        return {"error": str(e),
                "hint": "先跑 scripts/build_index.py 建向量索引"}


async def ask_library(question: str, k: int = 8) -> dict[str, Any]:
    """基于我的收藏回答问题,**答案带出处**。

    检索一条都没过线时不调模型,直接回「库里没有」—— 没有依据时让模型
    回答它一定会编,而编出来的分辨不出来。

    返回里 dropped_bogus_citations 非空,说明模型引用了不存在的编号
    (已剔除),那是这次回答不太可靠的直接信号,该告诉用户。
    """
    from knowledge import answer as ka
    from knowledge import vecdb
    store.init_db()
    try:
        return await asyncio.to_thread(ka.ask, question, max(1, min(k, 20)))
    except (vecdb.IndexMismatch, RuntimeError) as e:
        return {"error": str(e)}


async def video_raw(aweme_id: str, path: str | None = None) -> dict[str, Any]:
    """看一条作品的**完整原始响应**(抖音给的全部 787 个字段)。

    检索结果只给「这一轮提升出来的」字段。库里存的是完整响应且一个字段都没删,
    所以想看别的维度(评论配置、多码率地址、活动信息、锚点…)直接来这里,
    不需要重新采集。

    aweme_id: 作品 id
    path:     可选,点号路径(如 `statistics` 或 `video.play_addr`),
              只取这一小块。不给就只返回顶层字段名清单(避免一次灌几十 KB)。
    """
    store.init_db()
    raw = await asyncio.to_thread(store.get_raw, aweme_id)
    if raw is None:
        return {"error": "没有完整响应(早期数据只有 31 个字段,需要 refill 重采)"}
    if path:
        node: Any = raw
        for k in path.split("."):
            if isinstance(node, list):
                node = node[0] if node else None
            if not isinstance(node, dict):
                return {"error": f"路径 {path} 在 {k} 处断了"}
            node = node.get(k)
        return {"aweme_id": aweme_id, "path": path, "value": node}
    return {
        "aweme_id": aweme_id,
        "field_count": len(raw),
        "top_level_keys": sorted(raw.keys()),
        "hint": "用 path 参数取具体子树,例如 path='statistics'",
    }


async def creators(role: str | None = None) -> dict[str, Any]:
    """我关注的人 + 他们的分类。

    `role` 过滤:info=信息价值主播 · rival=竞品主播 · 不传=全部。

    每位都带 **saved_n**(我实际存过他几条作品)—— 那是判断值不值得跟的
    唯一实证信号。实测关注的 97 位里有 55 位我一条都没存过。
    """
    store.init_db()
    items = await asyncio.to_thread(store.list_following, False)
    if role:
        items = [u for u in items if u.get("role") == role]
    return {
        "items": [{k: u[k] for k in
                   ("sec_user_id", "nickname", "signature", "aweme_count",
                    "follower_count", "saved_n", "saved_with_summary",
                    "role", "crawled_n") if k in u} for u in items],
        "total": len(items),
        "n_info": sum(1 for u in items if u.get("role") == "info"),
        "n_rival": sum(1 for u in items if u.get("role") == "rival"),
    }


async def set_creator_role(sec_user_ids: list[str],
                           role: str | None = None) -> dict[str, Any]:
    """给博主分类,并开始每天跟他。

    role:
      info   信息价值主播 —— 要的是他**讲了什么**,进知识库能检索能问答
      rival  竞品主播     —— 要的是他的**打法**,选题/时长/节奏/互动和上期对比
      null   不跟了

    分类之后 `daily_round` 才会抓他。**别替用户决定谁是竞品** ——
    那是业务判断,不是数据能推出来的;用户没说就先问。
    """
    store.init_db()
    if role not in (None, "info", "rival"):
        return {"error": f"role 只能是 info / rival / null,收到 {role!r}"}
    if not sec_user_ids:
        return {"error": "要给 sec_user_ids(数组),用 creators 拿 id"}
    try:
        n = await asyncio.to_thread(store.set_following_role, sec_user_ids, role)
    except ValueError as e:
        return {"error": str(e)}
    return {"updated": n, "role": role}


async def daily_digest(days: int = 3) -> dict[str, Any]:
    """**他们这几天讲了什么** —— 这是回答「我关注的人最近有什么新内容」的主入口。

    只读本地库,不联网。要先抓新作品的话用 `collect_recent`。

    返回两块:
      info   信息价值主播的新内容。每条带 **content_src**:
               summary = 抖音自己生成的内容总结
               asr     = 我本地转写的逐字稿
               desc    = **只有作者文案,不是视频内容** —— 这条其实什么都没说,
                         别拿它当内容回答用户,要说「这条还没转写」
      rival  竞品主播的打法,每个数字都配了上一个同长度周期的对比
    """
    from knowledge import digest as kd
    store.init_db()
    days = max(1, min(days, 90))
    ov, info, rival = await asyncio.gather(
        asyncio.to_thread(kd.overview, days),
        asyncio.to_thread(kd.info_digest, days),
        asyncio.to_thread(kd.rival_report, days),
    )
    return {"overview": ov, "info": info, "rival": rival}


async def collect_recent(days: int = 3, role: str | None = None) -> dict[str, Any]:
    """抓已分类博主最近 N 天的新作品。**这是日常该用的采集**。

    每人翻一页左右(他们每天发 0.1–0.5 条),十来位约一分半,几乎不会触发限流。
    对比:全量爬他们的历史要 10 小时,而且实测第 20 页就被 403。
    """
    store.init_db()
    try:
        return await service.daily_round(days=max(1, min(days, 30)), role=role)
    except service.AlreadyCollecting as e:
        return {"error": str(e)}
    except RuntimeError as e:      # f2 加载不了(网络)
        return {"error": str(e)}


async def transcribe(limit: int = 10, scope: str = "following") -> dict[str, Any]:
    """把没有内容的作品转成文字(本地 Whisper,不联网问抖音)。

    为什么需要:**抖音的响应里没有字幕文本**(逐字段翻过两遍,只有一个
    is_subtitled 标记),而平台自己的内容总结只覆盖三分之一 ——
    剩下的在库里只有营销文案。转写是补齐它们的唯一办法。

    转完会自动重建片段并可被检索。首次调用要下模型(1.6GB),会比较久。
    """
    from knowledge import asr as ka
    store.init_db()
    try:
        return await asyncio.to_thread(
            ka.run_batch, max(1, min(limit, 100)), scope, None, None)
    except RuntimeError as e:
        return {"error": str(e)}


async def library_stats(top_tags: int = 20) -> dict[str, Any]:
    """知识库概览:总条数、各来源分布、作者数、官方分类、热门话题标签,
    以及各字段的实际覆盖率。

    categories 是抖音官方打的一级分类,可直接作为 search_videos 的 category 取值。
    coverage 说明哪些维度还不全 —— 比如 with_content 就是有平台 AI 总结的条数,
    只有这些能回答「视频里讲了什么」;其余只有作者写的文案。
    """
    store.init_db()
    s = await asyncio.to_thread(store.stats)
    tags = await asyncio.to_thread(store.top_tags, max(1, min(top_tags, 100)))
    cats = await asyncio.to_thread(store.top_categories, 20)
    cov = await asyncio.to_thread(store.coverage)
    return {
        "total": s["total"],
        "with_text": s["with_description"],
        # 三态都给 AI:它必须能区分「抖音确认没给」和「还没采全所以不知道」,
        # 否则会把后者当成「这条没内容」而给出错误结论。
        "with_content": s["content_have"],
        "content_confirmed_none": s["content_none"],
        "content_unknown_need_refill": s["content_unknown"],
        "authors": s["authors"],
        "by_source": {SRC_LABEL.get(k, k): v for k, v in (s["by_source"] or {}).items()},
        "categories": [{"category": c["cat"], "count": c["n"]} for c in cats],
        "top_tags": [{"tag": t["tag"], "count": t["n"]} for t in tags],
        "coverage": {
            "full_raw": cov["full_raw"],
            "engagement": cov["digg_count"],
            "official_category": cov["cat1"],
            "ai_summary": cov["ai_summary"],
            # 早期采集只存了 f2 暴露的 31 个字段,这些条目推不出上面的维度
            "legacy_rows_need_refill": cov["total"] - cov["digg_count"],
        },
    }


# ── 采集 ────────────────────────────────────────────────────

async def collect_status() -> dict[str, Any]:
    """采集进度与下一步计划。

    给出每类的「已采 / 抖音平台总数 / 完成度」,以及当前是否有采集在跑
    (可能来自命令行或网页 —— 三个入口共用跨进程锁)。

    缺口不一定是漏采:原作者删稿 / 设为私密 / 账号注销后,抖音列表里
    就再也拉不到,但计数器还算着。系统连续确认两遍后会停止追这个差额。
    """
    store.init_db()
    steps = await asyncio.to_thread(planner.plan_all)
    run = await asyncio.to_thread(store.active_run)
    return {
        "collecting": bool(run),
        "collecting_from": (run or {}).get("origin"),
        "progress": (run or {}).get("progress"),
        "scopes": [
            {
                "scope": s["scope"], "label": s["label"],
                "collected": s["collected"], "platform_total": s.get("total"),
                "percent": s.get("percent"), "gap": s.get("gap"),
                "next_action": s["action"], "reason": s["reason"],
            }
            for s in steps
        ],
    }


async def collect_smart(wait: bool = False) -> dict[str, Any]:
    """触发智能采集(日常唯一需要的采集动作)。

    每类自动判断:没采完就续采历史(有页数上限,采够主动收手)、
    已采完就做增量同步(从最新扫,连续 3 页无新增即停)、
    被限流就跳过(冷却期内不去试探)。403 自动指数退避 30分→1→2→4→6小时。

    wait=False(默认)立刻返回,之后用 collect_status 查进度;
    wait=True 等它跑完 —— 抖音有风控刻意留了翻页间隔,一轮可能要几分钟。
    """
    store.init_db()
    try:
        await asyncio.to_thread(service.guard_single_run)
    except service.AlreadyCollecting as e:
        return {"started": False, "reason": str(e)}

    if wait:
        r = await service.smart_collect()
        return {
            "started": True, "finished": True, "inserted": r["inserted"],
            "steps": [
                {"label": s["label"], "status": s["status"],
                 "inserted": s.get("inserted", 0), "reason": s.get("reason")}
                for s in r["steps"]
            ],
        }

    asyncio.create_task(_background_collect())
    return {"started": True, "finished": False,
            "hint": "已在后台开始,用 collect_status 查进度"}


async def collect_scope(
    scope: Scope,
    mode: Literal["resume", "sync", "fresh"] = "sync",
    max_items: int | None = None,
) -> dict[str, Any]:
    """只采某一类,用于需要精确控制时。日常请用 collect_smart。

    mode:
      sync   从最新扫,只找新增(连续 3 页无新增即停)。**不会动深挖游标。**
      resume 从断点往历史深处继续采
      fresh  忽略游标从最新完整重采 —— 会清掉深挖进度,慎用
    """
    store.init_db()
    if scope not in service._COLLECTORS:
        return {"error": f"未知分类:{scope}"}
    try:
        await asyncio.to_thread(service.guard_single_run)
    except service.AlreadyCollecting as e:
        return {"started": False, "reason": str(e)}

    kw: dict[str, Any] = {"max_items": max_items}
    if mode == "sync":
        kw["sync"] = True
    else:
        kw["resume"] = mode == "resume"     # fresh → resume=False,显式清游标

    try:
        r = await service._COLLECTORS[scope](**kw)
        return {"scope": scope, "mode": mode, "inserted": r["inserted"],
                "fetched": r["fetched"], "pages": r["pages"],
                "exhausted": r.get("exhausted"), "hit_cap": r.get("hit_cap")}
    except Exception as e:
        return {"scope": scope, "error": f"{type(e).__name__}: {e}",
                "note": "已采部分与游标都保留了,可以再试。403 表示被风控限流。"}


async def refill(
    scope: Scope | None = None,
    max_pages: int = 0,
) -> dict[str, Any]:
    """回补完整字段:重走列表,把已有作品的原始响应与新字段补上。

    早期采集时只存了 f2 提取的 31 个字段,而抖音真实响应有 787 个 ——
    互动数据(赞/评/藏/转)、视频与音轨地址、雪碧图、结构化话题、尺寸都丢了。
    这些只能重新请求才拿得到,且媒体地址带 x-expires 会过期,越早补越好。

    不会动深挖游标,也不会提前停(整页已知条目也要继续,目的就是更新它们)。
    """
    store.init_db()
    try:
        await asyncio.to_thread(service.guard_single_run)
    except service.AlreadyCollecting as e:
        return {"started": False, "reason": str(e)}

    scopes = [scope] if scope else ["collection", "like", "post"]
    out = []
    for sc in scopes:
        try:
            r = await service.refill_scope(sc, max_pages=max_pages)
            out.append({"scope": sc, "pages": r["pages"], "updated": r["fetched"],
                        "hit_cap": r.get("hit_cap")})
        except Exception as e:
            out.append({"scope": sc, "error": f"{type(e).__name__}: {e}"})
    return {"results": out}


async def refresh_totals(
    manual_scope: Scope | None = None,
    manual_total: int | None = None,
) -> dict[str, Any]:
    """刷新「平台总数」,即采集完整度的分母。

    从抖音 self 端点取作品 / 点赞 / 收藏三个官方计数(只发一个请求)。
    也可以用 manual_scope + manual_total 手填覆盖(留空 manual_total 则清除)。
    """
    store.init_db()
    out: dict[str, Any] = {}
    if manual_scope:
        try:
            await asyncio.to_thread(planner.set_manual_total, manual_scope, manual_total)
            out["manual_set"] = {manual_scope: manual_total}
        except ValueError as e:
            return {"error": str(e)}

    try:
        from collector import totals
        t = await totals.fetch()
        await asyncio.to_thread(planner.save_totals, t)
        out["fetched"] = t
    except Exception as e:
        out["fetch_error"] = f"{type(e).__name__}: {e}"

    out["scopes"] = [
        {"label": s["label"], "collected": s["collected"],
         "total": s.get("total"), "percent": s.get("percent")}
        for s in await asyncio.to_thread(planner.plan_all)
    ]
    return out


async def rebuild_tags() -> dict[str, Any]:
    """从所有作品文案里重抽 #话题标签,重建标签索引。

    纯本地计算,不发任何网络请求。采集后会自动执行,一般不用手动调。
    """
    store.init_db()
    tagged, distinct = await asyncio.to_thread(store.rebuild_tags)
    return {"tagged_videos": tagged, "distinct_tags": distinct}


# ── 环境诊断 ────────────────────────────────────────────────

async def auth_status() -> dict[str, Any]:
    """登录与环境状态,以及缺什么该跑哪条命令。

    注意:扫码登录**无法由 AI 代跑**(需要真人用抖音 App 扫码),
    必须在本机终端执行。这里只做诊断并给出命令。
    """
    import config

    store.init_db()
    await asyncio.to_thread(config.reload)
    st = config.settings
    keys = {p.split("=", 1)[0].strip() for p in st.douyin_cookie.split(";") if "=" in p}
    logged_in = bool({"sessionid", "sessionid_ss", "sid_tt", "sid_guard", "uid_tt"} & keys)

    web_up = False
    try:
        import httpx
        # 探本机服务也要绕代理:NO_PROXY 里虽然有 127.0.0.1,但不是每个环境
        # 都配了,漏配时这个 2 秒探测会走代理然后超时,把「服务没起」误报出来。
        async with httpx.AsyncClient(timeout=2, trust_env=False) as c:
            web_up = (await c.get("http://127.0.0.1:8000/api/health")).status_code == 200
    except Exception:
        pass

    todo: list[str] = []
    if not logged_in:
        todo.append(".venv/bin/python backend/cli.py qrlogin --keep-session   # 需真人扫码")
    if not st.douyin_sec_user_id.strip():
        todo.append(".venv/bin/python backend/cli.py whoami")
    if not web_up:
        todo.append(".venv/bin/python -m uvicorn main:app --app-dir backend --port 8000")

    return {
        "cookie_present": bool(st.douyin_cookie.strip()),
        "cookie_logged_in": logged_in,
        "sec_user_id_resolved": bool(st.douyin_sec_user_id.strip()),
        "db": str(st.db_file),
        "web_running": web_up,
        "web_url": "http://localhost:8000" if web_up else None,
        "todo": todo or ["一切就绪"],
    }


async def _background_collect() -> None:
    try:
        await service.smart_collect()
    except Exception:
        pass    # 失败原因已写入 collect_runs,collect_status 里能看到


# ── MCP 外壳:把上面的纯函数暴露成工具 ────────────────────────
# 1.x 需要手写 schema(2.x 才有类型注解自动生成)。
_TOOLS: list[Tool] = [
    Tool(
        name="search_videos",
        description=(
            "在我的抖音知识库里检索作品。回答「我收藏过的关于X的内容」这类问题的主入口。"
            "可按关键词(文案/作者/音乐名/平台AI总结)、来源、话题标签、作者、"
            "抖音官方分类组合筛选,并按互动数据排序挑优质内容。"
            "返回的 text 是作者写的文案,content 才是视频讲了什么。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "关键词,匹配文案/作者/音乐名/平台AI总结"},
                "source": {"type": "string", "enum": ["collection", "like", "post"],
                           "description": "collection=收藏, like=点赞, post=我发布的"},
                "tag": {"type": "string", "description": "话题标签(不带#),大小写不敏感"},
                "author": {"type": "string", "description": "作者昵称,需完全匹配"},
                "category": {"type": "string",
                             "description": "抖音官方一级分类(如 科技、人文社科)。"
                                            "取值见 library_stats 的 categories"},
                "only_with_content": {
                    "type": "boolean", "default": False,
                    "description": "只要有 content(平台 AI 总结)的 —— 想读「视频讲了什么」"
                                   "而不是作者标题时打开。实测约 17% 的作品有"},
                "sort": {"type": "string",
                         "enum": ["collected", "published", "duration", "author",
                                  "digg", "collect", "comment"],
                         "default": "collected",
                         "description": "digg/collect/comment=按互动数据排,用来挑优质内容"},
                "limit": {"type": "integer", "default": 20, "maximum": 100},
            },
        },
    ),
    Tool(
        name="library_stats",
        description=(
            "知识库概览:总条数、来源分布、作者数、抖音官方分类、热门话题标签,"
            "以及各字段覆盖率(哪些维度还不全)。"
        ),
        inputSchema={"type": "object", "properties": {
            "top_tags": {"type": "integer", "default": 20, "maximum": 100}}},
    ),
    Tool(
        name="search_library",
        description=(
            "语义检索我的抖音收藏 —— 换句话说也找得到(问「关于怎么练口语的内容」"
            "能命中标题里没这几个字的视频)。和 search_videos 分工:那个是字面匹配,"
            "适合找专名/作者/按分类筛选;这个是语义匹配。"
            "先看返回里的 verdict:relevant=有确定相关的 · only_maybe=只有可能相关"
            "(不要当答案)· nothing=库里真没有(不要用自己的知识补)。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "自然语言问法,中英文都行"},
                "limit": {"type": "integer", "default": 10, "maximum": 50},
                "include_maybe": {"type": "boolean", "default": True,
                                  "description": "是否带上「可能相关」那一档"},
                "scope": {
                    "type": "string", "enum": ["mine", "following", "all"],
                    "default": "mine",
                    "description":
                        "搜哪一侧。mine=用户主动收藏/点赞的(默认,几乎总是对的);"
                        "following=从关注者主页爬来、用户自己没存过的;all=两边。"
                        "只有用户明确说「我关注的人有没有讲过…」时才换 —— "
                        "关注者的全量产出比收藏多一个数量级,擅自用 all 凑答案"
                        "等于拿别人的内容农场冒充「我的收藏」,用户分辨不出来。",
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="daily_digest",
        description=(
            "**我关注的人这几天讲了什么** —— 回答「最近有什么新内容」「竞品在做什么」"
            "的主入口。只读本地,不联网。"
            "返回 info(信息价值主播的新内容)和 rival(竞品的打法,每个数字都配"
            "上一周期对比)。"
            "⚠️ info 里每条带 content_src:summary=抖音总结 · asr=本地转写的逐字稿 · "
            "**desc=只有作者文案,不是视频内容** —— desc 的那条其实什么都没说,"
            "不要拿它当内容回答,要说「这条还没转写」。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "default": 3, "minimum": 1, "maximum": 90,
                         "description": "看最近几天,默认 3"},
            },
        },
    ),
    Tool(
        name="creators",
        description=(
            "我关注的人 + 分类。每位带 saved_n(我实际存过他几条作品)—— "
            "那是判断值不值得跟的唯一实证信号(实测关注的 97 位里 55 位一条都没存过)。"
            "要给人分类前先用这个拿 sec_user_id。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "role": {"type": "string", "enum": ["info", "rival"],
                         "description": "只看某一类;不传=全部"},
            },
        },
    ),
    Tool(
        name="set_creator_role",
        description=(
            "给博主分类并开始每天跟他。info=信息价值(要他讲的内容,进知识库)· "
            "rival=竞品(要他的打法,和上期对比)· null=不跟了。"
            "⚠️ **谁是竞品是业务判断,数据推不出来** —— 用户没明说就先问,别替他决定。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "sec_user_ids": {"type": "array", "items": {"type": "string"},
                                 "description": "从 creators 拿"},
                "role": {"type": ["string", "null"], "enum": ["info", "rival", None],
                         "description": "info / rival / null(取消)"},
            },
            "required": ["sec_user_ids"],
        },
    ),
    Tool(
        name="collect_recent",
        description=(
            "抓已分类博主最近 N 天的新作品。**这是日常该用的采集** —— "
            "每人翻一页左右,十来位约一分半,几乎不触发限流。"
            "(对比:全量爬他们的历史要 10 小时,实测第 20 页就被 403。)"
            "抓完用 daily_digest 看结果。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "default": 3, "minimum": 1, "maximum": 30},
                "role": {"type": "string", "enum": ["info", "rival"],
                         "description": "只抓某一类;不传=两类都抓"},
            },
        },
    ),
    Tool(
        name="transcribe",
        description=(
            "把没有内容的作品转成文字(本地 Whisper,不联网问抖音)。"
            "抖音的响应里**没有字幕文本**,而它自己的内容总结只覆盖三分之一 —— "
            "剩下的在库里只有营销文案,转写是补齐的唯一办法。"
            "转完自动进检索。首次要下模型(1.6GB),会比较久。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 100},
                "scope": {"type": "string", "enum": ["mine", "following", "all"],
                          "default": "following"},
            },
        },
    ),
    Tool(
        name="ask_library",
        description=(
            "基于我的收藏回答问题,答案强制带出处(作者 / 链接 / 第几秒)。"
            "检索没有结果时不会编答案,直接说库里没有。"
            "需要服务端配好 DASHSCOPE_API_KEY;只想检索不问答就用 search_library。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "k": {"type": "integer", "default": 8, "maximum": 20,
                      "description": "给模型几条资料"},
            },
            "required": ["question"],
        },
    ),
    Tool(
        name="video_raw",
        description=(
            "看一条作品的完整原始响应(抖音给的全部 787 个字段)。"
            "检索结果只给常用字段;库里存的是完整响应、一个字段都没删,"
            "所以想看别的维度直接用这个,不需要重新采集。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "aweme_id": {"type": "string"},
                "path": {"type": "string",
                         "description": "点号路径(如 statistics 或 video.play_addr),"
                                        "只取这一小块;不给则返回顶层字段名清单"},
            },
            "required": ["aweme_id"],
        },
    ),
    Tool(
        name="collect_status",
        description=(
            "采集进度与下一步计划:每类「已采 / 抖音平台总数 / 完成度」,"
            "以及当前是否有采集在跑(可能来自命令行或网页)。"
            "缺口不一定是漏采 —— 原作者删稿后抖音列表就拉不到,但计数器还算着。"
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="collect_smart",
        description=(
            "触发智能采集(日常唯一需要的采集动作)。每类自动判断续采/增量/跳过,"
            "403 自动指数退避。同一时刻只允许一个采集,命令行或网页在采时会被拒。"
        ),
        inputSchema={"type": "object", "properties": {
            "wait": {"type": "boolean", "default": False,
                     "description": "true=等跑完(可能几分钟);false=立刻返回,之后用 collect_status 查"}}},
    ),
    Tool(
        name="collect_scope",
        description=(
            "只采某一类。日常请用 collect_smart。"
            "mode: sync=从最新只找新增(不动深挖游标);resume=从断点往历史采;"
            "fresh=清游标完整重采(慎用)。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["collection", "like", "post"]},
                "mode": {"type": "string", "enum": ["resume", "sync", "fresh"], "default": "sync"},
                "max_items": {"type": "integer"},
            },
            "required": ["scope"],
        },
    ),
    Tool(
        name="refill",
        description=(
            "回补完整字段。早期采集只存了 31/787 个字段,互动数据、视频与音轨地址、"
            "雪碧图、结构化话题、尺寸都丢了 —— 只能重走列表补回。"
            "不动深挖游标,不提前停。媒体地址会过期,越早补越好。"
        ),
        inputSchema={"type": "object", "properties": {
            "scope": {"type": "string", "enum": ["collection", "like", "post"],
                      "description": "留空则三类都补"},
            "max_pages": {"type": "integer", "description": "单次页数上限,0=不限"}}},
    ),
    Tool(
        name="refresh_totals",
        description=(
            "刷新「平台总数」(完整度的分母),从抖音 self 端点取作品/点赞/收藏三个官方计数。"
            "也可用 manual_scope + manual_total 手填覆盖。"
        ),
        inputSchema={"type": "object", "properties": {
            "manual_scope": {"type": "string", "enum": ["collection", "like", "post"]},
            "manual_total": {"type": "integer"}}},
    ),
    Tool(
        name="rebuild_tags",
        description="从作品文案重抽 #话题标签。纯本地计算,不发网络请求。",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="auth_status",
        description=(
            "登录与环境状态,以及缺什么该跑哪条命令。"
            "扫码登录无法由 AI 代跑(需真人扫码),这里只做诊断。"
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
]

_HANDLERS = {
    "search_videos": lambda a: search_videos(**a),
    "library_stats": lambda a: library_stats(**a),
    "search_library": lambda a: search_library(**a),
    "daily_digest": lambda a: daily_digest(**a),
    "creators": lambda a: creators(**a),
    "set_creator_role": lambda a: set_creator_role(**a),
    "collect_recent": lambda a: collect_recent(**a),
    "transcribe": lambda a: transcribe(**a),
    "ask_library": lambda a: ask_library(**a),
    "video_raw": lambda a: video_raw(**a),
    "collect_status": lambda a: collect_status(),
    "collect_smart": lambda a: collect_smart(**a),
    "collect_scope": lambda a: collect_scope(**a),
    "refill": lambda a: refill(**a),
    "refresh_totals": lambda a: refresh_totals(**a),
    "rebuild_tags": lambda a: rebuild_tags(),
    "auth_status": lambda a: auth_status(),
}


@app.list_tools()
async def _list_tools() -> list[Tool]:
    return _TOOLS


@app.call_tool()
async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
    fn = _HANDLERS.get(name)
    if not fn:
        payload: Any = {"error": f"未知工具:{name}"}
    else:
        try:
            payload = await fn(arguments or {})
        except TypeError as e:      # 参数不匹配,报清楚而不是栈
            payload = {"error": f"参数错误:{e}"}
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2))]


async def _main() -> None:
    service.ORIGIN = "mcp"      # 采集记录里能区分是 AI 触发的
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_main())
