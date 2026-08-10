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
  $('#pending').textContent = r?.pending ?? 0;
  const bad = !!st.lastErr;
  $('#svc').textContent = bad ? '没连上' : '正常';
  $('#svc').className = bad ? 'warn' : 'ok';
  // 列出抓到哪些接口 —— 让你自己判断是真数据还是噪音
  const by = Object.entries(st.byUrl || {}).sort((a,b)=>b[1]-a[1]).slice(0,6);
  $('#urls').innerHTML = by.length
    ? by.map(([u,n]) => `<div class="u"><span>${u.slice(0,34)}</span><b>${n}</b></div>`).join('')
    : '<div class="u dim">还没抓到岗位数据 —— 点开「我的投递」等列表页试试</div>';
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
