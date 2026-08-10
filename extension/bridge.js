/* 隔离世界:主世界拿不到 chrome.runtime,所以由它中转。 */
window.addEventListener('message', (e) => {
  if (e.source !== window || !e.data || e.data.__boss !== true) return;
  try {
    chrome.runtime.sendMessage({ type: 'capture', url: e.data.url, body: e.data.body });
  } catch (err) { /* 扩展重载时会短暂失联,忽略 */ }
});
