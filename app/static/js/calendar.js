// Календарь: FullCalendar + форма создания/редактирования события.
(function () {
  const el = document.getElementById("calendar");
  if (!el || !window.FullCalendar) return;

  const form = document.getElementById("event-form");
  const delForm = document.getElementById("event-delete");
  const allDay = document.getElementById("all-day");
  const pad = n => String(n).padStart(2, "0");
  const dateStr = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const timeStr = d => `${pad(d.getHours())}:${pad(d.getMinutes())}`;

  function toggleTime() {
    form.querySelectorAll(".time-field").forEach(f => (f.hidden = allDay.checked));
  }

  // form.elements: у формы есть свои свойства (title, action), которые перекрывают поля с теми же именами
  const f = form ? form.elements : null;

  function resetForm(date) {
    form.reset();
    form.action = "/calendar/events";
    f.remind_minutes.value = form.dataset.defaultReminder;
    document.getElementById("event-form-title").textContent = "Новое событие";
    document.getElementById("event-new").hidden = true;
    delForm.hidden = true;
    if (date) { f.start_date.value = dateStr(date); f.end_date.value = dateStr(date); }
    toggleTime();
  }

  function fillForm(ev) {
    const p = ev.extendedProps;
    const start = new Date(p.seriesStart);
    let end = p.seriesEnd ? new Date(p.seriesEnd) : start;
    if (ev.allDay) end = new Date(end.getTime() - 86400000); // в БД конец не включается
    form.action = `/calendar/events/${p.eventId}`;
    delForm.action = `/calendar/events/${p.eventId}/delete`;
    delForm.hidden = false;
    f.title.value = ev.title;
    f.all_day.checked = ev.allDay;
    f.start_date.value = dateStr(start);
    f.start_time.value = timeStr(start);
    f.end_date.value = dateStr(end);
    f.end_time.value = timeStr(end);
    f.repeat.value = p.repeat;
    f.remind_minutes.value = p.remind ?? "";
    f.description.value = p.description || "";
    document.getElementById("event-form-title").textContent = "Редактирование";
    document.getElementById("event-new").hidden = false;
    toggleTime();
  }

  const editable = el.hasAttribute("data-editable");
  const calendar = new FullCalendar.Calendar(el, {
    locale: "ru",
    firstDay: Number(el.dataset.firstDay),
    initialView: "dayGridMonth",
    height: "auto",
    headerToolbar: { left: "prev,next today", center: "title", right: "dayGridMonth,timeGridWeek,listWeek" },
    events: { url: "/api/calendar/events", failure: () => alert("Не удалось загрузить события") },
    eventClick: info => {
      if (info.event.extendedProps.issue) return; // срок задачи — переход по ссылке
      info.jsEvent.preventDefault();
      if (editable) fillForm(info.event);
      else alert(`${info.event.title}\n\n${info.event.extendedProps.description || ""}`);
    },
    dateClick: info => { if (editable) resetForm(info.date); },
  });
  calendar.render();

  if (form) {
    allDay.addEventListener("change", toggleTime);
    document.getElementById("event-new").addEventListener("click", () => resetForm(new Date()));
    resetForm(new Date());
  }
})();
