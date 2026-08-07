-- Douyin-DB schema
-- 设计原则:
--   1. **「存什么」和「用什么」是两回事**。
--      存:raw_z 里是完整响应,一个字段都不丢 —— 重采要冒 403,媒体地址还带
--          x-expires 会过期,丢了就真拿不回来。体积用压缩解决,不用删字段解决。
--      用:下面那些提升出来的列只是「这一轮要用的」,以后想加字段直接从 raw
--          解析再补列,不必重采。
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
    description     TEXT,                      -- 作者写的文案。⚠️ 不是视频内容
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
    -- ⚠️ 完整响应原文,zlib 压缩后存。
    -- 早期这里叫 raw_json 且只存 f2 提取后的 31 个字段,而抖音真实响应有 787 个 ——
    -- 等于每条丢掉 750+ 字段(互动数据、视频地址、结构化话题…)。
    -- 现在一个字段都不删:明文 84 KB/条 → 压缩后 17 KB/条(实测 4.9×),
    -- 全库 209 MB → 43 MB。读写都在 store.py 里透明处理,见 store.get_raw()。
    raw_z           BLOB,

    -- ── 从 raw 提升出来的可查询字段 ────────────────────────
    -- 互动数据:判断「我收藏的这条到底好不好」的唯一客观信号
    digg_count      INTEGER,                   -- 赞
    comment_count   INTEGER,
    share_count     INTEGER,
    collect_count   INTEGER,                   -- 该作品被多少人收藏
    -- 尺寸:封面/网格的比例排版需要,之前只能靠前端猜
    video_width     INTEGER,
    video_height    INTEGER,
    -- 媒体地址(都带 x-expires,会过期 —— 想用就得尽快取)
    play_url        TEXT,                      -- 视频地址
    music_url       TEXT,                      -- 原声音轨,可直接做 ASR
    -- 其它维度
    poi_name        TEXT,                      -- 拍摄地点
    mix_name        TEXT,                      -- 所属合集/连载
    is_subtitled    INTEGER DEFAULT 0,         -- 平台标记该视频有字幕
    is_deleted      INTEGER DEFAULT 0,         -- 作品已被删除
    -- 抖音官方三级分类(如 人文社科 > 人文艺术 > 历史),实测 100% 覆盖。
    -- 比从文案抠的 #标签 权威,可直接当类目主轴。
    cat1            TEXT,
    cat2            TEXT,
    cat3            TEXT,
    -- 一级分类的置信度(来自 related_video_extra.tags.level1.prob)。
    -- 官方分类偶尔会误判,有了它就能只信高置信的那批。
    cat_conf        REAL,
    -- 干净标题,不含话题标签(item_title,覆盖 44%)。desc 里混着一堆 #xxx,
    -- 做嵌入和列表展示都不如这个。
    item_title      TEXT,
    -- 抖音**自己生成的视频内容总结**(不是文案!)存在 transcripts(kind='summary'),
    -- 章节大纲存在 extractions。这里记状态,便于筛选与判断「数据全不全」。
    --
    -- ⚠️ 必须三态,不能用布尔:
    --   have    抖音给了内容总结
    --   none    已经采到完整响应,抖音**确认没给**(它只给长视频/知识类生成)
    --   unknown 这条还没采到完整响应,**压根不知道有没有**(早期只存了 31 个字段)
    -- 用 0/1 的话后两者会被混成同一个 0 —— 实测 1484 条「确认没有」和
    -- 771 条「还不知道」全被标成 0,界面和检索根本分不出来,
    -- 于是「这条没内容」到底该去补采还是该认命,谁也说不清。
    content_state   TEXT NOT NULL DEFAULT 'unknown',
    -- 你**大致什么时候收藏/点赞它**的。抖音不直接给这个字段(raw 里逐字段搜过
    -- 两遍都没有),但**翻页游标本身就是收藏时间戳** —— 收藏是微秒、点赞和
    -- 作品是毫秒。所以采集时按页记下来,不记就永久没有(和 x-expires 同性质)。
    --
    -- ⚠️ 精度只到「页」,而一页 ~19 条却能跨很久:实测相邻两页游标间隔
    -- 收藏中位 5.3 天、点赞中位 22.4 天(最大 181 天)。
    -- 所以语义是**下界**:这条不早于 saved_at 被收藏。
    --   能做:月级/季度级的兴趣漂移
    --   做不了:几点刷的、哪几天集中囤 —— 页粒度撑不住,别拿它算
    -- saved_exact=1 的那条是每页最后一条,它的时间是精确的。
    saved_at        TEXT,
    saved_exact     INTEGER DEFAULT 0,

    collected_at    TEXT    NOT NULL,          -- 本地入库时间
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_videos_source      ON videos(source);
CREATE INDEX IF NOT EXISTS idx_videos_collects    ON videos(collects_id);
CREATE INDEX IF NOT EXISTS idx_videos_collected   ON videos(collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_videos_nickname    ON videos(nickname);
-- 新维度的筛选/排序入口
CREATE INDEX IF NOT EXISTS idx_videos_cat1        ON videos(cat1);
CREATE INDEX IF NOT EXISTS idx_videos_digg        ON videos(digg_count DESC);
CREATE INDEX IF NOT EXISTS idx_videos_summary     ON videos(content_state);

-- ── 作品 ↔ 来源(多对多)──────────────────────────────────────
-- 同一作品可能同时出现在「收藏」「点赞」和某个收藏夹里。
-- videos.source 只是首次发现来源;真正的归属关系看这张表 ——
-- 否则采完点赞会把收藏的 source 覆盖掉,信息静默丢失。
CREATE TABLE IF NOT EXISTS video_sources (
    aweme_id      TEXT NOT NULL,
    -- collection 收藏 | like 点赞 | post 我的作品 | collects 收藏夹 | following 关注者主页
    --
    -- ⚠️ `following` 和前四种有**本质区别**:前四种是「我主动选的」,
    -- following 是「爬来的别人的全部产出」。混在一起会同时坏两件事:
    --   「认识自己」的分子分母全废(拿别人的产出算我的偏好)
    --   检索被淹(问「怎么练口语」被 1093 条外教营销视频顶掉真结果)
    -- 所以判据统一走 store.mine_pred(),别在各处自己写 source 条件。
    source        TEXT NOT NULL,
    collects_id   TEXT NOT NULL DEFAULT '',   -- 收藏夹 id;非收藏夹来源为空串(参与主键,不能用 NULL)
    collects_name TEXT,
    collected_at  TEXT NOT NULL,
    PRIMARY KEY (aweme_id, source, collects_id),
    FOREIGN KEY (aweme_id) REFERENCES videos(aweme_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_vs_source   ON video_sources(source);
CREATE INDEX IF NOT EXISTS idx_vs_collects ON video_sources(collects_id);

-- ── 我关注的人 ─────────────────────────────────────────────
-- 和 videos 里的「作者」是两回事:实测关注 97 位,而收藏/点赞里出现过 2122 位作者,
-- 两者只重叠 42 位 —— 55 位关注了却一条没存过,1918 位存过一条却没关注。
--
-- 存这张表是为了能**选择性**深挖。实测这 97 位共发过 93747 条作品,全爬要
-- 10.5 小时、1.5 GB,而其中 10 位高产号(小央视频 43451 条、董路 14401 条、
-- DOU+小助手…)就占 77% —— 而我从他们那儿一条都没存过。
-- 所以要按人开关,`crawl` 就是这个开关。
CREATE TABLE IF NOT EXISTS following (
    sec_user_id   TEXT PRIMARY KEY,
    uid           TEXT,
    nickname      TEXT,
    signature     TEXT,
    avatar        TEXT,
    aweme_count   INTEGER,                  -- 他一共发了多少(决定爬他要多久)
    follower_count INTEGER,
    rank_recent   INTEGER,                  -- 在「按最近关注」列表里的位次,越小越新关注
    crawl         INTEGER NOT NULL DEFAULT 0,   -- 1=深挖他的作品
    crawl_cursor  TEXT,                     -- 深挖到哪了(断点续跑)
    crawl_done_at TEXT,                     -- 上次走完全程的时间
    crawled_n     INTEGER DEFAULT 0,        -- 已入库多少条他的作品
    synced_at     TEXT NOT NULL             -- 这行是什么时候从平台刷的
);

CREATE INDEX IF NOT EXISTS idx_following_crawl ON following(crawl);

-- ── 文本层(同一作品可有多个文本来源)───────────────────────
--   desc     作者写的文案。**不是视频内容**,常是营销话术
--   summary  抖音自己生成的视频内容总结(chapter_abstract)。这才是
--            「视频讲了什么」,零成本零风险。实测 22% 的作品有 ——
--            注意要读顶层和 recommend_chapter_info 两处,它们基本互斥
--   queries  「大家都在搜」(suggest_words)。真人写的查询语句,62% 覆盖,
--            专门用来提升检索召回
--   asr / subtitle / vision  都还没做(ASR 是 130 小时音频的活,见
--            docs/KNOWLEDGE-BASE.md 里为什么先不做)
CREATE TABLE IF NOT EXISTS transcripts (
    aweme_id    TEXT NOT NULL,
    kind        TEXT NOT NULL,   -- desc | summary | queries | subtitle | asr | vision
    content     TEXT NOT NULL,
    meta        TEXT,            -- JSON:模型名/耗时/成本等
    created_at  TEXT NOT NULL,
    PRIMARY KEY (aweme_id, kind),
    FOREIGN KEY (aweme_id) REFERENCES videos(aweme_id) ON DELETE CASCADE
);

-- ── 结构化提取结果 ─────────────────────────────────────────
-- 现在只存平台给的章节大纲(category='chapters', tier=0)。
-- 以后 AI 抽的知识卡也进这里 —— 那时要加 prompt_version 做版本闸门。
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

-- ── 知识片段:检索的最小单位 ─────────────────────────────────
-- 为什么不直接检索 videos:一条作品的可用文本散在 5 个地方(干净标题、
-- 文案、整段总结、逐章说明、搜索意图词),而且长度差两个数量级。
-- 拼成等粒度的片段之后,关键词索引和向量索引才有一致的输入。
--
-- 三类段的分工:
--   overview 标题+文案+整段总结+话题 —— 几乎每条都有,是兜底
--   chapter  每章一段,带 start_sec —— 引用时能给出「第 12:30 处」
--   queries  「大家都在搜」—— 真人写的查询语句,专门提升召回;
--            它本身就是查询形态的文本,恰好补上向量对型号/专名召回差的短板
--
-- 这张表是**派生数据**,可以随时全量重建(scripts/fragments.py),
-- 所以不怕改拼装策略 —— 源头永远是 raw_z。
CREATE TABLE IF NOT EXISTS fragments (
    aweme_id   TEXT    NOT NULL,
    idx        INTEGER NOT NULL,       -- 同一作品内的序号
    kind       TEXT    NOT NULL,       -- overview | chapter | queries
    start_sec  INTEGER,                -- 只有 chapter 段有
    text       TEXT    NOT NULL,
    n_chars    INTEGER NOT NULL,       -- 冗余存一份,筛「太薄的段」不用算
    built_at   TEXT    NOT NULL,
    PRIMARY KEY (aweme_id, idx),
    FOREIGN KEY (aweme_id) REFERENCES videos(aweme_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_frag_kind  ON fragments(kind);
CREATE INDEX IF NOT EXISTS idx_frag_chars ON fragments(n_chars DESC);

-- ── 自我分析快照 ─────────────────────────────────────────────
-- 每次分析存一份,**不是缓存,是历史**。两个理由:
--   1. AI 写的那段结论要花钱和时间,数据没变就不该重算
--   2. 更重要的:**两次快照之间的差本身就是信息**。
--      我们拿不到「你什么时候收藏的」(抖音不给,游标只存了页级),
--      但只要定期存一份快照,「上次 vs 这次」就能看出兴趣漂移和囤积节奏 ——
--      用历史绕过缺失的时间戳。
--
-- data_fp 是数据指纹(条数 + 最新 updated_at + 分类覆盖数)。
-- 指纹没变就直接返回上次结果,变了才重算。
CREATE TABLE IF NOT EXISTS insights (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    data_fp     TEXT NOT NULL,      -- 数据指纹,用来判断要不要重算
    stats_json  TEXT NOT NULL,      -- 纯计算的部分(确定性,可复现)
    narrative   TEXT,               -- AI 写的那段话(可选,要 key)
    model       TEXT,               -- 写 narrative 用的模型
    n_videos    INTEGER NOT NULL,   -- 冗余存一份,列历史时不用解 JSON
    n_classified INTEGER NOT NULL   -- 有官方分类的条数 = 分类类指标的真实分母
);

CREATE INDEX IF NOT EXISTS idx_insights_time ON insights(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_insights_fp   ON insights(data_fp);

-- ── 采集游标:断点续跑 ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS cursors (
    scope       TEXT PRIMARY KEY,   -- collection | like | collects:<id>
    max_cursor  TEXT,
    updated_at  TEXT NOT NULL
);

-- ── 采集任务记录(同时充当跨进程锁)──────────────────────────
-- 为什么锁必须落库:命令行和 Web 是两个进程。进程内的 asyncio.Lock 拦不住
-- 对方 —— 实测命令行在采时点界面按钮会直接放行,两个进程同时打抖音接口,
-- 这正是触发风控的头号原因。
-- pid + heartbeat_at 用来区分「真的在跑」和「进程崩了留下的僵尸记录」。
CREATE TABLE IF NOT EXISTS collect_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scope        TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,      -- running | done | failed | stale
    fetched      INTEGER DEFAULT 0,
    inserted     INTEGER DEFAULT 0,
    error        TEXT,
    pid          INTEGER,            -- 哪个进程在跑
    origin       TEXT,               -- cli | web,界面要能说清是谁在采
    heartbeat_at TEXT,               -- 每页更新一次
    progress     TEXT                -- JSON 进度,跨进程可见
);

CREATE INDEX IF NOT EXISTS idx_runs_status ON collect_runs(status);

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
    total_pages      INTEGER DEFAULT 0,  -- 累计翻过的页数
    -- 平台侧总数(分母)。没有它就只能靠「生成器自然结束」推断采尽,
    -- 而这个推断实测会出错:点赞被 403 打断后续采,生成器自然结束了,
    -- 但抖音说有 1876 条、我们只有 1457 —— 少了 419 条却被标成已采尽。
    platform_total   INTEGER,
    platform_total_at TEXT,
    -- 分母从哪来:api = 抖音资料接口给的;manual = 用户在 App 里看到后手填。
    -- 收藏必须手填 —— 抖音不提供「我收藏了多少条」这个字段(收藏是私密的,
    -- 资料接口 127 个字段里只有作品数 aweme_count 和点赞数 favoriting_count)。
    total_source     TEXT,
    -- 完整走完一遍且仍有缺口的次数。缺口可能是原作者删稿导致的永久差额,
    -- 所以不能无限重试 —— 连续确认两遍就接受现状。
    exhaust_passes   INTEGER DEFAULT 0
);

-- ── 收藏夹 ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS collects_folders (
    collects_id   TEXT PRIMARY KEY,
    collects_name TEXT,
    cover         TEXT,
    total_number  INTEGER,
    updated_at    TEXT NOT NULL
);
