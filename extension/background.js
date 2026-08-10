/* 点扩展图标就开侧边栏,不弹小窗 —— 侧边栏是常驻的,更适合边浏览边看 */
chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel?.setPanelBehavior?.({ openPanelOnActionClick: true })
    .catch(() => {});
});

/* 后台:攒一批再发,别每个响应都打一次本地服务。
 * 数据**只发到 localhost**,不去任何别的地方。 */
const ENDPOINT = 'http://localhost:8001/api/boss/ingest';
const FLUSH_MS = 4000;      // 攒 4 秒
const MAX_BATCH = 40;

/* ── 「跟你学」的详情补齐 ────────────────────────────────────
 * 为什么要学而不是写死:我没见过 BOSS 的详情接口,写死必错。
 * 做法:你手动点开一个岗位,我把那个请求的 URL 和列表里的 id 对上 ——
 * URL 里出现了某个 id,就把它换成占位符,得到模板。之后照模板补剩下的。
 *
 * ⚠️ 这一步会产生**你没点过的请求**,破了「零额外请求」那条性质。
 * 所以:必须你点按钮才开始、按人的节奏(3–7 秒随机)、一出错就停。
 */
const ids = new Set();          // 列表里见过的岗位 id
const done = new Set();         // 已经补过详情的
let tmpl = null;                // 学到的详情 URL 模板,{ID} 是占位
let filling = false;

// 从响应里挖「像 id 的值」。不认字段名 —— 认形状:
// key 以 id 结尾、值是 10–48 位的字符串。BOSS 那种 encryptJobId 正好符合。
function harvest(node, depth = 0) {
  if (depth > 6 || !node) return;
  if (Array.isArray(node)) { node.forEach(x => harvest(x, depth + 1)); return; }
  if (typeof node !== 'object') return;
  for (const [k, v] of Object.entries(node)) {
    if (typeof v === 'string' && /id$/i.test(k) && v.length >= 10 && v.length <= 48
        && /^[A-Za-z0-9~_\-]+$/.test(v)) {
      ids.add(v);
    } else if (v && typeof v === 'object') {
      harvest(v, depth + 1);
    }
  }
}

// 学模板:URL 里含某个已知 id → 把它换成 {ID}
function learn(url) {
  if (tmpl) return;
  for (const id of ids) {
    if (url.includes(id)) {
      tmpl = url.split('?')[0].replace(id, '{ID}');
      // 查询串里也可能带 id,原样保留结构
      const q = url.split('?')[1];
      if (q && q.includes(id)) tmpl += '?' + q.replace(id, '{ID}');
      chrome.storage.local.set({ tmpl });
      return;
    }
  }
}

let buf = [];
let timer = null;
let stat = { sent: 0, failed: 0, last: null, lastErr: null, byUrl: {}, recent: [] };

async function flush() {
  timer = null;
  if (!buf.length) return;
  const items = buf.splice(0, MAX_BATCH);
  try {
    const r = await fetch(ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items }),
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    stat.sent += items.length;
    stat.last = new Date().toISOString();
    stat.lastErr = null;
  } catch (e) {
    // 本地服务没开就先放回去 —— 数据不能因为忘了开服务就丢
    buf = items.concat(buf).slice(0, 500);
    stat.failed++;
    stat.lastErr = String(e.message || e);
  }
  await chrome.storage.local.set({ stat, pending: buf.length });
  if (buf.length) schedule();
}

function schedule() {
  if (!timer) timer = setTimeout(flush, FLUSH_MS);
}

chrome.runtime.onMessage.addListener((msg, _s, reply) => {
  if (msg?.type === 'capture') {
    buf.push({ url: msg.url, at: new Date().toISOString(), body: msg.body });
    // 按接口计数 —— 光看总数分不出「抓到真数据」还是「抓了一堆噪音」
    const k = String(msg.url).replace('https://www.zhipin.com', '');
    stat.byUrl[k] = (stat.byUrl[k] || 0) + 1;
    // 存一条「人能看懂的」摘要,让弹窗能回答「刚才存了什么」——
    // 只给总数的话,用户没法判断存的是真数据还是噪音(前面就这么被骗过)
    stat.recent.unshift({
      at: Date.now(),
      url: k,
      n: countItems(msg.body),
      sample: firstTitle(msg.body),
    });
    stat.recent = stat.recent.slice(0, 12);
    harvest(msg.body);          // 列表里的 id 收进来
    learn(msg.url);             // 你手动点详情时,顺手学会模板
    if (tmpl && msg.url.includes('/')) {
      // 已经补过的记下来,免得重复打
      for (const id of ids) if (msg.url.includes(id)) done.add(id);
    }
    schedule();
    reply?.({ ok: true });
  }
  if (msg?.type === 'status') {
    chrome.storage.local.get(['stat', 'pending']).then(d => reply({
      ...d, tmpl, ids: ids.size, done: done.size,
      todo: [...ids].filter(i => !done.has(i)).length, filling,
    }));
    return true;
  }
  if (msg?.type === 'flush') { flush().then(() => reply?.({ ok: true })); return true; }
  if (msg?.type === 'fill') { startFill(msg.tabId).then(r => reply?.(r)); return true; }
  if (msg?.type === 'stopFill') { filling = false; reply?.({ ok: true }); }
});


/** 数一数这个响应里有多少条记录 —— 找最长的对象数组。 */
function countItems(node, depth = 0) {
  if (depth > 6 || !node || typeof node !== 'object') return 0;
  let best = 0;
  if (Array.isArray(node)) {
    if (node.length && typeof node[0] === 'object') best = node.length;
    for (const x of node.slice(0, 3)) best = Math.max(best, countItems(x, depth + 1));
    return best;
  }
  for (const v of Object.values(node)) best = Math.max(best, countItems(v, depth + 1));
  return best;
}

/** 挖一个岗位标题当样例。不认死字段名 —— 常见几个都试。 */
function firstTitle(node, depth = 0) {
  if (depth > 6 || !node || typeof node !== 'object') return '';
  if (Array.isArray(node)) {
    for (const x of node.slice(0, 3)) { const t = firstTitle(x, depth + 1); if (t) return t; }
    return '';
  }
  for (const k of ['jobName', 'jobTitle', 'positionName', 'title', 'brandName', 'companyName']) {
    if (typeof node[k] === 'string' && node[k].trim()) return node[k].slice(0, 22);
  }
  for (const v of Object.values(node)) { const t = firstTitle(v, depth + 1); if (t) return t; }
  return '';
}

/** 按人的节奏补详情。**一出错就停** —— 连续失败多半是被限流了,继续打只会更糟。 */
async function startFill(tabId) {
  if (!tmpl) return { error: '还没学会详情接口 —— 先手动点开一个岗位详情' };
  if (filling) { filling = false; return { stopped: true }; }
  const todo = [...ids].filter(i => !done.has(i));
  if (!todo.length) return { error: '没有待补的' };

  filling = true;
  let ok = 0, fail = 0;
  for (const id of todo) {
    if (!filling) break;
    const url = tmpl.replace(/\{ID\}/g, id);
    try {
      // 交给页面里的 bridge 去 fetch —— 它在 zhipin 源上,cookie 天然带着,
      // 和页面自己发的请求没有区别。
      const r = await chrome.tabs.sendMessage(tabId, { type: 'fetchOne', url });
      if (r?.ok) { ok++; done.add(id); } else { fail++; }
    } catch (e) { fail++; }
    if (fail >= 3) { filling = false; break; }   // 连错三次就收手
    // 3–7 秒随机 —— 固定间隔本身就是机器特征
    await new Promise(s => setTimeout(s, 3000 + Math.random() * 4000));
  }
  filling = false;
  return { ok, fail, stopped: fail >= 3 };
}


/* ══════════════════════════════════════════════════════════════
 * 自动存 + 批量按链接存
 *
 * **为什么搬到 background 来。** 原来这段在 sidepanel.js 里,有三个问题:
 *   1. 侧边栏关掉就完全不工作 —— 而人浏览时不会一直开着面板;
 *   2. 只听 tabs.onUpdated 的 'complete'。**BOSS 是单页应用**,
 *      从列表点进详情走的是 history API,不产生整页加载,
 *      'complete' 根本不触发 —— 这是「打开岗位详情却没自动存」的主因;
 *   3. 自动存时没带 force,被后端的判断挡掉了。
 *
 * 现在:两种跳转都听(整页加载 + SPA 换 URL),面板开不开都跑,
 * 而且**一律存**(用户明确说「存错了没关系」)—— 后端只记判定结果不拦。
 * 存的是页面原文,后面由 AI 过滤,所以宁可多存。
 * ══════════════════════════════════════════════════════════════ */

const TEXT_API = 'http://localhost:8001/api/boss/ingest_text';
// SPA 换 URL 之后内容是异步渲染的,立刻抓会抓到上一页或者空白。
// 等一下再抓,并且抓到的字数太少就再等一次。
const SPA_SETTLE_MS = 1400;
const MIN_CHARS = 200;      // 比这还少基本是导航骨架,不是「存错了」而是「存了个空的」

/* ⚠️ 开关**每次都从 storage 现读**,不缓存在模块变量里。
 *
 * 这是个已经踩过的坑:MV3 的 service worker 空闲约 30 秒就被杀,事件来了再重启。
 * 缓存成模块变量的话,worker 一重启它就回到初始值 false,而重新读 storage 是异步的
 * —— 事件先到就直接 return,于是「自动存完全不起作用」。
 * 模块变量在 MV3 里**活不过一次空闲**,凡是要跨事件保持的状态都必须放 storage。 */
async function isAutoOn() {
  const d = await chrome.storage.local.get('auto');
  return !!d.auto;
}

// 同理:统计和去重记录也不能只存内存 —— worker 一重启就归零,
// 界面上数字会莫名其妙倒退,让人以为没在工作。
async function getStat() {
  const d = await chrome.storage.local.get('saveStat');
  return d.saveStat || { ok: 0, skip: 0, fail: 0, nav: 0, last: null,
                         lastTitle: null, lastErr: null, lastUrl: null };
}

/** 在页面里跑:只抽文字。判断在后端,这里不做任何判断。 */
function grabText() {
  const drop = 'script,style,noscript,svg,nav,footer,header,iframe';
  const root = document.querySelector('#main,#wrap,.page-job-wrapper,.job-detail,main')
            || document.body;
  const clone = root.cloneNode(true);
  clone.querySelectorAll(drop).forEach(e => e.remove());
  const text = (clone.innerText || '').replace(/\n{3,}/g, '\n\n').trim();
  return {
    url: location.href,
    title: (document.title || '').trim().slice(0, 120),
    h1: (document.querySelector('h1')?.innerText || '').trim().slice(0, 80),
    text: text.slice(0, 24000), len: text.length,
  };
}

async function readTab(tabId) {
  const [r] = await chrome.scripting.executeScript({
    target: { tabId }, func: grabText, world: 'MAIN',
  });
  return r?.result || null;
}

/** 抓一次;字数太少就再等一下重抓 —— SPA 渲染慢的时候第一次会抓空。 */
async function readTabSettled(tabId) {
  let page = await readTab(tabId);
  if (!page || page.len < MIN_CHARS) {
    await sleep(1200);
    page = await readTab(tabId);
  }
  return page;
}

const sleep = ms => new Promise(s => setTimeout(s, ms));

/** 存一页。force=true → 后端不拦,一律入队(用户要的就是这个)。 */
async function saveText(page, { auto = false, force = true } = {}) {
  const r = await fetch(TEXT_API, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...page, auto, force }),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
  return d;
}

async function autoSave(tabId, url, via) {
  const stat = await getStat();
  // **不管存不存,先把「我确实收到了这次跳转」记下来。**
  // 否则「没自动存」这一个现象,可能是没收到事件、可能是开关没开、
  // 可能是抓不到文字、可能是本地服务没开 —— 四种原因看起来一模一样,
  // 没法定位。这个项目已经因为「看不见中间过程」绕过好几次弯路。
  stat.nav = (stat.nav || 0) + 1;
  stat.lastUrl = String(url || '').slice(0, 120);
  stat.lastVia = via;

  const finish = async (extra) => {
    Object.assign(stat, extra || {});
    await chrome.storage.local.set({ saveStat: stat });
  };

  if (!/^https:\/\/[^/]*zhipin\.com\//.test(url || ''))
    return finish({ lastErr: '不是 zhipin 页面,跳过' });
  if (!(await isAutoOn()))
    return finish({ lastErr: '「自动存」没勾上' });
  if (batch.running || filling)
    return finish({ lastErr: '批量任务在跑,自动存让路' });

  // 同一页别反复存。去重记录也放 storage —— worker 重启后内存里的 Map 就没了。
  const bare = String(url).split('#')[0];
  const { seenUrl = {} } = await chrome.storage.local.get('seenUrl');
  if (seenUrl[tabId] === bare) return finish({ lastErr: null });

  try {
    const page = await readTabSettled(tabId);
    if (!page || page.len < MIN_CHARS) {
      stat.skip = (stat.skip || 0) + 1;
      return finish({ lastErr: `只抓到 ${page?.len ?? 0} 字(要 ≥${MIN_CHARS})—— 像是还没渲染完` });
    }
    const d = await saveText(page, { auto: true, force: true });
    seenUrl[tabId] = bare;
    await chrome.storage.local.set({ seenUrl });
    stat.ok = (stat.ok || 0) + 1;
    return finish({
      last: new Date().toISOString(),
      lastTitle: page.h1 || page.title,
      lastErr: null,
      lastVerdict: d.detect?.why || null,
    });
  } catch (e) {
    stat.fail = (stat.fail || 0) + 1;
    return finish({ lastErr: /Failed to fetch/.test(String(e.message))
      ? '连不上本地服务(要先跑 ./boss.sh web)' : String(e.message).slice(0, 100) });
  }
}

// 整页加载
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status === 'complete') autoSave(tabId, tab?.url, '整页加载');
});
// SPA 换 URL —— **这条才是从列表点进详情时唯一会触发的**
chrome.webNavigation?.onHistoryStateUpdated.addListener(async d => {
  if (d.frameId !== 0) return;
  await sleep(SPA_SETTLE_MS);
  autoSave(d.tabId, d.url, 'SPA跳转');
}, { url: [{ hostSuffix: 'zhipin.com' }] });
// 标签关了就忘掉它的去重记录,免得越攒越多
chrome.tabs.onRemoved.addListener(async id => {
  const { seenUrl = {} } = await chrome.storage.local.get('seenUrl');
  delete seenUrl[id];
  await chrome.storage.local.set({ seenUrl });
});


/* ── 批量:粘一批链接,逐个打开→存→关 ──────────────────────
 * 为什么要它:一个个点开太慢。
 *
 * ⚠️ 这会产生**你没有亲手点过的请求**。所以:必须你点按钮才开始、
 * 用一个复用的标签页(不是几十个一起开)、按人的节奏 4–9 秒随机、
 * 连错三次就停、随时可中断。固定间隔本身就是机器特征,所以取随机。
 */
/* 长任务保活。
 *
 * **MV3 的 service worker 空闲约 30 秒就被终止**,而 setTimeout 不会阻止终止
 * (只有 chrome API 调用会重置那个空闲计时器)。批量存入几十个链接要跑好几分钟,
 * 不保活就会在中途被杀 —— 任务无声中断,进度条停在那儿,看不出发生了什么。
 * 所以长任务期间每 20 秒 ping 一次 API。
 *
 * 保活是变通,不是保证。所以进度**每一步都写 storage**:万一还是被杀了,
 * 界面能显示停在哪一条,而不是让人对着不动的进度条猜。
 * (自动存那边已经栽过一次同类问题 —— 模块变量活不过一次空闲。)
 */
let keepTimer = null;
function keepAlive(on) {
  if (on && !keepTimer) {
    keepTimer = setInterval(() => chrome.runtime.getPlatformInfo().catch(() => {}), 20000);
  } else if (!on && keepTimer) {
    clearInterval(keepTimer); keepTimer = null;
  }
}

let batch = { running: false, total: 0, done: 0, ok: 0, fail: 0, cur: '', log: [] };

async function startBatch(urls) {
  if (batch.running) return { error: '已经在跑了' };
  const list = [...new Set((urls || [])
    .map(u => String(u).trim())
    .filter(u => /^https?:\/\/[^/]*zhipin\.com\//.test(u)))];
  if (!list.length) return { error: '没有可用的 zhipin.com 链接' };

  batch = { running: true, total: list.length, done: 0, ok: 0, fail: 0, cur: '', log: [] };
  await chrome.storage.local.set({ batch });
  keepAlive(true);

  // 复用一个标签页 —— 一次开几十个既卡又扎眼
  const tab = await chrome.tabs.create({ url: list[0], active: false });
  try {
    for (let i = 0; i < list.length; i++) {
      if (!batch.running) break;
      const url = list[i];
      batch.cur = url;
      await chrome.storage.local.set({ batch });
      try {
        if (i > 0) await chrome.tabs.update(tab.id, { url });
        await waitLoaded(tab.id);
        await sleep(900);                       // 让异步内容渲染出来
        const page = await readTabSettled(tab.id);
        if (!page || page.len < MIN_CHARS) throw new Error(`只有 ${page?.len ?? 0} 字`);
        const d = await saveText(page, { auto: true, force: true });
        batch.ok++;
        batch.log.unshift({ url, ok: true,
          title: (page.h1 || page.title || '').slice(0, 40),
          note: d.replaced ? '覆盖' : '新增' });
      } catch (e) {
        batch.fail++;
        batch.log.unshift({ url, ok: false, note: String(e.message).slice(0, 60) });
        // 连错三次就收手 —— 多半是被限流了,继续打只会更糟
        if (batch.fail >= 3 && batch.ok === 0) { batch.log.unshift({ note: '连续失败,已停止' }); break; }
      }
      batch.done++;
      batch.log = batch.log.slice(0, 40);
      await chrome.storage.local.set({ batch });
      if (i < list.length - 1 && batch.running) await sleep(4000 + Math.random() * 5000);
    }
  } finally {
    batch.running = false; batch.cur = '';
    keepAlive(false);
    await chrome.storage.local.set({ batch });
    chrome.tabs.remove(tab.id).catch(() => {});
  }
  return { ok: batch.ok, fail: batch.fail, total: batch.total };
}

function waitLoaded(tabId, timeout = 25000) {
  return new Promise((res, rej) => {
    const t = setTimeout(() => { chrome.tabs.onUpdated.removeListener(fn); rej(new Error('加载超时')); }, timeout);
    function fn(id, info) {
      if (id === tabId && info.status === 'complete') {
        clearTimeout(t); chrome.tabs.onUpdated.removeListener(fn); res();
      }
    }
    chrome.tabs.onUpdated.addListener(fn);
  });
}

chrome.runtime.onMessage.addListener((msg, _s, reply) => {
  if (msg?.type === 'saveStat') {
    chrome.storage.local.get(['saveStat', 'batch']).then(d => reply(d)); return true;
  }
  if (msg?.type === 'startBatch') { startBatch(msg.urls).then(r => reply(r)); return true; }
  if (msg?.type === 'stopBatch') { batch.running = false; reply({ ok: true }); }
  // 侧边栏手动点「存入」也走这里,省得两处各写一份 fetch
  if (msg?.type === 'saveOne') {
    saveText(msg.page, { auto: false, force: true })
      .then(d => reply({ ok: true, ...d }))
      .catch(e => reply({ error: String(e.message) }));
    return true;
  }
});
