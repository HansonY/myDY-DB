/* 后台:攒一批再发,别每个响应都打一次本地服务。
 * 数据**只发到 localhost**,不去任何别的地方。 */
const ENDPOINT = 'http://localhost:8001/api/boss/ingest';
const FLUSH_MS = 4000;      // 攒 4 秒
const MAX_BATCH = 40;

let buf = [];
let timer = null;
let stat = { sent: 0, failed: 0, last: null, lastErr: null };

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
    schedule();
    reply?.({ ok: true });
  }
  if (msg?.type === 'status') {
    chrome.storage.local.get(['stat', 'pending']).then(reply);
    return true;
  }
  if (msg?.type === 'flush') { flush().then(() => reply?.({ ok: true })); return true; }
});
