"""把**岗位原文 + 我的简历**一起交给大模型,让它分析匹配程度。

和 `boss_match.py` 的关系:那边是确定性规则(城市/经验/学历/薪资/技能覆盖),
跑两次结果一样;这边是模型的判断,读起来有用但**不保证两次一致**。

所以这里做四件事把不确定性圈住:

1. **把规则已经算出来的事实一起喂给它。** 城市符不符、经验够不够、
   技能覆盖率多少、缺哪几个 —— 这些是算出来的,不该让模型再猜一遍,
   更不该让它猜出个和事实相反的结论。
2. **要求逐条给 quote**,而且 quote 必须能在 JD 或简历里字面找到。
   找不到的直接丢弃并计 `quote_miss` —— 这是把幻觉挡在数据外的唯一确定性手段。
3. **结果落库带 model + prompt_ver + jd_hash + resume_hash。**
   JD 是只升不降补上来的,补全之后旧结论必须失效;换了模型/prompt 也要能对比。
4. **规则和模型的结论并排显示。** 两者不一致时不去调和 —— 那是信息:
   要么规则的口径有问题,要么模型在编。藏起来才是真的错。

⚠️ 模型给的分数**两次可能不同**。所以落库缓存,页面上显示的是同一个数;
想重算就显式点。不缓存的话每次刷新都变,而一个会自己变的分数没法用来排序。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import boss_match as bm
import llm

# match2:技能匹配从机械词表对照改成**由模型判**。
# match1 时代把机械算的覆盖率喂进 C 段,结果是「缺 ios/网络/算法」这种清单 ——
# 对一个 iOS 开发说他缺 ios。词表对不出等价经历(「从 0 到上线」=「独立开发
# 且上线经验」),而等价经历恰恰是技能匹配里最值钱的判断。机械核对只留四个硬门槛。
PROMPT_VER = "match2"

PROMPT = """你在帮一个求职者判断「这个岗位和我的简历匹配到什么程度」。

我会给你三样东西:
  A. 岗位原文(标题、公司、城市、薪资、经验学历要求、职位描述全文)
  B. 我的简历(原文全文)
  C. **程序已经机械核对过的硬门槛结论**(城市/经验/学历/薪资 四项)

规则:
1. **C 是核对出来的结论,不要推翻它,也不要重复判。**
   比如程序说「城市不符」,你不要说「地点应该没问题」。
   除这四项外的一切判断都归你。
2. **技能匹配是你的重点,不做机械词表对照:**
   - `skills_hit`:岗位要求、而我简历里**有依据**的能力,1–6 条。
     每条 `point` 写「什么能力,依据是什么」,`quote` **逐字引自 B**。
     **等价经历也算**:简历没写「独立开发且上线经验」这个词、
     但写了「从 0 到上线」,就算有,引那句原文。
   - `skills_gap`:岗位**明确要求**、而简历里完全找不到依据的能力,0–6 条。
     `quote` **逐字引自 A**(引岗位提出这个要求的那句)。
     只列写明要求的;「加分项/优先」不算缺口,可进 `risks`。
3. 每一条判断都要带 `quote` —— **从 A 或 B 里逐字摘一句**支撑它。
   **写不出 quote 的那条就不要写。**
4. `fit` 给 0–100 的整数,由**职责对路程度 + 技能匹配 + 经验深度**共同决定,
   `fit_why` 用一句话说清这个数主要由什么决定。
5. `highlights`:我简历里**最能打动这个岗位**的 1–3 条,每条带 quote(引自 B)。
6. `risks`:我投这个岗位最可能被挑的 1–3 条,每条带 quote(引自 A 或 B)。
7. `hidden`:JD 里那些结构化不掉的要求(必须现场/要出差/要英语/统招/大小周…),
   每条带 quote(引自 A)。没有就空数组。
8. `verdict` 三选一:`worth`(值得投)/ `maybe`(可以试试)/ `skip`(别浪费时间)。
   ⚠️ **C 里有硬门槛判「不符合」时,不许给 `worth`**(最多 `maybe`),
   而且必须在 `risks` 里把那一项写出来。薪资上界低于我的底线、城市不符 ——
   这些是核对出来的事实,给「值得投」等于让我去投一个明知不合的岗位。
9. **不要写客套话、不要写「祝你好运」、不要复述岗位描述。** 中文,简洁。

只输出 JSON,不要解释、不要代码块围栏:
{"fit":0,"fit_why":"","verdict":"maybe",
 "skills_hit":[{"point":"","quote":""}],
 "skills_gap":[{"point":"","quote":""}],
 "highlights":[{"point":"","quote":""}],
 "risks":[{"point":"","quote":""}],
 "hidden":[{"point":"","quote":""}],
 "unclear":[]}"""


def _hash(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()[:16]


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def hashes(job: dict[str, Any], me: dict[str, Any]) -> tuple[str, str]:
    """`(jd_hash, resume_hash)` —— 缓存键里除 job_id/prompt_ver 之外的那两半。

    **必须只有这一处算法**:路由查缓存时算一遍、`analyze()` 落库时算一遍,
    两处口径差一点(比如一处取 `resume_raw` 一处取 `resume`)就永远命中不了缓存,
    而表现是「每次点都重新花钱」,不报错。
    """
    return (_hash(job.get("jd") or ""),
            _hash(me.get("resume_raw") or me.get("resume") or ""))


def haystack(job: dict[str, Any], me: dict[str, Any]) -> str:
    """quote 能在哪儿找到 —— 归一化掉空白的 A + B 全文。

    空白必须去掉:模型转述时空格、换行、全角空格都会变,而那不是编造。
    """
    return _norm_ws(f"{job.get('title')}{job.get('jd')}{job.get('salary_text')}"
                    f"{job.get('experience')}{job.get('degree')}"
                    f"{me.get('resume_raw') or me.get('resume') or ''}")


def verify_claims(items: Any, hay: str, cap: int = 3) -> tuple[list[dict[str, str]], int]:
    """逐条校验 quote,→ (留下的, 丢弃数)。**唯一能自动挡住幻觉的手段。**

    引不出原文的直接丢弃 —— 不是标记「可疑」然后照样显示:一条看着有理有据
    但原文里根本没有的判断,比没有这条更糟。丢弃数要报出来,它高了说明模型在编。

    独立成模块级函数是为了**能不调 LLM 就测** —— 埋在 analyze() 里的闭包
    没法写断言,而这一层正是最需要回归测试的地方。
    """
    out: list[dict[str, str]] = []
    miss = 0
    for it in (items if isinstance(items, list) else []):
        if not isinstance(it, dict):
            continue
        pt = str(it.get("point") or "").strip()
        q = str(it.get("quote") or "").strip()
        if not pt:
            continue
        if not q or _norm_ws(q) not in hay:
            miss += 1
            continue
        out.append({"point": pt[:120], "quote": q[:200]})
    return out[:cap], miss


_GATE_CN = {"city": "城市", "experience": "经验", "degree": "学历", "salary": "薪资"}


def conflicts(raw: dict[str, Any], facts: dict[str, Any], verdict: str | None,
              claims: list[dict[str, str]]) -> list[str]:
    """模型和 C 段(算出来的事实)对不上的地方。**只列出来,不修正。**

    `quote_miss` 抓的是「引不出原文」,抓不到这一类 —— 模型可以引一句真实的
    JD 原文,然后在结论里无视「薪资上界比我的底线低」这个算出来的事实。
    那种输出每一条都合规,只有整体判断是错的。

    检两样,都是确定性的:
      · 硬门槛有 fail 却给 `worth` —— 结构化比对,不看文字
      · 自己又报了一个覆盖率数字 —— prompt 明确说过这部分不许重算
    """
    out: list[str] = []
    fails = facts.get("hard_fail") or []
    if fails and verdict == "worth":
        out.append("硬门槛不符合(" + "、".join(_GATE_CN.get(f, f) for f in fails)
                   + ")却给了「值得投」")

    # 覆盖率交叉检只在 facts 里**真有**覆盖率时做(match1 的老缓存里有)。
    # match2 起 C 段不含覆盖率,模型提到百分比多半是它自己的措辞,没得比对。
    if "coverage" not in facts:
        return out
    blob = str(raw.get("fit_why") or "") + " " + " ".join(c["point"] for c in claims)
    m = re.search(r"覆盖率\s*(?:约|大约)?\s*(\d{1,3})\s*%", blob)
    rate = (facts.get("coverage") or {}).get("rate")
    if m:
        said = int(m.group(1)) / 100
        if rate is None:
            out.append(f"这个岗位覆盖率**算不出来**(没有技能证据),模型却报了 {m.group(1)}%")
        elif abs(said - rate) > 0.02:
            out.append(f"模型报的覆盖率 {m.group(1)}% 和规则算的 {rate:.0%} 不一致")
    return out


def build_context(job: dict[str, Any], me: dict[str, Any],
                  facts: dict[str, Any]) -> str:
    """拼 A/B/C 三段。C 只有四个硬门槛 —— 不给它,模型会在可核对的事情上瞎猜;
    但**技能不进 C**:match1 把机械覆盖率喂进去,等于用「缺 ios」这种结论
    去锚一个本该读原文的模型。技能判断整个归模型(见 PROMPT 规则 2)。"""
    sv = facts.get("salary") or {}
    gate_lines = []
    for it in (facts.get("gate_items") or []):
        v = {"pass": "符合", "fail": "**不符合**", "unknown": "数据缺失,未判定",
             "na": "不适用"}.get(it["verdict"], it["verdict"])
        gate_lines.append(f"  · {it['name']}:{v}"
                          + (f"(要求 {it['need']} / 我 {it['got']})"
                             if it.get("need") is not None else "")
                          + (f" —— {it['note']}" if it.get("note") else ""))

    A = f"""【A 岗位原文】
标题:{job.get('title')}
公司:{job.get('company')}
城市:{job.get('city') or '?'}{('·' + job['district']) if job.get('district') else ''}
薪资:{job.get('salary_text') or '?'}""" + (
        f"(月薪 {sv.get('monthly_min')}-{sv.get('monthly_max')}K"
        + (f",{sv.get('months')} 薪" if sv.get("months_src") != "assumed_12"
           else ",几薪未写明")
        + ")" if sv else "") + f"""
经验要求:{job.get('experience') or '未写'}
学历要求:{job.get('degree') or '未写'}
职位描述:
{(job.get('jd') or '(还没抓到职位描述)')[:6000]}"""

    # 技能词表**故意不给** —— 简历原文全文在,归一化过的 token 列表只会
    # 把模型往机械对照那边带,而它该做的是读经历判等价。
    B = f"""【B 我的简历】
工作年限:{me.get('years_exp') or '未填'} 年
学历:{me.get('degree') or '未填'}
期望城市:{', '.join(me.get('cities') or []) or '未填'}
薪资底线:{me.get('salary_floor') or '未填'}K / 理想 {me.get('salary_want') or '未填'}K
不接受:{', '.join(me.get('avoid') or []) or '未填'}
简历正文:
{(me.get('resume_raw') or me.get('resume') or '(没有简历原文)')[:6000]}"""

    C = f"""【C 程序已机械核对的硬门槛 —— 不要推翻,不要重判】
结论:{'四项全过' if facts.get('gate_pass') else '有不符合项'}
{chr(10).join(gate_lines)}
远程/不限地点:{'是' if facts.get('remote') else '否/未写明'}
岗位状态:{'已关闭' if job.get('job_state') == 'closed' else '未标记关闭'}"""
    return "\n\n".join((A, B, C))


def analyze(job: dict[str, Any], me: dict[str, Any],
            facts: dict[str, Any]) -> dict[str, Any]:
    """让模型分析一个岗位。→ 校验过 quote 的结构 + 元信息。

    不写库 —— 缓存是调用方的事。
    """
    if not llm.available():
        raise llm.NoKey("匹配分析要 LLM。到网页「AI 模型」里配一个。")

    ctx = build_context(job, me, facts)
    # 快档:实测 qwen-flash 3.9s vs qwen-plus 10.7s,判断的丰富度相当。
    # 「匹配太慢」的根因一半在这(另一半是首次要先提取)。
    raw = llm.chat_json(PROMPT, ctx, timeout=180,
                        model=llm.fast_model(), kind="match")
    if not isinstance(raw, dict):
        raise RuntimeError(f"模型返回的不是对象:{str(raw)[:120]}")

    hay = haystack(job, me)
    kept: dict[str, list[dict[str, str]]] = {}
    miss_n = 0
    # 技能两组是重点分析,允许多留几条;其余三组保持 3 条上限
    for k, cap in (("skills_hit", 6), ("skills_gap", 6),
                   ("highlights", 3), ("risks", 3), ("hidden", 3)):
        ok, n = verify_claims(raw.get(k), hay, cap)
        kept[k], miss_n = ok, miss_n + n

    fit = raw.get("fit")
    try:
        fit = max(0, min(100, int(float(fit))))
    except (TypeError, ValueError):
        fit = None
    verdict = raw.get("verdict") if raw.get("verdict") in ("worth", "maybe", "skip") else None

    total_claims = sum(len(raw.get(k) or []) if isinstance(raw.get(k), list) else 0
                       for k in ("skills_hit", "skills_gap",
                                 "highlights", "risks", "hidden"))
    jd_h, res_h = hashes(job, me)
    return {
        "fit": fit,
        "fit_why": str(raw.get("fit_why") or "")[:200],
        "verdict": verdict,
        "skills_hit": kept["skills_hit"],
        "skills_gap": kept["skills_gap"],
        "highlights": kept["highlights"],
        "risks": kept["risks"],
        "hidden": kept["hidden"],
        "unclear": [str(x)[:120] for x in (raw.get("unclear") or [])][:5],
        # **不调和,只指出来。** 两者不一致时那是信息:要么规则口径有问题,
        # 要么模型没把 C 当事实。悄悄把 worth 改成 maybe 会让这个信号消失。
        "conflicts": conflicts(raw, facts, verdict,
                               [c for v in kept.values() for c in v]),
        # 露出来,不藏:引不出原文的条数是这次输出可靠性的直接信号。
        # 它高了说明模型在编 —— 除了这个数,没有别的地方能看出来。
        "quote_miss": miss_n,
        "quote_total": total_claims,
        "model": llm.fast_model(),
        "prompt_ver": PROMPT_VER,
        # 和路由查缓存用的**同一个函数**,见 hashes() 的注释
        "jd_hash": jd_h,
        "resume_hash": res_h,
        # ⚠️ 同一个岗位再问一次,分数可能不一样(模型有随机性)。
        # 所以调用方要缓存,页面上显示的得是同一个数 —— 一个会自己变的分数没法排序。
        "reproducible": False,
    }


def facts_for(job: dict[str, Any], me: dict[str, Any]) -> dict[str, Any]:
    """机械核对的部分 —— **只有四个硬门槛**(城市/经验/学历/薪资)。

    match1 这里还算技能覆盖率,match2 起不算了:词表对照给出「缺 ios/网络/算法」
    这种结论(对一个 iOS 开发!),喂进 C 段就是拿错误结论锚模型。
    技能匹配归模型(skills_hit / skills_gap),它能判等价经历,词表不能。
    """
    g = bm.gate(job, me)
    return {
        "gate_pass": g["pass"], "gate_items": g["items"],
        "hard_fail": g["hard_fail"], "hard_unknown": g["hard_unknown"],
        "remote": g["remote"], "salary": g["salary"],
    }
