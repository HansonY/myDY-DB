"""把录制到的响应摊开看:哪个接口、有多少条、字段叫什么。

录制器只负责如实落盘,不做任何解释。解析要照着**真实响应**写,
这个工具就是用来读那些响应的 —— 避免又一次凭记忆猜字段名。
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import ROOT

CAPTURE_DIR = ROOT / "data" / "boss_capture"


def _walk_lists(node, path="", out=None):
    """找出响应里所有「对象数组」—— 列表数据基本都长这样。"""
    if out is None:
        out = []
    if isinstance(node, dict):
        for k, v in node.items():
            _walk_lists(v, f"{path}.{k}" if path else k, out)
    elif isinstance(node, list) and node and isinstance(node[0], dict):
        out.append((path, node))
    return out


def main() -> int:
    if not CAPTURE_DIR.exists() or not any(CAPTURE_DIR.glob("*.json")):
        print(f"还没有录制数据({CAPTURE_DIR})。先跑:./boss.sh record")
        return 1

    files = sorted(CAPTURE_DIR.glob("*.json"))
    print(f"录制文件 {len(files)} 个\n")

    # 两种来源都认:
    #   · 录制器落的单文件           {"url":…, "body":…}
    #   · 浏览器控制台片段下载的合集  [{"url":…, "body":…}, …]
    recs: list[dict] = []
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        recs.extend(d if isinstance(d, list) else [d])
    print(f"记录 {len(recs)} 条\n")

    by_url: dict[str, list[dict]] = {}
    for d in recs:
        by_url.setdefault(d.get("url", "?"), []).append(d)

    for url, group in sorted(by_url.items(), key=lambda x: -len(x[1])):
        print("─" * 64)
        print(f"{url.replace('https://www.zhipin.com', '')}   ({len(group)} 次)")
        body = group[0].get("body")
        if isinstance(body, dict):
            print(f"  顶层键: {', '.join(list(body.keys())[:10])}")
        lists = _walk_lists(body)
        if not lists:
            print("  (没有对象数组 —— 可能不是列表接口)")
            continue
        for path, arr in sorted(lists, key=lambda x: -len(x[1]))[:3]:
            keys = Counter()
            for it in arr[:20]:
                keys.update(it.keys())
            print(f"  数组 {path or '(根)'}: {len(arr)} 条")
            print(f"    字段: {', '.join(k for k, _ in keys.most_common(28))}")
            # 给一条样例,只显示短值 —— 长文本(JD)截断,避免刷屏
            sample = {k: (str(v)[:40] + "…" if len(str(v)) > 40 else v)
                      for k, v in list(arr[0].items())[:12]}
            print(f"    样例: {json.dumps(sample, ensure_ascii=False)[:300]}")
    print("─" * 64)
    print("\n把上面这段贴给我,我照着写解析。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
