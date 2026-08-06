"""SQLite 存取层。

同步实现:单机自部署场景下写入量很小,复杂度换不来收益。
异步侧(FastAPI / f2)通过 asyncio.to_thread 调用即可。
"""

from __future__ import annotations

import json
import os
import sqlite3
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from config import settings

SCHEMA_FILE = Path(__file__).with_name("schema.sql")

# videos 表里允许写入的列(白名单,避免采集端字段变动直接打到 SQL)。
# 注意 raw 不在这里 —— 它要压缩,单独处理,见 _pack_raw / get_raw。
_VIDEO_COLUMNS = (
    "aweme_id", "source", "collects_id", "collects_name", "aweme_type",
    "description", "nickname", "sec_user_id", "uid", "create_time",
    "video_duration", "cover", "music_title", "share_url",
    "is_prohibited", "author_deleted",
    "digg_count", "comment_count", "share_count", "collect_count",
    "video_width", "video_height", "play_url", "music_url",
    "poi_name", "mix_name",
    "is_subtitled", "is_deleted",
    "cat1", "cat2", "cat3", "cat_conf", "item_title",
    "content_state", "has_ai_summary",
)

# 内容总结的三态。这个区分是刚性的:
#   have    抖音给了视频内容总结
#   none    采到完整响应了,抖音确认没给(它只给长视频/知识类生成)
#   unknown 还没采到完整响应,不知道有没有 —— 该去补采,不是该认命
CONTENT_HAVE, CONTENT_NONE, CONTENT_UNKNOWN = "have", "none", "unknown"

# 列表/详情查询用的列。**不能用 `v.*`** —— 那会把 raw_z 也捞出来:
# 一是 17 KB × 100 条 = 1.7 MB 白传给浏览器,二是 bytes 过不了 JSON 序列化。
_SELECT_COLS = ", ".join(f"v.{c}" for c in _VIDEO_COLUMNS) + \
               ", v.collected_at, v.updated_at, v.rowid AS rowid"

# 压缩等级 9。实测 200 条真实数据:84 KB → 17.3 KB(4.9×)。
# 这里的取舍很清楚:CPU 便宜,而丢掉的字段永远拿不回来。
_RAW_LEVEL = 9


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── raw 压缩层(对上层透明)──────────────────────────────────

def _pack_raw(text: Any) -> bytes | None:
    if not text:
        return None
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, default=str)
    return zlib.compress(text.encode("utf-8"), _RAW_LEVEL)


def _unpack_raw(blob: Any) -> str | None:
    if blob is None:
        return None
    if isinstance(blob, str):      # 迁移期:老库里还是明文 raw_json
        return blob
    try:
        return zlib.decompress(blob).decode("utf-8")
    except (zlib.error, UnicodeDecodeError):
        return None


def get_raw(aweme_id: str) -> dict[str, Any] | None:
    """取一条作品的**完整原始响应**(787 个字段)。

    统一出口:优先 raw_z,老库没迁移完就回退明文 raw_json。
    想用新字段先从这里看,不必重采。
    """
    with connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
        picks = [c for c in ("raw_z", "raw_json") if c in cols]
        if not picks:
            return None
        row = conn.execute(
            f"SELECT {','.join(picks)} FROM videos WHERE aweme_id = ?", (aweme_id,)
        ).fetchone()
    if not row:
        return None
    for c in picks:
        text = _unpack_raw(row[c])
        if text:
            try:
                return json.loads(text)
            except ValueError:
                return None
    return None


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_file)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """给已存在的表补新增列(CREATE TABLE IF NOT EXISTS 不会加列)。"""
    wanted = {
        "collect_runs": {
            "pid": "INTEGER",
            "origin": "TEXT",
            "heartbeat_at": "TEXT",
            "progress": "TEXT",
        },
        "videos": {
            "raw_z": "BLOB",
            "digg_count": "INTEGER", "comment_count": "INTEGER",
            "share_count": "INTEGER", "collect_count": "INTEGER",
            "video_width": "INTEGER", "video_height": "INTEGER",
            "play_url": "TEXT", "music_url": "TEXT",
            "poi_name": "TEXT", "mix_name": "TEXT",
            "is_subtitled": "INTEGER DEFAULT 0", "is_deleted": "INTEGER DEFAULT 0",
            "cat1": "TEXT", "cat2": "TEXT", "cat3": "TEXT",
            "cat_conf": "REAL", "item_title": "TEXT",
            "content_state": "TEXT NOT NULL DEFAULT 'unknown'",
            "has_ai_summary": "INTEGER DEFAULT 0",
        },
        "collect_state": {
            "platform_total": "INTEGER",
            "platform_total_at": "TEXT",
            "exhaust_passes": "INTEGER DEFAULT 0",
            "total_source": "TEXT",
        },
    }
    for table, cols in wanted.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db() -> None:
    """建表 + 迁移(幂等)。"""
    with connect() as conn:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        _add_missing_columns(conn)
        # 从旧的 has_ai_summary 布尔推出三态(幂等,只补还是默认值的行):
        #   有总结                        → have
        #   有完整响应但没总结            → none    抖音确认没给
        #   连完整响应都还没有            → unknown 该去补采
        # digg_count 是「有完整响应」的判据:早期那 31 个字段里没有 statistics。
        conn.execute(
            "UPDATE videos SET content_state = CASE "
            "  WHEN has_ai_summary = 1        THEN 'have' "
            "  WHEN digg_count IS NOT NULL    THEN 'none' "
            "  ELSE 'unknown' END "
            "WHERE content_state IS NULL OR content_state = 'unknown'"
        )
        # 把早期只存在 videos.source 的归属关系补进 video_sources。
        # 幂等,可反复执行。
        conn.execute(
            "INSERT OR IGNORE INTO video_sources "
            "(aweme_id, source, collects_id, collects_name, collected_at) "
            "SELECT aweme_id, source, COALESCE(collects_id, ''), collects_name, collected_at "
            "FROM videos WHERE source IS NOT NULL AND TRIM(source) <> ''"
        )


# ── 作品 ────────────────────────────────────────────────────

def upsert_videos(items: Iterable[dict[str, Any]]) -> tuple[int, int]:
    """写入或更新作品。

    返回 (处理数, 新增数)。已存在的作品只刷新可变字段,不覆盖 collected_at ——
    这样「我什么时候收藏的」这个信息不会因为重复采集而丢失。
    """
    items = list(items)
    if not items:
        return 0, 0

    now = _now()
    with connect() as conn:
        existing = {
            r["aweme_id"]
            for r in conn.execute(
                f"SELECT aweme_id FROM videos WHERE aweme_id IN "
                f"({','.join('?' * len(items))})",
                [it["aweme_id"] for it in items],
            )
        }

        rows = []
        for it in items:
            row = {}
            for k in _VIDEO_COLUMNS:
                v = it.get(k)
                # SQLite 只接受标量。曾因 url_list 直接塞进来导致整页落库失败
                # (Error binding parameter: type 'list' is not supported)——
                # 一个字段的问题不该让整页数据丢掉,这里统一兜住。
                if isinstance(v, (list, dict, tuple, set)):
                    v = json.dumps(v, ensure_ascii=False, default=str)
                elif isinstance(v, bool):
                    v = int(v)
                row[k] = v
            # 完整响应压缩后入库。采集端仍然给字符串 raw_json,压缩是存储层的事。
            row["raw_z"] = _pack_raw(it.get("raw_json"))
            row["collected_at"] = now
            row["updated_at"] = now
            rows.append(row)

        cols = list(_VIDEO_COLUMNS) + ["raw_z", "collected_at", "updated_at"]
        placeholders = ",".join(f":{c}" for c in cols)
        # 这几列排除在 UPDATE 之外:
        #   collected_at / source / collects_* —— videos 表保留「首次发现」的信息,
        #   完整的归属关系由 video_sources 承担。否则采完点赞会把收藏的 source 冲掉。
        _keep = {"aweme_id", "collected_at", "source", "collects_id", "collects_name"}
        def _set(c: str) -> str:
            if c == "raw_z":
                # raw 只补不覆盖:某些路径拿不到真实响应(只有 f2 那 31 个字段),
                # 直接 excluded.raw_z 会把已经存好的完整响应清成 NULL —— 而它重采
                # 才能拿回来,媒体地址还可能已经过期。宁可保旧。
                return "raw_z=COALESCE(excluded.raw_z, videos.raw_z)"
            if c == "content_state":
                # 只升不降:unknown 是「还不知道」,不能拿它覆盖已经确定的
                # have/none。否则某一页 raw 配对失败,就把之前查清的结论抹掉,
                # 这条以后再也不会被补采。
                # have↔none 之间允许更新 —— 抖音是异步生成总结的,
                # 今天没有明天可能就有了。
                return ("content_state=CASE WHEN excluded.content_state='unknown' "
                        "THEN videos.content_state ELSE excluded.content_state END")
            return f"{c}=excluded.{c}"

        updates = ",".join(_set(c) for c in cols if c not in _keep)
        conn.executemany(
            f"INSERT INTO videos ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(aweme_id) DO UPDATE SET {updates}",
            rows,
        )

        # 归属关系:一条作品可以同时属于收藏、点赞、多个收藏夹
        conn.executemany(
            "INSERT INTO video_sources "
            "(aweme_id, source, collects_id, collects_name, collected_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(aweme_id, source, collects_id) DO NOTHING",
            [
                (
                    r["aweme_id"],
                    r["source"],
                    r.get("collects_id") or "",
                    r.get("collects_name"),
                    now,
                )
                for r in rows
            ],
        )

    return len(items), len(items) - len(existing)


def update_derived(aweme_id: str, fields: dict[str, Any]) -> None:
    """只更新「从 raw 推导出来的」那些列,不碰采集来源与首次入库时间。

    这是「存完整 raw」真正的回报:新加一个维度不用重采,遍历本地 raw
    重新投影一遍就行 —— 零网络请求、零 403 风险。见 scripts/reproject.py。
    """
    cols = [c for c in fields if c in _VIDEO_COLUMNS and c != "aweme_id"]
    if not cols:
        return
    row = {c: fields[c] for c in cols}
    row["aweme_id"] = aweme_id
    row["updated_at"] = _now()
    sets = ",".join(f"{c}=:{c}" for c in cols)
    with connect() as conn:
        conn.execute(
            f"UPDATE videos SET {sets}, updated_at=:updated_at WHERE aweme_id=:aweme_id",
            row,
        )


def iter_raw(only_full: bool = False) -> Iterable[tuple[str, dict[str, Any]]]:
    """遍历全库的完整响应。only_full=True 时跳过早期只有 31 个字段的旧数据。"""
    with connect() as conn:
        ids = [r["aweme_id"] for r in conn.execute(
            "SELECT aweme_id FROM videos WHERE raw_z IS NOT NULL ORDER BY rowid"
        )]
    for aid in ids:
        raw = get_raw(aid)
        if not raw:
            continue
        if only_full and len(raw) <= 100:   # f2 的 31 字段结构,推不出新维度
            continue
        yield aid, raw


# ── 知识片段 ────────────────────────────────────────────────

def save_fragments(aweme_id: str, frags: list[dict[str, Any]]) -> int:
    """整条替换某作品的片段。

    先删后插,而不是 upsert —— 拼装策略一变,段数和顺序都会变,
    留着旧段会混进检索结果里。片段是派生数据,重建成本为零。
    """
    with connect() as conn:
        conn.execute("DELETE FROM fragments WHERE aweme_id = ?", (aweme_id,))
        if not frags:
            return 0
        now = _now()
        conn.executemany(
            "INSERT INTO fragments (aweme_id, idx, kind, start_sec, text, n_chars, built_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(aweme_id, i, f["kind"], f.get("start_sec"), f["text"], len(f["text"]), now)
             for i, f in enumerate(frags)],
        )
    return len(frags)


def fragment_stats() -> dict[str, Any]:
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) n FROM fragments").fetchone()["n"]
        covered = conn.execute(
            "SELECT COUNT(DISTINCT aweme_id) n FROM fragments").fetchone()["n"]
        by_kind = {r["kind"]: r["n"] for r in conn.execute(
            "SELECT kind, COUNT(*) n FROM fragments GROUP BY kind ORDER BY n DESC")}
        # 按作品聚合后的可用度:一条作品的所有段加起来够不够撑起检索
        buckets = conn.execute(
            "SELECT SUM(t>=150) thick, SUM(t>=60 AND t<150) mid, SUM(t<60) thin "
            "FROM (SELECT SUM(n_chars) t FROM fragments GROUP BY aweme_id)"
        ).fetchone()
    return {
        "fragments": total, "videos_covered": covered, "by_kind": by_kind,
        "thick": buckets["thick"] or 0, "mid": buckets["mid"] or 0,
        "thin": buckets["thin"] or 0,
    }


def rebuild_tags() -> tuple[int, int]:
    """从所有文案里重抽 #话题标签,重建 tags 表。

    零成本的分类信息 —— 抖音文案几乎都带 hashtag,不调模型就能把上千条
    散装收藏变成可浏览类目。幂等,可反复执行。
    返回 (有标签的作品数, 不同标签数)。
    """
    from extractor.hashtags import extract

    with connect() as conn:
        rows = conn.execute(
            "SELECT aweme_id, description FROM videos "
            "WHERE description IS NOT NULL AND description LIKE '%#%'"
        ).fetchall()

        pairs = [
            (r["aweme_id"], t) for r in rows for t in extract(r["description"])
        ]
        conn.execute("DELETE FROM tags")
        conn.executemany(
            "INSERT OR IGNORE INTO tags (aweme_id, tag) VALUES (?, ?)", pairs
        )
        tagged = conn.execute(
            "SELECT COUNT(DISTINCT aweme_id) AS n FROM tags"
        ).fetchone()["n"]
        distinct = conn.execute(
            "SELECT COUNT(*) AS n FROM (SELECT 1 FROM tags GROUP BY LOWER(tag))"
        ).fetchone()["n"]
    return tagged, distinct


def save_hashtags(aweme_id: str, tags_: list[str]) -> None:
    """写入平台给的结构化话题。与 rebuild_tags 的正则结果共存于同一张表,
    但这些更可信 —— 直接来自 text_extra[].hashtag_name。"""
    if not tags_:
        return
    with connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO tags (aweme_id, tag) VALUES (?, ?)",
            [(aweme_id, t) for t in tags_ if t and t.strip()],
        )


def top_tags(limit: int = 40) -> list[dict[str, Any]]:
    """热门标签。按 LOWER 分组合并 AI/ai 这类大小写重复;
    展示取 MIN(tag) —— ASCII 里大写在前,缩写读起来更顺(AI 而非 ai)。
    """
    with connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT MIN(tag) AS tag, COUNT(DISTINCT aweme_id) AS n FROM tags "
                "GROUP BY LOWER(tag) ORDER BY n DESC, tag LIMIT ?",
                (limit,),
            )
        ]


def _filters(
    q: str | None,
    source: str | None,
    collects_id: str | None,
    nickname: str | None,
    tag: str | None = None,
    cat1: str | None = None,
    content: str | None = None,
) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if q:
        # 也搜平台 AI 总结 —— 那是视频真正讲了什么,比作者文案有用得多
        where.append(
            "(v.description LIKE ? OR v.nickname LIKE ? OR v.music_title LIKE ? "
            " OR EXISTS (SELECT 1 FROM transcripts t WHERE t.aweme_id = v.aweme_id "
            "            AND t.kind = 'summary' AND t.content LIKE ?))"
        )
        params += [f"%{q}%"] * 4
    if cat1:
        where.append("v.cat1 = ?")
        params.append(cat1)
    # content: have=有内容总结 · none=抖音确认没给 · unknown=还没采全,不知道
    if content in (CONTENT_HAVE, CONTENT_NONE, CONTENT_UNKNOWN):
        where.append("COALESCE(v.content_state,'unknown') = ?")
        params.append(content)
    if nickname:
        where.append("v.nickname = ?")
        params.append(nickname)
    # 来源筛选走关联表 —— 一条作品可能同属多个来源
    if source:
        where.append(
            "EXISTS (SELECT 1 FROM video_sources s "
            "WHERE s.aweme_id = v.aweme_id AND s.source = ?)"
        )
        params.append(source)
    if collects_id:
        where.append(
            "EXISTS (SELECT 1 FROM video_sources s "
            "WHERE s.aweme_id = v.aweme_id AND s.collects_id = ?)"
        )
        params.append(collects_id)
    if tag:
        # 大小写不敏感:AI 与 ai 视为同一个标签
        where.append(
            "EXISTS (SELECT 1 FROM tags t "
            "WHERE t.aweme_id = v.aweme_id AND LOWER(t.tag) = LOWER(?))"
        )
        params.append(tag)
    return where, params


# 排序白名单:直接拼列名进 SQL,必须限定取值。
# digg/collect 这几项是「我收藏的这条到底好不好」的唯一客观依据 ——
# 一千多条散装收藏,靠人翻是翻不出优质内容的。
_SORTS = {
    "collected": "v.collected_at DESC, v.rowid DESC",   # 我什么时候存的
    "published": "v.create_time DESC",                  # 作品什么时候发的
    "duration": "v.video_duration DESC",
    "author": "v.nickname ASC, v.create_time DESC",
    "digg": "COALESCE(v.digg_count, -1) DESC, v.rowid DESC",
    "collect": "COALESCE(v.collect_count, -1) DESC, v.rowid DESC",
    "comment": "COALESCE(v.comment_count, -1) DESC, v.rowid DESC",
}


def list_videos(
    q: str | None = None,
    source: str | None = None,
    limit: int = 50,
    offset: int = 0,
    collects_id: str | None = None,
    nickname: str | None = None,
    sort: str = "collected",
    tag: str | None = None,
    cat1: str | None = None,
    content: str | None = None,
) -> list[dict[str, Any]]:
    """列表 + 关键词搜索 + 来源/收藏夹/作者/标签/分类筛选 + 排序。

    关键词用 LIKE:几千条数据下瞬时返回,不值得引入 FTS5。
    语义检索留给后续的向量库。
    """
    where, params = _filters(q, source, collects_id, nickname, tag, cat1, content)

    sql = (
        f"SELECT {_SELECT_COLS}, "
        "  (SELECT content FROM transcripts t WHERE t.aweme_id = v.aweme_id "
        "    AND t.kind = 'summary') AS ai_summary, "
        "  (SELECT GROUP_CONCAT(DISTINCT s.source) FROM video_sources s "
        "    WHERE s.aweme_id = v.aweme_id) AS sources, "
        "  (SELECT GROUP_CONCAT(DISTINCT s.collects_name) FROM video_sources s "
        "    WHERE s.aweme_id = v.aweme_id AND s.collects_name IS NOT NULL) AS folders, "
        "  (SELECT GROUP_CONCAT(t.tag) FROM tags t "
        "    WHERE t.aweme_id = v.aweme_id) AS tags "
        "FROM videos v"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {_SORTS.get(sort, _SORTS['collected'])} LIMIT ? OFFSET ?"
    params += [limit, offset]

    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def count_videos(
    q: str | None = None,
    source: str | None = None,
    collects_id: str | None = None,
    nickname: str | None = None,
    tag: str | None = None,
    cat1: str | None = None,
    content: str | None = None,
) -> int:
    where, params = _filters(q, source, collects_id, nickname, tag, cat1, content)
    sql = "SELECT COUNT(*) AS n FROM videos v"
    if where:
        sql += " WHERE " + " AND ".join(where)
    with connect() as conn:
        return conn.execute(sql, params).fetchone()["n"]


def count_by_source(scope: str) -> int:
    """某一类已采到的条数(分母对照用)。"""
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(DISTINCT aweme_id) AS n FROM video_sources WHERE source = ?",
            (scope,),
        ).fetchone()["n"]


def top_authors(limit: int = 30) -> list[dict[str, Any]]:
    """按作品数排的作者榜 —— 900+ 作者时需要个入口。"""
    with connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT nickname, COUNT(*) AS n FROM videos "
                "WHERE nickname IS NOT NULL AND TRIM(nickname) <> '' "
                "GROUP BY nickname ORDER BY n DESC, nickname LIMIT ?",
                (limit,),
            )
        ]


def get_cover_url(aweme_id: str) -> str | None:
    """只取封面地址。封面代理每页要被调 100 次,不该顺带跑 get_video 那几条
    关联查询(标签/来源/章节)—— 那是详情页才需要的。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT cover FROM videos WHERE aweme_id = ?", (aweme_id,)
        ).fetchone()
    return row["cover"] if row else None


def get_video(aweme_id: str) -> dict[str, Any] | None:
    """作品详情。带上平台 AI 总结与章节大纲 —— 详情页要看的就是「视频讲了什么」。

    不含 raw:那是 17 KB 的压缩块,要看走 get_raw()。
    """
    with connect() as conn:
        row = conn.execute(
            f"SELECT {_SELECT_COLS} FROM videos v WHERE v.aweme_id = ?", (aweme_id,)
        ).fetchone()
        if not row:
            return None
        out = dict(row)
        out["ai_summary"] = None
        for r in conn.execute(
            "SELECT kind, content FROM transcripts WHERE aweme_id = ?", (aweme_id,)
        ):
            if r["kind"] == "summary":
                out["ai_summary"] = r["content"]
        ex = conn.execute(
            "SELECT fields_json FROM extractions "
            "WHERE aweme_id = ? AND category = 'chapters'", (aweme_id,)
        ).fetchone()
        out["chapters"] = None
        if ex and ex["fields_json"]:
            try:
                out["chapters"] = json.loads(ex["fields_json"])
            except ValueError:
                pass
        out["tags"] = [
            r["tag"] for r in conn.execute(
                "SELECT tag FROM tags WHERE aweme_id = ? ORDER BY tag", (aweme_id,)
            )
        ]
        out["sources"] = [
            r["source"] for r in conn.execute(
                "SELECT DISTINCT source FROM video_sources WHERE aweme_id = ?",
                (aweme_id,),
            )
        ]
        return out


def top_categories(limit: int = 30) -> list[dict[str, Any]]:
    """抖音官方一级分类。100% 覆盖、平台自己打的,比从文案抠的 #标签 权威,
    可以直接当类目主轴用。"""
    with connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT cat1 AS cat, COUNT(*) AS n FROM videos "
                "WHERE cat1 IS NOT NULL AND TRIM(cat1) <> '' "
                "GROUP BY cat1 ORDER BY n DESC, cat1 LIMIT ?",
                (limit,),
            )
        ]


def coverage() -> dict[str, Any]:
    """各字段的实际覆盖率 —— 「数据到底全不全」得能一眼看到,不能靠猜。

    此前两次把「采尽」判断错,就是因为没有可对照的分母。
    """
    with connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
        total = conn.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"]

        def n(sql: str) -> int:
            return conn.execute(f"SELECT COUNT(*) AS n FROM videos WHERE {sql}").fetchone()["n"]

        out = {
            "total": total,
            "full_raw": n("raw_z IS NOT NULL") if "raw_z" in cols else 0,
            # 还没迁移的明文老数据,迁移脚本跑完应为 0
            "legacy_raw": n("raw_json IS NOT NULL") if "raw_json" in cols else 0,
            "digg_count": n("digg_count IS NOT NULL"),
            # 内容总结三态。unknown 不是「没有」,是「还没采全,不知道」——
            # 前者该认命,后者该去补采,混在一起就没法决定下一步做什么。
            "content_have": n("content_state='have'"),
            "content_none": n("content_state='none'"),
            "content_unknown": n("COALESCE(content_state,'unknown')='unknown'"),
            "cat1": n("cat1 IS NOT NULL AND TRIM(cat1) <> ''"),
            "music_url": n("music_url IS NOT NULL AND TRIM(music_url) <> ''"),
            "ai_summary": conn.execute(
                "SELECT COUNT(*) AS n FROM transcripts WHERE kind='summary'"
            ).fetchone()["n"],
            "chapters": conn.execute(
                "SELECT COUNT(*) AS n FROM extractions WHERE category='chapters'"
            ).fetchone()["n"],
        }
    try:
        out["db_bytes"] = Path(settings.db_file).stat().st_size
    except OSError:
        out["db_bytes"] = None
    return out


def stats() -> dict[str, Any]:
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"]
        # 从关联表统计:一条作品同属多类时各计一次,所以各项之和会大于 total
        by_source = {
            r["source"]: r["n"]
            for r in conn.execute(
                "SELECT source, COUNT(DISTINCT aweme_id) AS n "
                "FROM video_sources GROUP BY source ORDER BY n DESC"
            )
        }
        with_desc = conn.execute(
            "SELECT COUNT(*) AS n FROM videos "
            "WHERE description IS NOT NULL AND TRIM(description) <> ''"
        ).fetchone()["n"]
        authors = conn.execute(
            "SELECT COUNT(DISTINCT nickname) AS n FROM videos"
        ).fetchone()["n"]
        # 内容总结三态。分开报,因为它们要的下一步动作不一样:
        #   none = 抖音确认没给 → 认命(或以后单独做 ASR)
        #   unknown = 还没采全 → 去补采
        cs = {r["s"]: r["n"] for r in conn.execute(
            "SELECT COALESCE(content_state,'unknown') AS s, COUNT(*) AS n "
            "FROM videos GROUP BY s")}
    return {
        "total": total,
        "by_source": by_source,
        "with_description": with_desc,
        "content_have": cs.get("have", 0),
        "content_none": cs.get("none", 0),
        "content_unknown": cs.get("unknown", 0),
        "authors": authors,
    }


# ── 收藏夹 ──────────────────────────────────────────────────

def upsert_collects_folders(folders: Iterable[dict[str, Any]]) -> int:
    folders = list(folders)
    if not folders:
        return 0
    now = _now()
    with connect() as conn:
        conn.executemany(
            "INSERT INTO collects_folders "
            "(collects_id, collects_name, cover, total_number, updated_at) "
            "VALUES (:collects_id, :collects_name, :cover, :total_number, :updated_at) "
            "ON CONFLICT(collects_id) DO UPDATE SET "
            "collects_name=excluded.collects_name, cover=excluded.cover, "
            "total_number=excluded.total_number, updated_at=excluded.updated_at",
            [{**f, "updated_at": now} for f in folders],
        )
    return len(folders)


def list_collects_folders() -> list[dict[str, Any]]:
    with connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM collects_folders ORDER BY collects_name"
            )
        ]


# ── 文本层 ──────────────────────────────────────────────────

def save_transcript(
    aweme_id: str, kind: str, content: str, meta: dict | None = None
) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO transcripts (aweme_id, kind, content, meta, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(aweme_id, kind) DO UPDATE SET "
            "content=excluded.content, meta=excluded.meta, created_at=excluded.created_at",
            (aweme_id, kind, content, json.dumps(meta, ensure_ascii=False) if meta else None, _now()),
        )


def save_extraction(aweme_id: str, category: str, fields: Any,
                    model: str, tier: int = 0,
                    title: str | None = None, summary: str | None = None) -> None:
    """写入结构化提取结果。tier=0 表示平台直接给的(零成本),
    1=文案、2=关键帧+视觉、3=整段视频。"""
    with connect() as conn:
        conn.execute(
            "INSERT INTO extractions "
            "(aweme_id, category, title, summary, fields_json, model, tier, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(aweme_id) DO UPDATE SET "
            "category=excluded.category, title=excluded.title, summary=excluded.summary, "
            "fields_json=excluded.fields_json, model=excluded.model, tier=excluded.tier, "
            "created_at=excluded.created_at",
            (aweme_id, category, title, summary,
             json.dumps(fields, ensure_ascii=False, default=str),
             model, tier, _now()),
        )


# ── 游标(断点续跑)─────────────────────────────────────────

def save_cursor(scope: str, max_cursor: Any) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO cursors (scope, max_cursor, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(scope) DO UPDATE SET "
            "max_cursor=excluded.max_cursor, updated_at=excluded.updated_at",
            (scope, str(max_cursor), _now()),
        )


def load_cursor(scope: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT max_cursor FROM cursors WHERE scope = ?", (scope,)
        ).fetchone()
    if not row or row["max_cursor"] in (None, "", "None"):
        return 0
    try:
        return int(row["max_cursor"])
    except (TypeError, ValueError):
        return 0


def clear_cursor(scope: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM cursors WHERE scope = ?", (scope,))


# ── 采集任务记录 ────────────────────────────────────────────

def start_run(scope: str, origin: str = "cli") -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO collect_runs (scope, started_at, status, pid, origin, heartbeat_at) "
            "VALUES (?, ?, 'running', ?, ?, ?)",
            (scope, _now(), os.getpid(), origin, _now()),
        )
        return cur.lastrowid


def beat_run(run_id: int, progress: dict[str, Any] | None = None) -> None:
    """心跳 + 进度。进度落库才能被另一个进程(界面)看到。"""
    with connect() as conn:
        conn.execute(
            "UPDATE collect_runs SET heartbeat_at=?, progress=?, fetched=?, inserted=? WHERE id=?",
            (
                _now(),
                json.dumps(progress, ensure_ascii=False) if progress else None,
                (progress or {}).get("fetched", 0),
                (progress or {}).get("inserted", 0),
                run_id,
            ),
        )


def finish_run(
    run_id: int, status: str, fetched: int = 0, inserted: int = 0, error: str | None = None
) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE collect_runs SET finished_at=?, status=?, fetched=?, inserted=?, error=? "
            "WHERE id=?",
            (_now(), status, fetched, inserted, error, run_id),
        )


# ── 跨进程采集锁 ────────────────────────────────────────────
# 进程内的 asyncio.Lock 拦不住另一个进程。命令行与 Web 同时采集会让两个进程
# 一起打抖音接口 —— 风控的头号诱因。所以「是否在采」必须以库为准。

_STALE_SECONDS = 150        # 心跳超过这么久没更新 → 疑似死亡
_HUNG_SECONDS = 30 * 60     # 进程还活着但这么久没心跳 → 判定卡死


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)     # 只探测,不发真信号
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True         # 存在但不属于当前用户
    except OSError:
        return False


def _age_seconds(ts: str | None) -> float:
    if not ts:
        return float("inf")
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
    except ValueError:
        return float("inf")


def active_run() -> dict[str, Any] | None:
    """返回当前真正在跑的采集,顺手把僵尸记录标掉。

    判活规则:进程还在 且 心跳没超过卡死阈值;或进程查不到但心跳很新
    (刚启动、还没来得及写 pid 的窗口)。
    """
    with connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM collect_runs WHERE status='running' ORDER BY id DESC"
            )
        ]
        alive_row = None
        for r in rows:
            age = _age_seconds(r.get("heartbeat_at") or r.get("started_at"))
            alive = _pid_alive(r.get("pid"))
            is_live = (alive and age < _HUNG_SECONDS) or (not alive and age < _STALE_SECONDS)
            if is_live and alive_row is None:
                alive_row = r
            elif not is_live:
                conn.execute(
                    "UPDATE collect_runs SET status='stale', finished_at=?, "
                    "error='进程已退出或长时间无心跳,记录被回收' WHERE id=?",
                    (_now(), r["id"]),
                )
    if alive_row and alive_row.get("progress"):
        try:
            alive_row["progress"] = json.loads(alive_row["progress"])
        except (TypeError, ValueError):
            alive_row["progress"] = None
    return alive_row


# ── 智能采集状态 ────────────────────────────────────────────

def get_state(scope: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM collect_state WHERE scope = ?", (scope,)
        ).fetchone()
    if row:
        return dict(row)
    return {
        "scope": scope, "exhausted": 0, "blocked_until": None,
        "backoff_level": 0, "consecutive_403": 0, "last_status": None,
        "last_error": None, "last_run_at": None, "total_pages": 0,
        "platform_total": None, "platform_total_at": None, "exhaust_passes": 0,
        "total_source": None,
    }


def all_states() -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM collect_state")]


def save_state(scope: str, **fields: Any) -> None:
    """只更新传入的字段,其余保持原样。"""
    cur = get_state(scope)
    cur.update(fields)
    cur["scope"] = scope
    cols = [
        "scope", "exhausted", "blocked_until", "backoff_level",
        "consecutive_403", "last_status", "last_error", "last_run_at",
        "total_pages", "platform_total", "platform_total_at", "exhaust_passes",
        "total_source",
    ]
    row = {c: cur.get(c) for c in cols}
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "scope")
    with connect() as conn:
        conn.execute(
            f"INSERT INTO collect_state ({','.join(cols)}) "
            f"VALUES ({','.join(':'+c for c in cols)}) "
            f"ON CONFLICT(scope) DO UPDATE SET {updates}",
            row,
        )


def latest_runs(limit: int = 10) -> list[dict[str, Any]]:
    with connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM collect_runs ORDER BY id DESC LIMIT ?", (limit,)
            )
        ]
