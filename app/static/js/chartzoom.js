// Крупный график с приближением (плагина zoom в vendor нет — делаем сами).
// chartZoom.attach(chart, title): в углу графика появляется кнопка, по ней в <dialog> строится копия графика.
// Колесо — приближение к курсору, перетаскивание — листание по времени, два пальца — щипок, кнопки −/+/сброс.
// Окно показа — это options.scales.x.min/max (индексы меток), без пересоздания графика.
(function () {
  if (!window.Chart) return;
  let dlg, big, title, lo = 0, hi = 0, total = 0;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    return e;
  }

  function build() {
    dlg = el("dialog", "dialog dialog-chart");
    const head = el("div", "card-head");
    title = el("h3");
    const tools = el("div", "row");
    [["−", "Отдалить", () => zoom(1.5, 0.5)], ["+", "Приблизить", () => zoom(1 / 1.5, 0.5)],
     ["Сброс", "Показать всё", () => show(0, total - 1)], ["Закрыть", "Закрыть", () => dlg.close()]].forEach(([t, tip, fn]) => {
      const b = el("button", "btn btn-sm", t);
      b.type = "button"; b.title = tip;
      b.addEventListener("click", fn);
      tools.appendChild(b);
    });
    head.append(title, tools);
    const box = el("div", "chart-box chart-big");
    box.appendChild(document.createElement("canvas"));
    dlg.append(head, box, el("p", "muted small", "Колесо — приблизить, перетаскивание — листать, щипок — на телефоне."));
    document.body.appendChild(dlg);
    dlg.addEventListener("click", e => { if (e.target === dlg) dlg.close(); });
    dlg.addEventListener("close", () => { if (big) { big.destroy(); big = null; } });
    gestures(box.firstChild);
  }

  function show(a, b) {
    const min = Math.max(1, Math.min(total - 1, 5));  // не приближать меньше ~5 точек
    if (b - a < min) { const mid = (a + b) / 2; a = mid - min / 2; b = mid + min / 2; }
    if (a < 0) { b -= a; a = 0; }
    if (b > total - 1) { a -= b - (total - 1); b = total - 1; }
    lo = Math.max(0, a); hi = Math.min(total - 1, b);
    big.options.scales.x.min = lo; big.options.scales.x.max = hi;
    big.update("none");
  }

  // frac — где под курсором (0..1) точка, которая должна остаться на месте
  function zoom(k, frac) {
    const span = hi - lo, point = lo + span * frac, next = Math.min(total - 1, span * k);
    show(point - next * frac, point + next * (1 - frac));
  }

  function gestures(canvas) {
    const ptrs = new Map();
    let pinch = 0;
    const frac = x => {
      const a = big.chartArea, r = canvas.getBoundingClientRect();
      return Math.max(0, Math.min(1, (x - r.left - a.left) / (a.right - a.left)));
    };
    canvas.style.touchAction = "none";
    canvas.addEventListener("wheel", e => { e.preventDefault(); if (big) zoom(e.deltaY > 0 ? 1.25 : 0.8, frac(e.clientX)); }, { passive: false });
    canvas.addEventListener("pointerdown", e => { canvas.setPointerCapture(e.pointerId); ptrs.set(e.pointerId, e.clientX); pinch = 0; });
    canvas.addEventListener("pointermove", e => {
      if (!ptrs.has(e.pointerId) || !big) return;
      const prev = ptrs.get(e.pointerId);
      ptrs.set(e.pointerId, e.clientX);
      if (ptrs.size === 2) {
        const [x1, x2] = [...ptrs.values()], d = Math.abs(x1 - x2);
        if (pinch && d) zoom(pinch / d, frac((x1 + x2) / 2));
        pinch = d;
      } else {
        const a = big.chartArea, shift = (prev - e.clientX) / (a.right - a.left) * (hi - lo);
        show(lo + shift, hi + shift);
      }
    });
    const up = e => { ptrs.delete(e.pointerId); pinch = 0; };
    canvas.addEventListener("pointerup", up);
    canvas.addEventListener("pointercancel", up);
  }

  function open(chart, name) {
    if (!dlg) build();
    title.textContent = name;
    total = chart.data.labels.length;
    const o = chart.config.options || {};
    const x = (o.scales && o.scales.x) || {};
    big = new Chart(dlg.querySelector("canvas"), {
      type: chart.config.type,
      data: { labels: chart.data.labels, datasets: chart.data.datasets.map(d => ({ ...d })) },
      options: { ...o, maintainAspectRatio: false, animation: false,
                 scales: { ...o.scales, x: { ...x, ticks: { ...x.ticks, maxTicksLimit: 16 } } },
                 plugins: { ...o.plugins, legend: { display: chart.data.datasets.length > 1 } } },
    });
    dlg.showModal();
    big.resize();
    show(0, total - 1);
  }

  window.chartZoom = {
    attach(chart, name) {
      const box = chart.canvas.parentElement;
      const b = el("button", "icon-btn chart-expand");
      b.type = "button"; b.title = "Открыть крупно";
      b.innerHTML = '<svg class="icon" aria-hidden="true"><use href="/static/icons.svg#maximize-2"></use></svg>';
      b.addEventListener("click", () => open(chart, name));
      box.appendChild(b);
    },
  };
})();
