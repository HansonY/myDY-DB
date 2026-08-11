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
    -- 岗位自己的状态,和 interactions(我做了什么)是两件事。
    -- 「职位已关闭」以前被塞进 interactions.note,混在「我投了/我沟通了」里面 ——
    -- 那会让「我投了多少」凭空多算一条。
    job_state     TEXT NOT NULL DEFAULT 'unknown',   -- open | closed | unknown
    -- ⚠️ 不是平台给的。boss_extract 的 PROMPT 让 LLM 从页面文字里抽技能词,
    -- 所以实测混着硬技术词、领域词、软能力整句三类(「独立开发且上线经验」)。
    -- 下游算技能覆盖率时必须按类过滤,详见 boss_match.skill_kind()。
    tags          TEXT,                  -- 技能词,JSON 数组
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

-- ── 向量索引指纹:**这里不建,由 kb/vecdb.py 独占** ──────────────
--
-- 以前这儿自己建了一份,少一列 `n_vectors`。而 `CREATE TABLE IF NOT EXISTS`
-- **不补列** —— 于是 kb 那边的 write_meta 报 `no such column: n_vectors`,
-- 而且老库怎么跑 init_db 都修不过来(表已存在,建表语句直接跳过)。
--
-- 更坏的是「只删数据不删定义」这种半修:现有库看着好了,而**全新库一定复现** ——
-- boss_main 的 startup 先 bs.init_db() 把 4 列版落地,之后 vecdb 建表变 no-op。
-- 「老库好了新库还坏」比原 bug 难查得多。
--
-- 所以规矩是:**vec_meta 只有一份定义,在 kb/vecdb.py 的 META_SQL 里。**
-- 抖音的 schema.sql 从来就没有它,一直是这么办的。
-- 顺带一条:永远不要给 vec_meta 加列,理由同上。要新字段就开新表。

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
-- 录入方式:页面粘简历原文 → AI 抽成结构化 → **摊在页面上逐项可手改**。
-- 「可手改」是整套匹配可信的前提:AI 抽错了你能纠,分数才有意义。
CREATE TABLE IF NOT EXISTS me (
    id           INTEGER PRIMARY KEY CHECK (id = 1),

    -- 粘进来的原文。**永不覆盖、永不删。**
    -- 理由同 raw_z:抽取口径以后一定会改(prompt 会调、字段会加),
    -- 原文在就能重抽,原文丢了就得重新翻简历。
    resume_raw   TEXT,
    parsed_json  TEXT,                 -- AI 那一次的**原始**输出,整份留着
    parsed_by    TEXT,                 -- 模型 + prompt 版本,用来判断要不要重抽
    edited_at    TEXT,                 -- 手改过的时间

    -- 下面这些是**最终生效**的值(手改之后的)。
    -- 和 parsed_json 分开存,是为了能 diff 出「哪些是我改的」——
    -- 那个 diff 本身是信息:AI 抽错什么,说明简历里哪儿写得不清楚。
    resume       TEXT,                 -- 保留老列名。清理过的简历正文
    skills       TEXT,                 -- JSON 数组,技能词(已按 norm_skill 归一)
    years_exp    REAL,                 -- 工作年限。硬门槛要它
    degree       TEXT,                 -- 走和 jobs.degree 同一张序关系表

    -- 求职偏好。**老 schema 完全没有这几项**,而整个硬门槛层都依赖它们 ——
    -- 没有期望城市就判不了城市,没有底线薪资就判不了薪资。
    cities       TEXT,                 -- JSON 数组,期望城市
    salary_floor INTEGER,              -- 千元/月。低于这个算不合格
    salary_want  INTEGER,              -- 千元/月。理想值,只进排序不做门槛
    avoid        TEXT,                 -- JSON 数组:不接受什么(现场坐班/出差/大小周…)
    want_axes    TEXT,                 -- JSON 数组:想要什么
    -- ⚠️ avoid / want_axes 的取值必须和 boss_match.AXES 是**同一张枚举** ——
    -- 不然 JD 隐性要求那一层算出来的 clash 永远匹配不上,而且不会报错,
    -- 只是永远显示「没有冲突」。

    updated_at   TEXT
);
