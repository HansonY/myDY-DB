"""适配器类型:告诉内核「这个知识库长什么样」。

内核(`kb/`)不认识 `videos` 也不认识 `jobs`。它只知道两件事:
片段表 `fragments` 和向量表 `frag_vec`。业务侧的一切差异都从这里注入。

**为什么用 frozen dataclass + Callable 字段,不用 Protocol。**
每个耦合点天然就是「一个值」或「一个函数」,dataclass 读起来就是一张对照表 ——
两个适配器并排放着能肉眼比对。Protocol 要求各写一个类 + 实现一堆方法,
样板多,而且没有 `frozen=True` 那种「运行时改不掉」的保证。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# id 列表 → {id: 给人看的字段}。**键名由适配器定,内核只 update 进去。**
# 这是抖音返回体逐字段不变的唯一办法(前端和 MCP 都在读 title/author/url/cat/digg_count)。
# 内核不做「规范化成 title/subtitle」那种翻译 —— 那等于改返回体。
MetaFetcher = Callable[[list[str]], dict[str, dict[str, Any]]]

# (scope, 表别名) → SQL 谓词;**返回 None 表示不过滤**。
# 现在的代码用 `scope == "all"` 这个字符串同时决定「不加 WHERE」和「过取倍数用哪个」,
# 改成看 `pred is None` 之后,"all" 这个业务词就从内核里消失了。
ScopeSQL = Callable[[str, str], "str | None"]

# (编号, item) → 资料抬头那一行。抖音是「[1] 作者《标题》 第 M:SS 处(相关度 x)」,
# 岗位没有 start_sec,该换成公司/薪资。
HeadFmt = Callable[[int, dict[str, Any]], str]


@dataclass(frozen=True)
class Space:
    """一个知识库的全部业务差异。"""

    name: str
    """只用于报错文案:「boss 的索引要求 jobs 表,但 …/douyin.db 里没有」。"""

    db: Callable[[], Path]
    """⚠️ **惰性求值,不是 Path。**

    `settings.db_file` 是 property(`config.reload()` 会改它),
    `boss_store.db_file()` 每次读 `BOSS_DB_PATH` 环境变量。
    在 import 时捕获成一个 Path,等于把路径静默冻结在进程启动那一刻。
    """

    owner_table: str
    """这个库必须有的业务表。**连错库的唯一自动防线** —— 见 kb.vecdb.connect。

    为什么需要它:向量指纹(vec_meta 的 model/dim)防得住「换了模型」,
    但**防不住「同一个模型、连错了库」** —— 两个库都是 bge-m3/1024,
    require_match 会顺利通过,然后 KNN 正常返回、fetch_meta 全 miss、
    页面显示「相关结果 10 条」但标题空白。看着像在工作,实际在搜别人的库。
    """

    init_db: Callable[[], None]
    fetch_meta: MetaFetcher

    id_key: str = "aweme_id"
    """返回体里 owner id 叫什么。

    ⚠️ `frag_vec` 的**物理列名**一直是 `aweme_id`(BOSS 那边刻意沿用了它来存 job_id,
    就为了复用这一层)。别去改那个列名 —— `create_vec_table` 是
    `CREATE ... IF NOT EXISTS`,改名不会自动迁移,真改就得重建抖音那 6235 条向量。
    所以:SQL 里是 `aweme_id`,返回体里用 `id_key`。
    """

    scope_sql: ScopeSQL | None = None
    scopes: tuple[str, ...] = ("all",)
    """只给路由和 UI 用。**内核不校验 scope。**

    校验一直在**调用方**:`main.py` 的 Query 带
    `pattern="^(mine|following|all)$"`(坏值 → 422),`mcp_server.py:170`
    自己 if 一下返回 error dict。内核层则完全不校验 —— 坏 scope 会落到
    `store.scope_pred` 的 else 分支照常返回结果(实测新旧两路在坏 scope 下
    也逐字段相等)。
    把校验搬进内核会改变这个既有分工,而且两侧的错误形状不一样
    (422 vs error dict),统一不了。所以:各层各管自己那一段。
    """

    default_scope: str = "all"

    good: float | None = None
    maybe: float | None = None
    """相关度分档线。`None` = 用全局 `settings.search_good / search_maybe`。

    **为什么要能按库配。** 那两个全局值是在**抖音内容**上扫出来的(短的视频总结)。
    岗位库的分数分布不一样:JD 是长段落、满是「任职要求:1、具备扎实的…」
    这类套话,短查询对它的余弦被稀释,整体偏低。
    实测(6 个技术类查询 + 1 个「全职远程」):技术类查询最高只到 0.626,
    全落进 only_maybe;而「全职远程」能到 0.660 过线。
    **所以不是「一律过不了线」,是分档线没有按这个库的分布校准过** ——
    尺子还能用,只是刻度不是为它定的。

    而全局改一下就会同时改掉抖音的行为(那边 6235 条向量、6 个页面在用)——
    所以阈值必须跟着库走。
    ⚠️ 但**别凭感觉填**:抖音那两个值是十几组样本扫出来的,而且当初我给
    「库里没有」打的标注还错过一次。岗位库的值要等有了手标基准再扫,
    在那之前留 None(继续用抖音的尺子),结果全落在 only_maybe ——
    那是诚实的:「可能相关,你自己判断」,而不是假装分档准。
    """

    citation_fields: tuple[str, ...] = ("author", "title", "url", "at_sec")
    context_head: HeadFmt | None = None
    system_prompt: str = ""

    meta: dict[str, Any] = field(default_factory=dict)
    """给适配器放杂物的地方,内核不看。"""

    def scope_pred(self, scope: str, alias: str) -> str | None:
        """算出 WHERE 谓词。没配 scope_sql 就一律不过滤。"""
        if self.scope_sql is None:
            return None
        return self.scope_sql(scope, alias)
