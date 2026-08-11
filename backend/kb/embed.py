"""嵌入器 —— 只是把 `knowledge.embed` 再导出一次。

**为什么不把文件搬过来。** `knowledge/insight.py:592` 也 `from knowledge import embed`,
而 insight.py 是明确不动的(718 行、深度绑定抖音表)。搬文件就得改它。

所以这一层的作用是:让内核里所有 import 都写成 `from kb import embed`,
**以后要翻转物理位置只改这一个文件的一行**。代价是内核有一条指向 knowledge/ 的
箭头 —— 但 `knowledge/embed.py` 本身零业务耦合(只读 settings 的两个配置项),
不构成循环依赖。

另一个实际好处:`get()` 拿到的是**同一个函数对象**,所以 `_cache` 单例只有一份。
两处各加载一次 bge-m3 就是多占 2.2 GB。
"""

from __future__ import annotations

from knowledge.embed import Embedder, get     # noqa: F401

__all__ = ["Embedder", "get"]
