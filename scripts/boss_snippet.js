/* BOSS 直聘 · 浏览器控制台录制片段
 * ───────────────────────────────────────────────────────────
 * 用法:
 *   1. 在**已登录**的 zhipin.com 标签页按 F12 → Console
 *   2. 把这整段粘进去回车(只需一次)
 *   3. 正常点开「我的投递 / 我的收藏 / 沟通过的」,各往下滚几屏
 *   4. 回控制台输入  bossDump()  回车 → 浏览器会下载一个 json 文件
 *   5. 把那个文件放进项目的 data/boss_capture/ 目录
 *
 * 为什么用这个而不是自动化浏览器:
 *   · 用你已经登录的会话,不用重新登录
 *   · 请求是你自己点出来的,反爬没什么可挑剔
 *   · **cookie 和 token 全程留在浏览器里**,不经手任何地方
 *   · 下载是本地行为,数据不发往任何服务器
 *
 * 它只做两件事:记录响应、下载文件。不发送、不修改、不点击。
 */
(() => {
  if (window.__bossRec) { console.log('%c已经在录了。','color:#4c8dff'); return; }

  const rec = [];
  // 只记 zhipin 自己的接口;排除日志/埋点/静态资源
  const want = u => /zhipin\.com\/.*(api|wapi|json)/i.test(u)
                 && !/(log|track|report|stat|monitor|heartbeat|\.gif|\.png|\.js|\.css)/i.test(u);

  const push = (url, body) => {
    // 丢掉查询串 —— 那里常带 token,没必要留
    const clean = String(url).split('?')[0];
    rec.push({ url: clean, at: new Date().toISOString(), body });
    console.log('%c● 记录 ' + clean.replace('https://www.zhipin.com',''), 'color:#5cb87a');
  };

  // ── 拦 fetch ──
  const of = window.fetch;
  window.fetch = async function (...a) {
    const r = await of.apply(this, a);
    try {
      const u = (typeof a[0] === 'string') ? a[0] : (a[0] && a[0].url) || '';
      if (want(u) && (r.headers.get('content-type') || '').includes('json')) {
        r.clone().json().then(b => push(u, b)).catch(() => {});
      }
    } catch (e) {}
    return r;
  };

  // ── 拦 XHR ──
  const oo = XMLHttpRequest.prototype.open;
  const os = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u, ...rest) {
    this.__u = u; return oo.call(this, m, u, ...rest);
  };
  XMLHttpRequest.prototype.send = function (...a) {
    this.addEventListener('load', () => {
      try {
        if (want(this.__u || '') &&
            (this.getResponseHeader('content-type') || '').includes('json')) {
          push(this.__u, JSON.parse(this.responseText));
        }
      } catch (e) {}
    });
    return os.apply(this, a);
  };

  window.__bossRec = rec;
  window.bossDump = () => {
    if (!rec.length) {
      console.log('%c还没记到东西 —— 先点开列表页并滚动几下。', 'color:#ff7a45');
      return;
    }
    const blob = new Blob([JSON.stringify(rec, null, 1)], {type: 'application/json'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'boss_capture_' + Date.now() + '.json';
    a.click();
    // 顺带在控制台summarize一下,方便你直接看抓到了什么
    const by = {};
    rec.forEach(r => by[r.url] = (by[r.url] || 0) + 1);
    console.table(Object.entries(by).map(([url, n]) =>
      ({ 接口: url.replace('https://www.zhipin.com',''), 次数: n })));
    console.log('%c已下载 ' + rec.length + ' 条。放进项目的 data/boss_capture/ 目录。',
                'color:#4c8dff;font-weight:600');
  };

  console.log('%c✓ 录制中。现在正常点「我的投递 / 我的收藏 / 沟通过的」,'
            + '滚几屏,然后输入 bossDump() 下载。',
              'color:#4c8dff;font-weight:600;font-size:13px');
})();
