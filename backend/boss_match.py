"""岗位 × 简历的匹配。**纯函数,不写库,不调 LLM。**

分层,每层都标清是 measured 还是 inferred:

    第 0 层  归一化           —— 地基,不是判断
    第 1 层  硬门槛(measured) 城市/经验/学历/薪资,**四态不是布尔**
    第 2 层  技能匹配(measured) 覆盖率 + 缺口清单
    第 3 层  JD 隐性要求(inferred) 走 LLM,单独放 boss_implicit.py
    排序键                     由前几层按写死的公式算出,**不让 AI 打分**

LLM 那层刻意放在另一个文件里 —— 这样 0/1/2 层和排序键完全可复算:
同一个库跑两次结果必须一样。「85 分」这种不可检验的东西不该进排名。

这一版只实现第 0 层(归一化 + 共用枚举)。1/2 层和排序键紧接着来。
"""

from __future__ import annotations

import json
import re
from typing import Any

# ── 求职偏好 / JD 隐性要求的**共用枚举** ────────────────────────
#
# ⚠️ `me.avoid` / `me.want_axes` 和第 3 层抽出来的 `axis` 必须用**同一张表**。
# 不同的话 clash 永远匹配不上,而且**不报错** —— 页面上会一直显示「没有冲突」,
# 你会以为这些岗位都合意。这类「静默永真/永假」比崩溃难查得多。
AXES: tuple[tuple[str, str], ...] = (
    ("scope",         "职责范围(做多宽)"),
    ("autonomy",      "自主度(自己定方案还是照着做)"),
    ("stack_breadth", "技术栈宽度(专精 vs 全栈)"),
    ("domain",        "业务领域"),
    ("team",          "团队与协作方式"),
    ("process",       "流程与规范"),
    ("work_mode",     "工作方式(远程/坐班/出差/加班)"),
    ("lang",          "语言要求"),
    ("edu_bar",       "学历门槛(统招/985/211)"),
    ("hidden_cost",   "隐性代价(大小周/值班/常态加班)"),
)
AXIS_KEYS = tuple(k for k, _ in AXES)
AXIS_LABEL = dict(AXES)


# ── 第 0 层:归一化 ──────────────────────────────────────────
#
# **这是地基,不是判断。** 简历侧和岗位侧必须走同一个函数 ——
# 各写一份的话「简历里的 reactnative」和「岗位里的 react native」配不上,
# 而且不报错,只是覆盖率偏低,谁也看不出来少了什么。

def norm_skill(s: str) -> str:
    """技能词归一化:小写 + 去掉空白/连字符/点/下划线/斜杠/中点。

        React Native / react-native / React_Native  → reactnative
        Node.js → nodejs      Objective-C → objectivec     CI/CD → cicd

    ⚠️ 实测库里是 `react native`(带空格)而不是 `react-native` ——
    所以空格必须吃掉,只处理连字符是不够的。
    """
    t = str(s or "").strip().lower()
    return re.sub(r"[\s\-_./·\\]+", "", t)


# 同一个技术的不同叫法。人工维护 —— **别名表要做厚**,因为语义匹配只做兜底:
# `java` 和 `javascript`、`react` 和 `react native` 的向量距离很近,
# 靠阈值分不开,只能靠别名表先把真正等价的收掉。
_ALIAS_RAW: dict[str, tuple[str, ...]] = {
    # `objectc` 是真库里实际出现的拼写(少了 ive),包含规则判不出来 —— 只能列进来
    "objectivec":  ("oc", "objc", "objective c", "objectc", "objective-c"),
    "reactnative": ("rn",),
    "javascript":  ("js",),
    "typescript":  ("ts",),
    "kubernetes":  ("k8s",),
    "llm":         ("大模型", "大语言模型", "largelanguagemodel"),
    "postgresql":  ("pg", "postgres"),
    "golang":      ("go",),
    "csharp":      ("c#",),
    "cplusplus":   ("c++",),
    "machinelearning": ("ml", "机器学习"),
    "deeplearning":    ("dl", "深度学习"),
    "computervision":  ("cv", "计算机视觉"),
}
# 展开成 canon → canon 的查表
_ALIAS: dict[str, str] = {}
for _canon, _alts in _ALIAS_RAW.items():
    for _a in _alts:
        _ALIAS[norm_skill(_a)] = _canon


def canon(tok: str) -> tuple[str, str]:
    """→ (canon_key, how)。`how` ∈ `norm` | `alias`。

    `how` 要带出去:命中别名的那几对必须**可审计**。
    「oc 算不算 objective-c」是个判断,判断就该能被看见和推翻。
    """
    k = norm_skill(tok)
    if k in _ALIAS:
        return _ALIAS[k], "alias"
    return k, "norm"


# 软能力的尾缀。命中这些的词是「定性要求」,**不进覆盖率分母**。
_SOFT_TAIL = ("能力", "经验", "意识", "思维", "协同", "沟通", "审美",
              "责任心", "主动性", "抗压", "学习力", "同理心", "优先", "加分")
# 常见工具 —— 和硬技术分开(便于展示),但都算进覆盖率
_TOOLS = {"cursor", "codex", "claudecode", "copilot", "dify", "coze", "n8n",
          "windsurf", "cline", "trae", "git", "xcode", "fastlane", "jenkins",
          "docker", "kubernetes", "figma", "postman", "jira", "sentry"}
# **业务领域**(不是技术能力)。显式列表,而不是「凡中文即领域」——
# 见 skill_kind 的说明。
_CJK_RE = re.compile(r"[一-鿿]")

# **JD 套话**:是「活动」不是「技术」。
#
# 实测缺口清单被这些词占满了前排:架构设计 75 个岗位、性能优化 75、稳定性 71、
# 软件 62、交付 58、用户体验 52、功能开发 50 ——「你缺 软件」毫无意义,
# 而它们把真缺口(android / unity / 鸿蒙 / jenkins)挤到了看不见的地方。
# skill_kind 会把它们判成 tech,因为它们短、又不以软能力尾缀结尾。
#
# 只列**不含糊**的那些。像 `算法`、`单元测试` 这种既是套话又可能是真要求的,
# 不进这张表 —— 交给频率标记(见 gaps 的 generic)。
# 判断该被看见,不该藏在停用表里。
_JD_FILLER = {
    "架构设计", "性能优化", "稳定性", "交付", "功能开发", "用户体验", "软件",
    "需求分析", "技术方案", "技术方案规划", "代码质量", "技术升级", "实施",
    "开发", "研发", "编码", "维护", "优化", "上线", "迭代", "重构",
    "技术选型", "方案设计", "项目管理", "团队协作", "业务理解",
    # 学历 / 专业要求 —— 不是技能。实测「计算机相关专业」出现在 39 个岗位里,
    # 只有 7 个汉字所以没到 CJK≥10 的阈值,被判成了 tech 然后进了缺口清单
    # (「你缺计算机相关专业」)。
    "计算机相关专业", "计算机专业", "软件工程专业", "统招本科", "全日制本科",
    "相关专业", "本科及以上", "学历要求",
}

_BIZ_DOMAIN = {"电商", "跨境电商", "金融", "教育", "医疗", "健康", "游戏", "社交",
               "出行", "内容", "广告", "营销", "企业服务", "saas", "toc", "tob",
               "供应链", "物流", "零售", "地产", "汽车", "制造", "政企", "外包"}


def skill_kind(tok: str) -> str:
    """→ `tech` | `tool` | `domain` | `soft`。

    **覆盖率只算 tech + tool。** 这一条很要紧:
    实测 `jobs.tags` **不是平台给的**,是 LLM 从页面文字里抽的,所以混着
    硬技术词、领域词、和「独立开发且上线经验」「跨部门协同」这类整句。
    拿软能力算覆盖率是在骗自己 —— 我简历里不会有「独立开发且上线经验」
    这个字符串,但我明明有那段经历;它进分母就是纯噪音,而且会把
    覆盖率系统性压低,让所有岗位都显得不匹配。

    ⚠️ **判据是「能力名 vs 描述句」,不是「中文 vs 英文」。**
    第一版我写成「含中文的短词 → domain」,结果 `推荐系统`、`音视频`、`风控`
    这些实打实的技术能力全掉进 domain 而被排除在覆盖率之外 ——
    那会让覆盖率虚高、缺口清单少一大半,而且**没有任何迹象能看出来**
    (分数照样算,只是分母小了)。中文技术名词就是技术名词。

    所以只有**显式列出的业务领域词**(电商/金融/健康…)才算 domain。
    domain 和 soft 都不进覆盖率,但会在缺口清单里**单独分组显示** ——
    「这些岗位要电商背景,你没有」仍然是有用的信息,只是不该混进百分比。
    """
    raw = str(tok or "").strip()
    k = norm_skill(raw)
    if not k:
        return "soft"
    if k in _TOOLS:
        return "tool"
    if k in _BIZ_DOMAIN:
        return "domain"
    if raw in _JD_FILLER or k in {norm_skill(x) for x in _JD_FILLER}:
        return "activity"          # 活动,不是技术 —— 不进覆盖率
    # 描述句 / 软能力:以软能力尾缀结尾
    if any(raw.rstrip("。.、;;").endswith(t) for t in _SOFT_TAIL):
        return "soft"
    # 「像一句话」只能用**中日韩字符数**判,不能用总长度。
    #
    # ⚠️ 我第一版写的是 `len(raw) >= 10`,拿真库 766 条技能词一跑,118 条被误判 ——
    # 而且恰好是最重要的那些:Objective-C(11 字符)、TypeScript(10)、
    # React Native、Spring Boot、AVFoundation、Jetpack Compose、Prompt Engineering
    # 全被判成软能力、全被排除在覆盖率之外。英文技术名 10~20 字符是常态,
    # 用总长度判「是不是句子」只对中文成立。
    # 这个错**不会报错、不会崩**,只是覆盖率的分母悄悄少掉一大块。
    #
    # 阈值 10 是**在真库 803 条技能词上扫出来的,不是拍的**:
    #   6/7 → 误伤 `自然语言处理`、`网络模块优化`、`应用稳定性治理`(都是真能力)
    #   8   → 误伤 `自然语言处理算法`、`模型加速/性能优化`
    #   10  → 只捞到 `系统集成项目管理工程师`(证书名)和
    #          `计算机/软件工程相关专业`(学历要求)—— 这两个确实不是技能 ✓
    #   12  → 一个都捞不到,规则变死代码,那两个非技能会漏进 tech
    # 也就是说 10 是「这条规则还在干活」的最大值。扫描过程见提交说明。
    cjk = len(_CJK_RE.findall(raw))
    if cjk >= 10:
        return "soft"
    # 其余一律 tech —— 中英文一视同仁,`推荐系统` 和 `PostgreSQL` 都是技术名
    return "tech"


def in_coverage(kind: str) -> bool:
    """这一类算不算进覆盖率分母。**只有一处定义**,免得各处自己判。

    activity(架构设计/性能优化…)和 soft/domain 一样不进 —— 它们是
    「几乎每个 JD 都写」的活动,进分母只会把覆盖率整体压低,
    而且「缺架构设计」不是一个能行动的缺口。
    """
    return kind in ("tech", "tool")


# ══════════════════════════════════════════════════════════════
# 第 1 层:硬门槛(measured)—— **四态,不是布尔**
# ══════════════════════════════════════════════════════════════
#
# pass / fail / unknown / na。这是 jd_state 三态初衷的平移:
# 「不符合」和「还不知道」混成一个 fail 之后,「该去补数据还是该认命」
# 谁也说不清。而 na 是「这一项对这个岗位不适用」(远程岗谈城市没意义)。

PASS, FAIL, UNKNOWN, NA = "pass", "fail", "unknown", "na"

# 远程 / 不限地点。**必须在城市之前判**。
#
# ⚠️ 实测:227 个岗位里 9 个提到远程,其中**两个是非上海的**
# (绵阳「AI Builder - 全职远程」、北京「高级 iOS 客户端工程师」提到 Remote)。
# 把远程判定放在城市之后,这两个会被判掉 —— 而它们恰好是地理上最兼容的,
# 判掉的理由("城市不符")看起来完全合理,没人会去查。
# 这是整套设计里最容易骗人的第一名。
REMOTE_RE = re.compile(
    r"全职远程|远程办公|远程居家|居家办公|不限地点|不限城市|可远程|线上兼职|remote",
    re.I)

# 经验原文 → (下限, 上限, 形态)。**穷举优先,正则兜底。**
# 实测库里只有 7 种取值,所以能穷举 —— 穷举的好处是可以逐条测。
_EXP_TABLE: dict[str, tuple[int | None, int | None, str]] = {
    "经验不限": (None, None, "any"),
    "不限":     (None, None, "any"),
    "在校/应届": (0, 1, "fresh"),
    "应届":     (0, 1, "fresh"),
    "应届生":   (0, 1, "fresh"),
}
_EXP_RANGE = re.compile(r"(\d+)\s*[-–~]\s*(\d+)\s*年")
_EXP_MIN = re.compile(r"(\d+)\s*年以上")


def parse_exp(text: str | None) -> tuple[int | None, int | None, str]:
    """经验要求 → (下限, 上限, 形态)。形态 ∈ range|min|any|fresh|unparsed。

    **读不懂就是 unparsed,绝不当成「不限」。**
    实测库里有一条 `'5天/周3个月'` —— 那是实习排班,不是经验要求。
    把它当成「不限」会让这个岗位静默通过经验门槛,而门槛是排序键的输入,
    一个没读懂的字符串就这样变成了「符合」,再被放大成排名。
    """
    t = (text or "").strip()
    if not t:
        return None, None, "unparsed"
    if t in _EXP_TABLE:
        return _EXP_TABLE[t]
    m = _EXP_RANGE.search(t)
    if m:
        return int(m.group(1)), int(m.group(2)), "range"
    m = _EXP_MIN.search(t)
    if m:
        return int(m.group(1)), None, "min"
    if "不限" in t or "无经验" in t:
        return None, None, "any"
    if "应届" in t or "在校" in t:
        return 0, 1, "fresh"
    return None, None, "unparsed"


# 学历序关系。**这是「要求的下界」,不是「越高越好」。**
_DEGREE_ORDER = {"不限": 0, "学历不限": 0, "中专": 1, "高中": 1, "中技": 1,
                 "大专": 2, "专科": 2, "本科": 3, "学士": 3,
                 "硕士": 4, "研究生": 4, "博士": 5}
_DEG_WORDS = sorted(_DEGREE_ORDER, key=len, reverse=True)


def degree_rank(text: str | None) -> int | None:
    """学历 → 序号。读不懂返回 None(门槛判 unknown,不判 pass)。"""
    t = (text or "").strip()
    if not t:
        return None
    # 先剥「以上/及以上」后缀,再匹配 —— 「本科以上」的要求是本科
    t = re.sub(r"(及)?以上$", "", t)
    for w in _DEG_WORDS:
        if w in t:
            return _DEGREE_ORDER[w]
    return None


def salary_view(job: dict[str, Any]) -> dict[str, Any]:
    """薪资的三个口径,**刻意不合成一个数**。

    · 月薪区间:measured(实测 226/227 齐全)
    · 几薪:**只有 121/227**。空的那 106 条**不要默认 12 然后把总包当 measured 报** ——
      实测有几薪的那些是 13/14/15/16/17/18,假设 12 薪会**系统性低估 8%–50%**。
    · 总包:standard 的算 measured,assumed_12 的算 inferred,两者绝不混在一张图里。

    `monthly_mid` 是唯一进排序键的 —— 总包里有 47% 是估的,
    把 inferred 塞进排序键会让排名不可复算。
    """
    lo, hi = job.get("salary_min"), job.get("salary_max")
    txt = job.get("salary_text") or ""
    months = job.get("salary_months")
    src = "structured"
    if not months:
        m = re.search(r"[·・]\s*(\d{2})\s*薪", txt) or re.search(r"(\d{2})\s*薪", txt)
        if m:
            months, src = int(m.group(1)), "regex"
    if not months:
        months, src = 12, "assumed_12"

    mid = (lo + hi) / 2 if (lo is not None and hi is not None) else (lo or hi)
    return {
        "monthly_min": lo, "monthly_max": hi, "monthly_mid": mid,
        "months": months, "months_src": src,
        "annual_min": (lo * months / 10) if lo else None,      # 万元
        "annual_max": (hi * months / 10) if hi else None,
        "annual_kind": "measured" if src != "assumed_12" else "inferred",
        "text": txt,
    }


def gate(job: dict[str, Any], prefs: dict[str, Any]) -> dict[str, Any]:
    """硬门槛。→ {pass, hard_fail[], hard_unknown[], items[], parse_failures[]}

    `pass` 的定义是「**没有任何一项 fail**」—— unknown 和 na 都不算 fail。
    因为 unknown 是「数据还没到」,拿它当不合格等于因为我没抓到而否掉一个岗位。
    """
    items: list[dict[str, Any]] = []
    fails: list[str] = []
    unknowns: list[str] = []
    parse_fail: list[str] = []

    def add(name: str, verdict: str, need: Any, got: Any, note: str = "") -> None:
        items.append({"name": name, "verdict": verdict,
                      "need": need, "got": got, "note": note})
        if verdict == FAIL:
            fails.append(name)
        elif verdict == UNKNOWN:
            unknowns.append(name)

    # ── ① 城市。远程先判 ──
    hay = f"{job.get('title') or ''} {job.get('jd') or ''}"
    rm = REMOTE_RE.search(hay)
    city = job.get("city")
    want_cities = prefs.get("cities") or []
    if rm:
        add("city", NA, want_cities, city, f"远程/不限地点(原文「{rm.group()}」)")
    elif not city:
        add("city", UNKNOWN, want_cities, None, "岗位没写城市")
    elif not want_cities:
        add("city", UNKNOWN, None, city, "我没填期望城市")
    elif any(w and w in city for w in want_cities):
        add("city", PASS, want_cities, city)
    else:
        add("city", FAIL, want_cities, city)

    # ── ② 经验 ──
    lo, hi, form = parse_exp(job.get("experience"))
    my = prefs.get("years_exp")
    if form == "unparsed":
        add("experience", UNKNOWN, None, job.get("experience"), "读不懂,不当成符合")
        if job.get("experience"):
            parse_fail.append(str(job["experience"]))
    elif my is None:
        add("experience", UNKNOWN, job.get("experience"), None, "我没填工作年限")
    elif lo is None or my >= lo:
        # 上界超出只提示,不算不合格 —— 国内 JD 的经验上界几乎不当门槛用
        over = hi is not None and my > hi + 2
        add("experience", PASS, job.get("experience"), my,
            "可能被判超配" if over else "")
    else:
        add("experience", FAIL, job.get("experience"), my, f"要 {lo} 年起")

    # ── ③ 学历 ──
    need = degree_rank(job.get("degree"))
    mine = degree_rank(prefs.get("degree"))
    if need is None:
        add("degree", UNKNOWN, job.get("degree"), prefs.get("degree"), "岗位没写学历")
    elif mine is None:
        add("degree", UNKNOWN, job.get("degree"), None, "我没填学历")
    elif mine >= need:
        add("degree", PASS, job.get("degree"), prefs.get("degree"))
    else:
        add("degree", FAIL, job.get("degree"), prefs.get("degree"))

    # ── ④ 薪资。**用 max 不用 min** ──
    sv = salary_view(job)
    floor = prefs.get("salary_floor")
    if floor is None:
        add("salary", UNKNOWN, None, sv["text"], "我没填薪资底线")
    elif sv["monthly_max"] is None:
        add("salary", UNKNOWN, floor, sv["text"], "岗位没写薪资")
    elif sv["monthly_max"] >= floor:
        # BOSS 的区间是可谈范围。用 min 判会把「20-50K」在 floor=30 时误判为不合格 ——
        # 而那明显是个够得到的岗位,只是要谈。
        stretch = sv["monthly_min"] is not None and sv["monthly_min"] < floor
        add("salary", PASS, floor, sv["text"], "够得到但要谈" if stretch else "")
    else:
        add("salary", FAIL, floor, sv["text"])

    return {"pass": not fails, "hard_fail": fails, "hard_unknown": unknowns,
            "items": items, "parse_failures": parse_fail, "salary": sv,
            "remote": bool(rm)}


# ══════════════════════════════════════════════════════════════
# 第 2 层:技能匹配(measured)
# ══════════════════════════════════════════════════════════════
#
# **不用 LLM。** 这一层进排序键,而排序键必须可复算:同一个库跑两次结果一样。
# LLM 不保证这个。所以走「词表 + 归一化 + 别名 + 向量兜底」。

# 岗位技能的三个来源,置信度不同。
#   tags   LLM 从页面抽的技能词    conf 1.0
#   jd     JD 正文里词表命中       conf 0.6
#   title  岗位名里的类型词         conf 0.6
#
# **为什么必须用 JD 而不只用 tags**:实测 227 个岗位里 199 个有 tags(87%),
# 但 tags 总共只有约 1900 条关联;而 198 条 JD 加起来十几万字。
# 更关键的是那 28 个没有 tags 的岗位里有一部分是有 JD 的 ——
# 只看 tags 会把它们判成「零技能要求」,然后覆盖率算出 100% 或 None,两种都是错的。
SRC_CONF = {"tags": 1.0, "jd": 0.6, "title": 0.6}


def _jd_hits(text: str, vocab: set[str]) -> set[str]:
    """在正文里找词表命中。

    ⚠️ **短的 ASCII 词必须要求词边界。** 不然 `ai` 会命中 "email"、`go` 命中 "google"、
    `r` 命中任何一个字母 r。中文不需要边界(中文没有词边界这回事)。
    长度 <2 的 ASCII 词一律不进词表 —— 那种词的误命中率高到没有信息量。
    """
    low = text.lower()
    hit = set()
    for v in vocab:
        if not v:
            continue
        if v.isascii():
            if len(v) < 2:
                continue
            if len(v) < 3:
                if re.search(rf"(?<![a-z0-9]){re.escape(v)}(?![a-z0-9])", low):
                    hit.add(v)
            elif v in low:
                hit.add(v)
        else:
            if v in low:
                hit.add(v)
    return hit


def job_skills(job: dict[str, Any], vocab: set[str]) -> dict[str, Any]:
    """岗位要求的技能集。→ {tokens, evidence, n_tech}

    `evidence` ∈ tags+jd | tags | jd | title_only | none —— **它决定覆盖率能不能算**。
    """
    toks: dict[str, dict[str, Any]] = {}

    def put(raw: str, src: str) -> None:
        key, how = canon(raw)
        if not key:
            return
        kind = skill_kind(raw)
        cur = toks.get(key)
        conf = SRC_CONF[src]
        if cur is None or conf > cur["conf"]:
            toks[key] = {"tok": raw, "canon": key, "kind": kind,
                         "src": src, "conf": conf, "how": how}

    tags = job.get("tags")
    if isinstance(tags, str):
        try:
            tags = json.loads(tags or "[]")
        except ValueError:
            tags = []
    has_tags = bool(tags)
    for t in (tags or []):
        put(str(t), "tags")

    jd = job.get("jd") or ""
    has_jd = bool(jd.strip())
    if has_jd:
        for v in _jd_hits(jd, vocab):
            put(v, "jd")

    title = job.get("title") or ""
    for v in _jd_hits(title, vocab):
        put(v, "title")

    evidence = ("tags+jd" if has_tags and has_jd else
                "tags" if has_tags else
                "jd" if has_jd else
                "title_only" if toks else "none")
    n_tech = sum(1 for t in toks.values() if in_coverage(t["kind"]))
    return {"tokens": list(toks.values()), "evidence": evidence, "n_tech": n_tech}


# ══════════════════════════════════════════════════════════════
# **覆盖率不用语义相似度。** 这是实测推翻设计之后的结论。
#
# 扫了 15 对已知答案的技能词,发现「该算命中」和「该算不同」在余弦空间里
# **交错分布**,没有任何阈值能分开:
#
#     0.736  swiftui ↔ swift          应命中
#     0.748  mysql ↔ postgresql       应分开   ← 比上面那个还高
#     0.760  coreanimation ↔ coredata 应分开
#     0.761  gitlab ↔ git             应命中   ← 又反过来
#     0.797  os ↔ ios                 应分开
#     0.838  github ↔ git             应命中
#
# 根因:bge-m3 量的是**话题相关**,而覆盖率要的是**技能可替代**。
# MySQL 和 PostgreSQL 话题上极近(都是关系库)、技能上完全不可互换;
# Core Data 和 Core Animation 都是 Apple 框架、做的事毫无关系。
# 这两件事不是同一个东西,拿话题相关当可替代性,方向还恰好偏向「虚高」——
# 覆盖率好看,而好看的方向没人会去查。
#
# 所以改成:精确 → 别名 → **包含 + 阻止表**。全是确定性规则,
# 同一个库跑两次必然一样(排序键的硬要求),而且**不需要加载模型** ——
# 覆盖率因此能在毫秒级算完。
#
# 语义还留着,但**只作为「差多少」的参考信息**放进 missing[].near,
# **不计入命中**。「你有 coredata,这个岗位要 coreanimation,余弦 0.76」
# 对人是有用的提示,对分数不该有贡献。

# 包含规则要求「我的词」至少这么长。
#
# **3 是扫出来的:**
#   4 → 挡掉了 `ios`(→ `ios开发`)和 `git`(→ `github`/`gitlab`),3 个真命中丢了
#   3 → 上面那些都对;而 2 字符的仍被挡住 —— 必须挡,因为
#        `go` 在 `mongodb`/`django` 里、`ai` 在 `email`/`chain` 里、`os` 在 `ios` 里,
#        那些包含全是噪音
_CONTAIN_MIN = 3

# **包含规则会错的那些对。** 显式列出,因为它们正是最经典的陷阱:
# `javascript` 包含 `java`,但会 Java 不等于会 JavaScript。
_NOT_SUBSTITUTABLE: set[tuple[str, str]] = {
    ("javascript", "java"),
    ("reactnative", "react"),      # 会 React 不等于会 React Native
    ("typescript", "type"),
    ("kotlin", "koa"),
    ("nodejs", "node"),            # node 太泛
    ("swiftui", "ui"),
    ("nextjs", "next"),
    ("nestjs", "nest"),
}


def _contains_ok(job_tok: str, my_tok: str) -> bool:
    """「岗位要的词」包含「我会的词」→ 算同一族。

    方向很要紧:`xcodeinstruments` 包含 `xcode` ✓,而 `os` **不**包含 `ios` ✓
    (所以 os↔ios 天然被拒,不用进阻止表)。
    """
    if len(my_tok) < _CONTAIN_MIN or my_tok == job_tok:
        return False
    if (job_tok, my_tok) in _NOT_SUBSTITUTABLE:
        return False
    return my_tok in job_tok


# 覆盖率算不到、但想给人看的「差多少」用这个阈值。**不影响分数。**
NEAR_TAU = 0.72


def coverage(my_skills: list[str], tokens: list[dict[str, Any]],
             emb: Any = None, near_tau: float = NEAR_TAU,
             evidence: str = "tags+jd") -> dict[str, Any]:
    """技能覆盖率。→ {rate, rate_confidence, matched, missing, n_job_tech, …}

    **`rate` 在没有技能证据时返回 `None`,不是 0。**
    返回 0 会让这些岗位排到最后、看起来像「不匹配」,而**这个错误会自我实现**:
    排最后就不看,不看就永远不会去补详情页,于是它永远是 0。

    **`rate_confidence`**:分母 <3 时是 `low`。
    实测按 rate 排序时,前 8 名全是 `(1/1)` `(2/2)` —— 一个岗位只抽出 1 个技能词
    而我正好有,rate 就是 100%。那不是「完美匹配」,是**分母太小**。
    不标出来的话,排序会把最没信息量的岗位顶到最前面。

    `emb` 只用来算 missing 里的「差多少」,**不参与命中判定** —— 传 None 也能算,
    结果完全一样。
    """
    job_tech = [t for t in tokens if in_coverage(t["kind"])]
    qual = [t for t in tokens if not in_coverage(t["kind"])]

    if evidence in ("none", "title_only") or not job_tech:
        return {"rate": None, "rate_confidence": "none", "matched": [], "missing": [],
                "qualitative": qual, "n_job_tech": len(job_tech),
                "denominator_note": "这条岗位没有技能证据(还没抓到详情页),"
                                    "覆盖率无法计算 —— 不是 0"}

    mine: dict[str, str] = {}
    for sk in my_skills or []:
        k, how = canon(sk)
        if k:
            mine[k] = how

    matched, missing = [], []
    for t in job_tech:
        k = t["canon"]
        if k in mine:
            how = "alias" if (t["how"] == "alias" or mine[k] == "alias") else "exact"
            matched.append({**t, "my_tok": k, "score": 1.0, "how": how})
            continue
        hit = next((m for m in sorted(mine, key=len, reverse=True)
                    if _contains_ok(k, m)), None)
        if hit:
            matched.append({**t, "my_tok": hit, "score": 1.0, "how": "contains"})
        else:
            missing.append({**t})

    # 「差多少」——**只是给人看的参考,不算命中**
    if missing and emb is not None and mine:
        my_keys = sorted(mine)
        mv = emb.encode_docs(my_keys)
        jv = emb.encode_docs([m["canon"] for m in missing])
        sims = jv @ mv.T
        for i, m in enumerate(missing):
            j = int(sims[i].argmax())
            sc = float(sims[i][j])
            m["near"] = {"my_tok": my_keys[j], "score": round(sc, 4)} if sc >= near_tau else None

    n = len(job_tech)
    return {
        "rate": round(len(matched) / n, 4),
        # 分母太小的 rate 不可信 —— 排序时别让它进 Top N
        "rate_confidence": "low" if n < 3 else "high",
        "matched": matched, "missing": missing, "qualitative": qual,
        "n_job_tech": n,
        "denominator_note": f"分母 = 该岗位 tech+tool 去重后 {n} 个"
                            f"(另有 {len(qual)} 个定性要求不计入)"
                            + ("  ⚠️ 分母 <3,这个百分比不可靠" if n < 3 else ""),
    }


def build_vocab(jobs: list[dict[str, Any]], extra: set[str] | None = None) -> set[str]:
    """全库技能词表 —— JD 正文匹配用它。

    只收 tech/tool,而且**只收长度 ≥2 的**(见 _jd_hits 的说明)。
    """
    vocab: set[str] = set(extra or ())
    for j in jobs:
        tags = j.get("tags")
        if isinstance(tags, str):
            try:
                tags = json.loads(tags or "[]")
            except ValueError:
                tags = []
        for t in (tags or []):
            if in_coverage(skill_kind(str(t))):
                k, _ = canon(str(t))
                if len(k) >= 2:
                    vocab.add(k)
    return vocab


# 出现在这么大比例的岗位里,就算不上「缺口」了 ——
# 一个半数岗位都要的词,要么是普遍门槛(学它不是一个离散动作),
# 要么还是套话。**标出来而不是删掉**:判断该被看见。
GENERIC_SHARE = 0.40


def gaps(my_skills: list[str], jobs_cov: list[tuple[dict[str, Any], dict[str, Any]]],
         min_jobs: int = 8) -> dict[str, Any]:
    """缺口清单 —— **最可行动的那个输出**。

    入参是 [(job, coverage 结果), …]。只统计**有技能证据**的岗位。

    → {items:[{tok,kind,n_jobs,share,generic,avg_salary,in_top_salary,examples}], denominator}

    三条口径都能骗人,所以都写进返回体:
      · **分母是「有证据的岗位数」,不是全库。** 写全库会让每个 share 都被稀释,
        而且没人会注意到。
      · `generic`:出现在 ≥40% 岗位里的词。**标记不删除** —— 判断该被看见。
      · `in_top_salary`:这个缺口词在「月薪中点前 1/3 的岗位」里出现几次。
        把缺口按**值钱程度**排,而不只按频次 —— 这一步把清单变成决策。
    """
    ev = [(j, c) for j, c in jobs_cov if c.get("rate") is not None]
    n_ev = len(ev)
    if not n_ev:
        return {"items": [], "denominator": {"jobs_considered": len(jobs_cov),
                                             "jobs_with_evidence": 0}}

    mids = sorted((salary_view(j)["monthly_mid"] or 0) for j, _ in ev)
    cut = mids[int(len(mids) * 2 / 3)] if mids else 0

    agg: dict[str, dict[str, Any]] = {}
    for j, c in ev:
        mid = salary_view(j)["monthly_mid"] or 0
        for m in c["missing"]:
            k = m["canon"]
            a = agg.setdefault(k, {"tok": m["tok"], "kind": m["kind"], "n_jobs": 0,
                                   "sal": [], "top": 0, "examples": []})
            a["n_jobs"] += 1
            if mid:
                a["sal"].append(mid)
            if mid >= cut:
                a["top"] += 1
            if len(a["examples"]) < 3:
                # **每个缺口都要能点开看原文出处。**
                # 最容易骗人的是「这个词其实是从 JD 里的『我们用 Kotlin 但你不需要会』
                # 抽出来的」—— 不给出处就永远发现不了。
                a["examples"].append({"job_id": j.get("job_id"),
                                      "title": j.get("title"),
                                      "src": m.get("src")})

    items = []
    for k, a in agg.items():
        if a["n_jobs"] < min_jobs:
            continue
        share = a["n_jobs"] / n_ev
        items.append({
            "tok": k, "raw": a["tok"], "kind": a["kind"],
            "n_jobs": a["n_jobs"], "share": round(share, 4),
            "generic": share >= GENERIC_SHARE,
            "avg_salary": round(sum(a["sal"]) / len(a["sal"]), 1) if a["sal"] else None,
            "in_top_salary": a["top"],
            "examples": a["examples"],
        })
    # 非 generic 的排前面;组内按「在高薪岗位里出现几次」排 —— 值钱的缺口先看
    items.sort(key=lambda x: (x["generic"], -x["in_top_salary"], -x["n_jobs"]))
    return {"items": items,
            "denominator": {"jobs_considered": len(jobs_cov),
                            "jobs_with_evidence": n_ev,
                            "note": f"分母是 {n_ev} 个**有技能证据**的岗位,"
                                    f"不是全部 {len(jobs_cov)} 个",
                            "top_salary_cut": cut}}
