"""判断一段页面文字「是不是招聘岗位页」—— 不看 URL,不看 class 名,只看内容。

**为什么不用 URL 判断。**
原来是 `/job_detail|\\/job\\//` 一条正则。它有两个方向都会错:
  · 漏判:BOSS 的岗位会出现在 /chat/、/web/geek/jobs、搜索结果、推荐流里,
    路径五花八门,漏一个就整页不存;
  · 误判:路径里带 job 的不一定是岗位页(比如「我的求职意向设置」)。
而且平台改一次路由,这条正则就废了 —— 这正是当初决定走「页面文字」而不是
「解析接口」的同一个理由:**结构会变,你看到的字不会变。**

**为什么不看 class 名。** 同上,而且更脆。

**机制:信号打分。**
招聘页有一批几乎不可能同时缺席的特征 —— 薪资范围、经验学历要求、
「岗位职责/任职要求」这种小标题、融资阶段和公司规模、五险一金。
每命中一类记一次分,够分数就认。单条信号都可能碰巧出现(某篇文章也会写
「五险一金」),但**同时出现五六类**基本只有招聘页。

**为什么要 report 命中了哪些信号。**
这个项目栽过一次:界面显示「已送入库 5」,实际 5 条全是性能监控噪音 ——
「看着像在工作比明显不工作更危险」。所以这里不只返回是/不是,
还返回**凭哪几条判的**,你在侧边栏一眼能看出它是不是在瞎认。

**唯一一份实现。** 浏览器插件不自己判断,它把文字发过来由这里判 ——
两份实现早晚会漂移,而漂移出来的 bug 最难查。
"""

from __future__ import annotations

import re
from typing import Any

# 每一类一组模式,命中任意一条算这一类得分。分类比堆正则重要:
# 同一类里多命中不该多得分(列表页会出现 20 次薪资,不代表它更像岗位页)。
SIGNALS: list[tuple[str, str, str]] = [
    # (代号, 说明, 正则)
    ("salary",  "薪资范围",   r"\d{1,3}\s*[-–~]\s*\d{1,3}\s*[Kk千]|\d{3,6}\s*[-–~]\s*\d{3,6}\s*元|薪资面议|月薪"),
    ("jd",      "职责/要求小标题", r"岗位职责|职位描述|职位详情|任职要求|任职资格|岗位要求|工作内容|职责描述|工作职责|我们希望你"),
    ("exp",     "经验学历",   r"\d+\s*[-–]\s*\d+\s*年|经验不限|\d+年以上|应届|在校|本科|大专|硕士|博士|学历不限|中专"),
    ("act",     "招聘动作",   r"立即沟通|马上沟通|继续沟通|与我沟通|投递|投个简历|发布于|在招职位|该职位|职位诱惑"),
    ("company", "公司规模/融资", r"未融资|天使轮|[A-Fa-f]\s*轮|已上市|不需要融资|战略融资|\d+\s*[-–]\s*\d+\s*人|\d+人以上|少于\d+人"),
    ("welfare", "福利",       r"五险一金|带薪年假|年终奖|双休|包吃|住房补贴|期权|绩效奖金|节日福利|定期体检|补充医疗"),
    ("hr",      "招聘方身份", r"招聘者|HR\b|人事|猎头|BOSS直聘|boss直聘|刚刚活跃|今日活跃|本周活跃|近期活跃"),
    ("loc",     "工作地点",   r"工作地址|上班地址|地铁|区\s*·|城市不限|远程办公"),
]

# 认定「是岗位页」的门槛。
#
# 为什么是 4:薪资 + 经验 + 福利 + 公司规模 这四类,任何一个招聘页都齐;
# 而一篇普通文章要同时凑齐四类几乎不可能。
# 定 3 太松(公司介绍页能过),定 5 太紧(简版岗位卡片会漏)。
# ⚠️ 这个数是**推断出来的,没在真实 BOSS 页面上标注验证过** ——
# 所以侧边栏会把命中的信号摊开给你看,发现认错了直接调这里。
MIN_SIGNALS = 4

# 列表页 vs 详情页:靠薪资出现**几次**分。
# 详情页只有一个岗位所以薪资出现一两次;列表页一屏十几个岗位。
# 用次数而不是用 URL —— 同一个路径既可能是列表也可能是详情。
LIST_SALARY_HITS = 4

_SALARY_ONE = re.compile(SIGNALS[0][2])


def classify(text: str, url: str = "", title: str = "") -> dict[str, Any]:
    """判断这段文字是什么页。

    返回 `is_job` / `kind`(detail|list|other)/ `score` / `hit`(命中的信号说明)
    / `why`(一句话解释,直接显示给人看)。

    **URL 只用来加一点旁证,不作为判据** —— 它可以为空,判断照样成立。
    """
    t = text or ""
    # 早退也必须返回**完全相同的字段集**。少给一个键,调用方就会 KeyError ——
    # 而且是只在「短页面」这种边角情形才炸,平时测不出来。
    if len(t) < 80:
        return {"is_job": False, "kind": "other", "score": 0, "hit": [],
                "miss": [lbl for _, lbl, _ in SIGNALS], "salary_hits": 0,
                "why": f"页面文字太少({len(t)} 字),没什么可提取的"}

    hit, miss = [], []
    for code, label, pat in SIGNALS:
        if re.search(pat, t):
            hit.append({"code": code, "label": label})
        else:
            miss.append(label)

    score = len(hit)
    codes = {h["code"] for h in hit}

    # 「岗位职责/任职要求」这种小标题是最强信号 —— 只有招聘页会这么写。
    # 它在场时放宽一档,免得漏掉那些没写福利、没写融资的简版岗位页。
    threshold = MIN_SIGNALS - 1 if "jd" in codes else MIN_SIGNALS
    is_job = score >= threshold and "salary" in codes

    # 薪资出现次数分列表/详情。**不看 URL** —— 只在两者难分时才用 URL 帮一把。
    n_salary = len(_SALARY_ONE.findall(t))
    if not is_job:
        kind = "other"
    elif "jd" in codes and n_salary < LIST_SALARY_HITS:
        kind = "detail"
    elif n_salary >= LIST_SALARY_HITS:
        kind = "list"
    else:
        kind = "detail" if re.search(r"job_detail", url or "") else "list"

    if is_job:
        why = (f"{'岗位详情' if kind == 'detail' else '岗位列表'} · "
               f"命中 {score} 类信号:" + "、".join(h["label"] for h in hit))
    elif "salary" not in codes:
        why = "没找到薪资范围 —— 招聘页不会不写薪资,判为不是岗位页"
    else:
        why = (f"只命中 {score} 类信号(要 {threshold} 类),缺:"
               + "、".join(miss[:4]))

    return {"is_job": is_job, "kind": kind, "score": score,
            "hit": hit, "miss": miss, "why": why, "salary_hits": n_salary}


def dedupe_key(title: str, url: str) -> str:
    """待提取队列的去重键 —— 同一个岗位反复打开只留最新一份。

    用**标题整串**当键,不解析它。BOSS 详情页的标题天然就带公司名和岗位名
    (这正是用户要的「公司 + 岗位名字」),但我**没有验证过它的确切格式**,
    所以绝不去正则拆它 —— 拆错了会把不同岗位并成一个。整串比对不用假设格式:
    同一个岗位标题一样,不同岗位标题不一样。

    标题拿不到时退回 URL(去掉查询串 —— 那里常带 securityId 之类的随机参数,
    带上就等于同一个岗位每次都算新的)。

    AI 提取之后还有一层按 公司+岗位名 的真去重(见 boss_main._job_key),
    这里只是省掉重复的 AI 调用,不是最终判定。
    """
    t = re.sub(r"\s+", " ", (title or "").strip())
    # 去掉站点后缀,不然同一岗位在不同入口标题会差一截
    t = re.sub(r"[-–|]\s*(BOSS直聘|boss直聘|BOSS\s*直聘).*$", "", t).strip()
    base = t if len(t) >= 4 else (url or "").split("?")[0]
    return re.sub(r"[^\w一-鿿]+", "_", base)[-90:] or "page"
