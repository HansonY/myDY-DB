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

## 现在能做什么

**采集**
- 我的收藏、我的点赞、我发布的作品、指定收藏夹
- **智能采集**:自己判断续采/增量/跳过,自己处理 403 退避(见下)
- 断点无损:逐页落库 + 逐页存游标,任何时候中断都能接着来
- 只读设计:不发评论、不点赞、不关注

**浏览**
- 列表视图(读文案)/ 网格视图(扫封面)
- **话题标签分类**:从文案里抽 `#hashtag`,零 AI 成本(实测 88% 的作品自带)
- 按来源 / 作者 / 标签筛选,按存入时间 / 发布时间 / 时长 / 作者排序
- 关键词搜索;封面服务端代理 + 永久缓存(抖音 CDN 防盗链且 URL 会过期)

**路线图**:大模型分析(无文案视频的视觉理解、按类型结构化提取:菜谱 → 食材/步骤)· 语义检索与问答 · 暴露为 MCP 服务供 AI 直接查询 · 导出 Markdown / Obsidian · 扩展到 B站 / 小红书 / YouTube。

---

## 快速开始

三条命令:

```bash
git clone git@github.com:HansonY/myDY-DB.git && cd myDY-DB
./setup.sh     # 挑 Python(3.10–3.13)→ venv → 依赖 → 浏览器 → 建库
./go.sh        # 扫码登录 + 自动采集(收藏 / 点赞 / 我的作品)
./web.sh       # 工作台 → http://localhost:8000
```

`./go.sh` **可以反复跑**,而且日常更新就是它:每一步先看「是不是已经做过了」,
登录过就跳过登录;采集本身是断点续跑的,被 403 打断下次接着走。

想单步控制或看每步在干什么,见 [从 clone 到能用](#从-clone-到能用完整步骤)。

> **采得到什么**:我的作品 · 收藏 · 点赞 · 收藏夹。
> **转发采不到** —— f2 的 21 个抓取方法里没有转发接口,而且实测「我的作品」里
> `is_share_post` 全是 0,转发也没混在里面。

### 怎么登录(三种,按省事排序)

**① 扫码登录 — 最省事**

```bash
.venv/bin/pip install -r backend/requirements-login.txt && .venv/bin/python -m playwright install chromium   # 一次性,约 300MB
.venv/bin/python backend/cli.py qrlogin --keep-session
```

弹出真浏览器 → 你自己点「登录」用抖音 App 扫码 → cookie 自动写进 `.env`。
`--keep-session` 会保留登录态到 `data/browser-profile`,下次多半不用再扫。

> 这里刻意**不自动化登录流程**(不点按钮、不找二维码元素),只开页面 + 轮询会话 cookie。
> 抖音怎么改登录页都不会挂 —— 依赖选择器的实现一改版就废。

**② 从本机浏览器读 — 零新依赖**

```bash
.venv/bin/python backend/cli.py login              # 自动探测
.venv/bin/python backend/cli.py login --browser chrome
```

前提是你已在该浏览器登录过抖音。
macOS 上读 Safari 需要给终端「系统设置 → 隐私与安全性 → **完全磁盘访问权限**」。

**③ 手工填 — 兜底**

浏览器登录 `www.douyin.com` → F12 → **Network** → 刷新 → 点任一 `www.douyin.com` 请求
→ **Request Headers** → 复制 `Cookie:` 后面一整串,粘进 `.env` 的 `DOUYIN_COOKIE=`。

> ⚠️ **cookie 等同于账号控制权。**
> 它只写在你本机的 `.env`(已被 `.gitignore` 忽略),不经过任何网络。
> 不要分享、不要粘到公开场所、不要提交进 git。

---

## 架构

分层、数据模型、采集流程图,以及 8 条关键设计决策(每条都附实测依据):
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**

---

## 三种操作方式

同一套逻辑、同一个数据库、同一把**跨进程采集锁**——三个入口互相可见、互相拦截,
不会两个进程同时打抖音接口(那是风控头号诱因)。

| 入口 | 怎么启动 | 适合 |
|---|---|---|
| **网页** | `.venv/bin/python -m uvicorn main:app --app-dir backend --port 8000` → http://localhost:8000 | 浏览、搜索、点按钮采集 |
| **命令行** | `.venv/bin/python backend/cli.py <命令>` | 日常采集、挂定时任务 |
| **MCP** | `.venv/bin/python backend/mcp_server.py` | 在 Cursor / Claude Code 里用自然语言问库、驱动采集 |

### 从 clone 到能用:完整步骤

> ⚠️ **Python 必须是 3.10–3.13。**
> 不能用 3.14 —— pydantic-core 依赖的 PyO3 目前最高支持 3.13,
> 用 3.14 装依赖会倒在一大片 Rust 编译错误上,而报错信息完全看不出根因。
> 程序有版本守卫会提前拦住并给出修复命令,但建好 venv 前它拦不住 pip,
> 所以**建 venv 时就要指定版本**。

```bash
# ── 0. 拿代码 ─────────────────────────────────────────
git clone git@github.com:HansonY/myDY-DB.git && cd myDY-DB

# ── 1. 建虚拟环境(注意显式指定 3.13,别用裸 python3)──
python3.13 -m venv .venv          # macOS 没有的话:brew install python@3.13
.venv/bin/pip install -r backend/requirements.txt

# ── 2. 扫码登录所需(可选但强烈建议,约 300MB)────────
.venv/bin/pip install -r backend/requirements-login.txt
.venv/bin/python -m playwright install chromium

# ── 3. 配置文件 ───────────────────────────────────────
cp .env.example .env              # 先不用改,登录步骤会自动写入

# ── 4. 建库 ───────────────────────────────────────────
.venv/bin/python backend/cli.py init

# ── 5. 登录(必须你本人操作:会弹浏览器,用抖音 App 扫码)──
.venv/bin/python backend/cli.py qrlogin --keep-session

# ── 6. 解析自己的 sec_user_id(采点赞/我的作品需要)────
.venv/bin/python backend/cli.py whoami

# ── 7. 先探 3 条核对字段(不写库,零风险)─────────────
.venv/bin/python backend/cli.py probe

# ── 8. 开始采集 ───────────────────────────────────────
.venv/bin/python backend/cli.py smart

# ── 9. 打开网页看 ─────────────────────────────────────
.venv/bin/python -m uvicorn main:app --app-dir backend --port 8000
#    → http://localhost:8000
```

**每一步大概花多久**

| 步骤 | 耗时 | 说明 |
|---|---|---|
| 1 装依赖 | 1–3 分钟 | |
| 2 Playwright | 2–5 分钟 | 下 Chromium,约 300MB |
| 5 扫码 | 半分钟 | **只能你自己做**,AI 代不了 |
| 8 首次采集 | **可能要几轮** | 抖音风控:一轮采一部分,403 就自动退避,过后再跑 |

首次采集不会一次采完是**正常的**。抖音对往历史深处翻有限制,程序会主动收手、
403 时指数退避。隔一段时间重跑 `smart` 就会接着采,已采的一条都不会丢。

### 之后日常只有两条命令

```bash
.venv/bin/python backend/cli.py smart                                # 采(几秒到几分钟)
.venv/bin/python -m uvicorn main:app --app-dir backend --port 8000   # 看
```

不确定当前状态就跑 `.venv/bin/python backend/cli.py state`,
它会告诉你每类采了多少 / 平台共多少 / 缺什么该做什么。

### 用 Docker 的话

```bash
git clone git@github.com:HansonY/myDY-DB.git && cd myDY-DB
cp .env.example .env
docker compose up -d          # → http://localhost:8000
```

但**扫码登录仍要在宿主机做**(容器里没有可交互的浏览器)。
先在宿主机按上面第 1、2、5、6 步拿到 cookie 写进 `.env`,再起容器。

---

## MCP 服务:让 AI 直接查你的库

市面上的抖音 MCP 都是**无状态的单链接解析**(给 URL 返回无水印地址或文案)。
这个背后有库,所以能回答跨作品的问题:

> 「我收藏过的做菜视频里,关于牛排回温的说法有哪些?」
> 「我点赞的 AI 相关内容,最近三个月有哪些?」
> 「帮我看看采集完整度,顺便把缺的补上」

**接进 Claude Code**:

```bash
claude mcp add douyin-db -- /绝对路径/Douyin-DB/.venv/bin/python /绝对路径/Douyin-DB/backend/mcp_server.py
```

**接进 Cursor / Claude Desktop**(`mcpServers` 配置):

```json
{
  "mcpServers": {
    "douyin-db": {
      "command": "/绝对路径/Douyin-DB/.venv/bin/python",
      "args": ["/绝对路径/Douyin-DB/backend/mcp_server.py"]
    }
  }
}
```

**8 个工具**:

| 工具 | 作用 |
|---|---|
| `search_videos` | 按关键词 / 标签 / 作者 / 来源检索(回答「我收藏过的关于X」的主入口) |
| `library_stats` | 库概览 + 热门话题标签 |
| `collect_status` | 各类完整度「已采 / 平台总数」+ 下一步计划 + 是否有采集在跑 |
| `collect_smart` | 触发智能采集(日常唯一需要的) |
| `collect_scope` | 只采某一类,指定 resume / sync / fresh |
| `refresh_totals` | 刷新分母,或手填 |
| `rebuild_tags` | 重抽 #话题标签(纯本地) |
| `auth_status` | 环境诊断 + 缺什么该跑哪条命令 |

⚠️ **扫码登录不能由 AI 代跑**——需要真人用抖音 App 扫码,必须在本机终端执行。
`auth_status` 会告诉你该跑哪条命令。

---

## 智能采集

**日常只需要这一条命令:**

```bash
.venv/bin/python backend/cli.py smart
```

它会自己判断每个分类该做什么,并处理限流:

| 情况 | 它的行为 |
|---|---|
| 历史还没采完 | 从断点续采,**采够页数上限就主动收手** |
| 历史已采尽 | 自动切成增量同步(从最新扫,连续 3 页无新增就停) |
| 上次被限流 | 冷却期内**直接跳过**,不去试探 |
| 被 403 | 指数退避(30 分钟 → 1 → 2 → 4 → 6 小时),已采数据与游标全保留 |
| 某一类失败 | 不影响其他类 |

看它打算做什么(不发任何请求):

```bash
.venv/bin/python backend/cli.py smart --dry-run
.venv/bin/python backend/cli.py state
```

### 这些规则是实测出来的,不是猜的

- **不同接口策略不同**:收藏能一路翻到历史尽头;点赞翻不深就 403,连续三次。所以每类有独立状态和页数上限。
- **调大间隔救不了点赞**:8 秒间隔撑了 55 页(1106 条),15 秒间隔只撑了 14 页(272 条)。慢并不能换来更深。所以对策是**主动收手**——没到 403 就先停,被拒之后的冷却代价比自己少采几页高得多。
- **游标只往旧翻**:翻到底之后再用续采模式,永远发现不了新增内容,必须切成增量同步。

### 挂定时任务(每天自动采)

macOS 用 launchd,把下面存成 `~/Library/LaunchAgents/com.douyin-db.smart.plist` 后 `launchctl load` 它:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.douyin-db.smart</string>
  <key>WorkingDirectory</key><string>/path/to/Douyin-DB</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/Douyin-DB/.venv/bin/python</string>
    <string>backend/cli.py</string><string>smart</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>9</integer></dict>
  <key>StandardOutPath</key><string>/tmp/douyin-db.log</string>
  <key>StandardErrorPath</key><string>/tmp/douyin-db.log</string>
</dict></plist>
```

Linux / cron:`0 9 * * * cd /path/to/Douyin-DB && .venv/bin/python backend/cli.py smart >> /tmp/douyin-db.log 2>&1`

---

## 全部命令

```bash
.venv/bin/python backend/cli.py init                       # 建库
.venv/bin/python backend/cli.py qrlogin [--keep-session]   # 扫码登录(需 playwright)
.venv/bin/python backend/cli.py login [--browser chrome]   # 从本机浏览器读 cookie
.venv/bin/python backend/cli.py whoami                     # 解析自己的 sec_user_id
.venv/bin/python backend/cli.py probe [--max 3]            # 拉几条核对字段,不写库

.venv/bin/python backend/cli.py smart [--dry-run]          # 智能采集(推荐)
.venv/bin/python backend/cli.py state                      # 各分类采集状态
.venv/bin/python backend/cli.py sync                       # 只做增量同步

.venv/bin/python backend/cli.py favorites [--max N] [--fresh]   # 单独采收藏
.venv/bin/python backend/cli.py likes     [--max N] [--fresh]   # 单独采点赞
.venv/bin/python backend/cli.py posts     [--max N] [--fresh]   # 单独采我的作品
.venv/bin/python backend/cli.py folders                    # 列出收藏夹
.venv/bin/python backend/cli.py folder <collects_id>       # 采集指定收藏夹

.venv/bin/python backend/cli.py refill                     # 回补旧数据的完整字段
.venv/bin/python backend/cli.py stats                      # 统计 + 字段覆盖率 + 官方分类
.venv/bin/python backend/cli.py tags [--top 25]            # 重抽 #话题标签
.venv/bin/python backend/cli.py search <关键词> [--sort digg] [--cat 科技] [--summary]
.venv/bin/python backend/cli.py raw <aweme_id> [--keys]    # 看完整原始响应
```

`--fresh` 忽略游标从最新重扫;默认是从断点续采。

`refill` 用**自己的一套游标**(`cursors` 表里的 `refill:<scope>`),和续采的深挖
进度互不干扰:被 403 打断后再跑一次接着往深处走,走完全程自动清零、下轮从最新
重来(顺带刷新赞/藏数)。点赞接口尤其容易被限流 —— 实测同一天内收藏能连走 74 页,
点赞 6 页就 403,所以这一类要分多轮、隔开时间跑。
`search --summary` 只看有平台 AI 总结的(那才是「视频讲了什么」);
`--sort digg` 按赞数排,用来从上千条散装收藏里挑真正优质的。

### 维护脚本

```bash
.venv/bin/python scripts/compress_raw.py      # raw 明文 → zlib 压缩 + 回收空间(一次性)
.venv/bin/python scripts/reproject.py         # 从本地 raw 重算派生字段,零网络请求
.venv/bin/python scripts/precache_covers.py   # 抢救封面(URL 带 x-expires 会过期)
```

`reproject.py` 是「存全量 raw」的兑现方式:新加一个维度不用重采,
遍历本地完整响应重投影一遍就行。

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
**重采才是风控风险**,所以完整响应(787 个字段)一个不丢地存进 `raw_z`(zlib 压缩,
84 KB → 17 KB)。「存什么」和「用什么」是两件事:存全量,用哪些字段可以慢慢演进 ——
想加维度跑 `scripts/reproject.py` 从本地 raw 重投影,不必重新采集。

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
