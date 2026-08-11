"""建 / 同步向量索引。业务无关 —— 只认 fragments 和 frag_vec。

两种模式,区别很实际:

  sync     只嵌「还没进索引」的片段。日常用这个 —— 采集完几条新作品,
           不该为此把 5153 段全嵌一遍。
  rebuild  全量重建。换模型、改分块策略时用,53 秒。

**为什么不在采集流程里顺手嵌。** bge-m3 是 2.2 GB 模型,加载进采集进程
等于每次采集多花几十秒 + 几 G 内存,而采集本身已经被 403 和翻页间隔
拖得很慢了。所以采集只负责生成片段(那是纯 Python、零成本),
向量批量补 —— 这就是 sync 存在的理由。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from kb import embed as embed_mod
from kb import vecdb
from kb.space import Space

BATCH = 256


def _pending(conn) -> list[dict[str, Any]]:
    """还没进索引的片段。

    判据是 fragments 的 rowid 不在 frag_vec 里。片段表是「先删后插」重建的
    (见 store.save_fragments),所以同一条作品重建后 rowid 会变 ——
    旧向量因此变成孤儿,需要一起清掉,否则会被检索到而对不上原文。
    """
    return [dict(r) for r in conn.execute("""
        SELECT f.rowid AS frag_id, f.aweme_id, f.kind, f.start_sec, f.n_chars, f.text
        FROM fragments f
        WHERE f.rowid NOT IN (SELECT frag_id FROM frag_vec)
        ORDER BY f.rowid
    """)]


def _orphans(conn) -> int:
    """索引里指向已不存在片段的向量条数。

    ⚠️ 这个查的是「rowid 还在不在」,**查不出「rowid 还在但指错了段」**。
    VACUUM 会重排 rowid(fragments 的主键是 `(aweme_id, idx)`,不是
    INTEGER PRIMARY KEY),重排之后每条向量都还有对应行,只是内容对不上了 ——
    症状是检索命中的原文和作品不匹配,而分数看着完全正常。
    抽样锚点校验见 scratchpad/baseline/QUIRKS.md Q2,单独一步做(会改 status 的返回体)。
    """
    return conn.execute(
        "SELECT COUNT(*) n FROM frag_vec "
        "WHERE frag_id NOT IN (SELECT rowid FROM fragments)"
    ).fetchone()["n"]


def sync(space: Space, rebuild: bool = False,
         on_progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    """把片段的向量补齐。返回统计。"""
    # 顺序要紧:先 init_db 再 connect。connect 里的 owner_table 断言靠这个顺序 ——
    # 建完表再断言,所以「space 拿到了另一个库的 init_db」会被抓到(建不出 jobs)。
    space.init_db()
    emb = embed_mod.get()
    conn = vecdb.connect(space.db(), expect_table=space.owner_table)
    try:
        meta = vecdb.read_meta(conn)
        # 模型或维度变了必须重建 —— 混着两个模型的向量,相似度完全没有意义
        if meta and (meta["model"] != emb.name or meta["dim"] != emb.dim):
            if not rebuild:
                raise vecdb.IndexMismatch(
                    f"索引是 {meta['model']}({meta['dim']} 维)建的,"
                    f"现在配置 {emb.name}({emb.dim} 维)。加 --rebuild 重建。"
                )
            rebuild = True

        if rebuild:
            vecdb.drop_vec_table(conn)
        vecdb.create_vec_table(conn, emb.dim)

        # 清掉孤儿:片段被重建过,旧 rowid 已经不存在
        killed = _orphans(conn)
        if killed:
            conn.execute("DELETE FROM frag_vec "
                         "WHERE frag_id NOT IN (SELECT rowid FROM fragments)")

        todo = _pending(conn)
        total = len(todo)
        t0 = time.time()
        done = 0
        for i in range(0, total, BATCH):
            chunk = todo[i:i + BATCH]
            vecs = emb.encode_docs([c["text"] for c in chunk])
            conn.executemany(
                "INSERT INTO frag_vec (frag_id, emb, aweme_id, kind, start_sec, n_chars) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(c["frag_id"], v.tobytes(), c["aweme_id"], c["kind"],
                  c["start_sec"], c["n_chars"])
                 for c, v in zip(chunk, vecs)],
            )
            conn.commit()
            done += len(chunk)
            if on_progress:
                on_progress(done, total)

        n = conn.execute("SELECT COUNT(*) n FROM frag_vec").fetchone()["n"]
        vecdb.write_meta(conn, emb.name, emb.dim, n)
        conn.commit()
        return {"model": emb.name, "dim": emb.dim, "embedded": done,
                "orphans_removed": killed, "total_vectors": n,
                "seconds": round(time.time() - t0, 1)}
    finally:
        conn.close()


def status(space: Space) -> dict[str, Any]:
    """索引现状。不加载模型 —— 光看状态不该等 bge-m3 加载几秒。"""
    from config import settings
    conn = vecdb.connect(space.db(), expect_table=space.owner_table)
    try:
        meta = vecdb.read_meta(conn)
        have = conn.execute(
            "SELECT name FROM sqlite_master WHERE name='frag_vec'").fetchone()
        n_vec = conn.execute("SELECT COUNT(*) n FROM frag_vec").fetchone()["n"] if have else 0
        n_frag = conn.execute("SELECT COUNT(*) n FROM fragments").fetchone()["n"]
        pending = (conn.execute(
            "SELECT COUNT(*) n FROM fragments WHERE rowid NOT IN "
            "(SELECT frag_id FROM frag_vec)").fetchone()["n"] if have else n_frag)
        return {
            "model": (meta or {}).get("model"),
            "dim": (meta or {}).get("dim"),
            "built_at": (meta or {}).get("built_at"),
            "vectors": n_vec, "fragments": n_frag, "pending": pending,
            "orphans": _orphans(conn) if have else 0,
            "configured_model": settings.embed_model,
            "in_sync": bool(have and pending == 0 and meta
                            and meta["model"] == settings.embed_model),
        }
    finally:
        conn.close()
