"""抖音侧的遗留薄壳。真正的实现在 `kb/vecdb.py`。

**这个文件是唯一被允许知道 `settings.db_file` 的地方。**
内核那边 `connect(db, *, expect_table)` 两个参数都必填,故意不给默认值 ——
留个 `db=None` 回落默认库会造成一条每一环都不报错的失败链:
BOSS 忘了传 → 连上抖音库 → 指纹校验通过(同模型同维度)→ KNN 正常返回
→ 业务字段全 miss → 页面显示「相关结果 10 条」但标题空白。
所以默认值只能待在这里,而这里只服务抖音。

`IndexMismatch` 是**从内核直接导入的同一个类对象** —— 全仓库 6 处
`except vecdb.IndexMismatch`(main.py ×2 / mcp_server.py ×2 / cli.py ×2)
因此一个字都不用改。换成自己重新定义一个同名类,那 6 处会静默失效:
异常照样抛,但 catch 不住,变成 500。
"""

from __future__ import annotations

import sqlite3

from config import settings
from kb import vecdb as _kb
# 必须是同一批对象,不是同名的新对象
from kb.vecdb import (          # noqa: F401
    META_SQL,
    IndexMismatch,
    WrongDatabase,
    create_vec_table,
    drop_vec_table,
    read_meta,
    require_match,
    write_meta,
)

__all__ = ["connect", "META_SQL", "IndexMismatch", "WrongDatabase",
           "create_vec_table", "drop_vec_table", "read_meta", "require_match",
           "write_meta"]


def connect() -> sqlite3.Connection:
    """开一个加载了 sqlite-vec 的抖音库连接。签名保持无参 —— 三个调用点零感知。"""
    return _kb.connect(settings.db_file, expect_table="videos")
