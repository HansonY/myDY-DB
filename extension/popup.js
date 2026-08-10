const $ = s => document.querySelector(s);
async function refresh() {
  const r = await chrome.runtime.sendMessage({ type: 'status' });
  const st = r?.stat || {};
  $('#sent').textContent = st.sent ?? 0;
  $('#pending').textContent = r?.pending ?? 0;
  const bad = !!st.lastErr;
  $('#svc').textContent = bad ? '没连上' : '正常';
  $('#svc').className = bad ? 'warn' : 'ok';
  $('#note').textContent = bad
    ? '本地服务没开。在项目目录跑 ./boss.sh web,数据会先排队,不会丢。'
    : '正常浏览「我的投递 / 我的收藏 / 沟通过的」,数据会自动入库。';
}
$('#flush').onclick = async () => {
  await chrome.runtime.sendMessage({ type: 'flush' });
  setTimeout(refresh, 400);
};
refresh();
