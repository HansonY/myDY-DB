"""BOSS 知识库的本地服务(:8001)。

**独立于抖音那套**:独立 db、独立端口。这里只做两件事:
  1. 接收 Chrome 插件送来的页面数据(/api/boss/ingest)
  2. 查库(状态、列表、检索)

为什么走插件而不是自己爬:
插件只读**你已经在浏览器里打开的页面**,不额外发一个请求 —— 反爬没什么可挑剔的,
也不需要任何 cookie 或登录自动化。你正常找工作,库自己就积累起来了。
"""

from __future__ import annotations

import asyncio
import json
import re
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


# 待提取队列。**存文字是瞬间的,提取才要花时间和钱** ——
# 所以分开:侧边栏点「存入」只入队(立刻返回),提取攒一批再做。
PENDING_DIR = ROOT / "data" / "boss_pending"


@app.post("/api/boss/detect")
async def detect(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """只判断「这是不是岗位页」,**不写任何东西**。侧边栏用它做预览。

    单独开一个只读接口,是为了让「判断」和「存」能分开验证 ——
    否则你没法在不污染库的前提下看它判得准不准。
    """
    import boss_detect as bd
    return bd.classify(body.get("text") or "", body.get("url") or "", body.get("title") or "")


@app.post("/api/boss/ingest_text")
async def ingest_text(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """收一个页面的纯文本,判断是不是岗位页,是就入队。**不在这里调 AI。**

    为什么不当场提取:一次 API 往返几秒,点一下要等几秒体验很差,
    而且逐页调用比多页一次贵得多。这里只落盘,提取由 /extract 批量做。

    **判断放在这里,不放在插件里** —— 只有一份实现。插件那边再写一份
    早晚会跟这边漂移,而漂移出来的 bug 最难查。

    `force=true`(手动点「存入」)跳过判断:我的判断可能错,
    不该因为我猜错就拦着人存。自动存则必须过判断,否则你随便浏览个网页
    都往库里灌东西。
    """
    import boss_detect as bd

    text = (body.get("text") or "").strip()
    url = str(body.get("url") or "")
    title = str(body.get("title") or "")
    auto = bool(body.get("auto"))
    force = bool(body.get("force"))

    verdict = bd.classify(text, url, title)
    if not force and not verdict["is_job"]:
        # 没存也要说清为什么 —— 「静默不存」会让人以为在存,
        # 这个项目已经吃过一次「看着像在工作」的亏。
        return {"queued": 0, "skipped": True, "detect": verdict, "note": verdict["why"]}

    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    # 去重键 = 公司+岗位名(取自标题整串,不解析格式),标题拿不到才退回 URL。
    # 同一个岗位反复打开 → 同一个文件 → **覆盖**,不堆积。
    key = bd.dedupe_key(title, url)
    f = PENDING_DIR / f"{key}.json"
    replaced = f.exists()
    f.write_text(json.dumps({
        "url": url, "title": title, "kind": verdict["kind"],
        "text": text, "at": datetime.now().isoformat(timespec="seconds"),
        "auto": auto, "detect_score": verdict["score"],
    }, ensure_ascii=False), encoding="utf-8")

    n = len(list(PENDING_DIR.glob("*.json")))
    return {"queued": 1, "replaced": replaced, "detect": verdict, "pending": n,
            "note": (f"已更新(同一岗位,覆盖上次)· 待提取 {n}" if replaced
                     else f"已入队 · 待提取 {n}")}


@app.post("/api/boss/extract")
async def do_extract(
    max_pages: int = Query(20, ge=1, le=60),
) -> dict[str, Any]:
    """把队列里的页面批量交给 AI 提取,写进 jobs 表。

    按字符预算打包,**一次调用处理多页**。提取成功的从队列移除;
    失败的留着 —— 下次还能再试,不会因为一次网络抖动就丢数据。
    """
    from knowledge import boss_fragments as bfr
    import boss_extract as bx

    if not PENDING_DIR.exists():
        return {"extracted": 0, "note": "队列是空的"}
    files = sorted(PENDING_DIR.glob("*.json"))[:max_pages]
    if not files:
        return {"extracted": 0, "note": "队列是空的"}

    if not bx.available():
        # 别写死某一家的键名 —— 提取是供应商无关的,写死了会把人往错方向指。
        import llm as _llm
        st = _llm.status()
        return {"extracted": 0, "pending": len(files),
                "error": f"AI 还不能用({st['label']},当前 {st['provider']})—— "
                         f"原文已留在队列里,一条都没丢。到 http://127.0.0.1:8000/data.html "
                         f"的「AI 模型」里选一家、填 key、点「测一下」,再回来点提取。"}

    pages, keep = [], []
    for f in files:
        try:
            pages.append(json.loads(f.read_text(encoding="utf-8")))
            keep.append(f)
        except ValueError:
            f.unlink(missing_ok=True)

    saved = skipped = calls = 0
    failed: list[str] = []
    for batch in bx.pack(pages):
        base = pages.index(batch[0])
        try:
            got = await asyncio.to_thread(bx.extract_batch, batch)
            calls += 1
        except bx.NoKey as e:
            return {"extracted": saved, "error": str(e)}
        except Exception as e:      # noqa: BLE001 —— 一批失败不该拖垮其它批
            failed.append(f"{type(e).__name__}: {str(e)[:90]}")
            continue

        for item in got:
            page = batch[item["idx"]]
            for j in item["jobs"]:
                jid = _job_key(j, page.get("url"))
                exists = bs.get_job(jid)
                # jd 只升不降:列表页提出来的没有 jd,不能把详情页存的冲掉
                bs.upsert_jobs([{
                    "job_id": jid, "url": page.get("url"),
                    "title": j.get("title"), "company": j.get("company"),
                    "city": j.get("city"), "district": j.get("district"),
                    "salary_text": j.get("salary_text"),
                    "salary_min": j.get("salary_min"), "salary_max": j.get("salary_max"),
                    "salary_months": j.get("salary_months"),
                    "experience": j.get("experience"), "degree": j.get("degree"),
                    "jd": j.get("jd"),
                    "jd_state": "have" if (j.get("jd") or "").strip() else "unknown",
                    "tags": j.get("tags") or [],
                    "hr_name": j.get("hr_name"), "hr_title": j.get("hr_title"),
                    "raw": {"from_page": page.get("url"), "extracted": j},
                }])
                if j.get("my_status"):
                    bs.save_interaction(jid, "viewed", note=str(j["my_status"])[:60])
                # 片段:JD 全文是核心,标题/公司/薪资兜底
                job = bs.get_job(jid) or {}
                bs.save_fragments(jid, bfr.build(job))
                skipped += 1 if exists else 0
                saved += 0 if exists else 1
        # 这一批处理完了,从队列移除
        for p_ in batch:
            idx = pages.index(p_)
            keep[idx].unlink(missing_ok=True)

    st = bs.stats()
    return {"extracted": saved, "updated": skipped, "ai_calls": calls,
            "pending": len(list(PENDING_DIR.glob("*.json"))),
            "jobs_total": st["jobs"], "failed": failed}


def _norm(s: str) -> str:
    """归一化后再做键:空格和全半角差异不该产生重复行。"""
    return re.sub(r"\s+", "", s).lower()


def _job_key(j: dict[str, Any], url: str | None) -> str:
    """岗位 id。

    没有平台 id 可用(我们走的是页面文字这条路),所以用
    公司+标题+薪资 做稳定指纹 —— 同一个岗位反复浏览要落到同一行,
    否则库里会堆一堆重复。
    """
    import hashlib
    # **公司 + 岗位名**,不含薪资。带上薪资是错的 —— 同一个岗位改了薪资
    # 就会变成两行,而它明明是同一个岗位(用户要的就是「覆盖」)。
    seed = "|".join(_norm(str(j.get(k) or "")) for k in ("company", "title"))
    if not seed.strip("|"):
        seed = str(url or "")
    return "t_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


@app.get("/api/boss/jobs")
async def list_jobs(limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    """已入库的岗位。侧边栏和状态页都用它。"""
    with bs.connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT job_id, title, company, city, salary_text, jd_state, updated_at "
            "FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,))]
        total = c.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
    pend = len(list(PENDING_DIR.glob("*.json"))) if PENDING_DIR.exists() else 0
    return {"items": rows, "total": total, "pending": pend}


@app.get("/api/boss/recent")
async def recent(limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    """最近存了什么 —— 带时间、条数、样例。

    「什么时候存的、存了什么」必须一眼能看到。只给一个总数的话,
    分不出存的是真数据还是噪音 —— 前面就这么被骗过一次
    (5 条捕获全是性能监控和登录心跳,但界面显示「已送入库 5」)。
    """
    if not CAPTURE_DIR.exists():
        return {"items": [], "note": "还没有任何捕获"}

    def count_and_sample(node, depth=0):
        """找最长的对象数组当条数,顺手挖一个标题当样例。不认死字段名。"""
        best, sample = 0, ""
        if depth > 6 or not isinstance(node, (dict, list)):
            return best, sample
        if isinstance(node, list):
            if node and isinstance(node[0], dict):
                best = len(node)
            for x in node[:3]:
                b, sm = count_and_sample(x, depth + 1)
                best = max(best, b)
                sample = sample or sm
            return best, sample
        for k in ("jobName", "jobTitle", "positionName", "title", "brandName"):
            if isinstance(node.get(k), str) and node[k].strip():
                sample = node[k][:26]
                break
        for v in node.values():
            b, sm = count_and_sample(v, depth + 1)
            best = max(best, b)
            sample = sample or sm
        return best, sample

    out = []
    for f in sorted(CAPTURE_DIR.glob("*.json"), reverse=True):
        try:
            recs = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for r in (recs if isinstance(recs, list) else [recs]):
            n, sample = count_and_sample(r.get("body"))
            out.append({
                "at": r.get("at"),
                "url": str(r.get("url", "")).replace("https://www.zhipin.com", ""),
                "records": n, "sample": sample,
                "file": f.name,
            })
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    return {"items": out, "total_files": len(list(CAPTURE_DIR.glob("*.json")))}


@app.get("/api/boss/pending")
async def pending(limit: int = Query(60, ge=1, le=300)) -> dict[str, Any]:
    """待提取队列里都有什么。

    队列必须**看得见**。只显示一个数字的话,分不出排着的是真岗位还是噪音 ——
    这个项目栽过一次:界面显示「已送入库 5」,实际 5 条全是性能监控。
    """
    if not PENDING_DIR.exists():
        return {"items": [], "total": 0}
    files = sorted(PENDING_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    out = []
    for f in files[:limit]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        out.append({"title": d.get("title"), "kind": d.get("kind"),
                    "chars": len(d.get("text") or ""), "at": d.get("at"),
                    "url": d.get("url"), "score": d.get("detect_score")})
    return {"items": out, "total": len(files)}


@app.post("/api/boss/known")
async def known(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """这批链接里,哪些已经存过了。

    从列表页抓来几十个链接,里面多半有一半是你之前看过的 ——
    不筛掉就要白开几十个标签页。这一步纯本地查库,不碰网络。

    按**路径**比对,不带查询串:BOSS 的 job_detail 链接后面挂着
    securityId / lid 这类每次都不同的参数,带上比对等于永远不重复。
    """
    urls = [str(u) for u in (body.get("urls") or []) if u]
    if not urls:
        return {"known": [], "fresh": [], "total": 0}

    def path_of(u: str) -> str:
        return str(u).split("?")[0].split("#")[0].rstrip("/")

    seen: set[str] = set()
    with bs.connect() as c:
        for r in c.execute("SELECT url FROM jobs WHERE url IS NOT NULL AND url != ''"):
            seen.add(path_of(r["url"]))
    # 队列里排着的也算已知 —— 免得重复排队
    if PENDING_DIR.exists():
        for f in PENDING_DIR.glob("*.json"):
            try:
                seen.add(path_of(json.loads(f.read_text(encoding="utf-8")).get("url") or ""))
            except ValueError:
                pass
    seen.discard("")

    known_l = [u for u in urls if path_of(u) in seen]
    fresh = [u for u in urls if path_of(u) not in seen]
    return {"known": known_l, "fresh": fresh, "total": len(urls)}


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
    s["db"] = str(bs.db_file())
    s["pending"] = len(list(PENDING_DIR.glob("*.json"))) if PENDING_DIR.exists() else 0
    # AI 三态。**别只报「key 填没填」** —— 填了也可能是 402 用不了,
    # 把「填了」显示成「可用」是假承诺。
    import llm as _llm
    s["ai"] = _llm.status()
    return s


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
