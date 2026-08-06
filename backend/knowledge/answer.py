"""基于检索结果回答问题,**强制带出处**。

知识库最大的失败模式不是找不到,是给出一个听着对、但查不到来源的答案 ——
你会拿它当真。这些内容是刷视频刷来的,本来就该带着「谁说的、第几分钟说的」。

所以这里做三件事,缺一不可:
  1. 没有过线的检索结果时,**不调模型**,直接说库里没有。
     让模型在没有依据的情况下回答,它一定会编。
  2. 上下文里每段都编号,提示词要求答案里标 [1][2]。
  3. **回来之后校验引用**:模型引用了不存在的编号就剔除。
     这一步是纯函数,不需要 API key 就能测。
"""

from __future__ import annotations

import re
from typing import Any

from config import settings
from knowledge import search as search_mod

# 通义千问兼容端点。用 qwen-plus 而不是带慢思考的型号 —— 后者会挂到分钟级。
ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
MODEL = "qwen-plus"
TIMEOUT = 60          # 端点必须带超时,不然卡住没人知道

SYSTEM = """你在回答用户关于「他自己收藏的抖音视频」的问题。

规则:
1. 只用下面给出的资料回答。资料里没有的,直说没有,不要用常识补。
2. 每个论断后面标出处编号,如 [1]、[2][3]。没有出处的话不要写。
3. 资料是视频内容的文字总结,可能不完整。信息不足就说明还缺什么。
4. 中文回答,除非用户用英文问。简洁,不写客套话。"""


def _build_context(items: list[dict[str, Any]]) -> str:
    """把检索结果拼成带编号的资料。编号从 1 开始 —— 和提示词里的 [1] 对齐。"""
    parts = []
    for i, it in enumerate(items, 1):
        head = f"[{i}] {it.get('author') or '未知作者'}《{(it.get('title') or '')[:60]}》"
        if it.get("at_sec") is not None:
            head += f" 第 {it['at_sec'] // 60}:{it['at_sec'] % 60:02d} 处"
        head += f"(相关度 {it['score']})"
        parts.append(f"{head}\n{it.get('text', '')}")
    return "\n\n".join(parts)


_CITE = re.compile(r"\[(\d+)\]")


def verify_citations(answer: str, n_sources: int) -> tuple[str, list[int], list[int]]:
    """校验并清理答案里的引用编号。**纯函数,可离线测试。**

    返回 (清理后的答案, 用到的合法编号, 被剔除的非法编号)。

    为什么必须做:模型会引用不存在的编号 —— 给了 4 条资料它写 [5]。
    留着的话读者点不开、也无法核对,而一个无法核对的出处比没有出处更糟,
    因为它看起来是可核对的。
    """
    used, bogus = [], []
    for m in _CITE.finditer(answer):
        k = int(m.group(1))
        (used if 1 <= k <= n_sources else bogus).append(k)
    # 删掉编号会留下断句:「见 [1] 和 。」「参考 和 [1]」。所以连着相邻的
    # 连接词一起删 —— 否则答案看起来像程序出了 bug,而它其实是在保护你
    # 不去信一个假出处。
    cleaned = answer
    for k in sorted(set(bogus)):
        # 优先吃掉前面的连接词,没有就吃后面的
        cleaned = re.sub(rf"\s*[和与及、]\s*\[{k}\]", "", cleaned)
        cleaned = re.sub(rf"\[{k}\]\s*[和与及、]\s*", "", cleaned)
        cleaned = cleaned.replace(f"[{k}]", "")
    cleaned = re.sub(r"\s+([。,、;:!?)】」])", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return cleaned, sorted(set(used)), sorted(set(bogus))


def _call_llm(question: str, context: str) -> str:
    import httpx
    key = settings.dashscope_api_key.strip()
    if not key:
        raise RuntimeError(
            "问答需要 DASHSCOPE_API_KEY(写进 .env)。\n"
            "只想检索不问答的话用 search —— 那一路全在本地,不需要任何 key。"
        )
    r = httpx.post(
        ENDPOINT,
        headers={"Authorization": f"Bearer {key}"},
        json={"model": MODEL, "temperature": 0.2,
              "messages": [{"role": "system", "content": SYSTEM},
                           {"role": "user",
                            "content": f"资料:\n\n{context}\n\n问题:{question}"}]},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def ask(question: str, k: int = 8) -> dict[str, Any]:
    """检索 + 回答 + 出处。"""
    res = search_mod.search(question, limit=k)
    items = res["good"] + res["maybe"]

    if not items:
        # 不调模型。没有依据时让它回答,它一定会编 ——
        # 而编出来的答案你分辨不出来,这比没有答案危险得多。
        return {
            "question": question, "answered": False,
            "answer": "库里没有相关内容。",
            "reason": f"没有片段的相关度达到下限 {res['thresholds']['maybe']}",
            "nearest_scores": res["nearest_below"],
            "citations": [], "model": None,
        }

    raw = _call_llm(question, _build_context(items))
    answer, used, bogus = verify_citations(raw, len(items))

    return {
        "question": question, "answered": True, "answer": answer,
        # 只回引用到的那几条 —— 列一堆没被用到的「出处」是噪音
        "citations": [
            {"n": i, "author": items[i - 1].get("author"),
             "title": items[i - 1].get("title"),
             "url": items[i - 1].get("url"),
             "at_sec": items[i - 1].get("at_sec"),
             "score": items[i - 1]["score"],
             "quote": (items[i - 1].get("text") or "")[:160]}
            for i in used
        ],
        "sources_given": len(items),
        # 露出来,不藏:模型编了几个不存在的编号是它可靠性的直接信号
        "dropped_bogus_citations": bogus,
        "verdict": res["verdict"],
        "only_maybe": res["verdict"] == "only_maybe",
        "model": MODEL,
    }
