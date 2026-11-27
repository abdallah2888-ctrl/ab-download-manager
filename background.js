const BASE = "http://127.0.0.1:48721";
const HEADERS = { "Content-Type": "application/json", "X-NovaGet": "1" };

async function post(path, body) {
  const res = await fetch(BASE + path, {
    method: "POST",
    headers: HEADERS,
    body: JSON.stringify(body),
  });
  return res.ok;
}

// رسائل من زر يوتيوب
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "video") {
    post("/video", { url: msg.url, quality: msg.quality || "best" })
      .then((ok) => sendResponse({ ok }))
      .catch(() => sendResponse({ ok: false }));
    return true; // رد غير متزامن
  }
});

// التقاط التحميلات العادية من كروم
chrome.downloads.onCreated.addListener(async (item) => {
  const url = item.finalUrl || item.url;
  if (!url || !/^https?:\/\//i.test(url)) return;
  try {
    const ok = await post("/download", {
      url,
      filename: item.filename ? item.filename.split(/[\\/]/).pop() : "",
    });
    if (ok) {
      chrome.downloads.cancel(item.id, () => chrome.downloads.erase({ id: item.id }));
    }
  } catch (_e) {
    // NovaGet غير مشغّل: يكمل كروم التحميل بنفسه
  }
});
