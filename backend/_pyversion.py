"""Python 版本守卫。

为什么需要:`python3` 在很多机器上已经是 3.14,而依赖链里的 pydantic-core
要经 PyO3 编译,PyO3 当前最高支持 3.13。用 3.14 建 venv 后 `pip install`
会倒在一大片 Rust 编译错误上,信息里完全看不出根因是 Python 版本 ——
新用户第一步就会卡死在这。

下界 3.10 来自 f2 的 requires-python。
"""

from __future__ import annotations

import sys

MIN = (3, 10)
MAX = (3, 13)   # PyO3(pydantic-core)当前上限


def check() -> None:
    v = sys.version_info[:2]
    if MIN <= v <= MAX:
        return

    cur = f"{v[0]}.{v[1]}"
    hint = (
        f"太新 —— pydantic-core 依赖的 PyO3 目前最高支持 "
        f"{MAX[0]}.{MAX[1]},用 {cur} 装依赖会倒在 Rust 编译错误上。"
        if v > MAX
        else f"太旧 —— f2 需要 {MIN[0]}.{MIN[1]} 以上。"
    )
    sys.exit(
        f"\n✗ 当前 Python {cur} {hint}\n\n"
        f"  请用 {MIN[0]}.{MIN[1]}–{MAX[0]}.{MAX[1]} 重建虚拟环境,例如:\n"
        f"    brew install python@{MAX[0]}.{MAX[1]}\n"
        f"    rm -rf .venv && $(brew --prefix)/bin/python{MAX[0]}.{MAX[1]} -m venv .venv\n"
        f"    .venv/bin/pip install -r backend/requirements.txt\n"
    )
