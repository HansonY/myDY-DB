"""语音转写:把视频里的话变成文字。

**为什么必须做这个。** 抖音的响应里**没有字幕文本** —— 逐个字段翻过两遍,
只有一个 `is_subtitled` 标记,既没有字幕文本也没有字幕 URL。而平台自己生成的
内容总结只覆盖 34%,剩下 66% 的作品在库里只有文案(常是营销话术)和话题标签。
那 66% 进了知识库也答不出「这条讲了什么」。

**为什么现在做得起,以前做不起。** 早先的判断是「ASR 是 130 小时存量音频的活」,
那是按「把 2555 条历史全转一遍」算的。改成「每天抓最近三天」之后,
每 3 天只有约 9 条新作品、约 11 分钟音频 —— 一两分钟就转完。
同一件事,量级差了三个数量级。

**音轨还是视频。** `music_url` 只有 417 KB,但它是「这条视频用的那首曲子」——
实测我收藏的里有 11% 用的是 BGM 而不是原声,对着 BGM 转写只会得到歌词。
所以按 `music_title` 分流:带「原声」的用音轨(省 10 倍流量),
其余一律用 `play_url`(视频本身,一定含人声)。

**和抖音总结的关系是互补,不是替代。** 总结是抽象的要点(「依恋半衰期约 4.18 年」),
逐字稿是原话和细节。两个都进片段层:问「大意」时总结更准,
问「他原话怎么说的」「有没有提到某个具体名词」时只有逐字稿能答。
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from config import settings
from db import store

# 下载媒体时要带的头。抖音 CDN 对没有 Referer 的请求会返回 403。
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://www.douyin.com/",
}

# 太短的基本是纯音乐/无人声片段,转出来只有噪声,不值得跑。
MIN_MS = 5_000

_model = None
_model_name = ""


def _load():
    """按需加载模型。首次会下载权重(large-v3-turbo 约 1.6 GB),之后走本地缓存。

    别在请求线程里第一次调用它 —— 首次下载十几分钟。用
    `scripts/fetch_asr_model.py` 预热。
    """
    global _model, _model_name
    want = getattr(settings, "asr_model", "small")
    if _model is not None and _model_name == want:
        return _model
    # 镜像在 config.py 顶部就设好了 —— 必须在 huggingface_hub 被 import
    # **之前**,它在自己 import 时就把 HF_ENDPOINT 读死进 constants.ENDPOINT。
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError(
            "没装 faster-whisper。装:\n"
            "  .venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple faster-whisper\n"
            "⚠️ 装完要把 click 钉回去:pip install 'click==8.1.7' —— "
            "它会被 huggingface-hub 顶到 8.4.2,而 f2 钉死 8.1.7,顶掉就采不了抖音了。"
        ) from e
    # int8 量化:M 系列 CPU 上比 float32 快 3–4 倍,中文识别质量几乎无差
    _model = WhisperModel(want, device="cpu", compute_type="int8",
                          cpu_threads=max(1, (os.cpu_count() or 4) - 2))
    _model_name = want
    return _model


def pick_media(video: dict[str, Any]) -> tuple[str | None, str]:
    """选转写用哪个地址。返回 (url, 理由)。

    实测 `music_url` 没有 x-expires,是稳定 CDN 地址 —— 所以存量也能补,
    不是只能配合当天的采集。
    """
    title = (video.get("music_title") or "")
    if "原声" in title and video.get("music_url"):
        return video["music_url"], "原声音轨(小 10 倍)"
    if video.get("play_url"):
        return video["play_url"], ("BGM 不是人声,改用视频" if title else "没有音轨名,用视频")
    if video.get("music_url"):
        return video["music_url"], "只有音轨可用"
    return None, "没有任何媒体地址"


def _download(url: str, dest: Path, timeout: float = 60.0,
              tries: int = 3) -> int:
    """下载媒体。**绕开系统代理** —— 这不是可选项。

    实测这台机器上有本地代理(HTTP_PROXY=127.0.0.1:6152),抖音 CDN 走它
    100% 失败(`SSL: UNEXPECTED_EOF_WHILE_READING`),因为流量被绕出国再回来。
    同一时刻直连是好的。`trust_env=False` 让 httpx 完全忽略 *_PROXY 环境变量。

    再加重试:CDN 偶尔会断流,而一次失败就放弃会让整批转写白跑。
    """
    last: Exception | None = None
    for i in range(tries):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True,
                              trust_env=False) as cl:
                with cl.stream("GET", url, headers=_HEADERS) as r:
                    r.raise_for_status()
                    n = 0
                    with dest.open("wb") as f:
                        for chunk in r.iter_bytes(65536):
                            f.write(chunk)
                            n += len(chunk)
            return n
        except Exception as e:      # noqa: BLE001 —— 网络错误种类多,统一重试
            last = e
            time.sleep(1.5 * (i + 1))
    raise last if last else RuntimeError("下载失败")


def transcribe_one(aweme_id: str, force: bool = False) -> dict[str, Any]:
    """转写一条作品。幂等 —— 已经转过就直接返回,不重跑。

    转写结果存 `transcripts(kind='asr')`,并**就地重建这条的片段**,
    否则新文字进不了检索(片段是检索的最小单位)。
    """
    existing = store.get_transcript(aweme_id, "asr") if hasattr(store, "get_transcript") else None
    if existing and not force:
        return {"aweme_id": aweme_id, "status": "already", "chars": len(existing)}

    v = store.get_video(aweme_id)
    if not v:
        return {"aweme_id": aweme_id, "status": "no_video"}
    dur = v.get("video_duration") or 0
    if dur and dur < MIN_MS:
        return {"aweme_id": aweme_id, "status": "too_short", "ms": dur}

    url, why = pick_media(v)
    if not url:
        return {"aweme_id": aweme_id, "status": "no_media", "reason": why}

    t0 = time.time()
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "media"
        try:
            size = _download(url, f)
        except Exception as e:
            return {"aweme_id": aweme_id, "status": "download_failed",
                    "error": f"{type(e).__name__}: {e}"[:160], "source": why}
        if size < 1024:
            return {"aweme_id": aweme_id, "status": "download_failed",
                    "error": f"只拿到 {size} 字节", "source": why}
        model = _load()
        # vad_filter 掐掉静音段:短视频前后常有几秒空白/纯音乐,
        # 不掐的话 whisper 会在那些段落上编出幻觉文本。
        segs, info = model.transcribe(
            str(f), vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            beam_size=5, condition_on_previous_text=False,
        )
        parts = [s.text.strip() for s in segs]
    text = " ".join(p for p in parts if p).strip()
    took = round(time.time() - t0, 1)

    if not text:
        return {"aweme_id": aweme_id, "status": "empty", "seconds": took, "source": why}

    store.save_transcript(aweme_id, "asr", text, {
        "model": f"faster-whisper/{_model_name}", "tier": 2,
        "lang": info.language, "lang_prob": round(info.language_probability, 3),
        "media": why, "bytes": size, "seconds": took,
    })
    _rebuild_fragments(aweme_id)
    return {"aweme_id": aweme_id, "status": "ok", "chars": len(text),
            "lang": info.language, "seconds": took, "source": why,
            "realtime_x": round((dur / 1000) / took, 1) if dur and took else None}


def _rebuild_fragments(aweme_id: str) -> None:
    """重建这条作品的片段,把新的逐字稿带进去。

    直接从库里现有字段拼,不碰 raw —— 转写是事后补的,那时 raw 早就落库了。
    """
    from knowledge import fragments as frag

    v = store.get_video(aweme_id)
    if not v:
        return
    ai = store.content_bundle(aweme_id) if hasattr(store, "content_bundle") else {}
    tags = store.video_tags(aweme_id) if hasattr(store, "video_tags") else []
    store.save_fragments(aweme_id, frag.build(v, ai or {}, tags or []))


def pending(limit: int = 50, scope: str = "following",
            only_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """列出**该转但还没转**的作品:没有抖音总结、也还没有逐字稿的。

    有总结的先不转 —— 平台已经给了要点,而 66% 一点内容都没有的那批更急。
    """
    where = [f"({store.scope_pred(scope, 'v')})" if scope != "all" else "1=1",
             "v.content_state <> 'have'",
             "NOT EXISTS(SELECT 1 FROM transcripts t "
             "  WHERE t.aweme_id = v.aweme_id AND t.kind = 'asr')",
             "(v.play_url IS NOT NULL OR v.music_url IS NOT NULL)",
             f"COALESCE(v.video_duration, 0) >= {MIN_MS}"]
    params: list[Any] = []
    if only_ids:
        where.append(f"v.aweme_id IN ({','.join('?' * len(only_ids))})")
        params.extend(only_ids)
    sql = (f"SELECT v.aweme_id, v.nickname, v.description, v.video_duration, "
           f"       v.music_title FROM videos v WHERE {' AND '.join(where)} "
           f"ORDER BY v.create_time DESC LIMIT ?")
    params.append(limit)
    with store.connect() as c:
        return [dict(r) for r in c.execute(sql, params)]


def run_batch(limit: int = 20, scope: str = "following",
              only_ids: list[str] | None = None,
              on_item=None) -> dict[str, Any]:
    """批量转写。返回逐条结果 —— **失败的也要带出来**,静默跳过会让人
    以为「转完了」,而实际上一半没成。"""
    todo = pending(limit, scope, only_ids)
    out, secs = [], 0.0
    for v in todo:
        r = transcribe_one(v["aweme_id"])
        r["nickname"] = v.get("nickname")
        r["title"] = (v.get("description") or "")[:40]
        secs += r.get("seconds") or 0
        out.append(r)
        if on_item:
            on_item(r)
    ok = [x for x in out if x["status"] == "ok"]
    return {
        "attempted": len(out), "ok": len(ok),
        "failed": [x for x in out if x["status"] not in ("ok", "already")],
        "chars": sum(x.get("chars", 0) for x in ok),
        "seconds": round(secs, 1),
        "items": out,
    }
