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


/* ── 第三个职责:盯住「内容变了但 URL 没变」 ────────────────────
 *
 * BOSS 的岗位页是**左右分栏**:左边一列岗位卡片,右边是选中那个的职位描述。
 * 点左边换右边时,很可能既不产生整页加载、也不换 URL —— 于是
 * tabs.onUpdated 和 webNavigation.onHistoryStateUpdated 一个都不响,
 * 自动存就完全不触发。用户实测到的就是这个。
 *
 * 补法:在页面里盯 DOM。内容真的换了才通知后台,而不是每次 DOM 抖动都报。
 * 判据是一个**轻量指纹** —— 标题 + 主标题 + 第一处薪资。
 * 不去认 class(那最容易被改版打断),也不去比整段文字(每次都在变,而且贵)。
 *
 * 800ms 防抖:渲染一次会触发几十上百条 mutation,不防抖等于自己 DDoS 自己。
 */
(() => {
  let last = '';
  let timer = null;

  function fingerprint() {
    // 主标题:h1 优先;拿不到就退回正文前 80 字
    const h1 = document.querySelector('h1')?.innerText?.trim().slice(0, 60) || '';
    // 第一处薪资 —— 换了岗位它几乎必然变
    const m = (document.body?.innerText || '').match(
      /\d{1,3}\s*[-–~]\s*\d{1,3}\s*[Kk千]|\d{3,6}\s*[-–~]\s*\d{3,6}\s*元/);
    const head = h1 || (document.body?.innerText || '').trim().slice(0, 80);
    return `${document.title}|${head}|${m ? m[0] : ''}`;
  }

  function ping() {
    const fp = fingerprint();
    if (fp === last) return;      // 没真的换,别打扰后台
    last = fp;
    try {
      chrome.runtime.sendMessage({ type: 'pageChanged', fp, url: location.href });
    } catch (e) { /* 扩展重载时会短暂失联 */ }
  }

  const schedule = () => {
    clearTimeout(timer);
    timer = setTimeout(ping, 800);
  };

  const start = () => {
    last = fingerprint();          // 首屏由 onUpdated 那条路负责,这里只记基线
    new MutationObserver(schedule).observe(document.body, {
      childList: true, subtree: true, characterData: true,
    });
  };

  if (document.body) start();
  else document.addEventListener('DOMContentLoaded', start, { once: true });
})();
