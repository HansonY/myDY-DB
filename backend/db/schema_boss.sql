-- BOSS 直聘 —— **独立知识库**,独立 db 文件(data/boss.db),不和抖音混。
--
-- 为什么独立:两边的实体、分析口径、界面完全不同,混在一个库里只会让
-- 每个查询都要先问「这行是视频还是岗位」。共用的只有下面这层:
-- fragments → 向量 → 检索/问答,那一层本来就不认识具体业务。
--
-- 沿用抖音项目验证过的两条:
--   1. **raw 压缩存全量,不剪枝**。重采要重新登录+翻页,而页面结构随时会变,
--      丢掉的字段拿不回来。体积用压缩解决(实测 4.9×)。
--   2. **状态用多态不用布尔**。「没有」和「还不知道」必须分得开,
--      混成一个 0 之后,「该去补采还是该认命」谁也说不清。

PRAGMA journal_mode = WAL;

-- ── 岗位 ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,      -- BOSS 的岗位 id(encryptJobId)
    title         TEXT,
    company       TEXT,
    company_id    TEXT,
    city          TEXT,
    district      TEXT,
    -- 薪资拆成上下界 + 原文。原文必须留 —— 「15-25K·14薪」这种结构化不掉的
    -- 信息(几薪、是否面议)全在里面。
    salary_text   TEXT,
    salary_min    INTEGER,               -- 千元/月
    salary_max    INTEGER,
    salary_months INTEGER,               -- 几薪,拿不到就空
    experience    TEXT,                  -- 经验要求原文
    degree        TEXT,                  -- 学历要求原文
    -- JD 全文。**这是这个库的核心** —— 「我投的岗位反复要求什么」全靠它。
    -- 列表页一般没有,要进详情页才拿得到,所以单独一个状态位。
    jd            TEXT,
    jd_state      TEXT NOT NULL DEFAULT 'unknown',   -- have | none | unknown
    tags          TEXT,                  -- 技能标签,平台给的,JSON 数组
    hr_name       TEXT,
    hr_title      TEXT,
    hr_active     TEXT,                  -- 「刚刚活跃」这类原文
    published_at  TEXT,
    url           TEXT,
    -- 完整原始响应,zlib 压缩。理由见文件头第 1 条。
    raw_z         BLOB,
    first_seen    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_city    ON jobs(city);
CREATE INDEX IF NOT EXISTS idx_jobs_jd      ON jobs(jd_state);

-- ── 我和这个岗位发生过什么 ──────────────────────────────────
-- 一个岗位可能既被我收藏、又投递、又聊过 —— 所以是多对多,
-- 不是往 jobs 上挂几个布尔列(抖音那边 video_sources 就是这个教训)。
CREATE TABLE IF NOT EXISTS interactions (
    job_id     TEXT NOT NULL,
    kind       TEXT NOT NULL,      -- saved 收藏 | applied 投递 | chatted 沟通 | viewed 浏览
    happened_at TEXT,              -- 平台给的时间,拿不到就空
    -- 投递后的进展。**这是求职分析最有价值的一列** ——
    -- 没有它就只能分析「我投了什么」,有了才能分析「什么样的我投得中」。
    --   sent 已投 · read 已读 · replied 沟通中 · interview 面试 · offer · rejected
    --   unknown 页面上看不出来
    status     TEXT NOT NULL DEFAULT 'unknown',
    note       TEXT,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (job_id, kind),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_int_kind   ON interactions(kind);
CREATE INDEX IF NOT EXISTS idx_int_status ON interactions(status);

-- ── 沟通记录 ────────────────────────────────────────────────
-- 只存**元信息和我这侧看得到的摘要**,不做聊天内容全量归档:
-- 那里面有对方的个人信息,而分析「我投的岗位要什么技能」根本用不到。
CREATE TABLE IF NOT EXISTS chats (
    job_id       TEXT NOT NULL,
    hr_name      TEXT,
    last_msg_at  TEXT,
    last_snippet TEXT,             -- 列表页那一行摘要,不进详情抓全文
    unread       INTEGER DEFAULT 0,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (job_id),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

-- ── 知识片段:检索的最小单位 ─────────────────────────────────
-- **和抖音那边同名同结构**,因为 knowledge/ 那一层就是照这个表写的,
-- 换个库照样能用。区别只在 owner_id 指向的是 job_id 而不是 aweme_id。
--
-- 三类段的分工:
--   overview 标题+公司+城市+薪资+技能标签 —— 每条都有,兜底
--   jd       职位描述正文,长的切块 —— 「反复要求什么」靠它
--   ask      我和 HR 聊的那一行摘要 —— 量少但很具体
CREATE TABLE IF NOT EXISTS fragments (
    aweme_id   TEXT    NOT NULL,       -- 沿用列名以复用 knowledge 层;这里存 job_id
    idx        INTEGER NOT NULL,
    kind       TEXT    NOT NULL,       -- overview | jd | chat
    start_sec  INTEGER,                -- 这边用不到,留着保持结构一致
    text       TEXT    NOT NULL,
    n_chars    INTEGER NOT NULL,
    built_at   TEXT    NOT NULL,
    PRIMARY KEY (aweme_id, idx)
);

CREATE INDEX IF NOT EXISTS idx_frag_kind ON fragments(kind);

-- ── 向量索引指纹 ────────────────────────────────────────────
-- 同维度不同模型**不会报错**(bge-m3 和 Qwen3-Embedding-0.6B 都是 1024 维),
-- 换模型不重建索引会让查询照跑、结果全是垃圾且看不出来。所以记模型名。
CREATE TABLE IF NOT EXISTS vec_meta (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    built_at   TEXT NOT NULL
);

-- ── 采集记录(兼跨进程锁)────────────────────────────────────
CREATE TABLE IF NOT EXISTS collect_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scope        TEXT NOT NULL,        -- applied | saved | chats | jd
    status       TEXT NOT NULL,        -- running | done | failed | stale
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    fetched      INTEGER DEFAULT 0,
    inserted     INTEGER DEFAULT 0,
    error        TEXT,
    pid          INTEGER,
    origin       TEXT,
    heartbeat_at TEXT,
    progress     TEXT
);

-- ── 我的画像(用来和 JD 比对)────────────────────────────────
-- 只有一行。存我的简历要点,这样才能回答「我和我投的岗位差在哪」。
-- 手填或从简历粘,不去抓 —— 那是我自己的东西,没必要过平台。
CREATE TABLE IF NOT EXISTS me (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    resume     TEXT,
    skills     TEXT,                   -- JSON 数组
    updated_at TEXT
);
