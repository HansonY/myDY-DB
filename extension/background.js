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
