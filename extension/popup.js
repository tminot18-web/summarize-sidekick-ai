const toneEl = document.getElementById("tone");
const maxEl = document.getElementById("max");
const statusEl = document.getElementById("status");
const outEl = document.getElementById("out");

async function loadPrefs() {
  return new Promise(resolve => {
    chrome.storage.sync.get(["tone", "maxSentences"], data => resolve(data));
  });
}
async function savePrefs(tone, maxSentences) {
  return new Promise(resolve => {
    chrome.storage.sync.set({ tone, maxSentences }, () => resolve());
  });
}

(async () => {
  const prefs = await loadPrefs();
  if (prefs.tone) toneEl.value = prefs.tone;
  if (prefs.maxSentences) maxEl.value = prefs.maxSentences;
})();

document.getElementById("summarize").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) return;

  const tone = toneEl.value;
  const maxSentences = Math.max(1, Math.min(10, parseInt(maxEl.value || "3", 10)));
  await savePrefs(tone, maxSentences);

  // Fetch selection text from the page without a content script
  const [{ result: selectedText }] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: () => (window.getSelection()?.toString() || "").trim(),
  });

  if (!selectedText) {
    statusEl.textContent = "";
    outEl.innerHTML = `<span class="error">No text selected.</span>`;
    return;
  }

  statusEl.textContent = "Summarizing…";
  outEl.textContent = "";

  // Ask background to call the backend
  chrome.runtime.sendMessage(
    { type: "SUMMARIZE_SELECTION", payload: { text: selectedText, tone, maxSentences } },
    resp => {
      if (!resp?.ok) {
        statusEl.textContent = "";
        const msg = String(resp?.error || "Request failed");
        const friendly =
          msg.includes("429") ? "Rate limited. Try again shortly." :
          msg.includes("402") || msg.toLowerCase().includes("quota") ? "Billing/credits issue." :
          msg.includes("502") ? "Model is busy. Try again." :
          "Something went wrong.";
        outEl.innerHTML = `<span class="error">${friendly}</span>`;
        return;
      }
      statusEl.textContent = "";
      outEl.textContent = resp.data.summary;
    }
  );
});

