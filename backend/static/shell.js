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

/* 建外壳:导航固定在顶,其余内容全部塞进一个自己滚动的容器。
   页面只管写自己的内容,不用关心这层结构 —— 调一次 mountNav 就位。 */
function mountNav(current) {
  // 把 body 现有内容(页面自己的 .wrap 等)搬进滚动容器
  const app = document.createElement('div');
  app.className = 'app';
  const scroll = document.createElement('div');
  scroll.className = 'scroll';
  while (document.body.firstChild) scroll.appendChild(document.body.firstChild);

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

  app.appendChild(el);
  app.appendChild(scroll);
  document.body.appendChild(app);
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

/* ── 待抓清单 + 页内选博主 ────────────────────────────────────
   简报页和竞品页共用。两件事必须在同一屏解决,否则每次都要跳走再跳回来。 */

/** 相对时间:给水位线用(「3 小时前抓过」比一串 ISO 有用得多) */
function ago(iso) {
  if (!iso) return '从没抓过';
  const h = (Date.now() - new Date(iso).getTime()) / 3.6e6;
  if (h < 1) return '刚刚抓过';
  if (h < 24) return `${Math.round(h)} 小时前抓过`;
  return `${Math.round(h / 24)} 天前抓过`;
}

const ROLE_LABEL = {info: '有价值', rival: '竞品'};

/**
 * 渲染「待抓 N 位」条 + 抓取按钮。
 * @param sel      容器选择器
 * @param role     'info' | 'rival'
 * @param days     当前窗口
 * @param onDone   抓完的回调(通常是重新 load 页面数据)
 */
async function mountFetchBar(sel, role, days, onDone) {
  const box = document.querySelector(sel);
  if (!box) return;
  let d;
  try { d = await api(`/api/creators/pending?days=${days}&role=${role}`); }
  catch (e) { box.innerHTML = ''; return; }

  if (!d.total) { box.innerHTML = ''; return; }        // 一个都没选,交给空状态去引导

  const stale = d.items.filter(x => x.stale);
  if (!stale.length) {
    const last = d.items.map(x => x.fetched_at).filter(Boolean).sort().pop();
    box.innerHTML = `<div class="fetchbar ok">
      <div class="t"><b>这 ${days} 天的内容都抓过了</b>
        <div class="who">${d.total} 位${ROLE_LABEL[role]}博主 · ${ago(last)}</div></div>
      <button class="btn" data-fetch="1">再抓一次</button></div>`;
  } else {
    // 把「会从哪天开始抓」摆出来 —— 点之前就知道要发生什么,不做黑盒
    const since = stale.map(x => x.since).sort()[0];
    box.innerHTML = `<div class="fetchbar">
      <div class="t"><b>${stale.length} 位需要抓取</b>
        <div class="who">${stale.map(x => esc(x.nickname)).join(' · ')}
          <br>会从 ${(since || '').slice(0, 10)} 之后的新作品开始抓,已经抓过的不重复翻</div></div>
      <button class="btn" data-fetch="1">抓这 ${stale.length} 位</button></div>`;
  }

  box.querySelector('[data-fetch]').onclick = async (e) => {
    const btn = e.target;
    btn.disabled = true; btn.textContent = '抓取中…';
    try {
      const r = await api(`/api/digest/refresh?days=${Math.min(days, 30)}&role=${role}`,
        {method: 'POST'});
      btn.textContent = r.stopped_on_403
        ? `被限流,已入库 ${r.new} 条` : `抓完:新增 ${r.new} 条`;
      if (onDone) setTimeout(onDone, 700);
    } catch (err) {
      btn.textContent = '失败:' + err.message.slice(0, 24);
      btn.disabled = false;
    }
  };
}

/**
 * 页内选博主面板。展开后能直接打标/取消,不用离开当前页。
 * @param sel     容器选择器
 * @param role    这一页关心的角色('info' 或 'rival')
 * @param onChange 打标后的回调
 */
async function mountPicker(sel, role, onChange) {
  const box = document.querySelector(sel);
  if (!box) return;
  let all = [];
  try { all = (await api('/api/following')).items || []; }
  catch (e) { box.innerHTML = `<div class="empty">读取关注列表失败:${esc(e.message)}</div>`; return; }

  let kw = '';
  const render = () => {
    // 排序:已选中的排前面,其余按「我存过他几条」降序 —— 那是唯一有实证的价值信号
    const list = all
      .filter(u => !kw || (u.nickname || '').toLowerCase().includes(kw))
      .sort((a, b) => (b.role ? 2 : 0) - (a.role ? 2 : 0) || (b.saved_n - a.saved_n))
      .slice(0, 60);
    box.innerHTML = `<div class="picker">
      <div class="hd"><b>挑博主</b>
        <span class="dim">已选 ${all.filter(u => u.role === role).length} 位${ROLE_LABEL[role]}
          · 共关注 ${all.length} 位</span>
        <span class="gap"></span>
        <input id="pk-q" placeholder="搜名字…" value="${esc(kw)}">
        <button class="btn sm" data-close="1">收起</button></div>
      <div class="list">${list.map(u => `<div class="prow" data-id="${esc(u.sec_user_id)}">
        <span class="nm">${esc(u.nickname || '(无名)')}</span>
        <span class="st">我存过 ${u.saved_n} · 他发 ${fmt(u.aweme_count)}</span>
        <span class="acts">
          <button data-role="info" class="${u.role === 'info' ? 'on-info' : ''}">有价值</button>
          <button data-role="rival" class="${u.role === 'rival' ? 'on-rival' : ''}">竞品</button>
          <button data-role="">不跟</button>
        </span></div>`).join('') || '<div class="empty">没有匹配的</div>'}</div></div>`;

    box.querySelector('#pk-q').oninput = e => {
      kw = e.target.value.trim().toLowerCase();
      const p = e.target.selectionStart;
      render();
      const n = box.querySelector('#pk-q'); n.focus(); n.setSelectionRange(p, p);
    };
    box.querySelector('[data-close]').onclick = () => { box.innerHTML = ''; };
    box.querySelectorAll('.prow .acts button').forEach(btn => btn.onclick = async () => {
      const id = btn.closest('.prow').dataset.id;
      const val = btn.dataset.role || null;
      const u = all.find(x => x.sec_user_id === id);
      const before = u.role;
      u.role = val; render();                     // 先本地生效,界面不卡
      try {
        await postJSON('/api/following/role', {sec_user_ids: [id], role: val});
        if (onChange) onChange();
      } catch (e) {
        u.role = before; render();                // 存不进去就回滚,别让界面骗人
        alert('保存失败:' + e.message);
      }
    });
  };
  render();
}

/** 状态行。warn=true 用警示色 —— 只给「有代价 / 出问题」用,别滥用 */
function say(sel, text, warn) {
  const el = typeof sel === 'string' ? $(sel) : sel;
  if (!el) return;
  el.textContent = text || '';
  el.style.color = warn ? 'var(--warn)' : 'var(--dim2)';
}
