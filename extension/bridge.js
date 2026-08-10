/* 隔离世界:两个职责
 *   1. 把主世界抄到的响应转给后台(主世界拿不到 chrome.runtime)
 *   2. 替后台发「补详情」的请求 —— 它跑在 zhipin 源上,cookie 天然带着,
 *      和页面自己发的请求没有区别,不需要任何凭证经手
 */
window.addEventListener('message', (e) => {
  if (e.source !== window || !e.data || e.data.__boss !== true) return;
  try {
    chrome.runtime.sendMessage({ type: 'capture', url: e.data.url, body: e.data.body });
  } catch (err) { /* 扩展重载时会短暂失联,忽略 */ }
});

chrome.runtime.onMessage.addListener((msg, _s, reply) => {
  if (msg?.type !== 'fetchOne') return;
  (async () => {
    try {
      const r = await fetch(msg.url, {
        credentials: 'include',
        headers: { 'accept': 'application/json, text/plain, */*' },
      });
      if (!r.ok) { reply({ ok: false, status: r.status }); return; }
      const b = await r.json();
      // 走同一条入库管道,不另开一条
      chrome.runtime.sendMessage({ type: 'capture', url: msg.url, body: b });
      reply({ ok: true });
    } catch (e) {
      reply({ ok: false, err: String(e.message || e) });
    }
  })();
  return true;      // 异步回复
});
