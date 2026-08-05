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


# 允许的缺口:原作者删稿 / 作品被限制后就再也拉不到,差额会永久存在。
# 超过这个量才认为「真的还没采完」。
def _tolerance(total: int) -> int:
    return max(10, int(total * 0.01))

# 完整走完一遍仍有缺口的确认次数上限。到了就接受现状(判定为删稿造成的永久差额),
# 否则会为了永远补不上的缺口无限重采,反而招风控。
MAX_EXHAUST_PASSES = 2


def gap_info(scope: str) -> dict[str, Any]:
    """已采 / 平台总数 / 缺口。没有分母时 total 为 None。"""
    st = store.get_state(scope)
    got = store.count_by_source(scope)
    total = st.get("platform_total")
    if not isinstance(total, int) or total <= 0:
        return {"collected": got, "total": None, "gap": None, "short": False}
    gap = total - got
    return {
        "collected": got, "total": total, "gap": gap,
        "percent": round(got * 100 / total, 1),
        "short": gap > _tolerance(total),      # 缺口超出容差 = 还没采完
    }


def decide(scope: str) -> dict[str, Any]:
    """决定这个分类现在该做什么。"""
    st = store.get_state(scope)
    plan = PLANS[scope]
    g = gap_info(scope)
    base = {"scope": scope, "label": plan["label"], **g}

    left = cooldown_left(scope)
    if left:
        mins = int(left.total_seconds() // 60) + 1
        return {**base, "action": "skip", "reason": f"被限流冷却中,还需 {mins} 分钟"}

    if not st.get("exhausted"):
        return {**base, "action": "resume", "max_pages": plan["max_pages_per_run"],
                "reason": "历史未采尽,从断点继续往前采"}

    # 标记为已采尽,但分母显示还差不少 —— 说明「生成器自然结束」判断错了
    # (实测点赞被 403 打断后就出现过)。再完整走一遍确认。
    passes = st.get("exhaust_passes") or 0
    if g["short"] and passes < MAX_EXHAUST_PASSES:
        return {**base, "action": "resume", "max_pages": plan["max_pages_per_run"],
                "reason": f"标记已采尽但仍缺 {g['gap']} 条,再确认一遍"
                          f"(第 {passes + 1}/{MAX_EXHAUST_PASSES} 次)"}

    # 走到这里说明:已采尽,且缺口要么在容差内、要么已确认到上限。
    # 后者是「不可达」而非「待采」—— 必须区分,否则界面会一直显示
    # 「还差 N 条」+ 警告色,让人以为有事可做,而实际上补不了。
    permanent = g["short"]
    tail = (f";仍缺 {g['gap']} 条,已确认 {passes} 遍,"
            "判定为原作者删稿/私密/注销造成的永久差额") if permanent else ""
    return {**base, "action": "sync", "max_pages": plan["max_pages_per_run"],
            "gap_permanent": permanent,
            "reason": f"历史已采尽,改为增量同步(发现新增内容){tail}"}


def plan_all() -> list[dict[str, Any]]:
    return [decide(s) for s in PLANS]


# ── 结果回写 ────────────────────────────────────────────────

def save_totals(totals: dict[str, int]) -> None:
    """写入平台侧总数(采集完整度的分母)。"""
    at = _now().isoformat(timespec="seconds")
    for scope, n in totals.items():
        if scope in PLANS:
            store.save_state(scope, platform_total=n, platform_total_at=at,
                             total_source="api")


def set_manual_total(scope: str, total: int | None) -> None:
    """手填分母。收藏只能走这条路 —— 抖音不给「我收藏了多少条」。

    填了之后完整度判定就生效:缺口超容差会继续续采而不是切增量。
    传 None 清除。
    """
    if scope not in PLANS:
        raise ValueError(f"未知分类:{scope}")
    if total is not None and total < 0:
        raise ValueError("总数不能为负")
    store.save_state(
        scope,
        platform_total=total,
        platform_total_at=_now().isoformat(timespec="seconds") if total else None,
        total_source="manual" if total else None,
        exhaust_passes=0,      # 换了分母就重新给它机会去补齐
    )


def record_success(scope: str, pages: int, exhausted: bool) -> None:
    """成功一轮:清掉退避,必要时标记已采尽。

    如果这一轮完整走完(exhausted)但分母显示仍有缺口,累加确认次数 ——
    到达上限就不再重试,把缺口当成删稿造成的永久差额。
    """
    st = store.get_state(scope)
    passes = st.get("exhaust_passes") or 0
    if exhausted and gap_info(scope)["short"]:
        passes += 1

    store.save_state(
        scope,
        exhaust_passes=passes,
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
