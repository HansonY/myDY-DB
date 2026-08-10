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
from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response
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


# 关掉静态资源缓存。本地单人自部署,没有带宽压力,而浏览器缓存 shell.js / *.html
# 会让「明明改了却看起来没变」—— 实测就因此反复误判成「UI 没改」。
# 正确性在这里远比缓存命中重要,一律 no-store。
@app.middleware("http")
async def _no_cache_static(request, call_next):
    resp = await call_next(request)
    p = request.url.path
    if p.endswith((".html", ".css", ".js")) or p == "/":
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


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
    scope: str = Query("mine", pattern="^(mine|following|all)$"),
) -> dict[str, Any]:
    """语义检索:换句话说也找得到。

    和 `/api/videos?q=` **刻意分成两个接口**。那一路是 LIKE 子串匹配
    (「MCP」「Claude Code」字面出现就命中),这一路是向量语义
    (「怎么练口语」也能找到)。合并之后「这条是怎么被找到的」就说不清了,
    出问题没法定位是哪一路的锅。

    分数一律带出来,三档:good(≥阈值)· maybe(可能相关)· 全没过就是库里没有。

    `scope` 默认 **mine**(只搜我主动选的)。关注者的全量产出比我的收藏多一个
    数量级,不设默认就等于把「我的知识库」悄悄换成「他们的内容农场」。
    """
    from knowledge import search as ks, vecdb
    try:
        return await asyncio.to_thread(ks.search, q, limit, include_maybe, scope)
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


@app.get("/api/insight/themes")
async def insight_themes(
    min_n: int = Query(8, ge=2, le=100),
    max_tags: int = Query(120, ge=20, le=400),
    threshold: float = Query(0.65, ge=0.4, le=0.9),
) -> dict[str, Any]:
    """兴趣主题:把碎标签语义聚类成主题(认识自己 第 01 段)。

    比抖音官方一级分类有信息量得多 —— 那套排前几的是「个人管理」「随拍」,
    看完不知道你在看什么;这里给的是「英语口语 307 条 / AI 197 条」。
    """
    from knowledge import insight as ki
    return await asyncio.to_thread(ki.tag_themes, min_n, max_tags, threshold)


@app.get("/api/insight/aspects")
async def insight_aspects() -> dict[str, Any]:
    """我关注的人都是做什么的 + 「关注了却没在看」的缺口(认识自己 第 02 段)。"""
    from knowledge import insight as ki
    return await asyncio.to_thread(ki.following_aspects)


@app.get("/api/insight/quality")
async def insight_quality(
    min_n: int = Query(15, ge=3, le=100),
    limit: int = Query(16, ge=3, le=60),
) -> dict[str, Any]:
    """每个标签的干货率 + 传播量 —— 这一层里唯一能直接行动的分析。"""
    from knowledge import insight as ki
    return await asyncio.to_thread(ki.tag_quality, min_n, limit)


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
        # trust_env=False 绕开系统代理。抖音 CDN 走本机代理(实测这台机器有
        # HTTP_PROXY=127.0.0.1:6152)必然 `SSL: UNEXPECTED_EOF_WHILE_READING` ——
        # 流量被绕出国再回来。同一时刻直连是好的,实测 1.6MB 音轨 1.7 秒下完。
        # 症状是缩略图整片裂掉,而日志里只有一堆 httpx 堆栈,看不出根因在代理。
        async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                     trust_env=False) as cli:
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


@app.get("/api/following")
async def get_following(only_tagged: bool = False) -> dict[str, Any]:
    """我关注的人 + 每位「我实际存过他几条」。

    `saved_n` 是决定要不要深挖的唯一实证信号,所以它必须和列表一起给出去 ——
    否则页面上只能看到「他发了 43451 条」,看不到「我一条都没存过」。
    """
    items = await asyncio.to_thread(store.list_following, only_tagged)
    return {
        "items": items,
        "total": len(items),
        # 把「全爬要多久」摆在脸上:这是不做全量深挖的理由
        "sum_aweme": sum(u["aweme_count"] or 0 for u in items),
        "picked": sum(1 for u in items if u["crawl"]),
        "picked_aweme": sum(u["aweme_count"] or 0 for u in items if u["crawl"]),
        "never_saved": sum(1 for u in items if not u["saved_n"]),
    }


@app.post("/api/following/sync")
async def post_following_sync() -> dict[str, Any]:
    """刷新关注列表(不采作品)。97 位约 5 页,很轻。"""
    try:
        return await service.sync_following()
    except service.AlreadyCollecting as e:
        raise HTTPException(409, str(e))


@app.post("/api/following/role")
async def post_following_role(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """给博主打标:info=信息价值 / rival=竞品 / null=取消。

    **role 是唯一的开关** —— 打上标就等于「每天跟他」。曾经还有个独立的
    crawl 开关,结果两者各走各的(实测 8 位 crawl=1 却没打标),已经删掉。
    """
    ids = body.get("sec_user_ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(422, "要给 sec_user_ids(数组)")
    try:
        n = await asyncio.to_thread(store.set_following_role, ids, body.get("role"))
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return {"updated": n, "role": body.get("role")}


# ── 设置 ────────────────────────────────────────────────────
#
# 密钥写在 .env,这里给个页面入口,免得每次都要手改文件。
# 三条铁律:
#   1. **绝不回传明文** —— 只回「配没配」和尾四位,页面上永远看不到完整密钥
#   2. 只动这几个白名单键,不让页面变成任意写文件的口子
#   3. 写回时保留 .env 里其它行和注释,不整个重写

_EDITABLE = {
    # LLM 供应商可换 —— 都是 OpenAI 兼容端点,差别只有这三项。
    # 换完记得跑 ./boss.sh llmtest 验证一下,各家模型名改得挺勤。
    "LLM_PROVIDER": "用哪家模型:qwen / minimax / deepseek / moonshot / zhipu / ollama",
    "LLM_API_KEY": "上面那家的 API Key(留空则回退去找该家专用键,如 DASHSCOPE_API_KEY)",
    "LLM_MODEL": "覆盖默认模型名(留空用默认;各家改版勤,不通就来这里改)",
    # 每家一把,互不干扰 —— 这样在页面上来回切供应商不用重新填 key。
    # (页面保存时会写到这里面对应的那一把,而不是写全局 LLM_API_KEY。)
    "DASHSCOPE_API_KEY": "通义千问 API Key",
    "MINIMAX_API_KEY": "MiniMax API Key",
    "DEEPSEEK_API_KEY": "DeepSeek API Key",
    "MOONSHOT_API_KEY": "月之暗面 API Key",
    "ZHIPU_API_KEY": "智谱 API Key",
    "DOUYIN_COOKIE": "抖音 cookie(采集必需)。⚠️ 等同账号控制权,只存本机",
    "DOUYIN_SEC_USER_ID": "我自己的 sec_user_id(采点赞/我的作品要)",
    "ASR_MODEL": "语音转写模型(默认 large-v3-turbo)",
    "EMBED_MODEL": "嵌入模型(默认 BAAI/bge-m3,中英互通)",
}
_SECRET_KEYS = {"DOUYIN_COOKIE", "LLM_API_KEY"} | {
    k for k in _EDITABLE if k.endswith("_API_KEY")}


def _mask(v: str) -> str:
    v = (v or "").strip()
    if not v:
        return ""
    return f"…{v[-4:]}" if len(v) > 8 else "已设置"


@app.get("/api/llm/providers")
async def llm_providers() -> dict[str, Any]:
    """能选哪些供应商 —— 给设置页做下拉。

    都是 OpenAI 兼容端点,换家只改配置。默认端点/模型可能过时(各家改版勤),
    所以页面上留了覆盖入口,并配一个「测一下」按钮。
    """
    import llm as _llm
    cur = _llm.config()
    return {
        "providers": [
            {"id": k, "base_url": v[0], "model": v[1],
             "label": {
                 "qwen": "通义千问(阿里)", "minimax": "MiniMax",
                 "deepseek": "DeepSeek", "moonshot": "月之暗面 Kimi",
                 "zhipu": "智谱 GLM(有免费的 glm-4-flash)",
                 "ollama": "本地 Ollama(不用 Key)",
             }.get(k, k),
             "key_env": _llm._KEY_FALLBACK.get(k, ""),
             "needs_key": k != "ollama"}
            for k, v in _llm._PROVIDERS.items()
        ],
        "current": {"provider": cur["provider"], "model": cur["model"],
                    "base_url": cur["base_url"], "key_set": bool(cur["_key"])},
    }


@app.post("/api/llm/test")
async def llm_test() -> dict[str, Any]:
    """测当前 LLM 配置通不通。**报服务端原话** ——
    「余额不足」和「key 无效」是完全不同的两件事,不能糊成一句「失败」。"""
    import llm as _llm
    return await asyncio.to_thread(_llm.probe)


@app.post("/api/llm/models")
async def llm_models() -> dict[str, Any]:
    """问供应商它自己有哪些模型 —— 不猜模型名。

    实测有用:MiniMax 的 /v1/models 返回 200 就直接证明了 key 认证正常,
    把「key 无效」这个可能性一次排除掉。
    """
    import llm as _llm
    return await asyncio.to_thread(_llm.list_models)


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    """当前配置。**密钥只回尾四位**,不回明文。"""
    config.reload()
    import llm as _llm
    lc = _llm.config()
    cur = {
        "LLM_PROVIDER": lc["provider"],
        "LLM_API_KEY": lc["_key"],
        "LLM_MODEL": lc["model"],
        "DASHSCOPE_API_KEY": settings.dashscope_api_key,
        "DOUYIN_COOKIE": settings.douyin_cookie,
        "DOUYIN_SEC_USER_ID": settings.douyin_sec_user_id,
        "ASR_MODEL": settings.asr_model,
        "EMBED_MODEL": settings.embed_model,
    }
    items = []
    for k, desc in _EDITABLE.items():
        v = (cur.get(k) or "").strip()
        secret = k in _SECRET_KEYS
        items.append({
            "key": k, "desc": desc, "secret": secret,
            "set": bool(v),
            "value": "" if secret else v,        # 非密钥的才回真值
            "hint": _mask(v) if secret else "",
        })
    return {
        "items": items,
        "env_path": str(ROOT / ".env"),
        # 各功能当前能不能用 —— 比单看「key 填没填」有用
        "features": {
            "collect": bool(settings.has_cookie),
            # 三态,不是布尔 —— 「填了 key」和「能用」是两件事(MiniMax 实测:
            # key 有效、9 个模型全 402)。界面据此显示,不再把「填了」说成「可用」。
            "ask": _llm.status(),
            "search": True,        # 本地向量,不需要任何 key
            "asr": True,           # 本地 whisper,不需要任何 key
        },
    }


@app.post("/api/settings")
async def post_settings(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """写回 .env。只认白名单键,保留文件里其它行和注释。"""
    body = dict(body or {})

    # 页面上的「API Key」是**跟着当前选的供应商**的,所以要写到那家自己的键上,
    # 而不是写全局 LLM_API_KEY。否则切一次供应商就把 key 带过去了 ——
    # 实测切到智谱还在发 MiniMax 的 key,对方回「令牌已过期」,极其误导人。
    if body.get("LLM_API_KEY"):
        import llm as _llm
        prov = str(body.get("LLM_PROVIDER") or _llm.config()["provider"]).lower()
        target = _llm._KEY_FALLBACK.get(prov)
        if target and target in _EDITABLE:
            body[target] = body.pop("LLM_API_KEY")

    updates = {k: str(v) for k, v in body.items()
               if k in _EDITABLE and v is not None}
    if not updates:
        raise HTTPException(422, f"没有可写的键。可写:{sorted(_EDITABLE)}")

    path = ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen = set()
    out = []
    for ln in lines:
        m = ln.split("=", 1)
        k = m[0].strip()
        if k in updates:
            # 空字符串 = 清空这一项,但保留键,免得下次看不到它存在过
            out.append(f"{k}={updates[k]}")
            seen.add(k)
        else:
            out.append(ln)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)      # 里面有 cookie 和密钥,别让同机其它用户读到
    except OSError:
        pass
    config.reload()
    return {"saved": sorted(updates), "env_path": str(path)}


@app.get("/api/creators/pending")
async def get_creators_pending(
    days: int = Query(3, ge=1, le=90),
    role: str | None = Query(None, pattern="^(info|rival)$"),
) -> dict[str, Any]:
    """待抓清单:这个窗口里还没抓过的博主。

    每人一条水位线(`fetched_at`),早于窗口起点就说明有缺口。
    `since` 是这次实际会从哪个时间点开始抓 —— 点之前就看得到,不做黑盒。
    """
    items = await asyncio.to_thread(store.creators_pending, days, role)
    stale = [x for x in items if x["stale"]]
    return {
        "items": items, "total": len(items),
        "stale": len(stale),
        "stale_ids": [x["sec_user_id"] for x in stale],
        "days": days, "role": role,
    }


# ── 每日简报 ────────────────────────────────────────────────

@app.get("/api/digest")
async def get_digest(days: int = Query(3, ge=1, le=90)) -> dict[str, Any]:
    """**已打标博主**最近 N 天的简报。纯本地查询,不碰抖音接口。

    没打标的人不会出现在这里 —— 关注列表有 97 位,但只跟你选的那几位。
    """
    from knowledge import digest as kd
    ov, info, rival = await asyncio.gather(
        asyncio.to_thread(kd.overview, days),
        asyncio.to_thread(kd.info_digest, days),
        asyncio.to_thread(kd.rival_report, days),
    )
    return {"overview": ov, "info": info, "rival": rival}


@app.post("/api/digest/refresh")
async def post_digest_refresh(
    days: int = Query(3, ge=1, le=30),
    role: str | None = Query(None, pattern="^(info|rival)$"),
) -> dict[str, Any]:
    """抓一轮:**只抓已打标的博主**最近 N 天的新作品。

    实测每人 1 页就够(他们每天发 0.1–0.5 条),零 403 风险。
    没打标的人一次请求都不会发 —— 有单元测试盯着这条。
    """
    try:
        return await service.daily_round(days=days, role=role,
                                         on_creator=lambda x: _track(x))
    except service.AlreadyCollecting as e:
        raise HTTPException(409, str(e))
    except RuntimeError as e:      # f2 加载不了(网络)
        raise HTTPException(503, str(e)) from e


# ── 语音转写 ────────────────────────────────────────────────

@app.get("/api/asr/pending")
async def get_asr_pending(
    limit: int = Query(50, ge=1, le=500),
    scope: str = Query("following", pattern="^(mine|following|all)$"),
) -> dict[str, Any]:
    """还没有任何真实内容的作品 —— 没抖音总结、也没转过。"""
    from knowledge import asr as ka
    items = await asyncio.to_thread(ka.pending, limit, scope)
    secs = sum((i.get("video_duration") or 0) / 1000 for i in items)
    return {
        "items": items, "total": len(items),
        "audio_seconds": round(secs),
        # 实测 large-v3-turbo 在这台机器上约 3× 实时
        "estimate_minutes": round(secs / 60 / 3.0, 1),
        "model": settings.asr_model,
    }


@app.post("/api/asr/run")
async def post_asr_run(
    limit: int = Query(10, ge=1, le=200),
    scope: str = Query("following", pattern="^(mine|following|all)$"),
) -> dict[str, Any]:
    """批量转写。**首次会下模型(1.6GB)**,先跑 scripts/fetch_asr_model.py 预热。"""
    from knowledge import asr as ka
    try:
        return await asyncio.to_thread(ka.run_batch, limit, scope, None, None)
    except RuntimeError as e:      # 没装依赖 / 模型下不下来
        raise HTTPException(501, str(e)) from e


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
# 老地址重定向。这个项目是自部署的,书签会失效 —— 三行代码的事,不要让人撞 404。
#   /daily.html      → /            简报成了首页
#   /following.html  → /creators.html
_MOVED = {"/daily.html": "/", "/following.html": "/creators.html"}


@app.get("/{old_path:path}.html", include_in_schema=False)
async def _moved(old_path: str):
    """只处理搬走的那几个;其余交给下面的 StaticFiles。"""
    key = f"/{old_path}.html"
    if key in _MOVED:
        return RedirectResponse(_MOVED[key], status_code=308)
    path = STATIC_DIR / f"{old_path}.html"
    if path.is_file():
        return FileResponse(path, media_type="text/html")
    raise HTTPException(404, "没有这个页面")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
