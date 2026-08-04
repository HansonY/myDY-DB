"""智能采集计划器 —— 把踩过的坑写成程序自己会处理的规则。

实践教训(2026-08 首次全量采集期间实测):

1. **不同接口的策略不一样**
   收藏能一路翻到历史尽头;点赞翻不深就 403,三次都是。
   所以每个分类要有独立的状态和页数上限,不能一套参数打天下。

2. **调大间隔救不了点赞**
   8s 间隔撑了 55 页(1106 条),15s 间隔只撑了 14 页(272 条)。
   慢并不能换来更深 —— 更像是接口本身对「往历史深处翻」有累计配额。
   所以对策是 **主动收手**:没到 403 就先停,下次接着来。
   被 403 拒过之后再回来,冷却成本比自己收手高得多。

3. **游标只往旧翻**
   翻到底之后再用 resume 是永远发现不了新增内容的,必须切成增量同步
   (从最新扫,连续几页无新增就停)。所以状态里要记 `exhausted`。

4. **中断必须无损**
   已采数据和游标逐页落库,任何时候被打断都能接着来。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from db import store

# ── 每类的采集策略 ──────────────────────────────────────────
# max_pages_per_run:单次主动收手的页数上限。宁可少采,也别撞 403。
PLANS: dict[str, dict[str, Any]] = {
    "collection": {
        "label": "收藏",
        # 收藏接口实测能一路到底,给个宽上限只为兜底
        "max_pages_per_run": 80,
    },
    "like": {
        "label": "点赞",
        # 点赞三次都被 403(55 页 / 14 页),主动压到 25 页收手
        "max_pages_per_run": 25,
    },
    "post": {
        "label": "我的作品",
        "max_pages_per_run": 40,
    },
}

# 指数退避:403 之后等多久再碰这个接口
BACKOFF_MINUTES = [30, 60, 120, 240, 360]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def cooldown_left(scope: str) -> timedelta | None:
    """还要冷却多久;None 表示现在就能采。"""
    until = _parse(store.get_state(scope).get("blocked_until"))
    if not until:
        return None
    left = until - _now()
    return left if left.total_seconds() > 0 else None


def decide(scope: str) -> dict[str, Any]:
    """决定这个分类现在该做什么。"""
    st = store.get_state(scope)
    plan = PLANS[scope]

    left = cooldown_left(scope)
    if left:
        mins = int(left.total_seconds() // 60) + 1
        return {
            "scope": scope, "label": plan["label"], "action": "skip",
            "reason": f"被限流冷却中,还需 {mins} 分钟",
        }

    if st.get("exhausted"):
        return {
            "scope": scope, "label": plan["label"], "action": "sync",
            "reason": "历史已采尽,改为增量同步(发现新增内容)",
            "max_pages": plan["max_pages_per_run"],
        }

    return {
        "scope": scope, "label": plan["label"], "action": "resume",
        "reason": "历史未采尽,从断点继续往前采",
        "max_pages": plan["max_pages_per_run"],
    }


def plan_all() -> list[dict[str, Any]]:
    return [decide(s) for s in PLANS]


# ── 结果回写 ────────────────────────────────────────────────

def record_success(scope: str, pages: int, exhausted: bool) -> None:
    """成功一轮:清掉退避,必要时标记已采尽。"""
    st = store.get_state(scope)
    store.save_state(
        scope,
        exhausted=1 if (exhausted or st.get("exhausted")) else 0,
        blocked_until=None,
        backoff_level=0,
        consecutive_403=0,
        last_status="done",
        last_error=None,
        last_run_at=_now().isoformat(timespec="seconds"),
        total_pages=(st.get("total_pages") or 0) + pages,
    )


def record_throttled(scope: str, pages: int, error: str) -> dict[str, Any]:
    """被限流:按级数退避。返回冷却信息供展示。"""
    st = store.get_state(scope)
    level = min((st.get("backoff_level") or 0), len(BACKOFF_MINUTES) - 1)
    mins = BACKOFF_MINUTES[level]
    until = _now() + timedelta(minutes=mins)

    store.save_state(
        scope,
        blocked_until=until.isoformat(timespec="seconds"),
        backoff_level=min(level + 1, len(BACKOFF_MINUTES) - 1),
        consecutive_403=(st.get("consecutive_403") or 0) + 1,
        last_status="throttled",
        last_error=error[:300],
        last_run_at=_now().isoformat(timespec="seconds"),
        total_pages=(st.get("total_pages") or 0) + pages,
    )
    return {"cooldown_minutes": mins, "until": until.isoformat(timespec="seconds")}


def record_failure(scope: str, pages: int, error: str) -> None:
    """非限流的失败(网络、解析等):不退避,下次照常重试。"""
    st = store.get_state(scope)
    store.save_state(
        scope,
        last_status="failed",
        last_error=error[:300],
        last_run_at=_now().isoformat(timespec="seconds"),
        total_pages=(st.get("total_pages") or 0) + pages,
    )


def is_throttle_error(err: BaseException) -> bool:
    """判断是不是被限流。抖音给的是 403;也兜住 429。"""
    s = f"{type(err).__name__}: {err}"
    return "403" in s or "429" in s or "Too Many Requests" in s
