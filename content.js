(() => {
  const BOX_ID = "novaget-box";
  const QUALITIES = [
    ["best", "أفضل جودة"],
    ["1080", "1080p"],
    ["720", "720p"],
    ["480", "480p"],
    ["360", "360p"],
    ["mp3", "صوت MP3"],
  ];

  function isVideoPage() {
    return location.pathname === "/watch" || location.pathname.startsWith("/shorts/");
  }

  function cleanUrl() {
    const u = new URL(location.href);
    if (u.pathname === "/watch") {
      const v = u.searchParams.get("v");
      if (v) return `https://www.youtube.com/watch?v=${v}`;
    }
    return u.origin + u.pathname;
  }

  function flash(btn, cls, text) {
    const old = "⬇ NovaGet";
    btn.className = cls;
    btn.textContent = text;
    setTimeout(() => { btn.className = ""; btn.textContent = old; }, 2200);
  }

  function build() {
    const box = document.createElement("div");
    box.id = BOX_ID;

    const sel = document.createElement("select");
    for (const [val, label] of QUALITIES) {
      const o = document.createElement("option");
      o.value = val;
      o.textContent = label;
      sel.appendChild(o);
    }
    try {
      chrome.storage.local.get("quality", (r) => { if (r && r.quality) sel.value = r.quality; });
    } catch (_e) {}
    sel.addEventListener("change", () => {
      try { chrome.storage.local.set({ quality: sel.value }); } catch (_e) {}
    });

    const btn = document.createElement("button");
    btn.textContent = "⬇ NovaGet";
    btn.addEventListener("click", () => {
      btn.disabled = true;
      try {
        chrome.runtime.sendMessage({ type: "video", url: cleanUrl(), quality: sel.value }, (res) => {
          btn.disabled = false;
          if (chrome.runtime.lastError || !res || !res.ok) {
            flash(btn, "fail", "شغّل NovaGet أولاً");
          } else {
            flash(btn, "ok", "✓ أُضيف للتحميل");
          }
        });
      } catch (_e) {
        btn.disabled = false;
        flash(btn, "fail", "أعد تحميل الصفحة");
      }
    });

    box.append(sel, btn);
    return box;
  }

  function inject() {
    let box = document.getElementById(BOX_ID);
    if (!isVideoPage()) {
      if (box) box.remove();
      return;
    }
    const anchor =
      document.querySelector("ytd-watch-metadata #actions") ||
      document.querySelector("ytd-watch-metadata #top-level-buttons-computed");

    if (!box) box = build();

    if (anchor) {
      box.classList.remove("novaget-floating");
      if (box.parentElement !== anchor) anchor.prepend(box);
    } else if (box.parentElement !== document.body) {
      box.classList.add("novaget-floating");
      document.body.appendChild(box);
    }
  }

  let timer = null;
  const schedule = () => {
    clearTimeout(timer);
    timer = setTimeout(inject, 300);
  };

  document.addEventListener("yt-navigate-finish", schedule);
  new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true });
  inject();
})();
