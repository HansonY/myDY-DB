const $ = s => document.querySelector(s);
async function refresh() {
  const r = await chrome.runtime.sendMessage({ type: 'status' });
  const st = r?.stat || {};
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
$('#flush').onclick = async () => {
  await chrome.runtime.sendMessage({ type: 'flush' });
  setTimeout(refresh, 400);
};
refresh();
