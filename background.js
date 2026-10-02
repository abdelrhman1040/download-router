const SERVER = "http://127.0.0.1:5959";

async function post(path, body) {
  const r = await fetch(SERVER + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}

// URL of the page the user is looking at (needs the "tabs" permission).
async function activeTabUrl() {
  try {
    const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    return (tabs[0] && tabs[0].url) || "";
  } catch (e) {
    return "";
  }
}

// Fires the moment a download starts. Chrome waits until suggest() is called.
chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  (async () => {
    try {
      const tabUrl = await activeTabUrl();
      await post("/ask", {
        id: item.id,
        filename: (item.filename || "").split(/[\\/]/).pop(),
        url: item.finalUrl || item.url || "",
        referrer: item.referrer || "",
        page: tabUrl || item.referrer || "",
      });
    } catch (e) {
      // Python app not running -> normal download
    }
    suggest(); // keep Chrome's default file name
  })();
  return true; // async response
});

// When the file is finished, tell Python where it ended up so it can move it.
chrome.downloads.onChanged.addListener((delta) => {
  if (delta.state && delta.state.current === "complete") {
    chrome.downloads.search({ id: delta.id }, ([item]) => {
      if (item) post("/done", { id: item.id, path: item.filename }).catch(() => {});
    });
  }
});
