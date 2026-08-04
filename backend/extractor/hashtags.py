"""从文案里抽 #话题标签。

抖音文案几乎都带 hashtag(#搞笑 #反转 #剧情),这是**免费的分类信息** ——
不调任何模型就能把上千条散装收藏变成可浏览的类目。
所以这一步一定要在上 AI 之前榨干。
"""

from __future__ import annotations

import re

# #后面跟到 空白 / 另一个# / @ 为止。中文话题没有空格分隔,所以不能按词切。
_TAG_RE = re.compile(r"#([^\s#@]+)")

# 结尾常粘上的标点,要剥掉
_TRAILING = "。，、；：！？…～·.,;:!?~-—()（）【】[]{}\"'“”‘’"

# 噪音标签:纯数字、单字符、以及平台活动类的泛标签
_STOP = {
    "抖音", "热门", "推荐", "dou上热门", "抖音小助手", "上热门", "热门推荐",
    "涨粉", "关注", "点赞", "创作灵感", "抖音创作者中心",
}


def extract(text: str | None) -> list[str]:
    """返回去重后的标签列表(保持出现顺序)。"""
    if not text:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for raw in _TAG_RE.findall(text):
        tag = raw.strip(_TRAILING).strip()
        if not tag or len(tag) < 2:          # 单字符标签没有区分度
            continue
        if tag.isdigit():                    # 纯数字多是编号,不是主题
            continue
        if tag.lower() in _STOP or tag in _STOP:
            continue
        if len(tag) > 30:                    # 异常长的多是把整句话当标签
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out
