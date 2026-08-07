"""把所有勾选了深挖的关注者挖一遍。长任务,建议 nohup 跑。

放成独立脚本而不是内联 heredoc:heredoc 喂给后台进程的 stdin 会丢,
`nohup python - <<EOF &` 实测直接静默失败,连日志都不生成。
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import service
from db import store

t0 = time.time()


def on_creator(x: dict) -> None:
    print(f"[{(time.time() - t0) / 60:5.1f}m] {(x.get('nickname') or '')[:20]:<22}"
          f"{x['status']:<8} 新增 {x.get('inserted', 0):>4} · {x.get('pages', 0):>3} 页"
          f" · 走完={x.get('walked_all')}  {(x.get('error') or '')[:70]}", flush=True)


def main() -> int:
    picked = store.list_following(True)
    todo = [u for u in picked if not u["crawl_done_at"] or u["crawl_cursor"]]
    print(f"勾选 {len(picked)} 位 · 待挖 {len(todo)} 位 · "
          f"合计 {sum(u['aweme_count'] or 0 for u in todo)} 条\n", flush=True)

    r = asyncio.run(service.crawl_followed(on_creator=on_creator))

    print(f"\n=== {(time.time() - t0) / 60:.1f} 分钟 · 新增 {r['inserted']} 条 · "
          f"撞403={r['stopped_on_403']} ===", flush=True)
    with store.connect() as c:
        tot = c.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        mine = c.execute(
            f"SELECT COUNT(*) FROM videos v WHERE {store.mine_pred('v')}").fetchone()[0]
        fol = c.execute(
            f"SELECT COUNT(*) FROM videos v WHERE {store.scope_pred('following','v')}"
        ).fetchone()[0]
    ok = mine == 2556
    print(f"总 {tot} · 算我的 {mine} {'✓' if ok else '✗ 隔离破了!'} · 爬来的 {fol}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
