"""把粘进来的简历原文抽成结构化。**只抽,不评价。**

三条设计立场,和 `boss_extract.py` 一脉相承:

1. **只提取原文里真实出现的。** 没写的留 null,绝不猜、绝不补全。
   「看起来像 3 年经验」这种推断会被当成事实进硬门槛,而硬门槛是排序键的输入 ——
   错一次会被放大成整个排名。

2. **年限必须给出依据。** 让模型列出「哪几段经历加起来」,这样你能核对。
   ⚠️ 而且明确禁止「按毕业年份推算」—— 实习、gap、在读都会让它算错,
   而算错的那个数看不出来是算的还是抄的。

3. **抽完的东西一律可手改。** 这是整套匹配可信的前提:AI 抽错了你能纠,
   分数才有意义。所以 `parsed_json`(AI 原始输出)和生效值分开存,
   两者的差就是「哪些是我改的」。

走 `llm.chat_json`(它已经把「稳定吐 JSON」做完了三层兜底),
**不抄 `knowledge/answer.py` 和 `insight.narrative()`** —— 那两处写死了
DashScope + qwen-plus,绕过了多供应商抽象,会出现「提取能用但这里说没 key」的割裂。
"""

from __future__ import annotations

import re
from typing import Any

import boss_match as bm
import llm

PROMPT_VER = "resume1"

PROMPT = """你从一份简历原文里提取结构化信息,给一个求职匹配程序用。

**只提取原文里真实出现的信息。没写的留 null,绝对不要猜、不要补全、不要推断。**

规则:
1. skills:技术和工具名词。**只要原文明确出现过的** ——
   写「Swift」就是 Swift,不要因为写了 iOS 就补 Objective-C。
   一律小写、去掉空格和连字符(React Native → reactnative,Node.js → nodejs)。
   **不要**把「独立开发经验」「跨部门协同」这类描述性短句放进 skills,
   它们放到 highlights 里。
2. years_exp:工作年限(数字,可带小数)。**必须在 years_basis 里列出你是
   按哪几段经历算的**,让人能核对。
   ⚠️ **不要按毕业年份推算** —— 实习、gap、在读都会让这个数算错。
   算不出来就两个都留 null。
3. degree:最高学历,用「大专/本科/硕士/博士」之一的原词。没写留 null。
4. cities:简历里写明的期望城市。没写留空数组,**不要拿现居地当期望地**。
5. salary_floor / salary_want:千元/月。原文没写薪资期望就都留 null。
6. titles:做过的岗位名原词(如「iOS 开发工程师」)。
7. highlights:最多 5 条,每条不超过 30 字,**照抄或紧贴原文**的经历要点
   (「独立完成 App 从 0 到上线」)。不要写评价性的话。
8. constraints:简历里明确写了的**限制或不接受的事**,**照抄原文**
   (「不接受长期出差」「希望远程」「不考虑外包」)。没写就留空数组。
   ⚠️ 只抄原文,**不要归类、不要转成标签** —— 归类要对上程序里的枚举,
   那一步由人在页面上勾,不由你猜。
9. unclear:原文里读不出来、你觉得该由人确认的点。

只输出 JSON,不要解释、不要代码块围栏:
{"skills":[],"years_exp":null,"years_basis":null,"degree":null,
 "cities":[],"salary_floor":null,"salary_want":null,
 "titles":[],"highlights":[],"constraints":[],"unclear":[]}"""

# 和 boss_store 的 DEGREE 序关系保持一致的取值
_DEGREES = ("大专", "本科", "硕士", "博士", "中专", "高中")

# ⚠️ 归一化**从 boss_match 拿,不在这里再写一份**。
# 我第一版在这儿抄了一个 norm_skill,还写了句「必须和 boss_match 那个一致」——
# 那就是两份实现:一致靠人记,不一致不报错,只是「简历里的 reactnative」
# 和「岗位里的 react native」配不上,覆盖率悄悄偏低。
norm_skill = bm.norm_skill


def parse(resume_raw: str) -> dict[str, Any]:
    """抽一次。返回 AI 的原始结构 + 清洗后的生效值。

    不写库 —— 写库是调用方的事,因为中间还要过一次人眼。
    """
    text = (resume_raw or "").strip()
    if len(text) < 40:
        raise ValueError(f"简历太短({len(text)} 字),抽不出东西")
    if not llm.available():
        raise llm.NoKey("抽简历要 LLM。到网页「AI 模型」里配一个,或者直接手填。")

    raw = llm.chat_json(PROMPT, text[:24000], timeout=180)
    if not isinstance(raw, dict):
        raise RuntimeError(f"模型返回的不是对象:{str(raw)[:120]}")

    # ── 清洗。类型兜底照抄 boss_extract._clean 的思路:模型偶尔给字符串数值 ──
    def _num(v: Any) -> float | None:
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            m = re.search(r"\d+(?:\.\d+)?", v)
            return float(m.group()) if m else None
        return None

    def _list(v: Any) -> list[str]:
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if str(x).strip()]

    skills_raw = _list(raw.get("skills"))
    # 归一化 + 去重,但**保留原始写法**供人核对(页面上显示 "React Native → reactnative")
    seen: dict[str, str] = {}
    for s in skills_raw:
        k = norm_skill(s)
        if k and k not in seen:
            seen[k] = s
    deg = raw.get("degree")
    deg = next((d for d in _DEGREES if d in str(deg or "")), None)

    live = {
        "skills": sorted(seen),
        "years_exp": _num(raw.get("years_exp")),
        "degree": deg,
        "cities": _list(raw.get("cities")),
        "salary_floor": int(_num(raw.get("salary_floor")) or 0) or None,
        "salary_want": int(_num(raw.get("salary_want")) or 0) or None,
    }
    return {
        "parsed": raw,                      # 原始输出整份留着
        "live": live,                       # 清洗后的生效值(还要过人眼)
        "skills_display": seen,             # canon → 原文写法
        "years_basis": raw.get("years_basis"),
        "titles": _list(raw.get("titles")),
        "highlights": _list(raw.get("highlights"))[:5],
        # 简历里写的限制,**照原文带出来**。归到 avoid 的哪一个 axis 由人在页面上勾 ——
        # 让模型猜枚举等于让它替你做决定,而且猜错了 clash 会永远静默不匹配。
        "constraints": _list(raw.get("constraints")),
        "axes": list(bm.AXES),          # 页面渲染勾选项用
        "unclear": _list(raw.get("unclear")),
        "parsed_by": f"{llm.config()['model']}/{PROMPT_VER}",
        # 抽不出来的项列出来,让人知道该自己填哪几个 ——
        # 静默留 null 的话,硬门槛那边会因为「没有偏好」而放过所有岗位
        "missing": [k for k, v in live.items() if v in (None, [], "")],
    }
