// background.js — MV3 service worker
// Context menu + Alt+S + popup messaging -> FastAPI (Render) -> in-page bubble

// ====== CONFIG ======
const API_BASE = "https://summarize-selection.onrender.com"; // Render URL
const MAX_RETRIES = 3;
const RETRY_BASE_DELAY_MS = 600;

// ====== HTTP helper with 429 backoff ======
async function postJSON(path, body) {
  let attempt = 0;
  let delay = RETRY_BASE_DELAY_MS;

  while (true) {
    const res = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    if (res.ok) {
      const txt = await res.text();
      try {
        return JSON.parse(txt);
      } catch {
        throw new Error(`Invalid JSON from backend: ${txt}`);
      }
    }

    if (res.status === 429 && attempt < MAX_RETRIES) {
      await new Promise((r) => setTimeout(r, delay));
      attempt += 1;
      delay *= 2;
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

// ====== Injected UI helpers ======
// One bubble per page that we can update in-place (shows loading then result)
function upsertSummaryBubble(text, opts = {}) {
  const id = "summarize-bubble";
  const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const bg = prefersDark ? "#151516" : "#ffffff";
  const fg = prefersDark ? "#e7e7ea" : "#111111";
  const border = prefersDark ? "1px solid rgba(255,255,255,0.08)" : "1px solid rgba(0,0,0,0.08)";
  const shadow = prefersDark ? "0 8px 32px rgba(0,0,0,0.6)" : "0 8px 32px rgba(0,0,0,0.25)";

  let el = document.getElementById(id);
  if (!el) {
    const sel = window.getSelection();
    const rect = sel && sel.rangeCount ? sel.getRangeAt(0).getBoundingClientRect() : null;

    el = document.createElement("div");
    el.id = id;
    el.style.position = "fixed";
    el.style.zIndex = "2147483647";
    el.style.maxWidth = "520px";
    el.style.minWidth = "240px";
    el.style.borderRadius = "12px";
    el.style.boxShadow = shadow;
    el.style.background = bg;
    el.style.color = fg;
    el.style.border = border;
    el.style.fontFamily = "system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif";
    el.style.left = `${Math.max(12, (rect ? rect.left + window.scrollX : 24))}px`;
    el.style.top = `${Math.max(12, (rect ? rect.top + window.scrollY - 12 : 24))}px`;

    const header = document.createElement("div");
    header.textContent = "Summary";
    header.style.fontSize = "12px";
    header.style.opacity = "0.85";
    header.style.padding = "8px 12px";
    header.style.cursor = "move";
    header.style.borderBottom = prefersDark ? "1px solid rgba(255,255,255,0.07)" : "1px solid rgba(0,0,0,0.07)";

    // drag
    header.addEventListener("mousedown", (e) => {
      const startX = e.clientX, startY = e.clientY;
      const startL = el.offsetLeft, startT = el.offsetTop;
      function move(ev) {
        el.style.left = `${Math.max(8, startL + (ev.clientX - startX))}px`;
        el.style.top = `${Math.max(8, startT + (ev.clientY - startY))}px`;
      }
      function up() {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
      }
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
      e.preventDefault();
    });

    const btns = document.createElement("div");
    btns.style.float = "right";

    const copy = document.createElement("button");
    copy.textContent = "Copy";
    copy.style.all = "unset";
    copy.style.cursor = "pointer";
    copy.style.fontSize = "12px";
    copy.style.marginRight = "8px";
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(body.textContent || "");
        const was = copy.textContent;
        copy.textContent = "Copied!";
        setTimeout(() => (copy.textContent = was), 1200);
      } catch {}
    });

    const close = document.createElement("button");
    close.textContent = "×";
    close.style.all = "unset";
    close.style.cursor = "pointer";
    close.style.fontSize = "16px";
    close.style.marginRight = "6px";
    close.addEventListener("click", () => el.remove());

    btns.appendChild(copy);
    btns.appendChild(close);
    header.appendChild(btns);

    const body = document.createElement("div");
    body.id = id + "-body";
    body.style.padding = "10px 12px";
    body.style.fontSize = "14px";
    body.style.lineHeight = "1.45";
    body.style.whiteSpace = "pre-wrap";
    body.style.wordBreak = "break-word";

    el.appendChild(header);
    el.appendChild(body);
    document.body.appendChild(el);
  }

  const body = document.getElementById(id + "-body");
  if (body) body.textContent = text;
  el.style.opacity = opts.loading ? "0.75" : "1";
}

async function showBubble(tabId, text, loading = false) {
  await chrome.scripting.executeScript({
    target: { tabId },
    func: upsertSummaryBubble,
    args: [text, { loading }],
  });
}

// ====== Actions ======
async function handleSummarizeInTab(tabId, selectedText) {
  // loading bubble
  await showBubble(tabId, "Summarizing…", true);

  try {
    const prefs = await chrome.storage.sync.get(["tone", "maxSentences"]);
    const tone = prefs.tone || "precise";
    const maxSentences = Math.max(1, Math.min(10, parseInt(prefs.maxSentences || "3", 10)));
    const result = await summarize(selectedText, tone, maxSentences);
    await showBubble(tabId, result.summary || "(no summary returned)");
  } catch (err) {
    await showBubble(tabId, `Error: ${String(err)}`);
  }
}

// ====== Context menu ======
chrome.runtime.onInstalled.addListener(() => {
  try {
    chrome.contextMenus.create({
      id: "summarize-selection",
      title: "Summarize selection",
      contexts: ["selection"],
    });
  } catch {}
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== "summarize-selection" || !tab?.id) return;
  const selectedText = (info.selectionText || "").trim();
  if (!selectedText) {
    await showBubble(tab.id, "No text selected.");
    return;
  }
  await handleSummarizeInTab(tab.id, selectedText);
});

// ====== Keyboard shortcut: Alt+S ======
chrome.commands.onCommand.addListener(async (command) => {
  if (command !== "summarize-selection") return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) return;

  try {
    const [{ result: selectedText }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => (window.getSelection()?.toString() || "").trim(),
    });
    if (!selectedText) {
      await showBubble(tab.id, "No text selected.");
      return;
    }
    await handleSummarizeInTab(tab.id, selectedText);
  } catch (err) {
    await showBubble(tab.id, `Error: ${String(err)}`);
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

