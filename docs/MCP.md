# MCP:让第三方 AI 直接查你的库

Douyin-DB 把库暴露成一个 MCP 服务,Claude Desktop 之类的 AI 应用可以直接问
「我关注的人这几天讲了什么」「我的收藏说明我是什么人」,不用你打开网页。

**只读为主**:除了少数几个明确的采集动作,其余工具都不碰抖音接口,
所以 AI 随便查都不会有风控风险。

---

## 配置

Claude Desktop 的配置文件:

- macOS `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows `%APPDATA%\Claude\claude_desktop_config.json`

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

路径必须写绝对路径,而且 `command` 要指向项目自己的 `.venv/bin/python` ——
系统 Python 没装依赖。改完重启 Claude Desktop。

验证:

```bash
.venv/bin/python backend/mcp_server.py --selftest
```

---

## 工具清单

| 工具 | 干什么 |
|---|---|
| `search_videos` | 在我的抖音知识库里检索作品。 |
| `library_stats` | 知识库概览:总条数、来源分布、作者数、抖音官方分类、热门话题标签,以及各字段覆盖率(哪些维度还不全)。 |
| `search_library` | 语义检索我的抖音收藏 —— 换句话说也找得到(问「关于怎么练口语的内容」能命中标题里没这几个字的视频)。 |
| `daily_digest` | **我关注的人这几天讲了什么** —— 回答「最近有什么新内容」「竞品在做什么」的主入口。 |
| `about_me` | 分析「我是个什么样的人」—— 从用户主动收藏/点赞的内容里看他自己。 |
| `sync_creators` | 把用户的关注列表从抖音同步下来(只拉名单,不采作品,约 5 页很轻)。 |
| `creators` | 我关注的人 + 分类。 |
| `set_creator_role` | 给博主分类并开始每天跟他。 |
| `collect_recent` | 抓已分类博主最近 N 天的新作品。 |
| `transcribe` | 把没有内容的作品转成文字(本地 Whisper,不联网问抖音)。 |
| `ask_library` | 基于我的收藏回答问题,答案强制带出处(作者 / 链接 / 第几秒)。 |
| `video_raw` | 看一条作品的完整原始响应(抖音给的全部 787 个字段)。 |
| `collect_status` | 采集进度与下一步计划:每类「已采 / 抖音平台总数 / 完成度」,以及当前是否有采集在跑(可能来自命令行或网页)。 |
| `collect_smart` | 触发智能采集(日常唯一需要的采集动作)。 |
| `collect_scope` | 只采某一类。 |
| `refill` | 回补完整字段。 |
| `refresh_totals` | 刷新「平台总数」(完整度的分母),从抖音 self 端点取作品/点赞/收藏三个官方计数。 |
| `rebuild_tags` | 从作品文案重抽 #话题标签。 |
| `auth_status` | 登录与环境状态,以及缺什么该跑哪条命令。 |

---

## 常用问法

```
我关注的人这几天讲了什么?            → daily_digest
我的收藏说明我是个什么样的人?          → about_me
我为什么老收藏不看?                  → about_me(gap) + ask_library
我收藏过关于怎么练口语的内容吗?         → search_library
把「刘纪鹏」标成有价值博主             → set_creator_role
抓一下最近三天的新作品                → collect_recent
```

## 给 AI 的三条硬规矩(已写进工具描述)

1. **`search_library` 默认只搜用户主动收藏的**。关注博主的全量产出比收藏多一个
   数量级,擅自用 `scope=all` 凑答案等于拿别人的内容农场冒充「我的收藏」,
   而用户分辨不出来。
2. **`verdict=nothing` 就是库里没有** —— 不要用自己的知识补,直接说没有。
   `only_maybe` 是「可能相关」,不是答案。
3. **时间只到月级**。抖音不给收藏的精确时间,问「我几点刷抖音」要直说做不了,别编。
