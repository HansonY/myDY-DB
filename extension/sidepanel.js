/* 侧边栏:识别当前页 → 一键把页面文字送去后端 → 后端让 AI 提取。
 *
 * 为什么存「页面文字」而不是接口响应:
 * 页面上你看到的文字就是数据本身,它不会因为平台改接口或改字段名而变。
 * 之前一直卡在「不知道 BOSS 字段叫什么」—— 这条路根本不需要知道。
 */
const $ = s => document.querySelector(s);
const API = 'http://localhost:8001';
let tab = null, page = null;

const esc = s => String(s ?? '').replace(/[&<>"]/g,
  m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]));

/** 在页面里跑:抽正文文字 + 猜这是什么页 */
function grab() {
  // 去掉导航/页脚/脚本这类噪音,留主内容。抓不准也没关系 ——
  // 后面是 AI 提取,它能从啰嗦的文本里挑出岗位信息。
  const drop = 'script,style,noscript,svg,nav,footer,header,iframe';
  const root = document.querySelector('#main,#wrap,.page-job-wrapper,.job-detail,main')
            || document.body;
  const clone = root.cloneNode(true);
  clone.querySelectorAll(drop).forEach(e => e.remove());
  const text = (clone.innerText || '').replace(/\n{3,}/g, '\n\n').trim();

  // 页面类型:只用 URL 判断,不依赖 class 名(那个最容易变)
  const u = location.pathname;
  const kind = /job_detail|\/job\//.test(u) ? 'detail'
             : /chat|geek\/(recommend|job|myjob)|recommend/.test(u) ? 'list'
             : 'other';
  // 标题:h1 优先,退回 document.title
  const h1 = document.querySelector('h1,.job-name,.name');
  return {
    url: location.href.split('?')[0],
    title: (h1 && h1.innerText || document.title || '').trim().slice(0, 80),
    kind, text: text.slice(0, 24000), len: text.length,
  };
}

async function readPage() {
  const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
  tab = t;
  const box = $('#now');
  if (!t || !/zhipin\.com/.test(t.url || '')) {
    page = null;
    box.innerHTML = '<div class="kind">当前页面</div>'
      + '<div class="warn">不是 BOSS 页面 —— 打开 zhipin.com 再来</div>';
    $('#save').disabled = true;
    return;
  }
  try {
    const [r] = await chrome.scripting.executeScript({
      target: { tabId: t.id }, func: grab, world: 'MAIN',
    });
    page = r.result;
  } catch (e) {
    page = null;
    box.innerHTML = '<div class="kind">当前页面</div>'
      + `<div class="warn">读不到页面:${esc(e.message).slice(0, 60)}</div>`;
    $('#save').disabled = true;
    return;
  }
  const label = { detail: '岗位详情', list: '岗位列表', other: '其它页面' }[page.kind];
  box.innerHTML = `<div class="kind">当前页面 · ${label}</div>
    <div class="t">${esc(page.title) || '(没有标题)'}</div>
    <div class="m">${page.len} 字可提取</div>`;
  // other 也允许存 —— 判断可能不准,不该因为我猜错就拦着你
  $('#save').disabled = page.len < 80;
}

async function save(auto) {
  if (!page || !tab) return;
  const btn = $('#save');
  btn.disabled = true; btn.textContent = '提取中…';
  try {
    const r = await fetch(API + '/api/boss/ingest_text', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...page, auto: !!auto }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
    msg(d.note || (d.queued ? '已入队' : '没入队'), d.queued ? 'ok' : '');
    refresh();
  } catch (e) {
    msg(/Failed to fetch/.test(e.message)
      ? '连不上本地服务 —— 在项目目录跑 ./boss.sh web'
      : '出错:' + e.message, 'bad');
  }
  btn.textContent = '存入岗位库';
  btn.disabled = false;
}

function msg(t, cls) { const m = $('#msg'); m.textContent = t || ''; m.className = 'msg ' + (cls || ''); }

async function refresh() {
  try {
    const d = await (await fetch(API + '/api/boss/jobs?limit=8')).json();
    $('#cnt').textContent = d.total ? `${d.total} 个` : '';
    const pend = d.pending || 0;
    const eb = $('#extract');
    eb.disabled = !pend;
    eb.textContent = pend ? `提取待处理的 ${pend} 页` : '没有待提取的';
    $('#list').innerHTML = (d.items || []).length
      ? d.items.map(j => `<div class="it">
          <div class="t">${esc(j.title) || '(无标题)'}</div>
          <div class="m">${esc(j.company || '')}${j.salary_text ? ' · ' + esc(j.salary_text) : ''}</div>
        </div>`).join('')
      : '<div class="empty">还没有。打开一个岗位页,点上面「存入」。</div>';
  } catch (e) {
    $('#list').innerHTML = '<div class="empty">本地服务没开 —— 跑 ./boss.sh web</div>';
  }
}

$('#extract').onclick = async () => {
  const b = $('#extract');
  b.disabled = true; b.textContent = '提取中…(多页一次,请稍等)';
  try {
    const r = await fetch(API + '/api/boss/extract', { method: 'POST' });
    const d = await r.json();
    if (d.error) msg(d.error, 'bad');
    else msg(`新增 ${d.extracted} 个岗位` +
      (d.updated ? `,更新 ${d.updated} 个` : '') +
      ` · 用了 ${d.ai_calls} 次 AI 调用` +
      (d.failed?.length ? ` · ${d.failed.length} 批失败` : ''), 'ok');
    refresh();
  } catch (e) {
    msg(/Failed to fetch/.test(e.message) ? '本地服务没开' : '出错:' + e.message, 'bad');
  }
  b.disabled = false;
};

$('#save').onclick = () => save(false);
$('#open').onclick = () => chrome.tabs.create({ url: API + '/' });
$('#autochk').onchange = e => chrome.storage.local.set({ auto: e.target.checked });

chrome.storage.local.get('auto').then(d => $('#autochk').checked = !!d.auto);
// 换标签页 / 页面跳转都重新识别 —— 侧边栏是常驻的,内容得跟着走
chrome.tabs.onActivated.addListener(() => readPage());
chrome.tabs.onUpdated.addListener(async (id, info) => {
  if (info.status !== 'complete') return;
  await readPage();
  const { auto } = await chrome.storage.local.get('auto');
  // 自动存只在岗位详情上做 —— 列表页自动存会把没看过的也灌进来
  if (auto && page && page.kind === 'detail') save(true);
});
readPage(); refresh();
setInterval(refresh, 6000);
