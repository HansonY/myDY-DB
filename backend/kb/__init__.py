r"""知识库内核:片段 → 向量 → 检索 → 问答。**业务无关。**

这一层不认识 `videos`,也不认识 `jobs`。它只知道两张表:
`fragments`(片段,7 列)和 `frag_vec`(向量)。所有业务差异从
`Space` 注入(见 `kb/space.py`)。

用法:

    from kb import bind
    from knowledge.space import SPACE          # 抖音适配器
    kb = bind(SPACE)
    kb.search("英语口语")
    kb.ask("怎么练口语")
    kb.sync()
    kb.status()

**两条硬规矩,CI 化的写法在计划 §9:**

1. `grep -rn "db_file\|db_path\|settings\.db" backend/kb/` 必须零命中。
   内核不许知道任何具体库在哪 —— 那是 Space.db 的事。
2. `grep -rn "videos\|video_sources\|jobs\|store\." backend/kb/` 必须零命中。
   唯一例外是 `frag_vec` 的物理列名 `aweme_id`,它只能出现在 SQL 字符串里,
   不能出现在返回体的键名里(返回体用 `space.id_key`)。
"""

from __future__ import annotations

from typing import Any, Callable

from kb import answer as _answer
from kb import index as _index
from kb import search as _search
from kb.space import Space
from kb.vecdb import IndexMismatch, WrongDatabase

__all__ = ["KB", "bind", "Space", "IndexMismatch", "WrongDatabase"]


class KB:
    """绑定了一个 Space 的门面。省掉每次调用都把 space 传一遍。"""

    def __init__(self, space: Space) -> None:
        self.space = space

    def search(self, query: str, limit: int = 10,
               include_maybe: bool = True, scope: str | None = None) -> dict[str, Any]:
        return _search.search(self.space, query, limit, include_maybe, scope)

    def ask(self, question: str, k: int = 8) -> dict[str, Any]:
        return _answer.ask(self.space, question, k)

    def sync(self, rebuild: bool = False,
             on_progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
        return _index.sync(self.space, rebuild, on_progress)

    def status(self) -> dict[str, Any]:
        return _index.status(self.space)

    def __repr__(self) -> str:          # 报错时能看出是哪个库
        return f"<KB {self.space.name} db={self.space.db()}>"


def bind(space: Space) -> KB:
    return KB(space)
