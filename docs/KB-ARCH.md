# 知识库架构

## 一条主线:只有源头是宝贝,其余全可重建

```
L0  videos.raw_z      2554 条完整原始响应         ← 唯一不可再生
     │                 重采要冒 403,媒体地址会过期
     │  纯 Python,零网络请求
L1  fragments         5153 段等粒度文本            ← 可重建(秒级)
     │  bge-m3
L2  frag_vec          5153 条 1024 维向量          ← 可重建(53 秒)
     │  无状态查询
L3  search            召回 + 三档 + 折叠            ← 无状态
     │  无状态调用
L4  answer            组上下文 + LLM + 出处          ← 无状态
```

**这个层级关系决定了几乎所有设计取舍。** 因为 L1–L4 全可重建:

- 分块策略随时改 → 重跑 `build_fragments.py`
- 换嵌入模型随时换 → 重跑 `index.py`,53 秒
- 阈值、折叠、提示词随时调 → 根本不用重建

反过来,**L0 一个字段都不能丢**。这就是为什么 raw 压缩不剪枝。

---

## 模块与依赖方向

```
backend/knowledge/
  fragments.py    已有   散字段 → 片段     纯函数,零依赖,不碰库
  embed.py        新     文本 ↔ 向量        只认字符串和数组,不碰库
  index.py        新     建/同步向量索引    唯一碰 sqlite-vec 的地方
  search.py       新     召回 + 三档 + 折叠  只读
  answer.py       新     组上下文 + LLM     只读 + 外部调用
```

依赖单向,和主项目一样的纪律:

```
fragments.py  →  无
embed.py      →  无(可插拔:本地 bge-m3 / 云 DashScope)
index.py      →  db.store + embed
search.py     →  db.store + embed
answer.py     →  search + 外部 LLM
入口三件      →  knowledge.*
```

**`knowledge/` 不认识 `collector/`。** 知识库和采集完全解耦 ——
知识库这一侧永远不会碰抖音接口,所以没有 403 风险,可以随便重跑。
这不是巧合,是刻意的边界。

---

## 表结构

```sql
-- 已有
fragments(aweme_id, idx, kind, start_sec, text, n_chars, built_at)

-- 新:向量。辅助列(+ 前缀)实测可用,所以 aweme_id/kind/start_sec
-- 直接存在向量表里,召回时不用回表 join。
CREATE VIRTUAL TABLE frag_vec USING vec0(
    frag_id   INTEGER PRIMARY KEY,   -- 指向 fragments 的 rowid
    emb       float[1024],
    +aweme_id TEXT,
    +kind     TEXT,
    +start_sec INTEGER,
    +n_chars  INTEGER
);

-- 新:索引指纹。**这张表是防静默出错的关键**
CREATE TABLE vec_meta(
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    model     TEXT NOT NULL,      -- 'BAAI/bge-m3'
    dim       INTEGER NOT NULL,   -- 1024
    n_vectors INTEGER NOT NULL,
    built_at  TEXT NOT NULL
);
```

### 为什么必须记模型名

维度不符时 sqlite-vec 会报错(实测:`Dimension mismatch for query vector`)。
但**同维度不同模型不会报错** —— bge-m3 和 Qwen3-Embedding-0.6B 都是 1024 维,
换了模型不重建索引,查询照样跑,返回的全是垃圾而且看不出来。

所以 `search` 启动时先比对 `vec_meta.model` 和当前配置,不符就**拒绝检索并提示重建**,
不猜、不降级。

这条教训是现成的:之前 `CREATE INDEX IF NOT EXISTS` 只看名字,
把索引从旧列改到新列时老索引静默留着,直到删列时才炸出来 ——
**同名不同义是最难查的一类 bug**。

---

## 查询链路

```
问题
 │
 ├─ embed(query)                          1 次前向,~30ms
 │
 ├─ sqlite-vec KNN  k = 需要数 × 4        多取,因为折叠会合并
 │      WITH hits AS MATERIALIZED (...)
 │      GROUP BY aweme_id                 ← 折叠在 SQL 里做
 │
 │   ⚠️ vec0 的 KNN 查询不允许除 distance 之外的 ORDER BY /
 │      GROUP BY。必须用 `AS MATERIALIZED` 强制物化 CTE 才能在外层聚合 ——
 │      不加就报 "Only a single 'ORDER BY distance' clause is allowed"。
 │
 ├─ 三档分流
 │      ≥ 0.62      相关
 │      0.52–0.62   可能相关,标出分数
 │      < 0.52      丢弃;全丢弃就明说「库里没有」
 │
 └─ (问答时) 组上下文 → LLM → 校验每条引用都能对回 aweme_id + start_sec
```

折叠规则:同一条作品命中多段时,只出**最高分那段**,其余记成
「这条视频还有 N 段命中」。不删数据,折叠是展示层的事。

---

## 增量:新采的作品怎么进索引

采集时 `_persist_page` 已经就地生成片段(这是复查时补的 ——
原来只在脚本里生成,新采的作品会静默没有片段)。

但**向量不在采集流程里做**,理由很硬:bge-m3 是 2.2 GB 模型,
把它加载进采集进程等于让每次采集多花几十秒 + 几 G 内存,而采集本身已经
被 403 和翻页间隔拖得很慢了。

所以走「标脏 + 批量补」:

```
index.py sync     找出 fragments 里还没进 frag_vec 的(或 built_at 更新的)
                  只嵌这些,不全量重建
index.py rebuild  全量重建(换模型、改分块策略时用),53 秒
```

判据是 `fragments.rowid` 不在 `frag_vec` 里,或 `fragments.built_at > vec_meta.built_at`。

---

## 和现有页面的关系:两套检索并存,不合并

```
/api/videos?q=xxx      LIKE 子串     已有,不动
                       「MCP」「Claude Code」——字面出现就找得到

/api/search?q=xxx      向量语义      新增
                       「怎么练口语」「为什么会拖延」——换句话说也找得到
```

**故意不合并成一个接口。** 两者回答的是不同问题,合在一起之后
「这条是怎么被找到的」就说不清了 —— 出问题时没法定位是哪一路的锅。
界面上可以并排给,但后端保持两条独立链路。

---

## 三个入口

和主项目一样,三个入口共用同一套 `knowledge.*`:

```
网页    GET  /api/search        语义搜
        POST /api/ask           问答(带出处)
命令行  cli.py ask "问题"        挂定时任务 / 脚本里用
MCP     search_library          让 AI 自己检索
        ask_library             让 AI 拿着出处回答
```

采集有跨进程锁,而**知识库这一侧不需要锁** —— 全是本地只读计算,
没有外部配额、没有风控。唯一的写操作是 `index.py`,它幂等且可中断重跑。
