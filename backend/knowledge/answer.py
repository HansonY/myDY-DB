"""抖音侧的遗留薄壳。真正的实现在 `kb/answer.py`,提示词在 `knowledge/space.py`。

`verify_citations` 直接再导出 —— 它是纯函数,**同一个对象**,离线可测。

⚠️ 内核那边**刻意仍然写死 DashScope 调用**(和重构前逐字节一致)。
统一走 `llm.chat_text()` 是单独一步:那是整套重构里唯一会改变抖音行为的改动,
必须能单独 revert。混进来的话,一旦问答出问题就分不清是「内核抽取」
还是「换了 LLM 出口」造成的。
"""

from __future__ import annotations

from typing import Any

from kb import answer as _kb
from kb.answer import verify_citations          # noqa: F401  纯函数,同一个对象
from knowledge.space import SPACE

__all__ = ["ask", "verify_citations", "MODEL"]

MODEL = _kb.MODEL


def ask(question: str, k: int = 8) -> dict[str, Any]:
    return _kb.ask(SPACE, question, k)
