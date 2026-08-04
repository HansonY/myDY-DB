"""SQLite 存取层。

同步实现:单机自部署场景下写入量很小,复杂度换不来收益。
异步侧(FastAPI / f2)通过 asyncio.to_thread 调用即可。
"""

from __future__ import annotations

import json
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


def init_db() -> None:
    """建表(幂等)。"""
    with connect() as conn:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))


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
        # 把 collected_at 排除在 UPDATE 之外 —— 重复采集时保留首次入库时间
        updates = ",".join(
            f"{c}=excluded.{c}" for c in cols if c not in ("aweme_id", "collected_at")
        )
        conn.executemany(
            f"INSERT INTO videos ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(aweme_id) DO UPDATE SET {updates}",
            rows,
        )

    return len(items), len(items) - len(existing)


def list_videos(
    q: str | None = None,
    source: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """列表 + 关键词搜索。

    Phase 1 用 LIKE:几千条数据下瞬时返回,不值得引入 FTS5。
    语义检索在 Phase 3 由向量库承担。
    """
    where, params = [], []
    if q:
        where.append("(description LIKE ? OR nickname LIKE ? OR music_title LIKE ?)")
        params += [f"%{q}%"] * 3
    if source:
        where.append("source = ?")
        params.append(source)

    sql = "SELECT * FROM videos"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY collected_at DESC, rowid DESC LIMIT ? OFFSET ?"
    params += [limit, offset]

    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def count_videos(q: str | None = None, source: str | None = None) -> int:
    where, params = [], []
    if q:
        where.append("(description LIKE ? OR nickname LIKE ? OR music_title LIKE ?)")
        params += [f"%{q}%"] * 3
    if source:
        where.append("source = ?")
        params.append(source)

    sql = "SELECT COUNT(*) AS n FROM videos"
    if where:
        sql += " WHERE " + " AND ".join(where)
    with connect() as conn:
        return conn.execute(sql, params).fetchone()["n"]


def get_video(aweme_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM videos WHERE aweme_id = ?", (aweme_id,)
        ).fetchone()
        return dict(row) if row else None


def stats() -> dict[str, Any]:
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"]
        by_source = {
            r["source"]: r["n"]
            for r in conn.execute(
                "SELECT source, COUNT(*) AS n FROM videos GROUP BY source"
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

def start_run(scope: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO collect_runs (scope, started_at, status) VALUES (?, ?, 'running')",
            (scope, _now()),
        )
        return cur.lastrowid


def finish_run(
    run_id: int, status: str, fetched: int = 0, inserted: int = 0, error: str | None = None
) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE collect_runs SET finished_at=?, status=?, fetched=?, inserted=?, error=? "
            "WHERE id=?",
            (_now(), status, fetched, inserted, error, run_id),
        )


def latest_runs(limit: int = 10) -> list[dict[str, Any]]:
    with connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM collect_runs ORDER BY id DESC LIMIT ?", (limit,)
            )
        ]
