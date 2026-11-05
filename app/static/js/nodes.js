// История узла: по графику на вид датчиков. Память и диски — в процентах, остальное — как прислал агент.
(async function () {
  const box = document.getElementById("node-charts");
  if (!box || !window.Chart) return;
  const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  Chart.defaults.color = css("--muted");
  Chart.defaults.borderColor = css("--border");
  const palette = window.chartPalette || [css("--accent")];  // общая палитра — в charts.js
  const TITLES = { temperature: "Температура, °C", load: "Нагрузка", memory: "Память, %", disk: "Диски, %",
                   frequency: "Частота", fan: "Вентиляторы", power: "Питание", latency: "Задержка" };

  const data = await api(box.dataset.historyUrl);
  if (!data.points.length) {
    box.innerHTML = '<p class="muted">История появится после первых данных агента.</p>';
    return;
  }
  const labels = data.points.map(p => new Date(p.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
  const pct = (used, total) => (used != null && total ? Math.round(used * 1000 / total) / 10 : null);

  // Ряды: {kind: [{label, values}]}
  const series = {};
  const add = (kind, label, values) => (series[kind] = series[kind] || []).push({ label, values });
  add("memory", "RAM", data.points.map(p => pct(p.values["memory.used"], p.values["memory.total"])));
  const mounts = new Set(data.sensors.filter(s => s.kind === "disk" && s.id.endsWith(".total"))
    .map(s => s.id.slice("disks.".length, -".total".length)));
  mounts.forEach(m => add("disk", m, data.points.map(p => pct(p.values[`disks.${m}.used`], p.values[`disks.${m}.total`]))));
  data.sensors.filter(s => s.kind !== "memory" && s.kind !== "disk").forEach(s =>
    add(s.kind, s.unit ? `${s.label}, ${s.unit}` : s.label, data.points.map(p => p.values[s.id] ?? null)));

  Object.entries(series).forEach(([kind, rows]) => {
    rows = rows.filter(r => r.values.some(v => v !== null));
    if (!rows.length) return;
    const wrap = document.createElement("div");
    wrap.innerHTML = `<h4 class="muted small">${escapeHtml(TITLES[kind] || kind)}</h4><div class="chart-box sm"><canvas></canvas></div>`;
    box.appendChild(wrap);
    const chart = new Chart(wrap.querySelector("canvas"), {
      type: "line",
      data: { labels, datasets: rows.map((r, i) => ({ label: r.label, data: r.values, borderColor: palette[i % palette.length],
                                                       tension: 0.35, pointRadius: 0, spanGaps: true })) },
      options: { maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
                 scales: { x: { ticks: { maxTicksLimit: 10 } }, ...(kind === "memory" || kind === "disk" ? { y: { min: 0, max: 100 } } : {}) },
                 plugins: { legend: { display: rows.length > 1 } } },
    });
    if (window.chartZoom) chartZoom.attach(chart, TITLES[kind] || kind);
  });
})();
