#!/usr/bin/env python
"""批量预缓存封面。

为什么必须尽快跑:所有封面 URL 都带 `x-expires` 签名。没落到本地的,
过期之后就永久拿不到了(只能重采该作品换新 URL,而重采是风控风险)。
浏览时才按需缓存是不够的 —— 你不会把每条都翻一遍。

用法:
    .venv/bin/python scripts/precache_covers.py            # 补齐所有缺的
    .venv/bin/python scripts/precache_covers.py --limit 200
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import httpx

import _pyversion
_pyversion.check()

from db import store  # noqa: E402

COVER_DIR = ROOT / "data" / "covers"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
# 抖音 CDN 防盗链,必须带 Referer
HEADERS = {"Referer": "https://www.douyin.com/", "User-Agent": UA}

# CDN 不是业务接口,并发高一些没关系;但也别太猛
CONCURRENCY = 6


async def fetch_one(cli: httpx.AsyncClient, sem: asyncio.Semaphore,
                    aweme_id: str, url: str) -> tuple[str, str]:
    path = COVER_DIR / f"{aweme_id}.jpg"
    async with sem:
        try:
            r = await cli.get(url, headers=HEADERS)
        except Exception as e:
            return "error", f"{aweme_id} {type(e).__name__}"
    if r.status_code != 200 or not r.content:
        # 多半是签名已过期 —— 无法补救
        return "expired", f"{aweme_id} HTTP {r.status_code}"
    path.write_bytes(r.content)
    return "ok", aweme_id


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条")
    args = ap.parse_args()

    store.init_db()
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    have = {p.stem for p in COVER_DIR.glob("*.jpg")}

    with store.connect() as conn:
        rows = conn.execute(
            "SELECT aweme_id, cover FROM videos "
            "WHERE cover IS NOT NULL AND TRIM(cover) <> ''"
        ).fetchall()
    todo = [(r["aweme_id"], r["cover"]) for r in rows if r["aweme_id"] not in have]
    if args.limit:
        todo = todo[: args.limit]

    print(f"共 {len(rows)} 条有封面,已缓存 {len(have)},待补 {len(todo)}")
    if not todo:
        print("没有需要补的。")
        return

    sem = asyncio.Semaphore(CONCURRENCY)
    stats = {"ok": 0, "expired": 0, "error": 0}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as cli:
        tasks = [fetch_one(cli, sem, i, u) for i, u in todo]
        for n, fut in enumerate(asyncio.as_completed(tasks), 1):
            kind, info = await fut
            stats[kind] += 1
            if n % 100 == 0 or n == len(tasks):
                print(f"  {n}/{len(tasks)}  成功 {stats['ok']} "
                      f"已失效 {stats['expired']} 出错 {stats['error']}", flush=True)

    print(f"\n完成:新增 {stats['ok']} 张 · 已失效 {stats['expired']} · 出错 {stats['error']}")
    if stats["expired"]:
        print("已失效的补不回来了 —— 那些 URL 的签名过期了,只能重采该作品换新地址。")


if __name__ == "__main__":
    asyncio.run(main())
