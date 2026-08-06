"""语义召回:向量 KNN → 按作品折叠 → 分三档。只读。

三个实测出来的约束,写代码时绕不过:

1. **`distance` 是 L2 本身,不是 L2²。** 归一化向量下
   `cosine = 1 - d²/2`(实测和直接点积吻合到小数点后四位)。
   用 `1 - d/2` 会把 0.745 算成 0.643 —— 阈值是绝对值,换算错了全盘失效。

2. **vec0 的 KNN 不允许除 `distance` 之外的 ORDER BY / GROUP BY**,
   直接聚合会报 `Only a single 'ORDER BY distance' clause is allowed`。
   必须 `WITH ... AS MATERIALIZED` 强制物化,才能在外层按作品折叠。

3. **同维度不同模型不会报错。** bge-m3 和 Qwen3-Embedding-0.6B 都是 1024 维,
   换了模型不重建索引,查询照跑、结果全是垃圾且看不出来 ——
   所以每次检索前比对指纹,不符就抛错。
"""

from __future__ import annotations

from typing import Any

from config import settings
from db import store
from knowledge import embed as embed_mod
from knowledge import vecdb

# 一条作品可能有 1~N 段(overview / 每章 / queries),多段会同时命中。
# 所以要多取再折叠 —— 想要 10 条作品就得先捞 ~40 段。
OVERFETCH = 4


def _cosine(distance: float) -> float:
    return 1.0 - distance * distance / 2.0


def search(query: str, limit: int = 10,
           include_maybe: bool = True) -> dict[str, Any]:
    """语义检索。

    返回三档分流后的结果,**分数一律带出去**:
      good   ≥ settings.search_good    直接当相关结果
      maybe  ≥ settings.search_maybe   标「可能相关」,让人自己判断
      更低的丢弃;全丢弃就是「库里没有」

    为什么不做静默过滤:阈值来自十几组样本,而且我给「库里没有」打的标注
    本身就错过一次(mortgage rate 其实找对了)。机器分不清的那一档,
    判断权该交回给人 —— 前提是分数看得见。
    """
    emb = embed_mod.get()
    conn = vecdb.connect()
    try:
        vecdb.require_match(conn, emb.name, emb.dim)
        qv = emb.encode_query(query).tobytes()
        k = max(limit * OVERFETCH, 20)

        # MATERIALIZED 是必须的,见文件头第 2 条
        rows = conn.execute("""
            WITH hits AS MATERIALIZED (
                SELECT frag_id, aweme_id, kind, start_sec, distance
                FROM frag_vec WHERE emb MATCH ? AND k = ?
            )
            SELECT aweme_id,
                   MIN(distance)                              AS best_d,
                   COUNT(*)                                   AS n_frags,
                   (SELECT frag_id  FROM hits h2 WHERE h2.aweme_id = hits.aweme_id
                     ORDER BY h2.distance LIMIT 1)            AS best_frag,
                   (SELECT kind      FROM hits h2 WHERE h2.aweme_id = hits.aweme_id
                     ORDER BY h2.distance LIMIT 1)            AS best_kind,
                   (SELECT start_sec FROM hits h2 WHERE h2.aweme_id = hits.aweme_id
                     ORDER BY h2.distance LIMIT 1)            AS best_sec
            FROM hits GROUP BY aweme_id ORDER BY best_d LIMIT ?
        """, (qv, k, limit)).fetchall()

        good, maybe, below = [], [], []
        for r in rows:
            score = _cosine(r["best_d"])
            item = {
                "aweme_id": r["aweme_id"],
                "score": round(score, 4),
                "matched_kind": r["best_kind"],
                "at_sec": r["best_sec"],
                "n_matched_fragments": r["n_frags"],
                "frag_id": r["best_frag"],
            }
            (good if score >= settings.search_good
             else maybe if score >= settings.search_maybe
             else below).append(item)

        picked = good + (maybe if include_maybe else [])
        _hydrate(conn, picked)
        return {
            "query": query,
            "good": good,
            "maybe": maybe,
            # 一条都没过线时,把最接近的几条也带上 —— 让人看到「差多少」,
            # 而不是只得到一句「没有」
            "nothing_relevant": not picked,
            "nearest_below": below[:3] if not picked else [],
            "thresholds": {"good": settings.search_good, "maybe": settings.search_maybe},
            "model": emb.name,
        }
    finally:
        conn.close()


def _hydrate(conn, items: list[dict[str, Any]]) -> None:
    """补上给人看的东西:命中的那段原文 + 作品信息。就地改写。"""
    if not items:
        return
    ids = [i["frag_id"] for i in items]
    texts = {
        r["rowid"]: r["text"]
        for r in conn.execute(
            f"SELECT rowid, text FROM fragments WHERE rowid IN "
            f"({','.join('?' * len(ids))})", ids)
    }
    for it in items:
        it["text"] = texts.get(it["frag_id"], "")
        v = store.get_video(it["aweme_id"]) or {}
        it["title"] = v.get("item_title") or v.get("description") or ""
        it["author"] = v.get("nickname")
        it["url"] = v.get("share_url")
        it["cat"] = " › ".join(c for c in (v.get("cat1"), v.get("cat2"),
                                           v.get("cat3")) if c) or None
        it["digg_count"] = v.get("digg_count")
        it.pop("frag_id", None)
