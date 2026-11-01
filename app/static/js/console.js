// SSH-консоль и передача файлов. Консоль: билет (POST с CSRF) → WebSocket → xterm.js, протокол — app/services/console.py.
// Файл: PUT /servers/{id}/files, тело — сам файл (XHR ради полосы прогресса).
(function () {
  const access = document.getElementById("console-access");
  if (!access) return;
  const serverId = access.dataset.server;
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const credSel = document.getElementById("console-cred");
  const cmdInp = document.getElementById("console-cmd");
  const passInp = document.getElementById("console-password");
  const viaSel = document.getElementById("console-via");  // есть, только если у сервера агент с туннелем
  const via = () => (viaSel && viaSel.value === "agent" ? "agent" : "");

  // Поля «Пользователь/Пароль» — только для входа вручную
  function syncManual() {
    const manual = credSel.selectedOptions[0].dataset.manual === "1";
    access.querySelectorAll(".console-manual").forEach((el) => { el.hidden = !manual; });
  }
  credSel.addEventListener("change", syncManual);
  syncManual();
  const isManual = () => credSel.selectedOptions[0].dataset.manual === "1";

  function say(el, text, bad) {
    el.textContent = text;
    el.classList.toggle("bad", !!bad);
  }

  // ---------- Консоль: вкладки ----------
  // У каждой вкладки свой xterm и свой WebSocket (свой сеанс SSH). Всегда есть хотя бы одна вкладка.
  const status = document.getElementById("console-status");
  const tabsEl = document.getElementById("term-tabs");
  const panes = document.getElementById("console-panes");
  const addBtn = document.getElementById("term-add");
  const connectBtn = document.getElementById("console-connect");
  const placeholder = document.getElementById("term-placeholder");
  const MAX_TABS = Number(addBtn.dataset.max) || 10;
  const tabs = [];
  let active = null;
  let counter = 0;

  function svgIcon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `/static/icons.svg#${name}`);
    svg.appendChild(use);
    return svg;
  }

  function sync() {
    // Кнопки и строка состояния показывают активную вкладку
    addBtn.disabled = tabs.length >= MAX_TABS;
    connectBtn.hidden = !active || active.state === "open" || active.state === "connecting";
    say(status, active ? active.msg : "", active && active.bad);
    tabs.forEach((t) => {
      t.btn.classList.toggle("active", t === active);
      t.btn.setAttribute("aria-selected", t === active ? "true" : "false");
      t.btn.dataset.state = t.state;
      t.pane.hidden = t !== active;
    });
  }

  function setMsg(tab, text, bad) {
    tab.msg = text;
    tab.bad = !!bad;
    if (tab === active) sync();
  }

  function activate(tab) {
    active = tab;
    sync();
    if (tab.fit) { tab.fit.fit(); tab.term.focus(); }  // скрытая панель не измеряется — подгоняем при показе
  }

  function newTab() {
    if (tabs.length >= MAX_TABS) return null;
    counter += 1;
    const tab = { n: counter, state: "idle", msg: "", bad: false, term: null, fit: null, ws: null };
    tab.btn = document.createElement("div");
    tab.btn.className = "term-tab";
    tab.btn.setAttribute("role", "tab");
    const label = document.createElement("button");
    label.type = "button";
    label.className = "term-tab-label";
    label.textContent = `Терминал ${tab.n}`;
    label.addEventListener("click", () => activate(tab));
    const close = document.createElement("button");
    close.type = "button";
    close.className = "term-tab-close";
    close.title = "Закрыть терминал";
    close.appendChild(svgIcon("x"));
    close.addEventListener("click", () => closeTab(tab));
    tab.btn.append(label, close);
    tabsEl.appendChild(tab.btn);
    tab.pane = document.createElement("div");
    tab.pane.className = "console-term";
    tab.pane.appendChild(placeholder.content.cloneNode(true));
    panes.appendChild(tab.pane);
    tabs.push(tab);
    activate(tab);
    return tab;
  }

  function closeTab(tab) {
    if (tab.ws) tab.ws.close();
    if (tab.term) tab.term.dispose();
    tab.btn.remove();
    tab.pane.remove();
    const i = tabs.indexOf(tab);
    tabs.splice(i, 1);
    if (!tabs.length) { newTab(); return; }
    if (active === tab) activate(tabs[Math.min(i, tabs.length - 1)]);
    else sync();
  }

  async function connect(tab) {
    if (typeof Terminal === "undefined" || typeof FitAddon === "undefined") {
      setMsg(tab, "Нет библиотеки xterm.js в app/static/vendor — см. README, раздел «SSH-консоль».", true);
      return;
    }
    tab.state = "connecting";
    setMsg(tab, "Подключение…");
    let ticket;
    try {
      const r = await fetch(`/servers/${serverId}/console/ticket`, {
        method: "POST", headers: { "X-CSRF-Token": csrf }, credentials: "same-origin",
      });
      if (!r.ok) throw new Error(r.status);
      ticket = (await r.json()).ticket;
    } catch (err) {
      tab.state = "closed";
      setMsg(tab, "Нет доступа к консоли (обновите страницу или войдите заново).", true);
      return;
    }

    if (tab.term) tab.term.dispose();
    tab.pane.replaceChildren();
    tab.term = new Terminal({ cursorBlink: true, fontSize: 14, scrollback: 5000 });
    tab.fit = new FitAddon.FitAddon();
    tab.term.loadAddon(tab.fit);
    tab.term.open(tab.pane);
    if (tab === active) tab.fit.fit();
    const term = tab.term;

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/servers/${serverId}/console/ws`);
    tab.ws = ws;
    ws.binaryType = "arraybuffer";
    const send = (msg) => ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify(msg));

    ws.onopen = () => {
      const manual = isManual();
      send({ ticket, via: via(), cred: credSel.value || null, cmd: manual ? cmdInp.value : "",
             password: manual ? passInp.value : "", c: term.cols, r: term.rows,
             record: (document.getElementById("console-record") || { checked: true }).checked });
    };
    ws.onmessage = (ev) => {
      if (ev.data instanceof ArrayBuffer) { term.write(new Uint8Array(ev.data)); return; }
      const msg = JSON.parse(ev.data);
      if (msg.t === "o") { tab.state = "open"; setMsg(tab, ""); if (tab === active) term.focus(); }
      else if (msg.t === "e") { setMsg(tab, msg.m, true); }
    };
    ws.onclose = () => {
      if (tab.ws !== ws) return;  // вкладку переподключили или закрыли
      term.options.disableStdin = true;
      tab.state = "closed";
      setMsg(tab, tab.bad ? tab.msg : "Консоль закрыта.", tab.bad);
    };
    term.onData((d) => send({ t: "i", d }));
    term.onResize(({ cols, rows }) => send({ t: "r", c: cols, r: rows }));
  }

  connectBtn.addEventListener("click", () => { if (active) connect(active); });
  addBtn.addEventListener("click", () => {
    const tab = newTab();  // новая вкладка сразу подключается выбранным выше входом
    if (tab) connect(tab);
  });
  window.addEventListener("resize", () => { if (active && active.fit) active.fit.fit(); });
  // Введённый вручную пароль остаётся в поле, пока открыта страница, — он нужен новым вкладкам (никуда не сохраняется)
  newTab();

  // ---------- Передача файла ----------
  const form = document.getElementById("upload-form");
  const bar = document.getElementById("upload-progress");
  const upStatus = document.getElementById("upload-status");

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const file = form.elements.file.files[0];
    if (!file) return;
    if (file.size > Number(form.dataset.max)) {
      say(upStatus, "Файл больше разрешённого размера.", true);
      return;
    }
    const q = new URLSearchParams({ name: file.name, overwrite: form.elements.overwrite.checked ? "1" : "0" });
    if (credSel.value) q.set("cred", credSel.value);
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/servers/${serverId}/files?${q}`);
    xhr.setRequestHeader("X-CSRF-Token", csrf);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("Accept", "application/json");  // ошибки — JSON с текстом, а не страница ошибки
    if (via()) xhr.setRequestHeader("X-SSH-Via", "agent");
    if (isManual()) {
      // В заголовке допустим только ASCII — кириллица в пароле передаётся процентной кодировкой
      xhr.setRequestHeader("X-SSH-Command", encodeURIComponent(cmdInp.value));
      if (passInp.value) xhr.setRequestHeader("X-SSH-Password", encodeURIComponent(passInp.value));
    }
    xhr.upload.onprogress = (ev) => { if (ev.lengthComputable) bar.value = Math.round(ev.loaded * 100 / ev.total); };
    xhr.onload = () => {
      form.querySelector("button").disabled = false;
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch (err) { /* не JSON — ниже общий текст */ }
      if (xhr.status === 200) say(upStatus, `Готово: ${body.path} у ${body.user} (${body.size} Б).`);
      else say(upStatus, body.detail || `Ошибка ${xhr.status}`, true);
    };
    xhr.onerror = () => { form.querySelector("button").disabled = false; say(upStatus, "Связь прервалась.", true); };
    form.querySelector("button").disabled = true;
    bar.hidden = false;
    bar.value = 0;
    say(upStatus, `Передача ${file.name}…`);
    xhr.send(file);
  });
})();
