"""每日简报:他们这几天讲了什么。

这一层是**产品正面**,不是采集管道。两类博主要的东西完全不同,所以分开算:

  信息价值主播  我要的是他讲的**内容** —— 每条给「讲了什么」+ 来源标注
                (抖音总结 / 我转的逐字稿 / 只有文案),让人一眼看出这条可不可信
  竞品主播      我要的是他的**打法** —— 选题分布、时长、发布节奏、互动效果,
                内容次要,重点是**可比**:和上一周期比、和别的竞品比

为什么内容要标来源:抖音总结只覆盖 34%,剩下的靠 ASR 补,还有一部分两者都没有
只剩营销文案。三者的可信度差很远,混在一起展示等于骗人 ——
「只有文案」的那条其实什么内容都没有,但看起来和有总结的一样。

只读,不采集,不调用抖音接口。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from db import store

# 内容来源分档。顺序就是可信度排序,前端按这个给不同的视觉权重。
SRC_SUMMARY = "summary"    # 抖音自己生成的内容总结
SRC_ASR = "asr"            # 我自己转的逐字稿
SRC_DESC = "desc"          # 只有作者写的文案 —— 常是营销话术,**不算内容**

_SRC_LABEL = {
    SRC_SUMMARY: "抖音总结",
    SRC_ASR: "我转的逐字稿",
    SRC_DESC: "只有文案(没有真实内容)",
}


def _cutoff(days: int) -> str:
    """N 天前的时间点,转成 `create_time` 那种**本地无时区**格式。

    ⚠️ `videos.create_time` 存的是本机本地时间且不带时区,而库里其它时间字段
    都是 UTC 带 +00:00。所以比较必须先换到本地再格式化,不能直接拿 UTC 串去比 ——
    差 8 小时不说,日期相同时 'T'(0x54) 和 ' '(0x20) 还会让比较结果整个反过来。
    """
    local = datetime.now(timezone.utc).astimezone() - timedelta(days=days)
    return local.strftime("%Y-%m-%d %H:%M:%S")


def _content_of(aweme_id: str, content_state: str) -> tuple[str, str]:
    """返回 (正文, 来源档)。优先级:抖音总结 > 逐字稿 > 文案。"""
    if content_state == "have":
        t = store.get_transcript(aweme_id, "summary")
        if t:
            return t, SRC_SUMMARY
    t = store.get_transcript(aweme_id, "asr")
    if t:
        return t, SRC_ASR
    return store.get_transcript(aweme_id, "desc") or "", SRC_DESC


def recent_by_role(role: str, days: int = 3,
                   limit_per_creator: int = 20) -> list[dict[str, Any]]:
    """某一类博主最近 N 天的新作品,按人分组。"""
    cut = _cutoff(days)
    with store.connect() as conn:
        creators = [dict(r) for r in conn.execute(
            "SELECT sec_user_id, nickname, avatar, aweme_count, follower_count "
            "FROM following WHERE role=? ORDER BY nickname", (role,))]
        out = []
        for c in creators:
            rows = [dict(r) for r in conn.execute(
                "SELECT aweme_id, item_title, description, create_time, "
                "       video_duration, digg_count, comment_count, share_count, "
                "       collect_count, cat1, cat2, content_state, share_url, cover "
                "FROM videos WHERE sec_user_id=? AND create_time >= ? "
                "ORDER BY create_time DESC LIMIT ?",
                (c["sec_user_id"], cut, limit_per_creator))]
            for r in rows:
                text, src = _content_of(r["aweme_id"], r["content_state"])
                r["content"] = text
                r["content_src"] = src
                r["content_src_label"] = _SRC_LABEL[src]
                r["tags"] = store.video_tags(r["aweme_id"])
            c["items"] = rows
            c["n"] = len(rows)
            # 这个人这几天有多少条是**真有内容**的 —— 只有文案的不算
            c["with_content"] = sum(
                1 for r in rows if r["content_src"] != SRC_DESC)
            out.append(c)
    return out


def info_digest(days: int = 3) -> dict[str, Any]:
    """信息价值主播的简报:他们讲了什么。"""
    creators = recent_by_role(store.ROLE_INFO, days)
    items = [i for c in creators for i in c["items"]]
    by_src: dict[str, int] = {}
    for i in items:
        by_src[i["content_src"]] = by_src.get(i["content_src"], 0) + 1
    return {
        "role": store.ROLE_INFO,
        "days": days,
        "creators": [c for c in creators if c["n"]],
        "silent": [c["nickname"] for c in creators if not c["n"]],
        "n_creators": len(creators),
        "n_items": len(items),
        "with_content": sum(1 for i in items if i["content_src"] != SRC_DESC),
        "by_source": {_SRC_LABEL[k]: v for k, v in by_src.items()},
        # 没有内容的那批要单独点名 —— 它们是「该跑 ASR」的清单,
        # 不说出来就会被当成「这几天没什么值得看的」
        "need_asr": [i["aweme_id"] for i in items if i["content_src"] == SRC_DESC],
    }


def rival_report(days: int = 3) -> dict[str, Any]:
    """竞品主播的打法报告。

    每个指标都配**上一周期同长度**的对比 —— 单看「这三天发了 5 条」没有意义,
    要看「比上三天多了 2 条」。没有对照的数字是装饰,不是分析。
    """
    now = datetime.now(timezone.utc).astimezone()
    cur_from = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    prev_from = (now - timedelta(days=days * 2)).strftime("%Y-%m-%d %H:%M:%S")

    with store.connect() as conn:
        creators = [dict(r) for r in conn.execute(
            "SELECT sec_user_id, nickname, avatar, aweme_count, follower_count "
            "FROM following WHERE role=? ORDER BY nickname", (store.ROLE_RIVAL,))]
        for c in creators:
            def window(lo: str, hi: str | None) -> dict[str, Any]:
                sql = ("SELECT COUNT(*) n, AVG(video_duration) dur, "
                       "  AVG(digg_count) digg, AVG(comment_count) cmt, "
                       "  SUM(digg_count) digg_sum "
                       "FROM videos WHERE sec_user_id=? AND create_time >= ?")
                p: list[Any] = [c["sec_user_id"], lo]
                if hi:
                    sql += " AND create_time < ?"
                    p.append(hi)
                r = conn.execute(sql, p).fetchone()
                return {"n": r["n"] or 0,
                        "avg_sec": round((r["dur"] or 0) / 1000),
                        "avg_digg": round(r["digg"] or 0),
                        "avg_comment": round(r["cmt"] or 0),
                        "total_digg": r["digg_sum"] or 0}

            c["now"] = window(cur_from, None)
            c["prev"] = window(prev_from, cur_from)
            c["delta"] = {k: c["now"][k] - c["prev"][k]
                          for k in ("n", "avg_sec", "avg_digg", "avg_comment")}
            # 选题分布:这几天他在讲什么类目
            c["topics"] = [dict(r) for r in conn.execute(
                "SELECT COALESCE(cat2, cat1) cat, COUNT(*) n FROM videos "
                "WHERE sec_user_id=? AND create_time >= ? AND cat1 IS NOT NULL "
                "GROUP BY cat ORDER BY n DESC LIMIT 6",
                (c["sec_user_id"], cur_from))]
            # 这几天最好的一条 —— 竞品分析里最有用的单点信息
            best = conn.execute(
                "SELECT aweme_id, item_title, description, digg_count, "
                "       video_duration, share_url FROM videos "
                "WHERE sec_user_id=? AND create_time >= ? AND digg_count IS NOT NULL "
                "ORDER BY digg_count DESC LIMIT 1",
                (c["sec_user_id"], cur_from)).fetchone()
            c["best"] = dict(best) if best else None

    active = [c for c in creators if c["now"]["n"]]
    return {
        "role": store.ROLE_RIVAL, "days": days,
        "creators": creators,
        "n_creators": len(creators),
        "n_active": len(active),
        "n_items": sum(c["now"]["n"] for c in creators),
        "note": ("每个数字都配了上一个同长度周期的对比。"
                 "只看「这几天发了几条」没有意义,要看和上个周期比的增减。"),
    }


def overview(days: int = 3) -> dict[str, Any]:
    """两类合在一起的总览,给页面顶部用。"""
    with store.connect() as conn:
        roles = {r["role"]: r["n"] for r in conn.execute(
            "SELECT role, COUNT(*) n FROM following WHERE role IS NOT NULL "
            "GROUP BY role")}
        cut = _cutoff(days)
        fresh = conn.execute(
            "SELECT COUNT(*) n FROM videos v WHERE v.create_time >= ? "
            "AND v.sec_user_id IN (SELECT sec_user_id FROM following "
            "                      WHERE role IS NOT NULL)", (cut,)).fetchone()["n"]
        # 待转写:有多少条新作品还没有任何真实内容
        need = conn.execute(
            "SELECT COUNT(*) n FROM videos v WHERE v.create_time >= ? "
            "AND v.content_state <> 'have' "
            "AND NOT EXISTS(SELECT 1 FROM transcripts t "
            "               WHERE t.aweme_id=v.aweme_id AND t.kind='asr') "
            "AND v.sec_user_id IN (SELECT sec_user_id FROM following "
            "                      WHERE role IS NOT NULL)", (cut,)).fetchone()["n"]
    return {
        "days": days,
        "n_info": roles.get(store.ROLE_INFO, 0),
        "n_rival": roles.get(store.ROLE_RIVAL, 0),
        "n_unclassified": 0,
        "fresh_items": fresh,
        "need_asr": need,
    }
