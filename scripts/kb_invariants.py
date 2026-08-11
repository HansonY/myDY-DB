#!/usr/bin/env python3
"""检查内核 `backend/kb/` 的隔离不变量。零网络、零依赖。

    .venv/bin/python scripts/kb_invariants.py

**为什么不能用 grep。** 内核里到处是在**解释**这些规矩的注释和文档串
(「这一层不认识 videos」「原来写死 settings.db_file」),grep 会把它们全算成违规。
一个只会误报的检查等于没有检查 —— 人会开始无视它,然后真的违规也一起无视掉。
所以走 AST。

⚠️ 但**文档串在 AST 里就是 `ast.Constant`**(我第一版以为它天然不在树里,
结果这个检查自己误报了 4 处:模块文档串里的 `kb`/`knowledge`、SQL 里的 CTE 名 `hits`、
以及 `ON CONFLICT DO UPDATE SET` 里的 `set`)。所以要显式跳过文档串,
并且认得出 CTE 名。注释确实不在树里,那部分成立。

两条不变量:

1. **内核不许知道任何具体库在哪。** 出现 `settings.db_file` / `db_path` 之类
   就是把路径写回内核了 —— 而那正是这次重构要拆掉的东西。
   路径只能来自 `Space.db`。

2. **内核不许认识业务表和业务模块。** `videos` / `video_sources` / `jobs` /
   `import store` 都不行。
   唯一例外:`aweme_id` —— 它是 `frag_vec` 的**物理列名**(BOSS 那边刻意沿用它
   存 job_id 就为了复用这一层),只能出现在 SQL 字符串里,不能出现在
   返回体的键名里(返回体用 `space.id_key`)。
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

KB = pathlib.Path(__file__).resolve().parent.parent / "backend" / "kb"

# 出现在**代码**里就算违规的记号
FORBIDDEN_ATTRS = {"db_file", "db_path"}          # settings.db_file 这种属性访问
FORBIDDEN_NAMES = {"videos", "video_sources", "jobs"}
FORBIDDEN_IMPORTS = {"db", "db.store", "db.boss_store", "store", "boss_store",
                     "knowledge.insight", "knowledge.digest"}
# SQL 字符串里允许出现业务表名吗?不允许 —— 除了 fragments / frag_vec / vec_meta
ALLOWED_TABLES = {"fragments", "frag_vec", "vec_meta", "sqlite_master"}

bad: list[str] = []


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """所有文档串常量的 id。**文档串在 AST 里是 Constant**,不跳过就会自己误报。"""
    out: set[int] = set()
    for n in ast.walk(tree):
        if not isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
            continue
        body = getattr(n, "body", None)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            out.add(id(body[0].value))
    return out


def _sql_tables(sql: str) -> set[str]:
    """从一段 SQL 里挑出真的表名。

    两个坑,都是这个检查自己踩出来的:
      · `WITH hits AS MATERIALIZED (…)` 里的 `hits` 是 CTE,不是表 —— 先收集再排除
      · `ON CONFLICT(id) DO UPDATE SET model=…` 里 `UPDATE` 后面跟的是 `SET`,
        不是表名
    """
    low = sql.lower()
    ctes = set(re.findall(r"\bwith\s+([a-z_][a-z0-9_]*)\s+as\b", low))
    ctes |= set(re.findall(r",\s*([a-z_][a-z0-9_]*)\s+as\s*\(", low))
    found = set()
    for m in re.finditer(r"\b(from|into|update|join)\s+([a-z_][a-z0-9_]*)", low):
        kw, name = m.group(1), m.group(2)
        if kw == "update" and name == "set":
            continue
        found.add(name)
    return found - ctes


def check(path: pathlib.Path) -> None:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    rel = path.relative_to(KB.parent.parent)
    docs = _docstring_nodes(tree)

    for node in ast.walk(tree):
        # ① settings.db_file / x.db_path
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            bad.append(f"{rel}:{node.lineno} 访问了 .{node.attr} —— "
                       f"路径只能来自 Space.db")
        # ② 裸标识符里的业务表名
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            bad.append(f"{rel}:{node.lineno} 用到了业务名 {node.id}")
        # ③ import 业务模块
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in FORBIDDEN_IMPORTS:
                    bad.append(f"{rel}:{node.lineno} import 了 {a.name}")
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod in FORBIDDEN_IMPORTS or mod.startswith("db"):
                bad.append(f"{rel}:{node.lineno} from {mod} import …")
        # ④ SQL 字符串里的业务表名。只看常量字符串,跳过文档串,只在像 SQL 时查。
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docs):
            low = node.value.lower()
            if not any(kw in low for kw in
                       (" from ", "insert into", "update ", "delete from", " join ")):
                continue
            for t in sorted(_sql_tables(node.value)):
                if t not in ALLOWED_TABLES:
                    bad.append(f"{rel}:{node.lineno} SQL 里出现了表 {t!r} —— "
                               f"内核只许碰 {sorted(ALLOWED_TABLES)}")


def main() -> None:
    files = sorted(KB.glob("*.py"))
    if not files:
        sys.exit(f"✗ {KB} 里没有 .py")
    for f in files:
        check(f)

    print(f"检查了 {len(files)} 个文件:" + ", ".join(f.name for f in files))
    if bad:
        print("\n✗ 违反隔离不变量:")
        for b in bad:
            print("  " + b)
        print("\n内核一旦知道了「哪个库」或「哪张业务表」,"
              "「改内核不碰业务、改业务不碰内核」就不成立了。")
        sys.exit(1)
    print("\n✓ 两条不变量都成立:内核不知道库在哪,也不认识业务表。")


if __name__ == "__main__":
    main()
