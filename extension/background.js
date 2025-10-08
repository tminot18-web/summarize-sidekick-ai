// background.js — MV3 service worker
// Context menu + Alt+S + popup/content messaging -> FastAPI -> draggable/dark-aware bubble

// ====== CONFIG ======
const API_BASE = "https://dbb18930007f.ngrok-free.app"; // <-- your current ngrok
const MAX_RETRIES = 3;
const RETRY_BASE_DELAY_MS = 500;

// ====== HTTP helper with 429 backoff ======
async function postJSON(path, body) {
  let attempt = 0;
  let delay = RETRY_BASE_DELAY_MS;

  while (true) {
    const res = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });

    if (res.ok) {
      const txt = await res.text();
      try { return JSON.parse(txt); }
      catch { throw new Error(`Invalid JSON from backend: ${txt}`); }
    }

    if (res.status === 429 && attempt < MAX_RETRIES) {
      await new Promise(r => setTimeout(r, delay));
      attempt += 1; delay *= 2;
      continue;
    }

    const msg = await res.text();
    throw new Error(`HTTP ${res.status}: ${msg || "Request failed"}`);
  }
}

async function summarize(text, tone = "precise", maxSentences = 3) {
  if (!text || !text.trim()) throw new Error("No text provided to summarize.");
  return postJSON("/summarize", { text, tone, maxSentences });
}

// ====== In-page renderer (injected; no content script required) ======
function inPageRenderBubble(summaryText) {
  const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const bg = prefersDark ? "#151516" : "#ffffff";
  const fg = prefersDark ? "#e7e7ea" : "#111111";
  const border = prefersDark ? "1px solid rgba(255,255,255,0.08)" : "1px solid rgba(0,0,0,0.08)";
  const shadow = prefersDark ? "0 8px 32px rgba(0,0,0,0.6)" : "0 8px 32px rgba(0,0,0,0.25)";

  const sel = window.getSelection();
  const rect = sel && sel.rangeCount ? sel.getRangeAt(0).getBoundingClientRect() : null;

  const el = document.createElement("div");
  el.style.position = "fixed";
  el.style.zIndex = "2147483647";
  el.style.maxWidth = "520px";
  el.style.minWidth = "240px";
  el.style.padding = "0";
  el.style.borderRadius = "12px";
  el.style.boxShadow = shadow;
  el.style.background = bg;
  el.style.color = fg;
  el.style.border = border;
  el.style.fontFamily = "system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif";
  el.style.userSelect = "none";

  const header = document.createElement("div");
  header.textContent = "Summary";
  header.style.fontSize = "12px";
  header.style.letterSpacing = "0.02em";
  header.style.opacity = "0.85";
  header.style.padding = "8px 12px";
  header.style.cursor = "move";
  header.style.borderBottom = prefersDark ? "1px solid rgba(255,255,255,0.07)" : "1px solid rgba(0,0,0,0.07)";
  header.style.display = "flex";
  header.style.alignItems = "center";
  header.style.justifyContent = "space-between";

  const controls = document.createElement("div");

  const copyBtn = document.createElement("button");
  copyBtn.textContent = "Copy";
  copyBtn.ariaLabel = "Copy summary";
  copyBtn.style.all = "unset";
  copyBtn.style.cursor = "pointer";
  copyBtn.style.fontSize = "12px";
  copyBtn.style.padding = "0 6px";
  copyBtn.style.marginRight = "6px";

  const closeBtn = document.createElement("button");
  closeBtn.textContent = "×";
  closeBtn.ariaLabel = "Close";
  closeBtn.style.all = "unset";
  closeBtn.style.cursor = "pointer";
  closeBtn.style.fontSize = "16px";
  closeBtn.style.lineHeight = "1";
  closeBtn.style.padding = "0 4px";

  controls.appendChild(copyBtn);
  controls.appendChild(closeBtn);
  header.appendChild(controls);

  const content = document.createElement("div");
  content.textContent = summaryText;
  content.style.padding = "10px 12px";
  content.style.fontSize = "14px";
  content.style.lineHeight = "1.45";
  content.style.whiteSpace = "pre-wrap";
  content.style.wordBreak = "break-word";
  content.style.userSelect = "text";

  el.appendChild(header);
  el.appendChild(content);

  const top = (rect ? rect.top + window.scrollY : 30) - 12;
  const left = (rect ? rect.left + window.scrollX : 30);
  el.style.top = `${Math.max(12, top)}px`;
  el.style.left = `${Math.max(12, left)}px`;

  let dragging = false, startX = 0, startY = 0, startLeft = 0, startTop = 0;
  const onDown = (e) => {
    dragging = true;
    startX = e.clientX; startY = e.clientY;
    startLeft = el.offsetLeft; startTop = el.offsetTop;
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
    e.preventDefault();
  };
  const onMove = (e) => {
    if (!dragging) return;
    const dx = e.clientX - startX, dy = e.clientY - startY;
    el.style.left = `${Math.max(8, startLeft + dx)}px`;
    el.style.top  = `${Math.max(8, startTop  + dy)}px`;
  };
  const onUp = () => {
    dragging = false;
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
  };
  header.addEventListener("mousedown", onDown);

  closeBtn.onclick = () => el.remove();
  copyBtn.onclick = async () => {
    try {
      await navigator.clipboard.writeText(content.textContent || "");
      const was = copyBtn.textContent;
      copyBtn.textContent = "Copied!";
      setTimeout(() => (copyBtn.textContent = was), 1200);
    } catch { /* ignore */ }
  };

  document.body.appendChild(el);
  setTimeout(() => el.remove(), 20000); // remove after 20s; delete to persist
}

async function showBubbleInTab(tabId, text) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: inPageRenderBubble,
      args: [text]
    });
  } catch (e) {
    // Restricted page fallback: open a new tab with the summary
    const html = `
      <!doctype html><meta charset="utf-8">
      <title>Summary</title>
      <div style="font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;margin:24px;max-width:800px">
        <h1 style="font-size:18px;margin:0 0 10px">Summary</h1>
        <pre style="white-space:pre-wrap;background:#fafafa;padding:12px;border-radius:10px;border:1px solid #eee">${text
          .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")}</pre>
      </div>`;
    const url = "data:text/html;charset=utf-8," + encodeURIComponent(html);
    await chrome.tabs.create({ url });
  }
}

// ====== Context menu ======
chrome.runtime.onInstalled.addListener(() => {
  try {
    chrome.contextMenus.create({
      id: "summarize-selection",
      title: "Summarize selection",
      contexts: ["selection"]
    });
  } catch { /* ignore duplicate on reload */ }
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== "summarize-selection" || !tab?.id) return;

  const selectedText = (info.selectionText || "").trim();
  if (!selectedText) {
    await showBubbleInTab(tab.id, "No text selected.");
    return;
  }

  try {
    const prefs = await chrome.storage.sync.get(["tone", "maxSentences"]);
    const tone = prefs.tone || "precise";
    const maxSentences = Math.max(1, Math.min(10, parseInt(prefs.maxSentences || "3", 10)));
    const result = await summarize(selectedText, tone, maxSentences);
    await showBubbleInTab(tab.id, result.summary || "(no summary returned)");
  } catch (err) {
    await showBubbleInTab(tab.id, `Error: ${String(err)}`);
  }
});

// ====== Keyboard shortcut: Alt+S ======
chrome.commands.onCommand.addListener(async (command) => {
  if (command !== "summarize-selection") return;

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) return;

  try {
    const [{ result: selectedText }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => (window.getSelection()?.toString() || "").trim()
    });
    if (!selectedText) return showBubbleInTab(tab.id, "No text selected.");

    const prefs = await chrome.storage.sync.get(["tone", "maxSentences"]);
    const tone = prefs.tone || "precise";
    const maxSentences = Math.max(1, Math.min(10, parseInt(prefs.maxSentences || "3", 10)));
    const result = await summarize(selectedText, tone, maxSentences);
    await showBubbleInTab(tab.id, result.summary || "(no summary returned)");
  } catch (err) {
    await showBubbleInTab(tab.id, `Error: ${String(err)}`);
  }
});

// ====== Popup/Content → Background messaging ======
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type !== "SUMMARIZE_SELECTION") return;

  (async () => {
    try {
      const { text, tone = "precise", maxSentences = 3 } = msg.payload || {};
      const data = await summarize(text, tone, maxSentences);
      sendResponse({ ok: true, data });
    } catch (err) {
      sendResponse({ ok: false, error: String(err) });
    }
  })();

  return true; // keep channel open for async response
});

