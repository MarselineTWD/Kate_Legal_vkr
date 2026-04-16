(function () {
  const host = document.getElementById('calendar-grid');
  const monthLabel = document.getElementById('calendar-month-label');
  const prevButton = document.getElementById('calendar-prev-month');
  const nextButton = document.getElementById('calendar-next-month');

  if (!host || !Array.isArray(window.calendarEvents)) return;

  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const formatLocalISO = (dateObj) => {
    const year = dateObj.getFullYear();
    const month = String(dateObj.getMonth() + 1).padStart(2, '0');
    const day = String(dateObj.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
  };
  const todayISO = formatLocalISO(today);
  const monthFormatter = new Intl.DateTimeFormat('ru-RU', {
    month: 'long',
    year: 'numeric',
  });
  let visibleMonth = new Date(now.getFullYear(), now.getMonth(), 1);

  const eventsByDay = {};
  window.calendarEvents.forEach((eventItem) => {
    if (!eventsByDay[eventItem.date]) eventsByDay[eventItem.date] = [];
    eventsByDay[eventItem.date].push(eventItem);
  });

  const capitalize = (value) => value.charAt(0).toUpperCase() + value.slice(1);

  const appendEmptyDay = () => {
    const empty = document.createElement('div');
    empty.className = 'day day--empty';
    empty.setAttribute('aria-hidden', 'true');
    host.appendChild(empty);
  };

  const render = () => {
    host.innerHTML = '';

    const year = visibleMonth.getFullYear();
    const month = visibleMonth.getMonth();
    const firstDay = new Date(year, month, 1);
    const lastDay = new Date(year, month + 1, 0);

    if (monthLabel) {
      monthLabel.textContent = capitalize(monthFormatter.format(firstDay));
    }

    const weekOffset = (firstDay.getDay() + 6) % 7;
    for (let index = 0; index < weekOffset; index += 1) {
      appendEmptyDay();
    }

    for (let dayIndex = 1; dayIndex <= lastDay.getDate(); dayIndex += 1) {
      const dateObj = new Date(year, month, dayIndex);
      const dateISO = formatLocalISO(dateObj);

      const day = document.createElement('div');
      day.className = 'day';
      if (dateISO === todayISO) {
        day.classList.add('today');
      }

      let dayLevel = '';

      const num = document.createElement('div');
      num.className = 'day__num';
      num.textContent = String(dayIndex);

      if (dateISO === todayISO) {
        const todayLabel = document.createElement('span');
        todayLabel.className = 'day__today-label';
        todayLabel.textContent = 'Сегодня';
        num.appendChild(todayLabel);
      }

      day.appendChild(num);

      (eventsByDay[dateISO] || []).slice(0, 5).forEach((eventItem) => {
        const dot = document.createElement('span');
        let level = '';

        if (eventItem.kind === 'TASK' && !eventItem.is_done) {
          const eventDate = new Date(`${eventItem.date}T00:00:00`);
          const diffDays = Math.ceil((eventDate - today) / (1000 * 60 * 60 * 24));
          if (diffDays < 0 || (diffDays >= 1 && diffDays <= 3)) {
            level = 'urgent';
          } else if (diffDays >= 4 && diffDays <= 7) {
            level = 'warning';
          }
        }

        if (level === 'urgent' || (level === 'warning' && dayLevel !== 'urgent')) {
          dayLevel = level;
        }

        const isCustomEvent = eventItem.kind === 'EVENT';
        const typeClass = isCustomEvent ? `dot--${String(eventItem.event_type || 'CUSTOM').toLowerCase()}` : '';
        dot.className = `dot ${eventItem.is_done ? 'done' : ''} ${level} ${typeClass}`.trim();
        dot.textContent = eventItem.case_number ? `${eventItem.case_number}: ${eventItem.title}` : eventItem.title;
        dot.title = eventItem.case_number
          ? `${eventItem.title} • ${eventItem.status} • ${eventItem.case_number}`
          : `${eventItem.title} • ${eventItem.status}`;
        day.appendChild(dot);
      });

      if (dayLevel) {
        day.classList.add(dayLevel);
      }

      host.appendChild(day);
    }

    const totalCells = weekOffset + lastDay.getDate();
    const tailCells = (7 - (totalCells % 7)) % 7;
    for (let index = 0; index < tailCells; index += 1) {
      appendEmptyDay();
    }
  };

  prevButton?.addEventListener('click', () => {
    visibleMonth = new Date(visibleMonth.getFullYear(), visibleMonth.getMonth() - 1, 1);
    render();
  });

  nextButton?.addEventListener('click', () => {
    visibleMonth = new Date(visibleMonth.getFullYear(), visibleMonth.getMonth() + 1, 1);
    render();
  });

  render();
})();
