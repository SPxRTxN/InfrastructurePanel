// Настройки → Обновление: опрос хода обновления раз в 2 секунды. Текст — только через textContent (лог не HTML).
(function () {
  const box = document.getElementById("update-box");
  if (!box) return;
  const state = document.getElementById("update-state");
  const stepEl = document.getElementById("update-step");
  const logEl = document.getElementById("update-log");
  const cancel = document.getElementById("update-cancel");
  // Обновление идёт, если сервер не показал формы запуска (их несколько: по релизам и ручная)
  let wasActive = box.querySelector("form[action$='/start']") === null;

  async function tick() {
    try {
      const s = await api(box.dataset.statusUrl, { headers: { "X-Background": "1" } });
      const st = s.status;
      if (s.active) {
        state.textContent = `Идёт обновление до ${s.version}, примерно до ${s.until}.`;
        stepEl.textContent = st ? `Шаг: ${s.steps[st.step] || st.step}` : "";
      } else if (wasActive) {
        // Режим выключен: итог готов — перезагрузим страницу, чтобы показать форму и результат
        location.reload();
        return;
      }
      wasActive = s.active;
      cancel.hidden = !s.can_cancel;
      if (s.log) {
        const atEnd = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 20;
        logEl.textContent = s.log;
        if (atEnd) logEl.scrollTop = logEl.scrollHeight;
      }
    } catch (e) {
      // Во время перезапуска панель недоступна — просто ждём следующего опроса
      stepEl.textContent = "Панель перезапускается…";
    }
  }
  setInterval(tick, 2000);
})();
