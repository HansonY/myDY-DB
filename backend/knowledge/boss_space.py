"""BOSS 岗位库的适配器。**内核认识的全部业务差异都在这一个文件里。**

和抖音那份(`knowledge/space.py`)对着看 —— 四行关键声明
(name / db / owner_table / init_db)刻意写在一起,肉眼可比。
`WrongDatabase` 断言抓得住「这份配置指向了抖音库」(那儿没有 jobs 表),
但抓不住反方向(douyin.db 里 videos 一直在),所以那一侧只能靠 review。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from db import boss_store as bs
from kb.space import Space

SYSTEM = """你在回答用户关于「他自己浏览过的招聘岗位」的问题。

规则:
1. 只用下面给出的资料回答。资料里没有的,直说没有,不要用常识补。
2. 每个论断后面标出处编号,如 [1]、[2][3]。没有出处的话不要写。
3. 资料是岗位页面的原文片段(职位描述为主),可能只抓到了列表页那部分 ——
   信息不足就说明还缺什么,不要替招聘方补充要求。
4. 涉及薪资时**照抄原文**(如「20-35K·14薪」),不要自己换算成年包 ——
   「几薪」这一项库里有近四成是空的,换算出来会系统性偏低。
5. 中文回答。简洁,不写客套话。"""


def _db() -> Path:
    """⚠️ 每次现取。`boss_store.db_file()` 读的是 `BOSS_DB_PATH` 环境变量,
    import 时捕获成 Path 等于把路径冻结在进程启动那一刻。"""
    return bs.db_file()


def _context_head(i: int, it: dict[str, Any]) -> str:
    """资料抬头。岗位没有 start_sec,换成公司 / 城市 / 薪资。

    刻意把 `jd_state != have` 标出来:这条资料信息少可能是「还没打开过详情页」,
    不是「这个岗位没写要求」。不标的话模型会把「我没抓到」讲成「岗位没要求」。
    """
    head = f"[{i}] {(it.get('title') or '')[:40]}"
    bits = [x for x in (it.get("company"), it.get("city"), it.get("salary")) if x]
    if bits:
        head += " · " + " · ".join(str(b) for b in bits)
    if it.get("jd_state") != bs.JD_HAVE:
        head += " ⚠️ 还没抓到职位描述"
    if it.get("job_state") == bs.JOB_CLOSED:
        head += " ⚠️ 岗位已关闭"
    head += f"(相关度 {it['score']})"
    return head


BOSS_SPACE = Space(
    name="boss",
    db=_db,
    owner_table="jobs",
    init_db=bs.init_db,
    fetch_meta=bs.get_jobs_meta,
    id_key="job_id",
    scope_sql=bs.scope_pred,
    scopes=("all", "applied", "saved", "chatted", "viewed", "open"),
    # **必须是 all,和抖音的 mine 相反。**
    # 抖音默认 mine 是因为关注者的产出会淹没我的收藏;BOSS 没有这个污染源,
    # 而 interactions 现在只有 3 行 —— 默认 applied 会让检索**永远返回空**,
    # 而且长得和「库里没有」一模一样。
    default_scope="all",
    citation_fields=("company", "title", "url", "city", "salary"),
    context_head=_context_head,
    system_prompt=SYSTEM,
    meta={"build_cmd": "scripts/boss_index.py"},
)
