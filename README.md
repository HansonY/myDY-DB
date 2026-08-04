# Douyin-DB

把你抖音里**收藏了却再也没打开过**的视频,变成一个可搜索、可提问的个人知识库。

> Turn your own Douyin (TikTok China) favorites into a searchable, self-hosted personal knowledge base.
> Runs entirely on your machine — your cookie never leaves it.

**完全自部署**:数据和登录态都只在你自己的机器上,项目方不接触、也没有服务器可以接触。

---

## 它解决什么

收藏夹是个黑洞。存了几百个"以后一定看"的做菜、健身、技巧类视频,
结果既搜不到、也想不起来存过什么。

Douyin-DB 把这些视频的**文案和内容**抽出来落到本地库里,于是:

- 能**搜**了 —— "我存过的那个煎牛排的视频"
- 能**看清**了 —— 一屏扫完,不用一个个点开
- 以后能**问**了 —— Phase 3 会加语义检索与问答

---

## 现在能做什么(Phase 1)

- 采集**我的收藏**、**我的点赞**、**指定收藏夹**
- 落本地 SQLite,含文案、作者、时长、封面、原链接
- 关键词搜索(文案 / 作者 / 音乐名)
- 断点续跑:中断后重跑从游标继续,不重复采集
- Web 界面 + 命令行两种用法

**路线图**:Phase 2 用视觉模型补全无文案的视频并做结构化提取(菜谱 → 食材/步骤)· Phase 3 语义检索与问答 · Phase 4 导出 Markdown / Obsidian、扩展到 B站小红书 YouTube。

---

## 快速开始

### 方式一:Docker(推荐)

```bash
git clone git@github.com:HansonY/myDY-DB.git && cd myDY-DB
cp .env.example .env      # 然后填入 DOUYIN_COOKIE
docker compose up -d
```

打开 http://localhost:8000

### 方式二:本地 Python(3.10+)

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
cp .env.example .env      # 然后填入 DOUYIN_COOKIE

.venv/bin/python backend/cli.py init      # 建库
.venv/bin/python backend/cli.py probe     # 先拉 3 条核对(不写库)
.venv/bin/python backend/cli.py favorites # 全量采集收藏

.venv/bin/python -m uvicorn main:app --app-dir backend --port 8000
```

### 怎么拿 cookie

1. 浏览器登录 `www.douyin.com`
2. 打开开发者工具(F12)→ **Network** 面板 → 刷新页面
3. 点任意一条 `www.douyin.com` 请求 → **Request Headers**
4. 复制 `Cookie:` 后面**一整串**值,粘进 `.env` 的 `DOUYIN_COOKIE=`

> ⚠️ **cookie 等同于账号控制权。**
> 它只写在你本机的 `.env`(已被 `.gitignore` 忽略)。
> 不要分享、不要粘到公开场所、不要提交进 git。

---

## 命令行

```bash
python backend/cli.py init                 # 建库
python backend/cli.py probe [--max 3]      # 拉几条核对字段,不写库
python backend/cli.py favorites [--max N] [--fresh]   # 采集收藏
python backend/cli.py likes     [--max N] [--fresh]   # 采集点赞(需 DOUYIN_PROFILE_URL)
python backend/cli.py folders              # 列出收藏夹
python backend/cli.py folder <collects_id> # 采集指定收藏夹
python backend/cli.py stats                # 统计 + 最近采集记录
python backend/cli.py search <关键词>       # 搜索
```

`--fresh` 忽略游标从头重采;默认是续采。

---

## 架构

```
浏览器 ──► FastAPI ──► SQLite (data/douyin.db)
              │
              ├─ collector/  f2 拉列表(收藏/点赞/收藏夹) · yt-dlp 兜底
              ├─ pipeline/   [Phase 2] 字幕 → 关键帧+视觉模型 → 整段视频
              ├─ extractor/  [Phase 2] 结构化提取 + 自动打标
              └─ search/     [Phase 3] 向量检索 + 问答
```

**三档降级理解**(Phase 2 的成本控制核心):

| 档 | 触发 | 成本 |
|---|---|---|
| ① 文案直用 | 抖音自带文案够用 | 免费 |
| ② 关键帧 + 视觉模型 | 文案不足 | 低 |
| ③ 整段视频理解 | 你标记的重点视频 | 高,按需 |

---

## 设计取舍

**为什么逐页落库而不是全拉完再存**
中途被风控掐断或断网时,已采的不丢;游标随页推进,下次从断点续采。
**重采才是风控风险**,所以原始条目全量存进 `raw_json` —— 以后要补字段不必重新采集。

**为什么只读,绝不写操作**
不发评论、不点赞、不关注。批量写操作是账号被封的主要诱因。
(这也是为什么本项目**不走** "在评论区 @AI 让它总结" 那条路 —— 那需要对每个视频自动发评论。)

**为什么翻页间隔默认 8 秒**
抖音对高频请求有风控。慢是刻意的。`COLLECT_PAGE_DELAY` 可调,但不建议调小。

**为什么没有前端构建步骤**
自部署工具多一条 Node 工具链就多一道门槛。前端是一个零依赖静态页,由 FastAPI 直接托管,一个端口跑完。

---

## 限制与风险

- **f2 依赖接口逆向**,抖音改版可能导致采集失效。已设计降级链(f2 → yt-dlp → 仅存元信息),流程不会整体中断。
- **点赞列表**需要账号设置为对自己可见,且需配置 `DOUYIN_PROFILE_URL`。
- **封面图有防盗链**,列表里可能显示为空白占位块 —— 不影响数据。
- 仅供**采集你自己账号的数据**用于个人管理。请遵守平台条款与当地法律。

---

## 依赖

[f2](https://github.com/Johnserf-Seed/f2) · [yt-dlp](https://github.com/yt-dlp/yt-dlp) · FastAPI · SQLite

## License

MIT
