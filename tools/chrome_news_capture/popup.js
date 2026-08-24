const status = document.getElementById("status");
const token = document.getElementById("token");
chrome.storage.local.get(["apiToken"], (v) => { token.value = v.apiToken || ""; });
token.addEventListener("change", () => chrome.storage.local.set({apiToken: token.value.trim()}));
document.getElementById("enable").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  if (!tab?.url || !/^https:\/\/(www|my)\.okx\.com\//.test(tab.url)) {
    status.textContent = "当前不是 OKX 页面"; return;
  }
  try {
    await chrome.scripting.executeScript({target: {tabId: tab.id}, files: ["content.js"]});
    status.textContent = "已监听当前标签页";
  } catch (e) { status.textContent = `启用失败：${e.message}`; }
});
