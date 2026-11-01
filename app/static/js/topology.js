// Топология сети на Cytoscape.js: узлы с иконками Lucide, цвет по статусу сервера, сохранение позиций.
(async function () {
  const el = document.getElementById("topology");
  if (!el || !window.cytoscape) return;

  const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const TYPE_ICON = { cloud: "cloud", router: "router", switch: "network", ap: "wifi", server: "server", pc: "monitor", iot: "lightbulb", other: "cpu" };
  // Узел — сама иконка без подложки и обводки; статус сервера — цветом линий иконки
  const STATUS_COLOR = { ok: css("--ok"), bad: css("--bad"), unknown: css("--muted"), none: css("--text") };

  // Иконки берём из того же спрайта, что и в интерфейсе, и превращаем в data:-URI для фона узла
  const sprite = new DOMParser().parseFromString(await (await fetch("/static/icons.svg")).text(), "image/svg+xml");
  const iconUri = (name, color) => {
    const symbol = sprite.getElementById(name);
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="${color}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${symbol ? symbol.innerHTML : ""}</svg>`;
    return "data:image/svg+xml;utf8," + encodeURIComponent(svg);
  };

  const data = await api("/api/topology");
  const isPlaced = n => n.x !== null && n.y !== null;
  const placed = data.nodes.filter(isPlaced).length;
  const segById = Object.fromEntries(data.segments.map(s => [s.id, s]));
  const segNode = s => ({ group: "nodes", classes: "seg", data: { id: "s" + s.id, label: s.name, color: s.color, segment: s.id } });
  const edgeLabel = e => (e.kind === "wifi" ? ["Wi-Fi", e.label] : [e.a_port, e.label, e.b_port]).filter(Boolean).join(" · ");
  const edgeData = e => ({ id: "e" + e.id, source: String(e.source), target: String(e.target), kind: e.kind, label: edgeLabel(e), raw: e });
  const elements = [
    // Рамка рисуется только у сегментов, в которых есть устройства
    ...data.segments.filter(s => data.nodes.some(n => n.segment_id === s.id)).map(segNode),
    ...data.nodes.map(n => ({
      classes: "dev",
      data: { id: String(n.id), label: n.ip ? `${n.name}\n${n.ip}` : n.name,
              parent: segById[n.segment_id] ? "s" + n.segment_id : undefined,
              icon: iconUri(TYPE_ICON[n.type] || "cpu", STATUS_COLOR[n.status] || STATUS_COLOR.none), raw: n },
      position: isPlaced(n) ? { x: n.x, y: n.y } : undefined,
    })),
    ...data.edges.map(e => ({ data: edgeData(e) })),
  ];

  const cy = cytoscape({
    container: el,
    elements,
    wheelSensitivity: 0.3,
    autoungrabify: !data.editable,
    // Автораскладка — только для совсем пустой схемы; дальше расставленные узлы не двигаются
    layout: placed === 0
      ? { name: "breadthfirst", directed: true, spacingFactor: 1.3, roots: data.nodes.filter(n => n.type === "cloud").map(n => "#" + n.id) }
      : { name: "preset" },
    style: [
      { selector: "node", style: {
        "background-opacity": 0, "background-image": "data(icon)", "background-fit": "contain",
        "background-clip": "none", width: 36, height: 36, "border-width": 0, shape: "round-rectangle",
        label: "data(label)", color: css("--text"), "font-size": 11, "text-wrap": "wrap",
        "text-valign": "bottom", "text-margin-y": 6, "text-outline-width": 3, "text-outline-color": css("--bg"),
      } },
      // Рамка сегмента (составной узел): полупрозрачная заливка цветом сегмента, подпись сверху
      { selector: "node.seg", style: {
        "background-image": "none", "background-color": "data(color)", "background-opacity": 0.08,
        "border-width": 1, "border-style": "dashed", "border-color": "data(color)", shape: "round-rectangle",
        padding: 28, "min-width": 90, "min-height": 60, "text-valign": "top", "text-halign": "center",
        "text-margin-y": -4, "font-size": 12, color: "data(color)", "text-outline-width": 0,
      } },
      { selector: ".hidden", style: { display: "none" } },
      { selector: "node.ghost", style: { width: 1, height: 1, "background-opacity": 0, "background-image": "none", label: "", events: "no" } },
      { selector: "edge.ghost", style: { "line-style": "dashed", "line-color": css("--accent"), width: 2, label: "", events: "no" } },
      { selector: "edge:selected", style: { "line-color": css("--accent"), width: 2.5 } },
      // Wi-Fi: пунктир цветом акцента
      { selector: "edge[kind = 'wifi']", style: { "line-style": "dashed", "line-dash-pattern": [6, 4], "line-color": css("--accent") } },
      { selector: "node:active", style: { "overlay-opacity": 0 } },
      { selector: "node:selected", style: { "underlay-color": css("--accent"), "underlay-opacity": 0.18, "underlay-padding": 10, "underlay-shape": "round-rectangle" } },
      { selector: "edge", style: {
        width: 1.5, "line-color": css("--border-strong"), "curve-style": "bezier",
        label: "data(label)", "font-size": 9, color: css("--muted"), "text-rotation": "autorotate",
        "text-background-color": css("--card"), "text-background-opacity": 1, "text-background-padding": 2,
      } },
    ],
  });

  // Новые узлы (без координат) на сложенной схеме: рядом с уже расставленным соседом, на свободное место
  const devs = () => cy.nodes(".dev");
  const fresh = placed === 0 ? devs() : devs().filter(n => !isPlaced(n.data("raw")));
  if (placed > 0 && fresh.length) {
    const taken = () => devs().filter(n => !fresh.has(n) || n.data("placed")).map(n => n.position());
    const free = p => taken().every(q => Math.hypot(p.x - q.x, p.y - q.y) > 70);
    fresh.forEach((node, i) => {
      const anchor = node.neighborhood("node").filter(n => !fresh.has(n) || n.data("placed"))[0];
      let pos;
      if (anchor) {
        const a = anchor.position();
        for (let r = 110; !pos && r < 600; r += 60)
          for (let k = 0; k < 12 && !pos; k++) {
            const angle = Math.PI / 2 + (k % 2 ? 1 : -1) * Math.ceil(k / 2) * Math.PI / 6; // сначала вниз
            const p = { x: a.x + r * Math.cos(angle), y: a.y + r * Math.sin(angle) };
            if (free(p)) pos = p;
          }
      }
      if (!pos) {  // без соседей — в ряд под схемой
        const bb = devs().filter(n => !fresh.has(n)).boundingBox();
        pos = { x: bb.x1 + i * 110, y: bb.y2 + 120 };
      }
      node.position(pos);
      node.data("placed", true);
    });
    cy.fit(undefined, 30);
  }

  // Сохранение позиций одним запросом; после перетаскивания — с небольшой задержкой
  let timer;
  const save = nodes => api("/api/topology/positions", {
    method: "POST",
    body: { positions: nodes.map(n => ({ id: Number(n.id()), x: n.position("x"), y: n.position("y") })) },
  }).catch(e => alert("Не удалось сохранить позиции: " + e.message));
  if (data.editable) {
    cy.on("dragfree", "node.dev", () => { clearTimeout(timer); timer = setTimeout(() => save(devs()), 400); });
    if (fresh.length) save(fresh); // зафиксировать только новые узлы — остальные не трогаем
  }

  // Панель информации об устройстве
  const info = document.getElementById("topo-info");
  // Wi-Fi-связи устройства: клиент — не точка доступа; если оба/ни один AP, клиент — сторона A
  const nodeRaw = id => data.nodes.find(x => String(x.id) === String(id));
  const wifiLines = n => {
    const mine = [], clients = [];
    data.edges.filter(e => e.kind === "wifi" && (e.source === n.id || e.target === n.id)).forEach(e => {
      const other = nodeRaw(e.source === n.id ? e.target : e.source);
      if (!other) return;
      const meIsHost = n.type === "ap" && other.type !== "ap";
      (meIsHost ? clients : mine).push(other.name);
    });
    return (mine.length ? `<dt>Wi-Fi</dt><dd>подключено по Wi-Fi к ${mine.map(escapeHtml).join(", ")}</dd>` : "")
      + (clients.length ? `<dt>Wi-Fi клиенты</dt><dd>${clients.map(escapeHtml).join(", ")}</dd>` : "");
  };
  const portsLine = n => n.ports.length
    ? `<dt>Порты</dt><dd>${n.ports.map(p => `<span class="${p.used ? "muted" : ""}">${escapeHtml(p.name)}</span>`).join(", ")}</dd>` : "";
  const segmentSelect = n => !data.editable ? "" : `<dt>Сегмент</dt><dd><select data-set-segment="${n.id}" aria-label="Сегмент">
      <option value="">— нет —</option>${data.segments.map(s => `<option value="${s.id}" ${s.id === n.segment_id ? "selected" : ""}>${escapeHtml(s.name)}</option>`).join("")}</select></dd>`;
  cy.on("tap", "node.dev", evt => {
    const n = evt.target.data("raw");
    const statusText = { ok: "онлайн", bad: "офлайн", unknown: "нет данных" }[n.status];
    const seg = segById[n.segment_id];
    info.innerHTML = `<h3>${escapeHtml(n.name)}</h3>
      <dl class="meta-dl"><dt>Тип</dt><dd>${escapeHtml(n.type)}</dd><dt>IP</dt><dd>${escapeHtml(n.ip || "—")}</dd>
      ${seg && !data.editable ? `<dt>Сегмент</dt><dd>${escapeHtml(seg.name)}</dd>` : ""}${segmentSelect(n)}
      ${wifiLines(n)}${portsLine(n)}
      ${n.server ? `<dt>Сервер</dt><dd><a href="/servers/${n.server.id}">${escapeHtml(n.server.name)}</a> <span class="status ${n.status}">${statusText}</span></dd>` : ""}
      ${n.description ? `<dt>Описание</dt><dd>${escapeHtml(n.description)}</dd>` : ""}</dl>
      ${n.mac ? `<dl class="meta-dl"><dt>MAC</dt><dd>${escapeHtml(n.mac)}</dd></dl>` : ""}
      <div class="row">${n.actions.map(actionButton(n)).join(" ")}
      ${data.editable ? `<a class="btn btn-sm" href="#device-${n.id}" data-open="device-${n.id}">Редактировать</a>` : ""}</div>
      <p class="muted small" id="topo-msg" role="status"></p>`;
  });

  // Действия по типу устройства: ссылки открываются как есть, остальные — POST с CSRF (Wake-on-LAN и др.)
  const actionButton = n => a => {
    if (a.kind === "link")
      return `<a class="btn btn-sm" href="${escapeHtml(a.href)}"${a.external ? ' target="_blank" rel="noopener noreferrer"' : ""}>${escapeHtml(a.label)}</a>`;
    const pick = a.agents && a.agents.length > 1
      ? `<select data-agent="${a.id}" aria-label="Агент">${a.agents.map(x => `<option value="${x.id}">${escapeHtml(x.name)}</option>`).join("")}</select> ` : "";
    return `${pick}<button class="btn btn-sm" type="button" data-action="${a.id}" data-device="${n.id}">${escapeHtml(a.label)}</button>`;
  };
  info.addEventListener("click", async e => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    const msg = document.getElementById("topo-msg");
    const body = new FormData();
    const sel = info.querySelector(`select[data-agent="${btn.dataset.action}"]`);
    if (sel) body.set("node_id", sel.value);
    btn.disabled = true;
    msg.textContent = "Отправляем…";
    try {
      msg.textContent = (await api(`/topology/devices/${btn.dataset.device}/actions/${btn.dataset.action}`, { method: "POST", body })).message;
    } catch (err) {
      msg.textContent = "Ошибка: " + err.message;
    }
    btn.disabled = false;
  });
  info.addEventListener("click", e => {
    const link = e.target.closest("[data-open]");
    if (!link) return;
    const d = document.getElementById(link.dataset.open);
    d.open = true;
    d.closest("details.collapsible").open = true;
  });

  document.getElementById("topo-fit")?.addEventListener("click", () => cy.fit(undefined, 30));

  // --- Фильтр карты по сегменту ---
  const filter = document.getElementById("topo-seg-filter");
  const applyFilter = () => {
    const v = filter ? filter.value : "";
    devs().forEach(n => {
      const sid = n.data("raw").segment_id;
      n.toggleClass("hidden", v !== "" && !(v === "none" ? !sid : String(sid) === v));
    });
    cy.nodes(".seg").forEach(s => s.toggleClass("hidden", v !== "" && String(s.data("segment")) !== v));
  };
  filter?.addEventListener("change", applyFilter);

  // --- Сегменты: перенос устройства в рамку / из рамки ---
  const assignSegment = async (node, segId) => {
    const raw = node.data("raw");
    try {
      await api(`/api/topology/devices/${raw.id}/segment`, { method: "POST", body: { segment_id: segId } });
    } catch (e) {
      alert("Не удалось сменить сегмент: " + e.message);
      return;
    }
    const old = raw.segment_id;
    raw.segment_id = segId;
    if (segId && !cy.getElementById("s" + segId).length) cy.add(segNode(segById[segId]));
    node.move({ parent: segId ? "s" + segId : null });
    const oldBox = old ? cy.getElementById("s" + old) : null;
    if (oldBox && oldBox.length && oldBox.children().length === 0) cy.remove(oldBox);
    applyFilter();
  };
  if (data.editable) {
    // Рамка сама растёт за потомками, поэтому границы запоминаем в момент захвата узла
    let boxes = [];
    cy.on("grab", "node.dev", () => {
      boxes = cy.nodes(".seg").map(s => ({ id: s.data("segment"), bb: s.boundingBox({ includeLabels: false, includeOverlays: false }) }));
    });
    cy.on("dragfree", "node.dev", evt => {
      const node = evt.target, p = node.position();
      const hit = boxes.find(b => p.x >= b.bb.x1 && p.x <= b.bb.x2 && p.y >= b.bb.y1 && p.y <= b.bb.y2);
      const target = hit ? hit.id : null;
      if ((node.data("raw").segment_id || null) !== target) assignSegment(node, target);
    });
    info.addEventListener("change", e => {
      const sel = e.target.closest("[data-set-segment]");
      if (sel) assignSegment(cy.getElementById(sel.dataset.setSegment), sel.value ? Number(sel.value) : null);
    });
  }

  // --- Связи на поле: в режиме «Связать» тянем от устройства к устройству ---
  const dlg = document.getElementById("topo-link-dialog");
  const linkBtn = document.getElementById("topo-link-mode");
  if (data.editable && dlg && linkBtn) {
    const form = document.getElementById("topo-link-form");
    const labA = document.getElementById("tl-a"), labB = document.getElementById("tl-b");
    const msg = document.getElementById("tl-msg");
    let linkMode = false, dragFrom = null, pair = null, ctlA = null, ctlB = null;

    // Свободные порты — в списке, занятые серые и недоступные; у устройства без портов — текстовое поле
    const buildPort = (lab, title, raw) => {
      lab.textContent = title;
      let ctl;
      if (raw.ports.length) {
        ctl = document.createElement("select");
        ctl.add(new Option("— не указан —", ""));
        raw.ports.forEach(p => {
          const o = new Option(p.used ? `${p.name} (занято)` : p.name, p.name);
          o.disabled = p.used;
          ctl.add(o);
        });
        const free = raw.ports.find(p => !p.used);
        if (free) ctl.value = free.name;
      } else {
        ctl = document.createElement("input");
        ctl.maxLength = 32;
        ctl.placeholder = "порт (текст)";
      }
      lab.append(ctl);
      return ctl;
    };
    const syncKind = () => { labA.hidden = labB.hidden = form.elements.kind.value === "wifi"; };
    form.elements.kind.addEventListener("change", syncKind);
    const openLinkDialog = (a, b) => {
      pair = [a.data("raw"), b.data("raw")];
      document.getElementById("tl-title").textContent = `Связь: ${pair[0].name} — ${pair[1].name}`;
      ctlA = buildPort(labA, "Порт " + pair[0].name, pair[0]);
      ctlB = buildPort(labB, "Порт " + pair[1].name, pair[1]);
      form.elements.label.value = "";
      form.elements.kind.value = "wired";
      msg.textContent = "";
      syncKind();
      dlg.showModal();
    };
    document.getElementById("tl-cancel").addEventListener("click", () => dlg.close());
    form.addEventListener("submit", async ev => {
      ev.preventDefault();
      const wifi = form.elements.kind.value === "wifi";
      try {
        const e = await api("/api/topology/links", { method: "POST", body: {
          a_id: pair[0].id, b_id: pair[1].id, a_port: wifi ? "" : ctlA.value, b_port: wifi ? "" : ctlB.value,
          kind: form.elements.kind.value, label: form.elements.label.value,
        } });
        data.edges.push(e);
        cy.add({ data: edgeData(e) });
        if (!wifi) [[pair[0], e.a_port], [pair[1], e.b_port]].forEach(([n, port]) => {
          const p = n.ports.find(x => x.name === port);
          if (p) p.used = true;
        });
        dlg.close();
      } catch (err) {
        msg.textContent = "Ошибка: " + err.message;
      }
    });

    linkBtn.addEventListener("click", () => {
      linkMode = !linkMode;
      linkBtn.setAttribute("aria-pressed", String(linkMode));
      linkBtn.classList.toggle("btn-primary", linkMode);
      el.classList.toggle("link-mode", linkMode);
      cy.autoungrabify(linkMode); // в режиме связи узлы не двигаются
    });
    cy.on("tapstart", "node.dev", evt => {
      if (!linkMode) return;
      dragFrom = evt.target;
      cy.userPanningEnabled(false);
      cy.add([
        { group: "nodes", classes: "ghost", data: { id: "ghost" }, position: { ...evt.position } },
        { group: "edges", classes: "ghost", data: { id: "ghost-e", source: dragFrom.id(), target: "ghost" } },
      ]);
    });
    cy.on("tapdrag", evt => { if (dragFrom) cy.getElementById("ghost").position({ ...evt.position }); });
    cy.on("tapend", evt => {
      if (!dragFrom) return;
      const from = dragFrom;
      dragFrom = null;
      cy.remove(cy.elements(".ghost"));
      cy.userPanningEnabled(true);
      const to = evt.target;
      if (to !== cy && to.isNode() && to.hasClass("dev") && to !== from) openLinkDialog(from, to);
    });

    // Клик по связи — данные и кнопка «Удалить»
    cy.on("tap", "edge", evt => {
      const e = evt.target.data("raw");
      if (!e) return;
      const a = nodeRaw(e.source), b = nodeRaw(e.target);
      info.innerHTML = `<h3>Связь</h3><dl class="meta-dl"><dt>Тип</dt><dd>${e.kind === "wifi" ? "Wi-Fi" : "проводная"}</dd>
        <dt>A</dt><dd>${escapeHtml(a.name)}${e.a_port ? " (" + escapeHtml(e.a_port) + ")" : ""}</dd>
        <dt>B</dt><dd>${escapeHtml(b.name)}${e.b_port ? " (" + escapeHtml(e.b_port) + ")" : ""}</dd>
        ${e.label ? `<dt>Подпись</dt><dd>${escapeHtml(e.label)}</dd>` : ""}</dl>
        <div class="row"><button class="btn btn-sm btn-danger" type="button" data-link-delete="${e.id}">Удалить связь</button></div>
        <p class="muted small" id="topo-msg" role="status"></p>`;
    });
    info.addEventListener("click", async ev => {
      const btn = ev.target.closest("[data-link-delete]");
      if (!btn || !confirm("Удалить связь?")) return;
      const id = Number(btn.dataset.linkDelete);
      try {
        await api(`/api/topology/links/${id}/delete`, { method: "POST" });
      } catch (err) {
        document.getElementById("topo-msg").textContent = "Ошибка: " + err.message;
        return;
      }
      const i = data.edges.findIndex(x => x.id === id);
      const [gone] = data.edges.splice(i, 1);
      cy.remove(cy.getElementById("e" + id));
      if (gone.kind === "wired") [[gone.source, gone.a_port], [gone.target, gone.b_port]].forEach(([nid, port]) => {
        const p = nodeRaw(nid).ports.find(x => x.name === port);
        if (p) p.used = false;
      });
      info.innerHTML = "<h3>Устройство</h3><p class=\"muted\">Связь удалена</p>";
    });
  }

  // --- Сегменты: список и форма в боковой панели ---
  document.querySelectorAll(".seg-dot").forEach(d => { d.style.background = d.dataset.color; });
  const segForm = document.getElementById("seg-form");
  if (segForm) {
    const segMsg = document.getElementById("seg-msg");
    const fill = li => {
      const f = segForm.elements;
      f.id.value = li ? li.dataset.seg : "";
      f.name.value = li ? li.dataset.name : "";
      f.color.value = li ? li.dataset.color : "#3b82f6";
      f.subnet.value = li ? li.dataset.subnet : "";
      f.description.value = li ? li.dataset.description : "";
    };
    document.getElementById("seg-reset").addEventListener("click", () => fill(null));
    document.getElementById("seg-list").addEventListener("click", async e => {
      const edit = e.target.closest("[data-seg-edit]"), del = e.target.closest("[data-seg-delete]");
      if (edit) fill(edit.closest("li"));
      if (del && confirm("Удалить сегмент? Устройства останутся на карте без сегмента.")) {
        try {
          await api(`/api/topology/segments/${del.dataset.segDelete}/delete`, { method: "POST" });
          location.reload();
        } catch (err) { segMsg.textContent = "Ошибка: " + err.message; }
      }
    });
    segForm.addEventListener("submit", async e => {
      e.preventDefault();
      const f = segForm.elements;
      const body = { name: f.name.value.trim(), color: f.color.value, subnet: f.subnet.value, description: f.description.value };
      try {
        await api(f.id.value ? `/api/topology/segments/${f.id.value}` : "/api/topology/segments", { method: "POST", body });
        location.reload(); // позиции уже сохранены, схема не сдвинется
      } catch (err) { segMsg.textContent = "Ошибка: " + err.message; }
    });
  }

  // Выбор шаблона портов заполняет поле «Порты» в той же форме
  document.addEventListener("change", e => {
    const sel = e.target.closest(".js-ports-template");
    if (!sel || !sel.value) return;
    sel.closest("form").elements.ports.value = sel.selectedOptions[0].dataset.ports.split(",").join("\n");
  });
})();
