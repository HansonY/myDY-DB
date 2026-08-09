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

# 「这条是我主动选的」的唯一判据。加了「深挖关注者主页」之后 videos 里混着
# 爬来的别人的全部产出 —— 拿那些去算「我的偏好」,分子分母一起废。
# 这一行以前散成 8 处裸 `FROM videos`,漏掉的地方不报错、只静默给错的数。
MINE = store.mine_pred("videos")


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
            FROM videos WHERE """ + store.mine_pred("videos")).fetchone()
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
        au = c.execute(f"""SELECT COUNT(*) total, SUM(n=1) once FROM
            (SELECT COUNT(*) n FROM videos WHERE nickname IS NOT NULL
             AND TRIM(nickname)<>'' AND {MINE} GROUP BY nickname)""").fetchone()
        top50 = c.execute(f"""SELECT SUM(n) s FROM
            (SELECT COUNT(*) n FROM videos WHERE nickname IS NOT NULL
             AND {MINE} GROUP BY nickname ORDER BY n DESC LIMIT 50)""").fetchone()["s"] or 0
        tot_v = c.execute(
            f"SELECT COUNT(*) n FROM videos WHERE nickname IS NOT NULL AND {MINE}"
        ).fetchone()["n"] or 1
        years = c.execute(f"""SELECT substr(create_time,1,4) y, COUNT(*) n FROM videos
            WHERE create_time IS NOT NULL AND create_time<>'' AND {MINE}
            GROUP BY y ORDER BY y DESC""").fetchall()

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
            f"SELECT video_duration FROM videos WHERE video_duration>0 AND {MINE}"))
        with_sum = c.execute(
            f"SELECT COUNT(*) n FROM videos WHERE content_state='have' AND {MINE}"
        ).fetchone()["n"]
        long_with_sum = c.execute(
            f"SELECT COUNT(*) n FROM videos WHERE content_state='have' "
            f"AND video_duration>300000 AND {MINE}").fetchone()["n"]
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


# ── 标签关联图 ─────────────────────────────────────────────

def tag_graph(min_count: int = 6, min_edge: int = 3,
              max_nodes: int = 70) -> dict[str, Any]:
    """标签共现网络:节点=标签,边=同一作品上同时出现的次数。

    为什么共现比「标签排行」有信息量:排行只告诉你「哪个标签多」,
    共现告诉你**哪些兴趣是连在一起的**。实测这个库里能自然分出
    英语簇(英语/口语/听力/启蒙)、AI 簇(ai/人工智能/大模型/程序员)、
    情感簇(情感/情感共鸣/婚姻)—— 这些簇就是你的真实关注面。

    **只染 3 个簇的颜色,其余归中性。** 这不是审美选择:网络图是
    「所有簇同时在屏上」的情形(all-pairs),用配色验证器在本页实际底色
    #0b0d11 上跑过 —— 3 色全过,加第 4 色时黄↔橙在红绿色盲下
    ΔE 只有 4.8、常视觉 10.6,硬失败。所以第 4 簇起一律中性灰,
    靠标签文字本身区分(secondary encoding)。
    """
    with store.connect() as c:
        nodes = {r["t"]: r["n"] for r in c.execute(
            "SELECT LOWER(tag) t, COUNT(DISTINCT aweme_id) n FROM tags "
            "GROUP BY t HAVING n >= ? ORDER BY n DESC LIMIT ?",
            (min_count, max_nodes))}
        if not nodes:
            return {"nodes": [], "edges": [], "clusters": []}
        ph = ",".join("?" * len(nodes))
        keys = list(nodes)
        edges = [
            {"a": r["t1"], "b": r["t2"], "w": r["w"]}
            for r in c.execute(f"""
                SELECT LOWER(a.tag) t1, LOWER(b.tag) t2, COUNT(*) w
                FROM tags a JOIN tags b
                  ON a.aweme_id = b.aweme_id AND LOWER(a.tag) < LOWER(b.tag)
                WHERE LOWER(a.tag) IN ({ph}) AND LOWER(b.tag) IN ({ph})
                GROUP BY t1, t2 HAVING w >= ?""", keys + keys + [min_edge])
        ]
        # 每个标签的主分类 —— 用来给簇起名字之外的第二种解释
        cats = {}
        for r in c.execute(f"""
            SELECT LOWER(t.tag) t, v.cat1 c, COUNT(*) n FROM tags t
            JOIN videos v ON v.aweme_id = t.aweme_id
            WHERE LOWER(t.tag) IN ({ph}) AND v.cat1 IS NOT NULL
            GROUP BY t, c ORDER BY n DESC""", keys):
            cats.setdefault(r["t"], r["c"])

    # 连通分量 = 簇。用并查集,边已经过了 min_edge 阈值
    parent = {k: k for k in nodes}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for e in edges:
        ra, rb = find(e["a"]), find(e["b"])
        if ra != rb:
            parent[ra] = rb

    groups: dict[str, list[str]] = {}
    for k in nodes:
        groups.setdefault(find(k), []).append(k)
    # 按「簇内作品总数」排,而不是按标签个数 —— 大簇应该是真的占比大
    ranked = sorted(groups.values(), key=lambda g: -sum(nodes[t] for t in g))

    # 只有前 3 个簇上色(见 docstring 里的配色验证结论)
    COLORED = 3
    cluster_of: dict[str, int] = {}
    clusters = []
    for i, g in enumerate(ranked):
        cid = i if i < COLORED else -1          # -1 = 中性
        for t in g:
            cluster_of[t] = cid
        if i < COLORED and len(g) > 1:
            lead = max(g, key=lambda t: nodes[t])
            clusters.append({
                "id": i, "lead": lead, "size": len(g),
                "videos": sum(nodes[t] for t in g),
                "tags": sorted(g, key=lambda t: -nodes[t])[:8],
                "cat": cats.get(lead),
            })

    return {
        "nodes": [{"tag": t, "n": n, "cluster": cluster_of.get(t, -1),
                   "cat": cats.get(t)} for t, n in nodes.items()],
        "edges": edges,
        "clusters": clusters,
        "note": (f"节点 = 出现 ≥{min_count} 次的标签,边 = 同一作品上共现 ≥{min_edge} 次。"
                 f"只有最大的 {COLORED} 个簇上色 —— 网络图上所有簇同屏,"
                 "配色验证器在本页底色上实测第 4 色会与第 2 色在红绿色盲下无法区分。"),
    }


# ── 值不值:每个标签的干货率 ────────────────────────────────

def tag_quality(min_n: int = 15, limit: int = 16) -> dict[str, Any]:
    """每个标签下「真的讲了内容」的比例,以及它的传播量。

    **这是这一层里唯一能直接行动的分析。** 前面那些行为画像回答的是
    「你是什么样的人」,看完了也不知道该干什么。这个回答的是
    「你这 72 条英语口语里,哪 19 条值得留,哪 53 条可以清掉」。

    判据:`content_state='have'` —— 抖音只给长视频/知识类生成内容总结,
    所以「有总结」是「这条真的讲了东西」的可用代理(不完美,是代理)。

    两个维度一起看才有意义:
      干货率低 + 平均赞高  →  你被同一个话题的**爆款**反复喂。
                              实测 #英语口语 72 条只 19 条有干货、平均 13 万赞。
      干货率高 + 平均赞低  →  更像你自己找的。
                              实测 #ai 87 条有 38 条干货、平均只 2 万赞。
    单看干货率会把「小众但水」和「爆款但水」混成一类,而它们该做的处置不同。

    刻意用**标签**而不是官方分类:上面的标签球和关联图都是标签,
    换一套词汇会让人得在脑子里翻译一遍(这个毛病第一版犯过)。
    """
    with store.connect() as c:
        # 只算「我主动选的」—— 认识自己的每一段都不能把爬来的博主内容算进去,
        # 否则干货率会被关注者的营销视频稀释。之前这里漏了这个过滤。
        rows = c.execute("""
            SELECT LOWER(t.tag) tg,
                   COUNT(DISTINCT t.aweme_id) n,
                   SUM(v.content_state='have')    AS have,
                   SUM(v.content_state='unknown') AS unknown,
                   AVG(v.digg_count) AS digg
            FROM tags t JOIN videos v ON v.aweme_id = t.aweme_id
            WHERE {mine}
            GROUP BY LOWER(t.tag)
            HAVING n >= ? ORDER BY n DESC LIMIT ?
        """.format(mine=store.mine_pred("v")), (min_n, limit)).fetchall()

    items = []
    for r in rows:
        n = r["n"]
        rate = round((r["have"] or 0) * 100 / n)
        digg = round(r["digg"] or 0)
        # 阈值是看着实测分布定的,不是理论值 —— 所以要露出来让人自己判断
        verdict = ("fed"   if rate < 35 and digg > 80000 else
                   "keep"  if rate >= 45 else
                   "quiet" if digg < 30000 else "mixed")
        items.append({
            "tag": r["tg"], "n": n, "have": r["have"] or 0,
            "rate": rate, "unknown": r["unknown"] or 0,
            "avg_digg": digg, "verdict": verdict,
            "thin": n - (r["have"] or 0) - (r["unknown"] or 0),
        })
    return {
        "items": items,
        "verdicts": {
            "fed":   "被爆款反复喂 —— 干货率低但传播量大,同一个话题换个人再讲一遍",
            "keep":  "值得留 —— 干货率高",
            "quiet": "小众 —— 传播量不大,更像你自己找的",
            "mixed": "一般",
        },
        "note": ("「有干货」= 抖音给它生成过内容总结(平台只给长视频/知识类生成),"
                 "是「真的讲了东西」的**代理指标**,不是精确判定。"
                 "「未采全」那些还不知道有没有,补齐信息后这些数会变。"),
    }


def following_aspects() -> dict[str, Any]:
    """我关注的人都是做什么的,以及「关注了却没在看」的缺口。

    这一段回答:我关注的 97 人,方面上集中在哪、哪些是「关注了但刷不到、
    最该做成简报」的。核心洞察是**关注 ≠ 在看**的落差 ——
    某个方面关注了很多人却几乎没收藏过,说明我想跟但平台没喂给我。

    ⚠️ 诚实前提:方面靠「这个博主在我库里的作品的主导官方分类」推断,
    所以**只有我存过或抓过其作品的人才能归类**。实测 97 人里只有 35 人
    能归类,其余 62 人从没存过也没抓过 —— 那 62 人单独计入「还不了解」,
    绝不编一个覆盖全部 97 人的假分布(高保真稿里那张满表是设计占位)。

    方面 = 把官方 cat1 归成几个大类。归法写在 _ASPECT 里,是手工的、可调。
    """
    with store.connect() as c:
        # 每位关注者的主导分类(在库里作品最多的那个 cat1)
        rows = c.execute("""
          WITH per AS (
            SELECT f.sec_user_id, v.cat1, COUNT(*) n,
                   ROW_NUMBER() OVER (PARTITION BY f.sec_user_id
                                      ORDER BY COUNT(*) DESC) rk
            FROM following f JOIN videos v ON v.sec_user_id = f.sec_user_id
            WHERE v.cat1 IS NOT NULL AND TRIM(v.cat1) <> ''
            GROUP BY f.sec_user_id, v.cat1)
          SELECT p.sec_user_id, p.cat1,
                 EXISTS(SELECT 1 FROM videos v2
                        WHERE v2.sec_user_id = p.sec_user_id AND {mine}) AS saved
          FROM per p WHERE rk = 1
        """.format(mine=store.mine_pred("v2"))).fetchall()
        total = c.execute("SELECT COUNT(*) n FROM following").fetchone()["n"]

    buckets: dict[str, dict[str, Any]] = {}
    for r in rows:
        asp = _ASPECT.get(r["cat1"], "其他")
        b = buckets.setdefault(asp, {"aspect": asp, "follow": 0, "saved": 0,
                                     "cats": set()})
        b["follow"] += 1
        b["saved"] += 1 if r["saved"] else 0
        b["cats"].add(r["cat1"])

    items = []
    for b in buckets.values():
        gap = b["follow"] - b["saved"]
        # 「关注了却几乎没收藏」= 最该做成简报的缺口
        verdict = ("主线,关注即在看" if b["saved"] >= b["follow"] * 0.6 else
                   "缺口最大,关注了却刷不到" if b["saved"] <= b["follow"] * 0.3 else
                   "在跟,量不大")
        items.append({**b, "cats": sorted(b["cats"]), "gap": gap,
                      "verdict": verdict})
    items.sort(key=lambda x: -x["follow"])

    known = sum(b["follow"] for b in buckets.values())
    return {
        "items": items,
        "total_following": total,
        "categorizable": known,
        "unknown": total - known,
        "note": (f"只有 {known}/{total} 位能推断方面(其余从没存过也没抓过)。"
                 "方面按「其作品的主导官方分类」归类,是估计不是精确。"),
    }


# cat1 → 方面 的归类。手工映射,官方分类有十几种,归成几个我真正在意的大类。
# 没列到的一律「其他」。这个表可以随时调 —— 它只影响这一段的呈现。
_ASPECT = {
    "校园教育": "英语 / 教育", "外语": "英语 / 教育",
    "财经": "财经 / 投资", "三农": "财经 / 投资",
    "科技": "AI / 科技", "科普": "AI / 科技",
    "人文社科": "人文 / 社科", "文化": "人文 / 社科",
    "个人管理": "个人成长", "职场": "个人成长",
    "二次元": "娱乐", "游戏": "娱乐", "影视": "娱乐",
    "音乐": "娱乐", "美食": "生活", "亲子": "生活", "随拍": "生活",
}
