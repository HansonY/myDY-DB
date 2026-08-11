#!/usr/bin/env python3
"""把老的 `viewed + note` 记录回填成真正的 kind/status。零网络。

    BOSS_DB_PATH=data/boss.db .venv/bin/python scripts/boss_backfill_status.py [--dry]

**为什么这件事做得成。** 以前 `boss_main` 把 LLM 提取的 `my_status` 一律写成
`kind="viewed", status="unknown"`,真语义只塞进 `note`。看着是丢了,但**原文没丢** ——
所以现在能照 `map_my_status` 重新解一遍捞回来。这也是「认不出来就留原文」
这条规矩的回报:留了原文的错误是可修的,没留的就真没了。

幂等:`save_interaction` 的 status 只升不降,`upsert_jobs` 的 job_state
unknown 不覆盖已知 —— 重复跑不会把已经修好的弄坏。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from db import boss_store as bs        # noqa: E402

DRY = "--dry" in sys.argv


def main() -> None:
    bs.init_db()
    with bs.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT job_id, kind, status, note FROM interactions "
            "WHERE note IS NOT NULL AND note <> ''")]

    print(f"带 note 的记录 {len(rows)} 条" + ("(--dry 只看不改)" if DRY else ""))
    n_act = n_state = n_skip = 0
    for r in rows:
        acts, job_state = bs.map_my_status(r["note"])
        if not acts and not job_state:
            n_skip += 1
            print(f"  · {r['job_id'][:12]} note={r['note']!r} → 认不出来,原样留着")
            continue
        bits = []
        if acts:
            bits.append(" + ".join(f"{k}/{s}" for k, s in acts))
            if not DRY:
                for kind, status in acts:
                    bs.save_interaction(r["job_id"], kind, status, note=r["note"])
            n_act += len(acts)
        if job_state:
            bits.append(f"岗位={job_state}")
            if not DRY:
                # 只改 job_state,别动别的列 —— 走 upsert 会把 title 之类置空
                with bs.connect() as conn:
                    conn.execute("UPDATE jobs SET job_state=? WHERE job_id=?",
                                 (job_state, r["job_id"]))
                    conn.commit()
            n_state += 1
        print(f"  ✓ {r['job_id'][:12]} note={r['note']!r} → {' · '.join(bits)}")

    # 说「写入尝试」而不是「补出」:重复跑时这个数会变大(第一次补出的 applied 行
    # 自己也带 note,会被再解一遍),但实际一行没新增。写「补出」会让人以为
    # 每次跑都在产生新数据 —— 看数字的人分不出「又补了 2 条」和「同样的 2 条又写了一遍」。
    # 真实结果看下面的分布。
    print(f"\n写入尝试 {n_act} 次 · 岗位状态 {n_state} 次 · {n_skip} 条认不出来"
          f"(幂等:重复跑分布不变)")
    if n_skip:
        print("认不出来的原文都还在 note 里 —— 补 MY_STATUS_MAP 之后可以再跑一遍。")

    if not DRY:
        with bs.connect() as conn:
            print("\n现在的分布:")
            for r in conn.execute("SELECT kind, status, COUNT(*) n FROM interactions "
                                  "GROUP BY kind, status ORDER BY n DESC"):
                print(f"  {r['kind']:<9} {r['status']:<10} {r['n']}")
            for r in conn.execute("SELECT job_state, COUNT(*) n FROM jobs "
                                  "GROUP BY job_state"):
                print(f"  岗位状态 {r['job_state']:<9} {r['n']}")


if __name__ == "__main__":
    main()
