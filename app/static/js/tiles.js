// Сворачиваемые плитки: у каждой section.card с .card-head появляется кнопка в шапке.
// Свёрнутая плитка показывает шапку и .card-summary (если есть). Состояние — в localStorage (ключ: путь + id плитки);
// без localStorage всё развёрнуто.
(function () {
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    rawSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* без хранилища порядок не запомнится */ } },
    set(k, v) { try { if (v) localStorage.setItem(k, "1"); else localStorage.removeItem(k); } catch (e) { /* без хранилища — не страшно */ } },
  };
  const ICON = '<svg class="icon" aria-hidden="true"><use href="/static/icons.svg#chevron-down"></use></svg>';

  function apply(card, collapsed) {
    card.classList.toggle("collapsed", collapsed);
    const b = card.querySelector(":scope > .card-head > .tile-toggle");
    if (b) {
      b.setAttribute("aria-expanded", String(!collapsed));
      b.title = collapsed ? "Развернуть" : "Свернуть";
    }
  }

  function init() {
    document.querySelectorAll("section.card").forEach((card, i) => {
      const head = card.querySelector(":scope > .card-head");
      if (!head || head.querySelector(".tile-toggle")) return;
      const b = document.createElement("button");
      b.type = "button";
      b.className = "icon-btn tile-toggle";
      b.innerHTML = ICON;
      head.appendChild(b);
      card.dataset.tileKey = "tile:" + location.pathname + ":" + (card.id || "n" + i);
      apply(card, store.get(card.dataset.tileKey) === "1");
    });
  }

  document.addEventListener("click", e => {
    const b = e.target.closest(".tile-toggle");
    if (!b) return;
    const card = b.closest("section.card");
    const now = !card.classList.contains("collapsed");
    apply(card, now);
    store.set(card.dataset.tileKey, now);
  });

  // Ссылка #agent (из блока VPN) раскрывает свёрнутый <details> «Настройки» и плитку вокруг него
  function openHash() {
    const t = location.hash.length > 1 ? document.getElementById(location.hash.slice(1)) : null;
    if (!t) return;
    const card = t.closest("section.card");
    if (card && card.classList.contains("collapsed")) apply(card, false);
    if (t.tagName === "DETAILS") t.open = true;
  }
  window.addEventListener("hashchange", openHash);
  document.addEventListener("click", e => {
    // тот же адрес (#agent уже в строке) hashchange не вызовет — раскрываем по клику сами
    const a = e.target.closest('a[href^="#"]');
    if (a) setTimeout(() => { if (a.hash === location.hash) openHash(); }, 0);
  });

  document.addEventListener("DOMContentLoaded", () => { init(); openHash(); });
  document.addEventListener("htmx:afterSwap", e => {
    init();
    if (e.target && e.target.id === "agent") openHash();  // блок подгрузился уже после открытия страницы с #agent
  });
})();
