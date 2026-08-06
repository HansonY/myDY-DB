"""抖音采集层:封装 f2,把「收藏 / 点赞 / 收藏夹」拉成统一结构。

要点:
  * 收藏接口只靠 cookie,不需要 sec_user_id;点赞接口需要自己的 sec_user_id。
  * 文本取 `desc_raw` 而非 `desc` —— 后者被 f2 做过文件名安全替换(replaceT),
    会丢标点和特殊字符。做知识库要原文。
  * 按页 yield,调用方逐页落库 + 存游标,天然支持断点续跑。
  * f2 的 kwargs["timeout"] 被 HTTP 超时与翻页休眠共用(f2 的设计),
    所以它同时也是限速旋钮。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from f2.apps.douyin.handler import DouyinHandler
from f2.apps.douyin.utils import ClientConfManager, SecUserIdFetcher

from config import settings

# 一页里我们关心的字段 → 库表列名
_FIELD_MAP = {
    "aweme_id": "aweme_id",
    "aweme_type": "aweme_type",
    "sec_user_id": "sec_user_id",
    "uid": "uid",
    "create_time": "create_time",
    "video_duration": "video_duration",
    "cover": "cover",
    "is_prohibited": "is_prohibited",
    "author_deleted": "author_deleted",
}


def build_kwargs() -> dict[str, Any]:
    """构造 f2 handler 需要的最小 kwargs。

    headers 沿用 f2 自带的默认值(含 User-Agent,ABogus 签名依赖它),
    只把 cookie 换成我们自己的。
    """
    if not settings.has_cookie:
        raise RuntimeError(
            "DOUYIN_COOKIE 未配置。请复制 .env.example 为 .env 并填入你自己的 cookie。"
        )

    conf = ClientConfManager.client()
    headers = dict(conf.get("headers") or {})

    return {
        "cookie": settings.douyin_cookie.strip(),
        "headers": headers,
        # 不走系统代理:国内接口经代理常被掐断
        "proxies": {"http://": None, "https://": None},
        # 同时是 HTTP 超时与翻页间隔(f2 的设计)
        "timeout": settings.collect_page_delay,
        "max_retries": 3,
        "max_connections": 5,
        "max_tasks": 5,
    }


def _make_handler() -> DouyinHandler:
    """建 handler,并关掉 f2 自带的 Bark 推送。

    f2 每次采集结束会往 https://api.day.app/ 发一条通知(它自带的 conf.yaml
    默认开着)。对本项目是纯负担:多一次不必要的外部请求 + 失败时刷一屏 ERROR。
    不去改 f2 的配置文件 —— 升级依赖就没了,改实例属性才稳。
    """
    handler = DouyinHandler(build_kwargs())
    handler.enable_bark = False
    return handler


def _pick(item: dict[str, Any], *names: str) -> Any:
    """按优先级取第一个非空字段(用于 desc_raw → desc 这类回退)。"""
    for n in names:
        v = item.get(n)
        if v not in (None, "", []):
            return v
    return None


def _fix_time(v: Any) -> Any:
    """f2 把时间格式成 `2026-07-17 10-47-29` —— 时间部分的冒号被换成横线
    (为了能安全用作文件名)。入库需要可解析、可排序的时间,把它还原回来。
    """
    if not isinstance(v, str) or " " not in v:
        return v
    date, _, clock = v.partition(" ")
    return f"{date} {clock.replace('-', ':')}"


def _dig(o: Any, path: str) -> Any:
    """按 a.b.c 取值,遇中间层是 list 时取第 0 项。任何一步缺失返回 None。"""
    for k in path.split("."):
        if isinstance(o, list):
            o = o[0] if o else None
        if not isinstance(o, dict):
            return None
        o = o.get(k)
    return o


def _first_url(o: Any, path: str) -> str | None:
    """取 url_list 这类地址数组的第一个。

    直接用 _dig 会把整个 list 返回,而 SQLite 绑不了 list ——
    实测报 `type 'list' is not supported`,整页落库全失败。
    """
    v = _dig(o, path)
    if isinstance(v, list):
        v = next((x for x in v if isinstance(x, str) and x), None)
    return v if isinstance(v, str) and v else None


def _from_raw(aw: dict[str, Any]) -> dict[str, Any]:
    """从**真实响应**里提取 f2 没暴露的高价值字段。

    f2 的 _to_list() 只给 31 个字段,而抖音一条作品有 787 个 —— 互动数据、
    视频地址、结构化话题、尺寸全在被丢掉的那 750+ 里。
    这些只能在采集当时拿到(媒体地址还带 x-expires 会过期),
    所以必须当场提取,不能指望以后回补。

    这里只提「这一轮要用的」。没提的字段并没有丢 —— 完整响应压缩存在
    videos.raw_z,以后想用哪个直接 store.get_raw() 解析再补列,不必重采。
    """
    st = aw.get("statistics") or {}
    status = aw.get("status") or {}

    return {
        "digg_count": st.get("digg_count"),
        "comment_count": st.get("comment_count"),
        "share_count": st.get("share_count"),
        "collect_count": st.get("collect_count"),
        "video_width": _dig(aw, "video.width"),
        "video_height": _dig(aw, "video.height"),
        "play_url": _first_url(aw, "video.play_addr.url_list"),
        "music_url": _first_url(aw, "music.play_url.url_list"),
        "poi_name": _dig(aw, "poi_info.poi_name"),
        "mix_name": _dig(aw, "mix_info.mix_name"),
        "is_subtitled": 1 if aw.get("is_subtitled") else 0,
        "is_deleted": 1 if status.get("is_delete") else 0,
    }


def _content_from_raw(aw: dict[str, Any]) -> dict[str, Any]:
    """抖音自己给的**视频内容**相关文本,以及官方分类。

    这是拿「视频讲了什么」最划算的来源 —— 平台已经算好了,直接在响应里给,
    不用发评论 @AI(那要批量写操作,是封号主因)、不用下视频做 ASR、
    也不用调视觉模型。

    字段位置和覆盖率是把 raw 里 228 个文本路径穷举一遍测出来的:

      summary   chapter_abstract(两处并集)   22%  整段内容总结
      chapters  chapter_list[].detail(两处)  23%  逐章说明,带时间戳
      queries   suggest_words[].word          62%  「大家都在搜」,真人写的查询语句
      title     item_title                    44%  干净标题,不含话题标签
      cat1/2/3  video_tag                    ~98%  官方三级分类
      cat_conf  related_video_extra.tags.prob      一级分类的置信度

    ⚠️ 位置陷阱,实测出来的:`chapter_abstract` 在顶层和 `recommend_chapter_info`
    里各有一个字段,但它们**基本互斥** —— 抖音对不同作品返回不同结构:

        只在顶层 256 · 只在 recommend 298 · 两处都有 1 · 并集 555

    所以必须两处都读。早期只读了嵌套那份,白丢了 256 条(覆盖 12% vs 22%)。
    `chapter_list` / `recommend_chapter_list` 同理。
    """
    ci = aw.get("recommend_chapter_info") or {}
    # 两处都要看:它们基本互斥,只读一处会漏掉另一批。
    summary = (aw.get("chapter_abstract") or ci.get("chapter_abstract") or "").strip()

    raw_chs = aw.get("chapter_list") or ci.get("recommend_chapter_list") or []
    chapters = [
        {
            # timestamp 是**毫秒**(实测:3384000 对应 56 分,该视频总长 3424370ms)。
            # 当成秒会把 56 分算成 940 小时,前端显示 LIVE。
            "t": ch.get("timestamp"),
            "desc": ch.get("desc"),
            "detail": ch.get("detail") or "",
            "points": [p.get("desc") for p in (ch.get("points") or [])
                       if isinstance(p, dict) and p.get("desc")],
        }
        for ch in raw_chs
        if isinstance(ch, dict) and ch.get("desc")
    ]

    # 「大家都在搜」。这是**真人写的查询语句**,等于平台白送的 query→文档配对 ——
    # 对检索召回极有价值,尤其能补向量对型号/专名召回差的短板。
    queries: list[str] = []
    groups = _dig(aw, "suggest_words.suggest_words")
    for g in groups if isinstance(groups, list) else []:
        if not isinstance(g, dict):
            continue
        for w in g.get("words") or []:
            if isinstance(w, dict) and (word := (w.get("word") or "").strip()):
                queries.append(word)

    cats = [t.get("tag_name") for t in (aw.get("video_tag") or [])
            if isinstance(t, dict) and t.get("tag_name")]
    # 另一处分类**带置信度**,可用来过滤误分类(实测 level1 常在 0.9+)
    conf = _dig(aw, "related_video_extra.tags")
    if isinstance(conf, str):
        try:
            conf = json.loads(conf)
        except ValueError:
            conf = None
    cat_conf = None
    if isinstance(conf, dict):
        lv1 = conf.get("level1")
        if isinstance(lv1, dict) and isinstance(lv1.get("prob"), (int, float)):
            cat_conf = float(lv1["prob"])

    return {
        "summary": summary,
        "chapters": chapters,
        "queries": list(dict.fromkeys(queries)),      # 去重保序
        "item_title": (aw.get("item_title") or "").strip() or None,
        "cat1": cats[0] if len(cats) > 0 else None,
        "cat2": cats[1] if len(cats) > 1 else None,
        "cat3": cats[2] if len(cats) > 2 else None,
        "cat_conf": cat_conf,
    }


def _hashtags_from_raw(aw: dict[str, Any]) -> list[str]:
    """结构化话题,比从文案里正则抠 #标签 准确(不会把标点粘进来)。"""
    return [
        t["hashtag_name"]
        for t in (aw.get("text_extra") or [])
        if isinstance(t, dict) and t.get("hashtag_name")
    ]


def _normalize(
    item: dict[str, Any],
    source: str,
    collects_id: str | None = None,
    collects_name: str | None = None,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """f2 的一条 → 我们的一行。拿不到 aweme_id 的条目直接丢弃。"""
    aweme_id = item.get("aweme_id")
    if not aweme_id:
        return None

    row: dict[str, Any] = {"source": source}
    for src, dst in _FIELD_MAP.items():
        row[dst] = item.get(src)

    # 原文优先:desc/nickname 被 f2 做过文件名安全替换
    row["description"] = _pick(item, "desc_raw", "desc")
    row["nickname"] = _pick(item, "nickname_raw", "nickname")
    row["music_title"] = _pick(item, "music_title_raw", "music_title")

    row["create_time"] = _fix_time(row.get("create_time"))
    row["aweme_id"] = str(aweme_id)
    row["collects_id"] = collects_id
    row["collects_name"] = collects_name
    row["share_url"] = f"https://www.douyin.com/video/{aweme_id}"

    # 有真实响应就用它:存原文 + 提取 f2 丢掉的字段。
    # 没有(理论上不该发生)才退回 f2 的提取结果。
    if raw:
        row.update(_from_raw(raw))
        row["hashtags"] = _hashtags_from_raw(raw)      # 供落库时写 tags 表
        ai = _content_from_raw(raw)
        row["cat1"], row["cat2"], row["cat3"] = ai["cat1"], ai["cat2"], ai["cat3"]
        row["cat_conf"] = ai["cat_conf"]
        row["item_title"] = ai["item_title"]
        # 拿到了完整响应,所以「有没有内容总结」这件事是**确定**的:
        # 有就 have,没有就 none —— 不是 unknown。
        row["content_state"] = "have" if ai["summary"] else "none"
        row["has_ai_summary"] = 1 if ai["summary"] else 0
        row["_ai"] = ai                                # 供落库时写 transcripts / extractions
        row["raw_json"] = json.dumps(raw, ensure_ascii=False, default=str)
    else:
        # 只有 f2 那 31 个字段,里面压根没有 recommend_chapter_info ——
        # 所以不能说「没有总结」,只能说「不知道」。写 none 会把它错标成
        # 已确认,以后就再也不会来补采了。
        row["content_state"] = "unknown"
        row["raw_json"] = json.dumps(item, ensure_ascii=False, default=str)

    for flag in ("is_prohibited", "author_deleted"):
        row[flag] = 1 if row.get(flag) else 0

    return row


async def _end_on_empty(agen):
    """把 f2 在空页上抛的 UnboundLocalError 当成「翻到底了」。

    f2 的 filter 假设响应里一定有 aweme_list,拿到空响应就会在构造
    `nickname_raw` 这类字段时炸 `UnboundLocalError`。正常从头翻时不会遇到
    (最后一页还有内容,再下一页 has_more=false 就停了),但**从一个正好指向
    列表末尾的游标起步时,第一页就是空的** —— 回补被中断后续跑恰好会这样。
    对我们来说那就是没有更多了,不是错误。
    """
    it = agen.__aiter__()
    while True:
        try:
            page = await it.__anext__()
        except StopAsyncIteration:
            return
        except UnboundLocalError:
            return          # f2 撞上空响应 —— 到底了
        yield page


async def _iter_pages(
    agen, source: str, collects_id: str | None = None, collects_name: str | None = None
) -> AsyncIterator[tuple[list[dict[str, Any]], Any]]:
    """把 f2 的 filter 异步生成器转成 (归一化条目列表, max_cursor)。"""
    async for page in _end_on_empty(agen):
        try:
            raw_items = page._to_list() or []
        except Exception:
            raw_items = []

        # 同时拿真实响应 —— f2 的 _to_list() 只留 31 个字段,
        # 真实响应有 787 个,不取原文就永久丢掉。
        by_id: dict[str, dict[str, Any]] = {}
        try:
            for aw in (page._to_raw() or {}).get("aweme_list") or []:
                if isinstance(aw, dict) and aw.get("aweme_id"):
                    by_id[str(aw["aweme_id"])] = aw
        except Exception:
            pass

        rows = []
        for it in raw_items:
            r = _normalize(
                it, source, collects_id, collects_name,
                raw=by_id.get(str(it.get("aweme_id") or "")),
            )
            if r:
                rows.append(r)
        yield rows, getattr(page, "max_cursor", None)


async def collect_favorites(
    max_items: int | None = None, start_cursor: int = 0
) -> AsyncIterator[tuple[list[dict[str, Any]], Any]]:
    """我收藏的作品。只需 cookie。"""
    handler = _make_handler()
    agen = handler.fetch_user_collection_videos(
        max_cursor=start_cursor,
        page_counts=settings.collect_page_size,
        max_counts=max_items,
    )
    async for out in _iter_pages(agen, "collection"):
        yield out


async def collect_likes(
    sec_user_id: str, max_items: int | None = None, start_cursor: int = 0
) -> AsyncIterator[tuple[list[dict[str, Any]], Any]]:
    """我点赞的作品。需要自己的 sec_user_id,且账号点赞列表须对自己可见。"""
    handler = _make_handler()
    agen = handler.fetch_user_like_videos(
        sec_user_id=sec_user_id,
        max_cursor=start_cursor,
        page_counts=settings.collect_page_size,
        max_counts=max_items,
    )
    async for out in _iter_pages(agen, "like"):
        yield out


async def collect_posts(
    sec_user_id: str, max_items: int | None = None, start_cursor: int = 0
) -> AsyncIterator[tuple[list[dict[str, Any]], Any]]:
    """我自己发布的作品。需要自己的 sec_user_id。"""
    handler = _make_handler()
    agen = handler.fetch_user_post_videos(
        sec_user_id=sec_user_id,
        max_cursor=start_cursor,
        page_counts=settings.collect_page_size,
        max_counts=max_items,
    )
    async for out in _iter_pages(agen, "post"):
        yield out


async def list_folders() -> list[dict[str, Any]]:
    """我的收藏夹清单。只靠 cookie —— 即使填别人的 URL 也只能拿到自己的。"""
    handler = _make_handler()
    folders: list[dict[str, Any]] = []

    async for page in handler.fetch_user_collects(
        page_counts=settings.collect_page_size
    ):
        ids = page.collects_id or []
        names = page.collects_name or []
        covers = page.collects_cover or []
        for i, cid in enumerate(ids):
            folders.append(
                {
                    "collects_id": str(cid),
                    "collects_name": names[i] if i < len(names) else None,
                    "cover": covers[i] if i < len(covers) else None,
                    "total_number": None,
                }
            )
    return folders


async def collect_folder(
    collects_id: str,
    collects_name: str | None = None,
    max_items: int | None = None,
    start_cursor: int = 0,
) -> AsyncIterator[tuple[list[dict[str, Any]], Any]]:
    """某个收藏夹内的作品。"""
    handler = _make_handler()
    agen = handler.fetch_user_collects_videos(
        collects_id=collects_id,
        max_cursor=start_cursor,
        page_counts=settings.collect_page_size,
        max_counts=max_items,
    )
    async for out in _iter_pages(agen, "collects", collects_id, collects_name):
        yield out


async def resolve_sec_user_id(profile_url: str) -> str:
    """从主页 URL 解析 sec_user_id(点赞采集需要)。"""
    return await SecUserIdFetcher.get_sec_user_id(profile_url)
