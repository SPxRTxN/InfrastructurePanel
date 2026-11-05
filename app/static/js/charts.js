// Графики Chart.js. Цвета берутся из CSS-переменных темы.
(function () {
  if (!window.Chart) return;
  const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const accent = css("--accent"), muted = css("--muted"), border = css("--border");
  Chart.defaults.color = muted;
  Chart.defaults.borderColor = border;
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;

  // Цвета рядов: акцент, дальше — заметно разные оттенки (не --accent-2: при индиго два ряда сливаются)
  // и не цвета статусов (зелёный/жёлтый/красный читались бы как «норма/тревога»). Общая с nodes.js.
  const palette = [accent, "#38bdf8", "#f59e0b", "#f472b6", "#2dd4bf", "#a3a3a3", "#c084fc"];
  window.chartPalette = palette;

  // Легенда и подсказка: вместо стандартных прямоугольников — точка цвета ряда, подпись тем же цветом
  // (как индикаторы статусов). Действует на все графики, включая nodes.js — он подключается после.
  const seriesColor = (type, item) => type === "line" ? item.strokeStyle : item.fillStyle;
  const recolor = base => chart => base(chart).map(item => {
    const color = seriesColor(chart.config.type, item);
    return { ...item, fillStyle: color, strokeStyle: color, lineWidth: 0, fontColor: item.hidden ? muted : color };
  });
  const legend = Chart.defaults.plugins.legend.labels;
  Object.assign(legend, { usePointStyle: true, pointStyle: "circle", boxWidth: 8, boxHeight: 8, padding: 14 });
  legend.generateLabels = recolor(legend.generateLabels);
  const doughnutLabels = Chart.overrides.doughnut.plugins.legend.labels;  // у колец своя функция подписей
  doughnutLabels.generateLabels = recolor(doughnutLabels.generateLabels);
  Object.assign(Chart.defaults.plugins.tooltip, { usePointStyle: true, boxWidth: 8, boxHeight: 8, boxPadding: 4 });
  Chart.defaults.plugins.tooltip.callbacks.labelColor = ctx => {
    const color = ctx.chart.config.type === "line" ? ctx.dataset.borderColor : ctx.element.options.backgroundColor;
    return { borderColor: color, backgroundColor: color };
  };

  // Кольцо с процентом: <canvas class="ring" data-ring="42">
  document.querySelectorAll("canvas[data-ring]").forEach(canvas => {
    const value = Math.max(0, Math.min(100, Number(canvas.dataset.ring) || 0));
    const color = value >= 90 ? css("--bad") : value >= 75 ? css("--warn") : accent;
    new Chart(canvas, {
      type: "doughnut",
      data: { datasets: [{ data: [value, 100 - value], backgroundColor: [color, css("--card-2")], borderWidth: 0, borderRadius: 6 }] },
      options: { cutout: "78%", responsive: false, events: [], plugins: { legend: { display: false }, tooltip: { enabled: false } } },
    });
  });

  // Кольцевая диаграмма по категориям: <canvas data-doughnut='{"labels":[...],"values":[...]}'>
  document.querySelectorAll("canvas[data-doughnut]").forEach(canvas => {
    const d = JSON.parse(canvas.dataset.doughnut);
    new Chart(canvas, {
      type: "doughnut",
      data: { labels: d.labels, datasets: [{ data: d.values, backgroundColor: palette, borderWidth: 0 }] },
      options: { cutout: "68%", maintainAspectRatio: false, plugins: { legend: { position: "right" } } },
    });
  });

  // История сервера: RAM и диски, %
  const hist = document.querySelector("canvas[data-history-url]");
  if (hist) {
    fetch(hist.dataset.historyUrl, { headers: { Accept: "application/json" } }).then(r => r.json()).then(points => {
      const ctx = hist.getContext("2d");
      const grad = ctx.createLinearGradient(0, 0, 0, hist.clientHeight || 260);
      grad.addColorStop(0, accent + "88");
      grad.addColorStop(1, accent + "00");
      const chart = new Chart(hist, {
        type: "line",
        data: {
          labels: points.map(p => new Date(p.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })),
          datasets: [
            { label: "RAM, %", data: points.map(p => p.ram_pct), borderColor: accent, backgroundColor: grad, fill: true, tension: 0.35, pointRadius: 0 },
            { label: "Диски, %", data: points.map(p => p.disk_pct), borderColor: palette[1], borderDash: [4, 4], tension: 0.35, pointRadius: 0 },
          ],
        },
        options: {
          maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
          scales: { y: { min: 0, max: 100 }, x: { ticks: { maxTicksLimit: 12 } } },
        },
      });
      if (window.chartZoom) chartZoom.attach(chart, "История сервера");
    });
  }
})();
