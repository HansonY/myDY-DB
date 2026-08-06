"""向量表的连接与建表。唯一碰 sqlite-vec 的地方。

**为什么不在 db/store.py 里加载扩展。** sqlite-vec 是可选依赖
(requirements-search.txt)。只做采集和字面搜索的人不该因为没装它
而连库都连不上 —— 所以扩展加载留在这里,store 保持零额外依赖。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from config import settings

# 索引指纹表。**这张表是防静默出错的关键**,不是记账用的。
#
# 维度不符时 sqlite-vec 会报错(实测 `Dimension mismatch for query vector`),
# 但**同维度不同模型不会报错** —— bge-m3 和 Qwen3-Embedding-0.6B 都是
# 1024 维,换了模型不重建索引,查询照样跑、返回的全是垃圾而且看不出来。
# 所以检索前必须比对模型名,不符就拒绝,不猜不降级。
META_SQL = """
CREATE TABLE IF NOT EXISTS vec_meta (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    model     TEXT    NOT NULL,
    dim       INTEGER NOT NULL,
    n_vectors INTEGER NOT NULL DEFAULT 0,
    built_at  TEXT    NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    """开一个加载了 sqlite-vec 的连接。"""
    try:
        import sqlite_vec
    except ImportError as e:
        raise RuntimeError(
            "缺少 sqlite-vec。装一下:\n"
            "  .venv/bin/pip install -r backend/requirements-search.txt"
        ) from e

    conn = sqlite3.connect(settings.db_file)
    conn.row_factory = sqlite3.Row
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except AttributeError as e:
        # 某些发行版的 Python 编译时关掉了扩展加载
        raise RuntimeError(
            "这个 Python 的 sqlite3 不支持加载扩展,装不了 sqlite-vec。"
            "换 python.org 或 homebrew 装的 Python 3.10–3.13。"
        ) from e
    conn.execute(META_SQL)
    return conn


def create_vec_table(conn: sqlite3.Connection, dim: int) -> None:
    """建向量表。

    辅助列(`+` 前缀)实测可用,所以 aweme_id / kind / start_sec 直接存在
    向量表里 —— 召回时不用回 fragments 表 join,少一次查询。
    """
    conn.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS frag_vec USING vec0(
            frag_id    INTEGER PRIMARY KEY,
            emb        float[{dim}],
            +aweme_id  TEXT,
            +kind      TEXT,
            +start_sec INTEGER,
            +n_chars   INTEGER
        )
    """)


def drop_vec_table(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS frag_vec")


def read_meta(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM vec_meta WHERE id = 1").fetchone()
    return dict(row) if row else None


def write_meta(conn: sqlite3.Connection, model: str, dim: int, n: int) -> None:
    from datetime import datetime, timezone
    conn.execute(
        "INSERT INTO vec_meta (id, model, dim, n_vectors, built_at) "
        "VALUES (1, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET model=excluded.model, dim=excluded.dim, "
        "n_vectors=excluded.n_vectors, built_at=excluded.built_at",
        (model, dim, n, datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )


class IndexMismatch(RuntimeError):
    """索引是用别的模型建的。宁可报错,也不能拿错模型的向量算相似度。"""


def require_match(conn: sqlite3.Connection, model: str, dim: int) -> dict[str, Any]:
    meta = read_meta(conn)
    if not meta or not meta["n_vectors"]:
        raise IndexMismatch(
            "还没有建向量索引。跑一次:\n"
            "  .venv/bin/python scripts/build_index.py"
        )
    if meta["model"] != model or meta["dim"] != dim:
        raise IndexMismatch(
            f"索引是用 {meta['model']}({meta['dim']} 维)建的,"
            f"现在配置的是 {model}({dim} 维)。\n"
            "同维度不同模型不会报错但结果全是垃圾,所以这里直接拒绝。重建:\n"
            "  .venv/bin/python scripts/build_index.py --rebuild"
        )
    return meta
