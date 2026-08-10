#!/usr/bin/env python3
"""「指一下下一页」这套逻辑的自检 —— 生成一个测试页,在浏览器里跑。

**为什么要它。** BOSS 的分页器改版之后,这套「学定位 → 照着点」很可能失效,
而失效的方式很隐蔽:它会一遍遍抓同一页,然后报告「翻了 10 页」。
这个脚本用一个合成的分页器把整条链路走一遍,包含最容易踩的那几种情形。

**关键设计:把 background.js 里的函数原样抠出来嵌进测试页**,不重新抄一遍 ——
抄一遍就变成「测试我抄的那份」,改了代码测试还是绿的,毫无意义。

用法:
    python3 scripts/pager_selftest.py        # 生成到 /tmp 并打印 URL
    # 然后 ./boss.sh web,浏览器打开那个 URL,控制台执行 __test()
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BG = ROOT / "extension" / "background.js"
WANT = ["armPickerInPage", "clickLearnedInPage", "pageFingerprintInPage", "clickNextInPage"]


def grab(src: str, name: str) -> str:
    """抠出一个顶层函数的完整源码(数花括号,不用正则硬啃函数体)。"""
    m = re.search(rf"^function {re.escape(name)}\([^)]*\)\s*\{{", src, re.M)
    if not m:
        sys.exit(f"✗ background.js 里找不到 {name}() —— 名字改了?")
    depth = 0
    for j in range(m.end() - 1, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    sys.exit(f"✗ {name}() 花括号不平衡")


FIXTURE = """
<div class="job-list" id="list">
  <a href="/job_detail/p1a.html?securityId=A">岗位 1</a>
  <a href="/job_detail/p1b.html?securityId=B">岗位 2</a>
</div>
<!-- 文字在 <span> 里,真正可点的是外面的 <a> —— 最容易踩的坑 -->
<div class="options-pages">
  <a href="javascript:;" class="prev">上一页</a>
  <a href="javascript:;" class="cur">6</a>
  <a href="javascript:;" class="page-item">7</a>
  <a href="javascript:;" class="next options-pages-next" ka="page-next"><span class="txt">下一页</span></a>
</div>
<!-- 干扰项:页脚也写着「下一页」,但是个不可点的 div -->
<div class="footer-tip"><div>下一页</div></div>
<script>
let page = 6;
document.querySelector('.next').addEventListener('click', () => {
  page++;
  document.getElementById('list').innerHTML =
    `<a href="/job_detail/p${page}a.html?securityId=X">岗位 ${page}-1</a>` +
    `<a href="/job_detail/p${page}b.html?securityId=Y">岗位 ${page}-2</a>`;
});
</script>
"""

CHECKS = r"""
window.__test = () => {
  const L = []; const ok = (c, t) => L.push((c ? '✓ ' : '✗ ') + t);
  const a = armPickerInPage();
  ok(a && a.ok, '① picker 武装');
  ok(!!document.querySelector('div[style*="2147483647"]'), '   提示条出现');

  // 模拟「点在 span 上」
  document.querySelector('.next .txt')
    .dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
  const p = window.__picked;
  ok(p && p.type === 'pickedNext', '② 捕获到点击');
  ok(p && p.text === '下一页', '   记下的文字正确');
  ok(p && (p.parentSels || []).some(s => s.includes('page-next')), '   用上了 ka 语义属性');
  ok(!document.querySelector('div[style*="2147483647"]'), '③ 监听已解除');
  window.__picked = null;
  document.querySelector('.cur').dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
  ok(window.__picked === null, '   后续点击不再被吞');

  const before = pageFingerprintInPage();
  const c = clickLearnedInPage({ sels: p.sels, parentSels: p.parentSels, text: p.text, tag: p.tag });
  ok(c && c.ok, '④ 照学到的定位点成功，用的是 ' + (c && c.used));
  ok(/\[ka=/.test(String(c && c.used)), '   优先选了语义属性，不是弱 class');
  ok(before.fp !== pageFingerprintInPage().fp, '   指纹变了 → 真的翻页了');
  ok(clickLearnedInPage({ sels: ['a.cur'], parentSels: [], text: '下一页' }).error,
     '⑤ 文字不符 → 拒绝点击');
  const g = clickNextInPage();
  ok(g && g.ok && String(g.cls).includes('next'),
     '⑥ 兜底猜法选中真按钮，不是页脚那个死 div');

  const bad = L.filter(x => x[0] === '✗').length;
  document.getElementById('RESULT').textContent =
    L.join('\n') + '\n\n' + (bad ? bad + ' 项失败' : '全部通过');
  return L.join('\n');
};
"""


def main() -> None:
    src = BG.read_text(encoding="utf-8")
    fns = "\n\n".join(grab(src, n) for n in WANT)
    html = f"""<meta charset="utf-8"><title>pager selftest</title>
<body style="font:13px/1.7 system-ui;padding:20px;background:#0a0c0f;color:#e9ecf1">
<h3>「指一下下一页」自检 —— 在控制台执行 <code>__test()</code></h3>
{FIXTURE}
<hr><pre id="RESULT" style="font-size:11px;line-height:1.8;white-space:pre-wrap"></pre>
<script>
// 桩件:注入函数里的 chrome.runtime.sendMessage 在真实环境由后台接收,这里截下来看
window.__picked = null;
window.chrome = {{ runtime: {{ sendMessage: m => {{ window.__picked = m; }} }} }};
/* ↓ 以下函数从 extension/background.js 原样抠出,不是副本 ↓ */
{fns}
/* ↑ 真实函数结束 ↑ */
{CHECKS}
</script>"""
    out = pathlib.Path("/tmp/pager_selftest.html")
    out.write_text(html, encoding="utf-8")
    print(f"✓ 已生成 {out}(嵌入了 {len(WANT)} 个真实函数)")
    print("  用浏览器打开它,控制台执行:__test()")


if __name__ == "__main__":
    main()
