// SSH-консоль и передача файлов. Консоль: билет (POST с CSRF) → WebSocket → xterm.js, протокол — app/services/console.py.
// Файл: PUT /servers/{id}/files, тело — сам файл (XHR ради полосы прогресса).
(function () {
  const access = document.getElementById("console-access");
  if (!access) return;
  const serverId = access.dataset.server;
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const dataset = access.dataset;
  const hasKey = dataset.hasKey === "1";
  const viaBox = document.getElementById("console-via");  // сегмент есть, только если у сервера агент с туннелем
  const credSel = document.getElementById("console-cred");  // select — когда сохранённых входов несколько
  const credOne = document.getElementById("console-cred-one");  // подпись — когда вход один
  const via = () => {
    const on = viaBox && viaBox.querySelector(".active");
    return on ? on.dataset.via : "";
  };
  if (viaBox) {
    viaBox.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-via]");
      if (!b) return;
      viaBox.querySelectorAll("button").forEach((x) => {
        x.classList.toggle("active", x === b);
        x.setAttribute("aria-checked", x === b ? "true" : "false");
      });
    });
  }
  const savedCred = () => (credSel ? credSel.value : credOne ? credOne.dataset.id : "");
  // Введённые в окне логин и пароль: только в памяти страницы, нужны новым вкладкам и файлам
  let manual = null;
  // Что отправить серверу: введённый вручную вход, сохранённый, либо ничего (поля сервера + его ключ); null — войти нечем
  function auth() {
    if (manual) return { cred: null, cmd: `ssh -p ${dataset.port} ${manual.user}@${dataset.host}`, password: manual.password };
    if (savedCred()) return { cred: savedCred(), cmd: "", password: "" };
    if (hasKey) return { cred: null, cmd: "", password: "" };
    return null;
  }

  // Окно логина и пароля — только когда войти нечем или сервер отказал
  const dlg = document.getElementById("console-login");
  const dlgForm = document.getElementById("console-login-form");
  let afterLogin = null;
  function askLogin(next, note) {
    afterLogin = next;
    document.getElementById("console-login-note").textContent =
      (note ? note + " " : "") + dlg.dataset.host;
    document.getElementById("login-password").value = "";
    dlg.showModal();
    document.getElementById("login-password").focus();
  }
  dlgForm.addEventListener("submit", (e) => {
    e.preventDefault();
    manual = { user: document.getElementById("login-user").value.trim(), password: document.getElementById("login-password").value };
    document.getElementById("login-password").value = "";
    const next = afterLogin;
    afterLogin = null;
    dlg.close();
    if (next) next();
  });
  dlg.addEventListener("close", () => { afterLogin = null; });

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
  const reconnectBtn = document.getElementById("console-reconnect");
  const uploadBtn = document.getElementById("console-upload");
  const fileInp = document.getElementById("console-file");
  const placeholder = document.getElementById("term-placeholder");
  const MAX_TABS = Number(addBtn.dataset.max) || 10;
  const tabs = [];
  let active = null;
  let counter = 0;
  let note = null;  // сообщение о передаче файла: показывается в строке состояния вместо сообщения вкладки

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
    reconnectBtn.hidden = !active || active.state !== "closed";
    if (note) say(status, note.text, note.bad);
    else say(status, active ? active.msg : "", active && active.bad);
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
    note = null;
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
    const a = auth();
    if (!a) {  // нет сохранённого входа и ключа — спрашиваем сразу
      tab.state = "closed";
      setMsg(tab, "Нужен логин и пароль.", true);
      askLogin(() => connect(tab));
      return;
    }
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
      send({ ticket, via: via(), cred: a.cred, cmd: a.cmd, password: a.password, c: term.cols, r: term.rows,
             record: (document.getElementById("console-record") || { checked: true }).checked });
    };
    ws.onmessage = (ev) => {
      if (ev.data instanceof ArrayBuffer) { term.write(new Uint8Array(ev.data)); return; }
      const msg = JSON.parse(ev.data);
      if (msg.t === "o") { tab.state = "open"; setMsg(tab, ""); if (tab === active) term.focus(); }
      else if (msg.t === "e") {
        setMsg(tab, msg.m, true);
        if (msg.code === "auth") tab.authFailed = true;
      }
    };
    ws.onclose = () => {
      if (tab.ws !== ws) return;  // вкладку переподключили или закрыли
      term.options.disableStdin = true;
      tab.state = "closed";
      setMsg(tab, tab.bad ? tab.msg : "Консоль закрыта.", tab.bad);
      if (tab.authFailed) {  // неверный пароль/ключ: забываем введённое и просим вход заново
        tab.authFailed = false;
        manual = null;
        if (tab === active) askLogin(() => connect(tab), "Вход не удался.");
      }
    };
    term.onData((d) => send({ t: "i", d }));
    term.onResize(({ cols, rows }) => send({ t: "r", c: cols, r: rows }));
  }

  reconnectBtn.addEventListener("click", () => { if (active) connect(active); });
  addBtn.addEventListener("click", () => {
    const tab = newTab();  // новая вкладка сразу подключается выбранным входом
    if (tab) connect(tab);
  });
  window.addEventListener("resize", () => { if (active && active.fit) active.fit.fit(); });
  const first = newTab();  // терминал подключается сам при открытии страницы
  if (first) connect(first);

  // ---------- Передача файла ----------
  function setNote(text, bad) {
    note = text ? { text, bad: !!bad } : null;
    sync();
  }

  function upload(file, overwrite) {
    const a = auth();
    if (!a) { askLogin(() => upload(file, overwrite)); return; }
    const q = new URLSearchParams({ name: file.name, overwrite: overwrite ? "1" : "0" });
    if (a.cred) q.set("cred", a.cred);
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/servers/${serverId}/files?${q}`);
    xhr.setRequestHeader("X-CSRF-Token", csrf);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("Accept", "application/json");  // ошибки — JSON с текстом, а не страница ошибки
    if (via()) xhr.setRequestHeader("X-SSH-Via", "agent");
    if (manual) {
      // В заголовке допустим только ASCII — кириллица в пароле передаётся процентной кодировкой
      xhr.setRequestHeader("X-SSH-Command", encodeURIComponent(a.cmd));
      if (a.password) xhr.setRequestHeader("X-SSH-Password", encodeURIComponent(a.password));
    }
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable) setNote(`Передача ${file.name}: ${Math.round(ev.loaded * 100 / ev.total)}%`);
    };
    xhr.onload = () => {
      uploadBtn.disabled = false;
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch (err) { /* не JSON — ниже общий текст */ }
      if (xhr.status === 200) setNote(`Готово: ${body.path} у ${body.user} (${body.size} Б).`);
      else if (xhr.status === 409) {  // файл уже есть
        setNote("");
        if (window.confirm(`Файл «${file.name}» уже есть. Заменить?`)) upload(file, true);
      } else setNote(body.detail || `Ошибка ${xhr.status}`, true);
    };
    xhr.onerror = () => { uploadBtn.disabled = false; setNote("Связь прервалась.", true); };
    uploadBtn.disabled = true;
    setNote(`Передача ${file.name}…`);
    xhr.send(file);
  }

  uploadBtn.addEventListener("click", () => fileInp.click());
  fileInp.addEventListener("change", () => {
    const file = fileInp.files[0];
    fileInp.value = "";
    if (!file) return;
    if (file.size > Number(uploadBtn.dataset.max)) { setNote("Файл больше разрешённого размера.", true); return; }
    upload(file, false);
  });
})();
