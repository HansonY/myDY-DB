#!/usr/bin/env python
"""把明文 raw_json 迁移成压缩后的 raw_z,然后回收空间。

为什么:完整响应是 84 KB/条明文,2552 条就是 ~209 MB。压缩后 17.3 KB/条
(实测 4.9×),全库落到 ~43 MB —— **一个字段都不丢**。
体积问题用压缩解决,不用删字段解决:重采要冒 403,媒体地址还带
x-expires 会过期,删掉的字段是真拿不回来的。

做三件事:
  1. 逐条 zlib 压缩 raw_json → raw_z(每条压完立刻校验解压结果一致,不一致就中止)
  2. DROP 掉 raw_json 和两个雪碧图列(用户决定不做雪碧图;
     数据仍在完整 raw 里,哪天要用直接解析)
  3. VACUUM —— SQLite 删列不会自动还盘,不 VACUUM 那 116 MB 一点也收不回来

可反复运行:已迁移的会跳过。

用法:
    .venv/bin/python scripts/compress_raw.py            # 迁移 + 删列 + VACUUM
    .venv/bin/python scripts/compress_raw.py --dry-run  # 只看会发生什么
    .venv/bin/python scripts/compress_raw.py --keep-legacy   # 迁移但不删 raw_json
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import _pyversion
_pyversion.check()

from config import settings  # noqa: E402
from db import store  # noqa: E402

BATCH = 200
# 已经不用的列。数据要么在完整 raw 里、要么已被更好的字段取代。
#   sprite_*        雪碧图,用户决定不做
#   has_ai_summary  被三态的 content_state 取代。两个字段表达同一件事就会漂移,
#                   而且布尔本来就表达不了「还不知道」——留着只会有人误用。
DROP_COLS = ("sprite_url", "sprite_frames", "has_ai_summary")


def mb(n: float) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def columns(conn: sqlite3.Connection) -> set[str]:
    return {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}


def migrate(dry: bool) -> int:
    """把还是明文的 raw_json 压进 raw_z。返回迁移条数。"""
    with store.connect() as conn:
        if "raw_json" not in columns(conn):
            print("raw_json 列已不存在 —— 迁移过了。")
            return 0

        todo = conn.execute(
            "SELECT COUNT(*) AS n FROM videos "
            "WHERE raw_json IS NOT NULL AND raw_z IS NULL"
        ).fetchone()["n"]
        print(f"待迁移 {todo} 条")
        if not todo or dry:
            return 0

        done = raw_bytes = z_bytes = 0
        while True:
            rows = conn.execute(
                "SELECT aweme_id, raw_json FROM videos "
                "WHERE raw_json IS NOT NULL AND raw_z IS NULL LIMIT ?",
                (BATCH,),
            ).fetchall()
            if not rows:
                break

            payload = []
            for r in rows:
                text = r["raw_json"]
                blob = zlib.compress(text.encode("utf-8"), 9)
                # 当场验一遍。压缩是为了省空间,不是为了赌运气 ——
                # 校验不过就立刻停,不能让一条坏数据静默替换掉原文。
                if zlib.decompress(blob).decode("utf-8") != text:
                    raise SystemExit(f"✗ {r['aweme_id']} 压缩后解不回原文,已中止")
                raw_bytes += len(text.encode("utf-8"))
                z_bytes += len(blob)
                payload.append((blob, r["aweme_id"]))

            conn.executemany(
                "UPDATE videos SET raw_z = ?, raw_json = NULL WHERE aweme_id = ?",
                payload,
            )
            done += len(payload)
            print(f"  {done}/{todo}  {mb(raw_bytes)} → {mb(z_bytes)}", flush=True)

        if raw_bytes:
            print(f"\n压缩比 {raw_bytes / z_bytes:.1f}×  "
                  f"({raw_bytes / done / 1024:.1f} KB → {z_bytes / done / 1024:.1f} KB 每条)")
        return done


def drop_columns(dry: bool, keep_legacy: bool) -> list[str]:
    """删掉已经不用的列。"""
    with store.connect() as conn:
        have = columns(conn)
        targets = [c for c in DROP_COLS if c in have]
        if not keep_legacy and "raw_json" in have:
            left = conn.execute(
                "SELECT COUNT(*) AS n FROM videos WHERE raw_json IS NOT NULL"
            ).fetchone()["n"]
            if left:
                print(f"⚠️  还有 {left} 条明文 raw_json 没迁移,先不删这一列。")
            else:
                targets.append("raw_json")
        if not targets:
            return []
        print(f"\n准备删列:{', '.join(targets)}")
        if dry:
            return targets
        for c in targets:
            conn.execute(f"ALTER TABLE videos DROP COLUMN {c}")
        return targets


def vacuum() -> None:
    """SQLite 删了数据不会自动还盘,必须 VACUUM。
    不能在事务里跑,所以单独开一个 autocommit 连接。
    """
    conn = sqlite3.connect(settings.db_file, isolation_level=None)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-legacy", action="store_true",
                    help="迁移但保留 raw_json 列(不删、不回收空间)")
    args = ap.parse_args()

    store.init_db()          # 确保 raw_z 列存在
    db = Path(settings.db_file)
    before = db.stat().st_size
    print(f"库文件 {db}\n当前 {mb(before)}\n")

    migrate(args.dry_run)
    drop_columns(args.dry_run, args.keep_legacy)

    if args.dry_run:
        print("\n(--dry-run,什么都没改)")
        return

    # 无条件 VACUUM:压缩本身就腾出了大量页,不还盘等于白压
    print("\nVACUUM 回收空间…")
    vacuum()

    after = db.stat().st_size
    print(f"\n✓ {mb(before)} → {mb(after)}  省下 {mb(before - after)}")

    cov = store.coverage()
    print(f"  完整 raw {cov['full_raw']}/{cov['total']} 条"
          + (f",未迁移明文 {cov['legacy_raw']} 条" if cov["legacy_raw"] else ""))


if __name__ == "__main__":
    main()
