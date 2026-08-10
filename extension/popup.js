const $ = s => document.querySelector(s);
let TAB = null;
async function refresh() {
  const r = await chrome.runtime.sendMessage({ type: 'status' });
  const st = r?.stat || {};
  $('#todo').textContent = r?.tmpl ? (r.todo ?? 0) : '未学会';
  const fill = $('#fill');
  fill.disabled = !r?.tmpl || !r?.todo;
  fill.textContent = r?.filling ? '停止补齐'
    : r?.tmpl ? `补齐详情(${r.todo ?? 0} 条)` : '补齐详情';
  fill.disabled = r?.filling ? false : fill.disabled;
  $('#sent').textContent = st.sent ?? 0;
  const r0 = (st.recent || [])[0];
  $('#lastat').textContent = r0
    ? new Date(r0.at).toLocaleTimeString('zh-CN', {hour12: false})
    : '还没有';
  $('#pending').textContent = r?.pending ?? 0;
  const bad = !!st.lastErr;
  $('#svc').textContent = bad ? '没连上' : '正常';
  $('#svc').className = bad ? 'warn' : 'ok';
  // 最近存了什么 —— 带时间和样例,能直接判断是真数据还是噪音
  const ago = t => { const s = (Date.now() - t) / 1000;
    return s < 60 ? Math.round(s) + '秒前' : s < 3600 ? Math.round(s/60) + '分前'
         : Math.round(s/3600) + '时前'; };
  const rec = st.recent || [];
  $('#urls').innerHTML = rec.length
    ? rec.slice(0, 6).map(r => `<div class="u2">
        <div class="t"><b>${r.n || 1} 条</b>${r.sample ? ' · ' + r.sample : ''}</div>
        <div class="m">${ago(r.at)} · ${r.url.slice(0, 40)}</div></div>`).join('')
    : '<div class="u dim">还没存到岗位数据。点开「我的投递 / 感兴趣」等列表页,'
      + '往下滚几屏 —— 停在首页不会有岗位接口。</div>';
  $('#note').textContent = bad
    ? '本地服务没开。在项目目录跑 ./boss.sh web,数据会先排队,不会丢。'
    : '正常浏览「我的投递 / 我的收藏 / 沟通过的」,数据会自动入库。';
}
$('#fill').onclick = async () => {
  if (!TAB) { $('#note').textContent = '请在 BOSS 页面上打开这个弹窗'; return; }
  const r = await chrome.runtime.sendMessage({ type: 'status' });
  if (r?.filling) {
    await chrome.runtime.sendMessage({ type: 'stopFill' });
    $('#note').textContent = '已停止。';
    setTimeout(refresh, 300); return;
  }
  if (!confirm(`要补 ${r?.todo ?? 0} 条岗位详情。\n\n`
    + '⚠️ 这会产生你没有手动点过的请求 —— 和「只被动记录」不一样。\n'
    + '节奏是每 3–7 秒一条(随机),连错三次自动停。\n\n继续?')) return;
  $('#note').textContent = '补齐中…可以关掉这个弹窗,后台继续。';
  chrome.runtime.sendMessage({ type: 'fill', tabId: TAB });
  setTimeout(refresh, 800);
};

$('#flush').onclick = async () => {
  await chrome.runtime.sendMessage({ type: 'flush' });
  setTimeout(refresh, 400);
};
chrome.tabs.query({ active: true, currentWindow: true }).then(([t]) => {
  if (t && /zhipin\.com/.test(t.url || '')) TAB = t.id;
  refresh();
});
setInterval(refresh, 3000);
