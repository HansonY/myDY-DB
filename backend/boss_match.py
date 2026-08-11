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

import re

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
    "objectivec":  ("oc", "objc", "objective c"),
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
    """这一类算不算进覆盖率分母。**只有一处定义**,免得各处自己判。"""
    return kind in ("tech", "tool")
