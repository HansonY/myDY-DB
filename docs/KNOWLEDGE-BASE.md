# 抖音收藏 → 知识库:架构与实现

**核心决定:不做 ASR。** 把平台已经给的字段榨干,79% 的作品就能得到可用的知识
片段 —— 而且知识类分类的片段正好是最厚的。ASR 是 130 小时音频的活,等这条路
走到头再说。

---

## 一、架构:用哪些库,怎么组合

```
                    ┌──────────────────────────────────────────┐
   抖音 Web 接口 ───►│ 采集层   f2 (Johnserf-Seed/f2)            │
                    │  · 提供 ABogus 签名 —— 没有它裸请求必被拒 │
                    │  · 只用它的 handler + filter,不用下载器   │
                    └──────────────┬───────────────────────────┘
                                   │ 每页 (list, raw)
                    ┌──────────────▼───────────────────────────┐
                    │ 提取层   纯 Python,零依赖                │
                    │  collector/douyin.py                     │
                    │  · _from_raw       互动/尺寸/媒体地址     │
                    │  · _content_from_raw  ← 本轮扩写          │
                    │  · _hashtags_from_raw                     │
                    └──────────────┬───────────────────────────┘
                                   │
                    ┌──────────────▼───────────────────────────┐
                    │ 存储层   SQLite(单文件,零运维)         │
                    │  · videos.raw_z   完整响应 zlib 留档      │
                    │  · videos.<列>    这一轮要用的字段        │
                    │  · transcripts    文本层(desc/summary)   │
                    │  · extractions    结构层(章节/知识卡)   │
                    │  · fragments      知识片段  ← 新增        │
                    │  · FTS5           关键词索引 ← 新增(内置)│
                    │  · sqlite-vec     向量索引  ← 新增(扩展)│
                    └──────────────┬───────────────────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
  网页 FastAPI              命令行 cli.py              MCP mcp_server.py
  backend/main.py           (挂定时任务)               (在 Cursor/CC 里问)
        └──────────── 三个入口共用同一套 service + store ──────────┘
                     并共用跨进程采集锁(collect_runs)
```

### 库清单与选择理由

| 层 | 库 | 为什么是它 | 硬约束 |
|---|---|---|---|
| 采集 | **f2** | 唯一现成带 **ABogus 签名**的。没签名裸请求返回 `status_code=8 用户未登录`,cookie 再对也没用 | 钉死 `httpx==0.27.2` `pydantic==2.9.*`,**下游全部要让它** |
| 提取 | 无(纯 Python) | f2 的 `_to_list()` 只暴露 31 个字段,真实响应 787 个。必须自己读 `_to_raw()` | — |
| 存储 | **sqlite3**(标准库) | 2552 条对 SQLite 是极小量。单文件、零运维、自带 FTS5 | — |
| 关键词检索 | **FTS5** | 编译进 SQLite,不用装扩展。已验证可用 | — |
| 向量检索 | **sqlite-vec** | 留在同一个文件里,不引 Qdrant/Milvus。已验证本机 `enable_load_extension` 可用 | 装 `sqlite-vec` |
| 嵌入 | **bge-m3**(本地) | 中文好、不出网、重建索引不心疼。首次下模型 ~2 GB | 走 `sentence-transformers`,和 f2 的钉子无冲突 |
| 生成 | **qwen-plus**(DashScope) | 已有通道。只用在「结构化抽取」和「问答」两处,不跑全量 | 需 API key |
| 网页 | **FastAPI** + 零构建单文件前端 | 自部署场景不值得上前端工程 | — |
| AI 入口 | **mcp** `>=1.9,<1.13` | 2.x 会把 pydantic 升到 2.13,和 f2 的 `2.9.*` 冲突。**必须钉 <2** | — |

> 踩过的坑:requirements 写 `mcp>=1.2` 时我的环境解析到 2.0、干净克隆解析到
> 1.12.4,同一份代码一边能跑一边 `ModuleNotFoundError`。所以钉版本不是洁癖。

### 调用链(一次采集的完整路径)

```
cli.py smart  /  POST /api/collect/smart  /  MCP collect_smart
      │
      ├─► service.guard_single_run()      跨进程锁:库里有活着的采集就拒绝
      ├─► planner.plan_all()              每类决定 续采 / 增量 / 跳过 + 页数上限
      └─► service._run(scope, factory, …)
             │  async for rows, cursor in factory(start_cursor)
             │        └─► collector.douyin._iter_pages()
             │                 page._to_list()  ← f2 的 31 字段
             │                 page._to_raw()   ← 真实响应 787 字段,按 aweme_id 配对
             │                 _normalize() → _from_raw / _content_from_raw / _hashtags
             ├─► service._persist_page(rows)    逐页落库(中断不丢)
             │        store.upsert_videos   raw_z 压缩 + 提升列
             │        store.save_transcript  desc / summary
             │        store.save_extraction  章节
             │        store.save_hashtags    话题
             └─► store.save_cursor(...)      逐页存游标 → 断点续跑
```

**为什么逐页落库**:403、断网、Ctrl+C 都不丢数据。重采才是风控风险。

---

## 二、采集与提取:这一轮要补的字段

穷举过 raw 里所有「长度≥12 的非 URL 字符串」,共 228 个路径。能用的就下面这些,
其余全是编码参数、文件哈希、埋点 JSON。

### 之前漏用的四个

| 字段 | 覆盖 | 干什么用 |
|---|---|---|
| `chapter_list[].detail` | **25%** | 逐章节内容说明。**比 `chapter_abstract` 的 17% 还高** —— 有些条目有逐章说明但没有整段总结,之前只抓了后者 |
| `suggest_words[].words[].word` | **62%** | 「大家都在搜」,平均 5.8 个词。**真人写的查询语句**,等于平台白送的 query→文档配对,对召回极有价值 |
| `item_title` | **44%** | 干净标题,不含话题标签。比 `desc` 适合做嵌入和列表展示 |
| `related_video_extra.tags` | 100% | 官方三级分类**带置信度**(如 0.98/0.95/0.94)。可以过滤低置信误分类 |

### 已经在用的

`statistics.*` 互动数据 · `video_tag` 官方分类 · `desc` 文案 ·
`text_extra[].hashtag_name` 话题 · `chapter_abstract` 整段总结 ·
`music.play_url` / `video.play_addr` 媒体地址 · 尺寸 / 时长 / 状态

### 确认不存在的

- **平台字幕文本**。`is_subtitled` 有 50% 是 1,但抽 18 条逐字段翻完 —— raw 里
  没有字幕文本。`caption` 只是 `desc` 重复,`caption_start/end` 是话题标签在文案里
  的字符偏移。
- **任何高覆盖的完整内容描述**。228 个路径里没有。

### 实现

```
backend/collector/douyin.py
  _ai_content_from_raw() 改名 _content_from_raw(),扩成:
    summary   chapter_abstract(顶层 或 recommend_chapter_info,取到即止)
    chapters  chapter_list 或 recommend_chapter_list(含 desc/detail/points)
    queries   suggest_words 里所有 word            ← 新
    title     item_title                           ← 新
    cat1/2/3  video_tag,并带上 related_video_extra.tags 的 prob  ← 新

backend/db/schema.sql
  videos 加  item_title TEXT · cat_conf REAL(一级分类置信度)
  transcripts 已有 (aweme_id, kind) 主键 —— queries 存成 kind='queries'

scripts/reproject.py
  不用改逻辑 —— 它遍历本地 raw 重投影,新字段自动补齐,零网络请求
```

**这就是「存全量 raw」的第二次兑现**:上面四个字段都不用重采,跑一遍
`reproject.py` 就有了。

---

## 三、知识库:三层,都不需要 ASR

### L1 片段层:把散字段拼成可检索的片段

```sql
CREATE TABLE fragments (
    aweme_id   TEXT NOT NULL,
    idx        INTEGER NOT NULL,   -- 同一作品的第几段
    kind       TEXT NOT NULL,      -- overview | chapter | queries
    start_sec  INTEGER,            -- 章节段才有,用于「第 12:30 处」
    text       TEXT NOT NULL,
    PRIMARY KEY (aweme_id, idx)
);
```

拼法(每条作品产出 1~N 段):

| 段类型 | 内容 | 覆盖 |
|---|---|---|
| `overview` | `item_title` + `desc` + `chapter_abstract` + 话题 | ~98% |
| `chapter` | 每个章节一段:`desc` + `detail` + `points`,带 `start_sec` | 25% |
| `queries` | 「大家都在搜」的词拼一起 —— 专门用来提升召回 | 62% |

**实测拼出来的效果**(1783 条有完整 raw 的):

```
够用(≥150字)   744  42%
能用(60-149)   667  37%    ← 合计 79% 可用
太薄(<60)      372  21%
```

按分类看中位长度:科普 376 · 财经 356 · 科技 352 · 校园教育 158 —— **知识类正好最厚**;
随拍 80 · 时政社会 74 —— 该薄的地方薄,不影响知识库。

那 21% 太薄的,就是 ASR 唯一真正该做的目标,但**不是现在**。

### L2 检索层:FTS5 + 向量,RRF 融合

```
关键词路  FTS5 对 fragments.text 建索引 → BM25
语义路    bge-m3 嵌入 → sqlite-vec 存向量 → cosine
融合      RRF(倒数排名融合),不调权重,比线性加权稳
```

**为什么不用纯向量**:问「M5 Pro」「Supertonic」这类型号专名,向量会召回一堆
「差不多」的东西。而 `queries` 段(真人查询语句)恰好补上向量的短板 ——
它本身就是查询形态的文本。

```
backend/search/index.py    建/重建 FTS5 + 向量索引(幂等,可增量)
backend/search/hybrid.py   混合召回 + RRF
```

### L3 问答层:MCP 加一个工具,强制带出处

```
ask_library(question, k=8)
  混合召回 k 段 → 组上下文(每段带 aweme_id + start_sec)
  → qwen-plus 回答
  → {answer, citations:[{aweme_id, author, url, at_sec, quote}]}
```

出处是硬要求。知识库最大的失败模式是给出一个听着对、但查不到来源的答案。
这些内容本来就该带着「谁说的、第几分钟说的」。

网页端复用同一个接口做搜索框,不另建一套。

### 结构化抽取(可选,晚一步)

`extractions` 表和 tier 语义已经在了。对片段 ≥150 字的那 744 条用 qwen-plus 抽
分类型知识卡(教程→步骤/材料;观点→主张/论据/反方;工具→用途/替代品)。

**必须带版本闸门**:`extractions` 加 `prompt_version INTEGER`,提示词或 schema
改了就 +1,版本不符重新生成、**永不迁移旧数据**。不加这个,半年后库里混着
三代 `fields_json`,谁都不敢用。

---

## 四、落地顺序

| 阶段 | 做什么 | 产出 |
|---|---|---|
| **P0** | 回补点赞剩余 769 条 | 全库都有完整 raw(现在 70%) |
| **P1** | 扩 `_content_from_raw` 补 4 个漏用字段 + 跑 `reproject.py` | 零网络请求,直接从本地 raw 补齐 |
| **P2** | `fragments` 表 + 拼装脚本 | 79% 的作品有可检索片段 |
| **P3** | FTS5 + sqlite-vec + RRF 混合召回 | 能按意思找 |
| **P4** | MCP `ask_library`,带出处 | 能回答跨条目的问题 |
| **P5** | 结构化知识卡(744 条)+ 版本闸门 | 能回答「步骤是什么」 |

P1 之后所有步骤**都不需要再碰抖音接口** —— 没有 403 风险,可以随便重跑。

## 五、明确不做

- **ASR**:130 小时音频。等 79% 这条路走到头,只对那 21% 太薄的做。
  (前期调研留档:音轨走 `music_url` 只需 11.9 GB 而下视频要 50.3 GB;
  但 10% 用商用 BGM 的作品音轨是配乐、ASR 无意义,得按 `music_title` 里有没有
  「创作的原声」判断并回落 `play_url` + ffmpeg。M5 Pro 上 mlx-whisper 约 15× 实时。)
- **下整段视频** / **雪碧图 + 视觉模型**
- **@豆包 评论区**:要批量发评论,封号主因
- **独立向量库**:单人 2552 条,sqlite-vec 够;零运维单文件是这个项目的地基
- **任何写操作**(评论/点赞/关注)—— 这条永远不变

## 六、一个必须一起处理的问题:重复

热门内容会被反复推,2552 条里同主题重复很多。不去重的话召回 top-8 里可能
5 条讲同一件事,上下文就浪费了。

向量建好后跑一次相似度聚类(阈值 0.92 起),检索时同簇只出代表条目、其余折叠。
**不删数据** —— 和 raw 一个原则,折叠是展示层的事。
