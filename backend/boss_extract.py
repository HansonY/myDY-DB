"""从页面文字里提取岗位信息 —— **一次调用处理多页**,供应商无关。

**为什么走这条路而不是解析接口。**
之前一直卡在「我不知道 BOSS 的字段叫什么」:接口路径会变、字段名会变、
页面 class 更是随时改。而**页面上你看到的文字不会变** —— 它就是数据本身。
抓 innerText 交给 LLM 提结构,平台改版对它没有影响。

**为什么批量。** 一次 API 往返几秒,逐页调用既慢又贵,而模型完全有能力
一次读几页。按字符预算打包而不是按页数 —— 详情页几千字、列表页可能两万字,
按页数打包会一会儿太空一会儿超限。

用哪家模型由 .env 的 LLM_PROVIDER 决定(千问 / MiniMax / DeepSeek / 本地 Ollama …)——
它们都是 OpenAI 兼容的 chat/completions,差别只有 base_url、model、key,
所以统一走 llm.py,这里不认任何一家。

没配 key 时**不假装能提取**:原文照样存下来(待处理),配好再补。
宁可留一批待处理,也不要瞎猜出错的结构化数据。
"""

from __future__ import annotations

import json
import re
from typing import Any

import llm

# 一次调用塞多少字。现在这几家上下文都很大,但塞太满会让模型偷懒漏页 ——
# 18K 字符大概是 4–6 个详情页,或 1–2 个长列表页。
CHARS_PER_CALL = 18000

PROMPT = """你从招聘网站页面的纯文本里提取岗位信息。**一次会给你多个页面**,逐个处理。

规则:
1. 只提取**真实出现在文本里**的信息。没写的字段留 null,**绝对不要猜、不要补全**。
2. 一个页面可能有多个岗位(列表页),也可能只有一个(详情页)。
3. salary_text 用原文(如 "20-35K·14薪"),同时尽量拆出 min/max(千元/月)和 months。
4. jd 是职位描述/岗位职责/任职要求的正文,**原样保留换行和条目编号**,不要改写不要总结。
   列表页通常没有 jd,那就留 null。
5. tags 是文本里明确出现的技能词(如 Python、FastAPI),不要自己联想。
6. 页面上如果有「已投递/已沟通/面试/已收藏」这类我和这个岗位的关系,写进 my_status;
   看不出来就 null。

输入是若干段,每段以 `=== PAGE n ===` 开头。
只输出 JSON,不要解释、不要代码块围栏。每个页面对应一个 pages 元素,
idx 必须和输入的 n 一致(没提到岗位的页面给空 jobs 数组):
{"pages":[{"idx":1,"jobs":[{"title":null,"company":null,"city":null,"district":null,
"salary_text":null,"salary_min":null,"salary_max":null,"salary_months":null,
"experience":null,"degree":null,"jd":null,"tags":[],
"hr_name":null,"hr_title":null,"my_status":null}]}]}"""


# 供应商无关 —— 千问 / MiniMax / DeepSeek 都行,换家只改 .env。
# 之前把千问的端点和模型名写死在这里,想换一家就得改代码。
NoKey = llm.NoKey
available = llm.available


def pack(pages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """按字符预算打包。单页超预算的自己独占一批。"""
    batches: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    n = 0
    for p in pages:
        ln = len(p.get("text") or "")
        if cur and n + ln > CHARS_PER_CALL:
            batches.append(cur)
            cur, n = [], 0
        cur.append(p)
        n += ln
    if cur:
        batches.append(cur)
    return batches


def extract_batch(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """一次提取多个页面。返回 `[{"idx": 在入参里的下标, "jobs": [...]}, …]`。

    只发**一次** API 请求。调用方要处理更多页时先用 `pack()` 分批。
    """
    if not llm.available():
        raise llm.NoKey("没配 LLM 的 API key")
    if not pages:
        return []

    parts = []
    for i, p in enumerate(pages, 1):
        kind = {"detail": "岗位详情页", "list": "岗位列表页"}.get(p.get("kind"), "页面")
        head = f"=== PAGE {i} ===\n[{kind}"
        if p.get("title"):
            head += f" · 标题:{p['title']}"
        head += "]"
        parts.append(f"{head}\n{(p.get('text') or '')[:CHARS_PER_CALL]}")

    data = llm.chat_json(PROMPT, "\n\n".join(parts), timeout=180)
    if not isinstance(data, dict):
        raise RuntimeError(f"模型返回的不是对象:{str(data)[:120]}")

    out: list[dict[str, Any]] = []
    for pg in (data.get("pages") or []):
        if not isinstance(pg, dict):
            continue
        idx = pg.get("idx")
        if not isinstance(idx, int) or not (1 <= idx <= len(pages)):
            continue
        out.append({"idx": idx - 1, "jobs": _clean(pg.get("jobs"))})
    return out


def _clean(jobs: Any) -> list[dict[str, Any]]:
    """清洗模型输出。没标题的丢掉 —— 那基本是从页脚之类的地方硬凑的。"""
    if not isinstance(jobs, list):
        return []
    out = []
    for j in jobs:
        if not isinstance(j, dict) or not str(j.get("title") or "").strip():
            continue
        # 数值字段兜底:模型偶尔会给 "20" 这种字符串
        for k in ("salary_min", "salary_max", "salary_months"):
            v = j.get(k)
            if isinstance(v, str):
                m = re.search(r"\d+", v)
                j[k] = int(m.group()) if m else None
            elif not isinstance(v, (int, float, type(None))):
                j[k] = None
        if not isinstance(j.get("tags"), list):
            j["tags"] = []
        out.append(j)
    return out
