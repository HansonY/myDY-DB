"""SQLite 存取层。

同步实现:单机自部署场景下写入量很小,复杂度换不来收益。
异步侧(FastAPI / f2)通过 asyncio.to_thread 调用即可。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from config import settings

SCHEMA_FILE = Path(__file__).with_name("schema.sql")

# videos 表里允许写入的列(白名单,避免采集端字段变动直接打到 SQL)
_VIDEO_COLUMNS = (
    "aweme_id", "source", "collects_id", "collects_name", "aweme_type",
    "description", "nickname", "sec_user_id", "uid", "create_time",
    "video_duration", "cover", "music_title", "share_url",
    "is_prohibited", "author_deleted", "raw_json",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
            row = {k: it.get(k) for k in _VIDEO_COLUMNS}
            row["collected_at"] = now
            row["updated_at"] = now
            rows.append(row)

        cols = list(_VIDEO_COLUMNS) + ["collected_at", "updated_at"]
        placeholders = ",".join(f":{c}" for c in cols)
        # 这几列排除在 UPDATE 之外:
        #   collected_at / source / collects_* —— videos 表保留「首次发现」的信息,
        #   完整的归属关系由 video_sources 承担。否则采完点赞会把收藏的 source 冲掉。
        _keep = {"aweme_id", "collected_at", "source", "collects_id", "collects_name"}
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in _keep)
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
) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if q:
        where.append(
            "(v.description LIKE ? OR v.nickname LIKE ? OR v.music_title LIKE ?)"
        )
        params += [f"%{q}%"] * 3
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


# 排序白名单:直接拼列名进 SQL,必须限定取值
_SORTS = {
    "collected": "v.collected_at DESC, v.rowid DESC",   # 我什么时候存的
    "published": "v.create_time DESC",                  # 作品什么时候发的
    "duration": "v.video_duration DESC",
    "author": "v.nickname ASC, v.create_time DESC",
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
) -> list[dict[str, Any]]:
    """列表 + 关键词搜索 + 来源/收藏夹/作者/标签筛选 + 排序。

    关键词用 LIKE:几千条数据下瞬时返回,不值得引入 FTS5。
    语义检索留给后续的向量库。
    """
    where, params = _filters(q, source, collects_id, nickname, tag)

    sql = (
        "SELECT v.*, "
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
) -> int:
    where, params = _filters(q, source, collects_id, nickname, tag)
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


def get_video(aweme_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM videos WHERE aweme_id = ?", (aweme_id,)
        ).fetchone()
        return dict(row) if row else None


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
    return {
        "total": total,
        "by_source": by_source,
        "with_description": with_desc,
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
