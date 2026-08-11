/* 侧边栏:识别当前页 → 一键把页面文字送去后端 → 后端让 AI 提取。
 *
 * 为什么存「页面文字」而不是接口响应:
 * 页面上你看到的文字就是数据本身,它不会因为平台改接口或改字段名而变。
 * 之前一直卡在「不知道 BOSS 字段叫什么」—— 这条路根本不需要知道。
 */
const $ = s => document.querySelector(s);
const API = 'http://localhost:8001';
let tab = null, page = null;

/* ── 心跳:「面板开着」就是抓取的总开关 ──────────────────────
 * background 抓不抓,看 storage.session 里这个时间戳新不新鲜(25 秒)。
 * 面板关掉 → 这段代码停止运行 → 心跳停 → 抓取全停。
 * 用户定的产品规则:侧边栏打开才抓取,不是装了插件就一直抓。 */
function heartbeat() {
  try { chrome.storage.session.set({ panelSeen: Date.now() }); } catch (e) {}
}
heartbeat();
setInterval(heartbeat, 8000);

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
  // ⚠️ 直接用 body。原来挑「主内容容器」,但选择器列表是**按 DOM 顺序**返回
  // 第一个命中的、不按我写的优先级 —— 左右分栏的岗位页上很可能只命中右边的
  // .job-detail,左边整列岗位就丢了。宁可多带点导航噪音(AI 会忽略)。
  // 这段和 background.js 的 grabText() 必须一致,改一处记得改另一处。
  const clone = document.body.cloneNode(true);
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

// 当前标签页上插件的页面脚本活着吗。**重载扩展不会让已打开的标签页拿到新代码**
// —— content script 只在页面加载时注入。这是「自动存怎么都不生效」最常见的原因,
// 而且从面板上完全看不出来,所以显式探一下。
let bridgeAlive = null;      // null=还没探 / false=没在跑 / 数字=版本
async function probeBridge(tabId) {
  try {
    const r = await chrome.tabs.sendMessage(tabId, { type: 'ping' });
    bridgeAlive = r?.alive ? (r.v || 1) : false;
  } catch (e) {
    bridgeAlive = false;     // 没有接收方 = 这个标签页没跑我们的脚本
  }
}

async function readPage() {
  const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
  tab = t;
  if (t?.id != null && /zhipin\.com/.test(t.url || '')) await probeBridge(t.id);
  else bridgeAlive = null;
  const box = $('#now');
  if (!t || !/zhipin\.com/.test(t.url || '')) {
    page = null;
    box.innerHTML = '<div class="k">当前页面</div>'
      + '<div class="t" style="color:var(--dim);font-weight:400">不是 BOSS 页面</div>';
    syncButtons();
    return;
  }
  try {
    const [r] = await chrome.scripting.executeScript({
      target: { tabId: t.id }, func: grab, world: 'MAIN',
    });
    page = r.result;
  } catch (e) {
    page = null;
    box.innerHTML = '<div class="k">当前页面</div>'
      + `<div class="t" style="color:var(--warn);font-weight:400">读不到页面:${
          esc(e.message).slice(0, 60)}</div>`;
    syncButtons();
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
    ? ({ detail: '岗位详情', list: '岗位列表', other: '不像岗位页' })[v.kind]
    : '本地服务没开,无法判定';
  // 只显示命中几类,不把未命中的也铺出来 —— 那会占掉半屏,而且属于调试信息
  // (要看细节就看后端 /api/boss/detect 的完整返回)。
  const sig = v?.hit?.length ? ` · 命中 ${v.hit.length} 类信号` : '';
  box.innerHTML = `<div class="k${v?.is_job ? ' yes' : ''}">当前页面 · ${esc(label)}</div>
    <div class="t">${esc(page.h1 || page.title) || '(没有标题)'}</div>
    <div class="m">${page.len ?? 0} 字${sig}</div>`;
  syncButtons();
  scorePage();
}

/* ── 当前岗位的匹配 ───────────────────────────────────────
 * 分两层,泾渭分明:
 *   机械核对(免费,自动显示)  四个硬门槛:城市/经验/学历/薪资。page_state,零 AI。
 *   AI 分析匹配(花钱,点按钮)  技能匹配 + 对不对路 + fit 分,全归模型。
 *      **不自动调** —— 模型调用由人显式触发;已经分析过的直接显示缓存(不花钱)。
 * 左右分栏被判成「列表页」的也一样:右边正在看的岗位有职位描述,能锁定。 */
let scoreToken = 0, analyzing = false;

const GN = { city: '城市', experience: '经验', degree: '学历', salary: '薪资' };

function gateBlock(fb) {
  if (!fb || !(fb.gates || []).length) return '';
  const VT = { pass: ['ok', '符合'], fail: ['bad', '不符'],
               unknown: ['', '未判定'], na: ['', '不适用'] };
  const cells = fb.gates.map(g => {
    const [cls, txt] = VT[g.verdict] || ['', g.verdict];
    return `<span class="ml ${cls}" style="display:inline;margin-right:8px"
      title="${esc(g.note || '')}">${GN[g.name] || g.name}·${txt}</span>`;
  }).join('');
  return `<div class="gate">机械核对(只有这四项):${cells}</div>`;
}

function aiBtn(label) {
  return `<button class="ai" data-act="run">${label}</button>
    <div class="mt" style="margin-top:5px"><span>模型只在你点的时候调用,结果落库,同一岗位不再花钱</span></div>`;
}

function fitRow(st) {
  const ai = st.match || st.ai;
  const vd = { worth: '值得投', maybe: '可以试试', skip: '别浪费时间' }[ai?.verdict] || '?';
  const li = (arr, tag, cls) => (arr || []).length
    ? `<div class="gate">${tag}${arr.slice(0, 3).map(x =>
        `<span class="ml ${cls}" style="display:inline" title="原文:${esc(x.quote || '')}">${esc(x.point)}</span>`)
        .join('<br>　')}</div>`
    : '';
  return `${gateBlock(st.facts_brief)}
    <div class="fitrow" style="margin-top:7px"><span class="fit">${ai?.fit ?? '?'}</span>
      <span class="vd ${esc(ai?.verdict || '')}">${vd}</span>
      <span style="font-size:9px;color:var(--dim2)">AI 分析</span></div>
    <div class="why">${esc(ai?.fit_why || '')}</div>
    ${li(ai?.skills_hit, '技能对得上:', 'ok')}
    ${li(ai?.skills_gap, '技能缺口:', 'bad')}
    <div class="mt">
      <a data-act="detail">完整分析 ↗</a>
      <a data-act="redo">重新分析</a>
      ${ai?.quote_miss ? `<span>丢弃 ${ai.quote_miss} 条引不出原文的判断</span>` : ''}
    </div>`;
}

function bindScore(st) {
  const box = $('#score');
  box.querySelector('[data-act="detail"]')?.addEventListener('click', () =>
    chrome.tabs.create({ url: API + '/match.html#' + (st.job?.job_id || '') }));
  box.querySelector('[data-act="redo"]')?.addEventListener('click', () => analyzePage(st, true));
  box.querySelector('[data-act="run"]')?.addEventListener('click', () => analyzePage(st, false));
  box.querySelector('[data-act="me"]')?.addEventListener('click', () =>
    chrome.tabs.create({ url: API + '/me.html' }));
}

async function scorePage() {
  const box = $('#score');
  if (!page?.verdict?.is_job) {           // 岗位页才有匹配可谈(详情或列表都行)
    box.hidden = true; box.innerHTML = '';
    return;
  }
  const my = ++scoreToken;
  let st;
  try {
    st = await (await fetch(API + '/api/boss/page_state', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: page.title, url: page.url, text: page.text }),
    })).json();
  } catch (e) { box.hidden = true; return; }   // 服务没开 —— #svc 那行已经在说了
  if (my !== scoreToken) return;               // 页面已经换了,别渲染旧结果

  box.hidden = false;
  st.page_key = st.page_key || '';
  if (st.need_me) {
    box.innerHTML = `<div class="busy">要匹配,先录一次简历(只用录一次)
      —— <a data-act="me" style="color:var(--accent-t);cursor:pointer;text-decoration:underline">去录 ↗</a></div>`;
    bindScore(st); return;
  }
  if (st.match) {                              // 已分析过:显示缓存,不调模型
    box.innerHTML = fitRow(st); bindScore(st); return;
  }
  if (st.ai?.state !== 'ok') {
    box.innerHTML = `${gateBlock(st.facts_brief)}
      <div class="err">AI ${esc(st.ai?.label || '不可用')} —— 分析要模型,
      到知识库页「AI 模型」里配好再回来。</div>`;
    return;
  }
  // 机械核对(有就显示)在上,醒目的 AI 按钮在下 —— 点了才花钱
  box.innerHTML = `${gateBlock(st.facts_brief)}
    ${st.job ? '' : '<div class="busy" style="margin:2px 0 4px">这页的岗位还没提取 —— 点下面一次搞定(提取 + 分析)</div>'}
    ${aiBtn('AI 分析匹配' + (st.job ? '' : '(提取 + 分析)'))}`;
  bindScore(st);
}

async function analyzePage(st, force) {
  const box = $('#score');
  if (analyzing) return;
  analyzing = true;
  const my = scoreToken;
  const btn = box.querySelector('[data-act="run"]');
  if (btn) { btn.disabled = true; btn.textContent = st.job ? '模型分析中…(约 10 秒)' : '提取 + 分析中…(20~40 秒)'; }
  else box.innerHTML = `<div class="busy">分析中…</div>`;
  try {
    const d = await (await fetch(API + '/api/boss/page_analyze?force=' + !!force, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: page.title, url: page.url, text: page.text }),
    })).json();
    if (my !== scoreToken) return;             // 人已经翻到别的岗位了
    if (d.error) {
      box.innerHTML = `${gateBlock(st.facts_brief)}<div class="err">${esc(d.error)}</div>
        ${aiBtn('再试一次')}`;
      bindScore(st);
    } else {
      box.innerHTML = fitRow({ ...d, match: d.ai, page_key: st.page_key });
      bindScore({ ...d, page_key: st.page_key });
      refresh();                               // 库里多了岗位,统计跟着动
    }
  } catch (e) {
    if (my === scoreToken)
      box.innerHTML = `<div class="err">连不上本地服务 —— 跑 ./boss.sh</div>${aiBtn('再试一次')}`;
    bindScore(st);
  } finally { analyzing = false; }
}

/* 三个抓取动作各自一个按钮,按钮名就是它干的事:
 *   抓取这页内容      → 只存当前这一页原文
 *   抓取该页所有岗位  → 逐个打开这一页每个岗位存原文
 *   自动翻页抓取      → 上面那件事每页做一遍,做完点「下一页」
 * 以前是「一个开始按钮 + 一个自动下一页勾选框」,点之前看不出会发生什么。
 *
 * 后两项只在**列表页**有意义(详情页没有岗位列表、也没有下一页),
 * 所以在详情页直接禁用并写清原因,而不是让人点了才发现没反应。
 */
let running = false;

function syncButtons() {
  const onList = page?.kind === 'list';
  const has = !!page && (page.len || 0) >= 80;
  const auto = $('#autonext').checked;
  $('#save').disabled = running || !has;
  $('#runOne').disabled = running || !onList;
  $('#autonext').disabled = running || !onList;
  // 「锁定下一页按钮」只在勾了自动翻页时才有意义 —— 不勾根本不会翻页
  $('#nextbox').hidden = running || !onList || !auto;
  $('#stop').hidden = !running;

  const b = $('#runOne').querySelector('b'), i = $('#runOne').querySelector('i');
  if (!onList) {
    // 说清为什么点不动,而不是让人干瞪眼
    b.textContent = '抓取该页所有岗位列表存入';
    i.textContent = !page ? '打开 zhipin.com 的岗位列表页再来'
      : '这一项要在岗位列表页用(推荐职位 / 搜索结果 / 我的收藏)';
  } else if (auto) {
    b.textContent = `抓取该页所有岗位,再往后翻 ${$('#pgn').value} 页`;
    i.textContent = '每页都逐个打开岗位存入,抓完自动点「下一页」';
  } else {
    b.textContent = '抓取该页所有岗位列表存入';
    i.textContent = '逐个打开这一页的每个岗位,存入原文';
  }
}

// 自动翻页开关:勾上才出现页数和「锁定下一页按钮」
$('#autonext').onchange = e => {
  chrome.storage.local.set({ autonext: e.target.checked });
  syncButtons(); pickInfo();
};
$('#pgn').oninput = syncButtons;
chrome.storage.local.get('autonext').then(d => {
  $('#autonext').checked = !!d.autonext; syncButtons();
});

async function save(auto) {
  if (!page || !tab) return;
  const btn = $('#save'), lab = btn.querySelector('b');
  const back = lab.textContent;
  btn.disabled = true; lab.textContent = '存入中…';
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
    msg('manual', d.note || (d.queued ? '已入队' : '没入队'), d.queued ? 'ok' : '', 15000);
    refresh();
  } catch (e) {
    msg('manual', /Failed to fetch/.test(e.message)
      ? '连不上本地服务 —— 在项目目录跑 ./boss.sh web'
      : '出错:' + e.message, 'bad');
  }
  lab.textContent = back;
  syncButtons();
}

/* 提示写到**发起这次操作的那张卡**里,不共用一个公共区。
 * 共用就看不出提示是谁给的 —— 用户说的「凌乱」正是这个。
 *   run    ① 抓取该页所有岗位
 *   auto   ② 浏览存入
 *   manual ③ 手动存入
 *   batch  折叠区里的手动粘链接
 */
const SLOT = { run: '#m1', auto: '#m2', manual: '#m3', batch: '#bstat' };

/* 「你点出来的结果」要压得住「定时刷新」。
 *
 * 定时器每 2.5 秒跑一次 autoStat/runStat,它们也会往这些格子里写状态 ——
 * 于是你点「测一下」看到的结论 2.5 秒后就被冲掉,根本来不及读。
 * 实测就是这样,自检按钮等于白做。
 * 所以:人主动触发的消息带一个保护期(hold),期内后台刷新不许覆盖。
 */
const HOLD = {};
function msg(slot, t, cls, holdMs) {
  const m = $(SLOT[slot]);
  if (!m) return;
  const now = Date.now();
  if (holdMs) HOLD[slot] = now + holdMs;
  else if ((HOLD[slot] || 0) > now) return;   // 保护期内,后台刷新让路
  m.textContent = t || '';
  m.className = (slot === 'batch' ? 'prog ' : 'fb ') + (cls || '');
}

async function refresh() {
  try {
    const [jb, st] = await Promise.all([
      (await fetch(API + '/api/boss/jobs?limit=8')).json(),
      (await fetch(API + '/api/boss/stats')).json(),
    ]);
    // 三个数字回答「我干到哪儿了」:原文攒了多少还没提取、提取出多少岗位、
    // 其中多少真拿到了职位描述(只在列表页见过的那些没有描述)。
    $('#sPend').textContent = jb.pending || 0;
    $('#sJobs').textContent = jb.total || 0;
    $('#sJd').textContent = st.jd_have || 0;
    $('#svc').textContent = '';
  } catch (e) {
    for (const id of ['#sPend', '#sJobs', '#sJd']) $(id).textContent = '–';
    // 服务没开是最常见的「怎么都不动」原因,单独说一句
    $('#svc').textContent = '本地服务没开 —— 在项目目录跑 ./boss.sh web';
  }
}

// AI 提取移到知识库页了(那边能看到队列、失败原因、模型状态)——
// 面板只管抓,不管提取。顶部「知识库 ↗」是入口。

$('#save').onclick = () => save(false);
$('#runOne').onclick = () => startRun($('#autonext').checked);
$('#stop').onclick = () => chrome.runtime.sendMessage({ type: 'stopRun' });
$('#open').onclick = () => chrome.tabs.create({ url: API + '/' });
$('#recent').onclick = () => chrome.tabs.create({ url: API + '/' });

/* 「测一下这条链路」—— 把自动存走的每一步当场跑一遍,断在哪就说哪。
 *
 * 为什么要它:「没存入」这一个现象,可能断在
 *   ① 这个标签页没跑插件脚本(重载扩展后没刷新页面)
 *   ② 开关没开
 *   ③ 读不到页面文字
 *   ④ 抓到的字太少
 *   ⑤ 本地服务没开
 * 五种表现一模一样。等页面变化再看太慢,给个按钮当场走一遍。
 */
$('#selftest').onclick = async () => {
  const b = $('#selftest');
  b.disabled = true; b.textContent = '测试中…';
  const step = [];
  try {
    const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!t || !/zhipin\.com/.test(t.url || '')) throw new Error('当前不是 BOSS 页面 —— 先切到 zhipin.com');
    step.push('① 当前页 ✓');

    await probeBridge(t.id);
    if (bridgeAlive === false) throw new Error('这个标签页没跑插件脚本 —— 按 ⌘R 刷新页面');
    step.push(`② 页面脚本在跑(v${bridgeAlive})✓`);

    const [r] = await chrome.scripting.executeScript({
      target: { tabId: t.id }, func: grab, world: 'MAIN' });
    const pg = r?.result;
    if (!pg) throw new Error('读不到页面文字');
    step.push(`③ 读到 ${pg.len} 字 ✓`);
    if (pg.len < 200) throw new Error(`只有 ${pg.len} 字,像是还没渲染完`);

    const resp = await fetch(API + '/api/boss/ingest_text', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...pg, auto: false, force: true }),
    });
    const d = await resp.json();
    if (!resp.ok) throw new Error(d.detail || ('HTTP ' + resp.status));
    step.push(`④ 存进去了 —— ${d.note || '已入队'} ✓`);
    msg('auto', step.join('\n') + '\n整条链路是通的。', 'ok', 20000);
    refresh();
  } catch (e) {
    const why = /Failed to fetch/.test(e.message)
      ? '连不上本地服务 —— 在项目目录跑 ./boss.sh web' : e.message;
    msg('auto', step.join('\n') + '\n✗ ' + why, 'bad', 20000);
  }
  b.disabled = false; b.textContent = '测一下这条链路';
};
// 换标签页 / 页面跳转都重新识别 —— 侧边栏是常驻的,内容得跟着走
chrome.tabs.onActivated.addListener(() => readPage());
// 左右分栏点左边换右边:URL 不变,靠页面里的 bridge 发「内容变化」。
// background 收它去自动存;这里也收,让「当前岗位 + 匹配分」跟着换。
let pcTimer = null;
chrome.runtime.onMessage.addListener(msg => {
  if (msg?.type !== 'pageChanged') return;
  clearTimeout(pcTimer);
  pcTimer = setTimeout(readPage, 900);   // 等内容稳定,别抓到渲染一半的
});
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
  // **一次都没跑过就不铺那三行** —— 空着比摆一堆「还没…」干净。
  // 注意别只清空就往下走:下面还会把 rows 写回去,那样等于没清。
  const idle = !nav && !(st.ok || st.skip || st.fail);
  const rows = [];
  rows.push(nav
    ? `<div class="ml ok">① 已监听到翻页 ${nav} 次${st.lastVia ? ' · ' + esc(st.lastVia) : ''}</div>`
    : `<div class="ml bad">① 没监听到翻页 —— 扩展可能没重载,或还没在 BOSS 上翻过页</div>`);
  // 开关就是「面板开着」—— 你能看到这行字,说明开关是开的
  rows.push('<div class="ml ok">② 面板开着,抓取生效中(关掉面板就全停)</div>');
  const acted = (st.ok || 0) + (st.skip || 0) + (st.fail || 0);
  rows.push(acted
    ? `<div class="ml ${st.ok ? 'ok' : 'bad'}">③ 存了 ${st.ok || 0} · 跳过 ${st.skip || 0} · 失败 ${st.fail || 0}</div>`
    : '<div class="ml">③ 还没存过</div>');
  if (st.lastUrl) rows.push(`<div class="ml">最近:${esc(st.lastUrl.replace('https://www.zhipin.com',''))}</div>`);
  if (st.lastTitle) rows.push(`<div class="ml ok">存的是:${esc(st.lastTitle)}</div>`);
  if (st.lastErr) rows.push(`<div class="ml bad">${esc(st.lastErr)}</div>`);
  $('#astat').innerHTML = idle ? '' : rows.join('');
  // ② 卡上只给一行结论 —— 平时要看的是「到底存下来没有」,不是排查过程。
  // 四环节细节留在折叠的「诊断」里。
  // ② 卡:先给结论,不生效时**把三种触发来源分开摆出来**。
  // 「没生效」有三种断点:整页加载没响 / SPA 换 URL 没响 / 左右分栏的内容变化没响。
  // 合成一个数字分不出是哪种 —— 这个项目已经因为「看不见中间过程」返工过几轮。
  const src = st.bySrc || {};
  const srcLine = ['整页加载', 'SPA跳转', '内容变化']
    .map(k => `${k} ${src[k] || 0}`).join(' · ');
  if (bridgeAlive === false) {
    msg('auto', '这个标签页还没加载插件脚本 —— 按 ⌘R 刷新一下页面。'
      + '(重载扩展不会让已经打开的页面拿到新代码)', 'bad');
  } else if (st.ok) {
    msg('auto', `已存 ${st.ok} 页`
      + (st.skip ? ` · 跳过 ${st.skip}` : '') + (st.fail ? ` · 失败 ${st.fail}` : '')
      + (st.lastTitle ? ` · 最近「${String(st.lastTitle).slice(0, 16)}」` : ''),
      st.fail ? '' : 'ok');
  } else if (!(st.nav || 0)) {
    msg('auto', '已开,但一次页面变化都没监听到 —— 扩展多半没重载(chrome://extensions 点刷新)', 'bad');
  } else {
    // 监听到了却没存下来:把来源和最近一次的原因都摆出来
    msg('auto', `监听到 ${srcLine},但没存下来。${st.lastErr || ''}`.trim(), 'bad');
  }

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
    if (b.running) $('#more').open = true;   // 跑起来就展开,否则进度藏在收起处
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
  if (!t || !/zhipin\.com/.test(t.url || '')) { msg('run', '先切到 BOSS 的列表页', 'bad'); return; }
  await chrome.storage.local.remove('pickState');
  const r = await chrome.runtime.sendMessage({ type: 'armPicker', tabId: t.id });
  if (r?.error) { msg('run', r.error, 'bad'); return; }
  msg('run', '好了 —— 切到 BOSS 页面,点一下「下一页」按钮。按 Esc 取消。', 'ok');
};

async function pickInfo() {
  const d = await chrome.storage.local.get(['nextSel', 'pickState']);
  const box = $('#pickinfo');
  // **没记住就什么都不显示。** 常驻一行「还没指过…」只是噪音 ——
  // 该说的话写在按钮的 title 里,需要时鼠标停一下就能看到。
  if (!d.nextSel?.sels?.length) {
    box.innerHTML = d.pickState?.cancelled ? '上次取消了,没记住' : '';
    return;
  }
  box.innerHTML = `<span class="ok">已锁定「${esc(d.nextSel.text || d.nextSel.tag)}」</span>`
    + ` <a id="forget">重新锁定</a>`;
  $('#forget').onclick = async () => {
    await chrome.runtime.sendMessage({ type: 'forgetNext' });
    msg('run', '已清掉,重新锁定一次。', ''); pickInfo();
  };
}

/* ── 批量存入:本页存完 → 翻下一页 ───────────────────────── */

async function startRun(autoNext) {
  const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!t || !/zhipin\.com/.test(t.url || '')) { msg('run', '先切到 BOSS 的岗位列表页', 'bad'); return; }
  if (autoNext) {
    const { nextSel } = await chrome.storage.local.get('nextSel');
    if (!nextSel?.sels?.length) {
      msg('run', '没指过「下一页」,这次按「下一页」三个字猜。猜错会停下来告诉你。', '');
    }
  }
  msg('run', '开跑了。别关这个列表标签页 —— 岗位会在另一个后台标签页里轮流打开。', 'ok');
  const r = await chrome.runtime.sendMessage({
    type: 'startRun', tabId: t.id, autoNext,
    rounds: autoNext ? (parseInt($('#pgn').value, 10) || 5) : 1,
  });
  if (r?.error) msg('run', r.error, 'bad');
  else msg('run', `跑完 ${r.rounds} 页 · 存了 ${r.saved} 页原文 · 岗位 ${r.jobs} 个`
    + (r.failed ? ` · 失败 ${r.failed}` : '')
    + ' → 到知识库页点「提取」让 AI 出结构', 'ok');
}

async function runStat() {
  let d = {};
  try { d = await chrome.runtime.sendMessage({ type: 'runStat' }) || {}; } catch (e) { return; }
  const r = d.run || {};
  const box = $('#m1');
  // ⚠️ running 的同步必须在早退**之前**:run 状态一被清空就 return 的话,
  // running 永远停在 true,按钮全灰着、停止键也不消失,人就没法再开始了。
  if (running !== !!r.running) { running = !!r.running; syncButtons(); }
  if (!r.rounds && !r.log?.length) return;   // 没跑过就别动这张卡的反馈行
  // 两层进度都要显示:第几页 + 这一页的第几个岗位。
  // 只显示一个总数的话,卡住时分不出是卡在翻页还是卡在某个岗位上。
  // 跑的时候给实时进度 + 最近几条;**跑完只留一行结果**。
  // 整段日志一直挂在面板上是噪音 —— 要复盘就展开「诊断」看。
  const headline = `<div class="bp">${r.running ? '进行中' : '已结束'} · `
    + `第 ${r.round || 0}/${r.rounds || 1} 页 · 岗位 ${r.jobsDone || 0}/${r.jobsTotal || 0}`
    + ` · 已存 ${r.saved || 0}${r.failed ? ` · 失败 ${r.failed}` : ''}</div>`;
  box.className = 'fb';
  box.innerHTML = r.running
    ? headline + (r.log || []).slice(0, 5).map(l =>
        `<div class="ml ${l.bad ? 'bad' : 'ok'}">${esc(l.t)}</div>`).join('')
    : headline;
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
      msg('batch', `这一页没找到岗位链接(扫了 ${r?.result?.anchors ?? 0} 个链接)。`
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
    msg('batch', `抓到 ${links.length} 个岗位链接`
      + (known.length ? `,其中 ${known.length} 个已存过(已剔除)` : '')
      + ` → 待存 ${fresh.length} 个。`
      + (fresh.length ? '确认后点下面「逐个打开并存入」。' : '这一页全都存过了。'),
      fresh.length ? 'ok' : '');
  } catch (e) {
    msg('batch', '抓不到:' + e.message, 'bad');
  }
  btn.disabled = false; btn.textContent = '抓本页岗位链接';
};

$('#bgo').onclick = async () => {
  const b = await chrome.storage.local.get('batch');
  if (b.batch?.running) { chrome.runtime.sendMessage({ type: 'stopBatch' }); return; }
  const urls = $('#links').value.split(/[\s,]+/).filter(Boolean);
  if (!urls.length) { msg('batch', '上面的框是空的 —— 先粘链接或点「抓本页岗位链接」。', 'bad'); return; }
  msg('batch', '');
  const r = await chrome.runtime.sendMessage({ type: 'startBatch', urls });
  if (r?.error) msg('batch', r.error, 'bad');
  autoStat();
};

readPage(); refresh(); autoStat(); runStat(); pickInfo();
setInterval(() => { refresh(); autoStat(); runStat(); pickInfo(); }, 2500);
