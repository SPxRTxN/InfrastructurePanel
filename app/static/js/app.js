// Общий JS: сайдбар, уведомления, подтверждения, сортируемые списки.
// Inline-скриптов нет (CSP script-src 'self'), данные страниц передаются через data-* и <script type="application/json">.

const csrfToken = () => document.querySelector('meta[name="csrf-token"]')?.content || "";

// fetch с CSRF-заголовком и JSON-ответом
async function api(url, options = {}) {
  const opts = { credentials: "same-origin", ...options };
  opts.headers = { "X-CSRF-Token": csrfToken(), Accept: "application/json", ...(options.headers || {}) };
  if (opts.body && typeof opts.body !== "string" && !(opts.body instanceof FormData)) {
    opts.body = JSON.stringify(opts.body);
    opts.headers["Content-Type"] = "application/json";
  }
  const resp = await fetch(url, opts);
  if (resp.status === 401) { location.href = "/login"; return null; }
  if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || resp.statusText);
  return resp.status === 204 ? null : resp.json();
}
window.api = api;

function readJSON(id) {
  const el = document.getElementById(id);
  return el ? JSON.parse(el.textContent) : null;
}
window.readJSON = readJSON;

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
window.escapeHtml = escapeHtml;

// ---------- Время из UTC в локальное: <span data-time="2026-01-01T10:00:00Z"> ----------
document.querySelectorAll("[data-time]").forEach(el => {
  if (el.dataset.time) el.textContent = new Date(el.dataset.time).toLocaleString();
});

// ---------- Сайдбар ----------
document.getElementById("sidebar-toggle")?.addEventListener("click", () => {
  document.body.classList.toggle("sidebar-collapsed");
  api("/settings/sidebar", { method: "POST" }).catch(() => {});
});

// ---------- Быстрый переход по меню: «/» — фокус, ввод фильтрует пункты, Enter — первый найденный ----------
const navSearch = document.getElementById("nav-search");
if (navSearch) {
  const items = [...document.querySelectorAll(".sidebar .nav-item[href]")];
  const visible = () => items.filter(a => !a.hidden);
  navSearch.addEventListener("input", () => {
    const q = navSearch.value.trim().toLowerCase();
    items.forEach(a => { a.hidden = !!q && !a.textContent.toLowerCase().includes(q); });
    document.querySelectorAll(".nav-group").forEach(g => { g.hidden = !g.querySelector(".nav-item:not([hidden])"); });
  });
  navSearch.addEventListener("keydown", e => {
    if (e.key === "Enter" && visible()[0]) location.href = visible()[0].href;
    if (e.key === "Escape") { navSearch.value = ""; navSearch.dispatchEvent(new Event("input")); navSearch.blur(); }
  });
  document.addEventListener("keydown", e => {
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.target.closest("input, textarea, select, [contenteditable], .xterm")) return;
    e.preventDefault();
    navSearch.focus();
  });
}

// ---------- Подтверждение опасных действий ----------
document.addEventListener("click", e => {
  const btn = e.target.closest("[data-confirm]");
  if (btn && !confirm(btn.dataset.confirm)) e.preventDefault();
  if (e.target.closest("[data-back]")) history.back();
  // Модальное окно (<dialog>): открыть по data-dialog-open="id", закрыть кнопкой «Отмена» или кликом по фону
  const open = e.target.closest("[data-dialog-open]");
  if (open) document.getElementById(open.dataset.dialogOpen)?.showModal();
  if (e.target.closest("[data-dialog-close]")) e.target.closest("dialog")?.close();
  else if (e.target.tagName === "DIALOG") e.target.close();
});

// ---------- Сортируемые списки (настройки виджетов) ----------
window.addEventListener("load", () => {
  if (!window.Sortable) return;
  document.querySelectorAll("[data-sortable]").forEach(el => Sortable.create(el, { handle: ".handle", animation: 150 }));
});

// ---------- Уведомления: колокольчик + Notification API ----------
const bell = {
  btn: document.getElementById("bell-btn"),
  menu: document.getElementById("bell-menu"),
  list: document.getElementById("bell-list"),
  count: document.getElementById("bell-count"),
};

function renderBell(data) {
  bell.count.textContent = data.unread;
  bell.count.hidden = !data.unread;
  bell.list.innerHTML = data.items.length
    ? data.items.map(n => `<a class="bell-item ${n.read ? "" : "unread"}" href="${escapeHtml(n.link || "#")}">
        <b>${escapeHtml(n.title)}</b><small>${escapeHtml(n.body)}</small>
        <small>${new Date(n.created_at).toLocaleString()}</small></a>`).join("")
    : '<p class="muted">Уведомлений нет</p>';
}

// Показывает браузерные уведомления для новых непрочитанных (пока страница открыта)
function notifyBrowser(items) {
  let lastSeen = 0;
  try { lastSeen = Number(localStorage.getItem("hl-last-notification") || 0); } catch { /* приватный режим */ }
  const fresh = items.filter(n => !n.read && n.id > lastSeen);
  if (!fresh.length) return;
  try { localStorage.setItem("hl-last-notification", String(Math.max(...fresh.map(n => n.id)))); } catch { /* */ }
  if (!lastSeen || !("Notification" in window) || Notification.permission !== "granted") return;
  fresh.forEach(n => new Notification(n.title, { body: n.body }));
}

async function pollNotifications() {
  try {
    // X-Background: фоновый опрос не продлевает сессию — тайм-аут бездействия считается по действиям человека
    const data = await api("/api/notifications", { headers: { "X-Background": "1" } });
    if (!data) return;
    renderBell(data);
    notifyBrowser(data.items);
  } catch { /* сеть недоступна — попробуем позже */ }
}

if (bell.btn) {
  bell.btn.addEventListener("click", () => {
    bell.menu.hidden = !bell.menu.hidden;
    // Разрешение на уведомления запрашиваем по действию пользователя — браузеры требуют жест
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
  });
  document.addEventListener("click", e => { if (!e.target.closest("#bell")) bell.menu.hidden = true; });
  document.getElementById("bell-read").addEventListener("click", async () => {
    await api("/api/notifications/read-all", { method: "POST" });
    pollNotifications();
  });
  pollNotifications();
  setInterval(pollNotifications, 30000);
}
