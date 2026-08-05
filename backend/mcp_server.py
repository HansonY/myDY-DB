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
        "按关键词、话题标签、作者、来源筛选;也能查看采集完整度并触发采集。\n"
        "回答「我收藏过的关于X」这类问题时用 search_videos。\n"
    "采集有抖音风控约束:同一时刻只允许一个采集任务,403 会自动指数退避。"
)

app = Server("douyin-db", instructions=SERVER_INSTRUCTIONS)


def _slim(v: dict[str, Any]) -> dict[str, Any]:
    """给 AI 的精简视图:去掉 raw_json 等噪音,保留可推理字段。"""
    return {
        "id": v["aweme_id"],
        "author": v.get("nickname"),
        "text": v.get("description"),
        "published": (v.get("create_time") or "")[:10],
        "duration_sec": round((v.get("video_duration") or 0) / 1000) or None,
        "sources": [SRC_LABEL.get(s, s) for s in (v.get("sources") or "").split(",") if s],
        "tags": [t for t in (v.get("tags") or "").split(",") if t],
        "url": v.get("share_url"),
    }


# ── 检索 ────────────────────────────────────────────────────

async def search_videos(
    query: str | None = None,
    source: Scope | None = None,
    tag: str | None = None,
    author: str | None = None,
    sort: Literal["collected", "published", "duration", "author"] = "collected",
    limit: int = 20,
) -> dict[str, Any]:
    """在我的抖音知识库里检索作品。

    这是回答「我收藏过的关于 X 的内容」这类问题的主入口。

    query:  关键词,匹配文案 / 作者 / 音乐名
    source: collection=收藏, like=点赞, post=我发布的
    tag:    话题标签(不带 #),大小写不敏感
    author: 作者昵称,需完全匹配
    sort:   collected=按存入时间(默认), published=按发布时间
    """
    store.init_db()
    limit = max(1, min(limit, 100))
    rows = await asyncio.to_thread(
        store.list_videos, query, source, limit, 0, None, author, sort, tag
    )
    total = await asyncio.to_thread(store.count_videos, query, source, None, author, tag)
    return {"matched": total, "returned": len(rows), "items": [_slim(r) for r in rows]}


async def library_stats(top_tags: int = 20) -> dict[str, Any]:
    """知识库概览:总条数、各来源分布、有文案数、作者数,以及热门话题标签。

    话题标签是从作品文案里的 #hashtag 抽出来的,零 AI 成本,可直接当类目用。
    """
    store.init_db()
    s = await asyncio.to_thread(store.stats)
    tags = await asyncio.to_thread(store.top_tags, max(1, min(top_tags, 100)))
    return {
        "total": s["total"],
        "with_text": s["with_description"],
        "authors": s["authors"],
        "by_source": {SRC_LABEL.get(k, k): v for k, v in (s["by_source"] or {}).items()},
        "top_tags": [{"tag": t["tag"], "count": t["n"]} for t in tags],
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
        async with httpx.AsyncClient(timeout=2) as c:
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
            "可按关键词(匹配文案/作者/音乐名)、来源、话题标签、作者组合筛选。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词,匹配文案/作者/音乐名"},
                "source": {"type": "string", "enum": ["collection", "like", "post"],
                           "description": "collection=收藏, like=点赞, post=我发布的"},
                "tag": {"type": "string", "description": "话题标签(不带#),大小写不敏感"},
                "author": {"type": "string", "description": "作者昵称,需完全匹配"},
                "sort": {"type": "string", "enum": ["collected", "published", "duration", "author"],
                         "default": "collected"},
                "limit": {"type": "integer", "default": 20, "maximum": 100},
            },
        },
    ),
    Tool(
        name="library_stats",
        description="知识库概览:总条数、各来源分布、有文案数、作者数,以及热门话题标签。",
        inputSchema={"type": "object", "properties": {
            "top_tags": {"type": "integer", "default": 20, "maximum": 100}}},
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
