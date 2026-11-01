// Проигрыватель записи консоли (asciicast v2) на xterm.js, только чтение. Вывод — через term.write, не innerHTML.
(function () {
  const box = document.getElementById("rec-term");
  if (!box) return;
  const status = document.getElementById("rec-status");
  const playBtn = document.getElementById("rec-play");
  const speedBtn = document.getElementById("rec-speed");
  const endBtn = document.getElementById("rec-end");
  const SPEEDS = [1, 2, 4];
  let speed = 1, events = [], pos = 0, timer = null, term = null;

  function sayError(text) { status.textContent = text; status.classList.add("bad"); }

  function apply(ev) {
    if (ev[1] === "o") term.write(ev[2]);
    else if (ev[1] === "r") {
      const [c, r] = String(ev[2]).split("x").map(Number);
      if (c > 0 && r > 0) term.resize(c, r);
    }
  }

  function stop() {
    clearTimeout(timer);
    timer = null;
    playBtn.textContent = pos >= events.length ? "Заново" : "Пуск";
  }

  function step() {
    if (pos >= events.length) { stop(); return; }
    apply(events[pos++]);
    if (pos >= events.length) { stop(); return; }
    // Пауза между событиями ограничена 2 с: долгое молчание в записи не должно усыплять проигрыватель
    const gap = Math.min(2, Math.max(0, events[pos][0] - events[pos - 1][0]));
    timer = setTimeout(step, (gap * 1000) / speed);
  }

  playBtn.addEventListener("click", () => {
    if (!term) return;
    if (timer) { stop(); return; }
    if (pos >= events.length) { term.reset(); pos = 0; }
    playBtn.textContent = "Пауза";
    step();
  });
  speedBtn.addEventListener("click", () => {
    speed = SPEEDS[(SPEEDS.indexOf(speed) + 1) % SPEEDS.length];
    speedBtn.textContent = speed + "×";
  });
  endBtn.addEventListener("click", () => {
    if (!term) return;
    stop();
    while (pos < events.length) apply(events[pos++]);
    stop();
  });

  async function load() {
    if (typeof Terminal === "undefined") { sayError("Нет библиотеки xterm.js в app/static/vendor."); return; }
    let text;
    try {
      const r = await fetch(box.dataset.src, { credentials: "same-origin" });
      if (!r.ok) throw new Error(r.status);
      text = await r.text();
    } catch (e) { sayError("Не удалось загрузить запись."); return; }
    const lines = text.split("\n").filter(Boolean);
    const head = JSON.parse(lines[0]);
    events = lines.slice(1).map((l) => JSON.parse(l));
    term = new Terminal({ cols: head.width || 80, rows: head.height || 24, disableStdin: true,
                          cursorBlink: false, fontSize: 14, scrollback: 5000 });
    term.open(box);
  }
  load();
})();
