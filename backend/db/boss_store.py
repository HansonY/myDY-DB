"""BOSS 直聘的存取层 —— **独立数据库**。

和抖音那个库完全分开(`data/boss.db` vs `data/douyin.db`),因为实体、
分析口径、界面都不一样;混在一起会让每个查询都先要问「这行是视频还是岗位」。

共用的只有更下面那层:`fragments` 表 → 向量 → 检索/问答。
那一层本来就不认识具体业务,所以这里的 `fragments` 刻意和抖音同名同结构
(列名也沿用 `aweme_id`,这里存 job_id)—— 换个库,knowledge/ 照样能跑。

DB 路径由 `BOSS_DB_PATH` 决定,默认 `data/boss.db`。
"""

from __future__ import annotations

import json
import os
import sqlite3
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from config import ROOT

SCHEMA_FILE = Path(__file__).with_name("schema_boss.sql")

# 和抖音那边同一个等级。实测 84 KB → 17 KB(4.9×);
# 这里的取舍一样:CPU 便宜,丢掉的字段永远拿不回来。
_RAW_LEVEL = 9

# JD 三态。和抖音的 content_state 同一个道理:
#   have    抓到了职位描述正文
#   none    进过详情页,平台确认没有(极少,但存在)
#   unknown **还没进过详情页**,不知道有没有 —— 该去补,不是该认命
JD_HAVE, JD_NONE, JD_UNKNOWN = "have", "none", "unknown"

# 投递进展。unknown 是「页面上看不出来」,不是「没进展」——
# 这个区分决定了分析时该不该把它算进分母。
STATUS = ("sent", "read", "replied", "interview", "offer", "rejected", "unknown")

_JOB_COLUMNS = (
    "job_id", "title", "company", "company_id", "city", "district",
    "salary_text", "salary_min", "salary_max", "salary_months",
    "experience", "degree", "jd", "jd_state", "tags",
    "hr_name", "hr_title", "hr_active", "published_at", "url",
)


def db_file() -> Path:
    p = Path(os.environ.get("BOSS_DB_PATH", "data/boss.db"))
    if not p.is_absolute():
        p = ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def profile_dir() -> Path:
    """BOSS 的浏览器登录态目录。

    **必须和抖音分开**:同一个 Chromium 用户目录里放两站的登录态,
    彼此的 cookie/localStorage 会互相干扰,而且一边重登可能把另一边挤掉。
    """
    p = ROOT / "data" / "profiles" / "boss"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_file())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))


# ── raw 压缩(对上层透明)──────────────────────────────────

def _pack(text: Any) -> bytes | None:
    if not text:
        return None
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, default=str)
    return zlib.compress(text.encode("utf-8"), _RAW_LEVEL)


def get_raw(job_id: str) -> dict[str, Any] | None:
    """取一条岗位的完整原始响应。想用新字段先从这里看,不必重采。"""
    with connect() as conn:
        row = conn.execute("SELECT raw_z FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row or row["raw_z"] is None:
        return None
    try:
        return json.loads(zlib.decompress(row["raw_z"]).decode("utf-8"))
    except (zlib.error, UnicodeDecodeError, ValueError):
        return None


# ── 岗位 ────────────────────────────────────────────────────

def upsert_jobs(items: Iterable[dict[str, Any]]) -> tuple[int, int]:
    """写入或更新岗位。返回 (处理数, 新增数)。

    **只升不降**:已经抓到 JD 的不会被后来只有列表页信息的那次覆盖成空。
    列表页和详情页是两次不同的抓取,顺序不保证,不做这个保护就会丢数据。
    """
    items = list(items)
    if not items:
        return 0, 0
    now = _now()
    with connect() as conn:
        have = {
            r["job_id"] for r in conn.execute(
                f"SELECT job_id FROM jobs WHERE job_id IN "
                f"({','.join('?' * len(items))})", [i["job_id"] for i in items])
        }
        rows = []
        for it in items:
            d = {c: it.get(c) for c in _JOB_COLUMNS}
            d["jd_state"] = d.get("jd_state") or JD_UNKNOWN
            if isinstance(d.get("tags"), (list, tuple)):
                d["tags"] = json.dumps(list(d["tags"]), ensure_ascii=False)
            d["raw_z"] = _pack(it.get("raw"))
            d["first_seen"] = now
            d["updated_at"] = now
            rows.append(d)

        cols = list(_JOB_COLUMNS) + ["raw_z", "first_seen", "updated_at"]
        sets = []
        for c in cols:
            if c in ("job_id", "first_seen"):
                continue
            if c == "jd":
                sets.append("jd=COALESCE(excluded.jd, jobs.jd)")
            elif c == "jd_state":
                # unknown 不能覆盖已经确定的值
                sets.append("jd_state=CASE WHEN excluded.jd_state='unknown' "
                            "THEN jobs.jd_state ELSE excluded.jd_state END")
            elif c == "raw_z":
                sets.append("raw_z=COALESCE(excluded.raw_z, jobs.raw_z)")
            else:
                sets.append(f"{c}=COALESCE(excluded.{c}, jobs.{c})")
        conn.executemany(
            f"INSERT INTO jobs ({','.join(cols)}) "
            f"VALUES ({','.join(':' + c for c in cols)}) "
            f"ON CONFLICT(job_id) DO UPDATE SET {','.join(sets)}",
            rows)
        conn.commit()
    return len(items), len(items) - len(have)


def get_job(job_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        r = conn.execute(
            f"SELECT {','.join(_JOB_COLUMNS)}, first_seen, updated_at "
            f"FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    return dict(r) if r else None


def save_interaction(job_id: str, kind: str, status: str = "unknown",
                     happened_at: str | None = None, note: str | None = None) -> None:
    """记「我和这个岗位发生过什么」。

    status 只升不降:已经是 interview 的不会被后来一次只看到「已投」的抓取
    降回 sent —— 列表页能看到的进展有限,而进展是单调的。
    """
    order = {s: i for i, s in enumerate(
        ("unknown", "sent", "read", "replied", "interview", "offer", "rejected"))}
    with connect() as conn:
        cur = conn.execute(
            "SELECT status FROM interactions WHERE job_id=? AND kind=?",
            (job_id, kind)).fetchone()
        if cur and order.get(status, 0) < order.get(cur["status"], 0):
            status = cur["status"]
        conn.execute(
            "INSERT INTO interactions (job_id, kind, happened_at, status, note, collected_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(job_id, kind) DO UPDATE SET "
            "  happened_at=COALESCE(excluded.happened_at, interactions.happened_at), "
            "  status=excluded.status, "
            "  note=COALESCE(excluded.note, interactions.note)",
            (job_id, kind, happened_at, status, note, _now()))
        conn.commit()


def save_chat(job_id: str, hr_name: str | None, last_msg_at: str | None,
              snippet: str | None, unread: int = 0) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO chats (job_id, hr_name, last_msg_at, last_snippet, unread, collected_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(job_id) DO UPDATE SET "
            "  hr_name=COALESCE(excluded.hr_name, chats.hr_name), "
            "  last_msg_at=COALESCE(excluded.last_msg_at, chats.last_msg_at), "
            "  last_snippet=COALESCE(excluded.last_snippet, chats.last_snippet), "
            "  unread=excluded.unread",
            (job_id, hr_name, last_msg_at, snippet, unread, _now()))
        conn.commit()


def save_fragments(job_id: str, frags: list[dict[str, Any]]) -> int:
    """重建这条岗位的片段(先删后插)。

    ⚠️ rowid 会变,所以旧向量会变成孤儿 —— 指向不存在的片段、能被检索到
    却对不上原文。索引 sync 时必须清理(抖音那边踩过,清掉过 1663 条)。
    """
    now = _now()
    with connect() as conn:
        conn.execute("DELETE FROM fragments WHERE aweme_id=?", (job_id,))
        conn.executemany(
            "INSERT INTO fragments (aweme_id, idx, kind, start_sec, text, n_chars, built_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [(job_id, i, f.get("kind", "overview"), f.get("start_sec"),
              f["text"], len(f["text"]), now) for i, f in enumerate(frags)])
        conn.commit()
    return len(frags)


def stats() -> dict[str, Any]:
    with connect() as conn:
        j = conn.execute(
            "SELECT COUNT(*) n, SUM(jd_state='have') jd, SUM(jd_state='unknown') unk "
            "FROM jobs").fetchone()
        by_kind = {r["kind"]: r["n"] for r in conn.execute(
            "SELECT kind, COUNT(*) n FROM interactions GROUP BY kind")}
        by_status = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM interactions "
            "WHERE kind='applied' GROUP BY status")}
        frags = conn.execute("SELECT COUNT(*) n FROM fragments").fetchone()["n"]
    return {
        "jobs": j["n"] or 0,
        "jd_have": j["jd"] or 0,
        "jd_unknown": j["unk"] or 0,
        "interactions": by_kind,
        "apply_status": by_status,
        "fragments": frags,
        "db": str(db_file()),
    }
