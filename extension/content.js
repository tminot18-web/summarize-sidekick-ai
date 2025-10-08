function ensurePanel() {
  let panel = document.getElementById("ss-summary-panel");
  if (panel) return panel;
  panel = document.createElement("div");
  panel.id = "ss-summary-panel";
  panel.style.cssText = `
    position: fixed; right: 16px; bottom: 16px; z-index: 2147483647;
    max-width: 420px; background: #111; color: #fff; padding: 12px 14px;
    border-radius: 10px; box-shadow: 0 10px 30px rgba(0,0,0,.35);
    font: 13px/1.45 system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
  `;
  panel.innerHTML = `
    <div id="ss-summary-text" style="white-space:pre-wrap"></div>
    <div style="display:flex; gap:8px; margin-top:10px; justify-content:flex-end">
      <button id="ss-copy">Copy</button>
      <button id="ss-close">Close</button>
    </div>
  `;
  panel.querySelectorAll("button").forEach(btn => {
    btn.style.cssText = "background:#2a2a2a;border:0;color:#fff;padding:6px 10px;border-radius:8px;cursor:pointer";
  });
  document.body.appendChild(panel);
  panel.querySelector("#ss-close").onclick = () => panel.remove();
  panel.querySelector("#ss-copy").onclick = async () => {
    const t = panel.querySelector("#ss-summary-text").innerText;
    try { await navigator.clipboard.writeText(t); } catch {}
  };
  return panel;
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.type !== "SHOW_SUMMARY") return;
  const panel = ensurePanel();
  panel.querySelector("#ss-summary-text").textContent = msg.payload.summary;
});

