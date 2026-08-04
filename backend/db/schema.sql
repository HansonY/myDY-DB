-- Douyin-DB schema
-- 设计原则:
--   1. raw_json 全量留档 —— 以后要补字段不必重新采集(重采才是风控风险)
--   2. 文本层与作品层分离 —— 同一作品可有 文案/字幕/ASR/视觉理解 多个来源
--   3. 游标独立存表 —— 支持断点续跑

PRAGMA journal_mode = WAL;

-- ── 作品主表 ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS videos (
    aweme_id        TEXT PRIMARY KEY,
    source          TEXT    NOT NULL,          -- collection(收藏) | like(点赞) | collects(收藏夹)
    collects_id     TEXT,                      -- source=collects 时所属收藏夹
    collects_name   TEXT,
    aweme_type      INTEGER,                   -- 0=视频, 68=图集 等
    description     TEXT,                      -- 作品文案:Phase 1 的免费信息主来源
    nickname        TEXT,
    sec_user_id     TEXT,
    uid             TEXT,
    create_time     TEXT,                      -- 作品发布时间(平台给的)
    video_duration  INTEGER,                   -- 毫秒
    cover           TEXT,
    music_title     TEXT,
    share_url       TEXT,                      -- 可直接点开的作品链接
    is_prohibited   INTEGER DEFAULT 0,
    author_deleted  INTEGER DEFAULT 0,
    raw_json        TEXT,                      -- 原始条目全量留档
    collected_at    TEXT    NOT NULL,          -- 本地入库时间
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_videos_source      ON videos(source);
CREATE INDEX IF NOT EXISTS idx_videos_collects    ON videos(collects_id);
CREATE INDEX IF NOT EXISTS idx_videos_collected   ON videos(collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_videos_nickname    ON videos(nickname);

-- ── 作品 ↔ 来源(多对多)──────────────────────────────────────
-- 同一作品可能同时出现在「收藏」「点赞」和某个收藏夹里。
-- videos.source 只是首次发现来源;真正的归属关系看这张表 ——
-- 否则采完点赞会把收藏的 source 覆盖掉,信息静默丢失。
CREATE TABLE IF NOT EXISTS video_sources (
    aweme_id      TEXT NOT NULL,
    source        TEXT NOT NULL,              -- collection 收藏 | like 点赞 | post 我的作品 | collects 收藏夹
    collects_id   TEXT NOT NULL DEFAULT '',   -- 收藏夹 id;非收藏夹来源为空串(参与主键,不能用 NULL)
    collects_name TEXT,
    collected_at  TEXT NOT NULL,
    PRIMARY KEY (aweme_id, source, collects_id),
    FOREIGN KEY (aweme_id) REFERENCES videos(aweme_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_vs_source   ON video_sources(source);
CREATE INDEX IF NOT EXISTS idx_vs_collects ON video_sources(collects_id);

-- ── 文本层(文案 / 字幕 / ASR / 视觉理解)──────────────────────
CREATE TABLE IF NOT EXISTS transcripts (
    aweme_id    TEXT NOT NULL,
    kind        TEXT NOT NULL,   -- desc | subtitle | asr | vision
    content     TEXT NOT NULL,
    meta        TEXT,            -- JSON:模型名/耗时/成本等
    created_at  TEXT NOT NULL,
    PRIMARY KEY (aweme_id, kind),
    FOREIGN KEY (aweme_id) REFERENCES videos(aweme_id) ON DELETE CASCADE
);

-- ── Phase 2:结构化提取结果 ──────────────────────────────────
CREATE TABLE IF NOT EXISTS extractions (
    aweme_id    TEXT PRIMARY KEY,
    category    TEXT,            -- recipe | tutorial | knowledge | fitness | other
    title       TEXT,            -- AI 归纳的标题(原文案常是营销话术)
    summary     TEXT,
    fields_json TEXT,            -- 按类型的结构化字段
    model       TEXT,
    tier        INTEGER,         -- 1=文案直用 2=关键帧+VL 3=整段视频
    created_at  TEXT NOT NULL,
    FOREIGN KEY (aweme_id) REFERENCES videos(aweme_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tags (
    aweme_id    TEXT NOT NULL,
    tag         TEXT NOT NULL,
    PRIMARY KEY (aweme_id, tag),
    FOREIGN KEY (aweme_id) REFERENCES videos(aweme_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

-- ── 采集游标:断点续跑 ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS cursors (
    scope       TEXT PRIMARY KEY,   -- collection | like | collects:<id>
    max_cursor  TEXT,
    updated_at  TEXT NOT NULL
);

-- ── 采集任务记录 ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS collect_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,      -- running | done | failed
    fetched     INTEGER DEFAULT 0,
    inserted    INTEGER DEFAULT 0,
    error       TEXT
);

-- ── 智能采集状态机 ──────────────────────────────────────────
-- 每个分类各自记状态。实践教训:
--   * 抖音对不同接口策略不同 —— 收藏能一路翻到底,点赞翻不深就 403(实测 3 次)
--   * 调大间隔并不能解决点赞的 403(8s 撑 55 页,15s 只撑 14 页),
--     所以要靠「主动收手 + 指数退避」,而不是猛试
--   * 游标只往历史深处翻,翻到底之后必须换成增量同步才能发现新增内容
CREATE TABLE IF NOT EXISTS collect_state (
    scope            TEXT PRIMARY KEY,   -- collection | like | post
    exhausted        INTEGER DEFAULT 0,  -- 是否已翻到历史尽头(之后只做增量)
    blocked_until    TEXT,               -- 被限流的冷却截止时间
    backoff_level    INTEGER DEFAULT 0,  -- 退避级数,成功后归零
    consecutive_403  INTEGER DEFAULT 0,
    last_status      TEXT,               -- done | failed | throttled | skipped
    last_error       TEXT,
    last_run_at      TEXT,
    total_pages      INTEGER DEFAULT 0   -- 累计翻过的页数
);

-- ── 收藏夹 ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS collects_folders (
    collects_id   TEXT PRIMARY KEY,
    collects_name TEXT,
    cover         TEXT,
    total_number  INTEGER,
    updated_at    TEXT NOT NULL
);
