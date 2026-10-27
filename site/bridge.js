"use strict";
(() => {
  const endpoint = "http://127.0.0.1:__RADAR_PORT__";
  let token = "", finish;
  const ready = new Promise(resolve => { finish = resolve; });
  function element(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  }
  async function localFetch(path, options = {}) {
    if (!token || typeof path !== "string" || !path.startsWith("/api/") || path.length > 2048) throw new Error("Подключи этот ПК.");
    return fetch(endpoint + path, {...options, mode: "cors", credentials: "omit", redirect: "error", cache: "no-store",
      targetAddressSpace: "loopback", headers: {...options.headers, "X-DealRadar-UI": "1", "X-DealRadar-Link": token},
      signal: options.signal || AbortSignal.timeout(15000)});
  }
  window.DealRadarBridge = Object.freeze({ready, fetch: localFetch, disconnect() { token = ""; location.reload(); }});
  document.body.classList.add("bridge-pending");
  const indicator = element("button", "ПК не подключён", "pc-status text-button");
  indicator.id = "pc-status"; indicator.type = "button";
  indicator.addEventListener("click", () => { if (token) location.reload(); else dialog.showModal(); });
  document.querySelector(".app-header").append(indicator);
  document.getElementById("stop-ui").textContent = "Отключить сайт от ПК";
  document.querySelector(".menu-content p").textContent = "Мониторинг запускается ярлыком на этом ПК. Вход — в окне запуска.";
  const dialog = element("dialog", undefined, "pc-dialog"); dialog.id = "pc-dialog";
  dialog.setAttribute("aria-labelledby", "pc-dialog-title");
  const title = element("h1", "Подключить этот ПК"); title.id = "pc-dialog-title";
  const description = element("p", "Запусти DealScope ярлыком «Радар» и скопируй временный код из локального окна подключения.");
  const form = element("form"); form.autocomplete = "off";
  const label = element("label", "Код подключения"), input = element("input");
  input.id = "pc-link-code"; input.type = "password"; input.autocomplete = "off"; input.spellcheck = false;
  input.maxLength = 43; input.required = true; input.setAttribute("aria-label", "Код подключения ПК");
  label.append(input);
  const error = element("p", "", "notice error"); error.setAttribute("role", "alert"); error.hidden = true;
  const submit = element("button", "Подключить", "button primary"); submit.type = "submit";
  const note = element("p", "Код даёт доступ к DealScope на этом ПК до закрытия программы. Реквизиты Telegram вводи только в окне запуска.", "muted");
  form.append(label, error, submit); dialog.append(title, description, form, note); document.body.append(dialog);
  dialog.addEventListener("cancel", event => event.preventDefault());
  const states = {waiting: "ПК подключён · ожидает запуска", login: "ПК подключён · ручной вход", running: "Мониторинг работает",
    stopped: "ПК подключён · мониторинг остановлен", logout_unconfirmed: "Проверь выход в Telegram"};
  async function updateStatus() {
    try {
      if (!document.hidden) {
        const response = await localFetch("/api/monitor");
        if (!response.ok) throw new Error();
        const result = await response.json();
        indicator.textContent = states[result.state] || "ПК подключён"; indicator.classList.remove("pc-offline");
      }
    } catch { indicator.textContent = "ПК недоступен · переподключить"; indicator.classList.add("pc-offline"); }
    setTimeout(updateStatus, 15000);
  }
  form.addEventListener("submit", async event => {
    event.preventDefault(); error.hidden = true;
    if (!/^[A-Za-z0-9_-]{43}$/.test(input.value.trim())) {
      error.textContent = "Скопируй код целиком из локального окна подключения."; error.hidden = false; return;
    }
    token = input.value.trim(); input.value = ""; submit.disabled = true;
    try {
      const response = await localFetch("/api/bootstrap");
      if (!response.ok) {
        if (response.status === 403) throw new Error("Код не подходит. Открой локальное окно текущего запуска и скопируй новый.");
        throw new Error("Локальная программа отклонила подключение.");
      }
      const result = await response.json();
      if (!/^[A-Za-z0-9_-]{43}$/.test(result.csrf)) throw new Error("Не удалось подтвердить локальную программу.");
      dialog.close(); document.body.classList.remove("bridge-pending"); indicator.textContent = "ПК подключён";
      finish(); void updateStatus();
    } catch (failure) {
      token = "";
      error.textContent = failure instanceof TypeError || failure.name === "TimeoutError"
        ? "Нет связи с ПК. Проверь ярлык и разреши этому сайту доступ к локальной программе в настройках браузера. Открывай сайт на том же ПК."
        : failure.message;
      error.hidden = false;
    } finally { submit.disabled = false; }
  });
  dialog.showModal();
  window.addEventListener("pagehide", () => { token = ""; input.value = ""; });
})();
