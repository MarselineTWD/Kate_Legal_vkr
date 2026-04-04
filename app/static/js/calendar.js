(function () {
  const host = document.getElementById('calendar-grid');
  if (!host || !window.calendarEvents) return;

  const now = new Date();
  const year = now.getFullYear();
  const month = now.getMonth();
  const firstDay = new Date(year, month, 1);
  const lastDay = new Date(year, month + 1, 0);

  const eventsByDay = {};
  window.calendarEvents.forEach(e => {
    if (!eventsByDay[e.date]) eventsByDay[e.date] = [];
    eventsByDay[e.date].push(e);
  });

  const weekOffset = (firstDay.getDay() + 6) % 7;
  for (let i = 0; i < weekOffset; i += 1) {
    const empty = document.createElement('div');
    empty.className = 'day';
    host.appendChild(empty);
  }

  for (let d = 1; d <= lastDay.getDate(); d += 1) {
    const dateObj = new Date(year, month, d);
    const dateISO = dateObj.toISOString().slice(0, 10);

    const day = document.createElement('div');
    day.className = 'day';

    const num = document.createElement('div');
    num.className = 'day__num';
    num.textContent = d;
    day.appendChild(num);

    (eventsByDay[dateISO] || []).slice(0, 4).forEach(ev => {
      const dot = document.createElement('span');
      dot.className = `dot ${ev.is_done ? 'done' : ''}`;
      dot.textContent = ev.title;
      day.appendChild(dot);
    });

    host.appendChild(day);
  }
})();
