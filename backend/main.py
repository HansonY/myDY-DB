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
        "collecting": _collect_lock.locked(),
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
    sort: str = "collected",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    items = await asyncio.to_thread(
        store.list_videos, q, source, limit, offset, collects_id, nickname, sort
    )
    total = await asyncio.to_thread(
        store.count_videos, q, source, collects_id, nickname
    )
    return {"total": total, "limit": limit, "offset": offset, "items": items}


@app.get("/api/authors")
async def get_authors(limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    return {"items": await asyncio.to_thread(store.top_authors, limit)}


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

    row = await asyncio.to_thread(store.get_video, safe_id)
    if not row or not row.get("cover"):
        raise HTTPException(404, "没有封面")

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as cli:
            r = await cli.get(
                row["cover"],
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


@app.get("/api/folders")
async def get_folders() -> dict[str, Any]:
    return {"items": await asyncio.to_thread(store.list_collects_folders)}


@app.get("/api/runs")
async def get_runs(limit: int = Query(10, ge=1, le=50)) -> dict[str, Any]:
    return {
        "collecting": _collect_lock.locked(),
        "progress": _last_progress,
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


@app.post("/api/collect/favorites")
async def collect_favorites(
    bg: BackgroundTasks,
    max_items: int | None = None,
    fresh: bool = False,
) -> dict[str, Any]:
    _require_cookie()
    if _collect_lock.locked():
        raise HTTPException(409, "已有采集任务在跑,请等它结束")

    bg.add_task(
        _guarded,
        lambda: service.collect_favorites(
            max_items=max_items, resume=not fresh, on_progress=_track
        ),
    )
    return {"started": True, "scope": "collection", "hint": "轮询 /api/runs 看进度"}


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
    if _collect_lock.locked():
        raise HTTPException(409, "已有采集任务在跑,请等它结束")

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
    if _collect_lock.locked():
        raise HTTPException(409, "已有采集任务在跑,请等它结束")

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
    if _collect_lock.locked():
        raise HTTPException(409, "已有采集任务在跑,请等它结束")
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
    if _collect_lock.locked():
        raise HTTPException(409, "已有采集任务在跑,请等它结束")

    bg.add_task(
        _guarded,
        lambda: service.collect_folder(
            collects_id, max_items=max_items, resume=not fresh, on_progress=_track
        ),
    )
    return {"started": True, "scope": f"collects:{collects_id}"}


# ── 前端(零构建静态页,必须挂在所有 /api 路由之后)──────────
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
