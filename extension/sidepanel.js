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

/** 在页面里跑:只抽文字,**不做判断**。
 *
 * 判断「是不是岗位页」全部交给后端 boss_detect.py —— 只有一份实现。
 * 插件里再写一份,两边早晚漂移,而漂移出来的 bug 最难查。
 * 这里也不再看 URL:BOSS 的岗位会出现在 /job_detail、/chat、/web/geek/job、
 * 搜索结果、推荐流里,路径五花八门,靠路径判必漏;平台改一次路由就全废。
 */
function grab() {
  // 去掉导航/页脚/脚本这类噪音,留主内容。抓不准也没关系 ——
  // 后面是 AI 提取,它能从啰嗦的文本里挑出岗位信息。
  const drop = 'script,style,noscript,svg,nav,footer,header,iframe';
  const root = document.querySelector('#main,#wrap,.page-job-wrapper,.job-detail,main')
            || document.body;
  const clone = root.cloneNode(true);
  clone.querySelectorAll(drop).forEach(e => e.remove());
  const text = (clone.innerText || '').replace(/\n{3,}/g, '\n\n').trim();

  // 标题用 document.title —— 它天然带公司名+岗位名,而且**整串当去重键不用解析**。
  // h1 只作补充:h1 依赖 class/结构,是最容易被改版打断的东西。
  return {
    url: location.href,
    title: (document.title || '').trim().slice(0, 120),
    h1: (document.querySelector('h1')?.innerText || '').trim().slice(0, 80),
    text: text.slice(0, 24000), len: text.length,
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
  // 问后端这是不是岗位页(只读接口,不写库)
  let v = null;
  try {
    const r = await fetch(API + '/api/boss/detect', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: page.text, url: page.url, title: page.title }),
    });
    v = await r.json();
  } catch (e) { /* 服务没开 —— 下面照样显示页面信息,只是没有判定 */ }
  page.verdict = v;
  page.kind = v?.kind || 'other';

  const label = v
    ? ({ detail: '岗位详情', list: '岗位列表', other: '不是岗位页' })[v.kind]
    : '本地服务没开,无法判定';
  const sig = v ? `<div class="sig">${
      (v.hit || []).map(h => `<i class="on">${esc(h.label)}</i>`).join('')
    }${(v.miss || []).map(m => `<i>${esc(m)}</i>`).join('')}</div>` : '';
  box.innerHTML = `<div class="kind">当前页面 · ${label}</div>
    <div class="t">${esc(page.h1 || page.title) || '(没有标题)'}</div>
    <div class="m">${page.len} 字可提取</div>
    ${v ? `<div class="why ${v.is_job ? 'yes' : 'no'}">${esc(v.why)}</div>${sig}` : ''}`;
  // other 也允许手动存 —— 判断可能不准,不该因为我猜错就拦着你
  $('#save').disabled = page.len < 80;
  $('#save').textContent = v && !v.is_job ? '仍然存入(我判它不是岗位页)' : '存入岗位库';
}

async function save(auto) {
  if (!page || !tab) return;
  const btn = $('#save');
  btn.disabled = true; btn.textContent = '提取中…';
  try {
    const r = await fetch(API + '/api/boss/ingest_text', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      // 手动点 = force:我的判断可能错,人点了就该存。
      // 自动存不 force,由后端判 —— 否则随便浏览个网页都往库里灌。
      body: JSON.stringify({ ...page, auto: !!auto, force: !auto }),
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
  // 每个 BOSS 页面都送去判一次,**存不存由后端说** ——
  // 前端不再用 kind 自己拦(那等于把判断写两遍)。列表页也存:
  // 它一屏十几个岗位,一次 AI 调用就能全提出来,比逐个点开划算得多。
  if (auto && page && page.len >= 80) save(true);
});
readPage(); refresh();
setInterval(refresh, 6000);
