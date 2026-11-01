// Канбан: перетаскивание карточек между колонками (SortableJS) с сохранением на сервере.
(function () {
  const board = document.querySelector(".kanban[data-editable]");
  if (!board || !window.Sortable) return;

  board.querySelectorAll(".kanban-col-body").forEach(col => {
    Sortable.create(col, {
      group: "issues",
      animation: 150,
      ghostClass: "kanban-ghost",
      onEnd: async evt => {
        const target = evt.to;
        const order = [...target.querySelectorAll(".kanban-card")].map(el => Number(el.dataset.id));
        try {
          await api(`/api/jira/issues/${evt.item.dataset.id}/move`, {
            method: "POST",
            body: { column_id: Number(target.dataset.columnId), order },
          });
          // обновляем счётчики колонок
          board.querySelectorAll(".kanban-col").forEach(c => {
            const pill = c.querySelector(".kanban-col-head .pill");
            if (pill) pill.textContent = c.querySelectorAll(".kanban-card").length;
          });
        } catch (e) {
          alert("Не удалось переместить: " + e.message);
          location.reload();
        }
      },
    });
  });
})();
