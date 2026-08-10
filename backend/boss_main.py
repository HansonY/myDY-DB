"""BOSS 知识库的本地服务(:8001)。

**独立于抖音那套**:独立 db、独立端口。这里只做两件事:
  1. 接收 Chrome 插件送来的页面数据(/api/boss/ingest)
  2. 查库(状态、列表、检索)

为什么走插件而不是自己爬:
插件只读**你已经在浏览器里打开的页面**,不额外发一个请求 —— 反爬没什么可挑剔的,
也不需要任何 cookie 或登录自动化。你正常找工作,库自己就积累起来了。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _pyversion
_pyversion.check()

from fastapi import Body, FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import ROOT
from db import boss_store as bs

STATIC_DIR = Path(__file__).resolve().parent / "static_boss"
# 插件送来的原始数据先落这儿。**先如实存,再谈解析** ——
# 我还不知道 BOSS 的字段长什么样,凭记忆写解析必错(这个项目已经吃过几次亏)。
CAPTURE_DIR = ROOT / "data" / "boss_capture"

app = FastAPI(title="Douyin-DB · BOSS", version="0.1.0")

# 插件的 content script 跑在 zhipin.com 源上,要往 localhost 发 —— 必须放行。
# 只放行这一个用途,不是全站开放。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.zhipin.com", "https://zhipin.com",
                   "http://localhost:8001", "http://127.0.0.1:8001"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    bs.init_db()
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/api/boss/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "db": str(bs.db_file())}


@app.post("/api/boss/ingest")
async def ingest(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """接收插件送来的一批捕获。

    body = {"items": [{"url":…, "kind":…, "body":…}, …]}

    **现在只落盘,不解析。** 解析要照着真实响应写 —— 等看到实际字段再说。
    这样即使解析还没写好,数据也不会白采:你浏览过的东西已经存下来了。
    """
    items = body.get("items") or []
    if not isinstance(items, list) or not items:
        return {"saved": 0, "note": "没有 items"}

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    f = CAPTURE_DIR / f"ext_{ts}.json"
    f.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")

    # 顺手统计一下抓到哪些接口,让插件那边能显示进度
    urls: dict[str, int] = {}
    for it in items:
        u = str(it.get("url", "?")).split("?")[0]
        urls[u] = urls.get(u, 0) + 1
    return {"saved": len(items), "file": f.name, "by_url": urls}


@app.get("/api/boss/stats")
async def stats() -> dict[str, Any]:
    s = bs.stats()
    caps = sorted(CAPTURE_DIR.glob("*.json")) if CAPTURE_DIR.exists() else []
    n = 0
    for c in caps[-50:]:
        try:
            d = json.loads(c.read_text(encoding="utf-8"))
            n += len(d) if isinstance(d, list) else 1
        except ValueError:
            pass
    s["capture_files"] = len(caps)
    s["capture_records_recent"] = n
    return s


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
