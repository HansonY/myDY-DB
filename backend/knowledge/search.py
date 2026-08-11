"""抖音侧的遗留薄壳。真正的实现在 `kb/search.py`,业务差异在 `knowledge/space.py`。

**签名和默认值一字不改**,尤其 `scope="mine"` —— 这是双保险:
哪怕适配器的 default_scope 写错了,抖音这一路也还是 mine。
默认必须是 mine,因为关注者的全量产出比我的收藏多一个数量级,
不设默认就等于把「我的知识库」悄悄换成「他们的内容农场」。

调用方(main.py:155 / mcp_server.py:173 都是**位置传参**)所以参数顺序也不能动。
"""

from __future__ import annotations

from typing import Any

from kb import search as _kb
from knowledge.space import SPACE

__all__ = ["search"]


def search(query: str, limit: int = 10,
           include_maybe: bool = True, scope: str = "mine") -> dict[str, Any]:
    return _kb.search(SPACE, query, limit, include_maybe, scope)
