"""语义召回:向量 KNN → 按 owner 折叠 → 分三档。只读。业务无关。

三个实测出来的约束,写代码时绕不过:

1. **`distance` 是 L2 本身,不是 L2²。** 归一化向量下
   `cosine = 1 - d²/2`(实测和直接点积吻合到小数点后四位)。
   用 `1 - d/2` 会把 0.745 算成 0.643 —— 阈值是绝对值,换算错了全盘失效。

2. **vec0 的 KNN 不允许除 `distance` 之外的 ORDER BY / GROUP BY**,
   直接聚合会报 `Only a single 'ORDER BY distance' clause is allowed`。
   必须 `WITH ... AS MATERIALIZED` 强制物化,才能在外层折叠。

3. **同维度不同模型不会报错。** bge-m3 和 Qwen3-Embedding-0.6B 都是 1024 维,
   换了模型不重建索引,查询照跑、结果全是垃圾且看不出来 ——
   所以每次检索前比对指纹,不符就抛错。
"""

from __future__ import annotations

from typing import Any

from config import settings
from kb import embed as embed_mod
from kb import vecdb
from kb.space import Space

# 一条作品可能有 1~N 段(overview / 每章 / queries),多段会同时命中。
# 所以要多取再折叠 —— 想要 10 条作品就得先捞 ~40 段。
OVERFETCH = 4


def _cosine(distance: float) -> float:
    return 1.0 - distance * distance / 2.0


# 要过滤时得在 KNN **之后**过滤,所以要多捞:只捞 k 段的话,
# 万一前 k 段全来自被过滤掉的那一类,过滤完就空了。实测某外教号一人 1093 条作品,
# 这不是假想。
SCOPED_OVERFETCH = 6


def search(space: Space, query: str, limit: int = 10,
           include_maybe: bool = True, scope: str | None = None) -> dict[str, Any]:
    """语义检索。

    返回三档分流后的结果,**分数一律带出去**:
      good   ≥ settings.search_good    直接当相关结果
      maybe  ≥ settings.search_maybe   标「可能相关」,让人自己判断
      更低的丢弃;全丢弃就是「库里没有」

    为什么不做静默过滤:阈值来自十几组样本,而且我给「库里没有」打的标注
    本身就错过一次(mortgage rate 其实找对了)。机器分不清的那一档,
    判断权该交回给人 —— 前提是分数看得见。

    `scope` 的取值和语义由适配器定(见 Space.scope_sql)。**内核不校验它** ——
    现在 `/api/search?scope=乱写` 会落到抖音的 mine_pred 然后返回 200,
    加校验就是把 200 变 4xx,那是回归。校验放路由层。
    """
    if scope is None:
        scope = space.default_scope
    emb = embed_mod.get()
    conn = vecdb.connect(space.db(), expect_table=space.owner_table)
    try:
        vecdb.require_match(conn, emb.name, emb.dim,
                            space.meta.get("build_cmd", "scripts/build_index.py"))
        qv = emb.encode_query(query).tobytes()

        # ⚠️ 过取倍数由「要不要过滤」决定,**不是由 scope 这个字符串决定**。
        # 原来写的是 `OVERFETCH if scope == "all" else SCOPED_OVERFETCH` ——
        # 那把「all」这个业务词硬编进了内核。改成看谓词是不是 None,
        # 对抖音逐位等价(它的 scope_sql 在 all 时正好返回 None)。
        pred = space.scope_pred(scope, "hits")
        over = OVERFETCH if pred is None else SCOPED_OVERFETCH
        k = max(limit * over, 20)

        # 过滤放在物化之后的外层:vec0 的 MATCH 子句里塞不进 EXISTS 子查询。
        # ⚠️ 谓词拿 hits.<id 列> 去比,而 hits 是 CTE 不是表 ——
        # 能用是因为它只引用列名,不依赖表结构。
        where = "" if pred is None else f" WHERE {pred}"
        # MATERIALIZED 是必须的,见文件头第 2 条
        rows = conn.execute(f"""
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
            FROM hits{where} GROUP BY aweme_id ORDER BY best_d LIMIT ?
        """, (qv, k, limit)).fetchall()

        good, maybe, below = [], [], []
        for r in rows:
            score = _cosine(r["best_d"])
            item = {
                # SQL 里的列名是 aweme_id(物理列),返回体里用 space.id_key。
                # 抖音两者相同 → 逐字段等价;BOSS 那边返回 job_id。
                space.id_key: r["aweme_id"],
                "score": round(score, 4),
                "matched_kind": r["best_kind"],
                "at_sec": r["best_sec"],
                "n_matched_fragments": r["n_frags"],
                "frag_id": r["best_frag"],
            }
            (good if score >= settings.search_good
             else maybe if score >= settings.search_maybe
             else below).append(item)

        # ⚠️ 这里有个**现存的形状不一致**,内核必须原样保留:
        # include_maybe=False 时 maybe **不被 hydrate 但照样返回** ——
        # 那些条目保留内部 frag_id、没有 text/title/author/url。
        # 详见 scratchpad/baseline/QUIRKS.md Q1。顺手「修好」它就会毁掉
        # 「返回体逐字段相等」这个唯一的零回归证据,所以留到重构之后单独修。
        picked = good + (maybe if include_maybe else [])
        _hydrate(space, conn, picked)

        # verdict 而不是 nothing_relevant:后者是个陷阱。
        # 「有 maybe 但没有 good」时 nothing_relevant=False,读的人(尤其 AI)
        # 会以为「有相关内容」,然后拿低分结果当答案 —— 正是要防的失败模式,
        # 却被字段命名放了进来。所以给一个不会误读的三值:
        #   relevant    有过 good 线的
        #   only_maybe  只有「可能相关」,**不能当确定答案**
        #   nothing     一条都没过 maybe 线,库里就是没有
        verdict = ("relevant" if good else
                   "only_maybe" if maybe else "nothing")
        return {
            "query": query,
            "verdict": verdict,
            "good": good,
            "maybe": maybe,
            "has_relevant": bool(good),
            # 一条都没过线时把最接近的分数带上 —— 让人看到「差多少」,
            # 而不是只得到一句「没有」
            "nearest_below": [x["score"] for x in below[:3]] if verdict == "nothing" else [],
            "thresholds": {"good": settings.search_good, "maybe": settings.search_maybe},
            "scope": scope,
            "model": emb.name,
        }
    finally:
        conn.close()


def _hydrate(space: Space, conn, items: list[dict[str, Any]]) -> None:
    """补上给人看的东西:命中的那段原文 + 业务信息。就地改写。"""
    if not items:
        return
    ids = [i["frag_id"] for i in items]
    texts = {
        r["rowid"]: r["text"]
        for r in conn.execute(
            f"SELECT rowid, text FROM fragments WHERE rowid IN "
            f"({','.join('?' * len(ids))})", ids)
    }
    # 业务字段一次问完。**键名由适配器定,内核只 update** —— 这是抖音返回体
    # 逐字段不变的唯一办法(前端和 MCP 都在读 title/author/url/cat/digg_count)。
    metas = space.fetch_meta([it[space.id_key] for it in items])
    for it in items:
        it["text"] = texts.get(it["frag_id"], "")
        it.update(metas.get(it[space.id_key]) or {})
        it.pop("frag_id", None)
