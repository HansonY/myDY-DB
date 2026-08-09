"""把一条岗位拼成等粒度的知识片段。

和抖音那份 `fragments.py` 是同一个角色、同一套输出结构 —— 因为下游
(embed / vecdb / search)完全不认识业务,只认 `{kind, text}`。

三类段的分工:
  overview 标题+公司+城市+薪资+经验学历+技能标签 —— 每条都有,是兜底。
           检索「上海 20K 以上的 Python 岗」时靠它。
  jd       职位描述正文,长的切块。**这是这个库的核心** ——
           「我投的岗位反复要求什么」只有它能回答。
  chat     我和 HR 那一行摘要。量少但很具体(常常是「你有没有 X 经验」)。

片段是派生数据,随时可全量重建,源头是 jobs.jd 和 jobs.raw_z。
"""

from __future__ import annotations

import json
from typing import Any

# 太短的段单独存没意义 —— 检索里只会当噪音,信息已被 overview 涵盖
MIN_CHARS = 12

# JD 切块大小。招聘 JD 常见 200–800 字,一般一到两块;
# 太长不切会让向量被稀释(整段取平均,什么都不像),切太碎又会丢上下文。
JD_CHUNK = 380


def _join(parts: list[str | None]) -> str:
    """去重保序拼接,**包含式去重**。

    为什么不能只做精确去重:岗位标题常常整句出现在 JD 第一行,
    两个字符串不相等但一个包含另一个 —— 重复内容会让向量偏向那句话,
    是实打实的召回损失。(抖音那边 item_title 和 desc 就是这个毛病。)
    """
    kept: list[str] = []
    for p in parts:
        if not p:
            continue
        t = " ".join(str(p).split())
        if not t:
            continue
        if any(t in k for k in kept):
            continue
        kept = [k for k in kept if k not in t]
        kept.append(t)
    return " ".join(kept)


def _tags(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v if x]
    if isinstance(v, str) and v.strip():
        try:
            got = json.loads(v)
            return [str(x) for x in got if x] if isinstance(got, list) else [v]
        except ValueError:
            return [v]
    return []


def _chunk(text: str, size: int = JD_CHUNK) -> list[str]:
    """按**句子边界**切,不按字符数硬切。

    硬切会把词劈开(实测切出过「n 开发经验,熟悉 FastAPI」——「Python」被拦腰截断),
    这种碎片进向量就是噪音。所以先按句末标点切成句子,再贪心装箱;
    单句超长才退回硬切。
    """
    import re
    sents = [s for s in re.split(r"(?<=[。;;!?!?\n])", text) if s.strip()]
    out: list[str] = []
    cur = ""
    for s in sents:
        if len(s) > size:                      # 单句就超长,只能硬切
            if cur:
                out.append(cur)
                cur = ""
            for i in range(0, len(s), size):
                out.append(s[i:i + size])
            continue
        if len(cur) + len(s) <= size:
            cur += s
        else:
            if cur:
                out.append(cur)
            cur = s
    if cur:
        out.append(cur)
    return [c.strip() for c in out if c.strip()]


def build(job: dict[str, Any], chat_snippet: str | None = None) -> list[dict[str, Any]]:
    """拼出一条岗位的所有片段。

    job          jobs 表的一行
    chat_snippet 这个岗位下我和 HR 聊的最后一句摘要(可选)
    """
    frags: list[dict[str, Any]] = []

    # ── overview ──
    # 公司和城市也放进去:检索「上海的 AI 岗」时它们是有效信号。
    # 薪资用原文而不是拆出来的数字 —— 「20-35K·14薪」整体才可读。
    head = _join([
        job.get("title"),
        job.get("company"),
        _join([job.get("city"), job.get("district")]),
        job.get("salary_text"),
        _join([job.get("experience"), job.get("degree")]),
        " ".join(_tags(job.get("tags"))) or None,
    ])
    if len(head) >= MIN_CHARS:
        frags.append({"kind": "overview", "text": head})

    # ── jd:核心 ──
    jd = (job.get("jd") or "").strip()
    if len(jd) >= MIN_CHARS:
        head_hint = f"{job.get('title') or ''} {job.get('company') or ''}".strip()
        for piece in _chunk(jd):
            if len(piece) < MIN_CHARS:
                continue
            # 每块前面带上岗位标题 —— 切开之后单看一块不知道是哪个岗位的要求。
            # 但**已经包含标题的块不再加**,否则第一块会出现两遍
            # (JD 正文常常以标题开头),重复内容会让向量偏向那句话。
            text = piece if (not head_hint or head_hint.split()[0] in piece) \
                else f"{head_hint} {piece}"
            frags.append({"kind": "jd", "text": text})

    # ── chat ──
    snip = (chat_snippet or "").strip()
    if len(snip) >= MIN_CHARS:
        frags.append({"kind": "chat", "text": snip})

    return frags
