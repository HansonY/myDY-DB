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
  // ⚠️ 直接用 body,不再挑「主内容容器」。
  // 原来是 querySelector('#main,#wrap,.page-job-wrapper,.job-detail,main'),
  // 但**选择器列表按 DOM 顺序返回第一个命中的,不按我写的优先级** ——
  // 在左右分栏的岗位页上很可能只命中右边那个 .job-detail,
  // 于是左边整列岗位全丢了。宁可多带点导航噪音(AI 提取时会忽略),
  // 也不能漏掉半个页面。
  const clone = document.body.cloneNode(true);
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

/* 正文指纹。和 bridge.js 里 fingerprint() 用的是**同一套算法和同一组剔除项** ——
 * 两边口径不一致的话,一边认为「变了」另一边认为「没变」,就会出现
 * 该存的不存 / 不该存的重复存。改一处记得改另一处。 */
const VOLATILE_RE = /刚刚|\d+\s*(秒|分钟|小时|天)前|正在输入|未读/g;
function hashText(t) {
  // 剔掉自变字眼之后**还要压掉所有空白**。
  // 不压的话,「刚刚」被剔走会留下一个空格,长度就变了 → 哈希跟着变 →
  // 同一个页面被当成新的又存一遍。后端 dedupe_key 早就这么做了,
  // 三处(bridge.js / background.js / boss_detect.py)口径必须一致。
  const s = String(t || '').replace(VOLATILE_RE, '').replace(/\s+/g, '');
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
  return `${s.length}#${(h >>> 0).toString(36)}`;
}

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

async function autoSave(tabId, url, via, sig) {
  const stat = await getStat();
  // **不管存不存,先把「我确实收到了这次跳转」记下来。**
  // 否则「没自动存」这一个现象,可能是没收到事件、可能是开关没开、
  // 可能是抓不到文字、可能是本地服务没开 —— 四种原因看起来一模一样,
  // 没法定位。这个项目已经因为「看不见中间过程」绕过好几次弯路。
  stat.nav = (stat.nav || 0) + 1;
  // **按来源分开计数。** 「自动存没生效」有三种断点:整页加载没响、
  // SPA 换 URL 没响、左右分栏的内容变化没响 —— 合成一个数字就分不出是哪种。
  stat.bySrc = stat.bySrc || {};
  stat.bySrc[via] = (stat.bySrc[via] || 0) + 1;
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
  if (batch.running || filling || run.running)
    return finish({ lastErr: '批量/翻页任务在跑,自动存让路' });

  try {
    const page = await readTabSettled(tabId);
    if (!page || page.len < MIN_CHARS) {
      stat.skip = (stat.skip || 0) + 1;
      return finish({ lastErr: `只抓到 ${page?.len ?? 0} 字(要 ≥${MIN_CHARS})—— 像是还没渲染完` });
    }

    // 同一页别反复存。**去重键按「实际抓到的正文」算,不用调用方传来的指纹。**
    //
    // 两个原因:
    //  · 只用 URL 不行 —— 左右分栏点左边换右边时 URL 根本不变,
    //    那样会把第一条之后的全挡掉,而那些正是我们要的职位描述;
    //  · 各条触发路径传来的指纹口径不一样(整页加载那条压根没有),
    //    于是同一次加载会被「整页加载」和「内容变化」各存一遍。
    //    统一按抓到的正文算,三条路自然一致。
    const bare = String(url).split('#')[0] + '|' + hashText(page.text);
    const { seenUrl = {} } = await chrome.storage.local.get('seenUrl');
    if (seenUrl[tabId] === bare) return finish({ lastErr: null });

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
// 内容变了但 URL 没变 —— 左右分栏的岗位页走的就是这条。
// 消息来自页面里的 bridge.js(它在 zhipin 源上,能看到 DOM 也能用 chrome.runtime)。
chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg?.type !== 'pageChanged') return;
  const tabId = sender?.tab?.id;
  if (tabId != null) autoSave(tabId, msg.url || sender.tab.url, '内容变化', msg.fp);
});

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


/* ══════════════════════════════════════════════════════════════
 * 模拟点「下一页」自动翻页
 *
 * 为什么点按钮而不是改 URL 的 page 参数:
 * 不用假设它怎么分页。BOSS 是单页应用,改 query 未必触发它自己的路由;
 * 而「下一页」这个按钮是它自己的翻页入口,点它等于走它设计好的那条路。
 *
 * **找按钮靠文字,不靠 class。** class 名是最容易被改版打断的东西,
 * 而「下一页」这三个字是给人看的,不会随便改。
 *
 * **最关键的一条:每次点完都验证内容真的换了。**
 * 如果选错了元素(比如点到一个没反应的 span),循环会一遍遍抓同一页,
 * 然后报告「成功翻了 10 页」—— 界面显示在工作、实际一页没动。
 * 这个项目已经栽过一次同类的(「已送入库 5」其实全是噪音),
 * 所以这里用**内容指纹**判断:指纹没变就是没翻动,立刻停,如实说。
 *
 * ⚠️ 这会产生你没有亲手点过的请求。所以:必须点按钮才开始、
 * 4–9 秒随机间隔、指纹不变立刻停、硬上限 30 页、随时可停。
 * ══════════════════════════════════════════════════════════════ */

const MAX_ROUNDS = 20;      // 翻页次数上限,别让它无限跑

/** 页面指纹:拿前若干个岗位链接拼起来。翻页成功它必然变。 */
function pageFingerprintInPage() {
  const hrefs = [...document.querySelectorAll('a[href]')]
    .map(a => { try { return new URL(a.getAttribute('href'), location.href).pathname; }
                catch (e) { return ''; } })
    .filter(p => /job_detail|\/job\//.test(p));
  return { fp: hrefs.slice(0, 12).join('|'), n: hrefs.length, url: location.href };
}

/** 找到并点「下一页」。找不到 / 已禁用都如实回报,不假装点了。 */
function clickNextInPage() {
  const WANT = /^(下一页|下页|next|›|»|>)$/i;
  let label = null;
  for (const e of document.querySelectorAll('a,button,li,span,div,i,em')) {
    const t = (e.textContent || '').trim();
    if (!WANT.test(t)) continue;
    // 别选到「包着按钮的外层容器」—— 点容器往往没反应
    if (e.querySelector('a,button')) continue;
    label = e; break;
  }
  if (!label) return { error: '页面上找不到「下一页」' };

  // 文字可能在 <span> 里,真正可点的是外面的 <a>/<button>/<li>
  let btn = label;
  for (let i = 0; i < 4; i++) {
    if (/^(A|BUTTON)$/.test(btn.tagName)) break;
    const p = btn.parentElement;
    if (!p) break;
    if (/^(A|BUTTON|LI)$/.test(p.tagName)) { btn = p; break; }
    btn = p;
  }

  // 到最后一页时按钮通常被置灰 —— 但**不能只靠这个判断**,
  // 各家写法不一样。真正的判据是点完指纹有没有变(见调用方)。
  const cls = `${label.className || ''} ${btn.className || ''}`;
  if (/disabled|is-disabled|no-more|cur-last/i.test(String(cls))
      || btn.getAttribute?.('aria-disabled') === 'true'
      || btn.hasAttribute?.('disabled')) {
    return { error: '「下一页」是禁用状态,已经是最后一页' };
  }
  btn.click();
  return { ok: true, tag: btn.tagName, cls: String(btn.className || '').slice(0, 40) };
}

/* ── 一趟跑完:本页所有岗位存完 → 点「下一页」→ 再来一轮 ─────────
 *
 * 顺序是**嵌套**的,不是先翻完再补:
 *   ① 存这一页列表的原文(一屏十几个岗位,AI 一次全提出来,只是没有职位描述)
 *   ② 抓这一页的岗位链接,筛掉已经存过的
 *   ③ 逐个打开这些岗位,存详情原文(职位描述就是从这儿来的)
 *   ④ 「自动下一页」开着就点一次「下一页」,回到 ①
 *
 * **详情必须用另一个标签页开。**
 * 列表页所在的标签页一旦被导航到详情,列表就没了 —— 它是单页应用,
 * 翻到哪儿是它内部状态,不是 URL 决定的,退回来未必能复原。
 * 所以:列表页那个标签页全程不动,详情在一个专用标签页里轮流打开。
 *
 * ⚠️ 这会产生你没有亲手点过的请求。所以:必须点按钮才开始、
 * 每个岗位之间 4–9 秒随机、连错三次就停、随时可停、翻页次数有上限。
 */
let run = {
  running: false, round: 0, rounds: 1, autoNext: false,
  jobsDone: 0, jobsTotal: 0, saved: 0, failed: 0, links: 0, log: [],
};

const putRun = () => chrome.storage.local.set({ run });

async function startRun(tabId, autoNext, rounds) {
  if (run.running) return { error: '已经在跑了' };
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (!tab || !/^https:\/\/[^/]*zhipin\.com\//.test(tab.url || ''))
    return { error: '当前标签页不是 BOSS 页面' };

  // 不开自动下一页就只做当前这一页
  rounds = autoNext ? Math.min(MAX_ROUNDS, Math.max(1, parseInt(rounds, 10) || 5)) : 1;
  run = { running: true, round: 0, rounds, autoNext: !!autoNext,
          jobsDone: 0, jobsTotal: 0, saved: 0, failed: 0, links: 0, log: [] };
  await putRun();
  keepAlive(true);

  const inList = async (fn, args) => {
    const [r] = await chrome.scripting.executeScript(
      { target: { tabId }, func: fn, ...(args ? { args } : {}) });
    return r?.result;
  };
  const say = (t, bad) => {
    run.log.unshift({ t, bad: !!bad });
    run.log = run.log.slice(0, 60);
    return putRun();
  };

  let worker = null;      // 开详情专用的标签页
  try {
    for (let r0 = 1; r0 <= rounds; r0++) {
      if (!run.running) break;
      run.round = r0;
      await putRun();

      // ① 存这一页列表原文
      const listPage = await readTabSettled(tabId);
      if (listPage && listPage.len >= MIN_CHARS) {
        try { await saveText(listPage, { auto: true, force: true }); run.saved++; }
        catch (e) { await say('列表页存失败:' + String(e.message).slice(0, 50), true); }
      }

      // ② 抓链接 + 筛掉已存过的
      const h = await inList(harvestLinksInPage);
      const links = h?.links || [];
      run.links += links.length;
      if (!links.length) {
        await say(`第 ${r0} 轮:这一页没抓到岗位链接,停止`, true);
        break;
      }
      let todo = links;
      try {
        const k = await (await fetch('http://localhost:8001/api/boss/known', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ urls: links }),
        })).json();
        todo = k.fresh || links;
        await say(`第 ${r0} 轮:${links.length} 个岗位,${(k.known || []).length} 个已存过,要开 ${todo.length} 个`);
      } catch (e) {
        await say(`第 ${r0} 轮:${links.length} 个岗位(本地服务没应答,不筛重复)`);
      }

      // ③ 逐个打开存详情
      run.jobsTotal += todo.length;
      let miss = 0;
      for (let i = 0; i < todo.length; i++) {
        if (!run.running) break;
        const url = todo[i];
        try {
          if (!worker) worker = await chrome.tabs.create({ url, active: false });
          else await chrome.tabs.update(worker.id, { url });
          await waitLoaded(worker.id);
          await sleep(900);
          const page = await readTabSettled(worker.id);
          if (!page || page.len < MIN_CHARS) throw new Error(`只有 ${page?.len ?? 0} 字`);
          await saveText(page, { auto: true, force: true });
          run.saved++; miss = 0;
          await say(`  ✓ ${(page.h1 || page.title || '').slice(0, 30)}`);
        } catch (e) {
          run.failed++; miss++;
          await say(`  ✗ ${String(e.message).slice(0, 46)}`, true);
          // 连错三次就收手 —— 多半是被限流了,继续打只会更糟
          if (miss >= 3) { await say('连续三个失败,停止', true); run.running = false; break; }
        }
        run.jobsDone++;
        await putRun();
        if (i < todo.length - 1 && run.running) await sleep(4000 + Math.random() * 5000);
      }
      if (!run.running) break;

      // ④ 翻下一页
      if (!run.autoNext || r0 === rounds) break;
      const before = await inList(pageFingerprintInPage);
      const { nextSel } = await chrome.storage.local.get('nextSel');
      let c;
      if (nextSel?.sels?.length) {
        c = await inList(clickLearnedInPage, [nextSel]);
      } else {
        // 没指过就按文字猜。猜是兜底不是主路 —— 猜错会一直抓同一页。
        c = await inList(clickNextInPage);
        await say('没指过「下一页」,这次是按文字猜的');
      }
      if (c?.error) { await say('翻页停止:' + c.error, true); break; }

      // **验证真的翻动了。** 不验证的话,选错元素会一遍遍抓同一页,
      // 然后报「跑了 5 轮」—— 看着在工作,其实一页没动。
      let changed = false;
      for (let t = 0; t < 24; t++) {
        await sleep(500);
        const now = await inList(pageFingerprintInPage);
        if (now?.fp && now.fp !== before?.fp) { changed = true; break; }
      }
      if (!changed) {
        await say('点了「下一页」但列表没变 —— 可能到最后一页了,或者点到的不是真按钮', true);
        break;
      }
      await sleep(1000);
    }
  } catch (e) {
    await say('出错:' + String(e.message).slice(0, 80), true);
  } finally {
    run.running = false;
    keepAlive(false);
    await putRun();
    if (worker) chrome.tabs.remove(worker.id).catch(() => {});
  }
  return { rounds: run.round, jobs: run.jobsDone, saved: run.saved, failed: run.failed };
}

/* harvestLinks 的副本。executeScript 把函数序列化后注入页面,**拿不到外层作用域**,
 * 所以不能复用侧边栏里那份。两处逻辑必须一致 ——
 * 改这里记得改 extension/sidepanel.js 里的 harvestLinks。 */
function harvestLinksInPage() {
  const out = new Map();
  for (const a of document.querySelectorAll('a[href]')) {
    let u;
    try { u = new URL(a.getAttribute('href'), location.href); } catch (e) { continue; }
    if (!/zhipin\.com$/.test(u.hostname.replace(/^www\./, ''))) continue;
    if (!/job_detail|\/job\//.test(u.pathname)) continue;
    const key = u.origin + u.pathname;
    if (!out.has(key)) out.set(key, u.href);
  }
  return { links: [...out.values()] };
}

chrome.runtime.onMessage.addListener((msg, _s, reply) => {
  if (msg?.type === 'armPicker') { armPicker(msg.tabId).then(r => reply(r)); return true; }
  if (msg?.type === 'pickedNext') {
    if (msg.cancelled) { chrome.storage.local.set({ pickState: { cancelled: true } }); return; }
    // 学到的东西必须落 storage:MV3 的 worker 说没就没,模块变量留不住
    chrome.storage.local.set({
      nextSel: { sels: msg.sels, parentSels: msg.parentSels, text: msg.text, tag: msg.tag },
      pickState: { got: true, text: msg.text, n: (msg.sels || []).length },
    });
    return;
  }
  if (msg?.type === 'forgetNext') { chrome.storage.local.remove(['nextSel', 'pickState']); reply?.({ ok: true }); }
  if (msg?.type === 'startRun') {
    startRun(msg.tabId, msg.autoNext, msg.rounds).then(r => reply(r)); return true;
  }
  if (msg?.type === 'stopRun') { run.running = false; reply({ ok: true }); }
  if (msg?.type === 'runStat') { chrome.storage.local.get('run').then(d => reply(d)); return true; }
});


/* ══════════════════════════════════════════════════════════════
 * 「你指一次,我记住」—— 学「下一页」在哪
 *
 * 上一版靠文字找按钮(匹配「下一页」三个字)。能用,但仍然是**我在猜**:
 * 匹配到的可能是个没反应的 span,也可能页面上有好几个类似的东西。
 *
 * 现在改成学:你点一下「指一下下一页」,然后**在页面上点那个按钮**。
 * 我把它的定位方式记下来(不止一种,见下),之后照着点就行。
 * 这跟这个项目里「学详情接口模板」是同一套思路 ——
 * **我没见过的东西就别猜,让用户指一次。**
 *
 * 记多种定位方式并按顺序回退,是因为单一种都可能失效:
 *   ① id —— 最准,但很多按钮没有,且可能是随机生成的
 *   ② ka / aria-label / data-* —— BOSS 用 ka 做埋点,语义稳定(如 ka="page-next")
 *   ③ tag + class 组合 —— 过滤掉看起来是构建工具生成的(css-xxx / 长数字)
 *   ④ nth-child 路径 —— 最后手段,DOM 一动就失效,所以放最后
 * 同时记下按钮文字,点之前核对一下,防止选中了位置相同但含义不同的元素。
 * ══════════════════════════════════════════════════════════════ */

/* 注入到 **ISOLATED** world:那里既能操作 DOM,又能用 chrome.runtime
 * (MAIN world 拿不到 chrome.runtime —— 这个坑在 content.js 那边已经踩过一次)。 */
function armPickerInPage() {
  if (window.__pickArmed) return { ok: true, already: true };
  window.__pickArmed = true;

  const tip = document.createElement('div');
  tip.textContent = '点一下「下一页」按钮 —— 按 Esc 取消';
  tip.style.cssText = 'position:fixed;z-index:2147483647;left:50%;top:14px;transform:translateX(-50%);'
    + 'background:#4c8dff;color:#fff;font:13px/1 -apple-system,sans-serif;padding:10px 16px;'
    + 'border-radius:8px;box-shadow:0 4px 20px rgba(0,0,0,.4);pointer-events:none';
  document.body.appendChild(tip);

  let hot = null;
  const paint = e => {
    if (hot) hot.style.outline = '';
    hot = e.target;
    if (hot && hot.style) hot.style.outline = '2px solid #4c8dff';
  };

  const cssEsc = s => (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/[^\w-]/g, '\\$&');

  function cssPath(el) {
    const parts = [];
    for (let e = el; e && e.nodeType === 1 && parts.length < 6; e = e.parentElement) {
      let p = e.tagName.toLowerCase();
      if (e.parentElement) {
        const sibs = [...e.parentElement.children].filter(x => x.tagName === e.tagName);
        if (sibs.length > 1) p += `:nth-of-type(${sibs.indexOf(e) + 1})`;
      }
      parts.unshift(p);
      if (e.id) { parts[0] = '#' + cssEsc(e.id); break; }
    }
    return parts.join(' > ');
  }

  function selectorsFor(el) {
    const out = [];
    const tag = el.tagName.toLowerCase();
    // ① id(排除看起来是随机生成的)
    if (el.id && !/^\d/.test(el.id) && !/\d{5,}/.test(el.id)) out.push('#' + cssEsc(el.id));
    // ② 语义属性 —— BOSS 的 ka 是埋点标记,比 class 稳定
    for (const a of ['ka', 'data-ka', 'aria-label', 'data-testid', 'title']) {
      const v = el.getAttribute && el.getAttribute(a);
      if (v && v.length < 40 && !/["\\]/.test(v)) out.push(`${tag}[${a}="${v}"]`);
    }
    // ③ class 组合,过掉构建工具生成的
    const cls = String(el.className || '').split(/\s+/)
      .filter(c => c && c.length < 30 && !/\d{3,}/.test(c) && !/^(css|sc|jsx)-/.test(c));
    if (cls.length) out.push(tag + '.' + cls.map(cssEsc).join('.'));
    // ④ 路径,最后手段
    out.push(cssPath(el));
    return [...new Set(out)];
  }

  const cleanup = () => {
    if (hot) hot.style.outline = '';
    tip.remove();
    window.__pickArmed = false;
    document.removeEventListener('mouseover', paint, true);
    document.removeEventListener('click', onClick, true);
    document.removeEventListener('keydown', onKey, true);
  };

  const onKey = e => { if (e.key === 'Escape') { cleanup(); chrome.runtime.sendMessage({ type: 'pickedNext', cancelled: true }); } };

  const onClick = e => {
    // 拦住这一次点击:此刻是「指给我」,不是真要翻页
    e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
    const el = e.target;
    const sels = selectorsFor(el);
    const text = (el.textContent || '').trim().slice(0, 24);
    cleanup();
    chrome.runtime.sendMessage({
      type: 'pickedNext', sels, text, tag: el.tagName,
      // 记一下祖先里最近的 a/button —— 有时文字在 span 上,真正可点的是外面那层
      parentSels: (() => {
        let p = el.parentElement;
        for (let i = 0; i < 3 && p; i++, p = p.parentElement) {
          if (/^(A|BUTTON|LI)$/.test(p.tagName)) return selectorsFor(p);
        }
        return [];
      })(),
    });
  };

  document.addEventListener('mouseover', paint, true);
  document.addEventListener('click', onClick, true);
  document.addEventListener('keydown', onKey, true);
  return { ok: true };
}

/** 照记下来的定位方式点。按顺序试,报告用了哪一条 —— 失败时能看出是哪种定位失效了。 */
function clickLearnedInPage(learned) {
  // **按定位方式的可靠度排序,而不是按「自身优先/祖先优先」。**
  // 实测暴露的问题:你点在 <span class="txt">下一页</span> 上时,
  // 自身定位是 `span.txt` —— 真实页面上这种 class 可能匹配到别处几十个 span;
  // 而祖先定位里的 `a[ka="page-next"]` 是语义属性,准确得多。
  // 按 自身/祖先 排序会先试那个弱的,所以改成按质量排:
  //   #id > 语义属性([ka]/[aria-label]/…) > class 组合 > nth-of-type 路径
  const rank = (sel) => sel.startsWith('#') ? 0
    : /\[[a-z-]+=/.test(sel) ? 1
    : sel.includes('.') ? 2
    : 3;
  const all = [...(learned.sels || []), ...(learned.parentSels || [])]
    .filter(Boolean)
    .sort((a, b) => rank(a) - rank(b));
  for (const sel of all) {
    let el;
    try { el = document.querySelector(sel); } catch (e) { continue; }
    if (!el) continue;
    // 核对文字:防止选中了位置相同但含义不同的元素(页面改版后很容易发生)
    const t = (el.textContent || '').trim().slice(0, 24);
    if (learned.text && t && t !== learned.text && !t.includes(learned.text)) continue;
    // 文字在 span 上时点它本身可能没反应 —— 往上找一层可点的
    let btn = el;
    for (let i = 0; i < 3; i++) {
      if (/^(A|BUTTON)$/.test(btn.tagName)) break;
      const p = btn.parentElement;
      if (!p || /^(A|BUTTON|LI)$/.test(p.tagName)) { btn = p || btn; break; }
      btn = p;
    }
    (btn || el).click();
    return { ok: true, used: sel, text: t };
  }
  return { error: `记下的 ${all.length} 种定位方式都没找到元素 —— 页面可能变了,重新指一次` };
}

async function armPicker(tabId) {
  const [r] = await chrome.scripting.executeScript({
    target: { tabId }, func: armPickerInPage,      // ISOLATED —— 要用 chrome.runtime
  });
  return r?.result || { error: '注入失败' };
}
