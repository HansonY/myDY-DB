"""FastAPI 后端。

采集是长任务(几百条 × 翻页间隔),所以走后台任务 + 轮询 /api/runs 看进度,
不阻塞请求。同一时刻只允许一个采集任务 —— 并发请求抖音接口是风控的主要诱因。
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _pyversion
_pyversion.check()   # Python 版本不对就早失败,别让人撞 Rust 编译错误

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

import config
import service
from config import ROOT, settings
from db import store

STATIC_DIR = Path(__file__).resolve().parent / "static"
COVER_DIR = ROOT / "data" / "covers"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.init_db()
    service.ORIGIN = "web"      # 界面里要能区分「网页在采」和「命令行在采」
    yield


app = FastAPI(
    title="Douyin-DB",
    description="抖音收藏夹 → 个人知识库",
    version="0.1.0",
    lifespan=lifespan,
)

# 自部署场景:前端跑在本机另一个端口
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局采集锁:同一时刻只跑一个采集任务
_collect_lock = asyncio.Lock()
_last_progress: dict[str, Any] = {}


# ── 只读接口 ────────────────────────────────────────────────

@app.get("/api/health")
async def health() -> dict[str, Any]:
    # 重载配置:用户可能在服务运行期间跑了 qrlogin 改写 .env
    config.reload()
    return {
        "ok": True,
        "cookie_configured": settings.has_cookie,
        "db": str(settings.db_file),
        "collecting": bool(await asyncio.to_thread(store.active_run)),
    }


@app.get("/api/stats")
async def get_stats() -> dict[str, Any]:
    return await asyncio.to_thread(store.stats)


@app.get("/api/videos")
async def get_videos(
    q: str | None = None,
    source: str | None = None,
    collects_id: str | None = None,
    nickname: str | None = None,
    tag: str | None = None,
    cat1: str | None = None,
    # 内容总结三态筛选:have=有 · none=抖音确认没给 · unknown=还没采全
    content: str | None = None,
    sort: str = "collected",
    # 上限放宽到 500:网格视图下每页 50 条要翻 50 页,太碎
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    items = await asyncio.to_thread(
        store.list_videos, q, source, limit, offset, collects_id, nickname,
        sort, tag, cat1, content,
    )
    total = await asyncio.to_thread(
        store.count_videos, q, source, collects_id, nickname, tag, cat1, content
    )
    return {"total": total, "limit": limit, "offset": offset, "items": items}


@app.get("/api/authors")
async def get_authors(limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    return {"items": await asyncio.to_thread(store.top_authors, limit)}


@app.get("/api/tags")
async def get_tags(limit: int = Query(40, ge=1, le=300)) -> dict[str, Any]:
    return {"items": await asyncio.to_thread(store.top_tags, limit)}


@app.get("/api/categories")
async def get_categories(limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    """抖音官方一级分类 —— 平台自己打的,比从文案抠的 #标签 权威。"""
    return {"items": await asyncio.to_thread(store.top_categories, limit)}


@app.get("/api/search")
async def semantic_search(
    q: str,
    limit: int = Query(10, ge=1, le=50),
    include_maybe: bool = True,
) -> dict[str, Any]:
    """语义检索:换句话说也找得到。

    和 `/api/videos?q=` **刻意分成两个接口**。那一路是 LIKE 子串匹配
    (「MCP」「Claude Code」字面出现就命中),这一路是向量语义
    (「怎么练口语」也能找到)。合并之后「这条是怎么被找到的」就说不清了,
    出问题没法定位是哪一路的锅。

    分数一律带出来,三档:good(≥阈值)· maybe(可能相关)· 全没过就是库里没有。
    """
    from knowledge import search as ks, vecdb
    try:
        return await asyncio.to_thread(ks.search, q, limit, include_maybe)
    except vecdb.IndexMismatch as e:
        raise HTTPException(409, str(e)) from e
    except RuntimeError as e:      # 缺依赖 / 扩展加载不了
        raise HTTPException(501, str(e)) from e


@app.post("/api/ask")
async def ask(q: str, k: int = Query(8, ge=1, le=20)) -> dict[str, Any]:
    """基于收藏回答问题,**强制带出处**。

    检索一条都没过线时**不调模型**直接说没有 —— 没有依据时让模型回答,
    它一定会编,而编出来的你分辨不出来。
    """
    from knowledge import answer as ka
    from knowledge import vecdb
    try:
        return await asyncio.to_thread(ka.ask, q, k)
    except vecdb.IndexMismatch as e:
        raise HTTPException(409, str(e)) from e
    except RuntimeError as e:      # 缺 key / 缺依赖
        raise HTTPException(501, str(e)) from e


@app.get("/api/insight")
async def get_insight(
    force: bool = False,
    narrative: bool = False,
) -> dict[str, Any]:
    """自我分析。**数据没变就返回上次的结果**(指纹比对),不重算。

    force=true 强制重算并存一份新快照;narrative=true additionally 让 AI
    把数字写成一段话(需要 DASHSCOPE_API_KEY,没有就只回数字)。
    """
    from knowledge import insight as ki
    return await asyncio.to_thread(ki.analyze, force, narrative)


@app.get("/api/insight/graph")
async def insight_graph(
    min_count: int = Query(6, ge=2, le=50),
    min_edge: int = Query(3, ge=1, le=20),
    max_nodes: int = Query(70, ge=10, le=200),
) -> dict[str, Any]:
    """标签共现网络。节点=标签,边=同一作品上共现次数。

    共现比「标签排行」有信息量:排行只说哪个多,共现说**哪些兴趣连在一起**。
    """
    from knowledge import insight as ki
    return await asyncio.to_thread(ki.tag_graph, min_count, min_edge, max_nodes)


@app.get("/api/insight/history")
async def insight_history(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    """历史快照列表。两次之间的差就是时间信息 —— 抖音不给收藏时间,
    这是唯一能看出兴趣漂移的办法。"""
    return {"items": await asyncio.to_thread(store.insight_history, limit)}


@app.get("/api/insight/{insight_id}")
async def get_insight_by_id(insight_id: int) -> dict[str, Any]:
    row = await asyncio.to_thread(store.get_insight, insight_id)
    if not row:
        raise HTTPException(404, "没有这份快照")
    import json as _json
    return {**row, **_json.loads(row["stats_json"])}


@app.get("/api/search/status")
async def search_status() -> dict[str, Any]:
    """向量索引现状。不加载模型 —— 光看状态不该等 bge-m3 加载几秒。"""
    from knowledge import index as ki
    try:
        return await asyncio.to_thread(ki.status)
    except RuntimeError as e:
        return {"available": False, "reason": str(e)}


@app.get("/api/coverage")
async def get_coverage() -> dict[str, Any]:
    """各字段覆盖率。「数据到底全不全」要能一眼看到,不能靠推断 ——
    此前两次把「已采尽」判断错就是因为没有分母。"""
    cov = await asyncio.to_thread(store.coverage)
    cov["fragments"] = await asyncio.to_thread(store.fragment_stats)
    return cov


@app.post("/api/tags/rebuild")
async def rebuild_tags() -> dict[str, Any]:
    """从文案重抽 hashtag。零成本、无外部请求,采集后跑一次即可。"""
    tagged, distinct = await asyncio.to_thread(store.rebuild_tags)
    return {"tagged_videos": tagged, "distinct_tags": distinct}


@app.get("/api/cover/{aweme_id}")
async def get_cover(aweme_id: str):
    """封面图代理 + 本地缓存。

    为什么必须走服务端:
      1. **防盗链** —— 抖音 CDN 要 `Referer: https://www.douyin.com/`,
         浏览器从本站直连拿不到图(这就是列表里那些空白块的真因)。
      2. **URL 会过期** —— 所有封面 URL 都带 `x-expires` 签名参数。
         不落本地的话,过一段时间全部失效,知识库就没有缩略图了。
    缓存一次即永久可用。
    """
    safe_id = "".join(ch for ch in aweme_id if ch.isalnum())  # 防路径穿越
    if not safe_id:
        raise HTTPException(400, "非法 id")

    path = COVER_DIR / f"{safe_id}.jpg"
    if path.exists():
        return FileResponse(path, media_type="image/jpeg")

    url = await asyncio.to_thread(store.get_cover_url, safe_id)
    if not url:
        raise HTTPException(404, "没有封面")

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as cli:
            r = await cli.get(
                url,
                headers={
                    "Referer": "https://www.douyin.com/",
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
                    ),
                },
            )
    except Exception as e:
        raise HTTPException(502, f"取封面失败:{type(e).__name__}") from e

    if r.status_code != 200 or not r.content:
        # 多半是签名已过期 —— 无法补救,只能重采该作品
        raise HTTPException(404, f"封面已失效(上游 {r.status_code})")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(r.content)
    return Response(r.content, media_type="image/jpeg")


@app.get("/api/videos/{aweme_id}")
async def get_video(aweme_id: str) -> dict[str, Any]:
    row = await asyncio.to_thread(store.get_video, aweme_id)
    if not row:
        raise HTTPException(404, "作品不存在")
    return row


@app.get("/api/videos/{aweme_id}/raw")
async def get_video_raw(aweme_id: str) -> dict[str, Any]:
    """完整原始响应(787 个字段)。

    列表和详情都只给「这一轮要用的」字段;想加新维度先来这里看有什么可用。
    存的时候一个字段都没丢,所以永远不必为了看某个字段而重采。
    """
    raw = await asyncio.to_thread(store.get_raw, aweme_id)
    if raw is None:
        raise HTTPException(404, "没有完整响应(旧数据需要 refill 重采)")
    return {"aweme_id": aweme_id, "fields": len(raw), "raw": raw}


@app.get("/api/folders")
async def get_folders() -> dict[str, Any]:
    return {"items": await asyncio.to_thread(store.list_collects_folders)}


@app.get("/api/runs")
async def get_runs(limit: int = Query(10, ge=1, le=50)) -> dict[str, Any]:
    """进度以库为准,这样命令行跑的采集在界面上也看得见。"""
    run = await asyncio.to_thread(store.active_run)
    return {
        "collecting": bool(run),
        # 优先用库里的进度(可能来自命令行进程);没有再退回本进程内存
        "progress": (run or {}).get("progress") or _last_progress,
        "origin": (run or {}).get("origin"),
        "active_scope": (run or {}).get("scope"),
        "items": await asyncio.to_thread(store.latest_runs, limit),
    }


# ── 采集接口 ────────────────────────────────────────────────

def _track(info: dict) -> None:
    _last_progress.clear()
    _last_progress.update(info)


async def _guarded(coro_factory) -> None:
    """确保同一时刻只有一个采集任务。"""
    if _collect_lock.locked():
        return
    async with _collect_lock:
        try:
            await coro_factory()
        except Exception:
            # 失败原因已由 service 写入 collect_runs,这里不再抛(后台任务无处可抛)
            pass


def _require_cookie() -> None:
    config.reload()  # 同上:采集前先确认拿到的是最新 cookie
    if not settings.has_cookie:
        raise HTTPException(
            400, "DOUYIN_COOKIE 未配置。请复制 .env.example 为 .env 并填入你自己的 cookie。"
        )


@app.get("/api/collect/plan")
async def collect_plan() -> dict[str, Any]:
    """当前各分类该做什么 + 状态。给界面展示,不发任何抖音请求。"""
    import planner

    def enrich(step: dict[str, Any]) -> dict[str, Any]:
        st = store.get_state(step["scope"])
        left = planner.cooldown_left(step["scope"])
        return {
            **step,
            "exhausted": bool(st.get("exhausted")),
            "total_pages": st.get("total_pages") or 0,
            "last_status": st.get("last_status"),
            "last_error": st.get("last_error"),
            "last_run_at": st.get("last_run_at"),
            "cooldown_minutes": int(left.total_seconds() // 60) + 1 if left else 0,
        }

    steps = await asyncio.to_thread(planner.plan_all)
    return {"steps": [enrich(s) for s in steps]}


@app.post("/api/collect/totals/manual")
async def set_manual_total(scope: str, total: int | None = None) -> dict[str, Any]:
    """手填某一类的平台总数。

    收藏只能走这条路 —— 抖音资料接口不提供「我收藏了多少条」
    (127 个字段里只有 aweme_count 作品数与 favoriting_count 点赞数,
     收藏是私密的,没有计数器)。用户在 App 里看到数字后填进来即可。
    """
    import planner

    try:
        await asyncio.to_thread(planner.set_manual_total, scope, total)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"scope": scope, "total": total}


@app.post("/api/collect/totals/refresh")
async def refresh_totals() -> dict[str, Any]:
    """向抖音取一次平台侧总数(完整度的分母)。只发一个请求。

    不放在 /plan 里自动做 —— 那样每次刷页面都会打抖音一次。
    smart_collect 每轮开始时会自动刷新。
    """
    _require_cookie()
    import planner
    from collector import totals

    try:
        t = await totals.fetch()
    except Exception as e:
        raise HTTPException(502, f"取平台计数失败:{type(e).__name__}: {e}") from e
    await asyncio.to_thread(planner.save_totals, t)
    return {"totals": t}


@app.post("/api/collect/smart")
async def collect_smart(bg: BackgroundTasks) -> dict[str, Any]:
    """智能采集:每类自己判断续采/增量/跳过,自己处理限流退避。"""
    _require_cookie()
    await _require_idle()

    bg.add_task(_guarded, lambda: service.smart_collect(on_progress=_track))
    return {"started": True, "scope": "smart", "hint": "轮询 /api/runs 看进度"}


@app.post("/api/collect/refill")
async def collect_refill(
    bg: BackgroundTasks,
    scope: str | None = None,
    max_pages: int = 0,
) -> dict[str, Any]:
    """回补完整字段:重走列表,把早期只存了 31 个字段的作品补全。

    这是界面上那些「还不知道有没有内容总结」的唯一解法 ——
    它们不是真没有,而是当时没采到完整响应。

    特点(和普通采集不同):
      * 用**自己的一套游标** `refill:<scope>`,不碰续采的深挖进度;
        被 403 打断后再点一次接着往深处走,不会从最新重刷
      * 不提前停 —— 整页都是已知条目也要继续,因为目的就是更新它们
    """
    _require_cookie()
    if scope and scope not in ("collection", "like", "post"):
        raise HTTPException(400, "scope 只能是 collection / like / post")
    if scope in ("like", "post"):
        _require_own_id()
    await _require_idle()

    scopes = [scope] if scope else ["collection", "like", "post"]

    async def _all() -> None:
        for sc in scopes:
            try:
                await service.refill_scope(sc, max_pages=max_pages, on_progress=_track)
            except Exception:
                # 被 403 打断是常态(点赞实测 6 页就断)。已补的都留住了,
                # 回补游标也存住了 —— 再点一次接着走,所以这里不中断后面的类。
                continue

    bg.add_task(_guarded, _all)
    return {"started": True, "scope": "refill:" + ",".join(scopes),
            "hint": "轮询 /api/runs 看进度;403 打断是正常的,再点一次接着走"}


@app.post("/api/collect/favorites")
async def collect_favorites(
    bg: BackgroundTasks,
    max_items: int | None = None,
    fresh: bool = False,
) -> dict[str, Any]:
    _require_cookie()
    await _require_idle()

    bg.add_task(
        _guarded,
        lambda: service.collect_favorites(
            max_items=max_items, resume=not fresh, on_progress=_track
        ),
    )
    return {"started": True, "scope": "collection", "hint": "轮询 /api/runs 看进度"}


async def _require_idle() -> None:
    """跨进程互斥。命令行在采时点界面按钮必须被拒 —— 两个进程一起打抖音接口
    是风控的主要诱因。"""
    run = await asyncio.to_thread(store.active_run)
    if run:
        who = service.AlreadyCollecting.ORIGIN_LABEL.get(run.get("origin") or "", "另一个进程")
        p = run.get("progress") or {}
        at = f",已到第 {p.get('pages')} 页" if p.get("pages") else ""
        raise HTTPException(409, f"{who}正在采集「{run.get('scope')}」{at},请等它结束")


def _require_own_id() -> None:
    if not (settings.douyin_sec_user_id.strip() or settings.douyin_profile_url.strip()):
        raise HTTPException(
            400, "需要自己的 sec_user_id。先跑一次:python backend/cli.py whoami"
        )


@app.post("/api/collect/likes")
async def collect_likes(
    bg: BackgroundTasks,
    max_items: int | None = None,
    fresh: bool = False,
) -> dict[str, Any]:
    _require_cookie()
    _require_own_id()
    await _require_idle()

    bg.add_task(
        _guarded,
        lambda: service.collect_likes(
            max_items=max_items, resume=not fresh, on_progress=_track
        ),
    )
    return {"started": True, "scope": "like", "hint": "轮询 /api/runs 看进度"}


@app.post("/api/collect/posts")
async def collect_posts(
    bg: BackgroundTasks,
    max_items: int | None = None,
    fresh: bool = False,
) -> dict[str, Any]:
    _require_cookie()
    _require_own_id()
    await _require_idle()

    bg.add_task(
        _guarded,
        lambda: service.collect_posts(
            max_items=max_items, resume=not fresh, on_progress=_track
        ),
    )
    return {"started": True, "scope": "post", "hint": "轮询 /api/runs 看进度"}


@app.post("/api/collect/folders/sync")
async def sync_folders() -> dict[str, Any]:
    _require_cookie()
    await _require_idle()
    async with _collect_lock:
        return {"items": await service.sync_folders()}


@app.post("/api/collect/folder/{collects_id}")
async def collect_folder(
    collects_id: str,
    bg: BackgroundTasks,
    max_items: int | None = None,
    fresh: bool = False,
) -> dict[str, Any]:
    _require_cookie()
    await _require_idle()

    bg.add_task(
        _guarded,
        lambda: service.collect_folder(
            collects_id, max_items=max_items, resume=not fresh, on_progress=_track
        ),
    )
    return {"started": True, "scope": f"collects:{collects_id}"}


# ── 前端(零构建静态页,必须挂在所有 /api 路由之后)──────────
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
