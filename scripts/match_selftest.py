#!/usr/bin/env python3
"""匹配规则的口径自检。零网络、零模型、不写库。

    .venv/bin/python scripts/match_selftest.py

**为什么要它。** 这一路的 bug 全是「不报错、不崩溃,只是数悄悄错了」那种 ——
分母少一块、门槛静默放过、真技能被当成软能力排除。指标只掉两个点,埋在噪音里。
所以把每条口径钉成断言,改完跑一遍。

**直接 import boss_match,不重抄逻辑** —— 抄一遍就变成「测试我抄的那份」,
改了生产代码测试还是绿的。
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))

import boss_match as bm        # noqa: E402

R: list[tuple[bool, str]] = []


def ok(cond: bool, label: str) -> None:
    R.append((bool(cond), label))


ME = {"cities": ["上海"], "years_exp": 7.5, "degree": "本科", "salary_floor": 35,
      "skills": ["swift", "objectivec", "ios", "git", "xcode", "python"]}


# ── 归一化 ──
for raw, want in [("React Native", "reactnative"), ("react native", "reactnative"),
                  ("Node.js", "nodejs"), ("Objective-C", "objectivec"),
                  ("CI/CD", "cicd")]:
    ok(bm.norm_skill(raw) == want, f"norm {raw!r} → {want!r}")
for raw, want in [("oc", "objectivec"), ("objectc", "objectivec"), ("rn", "reactnative"),
                  ("大模型", "llm"), ("k8s", "kubernetes")]:
    ok(bm.canon(raw)[0] == want and bm.canon(raw)[1] == "alias", f"alias {raw!r} → {want!r}")

# ── 分型:**英文技术名不管多长都是 tech** ──
for raw in ("Objective-C", "TypeScript", "React Native", "Spring Boot",
            "AVFoundation", "Jetpack Compose", "Prompt Engineering", "PostgreSQL"):
    ok(bm.skill_kind(raw) == "tech", f"英文技术名 {raw!r} → tech(不是 soft)")
# 中文技术名词也是 tech
for raw in ("推荐系统", "音视频", "风控", "知识图谱"):
    ok(bm.skill_kind(raw) == "tech", f"中文技术名 {raw!r} → tech")
# 活动 / 软能力 / 学历 —— 都不进覆盖率
for raw in ("架构设计", "性能优化", "交付", "用户体验"):
    ok(bm.skill_kind(raw) == "activity", f"JD 套话 {raw!r} → activity")
for raw in ("沟通能力", "跨部门协同", "独立开发且上线经验"):
    ok(bm.skill_kind(raw) == "soft", f"软能力 {raw!r} → soft")
ok(bm.skill_kind("计算机相关专业") == "activity", "学历要求「计算机相关专业」不算技能")
for raw in ("架构设计", "沟通能力", "电商", "计算机相关专业"):
    ok(not bm.in_coverage(bm.skill_kind(raw)), f"{raw!r} 不进覆盖率分母")
for raw in ("swift", "cursor"):
    ok(bm.in_coverage(bm.skill_kind(raw)), f"{raw!r} 进覆盖率分母")

# ── 包含规则:2 字符必须挡 ──
for a, b, want in [("ios开发", "ios", True), ("github", "git", True),
                   ("swiftui", "swift", True), ("xcodeinstruments", "xcode", True),
                   ("mongodb", "go", False), ("django", "go", False),
                   ("email", "ai", False), ("os", "ios", False),
                   ("javascript", "java", False), ("reactnative", "react", False),
                   ("coreanimation", "coredata", False), ("mysql", "postgresql", False)]:
    ok(bm._contains_ok(a, b) is want,
       f"包含 「{a}」←「{b}」 {'命中' if want else '不命中'}")

# ── 经验:**解析失败绝不算通过** ──
ok(bm.parse_exp("3-5年") == (3, 5, "range"), "parse_exp('3-5年')")
ok(bm.parse_exp("10年以上") == (10, None, "min"), "parse_exp('10年以上')")
ok(bm.parse_exp("经验不限") == (None, None, "any"), "parse_exp('经验不限')")
ok(bm.parse_exp("五年以上")[2] == "unparsed", "中文数字读不懂 → unparsed")
# ★ 库里真实存在的陷阱:那是实习排班,不是经验要求
ok(bm.parse_exp("5天/周3个月")[2] == "unparsed", "★ '5天/周3个月' → unparsed(不是「不限」)")
g = bm.gate({"city": "上海", "experience": "5天/周3个月", "degree": "本科",
             "salary_min": 40, "salary_max": 50}, ME)
e = {i["name"]: i for i in g["items"]}["experience"]
ok(e["verdict"] == bm.UNKNOWN, "★ 读不懂 → 门槛判 unknown,**不判 pass**")
ok("5天/周3个月" in g["parse_failures"], "★ 原文进 parse_failures(口径预警)")
ok(g["pass"] and "experience" in g["hard_unknown"], "unknown 不算 fail,但单独计数")

# ── 学历 ──
ok(bm.degree_rank("本科以上") == 3, "degree_rank('本科以上') 剥「以上」")
ok(bm.degree_rank("乱写") is None, "读不懂 → None")

# ── 城市:★ 远程必须在城市之前判 ──
g = bm.gate({"title": "AI Builder - 全职远程", "city": "绵阳",
             "jd": "全职远程:工作地点就在家里", "experience": "3-5年",
             "degree": "本科", "salary_min": 20, "salary_max": 40}, ME)
c = {i["name"]: i for i in g["items"]}["city"]
ok(c["verdict"] == bm.NA, "★ 非期望城市的**远程**岗 → city=na,不是 fail")
ok(g["pass"], "★ 远程岗整体通过(全库唯一地理兼容的那个不能被判掉)")
ok(g["remote"], "remote 标记")
g2 = bm.gate({"title": "iOS", "city": "深圳", "jd": "岗位职责", "experience": "3-5年",
              "degree": "本科", "salary_min": 30, "salary_max": 50}, ME)
ok(not g2["pass"], "非远程 + 非期望城市 → 不通过")

# ── 薪资:三个口径 ──
sv = bm.salary_view({"salary_min": 20, "salary_max": 35, "salary_months": 14,
                     "salary_text": "20-35K·14薪"})
ok(sv["annual_kind"] == "measured", "有几薪 → 年包 measured")
sv2 = bm.salary_view({"salary_min": 20, "salary_max": 35, "salary_months": None,
                      "salary_text": "20-35K"})
ok(sv2["months_src"] == "assumed_12" and sv2["annual_kind"] == "inferred",
   "★ 没几薪 → assumed_12 且年包标 **inferred**(不能当 measured 报)")
sv3 = bm.salary_view({"salary_min": 20, "salary_max": 35, "salary_months": None,
                      "salary_text": "20-35K·16薪"})
ok(sv3["months"] == 16 and sv3["months_src"] == "regex", "结构化列空 → 从原文正则捞")
g3 = bm.gate({"city": "上海", "experience": "3-5年", "degree": "本科",
              "salary_min": 20, "salary_max": 50, "salary_text": "20-50K"}, ME)
s3 = {i["name"]: i for i in g3["items"]}["salary"]
ok(s3["verdict"] == bm.PASS and "要谈" in s3["note"],
   "★ 20-50K vs 底线 35 → pass(用 max 判,不是 min)+ 标「要谈」")

# ── 覆盖率:★ 没证据返回 None 不是 0 ──
sk = bm.job_skills({"title": "iOS", "tags": None, "jd": None}, set())
cov = bm.coverage(ME["skills"], sk["tokens"], emb=None, evidence=sk["evidence"])
ok(cov["rate"] is None, "★ 没有技能证据 → rate=None,**不是 0**")
ok(cov["rate_confidence"] == "none", "  可信度标 none")
# 分母 <3 → low
sk2 = bm.job_skills({"title": "x", "tags": ["Swift", "UIKit"], "jd": "x"}, set())
cov2 = bm.coverage(ME["skills"], sk2["tokens"], emb=None, evidence=sk2["evidence"])
ok(cov2["rate_confidence"] == "low", "★ 分母 <3 → rate_confidence=low(百分比不可靠)")
ok("分母 <3" in cov2["denominator_note"], "  note 里写明了")
# 覆盖率不需要模型
sk3 = bm.job_skills({"title": "iOS", "tags": ["Swift", "Objective-C", "UIKit", "Kotlin"],
                     "jd": "熟悉 Swift 和 Xcode"}, {"swift", "xcode", "kotlin"})
cov3 = bm.coverage(ME["skills"], sk3["tokens"], emb=None, evidence=sk3["evidence"])
ok(cov3["rate"] is not None and all(m["how"] in ("exact", "alias", "contains")
                                    for m in cov3["matched"]),
   "★ 覆盖率**零模型**可算,命中方式全是确定性规则")
ok(any(m["canon"] == "kotlin" for m in cov3["missing"]), "  kotlin 算缺口(我不会)")

bad = [x for x in R if not x[0]]
for good, label in R:
    print(("  ✓ " if good else "  ✗ ") + label)
print(f"\n  {len(R) - len(bad)}/{len(R)} 通过"
      + ("" if not bad else f"  ← {len(bad)} 项失败"))
sys.exit(1 if bad else 0)
