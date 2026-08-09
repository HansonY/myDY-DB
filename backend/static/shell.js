/* 全站共享外壳:导航 + 几个每页都在用的小工具。
 *
 * 导航写在一处而不是每页各抄一份 —— 原来就是各抄一份,结果四个页面的
 * 返回方式全不一样(「← 回浏览」/「选人 →」/顶部按钮),而且加一个页面
 * 要改四个文件,必然漏。
 *
 * 用法:页面里 <script src="/shell.js"></script> 然后
 *       mountNav('today')  —— 参数是当前页的 key
 */
'use strict';

/* 四个内容 tab 对应用户的三个需求(情报拆成简报+竞品两个),
   顺序照高保真稿:浏览 打头(打开就能翻),情报类在后。
   维护(采集/转写/索引)推到最右、做成边框按钮 —— 它是运维,
   不该和日常动作抢注意力,但要能一眼找到。 */
const NAV = [
  {key: 'browse',  href: '/',             label: '浏览',     title: '浏览和搜索所有抓来的内容'},
  {key: 'insight', href: '/insight.html', label: '认识自己', title: '从我做过的收藏和点赞里看我自己'},
  {key: 'digest',  href: '/digest.html',  label: '每日简报', title: '信息价值博主这几天讲了什么'},
  {key: 'rival',   href: '/rival.html',   label: '竞品',     title: '竞品博主的打法,对比上期'},
];

function mountNav(current) {
  const el = document.createElement('nav');
  el.className = 'nav';
  el.innerHTML =
    // local only:数据只在本机,是这个产品的核心承诺,常驻显示
    `<a class="brand" href="/"><b>Douyin-DB</b><span>local only</span></a>` +
    `<div class="tabs">` +
    NAV.map(n =>
      `<a href="${n.href}" title="${n.title}"` +
      `${n.key === current ? ' aria-current="page"' : ''}>${n.label}</a>`).join('') +
    `</div>` +
    `<span class="gap"></span>` +
    // 只读模式是安全承诺(绝不评论/点赞/关注)。同步时间由页面按需填(navSync),
    // 拿不到就只显示「只读模式」,不写死假时间。
    `<span class="status" id="nav-status"><span class="dot"></span>` +
    `<span id="nav-sync">只读模式</span></span>` +
    `<a class="maint" href="/data.html" title="采集、补齐、转写、索引">维护</a>`;
  document.body.insertBefore(el, document.body.firstChild);
}

/** 在顶栏显示「只读模式 · N 小时前同步」。传入最近一次采集时间(ISO)。 */
function navSync(lastIso) {
  const box = document.getElementById('nav-status');
  if (!box || !lastIso) return;
  const t = new Date(lastIso), now = new Date();
  const h = Math.round((now - t) / 3.6e6);
  const ago = h < 1 ? '刚刚' : h < 24 ? h + ' 小时前' : Math.round(h / 24) + ' 天前';
  document.getElementById('nav-sync').textContent = `只读模式 · ${ago}同步`;
}

/* ── 每页都要的三个小工具 ────────────────────────────────── */

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g,
    m => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[m]));
}

const fmt = n => (n == null ? '—' : Number(n).toLocaleString('en-US'));

/** 秒/毫秒 → 人能读的时长 */
function dur(ms) {
  const s = Math.round((ms || 0) / 1000);
  if (s < 60) return s + '秒';
  return Math.floor(s / 60) + '分' + String(s % 60).padStart(2, '0') + '秒';
}

/** fetch + 统一的错误拆包。
 *  FastAPI 的 422 detail 是**数组**,直接模板化会变成 [object Object] —— 踩过。 */
async function api(url, opt) {
  const r = await fetch(url, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = j.detail;
    throw new Error(
      Array.isArray(d) ? d.map(x => x.msg || JSON.stringify(x)).join('; ')
      : typeof d === 'string' ? d
      : JSON.stringify(j));
  }
  return j;
}

/** POST JSON 的快捷方式 */
const postJSON = (url, body) => api(url, {
  method: 'POST', headers: {'Content-Type': 'application/json'},
  body: JSON.stringify(body),
});

/** 状态行。warn=true 用警示色 —— 只给「有代价 / 出问题」用,别滥用 */
function say(sel, text, warn) {
  const el = typeof sel === 'string' ? $(sel) : sel;
  if (!el) return;
  el.textContent = text || '';
  el.style.color = warn ? 'var(--warn)' : 'var(--dim2)';
}
