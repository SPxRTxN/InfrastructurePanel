// Настройки агента: «Добавить параметр» показывает поле (и включает его ввод), крестик — прячет и выключает.
// Выключенные поля не отправляются: на узле останется значение по умолчанию.
(function () {
  const form = document.getElementById("agent-config");
  if (!form) return;

  function toggle(row, on) {
    row.hidden = !on;
    row.querySelectorAll("input, select, textarea").forEach((el) => { el.disabled = !on; });
    const opt = form.querySelector(`.cfg-add option[value="${CSS.escape(row.dataset.field)}"]`);
    if (opt) { opt.hidden = on; opt.disabled = on; }
  }

  form.addEventListener("change", (e) => {
    const sel = e.target.closest(".cfg-add select");
    if (!sel || !sel.value) return;
    const row = form.querySelector(`.cfg-row[data-field="${CSS.escape(sel.value)}"]`);
    // Поле сборщика добавляет и сам сборщик: без него параметр ни на что не влияет
    const m = /^f\.collectors\.([a-z]+)\./.exec(sel.value);
    const flag = m && form.querySelector(`.cfg-row[data-field="f.collectors.${m[1]}"]`);
    if (flag && flag.hidden) toggle(flag, true);
    if (row) { toggle(row, true); (row.querySelector("input:not([type=hidden]), select, textarea") || row).focus(); }
    sel.value = "";
  });

  form.addEventListener("click", (e) => {
    const b = e.target.closest(".cfg-remove");
    if (!b) return;
    const row = b.closest(".cfg-row");
    toggle(row, false);
    // Убрали сборщик — убираем и его параметры
    const m = /^f\.collectors\.([a-z]+)$/.exec(row.dataset.field);
    if (m) form.querySelectorAll(`.cfg-row[data-field^="f.collectors.${m[1]}."]`).forEach((r) => toggle(r, false));
  });
})();
