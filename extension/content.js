/* 跑在页面主世界(MAIN world)—— 只有这里能拦到页面自己的 fetch/XHR。
 *
 * 它**不发任何请求**,只是在页面已经发生的请求上搭个便车,把响应抄一份。
 * 反爬看到的流量和你手动浏览完全一样,因为本来就是你手动浏览产生的。
 */
(() => {
  const WANT = /zhipin\.com\/.*(api|wapi|json)/i;
  // 排除规则要够狠。第一版太宽,结果 5 条捕获全是噪音:
  //   apm-fe.zhipin.com/wapi/zpApm/httpMetrics/getConfig   性能监控
  //   /wapi/zppassport/get/wt                              登录态心跳
  // 这些一打开页面就发,而真正的岗位数据要点进列表才有。
  const SKIP = new RegExp([
    'apm', 'httpMetrics', 'zppassport', 'security', 'captcha', 'verify',
    'log', 'track', 'report', 'monitor', 'heartbeat', 'metric', 'collect',
    'getConfig', 'common/data', 'banner', 'advert',
    '\\.gif', '\\.png', '\\.jpg', '\\.js', '\\.css', '\\.svg',
  ].join('|'), 'i');

  // 只有「看起来含岗位数据」的才送。判据不猜字段名 —— 用一组常见 key 的**任意命中**,
  // 外加「响应里有对象数组」这个结构特征。宁可少收也不要拿噪音把库填满。
  const looksUseful = (b) => {
    const s = JSON.stringify(b || {});
    if (s.length < 120) return false;                    // 太小的多是配置/心跳
    if (/job|Job|position|salary|brand|company|delivery|geek/.test(s)) return true;
    return false;
  };

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
        r.clone().json().then(b => { if (looksUseful(b)) send(u, b); }).catch(() => {});
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
          const b = JSON.parse(this.responseText);
          if (looksUseful(b)) send(u, b);
        }
      } catch (e) {}
    });
    return os.apply(this, a);
  };
})();
