"use strict";
void (async () => {
  const message = document.getElementById("connect-message"), input = document.getElementById("link-code");
  try {
    const response = await fetch("/api/local-link", {headers: {"X-DealRadar-UI": "1"}, credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(8000)});
    if (!response.ok) throw new Error();
    const data = await response.json();
    if (!/^[A-Za-z0-9_-]{43}$/.test(data.token) || !/^https:\/\/[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*\.chatgpt\.site$/.test(data.site_url)) throw new Error();
    input.value = data.token;
    const open = document.getElementById("open-radar-site"); open.href = data.site_url; open.hidden = false;
    const copy = document.getElementById("copy-link-code"); copy.disabled = false;
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(input.value); message.textContent = "Код скопирован. Вставь его в окно подключения на своём сайте."; }
      catch { message.textContent = "Раскрой поле ниже и скопируй код вручную. Не отправляй его в чат."; }
    });
    document.getElementById("select-link-code").addEventListener("click", () => { input.type = "text"; input.focus(); input.select(); });
    message.textContent = "Скопируй код и открой сайт. После подключения можно закрыть эту вкладку.";
  } catch { message.textContent = "Программа недоступна. Запусти ярлык и открой новое окно подключения."; }
  window.addEventListener("pagehide", () => { input.value = ""; });
})();
