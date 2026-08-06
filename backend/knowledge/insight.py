"""自我分析:从「你自己做过的动作」里看你自己。只读库,纯计算。

**立场:做落差,不做图表。** 分类饼图谁都能画,看完你什么也没多知道 ——
你早就知道自己爱看什么。有价值的是**落差**:你以为的和实际的不一样的地方。

所以每个分析都产出一个 `headline`(一句话结论)+ `evidence`(支撑它的具体数字)
+ `sample`(能点开的作品 id)。**只给百分比不给证据,那和星座运势没区别。**

另一条硬规矩:每条结论标明 `kind` 是 `measured`(实测)还是 `inferred`(推断)。
一个听起来很懂你、但其实是猜的画像,比没有画像更糟 —— 你会照着它调整自己。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from db import store


# ── 数据指纹:决定要不要重算 ────────────────────────────────

def data_fingerprint() -> tuple[str, int, int]:
    """(指纹, 作品数, 有分类的条数)。

    指纹只用会影响分析结果的东西:条数、最新更新时间、分类覆盖、来源计数。
    数据没动就不重算 —— 尤其 AI 写的那段要花钱。
    """
    with store.connect() as c:
        r = c.execute("""
            SELECT COUNT(*) n, COALESCE(MAX(updated_at),'') mx,
                   SUM(cat1 IS NOT NULL AND TRIM(cat1)<>'') nc
            FROM videos""").fetchone()
        srcs = c.execute(
            "SELECT source, COUNT(*) n FROM video_sources GROUP BY source ORDER BY source"
        ).fetchall()
    key = json.dumps({
        "n": r["n"], "mx": r["mx"], "nc": r["nc"],
        "src": {x["source"]: x["n"] for x in srcs},
    }, sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:16], r["n"], r["nc"] or 0


# ── 一、收藏 vs 点赞:「想学的」和「被吸引的」不是一回事 ──────

def contrast(min_total: int = 25) -> dict[str, Any]:
    """两个动作的分类分布落差。

    这是最有信息量的一刀,因为**两个动作的含义本来就不同**:
      收藏 = 我打算之后再看(对未来的自己的期待)
      点赞 = 当下的情绪反应(此刻真实的消费)
    同一个人做这两件事,分类分布如果差得多,那个差就是「理想 vs 实际」。
    """
    with store.connect() as c:
        rows = c.execute("""
          SELECT v.cat1 cat,
            SUM(EXISTS(SELECT 1 FROM video_sources s
                WHERE s.aweme_id=v.aweme_id AND s.source='collection')) fav,
            SUM(EXISTS(SELECT 1 FROM video_sources s
                WHERE s.aweme_id=v.aweme_id AND s.source='like')) lik
          FROM videos v WHERE v.cat1 IS NOT NULL AND TRIM(v.cat1)<>''
          GROUP BY v.cat1 HAVING fav+lik >= ? ORDER BY (fav+lik) DESC
        """, (min_total,)).fetchall()
        both = c.execute("""
          SELECT COUNT(*) n FROM videos v
          WHERE EXISTS(SELECT 1 FROM video_sources s
                  WHERE s.aweme_id=v.aweme_id AND s.source='collection')
            AND EXISTS(SELECT 1 FROM video_sources s
                  WHERE s.aweme_id=v.aweme_id AND s.source='like')
        """).fetchone()["n"]

    tf = sum(r["fav"] for r in rows) or 1
    tl = sum(r["lik"] for r in rows) or 1
    items = []
    for r in rows:
        pf, pl = r["fav"] * 100 / tf, r["lik"] * 100 / tl
        items.append({"cat": r["cat"], "fav": r["fav"], "lik": r["lik"],
                      "fav_pct": round(pf, 1), "lik_pct": round(pl, 1),
                      "gap": round(pf - pl, 1)})
    items.sort(key=lambda x: -x["gap"])
    want = [x for x in items if x["gap"] >= 1.5][:4]      # 偏收藏 = 想学
    real = [x for x in items if x["gap"] <= -1.5][-4:]    # 偏点赞 = 图一乐

    head = "收藏和点赞的分类分布几乎一致 —— 你想学的和实际被吸引的是同一批东西"
    if want and real:
        head = (f"你**存起来打算学**的是「{'、'.join(x['cat'] for x in want[:2])}」,"
                f"但**当下真正吸引你**的是「{'、'.join(x['cat'] for x in real[-2:])}」")
    return {
        "id": "contrast", "kind": "measured",
        "title": "想学的 vs 被吸引的",
        "headline": head,
        "why": "收藏是「打算之后再看」,点赞是「当下的情绪反应」。"
               "同一个人两个动作,分类分布的差就是理想和实际的距离。",
        "evidence": (
            [f"偏收藏(想学):{x['cat']} +{x['gap']}(收藏 {x['fav_pct']}% vs 点赞 {x['lik_pct']}%)"
             for x in want] +
            [f"偏点赞(图一乐):{x['cat']} {x['gap']}(收藏 {x['fav_pct']}% vs 点赞 {x['lik_pct']}%)"
             for x in reversed(real)] +
            [f"既收藏又点赞 {both} 条 —— 这批大概率是真在意的"]
        ),
        "table": items,
        "denominator": f"收藏 {tf} 条 · 点赞 {tl} 条(仅统计有官方分类的)",
    }


# ── 二、你是被推荐流喂的,还是自己找的 ──────────────────────

def sourcing() -> dict[str, Any]:
    """热度分布 + 作者集中度 + 追新度,三个角度指向同一个问题。

    ⚠️ 这是 **inferred**,不是 measured:raw 里没有「这条是推荐来的还是搜来的」
    字段(翻过 228 个文本路径)。下面全是旁证,不是直接证据。
    """
    with store.connect() as c:
        digg = {}
        for src in ("collection", "like"):
            d = sorted(x["digg_count"] for x in c.execute(
                f"""SELECT digg_count FROM videos v
                    JOIN video_sources s ON s.aweme_id=v.aweme_id AND s.source='{src}'
                    WHERE digg_count IS NOT NULL"""))
            if d:
                mid_band = sum(1 for x in d if 10000 <= x < 1000000)
                digg[src] = {"n": len(d), "median": d[len(d) // 2],
                             "mid_band_pct": round(mid_band * 100 / len(d), 1)}
        au = c.execute("""SELECT COUNT(*) total, SUM(n=1) once FROM
            (SELECT COUNT(*) n FROM videos WHERE nickname IS NOT NULL
             AND TRIM(nickname)<>'' GROUP BY nickname)""").fetchone()
        top50 = c.execute("""SELECT SUM(n) s FROM
            (SELECT COUNT(*) n FROM videos WHERE nickname IS NOT NULL
             GROUP BY nickname ORDER BY n DESC LIMIT 50)""").fetchone()["s"] or 0
        tot_v = c.execute("SELECT COUNT(*) n FROM videos WHERE nickname IS NOT NULL").fetchone()["n"] or 1
        years = c.execute("""SELECT substr(create_time,1,4) y, COUNT(*) n FROM videos
            WHERE create_time IS NOT NULL AND create_time<>'' GROUP BY y ORDER BY y DESC""").fetchall()

    ty = sum(x["n"] for x in years) or 1
    old = sum(x["n"] for x in years if x["y"] and x["y"] < "2024")
    once_pct = round((au["once"] or 0) * 100 / max(au["total"] or 1, 1))
    top50_pct = round(top50 * 100 / tot_v, 1)

    fav, lik = digg.get("collection"), digg.get("like")
    ev = []
    if fav:
        ev.append(f"收藏的中位赞 {fav['median']:,},{fav['mid_band_pct']}% 落在 1 万–100 万"
                  "(推荐流的典型热度带)")
    if lik:
        ev.append(f"点赞的中位赞 {lik['median']:,} —— 和收藏几乎重合。"
                  "如果收藏是主动搜来的,该更偏冷门")
    ev.append(f"{au['total']} 个作者,{once_pct}% 只出现过一次 —— 不追人")
    ev.append(f"前 50 个作者只占 {top50_pct}% —— 没有稳定关注对象")
    ev.append(f"2023 年及更早的内容只占 {round(old * 100 / ty, 1)}% —— 不回溯,跟着流走")

    fed = bool(fav and fav["mid_band_pct"] > 55 and once_pct > 80)
    return {
        "id": "sourcing", "kind": "inferred",
        "title": "被喂的,还是自己找的",
        "headline": ("你的收藏是「推荐流刷到、觉得有用就存」,不是「有需求去找」"
                     if fed else "你的收藏里有相当比例不像是推荐流刷来的"),
        "why": "raw 里没有「推荐来的还是搜来的」这个字段(翻过 228 个文本路径),"
               "所以这条是三个旁证拼出来的**推断**,不是直接证据。",
        "evidence": ev,
        "denominator": f"作者集中度基于 {tot_v} 条;热度基于有互动数据的那批",
    }


# ── 三、形态:你消费的是什么长度的东西 ──────────────────────

def shape() -> dict[str, Any]:
    with store.connect() as c:
        d = sorted(x["video_duration"] / 1000 for x in c.execute(
            "SELECT video_duration FROM videos WHERE video_duration>0"))
        with_sum = c.execute(
            "SELECT COUNT(*) n FROM videos WHERE content_state='have'").fetchone()["n"]
        long_with_sum = c.execute(
            "SELECT COUNT(*) n FROM videos WHERE content_state='have' "
            "AND video_duration>300000").fetchone()["n"]
    if not d:
        return {"id": "shape", "kind": "measured", "title": "长短偏好",
                "headline": "还没有时长数据", "evidence": [], "why": ""}
    med = d[len(d) // 2]
    over5 = sum(1 for x in d if x > 300)
    return {
        "id": "shape", "kind": "measured",
        "title": "你消费的形态",
        "headline": f"中位 {med:.0f} 秒 —— 只有 {round(over5 * 100 / len(d))}% 超过 5 分钟,"
                    "这是短内容为主的库",
        "why": "长度决定了能装多少信息。1 分钟的视频装不下一个完整方法,"
               "所以「存起来学」这个动作本身就和形态有张力。",
        "evidence": [
            f"中位时长 {med:.0f} 秒 · 最长 {max(d) / 60:.0f} 分钟",
            f"超过 5 分钟的 {over5} 条({round(over5 * 100 / len(d))}%)",
            f"有抖音内容总结的 {with_sum} 条,其中 {long_with_sum} 条超过 5 分钟 —— "
            "平台只给长视频/知识类生成总结,所以这个数也是「有料内容」的下界",
        ],
        "denominator": f"基于 {len(d)} 条有时长的作品",
    }


# ── 四、把上面拼成一句话 ────────────────────────────────────

def tension(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """核心张力。刻意只出**一句**,不出第二句 —— 结论多了就没有结论。"""
    by = {p["id"]: p for p in parts}
    c, s, sh = by.get("contrast"), by.get("sourcing"), by.get("shape")
    want = [x for x in (c or {}).get("table", []) if x["gap"] >= 1.5][:2]
    med = None
    if sh:
        import re
        m = re.search(r"中位 (\d+) 秒", sh["headline"])
        med = m.group(1) if m else None

    line = "数据还不够,拼不出结论。"
    if want and med:
        line = (f"你在用一个为「即时消费」设计的推荐流,"
                f"囤积「{'、'.join(x['cat'] for x in want)}」这类打算之后学的内容 —— "
                f"而且是中位 {med} 秒的短视频形态。")
    return {
        "id": "tension", "kind": "inferred",
        "title": "核心张力",
        "headline": line,
        "why": "这句话是上面三条拼出来的。每个词都能追到具体数字,"
               "但「张力」本身是解读,不是测量结果。",
        "evidence": [p["headline"] for p in (c, s, sh) if p],
    }


# ── 五、和上次快照比:用历史绕过缺失的时间戳 ──────────────────

def diff_with_last(stats: dict[str, Any]) -> dict[str, Any] | None:
    """和上一份快照对比。

    **这是历史快照最重要的用途,不是省算力。** 我们拿不到「你什么时候收藏的」
    (抖音不给,而游标只存了页级),所以时间维度本来是空的。
    但只要定期存快照,「上次 vs 这次」就能看出兴趣在往哪偏、囤积速度多快 ——
    用历史把缺失的时间戳补回来。
    """
    prev = store.last_insight(skip_fp=stats.get("_fp"))
    if not prev:
        return None
    try:
        old = json.loads(prev["stats_json"])
    except ValueError:
        return None

    o = {x["cat"]: x for x in next(
        (p["table"] for p in old.get("parts", []) if p["id"] == "contrast"), [])}
    n = {x["cat"]: x for x in next(
        (p["table"] for p in stats.get("parts", []) if p["id"] == "contrast"), [])}
    moves = []
    for cat, cur in n.items():
        was = o.get(cat)
        if not was:
            continue
        d_fav = cur["fav"] - was["fav"]
        if d_fav:
            moves.append({"cat": cat, "delta_fav": d_fav})
    moves.sort(key=lambda x: -abs(x["delta_fav"]))

    dv = stats["n_videos"] - prev["n_videos"]
    return {
        "id": "drift", "kind": "measured",
        "title": f"和上次分析相比(上次 {prev['created_at'][:16].replace('T', ' ')})",
        "headline": (f"这段时间多了 {dv} 条" if dv > 0 else
                     "条数没变" if dv == 0 else f"少了 {-dv} 条"),
        "why": "抖音不给「你什么时候收藏的」,所以时间维度只能靠快照之间的差来看。"
               "分析越频繁,这条越准。",
        "evidence": [f"{m['cat']} 收藏 {'+' if m['delta_fav'] > 0 else ''}{m['delta_fav']} 条"
                     for m in moves[:6]] or ["各分类都没有明显变化"],
    }


# ── 组装 ────────────────────────────────────────────────────

def compute() -> dict[str, Any]:
    """算一份完整快照(不含 AI 那段)。纯确定性,可复现。"""
    fp, n_videos, n_class = data_fingerprint()
    parts = [contrast(), sourcing(), shape()]
    parts.append(tension(parts))
    stats = {
        "_fp": fp, "n_videos": n_videos, "n_classified": n_class,
        "parts": parts,
        # 分母写在脸上。分类类指标的真实分母是「有官方分类的条数」,
        # 不是总条数 —— 不写清就会给出一个看起来很确定的百分比。
        "coverage_note": (
            f"分类相关的比例只基于有官方分类的 {n_class} 条(全库 {n_videos} 条)。"
            f"还有 {n_videos - n_class} 条没采全,补齐后比例会变。"
        ),
    }
    d = diff_with_last(stats)
    if d:
        stats["parts"].append(d)
    return stats


# ── AI 那段:把确定性数字写成一段话 ─────────────────────────

NARRATIVE_SYSTEM = """你在帮用户理解他自己的抖音收藏行为。

下面给你的**全部是实测数字**。规则:
1. 只用给出的数字说话。**不要加任何常识性心理学解释** ——
   「这说明你焦虑」这种话没有数据支撑,不要写。
2. 标了「推断」的部分,措辞必须留有余地(「看起来像」「大概率」),
   不要说成事实。
3. 不要安慰,也不要说教。用户要的是看清自己,不是被评价。
4. 300 字以内。中文。不写客套话,不写「总的来说」。
5. 如果数字之间有矛盾,直接指出来 —— 那才是最有价值的部分。"""


def narrative(stats: dict[str, Any]) -> tuple[str, str]:
    """让模型把数字写成一段话。返回 (正文, 模型名)。

    刻意放在最后、且可选:确定性的数字先给人看,AI 那段是锦上添花。
    没有 key 就不给 —— 而不是让它编。
    """
    import httpx
    from config import settings
    key = settings.dashscope_api_key.strip()
    if not key:
        raise RuntimeError(
            "AI 解读需要 DASHSCOPE_API_KEY(写进 .env)。\n"
            "没有它也能看:上面那些数字和结论全是本地算的,不依赖任何 key。"
        )
    lines = []
    for p in stats["parts"]:
        lines.append(f"## {p['title']}({'实测' if p['kind']=='measured' else '推断'})")
        lines.append(p["headline"])
        lines += [f"- {e}" for e in p.get("evidence", [])]
    lines.append("\n" + stats["coverage_note"])

    r = httpx.post(
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "qwen-plus", "temperature": 0.4,
              "messages": [{"role": "system", "content": NARRATIVE_SYSTEM},
                           {"role": "user", "content": "\n".join(lines)}]},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip(), "qwen-plus"


def analyze(force: bool = False, with_narrative: bool = False) -> dict[str, Any]:
    """入口:算 / 复用 / 存快照。

    **数据没变就直接返回上次的** —— 这是用户明确要的「分析过就不用从头来」。
    指纹用条数 + 最新 updated_at + 分类覆盖 + 各来源计数算,
    这几项任一变化都会让指纹变,所以不会漏掉真更新。
    """
    fp, nv, nc = data_fingerprint()
    if not force:
        hit = store.insight_by_fp(fp)
        if hit and (hit["narrative"] or not with_narrative):
            st = json.loads(hit["stats_json"])
            return {"from_cache": True, "insight_id": hit["id"],
                    "created_at": hit["created_at"], "narrative": hit["narrative"],
                    "model": hit["model"], **st}

    st = compute()
    iid = store.save_insight(fp, st, nv, nc)
    narr = model = None
    if with_narrative:
        try:
            narr, model = narrative(st)
            store.attach_narrative(iid, narr, model)
        except Exception as e:
            narr, model = None, None
            st["narrative_error"] = str(e)
    return {"from_cache": False, "insight_id": iid,
            "created_at": store.get_insight(iid)["created_at"],
            "narrative": narr, "model": model, **st}
