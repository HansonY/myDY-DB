/* 跑在页面主世界(MAIN world)—— 只有这里能拦到页面自己的 fetch/XHR。
 *
 * 它**不发任何请求**,只是在页面已经发生的请求上搭个便车,把响应抄一份。
 * 反爬看到的流量和你手动浏览完全一样,因为本来就是你手动浏览产生的。
 */
(() => {
  const WANT = /zhipin\.com\/.*(api|wapi|json)/i;
  const SKIP = /(log|track|report|stat|monitor|heartbeat|\.gif|\.png|\.js|\.css)/i;

  const send = (url, body) => {
    // 丢掉查询串 —— 那里常带 token,没必要留
    const clean = String(url).split('?')[0];
    // 通过 postMessage 交给隔离世界的 bridge.js,再由它转给扩展后台。
    // 主世界拿不到 chrome.runtime,必须走这一跳。
    window.postMessage({ __boss: true, url: clean, body }, '*');
  };

  const of = window.fetch;
  window.fetch = async function (...a) {
    const r = await of.apply(this, a);
    try {
      const u = typeof a[0] === 'string' ? a[0] : (a[0] && a[0].url) || '';
      if (WANT.test(u) && !SKIP.test(u) &&
          (r.headers.get('content-type') || '').includes('json')) {
        r.clone().json().then(b => send(u, b)).catch(() => {});
      }
    } catch (e) {}
    return r;
  };

  const oo = XMLHttpRequest.prototype.open;
  const os = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u, ...rest) {
    this.__u = u; return oo.call(this, m, u, ...rest);
  };
  XMLHttpRequest.prototype.send = function (...a) {
    this.addEventListener('load', () => {
      try {
        const u = this.__u || '';
        if (WANT.test(u) && !SKIP.test(u) &&
            (this.getResponseHeader('content-type') || '').includes('json')) {
          send(u, JSON.parse(this.responseText));
        }
      } catch (e) {}
    });
    return os.apply(this, a);
  };
})();
