"use strict";

const $ = (id) => document.getElementById(id);
const state = {view: "feed", csrf: "", overview: null, decision: "matched", feedBefore: null,
  jobBefore: null, feedPages: 0, jobPages: 0, presets: [], editing: null, selected: "", mode: "all",
  dirty: false, initialized: false, saving: false, switching: false, rechecking: false, tasks: 0,
  feedEpoch: 0, jobEpoch: 0, stopping: false, stopped: false, timer: null, recheckAfter: 0};
const labels = {matched: "Подходит", needs_review: "Нужно проверить", rejected: "Исключено", unassessed: "Не пересчитано",
  held: "Предпросмотр", pending: "Ожидает", in_flight: "Отправляется", sent: "Доставлено", unknown: "Результат неизвестен",
  failed: "Ошибка отправки", cancelled: "Отменено", mixed: "Несколько статусов"};
const errors = {network_error: "Нет связи с Telegram", timeout: "Истекло время ожидания", access_denied: "Нет доступа к каналу",
  rate_limited: "Пауза по ограничению Telegram", auth_required: "Требуется вход в своей консоли", identity_changed: "ID канала изменился; чтение остановлено",
  protocol_error: "Некорректный ответ Telegram", invalid_input: "Не удалось обработать входные данные", storage_error: "Ошибка локальной базы",
  token_rejected: "Токен бота отклонён", recipient_unavailable: "Получатель недоступен", delivery_unconfirmed: "Доставка не подтверждена"};
const reasons = {sold: "Товар помечен как проданный", buy_request: "Это объявление о покупке", excluded_keyword: "Найдено слово-исключение",
  keywords_not_matched: "Не совпали ключевые слова", wrong_drive_type: "Другой тип накопителя", wrong_interface: "Другой интерфейс",
  wrong_capacity_gb: "Другой объём", wrong_brand: "Другой бренд", wrong_model: "Другая модель", wrong_condition: "Другое состояние",
  unknown_drive_type: "Не указан тип накопителя", unknown_interface: "NVMe / SATA не указан однозначно", unknown_capacity_gb: "Не указан объём",
  unknown_brand: "Не указан бренд", unknown_model: "Не указана модель", unknown_condition: "Не указано состояние",
  unknown_currency: "Не указана валюта", currency_not_comparable: "Валюта отличается от пресета", unknown_price: "Не указана полная цена",
  price_above_limit: "Цена выше твоего потолка", not_unambiguous_full_price: "Полная цена не установлена однозначно",
  ambiguous_availability: "Наличие требует проверки", all_required_fields_match: "Обязательные характеристики совпали",
  price_within_limit: "Цена не выше потолка", rule_disabled: "Пресет выключен"};
const views = ["feed", "sources", "notifications"];

function clean(value) { return String(value ?? "").replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]/g, ""); }
function node(tag, text, className) {
  const result = document.createElement(tag);
  if (text !== undefined) result.textContent = clean(text);
  if (className) result.className = className;
  return result;
}
function setText(id, value) { $(id).textContent = clean(value); }
function stamp(value) {
  if (!value) return "Ещё не было";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Дата не указана" : new Intl.DateTimeFormat("ru-RU", {dateStyle: "short", timeStyle: "short"}).format(date);
}
function capacity(value) { return value === 1000 ? "1 ТБ" : value ? value + " ГБ" : "Объём не указан"; }
function price(value, currency) {
  if (value === null || value === undefined || value === "") return "Цена не указана";
  return new Intl.NumberFormat("ru-RU", {maximumFractionDigits: 2}).format(Number(value)) + " " + (currency === "UZS" ? "сум" : currency === "USD" ? "$" : "(валюта не указана)");
}
function pill(status) { return node("span", labels[status] || "Требуется проверка", "status-pill " + (Object.hasOwn(labels, status) ? status : "")); }
function link(url, text) {
  const match = /^https:\/\/t\.me\/([a-z][a-z0-9_]{4,31})\/[1-9][0-9]*$/.exec(url || "");
  if (!match || !state.overview?.sources.some(source => source.source_key === "telegram:" + match[1])) return null;
  const anchor = node("a", text);
  anchor.href = url; anchor.target = "_blank"; anchor.rel = "noopener noreferrer";
  return anchor;
}
function errorMessage(error) { return error instanceof Error ? error.message : "Не удалось выполнить действие."; }
function showError(error) { setText("global-error", errorMessage(error)); $("global-error").hidden = false; }
let toastTimer;
function toast(message) {
  setText("toast", message); $("toast").hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { $("toast").hidden = true; }, 6500);
}
async function api(path, body) {
  const headers = {"X-DealRadar-UI": "1"};
  const options = {method: body === undefined ? "GET" : "POST", headers, credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(15000)};
  if (body !== undefined) {
    headers["Content-Type"] = "application/json"; headers["X-CSRF-Token"] = state.csrf;
    options.body = JSON.stringify(body);
  }
  let response;
  try { response = await (window.DealRadarBridge ? window.DealRadarBridge.fetch(path, options) : fetch(path, options)); }
  catch { throw new Error("Нет связи с локальной программой. Проверь, что DealScope открыт, и обнови страницу."); }
  const result = await response.json();
  if (!response.ok) throw new Error(result.message || "Запрос не выполнен.");
  return result;
}
async function action(control, task) {
  if (state.stopped || state.stopping || control.disabled) return;
  control.disabled = true; state.tasks++;
  try { await task(); } catch (error) { showError(error); }
  finally { state.tasks--; control.disabled = state.stopped; }
}
function button(text, handler, className = "text-button") {
  const result = node("button", text, className); result.type = "button";
  result.addEventListener("click", () => { void action(result, handler); });
  return result;
}
function empty(container, title, description, create = false) {
  const panel = node("div", undefined, "empty-state");
  panel.append(node("h2", title));
  if (description) panel.append(node("p", description));
  if (create) panel.append(button("Новый поиск", () => selectPreset(null), "text-button"));
  container.append(panel);
}
async function confirmAction(text, word = "") {
  setText("confirm-text", text);
  $("confirm-word-label").hidden = !word; setText("confirm-word-name", word);
  const input = $("confirm-word"), ok = $("confirm-ok");
  input.value = ""; ok.disabled = Boolean(word);
  input.oninput = () => { ok.disabled = Boolean(word) && input.value !== word; };
  const dialog = $("confirm-dialog"); dialog.returnValue = "";
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "yes"), {once: true});
    dialog.showModal();
  });
}

async function loadOverview() {
  const overview = await api("/api/overview");
  if (state.stopped) return;
  state.overview = overview;
  setText("database-name", overview.database);
  setText("sync-state", "Обновлено: " + stamp(overview.as_of));
  $("demo-banner").hidden = !overview.synthetic;
  const select = $("feed-source"), selected = select.value;
  select.replaceChildren(node("option", "Все источники")); select.firstChild.value = "";
  for (const source of overview.sources) {
    const option = node("option", source.name + (source.enabled ? "" : " · пауза"));
    option.value = source.source_key; select.append(option);
  }
  if ([...select.options].some((option) => option.value === selected)) select.value = selected;
  renderSources(); renderNotifier();
}
function offerName(offer) {
  return [offer.brand, offer.model].filter(Boolean).join(" ")
    || (offer.drive_type ? offer.drive_type.toUpperCase() + " · модель не указана" : "Накопитель · модель не указана");
}
function renderOffer(item) {
  const offer = item.offer, row = node("article", undefined, "offer-row");
  const open = button("", () => openPost(item.source_key, item.message_id, item), "offer-open");
  open.setAttribute("aria-label", "Открыть " + clean(offerName(offer)));
  const specs = [capacity(offer.capacity_gb), offer.interface ? offer.interface.toUpperCase() : "Интерфейс не указан",
    offer.condition === "new" ? "Новый" : offer.condition === "used" ? "Б/у" : "Состояние не указано"].join(" · ");
  const source = node("span", item.source_name + (item.source_enabled ? "" : " · пауза"), "offer-source");
  if (item.decision !== "matched") source.append(pill(item.decision));
  open.append(node("span", offerName(offer), "offer-name"),
    node("span", offer.price_kind === "full" ? price(offer.price, offer.currency) : "Цена требует проверки", "offer-price" + (offer.price_kind === "full" ? "" : " uncertain")),
    node("span", specs, "offer-specs"), source, node("span", stamp(item.published_at), "offer-time"));
  if (item.decision !== "matched") {
    const values = [...new Set(item.evaluations.filter(entry => entry.decision === item.decision)
      .flatMap(entry => entry.reasons.map(reason => reasons[reason] || "Неоднозначные данные")))];
    if (values.length) open.append(node("span", values.join(" · "), "offer-reasons"));
  }
  row.append(open); return row;
}
async function loadFeed(more = false) {
  const epoch = ++state.feedEpoch, container = $("feed-items");
  if (!more) { container.replaceChildren(); state.feedPages = 0; $("feed-more").hidden = true; }
  if (state.mode === "new") {
    setText("feed-count", "Новый поиск");
    empty(container, "Задай условия поиска", "Сохрани поиск, чтобы проверять новые объявления по этим условиям.");
    return;
  }
  setText("feed-count", "Загрузка…");
  const query = new URLSearchParams({decision: state.decision, source: $("feed-source").value});
  if (state.mode === "edit") query.set("preset", state.selected);
  if (more && state.feedBefore) query.set("before", state.feedBefore);
  const result = await api("/api/feed?" + query);
  if (epoch !== state.feedEpoch || state.stopped) return;
  if (state.decision === "rejected" && result.rejected_scope_version !== 1) {
    state.feedBefore = null; $("feed-more").hidden = true; setText("feed-count", "Нужен перезапуск локальной программы");
    throw new Error("Для исправленных «Исключённых» перезапусти DealScope, затем обнови сайт и подключи ПК заново. Перед закрытием мониторинга проверь подтверждение выхода из Telegram.");
  }
  result.items.forEach((item) => container.append(renderOffer(item)));
  state.feedPages++; state.feedBefore = result.next_before; $("feed-more").hidden = !result.next_before;
  const count = container.querySelectorAll(".offer-row").length;
  const word = count % 10 === 1 && count % 100 !== 11 ? "предложение" : count % 10 >= 2 && count % 10 <= 4 && (count % 100 < 12 || count % 100 > 14) ? "предложения" : "предложений";
  setText("feed-count", count + (result.next_before ? "+" : "") + " " + word);
  if (!count) empty(container, state.mode === "edit" && !state.editing.config.enabled ? "Поиск выключен"
    : state.decision === "rejected" ? "Нет исключённых предложений"
    : state.decision === "needs_review" ? "Нет объявлений для проверки" : "Совпадений пока нет",
    state.decision === "rejected" ? "Здесь только товары текущего поиска, отклонённые, например, по цене или пометке о продаже."
    : state.mode === "edit" ? "Новые публикации проверяются по сохранённым условиям. Сохранённые можно пересчитать кнопкой выше."
      : "Выбери поиск или создай свой.", state.mode === "all" && !state.presets.length);
}
async function openPost(source, id, item = null) {
  const result = await api("/api/post?" + new URLSearchParams({source, message_id: id}));
  const container = $("post-content"); container.replaceChildren();
  const meta = node("div", undefined, "post-meta");
  const text = node("pre", undefined, "post-text");
  function version(index) {
    const current = result.versions[index];
    meta.textContent = clean(current.source_name + " · №" + result.message_id + " · опубликовано " + stamp(current.published_at)
      + (current.edited_at ? " · изменено " + stamp(current.edited_at) : "") + (current.unavailable_at ? " · оригинал сейчас недоступен" : ""));
    text.textContent = clean(current.raw_text || "Текст отсутствует. Изображения не распознаются.");
  }
  container.append(meta);
  if (item) {
    const details = node("details", undefined, "post-evaluations");
    details.append(node("summary", "Проверка версии " + item.revision + ": " + (labels[item.decision] || "Не пересчитано")));
    for (const evaluation of item.evaluations) details.append(node("p", "«" + evaluation.name + "»: " + evaluation.reasons.map((reason) => reasons[reason] || "Неоднозначные данные").join("; ")));
    if (!item.evaluations.length) details.append(node("p", "Для текущей версии поиска проверка ещё не выполнена."));
    if (result.versions[0].revision !== item.revision) details.append(node("p", "Публикация изменилась после загрузки ленты. Обнови ленту для нового результата."));
    container.append(details);
  }
  if (result.versions.length > 1) {
    const label = node("label", "Сохранённая версия", "version-picker"), select = node("select");
    result.versions.forEach((entry, index) => { const option = node("option", "Версия " + entry.revision + " · " + stamp(entry.detected_at)); option.value = index; select.append(option); });
    select.addEventListener("change", () => version(Number(select.value))); label.append(select); container.append(label);
  }
  container.append(text); version(0);
  if (result.versions[0].has_media) container.append(node("p", "Вложение не загружалось. Показан сохранённый текст публикации.", "fine-print"));
  if (result.older_versions) container.append(node("p", "Показаны последние 20 сохранённых версий.", "fine-print"));
  const original = link(result.original_url, "Открыть оригинал в Telegram ↗"); if (original) { original.className = "original-link"; container.append(original); }
  $("post-dialog").showModal();
}

const form = $("search-form");
const field = (name) => form.elements.namedItem(name);
function showView(view) {
  state.view = view;
  document.querySelectorAll(".view").forEach((section) => { section.hidden = section.id !== "view-" + view; });
  document.querySelectorAll("[data-view]").forEach((tab) => {
    const active = tab.dataset.view === view; tab.classList.toggle("active", active);
    if (active) tab.setAttribute("aria-current", "page"); else tab.removeAttribute("aria-current");
  });
  renderSidebar();
}
function renderSidebar() {
  const container = $("preset-select"); container.replaceChildren();
  const all = node("option", "Все предложения"); all.value = ""; container.append(all);
  if (state.mode === "new") {
    const draft = node("option", "Новый поиск"); draft.value = "@new"; draft.disabled = true; container.append(draft);
  }
  for (const entry of state.presets) {
    const control = node("option", entry.config.name + (entry.config.enabled ? "" : " · пауза"));
    control.value = entry.config.key;
    container.append(control);
  }
  container.value = state.mode === "new" ? "@new" : state.selected;
  setText("feed-title", state.mode === "all" ? "Все предложения" : state.mode === "new" ? "Новый поиск" : state.editing?.config.name || "Предложения");
}
function setEditorDisabled(disabled) {
  for (const control of form.querySelectorAll("input,select,textarea,button")) {
    if (control.id !== "feed-source") control.disabled = disabled || state.stopped;
  }
  $("save-preset").disabled = disabled || state.stopped || (state.mode === "edit" && !state.dirty);
}
function updateEditorVisibility() {
  const all = state.mode === "all";
  $("search-name-wrap").hidden = all; $("all-title").hidden = !all; $("save-preset").hidden = all;
  $("search-settings-title").hidden = all;
  $("spec-summary").hidden = all;
  form.querySelectorAll(".editable-field").forEach((element) => { element.hidden = all; });
  $("recheck").hidden = state.mode === "new";
  $("draft-notice").hidden = !state.dirty;
}
function updateSpecSummary() {
  const values = [256, 512, 1000, 2000].filter((value) => field("cap" + value).checked).map(capacity);
  const other = field("other_capacities").value.trim();
  if (other) values.push("другие объёмы");
  if (field("interface").value) values.push(field("interface").value.toUpperCase());
  setText("spec-summary", values.length ? values.join(" · ") : "Любые");
}
function fillEditor(entry) {
  const source = $("feed-source").value;
  form.reset(); $("feed-source").value = source;
  state.editing = entry ? structuredClone(entry) : null;
  state.dirty = false; state.recheckAfter = 0;
  $("recheck-result").hidden = true;
  $("preset-error").hidden = true;
  form.querySelector(".extra-fields").open = !entry;
  if (entry) {
    const rule = entry.config;
    for (const key of ["name", "max_price", "currency", "keyword_mode"]) field(key).value = rule[key];
    for (const key of ["drive_type", "interface"]) field(key).value = rule[key] || "";
    for (const key of ["brands", "models", "keywords", "exclusions"]) field(key).value = rule[key].join("\n");
    field("enabled").checked = rule.enabled;
    for (const value of [256, 512, 1000, 2000]) field("cap" + value).checked = rule.capacities_gb.includes(value);
    field("other_capacities").value = rule.capacities_gb.filter((value) => ![256, 512, 1000, 2000].includes(value)).join(", ");
    field("condition").value = !rule.conditions.length ? "any" : rule.conditions.length === 2 ? "known" : rule.conditions[0];
  }
  updateEditorVisibility(); updateSpecSummary(); setEditorDisabled(state.saving || state.rechecking);
}
async function loadPresets() {
  const result = await api("/api/presets"); state.presets = result.items;
  if (!state.initialized) {
    state.initialized = true;
    const first = result.items.find((entry) => entry.config.enabled) || result.items[0];
    state.mode = first ? "edit" : "new"; state.selected = first?.config.key || "";
    fillEditor(first || null);
  } else if (state.mode === "edit" && !state.dirty && !state.saving) {
    const entry = result.items.find((item) => item.config.key === state.selected);
    if (entry && entry.version !== state.editing?.version) fillEditor(entry);
  }
  renderSidebar();
  if (result.truncated) toast("Показаны последние 200 поисков.");
}
async function selectPreset(key) {
  if (state.switching || state.saving || state.rechecking || state.stopped) return;
  state.switching = true;
  try {
    if (state.dirty && !await confirmAction("Перейти к другому поиску и отменить несохранённые изменения?")) return;
    if (key === "") { state.mode = "all"; state.selected = ""; state.dirty = false; updateEditorVisibility(); }
    else {
      const entry = key === null ? null : state.presets.find((item) => item.config.key === key);
      if (key !== null && !entry) throw new Error("Поиск не найден. Обнови страницу.");
      state.mode = entry ? "edit" : "new"; state.selected = entry?.config.key || "";
      fillEditor(entry);
    }
    $("preset-error").hidden = true;
    showView("feed"); await loadFeed();
    if (key === null) field("name").focus();
  } finally { state.switching = false; renderSidebar(); }
}
function readConfig() {
  const data = new FormData(form);
  const list = (name) => [...new Set(String(data.get(name) || "").split(/\r?\n/).map((item) => item.trim()).filter(Boolean))];
  const raw = String(data.get("other_capacities") || "").split(/[,;\s]+/).filter(Boolean);
  if (raw.some((value) => !/^[0-9]+$/.test(value))) throw new Error("Другие объёмы укажи целыми числами через запятую.");
  const capacities = raw.map(Number);
  for (const value of [256, 512, 1000, 2000]) if (data.has("cap" + value)) capacities.push(value);
  const condition = data.get("condition");
  const name = String(data.get("name") || "").trim(), max = String(data.get("max_price") || "").trim().replace(",", ".");
  if (!name) throw new Error("Укажи название поиска.");
  if (!/^[0-9]+(\.[0-9]{1,2})?$/.test(max) || Number(max) > 1e12) throw new Error("Укажи максимальную цену: число, не более двух знаков после запятой.");
  return {key: state.editing?.config.key || "preset:" + crypto.randomUUID().replaceAll("-", ""), name,
    currency: data.get("currency"), max_price: max, capacities_gb: [...new Set(capacities)],
    drive_type: data.get("drive_type") || null, interface: data.get("interface") || null,
    conditions: condition === "any" ? [] : condition === "known" ? ["new", "used"] : [condition],
    brands: list("brands"), models: list("models"), keywords: list("keywords"), exclusions: list("exclusions"),
    keyword_mode: data.get("keyword_mode"), enabled: data.has("enabled")};
}
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.mode === "all" || state.saving || state.rechecking || state.stopped || state.switching) return;
  $("preset-error").hidden = true;
  try {
    const config = readConfig(), expected = state.editing?.version || 0;
    state.saving = true; setEditorDisabled(true);
    const saved = await api("/api/presets/save", {config, expected_version: expected});
    state.mode = "edit"; state.selected = config.key;
    fillEditor({config, version: saved.version}); renderSidebar();
    await Promise.all([loadPresets(), loadOverview()]); await loadFeed();
    toast("Поиск сохранён. Для сохранённых объявлений — «Применить к истории».");
  } catch (error) { setText("preset-error", errorMessage(error)); $("preset-error").hidden = false; }
  finally { state.saving = false; setEditorDisabled(state.rechecking); }
});
form.addEventListener("input", (event) => {
  if (event.target === $("feed-source") || state.mode === "all") return;
  state.dirty = true; $("preset-error").hidden = true; updateSpecSummary(); updateEditorVisibility(); setEditorDisabled(false);
});
form.addEventListener("change", (event) => {
  if (event.target !== $("feed-source") && state.mode !== "all") {
    state.dirty = true; updateSpecSummary(); updateEditorVisibility(); setEditorDisabled(false);
  }
});
async function recheck() {
  if (state.dirty) throw new Error("Сначала сохрани изменения поиска.");
  if (state.rechecking || state.saving) return;
  state.rechecking = true; setEditorDisabled(true);
  let after = state.recheckAfter, count = 0, pages = 0;
  $("recheck-result").hidden = false;
  try {
    do {
      const result = await api("/api/recheck", {after}); after = result.next_after; count += result.posts; pages++;
      state.recheckAfter = after || 0; setText("recheck-result", "Пересчитано: " + count);
    } while (after !== null && pages < 100 && !state.stopped);
    await loadOverview(); await loadFeed();
    toast(after === null ? "История пересчитана. Рассылка старых объявлений не запускается." : "Пересчёт приостановлен. Нажми ещё раз для продолжения.");
  } finally { state.rechecking = false; setEditorDisabled(false); }
}

function renderSources() {
  const container = $("source-items"); container.replaceChildren();
  for (const source of state.overview.sources) {
    const card = node("article", undefined, "source-card"), top = node("div", undefined, "card-top");
    const status = !source.enabled ? "Приостановлен" : source.last_error_code ? "Ошибка последней проверки" : source.kind === "telegram" && !source.channel_id ? "Ожидает подключения" : source.check_state === "checking" ? "Проверяется" : source.check_state === "catching_up" ? "Догрузка пропусков" : source.last_successful_check_at ? "Последняя проверка успешна" : "Проверок Telegram ещё не было";
    top.append(node("h2", source.name), node("span", status, "status-pill")); card.append(top);
    const facts = node("div", undefined, "source-facts");
    for (const [title, value] of [["Последняя успешная проверка", stamp(source.last_successful_check_at)], ["Последнее получение публикации", stamp(source.last_successful_ingest_at)], ["Продолжение после паузы", source.retry_not_before ? stamp(source.retry_not_before) : "Пауза не назначена"], ["Последняя обработанная публикация", source.last_message_id || "Ещё нет"]]) {
      const fact = node("div"); fact.append(node("span", title), node("div", value)); facts.append(fact);
    }
    card.append(facts);
    if (source.last_error_code) card.append(node("p", errors[source.last_error_code] || "Ошибка проверки источника", "error-detail"));
    card.append(button(source.enabled ? "Приостановить источник" : "Включить источник", async () => {
      await api("/api/sources/state", {key: source.source_key, enabled: !source.enabled});
      await loadOverview(); toast(source.enabled ? "Источник приостановлен; ожидающие уведомления отменены. Уже начатый запрос может завершиться." : "Источник включён. Для чтения нужен работающий мониторинг.");
    }, "button secondary")); container.append(card);
  }
  if (!state.overview.sources.length) empty(container, "Источников пока нет", "Добавь ссылку на канал выше.");
}

$("source-form").addEventListener("submit", event => {
  event.preventDefault();
  void action($("source-add"), async () => {
    $("source-error").hidden = true;
    try {
      await api("/api/sources/add", {address: $("source-address").value, already_joined: $("source-joined").checked, start_from: "connection"});
      $("source-form").reset(); await loadOverview();
      toast("Источник добавлен. Доступ проверится при подключении; старая история не загрузится.");
    } catch (error) {
      setText("source-error", errorMessage(error)); $("source-error").hidden = false;
    }
  });
});
function renderNotifier() {
  const container = $("notifier-summary"); container.replaceChildren();
  const config = state.overview.notifier;
  container.append(node("h2", !config ? "Бот ещё не привязан к этой базе" : config.enabled ? "Личный получатель привязан" : "Отправка уведомлений отключена"));
  container.append(node("p", !config ? "Создание и привязка бота выполняются вручную в своей консоли. Токен здесь не запрашивается." : "Последняя подтверждённая отправка: " + stamp(config.last_successful_send_at) + ". Привязка сама по себе не подтверждает, что сбор с отправкой запущен."));
  if (config?.not_before && new Date(config.not_before) > new Date()) container.append(node("p", "Отправка доступна после " + stamp(config.not_before)));
  if (config?.last_error_code) container.append(node("p", errors[config.last_error_code] || "Отправку нужно проверить."));
  if (config?.enabled) container.append(button("Отключить будущие отправки", async () => {
    if (!await confirmAction("Отключить будущие отправки и отменить ожидающие? Уже начатый запрос может успеть доставиться. Для повторного включения понадобится локальная привязка бота в консоли.")) return;
    await api("/api/notifier/disable", {}); await Promise.all([loadOverview(), loadNotifications()]); toast("Будущие уведомления отключены.");
  }, "button secondary"));
  if (config) container.append(button("Отвязать бота", async () => {
    if (!await confirmAction("Сначала останови мониторинг. Привязка бота и история уведомлений в этой базе будут удалены. Объявления и пресеты сохранятся. Сообщения в Telegram и сам бот останутся.", "ОТВЯЗАТЬ")) return;
    const result = await api("/api/notifier/forget", {confirmation: "ОТВЯЗАТЬ"});
    state.jobEpoch++; await Promise.all([loadOverview(), loadNotifications()]);
    toast(result.checkpoint_complete ? "Привязка и история уведомлений удалены." : "Привязка удалена. Служебный журнал базы занят; закрой другие окна и перезапусти интерфейс.");
  }, "button secondary"));
}
async function loadNotifications(more = false) {
  const epoch = ++state.jobEpoch, query = new URLSearchParams({status: $("notification-status").value});
  if (more && state.jobBefore) query.set("before", state.jobBefore);
  const result = await api("/api/notifications?" + query);
  if (epoch !== state.jobEpoch) return;
  const container = $("notification-items"); if (!more) { container.replaceChildren(); state.jobPages = 0; }
  for (const item of result.items) {
    const card = node("article", undefined, "notification-card"), top = node("div", undefined, "card-top");
    top.append(node("h2", item.source_name + " · №" + item.message_id), pill(item.status)); card.append(top);
    if (item.reason === "owner_confirmed") card.append(node("p", "Отмечено тобой после ручной проверки Telegram; это не подтверждение от API.", "muted"));
    if (item.delivery_text) card.append(node("div", item.delivery_text, "notification-body"));
    else card.append(node("p", item.status === "held" ? "Сохранено для предпросмотра. Отправка по этому заданию не включена." : "Текст карточки ещё не сформирован.", "muted"));
    const bottom = node("div", undefined, "card-bottom");
    bottom.append(node("span", "Обновлено " + stamp(item.updated_at) + " · попыток: " + item.attempts + " · пресетов: " + item.preset_count), button("Публикация", () => openPost(item.source_key, item.message_id)));
    card.append(bottom);
    if (item.last_error_code) card.append(node("p", errors[item.last_error_code] || "Отправка требует проверки", "muted"));
    if (item.retry_not_before && item.status === "pending") card.append(node("p", "Повтор не раньше " + stamp(item.retry_not_before), "muted"));
    if (item.status === "unknown") {
      card.append(node("p", "Telegram мог доставить сообщение. Автоматического повтора нет. Сначала проверь свой личный чат с ботом.", "fine-print"));
      const actions = node("div", undefined, "card-actions");
      for (const [outcome, label] of [["sent", "Есть в моём Telegram"], ["skip", "Пропустить без повтора"]]) actions.append(button(label, async () => {
        if (!await confirmAction(outcome === "sent" ? "Ты проверил свой Telegram и нашёл это уведомление? Будет сохранена твоя ручная отметка, без повторной отправки." : "Отметить неизвестную отправку как пропущенную? Повторной отправки не будет.")) return;
        await api("/api/notifications/resolve", {id: item.id, outcome, source: item.source_key, message_id: item.message_id}); await loadNotifications(); toast("Твоя отметка сохранена. Повторной отправки нет.");
      })); card.append(actions);
    }
    container.append(card);
  }
  state.jobPages++; state.jobBefore = result.next_before; $("notifications-more").hidden = !result.next_before;
  if (!container.childElementCount) empty(container, "Уведомлений в этой выборке пока нет", "Здесь появится история новых совпадений и результаты отправки. Старая история автоматически не рассылается.");
}
async function navigate(view) {
  if (!views.includes(view) || state.stopped) return;
  showView(view);
  if (view === "feed") await loadFeed();
  if (view === "notifications") await loadNotifications();
}
async function refresh(quiet = false) {
  await Promise.all([loadOverview(), loadPresets()]);
  if (state.stopped) return;
  if (state.view === "feed" && (!quiet || state.feedPages <= 1)) await loadFeed();
  if (state.view === "notifications" && (!quiet || state.jobPages <= 1)) await loadNotifications();
}
document.querySelectorAll("[data-view]").forEach((tab) => tab.addEventListener("click", () => { void action(tab, () => navigate(tab.dataset.view)); }));
document.querySelectorAll("[data-close]").forEach((control) => control.addEventListener("click", () => $(control.dataset.close).close()));
$("confirm-ok").addEventListener("click", () => $("confirm-dialog").close("yes"));
$("confirm-cancel").addEventListener("click", () => $("confirm-dialog").close("no"));
$("new-preset").addEventListener("click", () => { void action($("new-preset"), () => selectPreset(null)); });
$("preset-select").addEventListener("change", () => { void action($("preset-select"), () => selectPreset($("preset-select").value)); });
$("feed-source").addEventListener("change", () => { void action($("refresh"), () => loadFeed()); });
function chooseDecision(value, control = $("feed-decision")) {
  if (!["matched", "needs_review", "rejected", "all", "unassessed"].includes(value)) return;
  state.decision = value;
  document.querySelectorAll("[data-decision]").forEach(control => control.setAttribute("aria-pressed", String(control.dataset.decision === value)));
  $("feed-decision").value = ["all", "unassessed"].includes(value) ? value : "";
  void action(control, () => loadFeed());
}
document.querySelectorAll("[data-decision]").forEach(control => control.addEventListener("click", () => chooseDecision(control.dataset.decision, control)));
$("feed-decision").addEventListener("change", () => chooseDecision($("feed-decision").value));
$("notification-status").addEventListener("change", () => { void action($("refresh"), () => loadNotifications()); });
$("feed-more").addEventListener("click", () => { void action($("feed-more"), () => loadFeed(true)); });
$("notifications-more").addEventListener("click", () => { void action($("notifications-more"), () => loadNotifications(true)); });
$("recheck").addEventListener("click", () => { void action($("recheck"), recheck); });
$("refresh").addEventListener("click", () => { $("global-error").hidden = true; void action($("refresh"), () => refresh()); });
$("clear-history").addEventListener("click", () => { void action($("clear-history"), async () => {
  if (state.tasks > 1 || state.saving || state.rechecking || state.switching || state.dirty) throw new Error("Дождись завершения действий и сохрани изменения поиска перед очисткой.");
  if (!await confirmAction("Сначала останови мониторинг. Удалить все объявления, их версии и историю уведомлений из этой базы? Пресеты, источники, привязка бота и позиция чтения сохранятся. Старые публикации повторно не загрузятся. Резервные копии и сообщения в Telegram не удаляются.", "УДАЛИТЬ")) return;
  const result = await api("/api/history/clear", {confirmation: "УДАЛИТЬ"});
  state.feedEpoch++; state.jobEpoch++; state.recheckAfter = 0;
  $("notification-items").replaceChildren(); $("feed-items").replaceChildren();
  $("post-content").replaceChildren(); $("recheck-result").hidden = true;
  await refresh();
  toast(result.checkpoint_complete ? "История очищена. Настройки и позиция чтения сохранены." : "История очищена. Служебный журнал базы занят; закрой другие окна и перезапусти интерфейс.");
}); });
$("stop-ui").addEventListener("click", () => { void action($("stop-ui"), async () => {
  if (state.saving || state.rechecking || state.switching) throw new Error("Дождись завершения текущего действия.");
  if (window.DealRadarBridge) {
    if (await confirmAction("Отключить сайт от этого ПК? Мониторинг продолжит работу в окне запуска.")) {
      state.dirty = false; window.DealRadarBridge.disconnect();
    }
    return;
  }
  if (!await confirmAction("Остановить интерфейс" + (state.dirty ? " и отменить несохранённые изменения" : "") + "? Мониторинг в отдельном PowerShell продолжит работать.")) return;
  state.stopping = true;
  try {
    await api("/api/shutdown", {});
    state.stopped = true; state.dirty = false; clearInterval(state.timer);
    $("shutdown-notice").hidden = false; setText("sync-state", "Интерфейс остановлен");
    document.querySelectorAll("button,input,select,textarea").forEach((control) => { control.disabled = true; });
  } finally { state.stopping = false; }
}); });
document.addEventListener("click", (event) => {
  for (const details of document.querySelectorAll("#app-menu[open]")) {
    if (!details.contains(event.target)) details.open = false;
  }
});
window.addEventListener("beforeunload", (event) => {
  if (state.dirty) { event.preventDefault(); event.returnValue = ""; }
});
async function start() {
  try {
    if (window.DealRadarBridge) await window.DealRadarBridge.ready;
    state.csrf = (await api("/api/bootstrap")).csrf;
    await refresh();
    state.timer = setInterval(() => {
      if (!state.stopped && !state.stopping && !state.dirty && !state.saving && !state.rechecking && !state.switching && !state.tasks
          && !document.hidden && !document.querySelector("dialog[open],details[open]")) {
        void action($("refresh"), () => refresh(true));
      }
    }, 20000);
  } catch (error) { showError(error); }
}

void start();
