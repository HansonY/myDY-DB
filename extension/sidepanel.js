/* 侧边栏:识别当前页 → 一键把页面文字送去后端 → 后端让 AI 提取。
 *
 * 为什么存「页面文字」而不是接口响应:
 * 页面上你看到的文字就是数据本身,它不会因为平台改接口或改字段名而变。
 * 之前一直卡在「不知道 BOSS 字段叫什么」—— 这条路根本不需要知道。
 */
const $ = s => document.querySelector(s);
const API = 'http://localhost:8001';
let tab = null, page = null;

const esc = s => String(s ?? '').replace(/[&<>"]/g,
  m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]));

/** 在页面里跑:只抽文字,**不做判断**。
 *
 * 判断「是不是岗位页」全部交给后端 boss_detect.py —— 只有一份实现。
 * 插件里再写一份,两边早晚漂移,而漂移出来的 bug 最难查。
 * 这里也不再看 URL:BOSS 的岗位会出现在 /job_detail、/chat、/web/geek/job、
 * 搜索结果、推荐流里,路径五花八门,靠路径判必漏;平台改一次路由就全废。
 */
function grab() {
  // 去掉导航/页脚/脚本这类噪音,留主内容。抓不准也没关系 ——
  // 后面是 AI 提取,它能从啰嗦的文本里挑出岗位信息。
  const drop = 'script,style,noscript,svg,nav,footer,header,iframe';
  const root = document.querySelector('#main,#wrap,.page-job-wrapper,.job-detail,main')
            || document.body;
  const clone = root.cloneNode(true);
  clone.querySelectorAll(drop).forEach(e => e.remove());
  const text = (clone.innerText || '').replace(/\n{3,}/g, '\n\n').trim();

  // 标题用 document.title —— 它天然带公司名+岗位名,而且**整串当去重键不用解析**。
  // h1 只作补充:h1 依赖 class/结构,是最容易被改版打断的东西。
  return {
    url: location.href,
    title: (document.title || '').trim().slice(0, 120),
    h1: (document.querySelector('h1')?.innerText || '').trim().slice(0, 80),
    text: text.slice(0, 24000), len: text.length,
  };
}

async function readPage() {
  const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
  tab = t;
  const box = $('#now');
  if (!t || !/zhipin\.com/.test(t.url || '')) {
    page = null;
    box.innerHTML = '<div class="kind">当前页面</div>'
      + '<div class="warn">不是 BOSS 页面 —— 打开 zhipin.com 再来</div>';
    $('#save').disabled = true;
    return;
  }
  try {
    const [r] = await chrome.scripting.executeScript({
      target: { tabId: t.id }, func: grab, world: 'MAIN',
    });
    page = r.result;
  } catch (e) {
    page = null;
    box.innerHTML = '<div class="kind">当前页面</div>'
      + `<div class="warn">读不到页面:${esc(e.message).slice(0, 60)}</div>`;
    $('#save').disabled = true;
    return;
  }
  // 问后端这是不是岗位页(只读接口,不写库)
  let v = null;
  try {
    const r = await fetch(API + '/api/boss/detect', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: page.text, url: page.url, title: page.title }),
    });
    v = await r.json();
  } catch (e) { /* 服务没开 —— 下面照样显示页面信息,只是没有判定 */ }
  page.verdict = v;
  page.kind = v?.kind || 'other';

  const label = v
    ? ({ detail: '岗位详情', list: '岗位列表', other: '不是岗位页' })[v.kind]
    : '本地服务没开,无法判定';
  const sig = v ? `<div class="sig">${
      (v.hit || []).map(h => `<i class="on">${esc(h.label)}</i>`).join('')
    }${(v.miss || []).map(m => `<i>${esc(m)}</i>`).join('')}</div>` : '';
  box.innerHTML = `<div class="kind">当前页面 · ${label}</div>
    <div class="t">${esc(page.h1 || page.title) || '(没有标题)'}</div>
    <div class="m">${page.len ?? 0} 字可提取</div>
    ${v ? `<div class="why ${v.is_job ? 'yes' : 'no'}">${esc(v.why)}</div>${sig}` : ''}`;
  // other 也允许手动存 —— 判断可能不准,不该因为我猜错就拦着你
  $('#save').disabled = page.len < 80;
  $('#save').textContent = v && !v.is_job ? '仍然存入(我判它不是岗位页)' : '存入当前这一页';
}

async function save(auto) {
  if (!page || !tab) return;
  const btn = $('#save');
  btn.disabled = true; btn.textContent = '提取中…';
  try {
    const r = await fetch(API + '/api/boss/ingest_text', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      // **一律 force**。用户明确要求「存错了没关系,自动存入」——
      // 存的是页面原文,后面由 AI 过滤,所以宁可多存不要漏存。
      // 判定结果照样记下来(页面上能看出哪些像岗位),但不用它拦人。
      body: JSON.stringify({ ...page, auto: !!auto, force: true }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
    msg(d.note || (d.queued ? '已入队' : '没入队'), d.queued ? 'ok' : '');
    refresh();
  } catch (e) {
    msg(/Failed to fetch/.test(e.message)
      ? '连不上本地服务 —— 在项目目录跑 ./boss.sh web'
      : '出错:' + e.message, 'bad');
  }
  btn.textContent = '存入当前这一页';
  btn.disabled = false;
}

function msg(t, cls) { const m = $('#msg'); m.textContent = t || ''; m.className = 'msg ' + (cls || ''); }

async function refresh() {
  try {
    const d = await (await fetch(API + '/api/boss/jobs?limit=8')).json();
    $('#cnt').textContent = d.total ? `${d.total} 个` : '';
    const pend = d.pending || 0;
    const eb = $('#extract');
    eb.disabled = !pend;
    eb.textContent = pend ? `提取待处理的 ${pend} 页` : '没有待提取的';
    $('#list').innerHTML = (d.items || []).length
      ? d.items.map(j => `<div class="it">
          <div class="t">${esc(j.title) || '(无标题)'}</div>
          <div class="m">${esc(j.company || '')}${j.salary_text ? ' · ' + esc(j.salary_text) : ''}</div>
        </div>`).join('')
      : '<div class="empty">还没有。打开一个岗位页,点上面「存入」。</div>';
  } catch (e) {
    $('#list').innerHTML = '<div class="empty">本地服务没开 —— 跑 ./boss.sh web</div>';
  }
}

$('#extract').onclick = async () => {
  const b = $('#extract');
  b.disabled = true; b.textContent = '提取中…(多页一次,请稍等)';
  try {
    const r = await fetch(API + '/api/boss/extract', { method: 'POST' });
    const d = await r.json();
    if (d.error) msg(d.error, 'bad');
    else msg(`新增 ${d.extracted} 个岗位` +
      (d.updated ? `,更新 ${d.updated} 个` : '') +
      ` · 用了 ${d.ai_calls} 次 AI 调用` +
      (d.failed?.length ? ` · ${d.failed.length} 批失败` : ''), 'ok');
    refresh();
  } catch (e) {
    msg(/Failed to fetch/.test(e.message) ? '本地服务没开' : '出错:' + e.message, 'bad');
  }
  b.disabled = false;
};

$('#save').onclick = () => save(false);
$('#open').onclick = () => chrome.tabs.create({ url: API + '/' });
$('#autochk').onchange = e => chrome.storage.local.set({ auto: e.target.checked });

chrome.storage.local.get('auto').then(d => $('#autochk').checked = !!d.auto);
// 换标签页 / 页面跳转都重新识别 —— 侧边栏是常驻的,内容得跟着走
chrome.tabs.onActivated.addListener(() => readPage());
// 自动存**不在这里做**,在 background.js。原因:
//   · 面板关着时也要工作(人浏览时不会一直开着面板);
//   · BOSS 是单页应用,从列表点进详情走 history API,
//     tabs.onUpdated 的 'complete' 不触发 —— 这是之前一直没自动存的主因。
// 这里只负责跟着刷新显示。
chrome.tabs.onUpdated.addListener((id, info) => {
  if (info.status === 'complete') readPage();
});
/* ── 自动存状态 ─────────────────────────────────────────
 * 必须看得见。这个项目栽过一次「界面显示已入库 5、实际全是噪音」——
 * 自动的东西不显示战果,等于让人蒙着眼睛信它。 */
async function autoStat() {
  let d = {};
  try { d = await chrome.runtime.sendMessage({ type: 'saveStat' }) || {}; } catch (e) {
    $('#astat').innerHTML = '<span class="bad">后台脚本没响应 —— 到 chrome://extensions 点刷新</span>';
    return;
  }
  const st = d.saveStat || {}, b = d.batch || {};

  /* 「没自动存」有四种原因,现象一模一样:
   *   ① 根本没收到跳转事件(监听没生效 / 扩展没重载)
   *   ② 收到了但开关没勾
   *   ③ 勾了但抓到的字数太少(SPA 还没渲染完)
   *   ④ 抓到了但连不上本地服务
   * 所以四个环节都摊开显示,一眼就知道断在哪一环。 */
  const nav = st.nav || 0;
  const rows = [];
  rows.push(nav
    ? `<div class="ml ok">① 收到跳转 ${nav} 次${st.lastVia ? '(最近:' + esc(st.lastVia) + ')' : ''}</div>`
    : `<div class="ml bad">① 还没收到任何跳转 —— 扩展可能没重载,或没在 BOSS 页面上翻页</div>`);
  rows.push($('#autochk').checked
    ? '<div class="ml ok">② 自动存已开</div>'
    : '<div class="ml bad">② 「自动存」没勾上 ← 就是这里</div>');
  const acted = (st.ok || 0) + (st.skip || 0) + (st.fail || 0);
  rows.push(acted
    ? `<div class="ml ${st.ok ? 'ok' : 'bad'}">③ 已存 ${st.ok || 0} · 跳过 ${st.skip || 0} · 失败 ${st.fail || 0}</div>`
    : '<div class="ml">③ 还没尝试过存</div>');
  if (st.lastUrl) rows.push(`<div class="ml">最近页面:${esc(st.lastUrl.replace('https://www.zhipin.com',''))}</div>`);
  if (st.lastTitle) rows.push(`<div class="ml ok">最近存了:${esc(st.lastTitle)}</div>`);
  if (st.lastErr) rows.push(`<div class="ml bad">${esc(st.lastErr)}</div>`);
  $('#astat').innerHTML = rows.join('');

  // 手动粘链接那条路的进度(在折叠区里)。跑起来了就把折叠区自动展开 ——
  // 否则进度藏在收起来的地方,看着像什么都没发生。
  const bb = $('#bstat');
  if (b.total) {
    bb.innerHTML = `<div class="bp">${b.running ? '进行中' : '已结束'} `
      + `${b.done}/${b.total} · 成功 ${b.ok} 失败 ${b.fail}</div>`
      + (b.cur ? `<div class="ml">正在:${esc(b.cur.slice(0, 46))}</div>` : '')
      + (b.log || []).slice(0, 6).map(l =>
          `<div class="ml ${l.ok ? 'ok' : 'bad'}">${l.ok ? '✓' : '✗'} ${
            esc(l.title || l.url || '')?.slice(0, 34)} ${esc(l.note || '')}</div>`).join('');
    $('#bgo').textContent = b.running ? '停止' : '逐个打开并存入';
    if (b.running) { const d = document.querySelector('details'); if (d) d.open = true; }
  } else { bb.innerHTML = ''; }
}


/* ── 从当前列表页抓岗位链接 ───────────────────────────────
 * 比手工粘链接实用得多:打开「推荐职位 / 我的收藏 / 沟通过的」,
 * 往下滚几屏,点一下就把这一屏的岗位链接全抓出来。
 *
 * **抓 href,不认 class。** class 名是最容易被改版打断的东西;
 * 而链接里出现 job_detail 这个事实,是 BOSS 的 URL 结构决定的,稳定得多。
 *
 * 这一步**不产生任何请求** —— 只读你已经加载出来的 DOM。
 * 所以链接数量取决于你滚了多少屏:列表是懒加载的,没滚到就没在 DOM 里。
 */
function harvestLinks() {
  const out = new Map();      // 路径 → 完整链接(去重按路径,保留完整的)
  for (const a of document.querySelectorAll('a[href]')) {
    let u;
    try { u = new URL(a.getAttribute('href'), location.href); } catch (e) { continue; }
    if (!/zhipin\.com$/.test(u.hostname.replace(/^www\./, ''))) continue;
    if (!/job_detail|\/job\//.test(u.pathname)) continue;
    // ⚠️ 去重按**路径**,但保留**带查询串的完整链接**。
    // BOSS 的 job_detail 后面挂着 securityId / lid,每次渲染都不同 ——
    // 按完整 URL 去重等于永远不重复;但打开时又必须带上它们,
    // 去掉可能打不开。所以:比对用路径,打开用完整的。
    const key = u.origin + u.pathname;
    if (!out.has(key)) out.set(key, u.href);
  }
  return { links: [...out.values()], anchors: document.querySelectorAll('a[href]').length };
}

/* ── 「指一下下一页」───────────────────────────────────
 * 不让我猜元素:你在页面上点一次那个按钮,我把它的定位方式记下来。 */
$('#pick').onclick = async () => {
  const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!t || !/zhipin\.com/.test(t.url || '')) { msg('先切到 BOSS 的列表页', 'bad'); return; }
  await chrome.storage.local.remove('pickState');
  const r = await chrome.runtime.sendMessage({ type: 'armPicker', tabId: t.id });
  if (r?.error) { msg(r.error, 'bad'); return; }
  msg('已就绪 —— 切到 BOSS 那个标签页,点一下「下一页」按钮(按 Esc 取消)', 'ok');
};

async function pickInfo() {
  const d = await chrome.storage.local.get(['nextSel', 'pickState']);
  const box = $('#pickinfo');
  if (d.nextSel?.sels?.length) {
    box.innerHTML = `<span class="ok">已记住:「${esc(d.nextSel.text || d.nextSel.tag)}」`
      + ` · ${d.nextSel.sels.length + (d.nextSel.parentSels?.length || 0)} 种定位方式</span>`
      + ` <a id="forget">忘掉重指</a>`;
    $('#forget').onclick = async () => {
      await chrome.runtime.sendMessage({ type: 'forgetNext' });
      msg('已忘掉 —— 重新指一次', ''); pickInfo();
    };
  } else if (d.pickState?.cancelled) {
    box.innerHTML = '<span class="dimz">上次取消了 —— 还没记住任何按钮</span>';
  } else {
    box.innerHTML = '<span class="dimz">还没指过 —— 不指也能跑,但那是我按「下一页」三个字猜的,可能猜错</span>';
  }
}

/* ── 批量存入:本页存完 → 翻下一页 ───────────────────────── */

// 「自动下一页」是开关:不勾就只做当前这一页,那时「翻几次」和「指按钮」都是多余的
function syncNextBox() {
  $('#nextbox').hidden = !$('#autonext').checked;
  $('#rgo').textContent = $('#autonext').checked
    ? `开始:逐页存入(最多翻 ${$('#pgn').value} 次)`
    : '开始:只存当前这一页的岗位';
}
$('#autonext').onchange = e => {
  chrome.storage.local.set({ autonext: e.target.checked });
  syncNextBox();
};
$('#pgn').oninput = syncNextBox;
chrome.storage.local.get('autonext').then(d => {
  $('#autonext').checked = !!d.autonext; syncNextBox();
});

$('#rgo').onclick = async () => {
  const d = await chrome.storage.local.get('run');
  if (d.run?.running) { chrome.runtime.sendMessage({ type: 'stopRun' }); return; }
  const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!t || !/zhipin\.com/.test(t.url || '')) { msg('先切到 BOSS 的岗位列表页', 'bad'); return; }
  const autoNext = $('#autonext').checked;
  if (autoNext) {
    const { nextSel } = await chrome.storage.local.get('nextSel');
    if (!nextSel?.sels?.length) {
      msg('还没指过「下一页」—— 不指也能跑(我按文字猜),但建议先指一次更稳', '');
    }
  }
  msg('开跑 —— 别关那个列表标签页。详情会在另一个后台标签页里轮流打开。', 'ok');
  const r = await chrome.runtime.sendMessage({
    type: 'startRun', tabId: t.id, autoNext,
    rounds: parseInt($('#pgn').value, 10) || 5,
  });
  if (r?.error) msg(r.error, 'bad');
  else msg(`跑完 ${r.rounds} 页 · 存了 ${r.saved} 页原文 · 岗位 ${r.jobs} 个`
    + (r.failed ? ` · 失败 ${r.failed}` : '')
    + ' → 回岗位库点「提取」让 AI 出结构', 'ok');
};

async function runStat() {
  let d = {};
  try { d = await chrome.runtime.sendMessage({ type: 'runStat' }) || {}; } catch (e) { return; }
  const r = d.run || {};
  const box = $('#rstat');
  if (!r.rounds && !r.log?.length) { box.innerHTML = ''; return; }
  // 两层进度都要显示:第几页 + 这一页的第几个岗位。
  // 只显示一个总数的话,卡住时分不出是卡在翻页还是卡在某个岗位上。
  box.innerHTML = `<div class="bp">${r.running ? '进行中' : '已结束'} · `
    + `第 ${r.round || 0}/${r.rounds || 1} 页 · 岗位 ${r.jobsDone || 0}/${r.jobsTotal || 0}`
    + ` · 已存 ${r.saved || 0}${r.failed ? ` · 失败 ${r.failed}` : ''}</div>`
    + (r.log || []).slice(0, 8).map(l =>
        `<div class="ml ${l.bad ? 'bad' : 'ok'}">${esc(l.t)}</div>`).join('');
  $('#rgo').textContent = r.running ? '停止' : ($('#autonext').checked
    ? `开始:逐页存入(最多翻 ${$('#pgn').value} 次)` : '开始:只存当前这一页的岗位');
}

$('#harvest').onclick = async () => {
  const btn = $('#harvest');
  btn.disabled = true; btn.textContent = '抓取中…';
  try {
    const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!t || !/zhipin\.com/.test(t.url || '')) throw new Error('先切到 BOSS 的列表页');
    const [r] = await chrome.scripting.executeScript({
      target: { tabId: t.id }, func: harvestLinks, world: 'MAIN',
    });
    const links = r?.result?.links || [];
    if (!links.length) {
      msg(`这一页没找到岗位链接(扫了 ${r?.result?.anchors ?? 0} 个链接)。`
        + '换到「推荐职位 / 我的收藏 / 沟通过的」这类列表页,先往下滚几屏再点 ——'
        + '列表是懒加载的,没滚到的不在页面里。', 'bad');
      return;
    }
    // 把已经存过的筛掉 —— 不然白开几十个已有的
    let fresh = links, known = [];
    try {
      const k = await (await fetch(API + '/api/boss/known', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ urls: links }),
      })).json();
      fresh = k.fresh || links; known = k.known || [];
    } catch (e) { /* 服务没开就不筛,全放进去 */ }

    $('#links').value = fresh.join('\n');
    msg(`抓到 ${links.length} 个岗位链接`
      + (known.length ? `,其中 ${known.length} 个已存过(已剔除)` : '')
      + ` → 待存 ${fresh.length} 个。`
      + (fresh.length ? '确认后点下面「开始」。' : '这一页全都存过了。'),
      fresh.length ? 'ok' : '');
  } catch (e) {
    msg('抓不到:' + e.message, 'bad');
  }
  btn.disabled = false; btn.textContent = '抓本页岗位链接';
};

$('#bgo').onclick = async () => {
  const b = await chrome.storage.local.get('batch');
  if (b.batch?.running) { chrome.runtime.sendMessage({ type: 'stopBatch' }); return; }
  const urls = $('#links').value.split(/[\s,]+/).filter(Boolean);
  if (!urls.length) { msg('先把岗位链接粘进上面的框(一行一个)', 'bad'); return; }
  msg('');
  const r = await chrome.runtime.sendMessage({ type: 'startBatch', urls });
  if (r?.error) msg(r.error, 'bad');
  autoStat();
};

readPage(); refresh(); autoStat(); runStat(); pickInfo();
setInterval(() => { refresh(); autoStat(); runStat(); pickInfo(); }, 2500);
