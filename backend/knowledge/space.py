"""抖音这个知识库的适配器。**内核认识的全部业务差异都在这一个文件里。**

配置和另一个适配器(`knowledge/boss_space.py`)对着看:
四行关键声明(name / db / owner_table / init_db)写在一起,肉眼可比 ——
`WrongDatabase` 断言抓得住「boss space 拿到抖音库」,
但抓不住反方向(douyin.db 里 `videos` 一直在),那一侧只能靠 review。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from config import settings
from db import store
from kb.space import Space

SYSTEM = """你在回答用户关于「他自己收藏的抖音视频」的问题。

规则:
1. 只用下面给出的资料回答。资料里没有的,直说没有,不要用常识补。
2. 每个论断后面标出处编号,如 [1]、[2][3]。没有出处的话不要写。
3. 资料是视频内容的文字总结,可能不完整。信息不足就说明还缺什么。
4. 中文回答,除非用户用英文问。简洁,不写客套话。"""


def _db() -> Path:
    """⚠️ 每次现取,不在 import 时捕获。

    `settings.db_file` 是 property,而 `config.reload()` 会换掉 settings ——
    import 时捕获成一个 Path 等于把路径冻结在进程启动那一刻。
    """
    return settings.db_file


def _scope_sql(scope: str, alias: str) -> str | None:
    """`all` → 不过滤(返回 None);其余交给 store 的单一判据。

    **不要在这里自己拼 SQL。** `store.mine_pred` 是「什么算我主动选的」的
    唯一定义 —— 散开写过一次,结果 8 处裸 `FROM videos` 漏了过滤,
    而漏掉的地方不报错、只是给错数。
    """
    if scope == "all":
        return None
    return store.scope_pred(scope, alias)


def _fetch_meta(ids: list[str]) -> dict[str, dict[str, Any]]:
    """给检索结果补业务字段。

    ⚠️ **逐条调 `store.get_video()`,刻意不批量。**
    批量查能少几次往返,但那会改变取值路径,而这一步的全部要求是
    「返回体逐字段和重构前相同」。等零回归验证过了再优化 ——
    先证明没坏,再谈快。
    """
    out: dict[str, dict[str, Any]] = {}
    for aid in ids:
        v = store.get_video(aid) or {}
        out[aid] = {
            "title": v.get("item_title") or v.get("description") or "",
            "author": v.get("nickname"),
            "url": v.get("share_url"),
            "cat": " › ".join(c for c in (v.get("cat1"), v.get("cat2"),
                                          v.get("cat3")) if c) or None,
            "digg_count": v.get("digg_count"),
        }
    return out


def _context_head(i: int, it: dict[str, Any]) -> str:
    """资料抬头。格式和重构前一字不差 —— 它进 prompt,改了模型的回答就会变。"""
    head = f"[{i}] {it.get('author') or '未知作者'}《{(it.get('title') or '')[:60]}》"
    if it.get("at_sec") is not None:
        head += f" 第 {it['at_sec'] // 60}:{it['at_sec'] % 60:02d} 处"
    head += f"(相关度 {it['score']})"
    return head


SPACE = Space(
    name="douyin",
    db=_db,
    owner_table="videos",
    init_db=store.init_db,
    fetch_meta=_fetch_meta,
    id_key="aweme_id",
    scope_sql=_scope_sql,
    scopes=("mine", "following", "all"),
    # 默认必须是 mine:关注者的全量产出比我的收藏多一个数量级,
    # 不设默认就等于把「我的知识库」悄悄换成「他们的内容农场」。
    default_scope="mine",
    citation_fields=("author", "title", "url", "at_sec"),
    context_head=_context_head,
    system_prompt=SYSTEM,
    meta={"build_cmd": "scripts/build_index.py"},
)
