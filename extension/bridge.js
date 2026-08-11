/* 版本号:面板拿它判断这个标签页跑的是不是新代码。改了 bridge.js 就 +1。 */
const BRIDGE_VERSION = 2;

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
  // 「这个标签页上,插件的页面脚本在跑吗?」
  // **重载扩展不会让已经打开的标签页拿到新代码** —— content script 只在
  // 页面加载时注入。用户重载了扩展但没刷新 BOSS 页面时,那个页面还跑着旧脚本
  // (旧版根本没有内容变化观察器),表现就是「自动存怎么都不生效」,
  // 而面板那边完全看不出来。所以给面板一个探针。
  if (msg?.type === 'ping') { reply({ alive: true, v: BRIDGE_VERSION }); return; }
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

  // 每次都在变、但和「看的是哪个岗位」无关的东西。不剔掉的话页面上一个
  // 「刚刚活跃」的计时就会让指纹一直变,等于每秒都在存。
  const VOLATILE = /刚刚|\d+\s*(秒|分钟|小时|天)前|正在输入|未读/g;

  /** 整页文字的哈希。
   *
   * ⚠️ **必须算整页,不能只取开头。** 第一版我用「title + h1 + 第一处薪资」,
   * 在左右分栏页上三个信号**全取自左边那列** —— 点左边换右边时它们一个都不变,
   * 于是永远不触发。合成页实测:换岗位 0 次触发。这就是「打开就存没生效」的根因。
   *
   * djb2,够快(几百 KB 也是毫秒级),而且只要页面任何一处文字变了它就变。
   */
  function fingerprint() {
    const t = (document.body?.innerText || '').replace(VOLATILE, '');
    let h = 5381;
    for (let i = 0; i < t.length; i++) h = ((h << 5) + h + t.charCodeAt(i)) | 0;
    return `${document.title}#${t.length}#${(h >>> 0).toString(36)}`;
  }

  // 两次通知之间的最小间隔。
  // 指纹是整页哈希,页面上任何自变内容(计时器、未读角标、轮播)都会让它变 ——
  // 剔除常见的那几种之后仍不可能穷尽。人切换岗位最快也要一秒以上,
  // 所以设 3 秒既不会漏掉真实切换,又能把自变内容造成的重复压下去。
  const MIN_GAP_MS = 3000;
  let lastAt = 0;

  function ping() {
    const fp = fingerprint();
    if (fp === last) return;      // 没真的换,别打扰后台
    const now = performance.now();
    if (now - lastAt < MIN_GAP_MS) { schedule(); return; }   // 太密,推迟再看
    lastAt = now;
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
