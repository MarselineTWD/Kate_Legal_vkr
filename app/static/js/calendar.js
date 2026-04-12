(function () {
  const host = document.getElementById('calendar-grid');
  if (!host || !window.calendarEvents) return;

  const now = new Date();
  const year = now.getFullYear();
  const month = now.getMonth();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const todayISO = today.toISOString().slice(0, 10);
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
    if (dateISO === todayISO) {
      day.classList.add('today');
    }
    let dayLevel = '';

    const num = document.createElement('div');
    num.className = 'day__num';
    num.textContent = d;
    if (dateISO === todayISO) {
      const todayLabel = document.createElement('span');
      todayLabel.className = 'day__today-label';
      todayLabel.textContent = 'Сегодня';
      num.appendChild(todayLabel);
    }
    day.appendChild(num);

    (eventsByDay[dateISO] || []).slice(0, 4).forEach(ev => {
      const dot = document.createElement('span');
      let level = '';
      if (!ev.is_done) {
        const eventDate = new Date(`${ev.date}T00:00:00`);
        const diffDays = Math.ceil((eventDate - today) / (1000 * 60 * 60 * 24));
        if (diffDays >= 1 && diffDays <= 3) {
          level = 'urgent';
        } else if (diffDays >= 4 && diffDays <= 7) {
          level = 'warning';
        }
      }
      if (level === 'urgent' || (level === 'warning' && dayLevel !== 'urgent')) {
        dayLevel = level;
      }
      dot.className = `dot ${ev.is_done ? 'done' : ''} ${level}`.trim();
      dot.textContent = ev.title;
      day.appendChild(dot);
    });

    if (dayLevel) {
      day.classList.add(dayLevel);
    }

    host.appendChild(day);
  }
})();
