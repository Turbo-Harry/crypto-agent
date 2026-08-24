(() => {
  if (window.__cryptoAgentOkxCapture) return;
  window.__cryptoAgentOkxCapture = true;
  let timer = null;
  let lastText = "";

  const visibleText = () => {
    const clone = document.body.cloneNode(true);
    clone.querySelectorAll("script,style,noscript,input,textarea,select,[contenteditable='true']")
      .forEach((node) => node.remove());
    return (clone.innerText || clone.textContent || "")
      .replace(/\s+/g, " ").trim().slice(0, 20000);
  };

  const capture = () => {
    timer = null;
    if (document.visibilityState !== "visible") return;
    const text = visibleText();
    if (!text || text === lastText) return;
    lastText = text;
    chrome.runtime.sendMessage({
      type: "okx-page-snapshot",
      payload: {
        captured_ts: Date.now() / 1000,
        url: location.href,
        page_title: document.title,
        visible_text: text,
        source: "okx_chrome",
        metadata: {language: document.documentElement.lang || "", path: location.pathname}
      }
    });
  };

  const schedule = () => {
    clearTimeout(timer);
    timer = setTimeout(capture, 1500);
  };
  new MutationObserver(schedule).observe(document.documentElement, {
    subtree: true, childList: true, characterData: true
  });
  document.addEventListener("visibilitychange", schedule);
  capture();
})();
