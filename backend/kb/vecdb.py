"""向量表的连接与建表。唯一碰 sqlite-vec 的地方。

**为什么不在 db/store.py 里加载扩展。** sqlite-vec 是可选依赖
(requirements-search.txt)。只做采集和字面搜索的人不该因为没装它
而连库都连不上 —— 所以扩展加载留在这里,store 保持零额外依赖。

**为什么 `db` 是必填的、没有默认值。**
这一层原来写死 `settings.db_file`(抖音库)。给它加个 `db=None` 回落默认值
看着方便,但会留下一个**静默连错库**的口子,而那条失败链每一环都不报错:

    BOSS 忘了传 db → 连上 douyin.db
    → require_match **通过**(两个库都是 bge-m3/1024,指纹防不住「同模型不同库」)
    → KNN 正常返回 10 条
    → fetch_meta 拿 aweme_id 去 boss.db 查 jobs → 全 miss → {}
    → 页面显示「相关结果 10 条,分数 0.71」,标题空白但正文言之有物

看着像在工作,实际在搜别人的库。而且 index.sync 走这条路会返回
「embedded 0 / total_vectors 6235」这么一个**成功的统计**,还顺手往 douyin.db
写一次 vec_meta(值相同但 built_at 变了,你会以为有人重建过抖音索引)。

反方向反而是安全的(抖音误连 boss.db → require_match 抛 IndexMismatch,响亮)——
**危险是单向的,恰好在新加的那一侧。** 所以这里两个参数都必填:忘了传就是 TypeError。
默认值只允许存在于 `knowledge/vecdb.py` 那个遗留薄壳里,它是唯一被允许知道
`settings.db_file` 的地方。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

# 索引指纹表。**这张表是防静默出错的关键**,不是记账用的。
#
# 维度不符时 sqlite-vec 会报错(实测 `Dimension mismatch for query vector`),
# 但**同维度不同模型不会报错** —— bge-m3 和 Qwen3-Embedding-0.6B 都是
# 1024 维,换了模型不重建索引,查询照样跑、返回的全是垃圾而且看不出来。
# 所以检索前必须比对模型名,不符就拒绝,不猜不降级。
#
# ⚠️ **永远不要给这张表加列。** `CREATE TABLE IF NOT EXISTS` 不补列 ——
# BOSS 那边就是因为 schema_boss.sql 自己建了个少一列的版本,导致
# write_meta 报 `no such column: n_vectors`,而且老库怎么都修不过来。
# 要新字段就开新表,或者写显式的 ALTER 迁移。
META_SQL = """
CREATE TABLE IF NOT EXISTS vec_meta (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    model     TEXT    NOT NULL,
    dim       INTEGER NOT NULL,
    n_vectors INTEGER NOT NULL DEFAULT 0,
    built_at  TEXT    NOT NULL
);
"""


class IndexMismatch(RuntimeError):
    """索引是用别的模型建的。宁可报错,也不能拿错模型的向量算相似度。"""


class WrongDatabase(RuntimeError):
    """连上的库里没有这个知识库该有的业务表 —— 十成是连错了。"""


def connect(db: Path | str, *, expect_table: str) -> sqlite3.Connection:
    """开一个加载了 sqlite-vec 的连接,并确认没连错库。

    两个参数都必填,理由见模块文档。
    """
    try:
        import sqlite_vec
    except ImportError as e:
        raise RuntimeError(
            "缺少 sqlite-vec。装一下:\n"
            "  .venv/bin/pip install -r backend/requirements-search.txt"
        ) from e

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except AttributeError as e:
        # 某些发行版的 Python 编译时关掉了扩展加载
        conn.close()
        raise RuntimeError(
            "这个 Python 的 sqlite3 不支持加载扩展,装不了 sqlite-vec。"
            "换 python.org 或 homebrew 装的 Python 3.10–3.13。"
        ) from e

    # 连错库的防线。放在建 vec_meta **之前** —— 否则连错了还会顺手在别人库里建表。
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (expect_table,)).fetchone()
    if not row:
        conn.close()
        raise WrongDatabase(
            f"这个知识库要求 {expect_table} 表,但 {db} 里没有 —— 十成是连错库了。\n"
            f"检查一下 Space.db 指向哪儿。"
        )

    conn.execute(META_SQL)
    return conn


def create_vec_table(conn: sqlite3.Connection, dim: int) -> None:
    """建向量表。

    辅助列(`+` 前缀)实测可用,所以 aweme_id / kind / start_sec 直接存在
    向量表里 —— 召回时不用回 fragments 表 join,少一次查询。

    ⚠️ `aweme_id` 是**物理列名**,BOSS 那边沿用它存 job_id(见 Space.id_key)。
    别改名:这是 `IF NOT EXISTS`,改名不会迁移,真改就得重建抖音那 6235 条。
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


def require_match(conn: sqlite3.Connection, model: str, dim: int,
                  build_cmd: str = "scripts/build_index.py") -> dict[str, Any]:
    """检索前比对索引指纹。不符就拒绝,不猜不降级。

    `build_cmd` 是「该跑哪个脚本重建」——**必须能换**。这段文案会经
    main.py 的 409 直接给到人眼前,写死抖音的脚本名会让 BOSS 用户照着跑,
    结果跑到抖音库上去重建。默认值保持抖音原文,所以抖音那侧一字不变。
    """
    meta = read_meta(conn)
    if not meta or not meta["n_vectors"]:
        raise IndexMismatch(
            "还没有建向量索引。跑一次:\n"
            f"  .venv/bin/python {build_cmd}"
        )
    if meta["model"] != model or meta["dim"] != dim:
        raise IndexMismatch(
            f"索引是用 {meta['model']}({meta['dim']} 维)建的,"
            f"现在配置的是 {model}({dim} 维)。\n"
            "同维度不同模型不会报错但结果全是垃圾,所以这里直接拒绝。重建:\n"
            f"  .venv/bin/python {build_cmd} --rebuild"
        )
    return meta
