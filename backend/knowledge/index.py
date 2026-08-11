"""抖音侧的遗留薄壳。真正的实现在 `kb/index.py`。

签名和默认值一字不改 —— 调用方有 4 处(main.py / mcp_server.py / cli.py /
scripts/build_index.py),`scripts/build_index.py --status` 的 stdout 还是
零回归验证里逐字节比对的一项。
"""

from __future__ import annotations

from typing import Any, Callable

from kb import index as _kb
from knowledge.space import SPACE

__all__ = ["sync", "status", "BATCH"]

BATCH = _kb.BATCH


def sync(rebuild: bool = False,
         on_progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    return _kb.sync(SPACE, rebuild, on_progress)


def status() -> dict[str, Any]:
    return _kb.status(SPACE)
