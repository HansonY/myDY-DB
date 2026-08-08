"""把一条作品的散字段拼成等粒度的知识片段。

**为什么需要这一层。** 一条作品的可用文本散在五个地方:干净标题、作者文案、
整段内容总结、逐章说明、搜索意图词。长度从 10 字到 2000 字差两个数量级,
结构也各不相同。直接拿 videos 做检索,等于让关键词索引和向量索引去啃
一堆形状不一的东西 —— 召回质量没法控。

拼成片段之后,检索的最小单位统一了,而且**引用能精确到章节的时间戳**。

三类段各有分工:
  overview  标题+文案+整段总结+话题 —— 几乎每条都有,是兜底
  chapter   每章一段,带 start_sec —— 引用时能说「第 12:30 处」
  queries   「大家都在搜」—— 真人写的查询语句。它本身就是查询形态的文本,
            恰好补上向量对型号/专名召回差的短板

片段是**派生数据**,源头永远是 raw_z,所以拼装策略随时可以改、全量重建。
"""

from __future__ import annotations

from typing import Any

# 段的下限。低于这个字数的段单独存没意义 —— 检索里只会当噪音,
# 还会挤掉真正有内容的段。它们的信息已经被 overview 段涵盖了。
MIN_CHARS = 12

# 一条 overview 段的软上限。超长的作者文案(实测最长 939 字)整段塞进去,
# 嵌入时会被主题稀释;但也不硬切 —— 文案本来就是一个整体。
OVERVIEW_SOFT_MAX = 1200

# 逐字稿分块大小。一条 60–120 秒的视频大约 200–500 字,多数一块装得下;
# 长视频切开,让每块都能独立命中(向量对整段取平均,一块太长就什么都不像)。
ASR_CHUNK = 420


def _join(parts: list[str | None]) -> str:
    """去重保序地拼接,**包含式去重**而不只是精确去重。

    为什么不能只做精确去重:`item_title` 常常就是 `desc` 的第一句 ——
    实测「基础教学第4集,liftoff连接遥控器设置教学~一通百通~」在同一段里
    出现了两遍(item_title 一次、desc 开头一次),两个字符串不相等,
    精确去重抓不到。重复内容会让嵌入偏向那句话,是实打实的召回损失。

    所以:某一段已经被前面收进去的内容包含,就丢掉;反过来如果新来的更长
    且包含旧的,就用新的替换旧的。段数只有 6 个左右,O(n²) 无所谓。
    """
    kept: list[str] = []
    for p in parts:
        if not p:
            continue
        t = " ".join(str(p).split())      # 压掉换行和连续空格
        if not t:
            continue
        # 已被收录的内容包含它 → 跳过
        if any(t in k for k in kept):
            continue
        # 它包含某些已收录的(更完整)→ 替换掉那些
        kept = [k for k in kept if k not in t]
        kept.append(t)
    return " ".join(kept)


def build(video: dict[str, Any], content: dict[str, Any],
          hashtags: list[str] | None = None) -> list[dict[str, Any]]:
    """拼出一条作品的所有片段。

    video   videos 表的一行(要 item_title / description / nickname / cat*)
    content collector.douyin._content_from_raw() 的返回
    hashtags 结构化话题(平台给的,比从文案正则抠准)
    """
    frags: list[dict[str, Any]] = []

    # ── overview ──────────────────────────────────────────
    # 作者和分类也放进去:检索「某某讲的科技内容」时它们是有效信号,
    # 而且分类是官方打的,比话题标签权威。
    cats = [video.get("cat1"), video.get("cat2"), video.get("cat3")]
    head = _join([
        content.get("item_title") or video.get("item_title"),
        video.get("description"),
        content.get("summary"),
        video.get("nickname"),
        " ".join(c for c in cats if c) or None,
        " ".join(f"#{h}" for h in (hashtags or [])) or None,
    ])
    if len(head) >= MIN_CHARS:
        frags.append({"kind": "overview", "text": head[:OVERVIEW_SOFT_MAX]})

    # ── chapter:一章一段,带时间戳 ────────────────────────
    for ch in content.get("chapters") or []:
        text = _join([ch.get("desc"), ch.get("detail")] + list(ch.get("points") or []))
        if len(text) < MIN_CHARS:
            continue
        # timestamp 是毫秒(实测 3384000 = 56 分),存秒方便前端直接用
        ms = ch.get("t")
        frags.append({
            "kind": "chapter",
            "start_sec": int(ms // 1000) if isinstance(ms, (int, float)) else None,
            "text": text,
        })

    # ── queries:「大家都在搜」 ────────────────────────────
    qs = content.get("queries") or []
    if qs:
        text = " / ".join(qs)
        if len(text) >= MIN_CHARS:
            frags.append({"kind": "queries", "text": text})

    # ── asr:自己转出来的逐字稿 ─────────────────────────────
    # 和 summary 是**互补不是替代**:总结是抽象的要点(「依恋半衰期约 4.18 年」),
    # 逐字稿是原话和细节。问「大意」时总结更准,问「他原话怎么说的」
    # 「有没有提到某个具体名词」时只有逐字稿能答。
    #
    # 单独成段而不是拼进 overview:一段几百上千字的逐字稿混进 overview 会把
    # 标题和总结冲淡 —— 向量是整段取平均,长文本会把短而准的信号淹掉。
    # 太长就切块,每块都能独立命中。
    asr = (content.get("asr") or "").strip()
    if len(asr) >= MIN_CHARS:
        for i in range(0, len(asr), ASR_CHUNK):
            piece = asr[i:i + ASR_CHUNK]
            if len(piece) >= MIN_CHARS:
                frags.append({"kind": "asr", "text": piece})

    return frags
