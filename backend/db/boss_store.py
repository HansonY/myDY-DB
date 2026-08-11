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

# 进展的单调顺序。**只有这一份** —— 原来内联在 save_interaction 里,
# 现在 map_my_status 也要用它(一次抓取可能同时看到「已投递」和「不合适」,
# 得知道哪个更靠后)。两处各写一份早晚会漂移,而漂移的表现是「进展莫名倒退」。
_STATUS_ORDER = {s: i for i, s in enumerate(
    ("unknown", "sent", "read", "replied", "interview", "offer", "rejected"))}

# 岗位自己的状态,和「我做了什么」是两件事。
JOB_OPEN, JOB_CLOSED, JOB_STATE_UNKNOWN = "open", "closed", "unknown"

# 页面上那句话 → (我做了什么, 到哪一步)。按**包含**匹配,不按相等 ——
# 平台文案会变(「已投递」/「投递过」/「已投简历」),相等匹配一改版就全废。
#
# ⚠️ 这张表是从 LLM 提取的 my_status 文本映射过来的,而那段文本是页面上给人看的。
# 所以认不出来是常态,不是异常 —— 认不出来必须留原文(见 map_my_status)。
MY_STATUS_MAP: tuple[tuple[str, str, str], ...] = (
    # (关键词, kind, status) —— 顺序即优先级,长的写在前面
    ("已投递",   "applied", "sent"),
    ("投递",     "applied", "sent"),
    ("已查看",   "applied", "read"),
    ("已读",     "applied", "read"),
    ("继续沟通", "chatted", "replied"),
    ("已沟通",   "chatted", "replied"),
    ("沟通中",   "chatted", "replied"),
    ("约面",     "applied", "interview"),
    ("面试",     "applied", "interview"),
    ("已收藏",   "saved",   "unknown"),
    ("收藏",     "saved",   "unknown"),
    ("不合适",   "applied", "rejected"),
    ("已结束",   "applied", "rejected"),
)

# 这些说的是**岗位**的状态,不是我的。写进 interactions 会让「我投了多少」凭空多一条。
JOB_STATE_WORDS = ("职位已关闭", "已关闭", "已下线", "停止招聘", "职位不存在", "招聘已结束")


def map_my_status(text: str | None) -> tuple[list[tuple[str, str]], str | None]:
    """把页面上那句话拆成「我做了什么」和「岗位现在什么状态」。

    返回 `([(kind, status), …], job_state | None)`。

    **为什么返回 list 而不是单个**:BOSS 上「已投递 + 已沟通」很常见,而
    interactions 的主键是 `(job_id, kind)` —— 两行都该存,合成一行会丢掉一半。

    **为什么两者独立判、不短路**:一个页面完全可能同时写着「已投递」和
    「职位已关闭」。命中岗位状态就 return 的话,那条投递记录就丢了。

    **认不出来返回 `([], None)`,不猜。** 调用方应退回原来的行为
    (记一条 viewed + note 留原文)。现在这套之所以还能救回来,就是因为
    原文一直留在 note 里 —— 那 2 条老数据的 note 分别是「已投递」和
    「职位已关闭」,两种语义混在一列,但没丢。
    """
    t = (text or "").strip()
    if not t:
        return [], None

    job_state = JOB_CLOSED if any(w in t for w in JOB_STATE_WORDS) else None

    # 同一个 kind 命中多次时取**最靠后**的进展(「已投递 不合适」→ rejected)。
    # 这和 save_interaction 的只升不降是同一条规则,所以共用 _STATUS_ORDER。
    best: dict[str, str] = {}
    for word, kind, status in MY_STATUS_MAP:
        if word not in t:
            continue
        cur = best.get(kind)
        if cur is None or _STATUS_ORDER.get(status, 0) > _STATUS_ORDER.get(cur, 0):
            best[kind] = status
    return sorted(best.items()), job_state

_JOB_COLUMNS = (
    "job_id", "title", "company", "company_id", "city", "district",
    "salary_text", "salary_min", "salary_max", "salary_months",
    "experience", "degree", "jd", "jd_state", "job_state", "tags",
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


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """给已存在的表补新增列。**`CREATE TABLE IF NOT EXISTS` 不会加列** ——
    这一点已经在 vec_meta 上咬过一次(schema 里那份少一列,永远补不上)。

    照抄抖音侧 `store._add_missing_columns` 的做法,包括「跑两次」:
    第一次给已存在的表补列,executescript 之后再跑一次给这一轮新建的表补。
    """
    wanted = {
        "jobs": {
            # 岗位自己的状态。以前「职位已关闭」被塞进了 interactions.note,
            # 混在「我做了什么」里 —— 那会让「我投了多少」凭空多算。
            "job_state": "TEXT NOT NULL DEFAULT 'unknown'",
        },
        # me 表实测 0 行,但**表本身已经存在** —— 所以改 CREATE TABLE 不够,
        # 还得 ALTER 补列(CREATE TABLE IF NOT EXISTS 会整段跳过)。
        "me": {
            "resume_raw": "TEXT", "parsed_json": "TEXT", "parsed_by": "TEXT",
            "edited_at": "TEXT", "years_exp": "REAL", "degree": "TEXT",
            "cities": "TEXT", "salary_floor": "INTEGER", "salary_want": "INTEGER",
            "avoid": "TEXT", "want_axes": "TEXT",
        },
    }
    for table, cols in wanted.items():
        try:
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            continue
        if not have:                      # 表还不存在,交给 executescript
            continue
        for col, decl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db() -> None:
    with connect() as conn:
        # 顺序要紧:先补列再 executescript。schema 里可能有引用新列的索引,
        # 反过来会在「索引引用了还不存在的列」上炸。
        _add_missing_columns(conn)
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        _add_missing_columns(conn)        # 再跑一次:这一轮才新建的表轮到它
        conn.commit()


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
            d["job_state"] = d.get("job_state") or JOB_STATE_UNKNOWN
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
                # ⚠️ 两条规则,缺一条就丢数据:
                #  1) 已经是 have 的,只有另一个 have 能动它。
                #     原来只挡了 unknown,而 none **也是确定值** —— 同一个岗位先从
                #     详情页抓到 JD、后来又从一个被判成 detail 的页面没抽到,
                #     就会把 have 打成 none。JD 是这个库的核心,打错了看不出来。
                #  2) unknown 不覆盖任何已确定的值(「还没看过」不是结论)。
                sets.append(
                    "jd_state=CASE "
                    "  WHEN jobs.jd_state='have' AND excluded.jd_state<>'have' "
                    "    THEN jobs.jd_state "
                    "  WHEN excluded.jd_state='unknown' THEN jobs.jd_state "
                    "  ELSE excluded.jd_state END")
            elif c == "job_state":
                # 同理:unknown 不覆盖已知。但 closed→open 要允许 ——
                # 岗位是真会重新开的,这个方向不该锁。
                sets.append("job_state=CASE WHEN excluded.job_state='unknown' "
                            "THEN jobs.job_state ELSE excluded.job_state END")
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


def get_jobs_meta(job_ids: list[str]) -> dict[str, dict[str, Any]]:
    """给检索结果补展示字段。一次 IN 查完,不逐条开连接。

    键名是**给人看的那一套**,由这里定 —— 内核只把它 update 进返回体
    (见 kb.space.MetaFetcher)。抖音那边同位置返回 title/author/url/cat/digg_count,
    岗位这边返回 title/company/url/city/salary/jd_state:字段不同是应该的,
    内核不该知道也不该翻译。
    """
    ids = [i for i in job_ids if i]
    if not ids:
        return {}
    with connect() as conn:
        rows = conn.execute(
            f"SELECT job_id, title, company, city, district, salary_text, "
            f"       jd_state, job_state, url "
            f"FROM jobs WHERE job_id IN ({','.join('?' * len(ids))})", ids).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        out[r["job_id"]] = {
            "title": r["title"] or "",
            "company": r["company"],
            "city": " · ".join(x for x in (r["city"], r["district"]) if x) or None,
            "salary": r["salary_text"],
            "url": r["url"],
            # 这两个三态一起带出去 —— 检索结果里看到「还没看到职位描述」
            # 才知道这条为什么信息少,不然会以为是岗位本身没写
            "jd_state": r["jd_state"],
            "job_state": r["job_state"],
        }
    return out


def scope_pred(scope: str, alias: str = "jobs") -> str | None:
    """检索范围的 SQL 谓词。`None` = 不过滤。

    **「什么算我投过的」只在这里定义一处。** 抖音那边散开写过一次
    (8 处裸 FROM videos 漏了过滤),而漏掉的地方不报错、只是给错数。

    ⚠️ `applied` / `saved` 目前基本是空集(interactions 只有 3 行)——
    所以 BOSS 的默认 scope 必须是 all。默认 applied 会让检索永远返回空,
    而且长得和「库里没有」一模一样。
    """
    if scope == "all":
        return None
    if scope in ("applied", "saved", "chatted", "viewed"):
        return (f"EXISTS (SELECT 1 FROM interactions i_s "
                f"WHERE i_s.job_id = {alias}.aweme_id AND i_s.kind = '{scope}')")
    if scope == "open":            # 只搜还开着的岗位
        return (f"EXISTS (SELECT 1 FROM jobs j_s "
                f"WHERE j_s.job_id = {alias}.aweme_id AND j_s.job_state <> 'closed')")
    # 认不出来就不过滤。和抖音那侧「落到 mine_pred」的兜底思路一致:
    # 检索是只读的,宁可多给也不要静默返回空集(空集会被读成「库里没有」)。
    return None


def save_interaction(job_id: str, kind: str, status: str = "unknown",
                     happened_at: str | None = None, note: str | None = None) -> None:
    """记「我和这个岗位发生过什么」。

    status 只升不降:已经是 interview 的不会被后来一次只看到「已投」的抓取
    降回 sent —— 列表页能看到的进展有限,而进展是单调的。
    """
    order = _STATUS_ORDER          # 和 map_my_status 共用同一份,见模块顶部
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


# ── 我的画像(简历 + 求职偏好)────────────────────────────────

_ME_FIELDS = ("resume", "skills", "years_exp", "degree", "cities",
              "salary_floor", "salary_want", "avoid", "want_axes")
_ME_JSON = ("skills", "cities", "avoid", "want_axes")


def get_me() -> dict[str, Any] | None:
    """取我的画像。JSON 列已经解好,调用方不用再 loads。

    没录入过返回 None —— **不返回一个空壳**。空壳会让匹配层以为
    「偏好是空的」然后放过所有硬门槛,那比报错糟:分数照样算出来,
    只是每一条都通过,看着像「全都合适」。
    """
    with connect() as conn:
        r = conn.execute("SELECT * FROM me WHERE id=1").fetchone()
    if not r:
        return None
    d = dict(r)
    for k in _ME_JSON:
        try:
            d[k] = json.loads(d[k]) if d.get(k) else []
        except ValueError:
            d[k] = []
    return d


def set_me(**fields: Any) -> dict[str, Any]:
    """写我的画像。只更新传进来的字段,没传的保持原值。

    两条硬规矩:
      · `resume_raw` **只在为空时写入**。原文永不被覆盖 —— 抽取口径以后会改,
        原文在就能重抽。想换简历就显式传 `replace_raw=True`。
      · 手改过任何**生效字段**就记 `edited_at`。之后 `parsed_json` 和生效值的差
        就是「哪些是我改的」,而那个差本身是信息(AI 抽错什么 =
        简历里哪儿写得不清楚)。
    """
    replace_raw = bool(fields.pop("replace_raw", False))
    now = _now()
    with connect() as conn:
        cur = conn.execute("SELECT * FROM me WHERE id=1").fetchone()
        old = dict(cur) if cur else {}

        d = {k: v for k, v in fields.items() if k in _ME_FIELDS
             or k in ("resume_raw", "parsed_json", "parsed_by")}
        for k in _ME_JSON:
            if k in d and isinstance(d[k], (list, tuple)):
                d[k] = json.dumps(list(d[k]), ensure_ascii=False)

        if "resume_raw" in d and old.get("resume_raw") and not replace_raw:
            d.pop("resume_raw")        # 原文已有且没说要换 → 不动

        # 生效字段被改动过就记时间(和 AI 刚抽完那一次区分开)
        touched_live = any(k in d for k in _ME_FIELDS)
        if touched_live and "parsed_json" not in d:
            d["edited_at"] = now
        d["updated_at"] = now

        if not old:
            cols = ["id"] + list(d)
            conn.execute(
                f"INSERT INTO me ({','.join(cols)}) "
                f"VALUES (1,{','.join('?' * len(d))})", list(d.values()))
        elif d:
            conn.execute(
                f"UPDATE me SET {','.join(f'{k}=?' for k in d)} WHERE id=1",
                list(d.values()))
        conn.commit()
    return get_me() or {}


# ── 匹配分析缓存 ────────────────────────────────────────────

def get_match(job_id: str, prompt_ver: str, jd_hash: str,
              resume_hash: str) -> dict[str, Any] | None:
    """取缓存。**JD 或简历变了就当没有** —— 旧结论必须失效。

    不比对 hash 的话,你会拿列表页时代抽的结论去配后来补全的 JD,
    而那个结论看起来完全正常。
    """
    with connect() as conn:
        r = conn.execute(
            "SELECT * FROM job_match WHERE job_id=? AND prompt_ver=?",
            (job_id, prompt_ver)).fetchone()
    if not r:
        return None
    if r["jd_hash"] != jd_hash or r["resume_hash"] != resume_hash:
        return None
    d = dict(r)
    try:
        d["detail"] = json.loads(d.pop("detail_json") or "{}")
    except ValueError:
        d["detail"] = {}
    return d


def save_match(job_id: str, res: dict[str, Any], facts: dict[str, Any]) -> None:
    """存一次分析。规则算的事实一起留痕 —— 两者不一致时能查出是谁变了。"""
    with connect() as conn:
        conn.execute(
            "INSERT INTO job_match (job_id, prompt_ver, model, jd_hash, resume_hash,"
            " fit, verdict, detail_json, quote_miss, computed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(job_id, prompt_ver) DO UPDATE SET "
            "  model=excluded.model, jd_hash=excluded.jd_hash,"
            "  resume_hash=excluded.resume_hash, fit=excluded.fit,"
            "  verdict=excluded.verdict, detail_json=excluded.detail_json,"
            "  quote_miss=excluded.quote_miss, computed_at=excluded.computed_at",
            (job_id, res["prompt_ver"], res["model"], res["jd_hash"],
             res["resume_hash"], res["fit"], res["verdict"],
             json.dumps({"ai": res, "facts": facts}, ensure_ascii=False, default=str),
             res["quote_miss"], _now()))
        conn.commit()


def list_matches(limit: int = 200) -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT m.job_id, m.fit, m.verdict, m.quote_miss, m.computed_at, m.model,"
            "       j.title, j.company, j.city, j.salary_text, j.jd_state, j.job_state "
            "FROM job_match m JOIN jobs j ON j.job_id = m.job_id "
            "ORDER BY m.fit DESC NULLS LAST LIMIT ?", (limit,))]


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
