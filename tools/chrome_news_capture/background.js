const ENDPOINT = "http://127.0.0.1:8091/browser/okx/events";

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type !== "okx-page-snapshot") return false;
  chrome.storage.local.get(["apiToken"], async ({apiToken}) => {
    try {
      const headers = {"content-type": "application/json"};
      if (apiToken) headers["x-api-token"] = apiToken;
      const response = await fetch(ENDPOINT, {
        method: "POST",
        headers,
        body: JSON.stringify({...message.payload, tab_id: sender.tab?.id ?? null})
      });
      sendResponse({ok: response.ok, status: response.status, body: await response.text()});
    } catch (error) {
      sendResponse({ok: false, error: String(error)});
    }
  });
  return true;
});
