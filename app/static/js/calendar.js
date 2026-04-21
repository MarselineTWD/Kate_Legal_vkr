(function () {
  const monthHost = document.getElementById('calendar-grid');
  const weekHost = document.getElementById('calendar-week-grid');
  const listHost = document.getElementById('calendar-list-view');
  const labelNode = document.getElementById('calendar-range-label');
  const prevButton = document.getElementById('calendar-prev-range');
  const nextButton = document.getElementById('calendar-next-range');
  const weekdays = document.getElementById('calendar-weekdays');
  const modeButtons = Array.from(document.querySelectorAll('[data-calendar-mode-button]'));

  if (!monthHost || !weekHost || !listHost || !labelNode || !prevButton || !nextButton) return;
  if (!Array.isArray(window.calendarEvents)) return;

  const monthFormatter = new Intl.DateTimeFormat('ru-RU', { month: 'long', year: 'numeric' });
  const dayFormatter = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short' });
  const fullDateFormatter = new Intl.DateTimeFormat('ru-RU', {
    day: 'numeric',
    month: 'long',
    year: 'numeric',
  });
  const weekdayFormatter = new Intl.DateTimeFormat('ru-RU', { weekday: 'short' });
  const rangeMonthFormatter = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short' });
  const today = parseDate(window.calendarToday) || startOfDay(new Date());
  let currentMode = 'month';
  let cursor = startOfDay(today);

  const events = window.calendarEvents
    .map((item) => ({
      ...item,
      parsedDate: parseDate(item.date),
    }))
    .filter((item) => item.parsedDate instanceof Date && !Number.isNaN(item.parsedDate.getTime()))
    .sort((a, b) => a.parsedDate - b.parsedDate || String(a.title || '').localeCompare(String(b.title || ''), 'ru'));

  const eventsByDay = events.reduce((acc, item) => {
    const key = toISODate(item.parsedDate);
    if (!acc[key]) acc[key] = [];
    acc[key].push(item);
    return acc;
  }, {});

  function parseDate(value) {
    if (!value) return null;
    const [year, month, day] = String(value).split('-').map((part) => Number(part));
    if (!year || !month || !day) return null;
    return new Date(year, month - 1, day);
  }

  function startOfDay(date) {
    return new Date(date.getFullYear(), date.getMonth(), date.getDate());
  }

  function toISODate(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
  }

  function startOfMonth(date) {
    return new Date(date.getFullYear(), date.getMonth(), 1);
  }

  function endOfMonth(date) {
    return new Date(date.getFullYear(), date.getMonth() + 1, 0);
  }

  function startOfWeek(date) {
    const base = startOfDay(date);
    const day = (base.getDay() + 6) % 7;
    base.setDate(base.getDate() - day);
    return base;
  }

  function addDays(date, days) {
    const next = new Date(date);
    next.setDate(next.getDate() + days);
    return next;
  }

  function capitalize(value) {
    return value ? value.charAt(0).toUpperCase() + value.slice(1) : '';
  }

  function getUrgency(eventItem, date) {
    if (eventItem.kind === 'TASK' && !eventItem.is_done) {
      const diffDays = Math.ceil((startOfDay(date) - today) / (1000 * 60 * 60 * 24));
      if (diffDays < 0 || diffDays <= 3) return 'urgent';
      if (diffDays <= 7) return 'warning';
    }
    if ((eventItem.event_type || '').toUpperCase() === 'COURT') return 'urgent';
    return '';
  }

  function eventTypeClass(eventItem) {
    return `dot--${String(eventItem.event_type || 'CUSTOM').toLowerCase()}`;
  }

  function listBadgeClass(eventItem) {
    const urgency = getUrgency(eventItem, eventItem.parsedDate);
    if (urgency === 'urgent') return 'status-badge--danger';
    if (urgency === 'warning') return 'status-badge--warning';
    if ((eventItem.event_type || '').toUpperCase() === 'CLIENT') return 'status-badge--brand-soft';
    if ((eventItem.event_type || '').toUpperCase() === 'MEETING') return 'status-badge--info';
    return 'status-badge--surface';
  }

  function createEventDot(eventItem) {
    const dot = document.createElement('span');
    const urgency = getUrgency(eventItem, eventItem.parsedDate);
    dot.className = ['dot', eventItem.is_done ? 'done' : '', urgency, eventTypeClass(eventItem)]
      .filter(Boolean)
      .join(' ');
    dot.textContent = eventItem.case_number
      ? `${eventItem.case_number}: ${eventItem.title}`
      : eventItem.title;
    dot.title = eventItem.case_number
      ? `${eventItem.title} • ${eventItem.status} • ${eventItem.case_number}`
      : `${eventItem.title} • ${eventItem.status}`;
    return dot;
  }

  function createDayCard(date, isCompact) {
    const iso = toISODate(date);
    const card = document.createElement('div');
    card.className = 'day';
    if (iso === toISODate(today)) card.classList.add('today');

    const items = eventsByDay[iso] || [];
    const dayUrgency = items.some((item) => getUrgency(item, date) === 'urgent')
      ? 'urgent'
      : items.some((item) => getUrgency(item, date) === 'warning')
        ? 'warning'
        : '';

    if (dayUrgency) card.classList.add(dayUrgency);

    const header = document.createElement('div');
    header.className = 'day__num';
    header.textContent = String(date.getDate());

    const weekday = document.createElement('span');
    weekday.className = 'day__today-label';
    weekday.textContent = capitalize(weekdayFormatter.format(date)).replace('.', '');
    if (iso === toISODate(today)) {
      weekday.textContent = 'Сегодня';
    }
    header.appendChild(weekday);
    card.appendChild(header);

    const maxItems = isCompact ? 6 : 5;
    items.slice(0, maxItems).forEach((item) => {
      card.appendChild(createEventDot(item));
    });

    if (!items.length) {
      const empty = document.createElement('span');
      empty.className = 'ops-empty-note';
      empty.textContent = isCompact ? 'Нет событий' : 'Спокойный день';
      card.appendChild(empty);
    }

    return card;
  }

  function renderMonth() {
    monthHost.innerHTML = '';
    const monthStart = startOfMonth(cursor);
    const monthEnd = endOfMonth(cursor);
    const firstWeekday = (monthStart.getDay() + 6) % 7;
    const totalDays = monthEnd.getDate();

    labelNode.textContent = capitalize(monthFormatter.format(monthStart));

    for (let index = 0; index < firstWeekday; index += 1) {
      const empty = document.createElement('div');
      empty.className = 'day day--empty';
      empty.setAttribute('aria-hidden', 'true');
      monthHost.appendChild(empty);
    }

    for (let day = 1; day <= totalDays; day += 1) {
      monthHost.appendChild(createDayCard(new Date(monthStart.getFullYear(), monthStart.getMonth(), day), false));
    }

    const tail = (7 - ((firstWeekday + totalDays) % 7)) % 7;
    for (let index = 0; index < tail; index += 1) {
      const empty = document.createElement('div');
      empty.className = 'day day--empty';
      empty.setAttribute('aria-hidden', 'true');
      monthHost.appendChild(empty);
    }
  }

  function renderWeek() {
    weekHost.innerHTML = '';
    const weekStart = startOfWeek(cursor);
    const weekEnd = addDays(weekStart, 6);
    labelNode.textContent = `${capitalize(rangeMonthFormatter.format(weekStart))} — ${capitalize(rangeMonthFormatter.format(weekEnd))}`;

    for (let offset = 0; offset < 7; offset += 1) {
      weekHost.appendChild(createDayCard(addDays(weekStart, offset), true));
    }
  }

  function renderList() {
    listHost.innerHTML = '';
    const listStart = startOfDay(cursor);
    const listEnd = addDays(listStart, 13);
    labelNode.textContent = `${capitalize(rangeMonthFormatter.format(listStart))} — ${capitalize(rangeMonthFormatter.format(listEnd))}`;

    const visible = events.filter((item) => item.parsedDate >= listStart && item.parsedDate <= listEnd);

    if (!visible.length) {
      const empty = document.createElement('div');
      empty.className = 'portal-empty portal-empty--compact';
      empty.innerHTML = '<h3>В выбранном диапазоне нет событий</h3><p>Переключите диапазон или добавьте новую точку контроля.</p>';
      listHost.appendChild(empty);
      return;
    }

    visible.forEach((item) => {
      const card = document.createElement('article');
      card.className = 'calendar-list-card';

      const dateCol = document.createElement('div');
      dateCol.className = 'calendar-list-card__date';
      dateCol.innerHTML = `<strong>${item.parsedDate.getDate()}</strong><span>${capitalize(dayFormatter.format(item.parsedDate))}</span><small>${item.parsedDate.getFullYear()}</small>`;

      const content = document.createElement('div');
      content.className = 'calendar-list-card__content';

      const meta = document.createElement('div');
      meta.className = 'ops-inline-meta';

      const typeBadge = document.createElement('span');
      typeBadge.className = `status-badge ${listBadgeClass(item)}`;
      typeBadge.textContent = item.status;
      meta.appendChild(typeBadge);

      if (item.case_number) {
        const caseNode = document.createElement('span');
        caseNode.textContent = item.case_number;
        meta.appendChild(caseNode);
      }

      const title = document.createElement('h3');
      title.className = 'ops-primary';
      title.textContent = item.title;

      content.appendChild(meta);
      content.appendChild(title);
      card.appendChild(dateCol);
      card.appendChild(content);
      listHost.appendChild(card);
    });
  }

  function toggleMode(mode) {
    currentMode = mode;
    modeButtons.forEach((button) => {
      const active = button.dataset.calendarModeButton === mode;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', String(active));
    });

    monthHost.hidden = mode !== 'month';
    weekHost.hidden = mode !== 'week';
    listHost.hidden = mode !== 'list';
    if (weekdays) {
      weekdays.hidden = mode === 'list';
    }

    render();
  }

  function shiftRange(direction) {
    if (currentMode === 'month') {
      cursor = new Date(cursor.getFullYear(), cursor.getMonth() + direction, 1);
      return;
    }
    if (currentMode === 'week') {
      cursor = addDays(cursor, direction * 7);
      return;
    }
    cursor = addDays(cursor, direction * 14);
  }

  function render() {
    if (currentMode === 'month') {
      renderMonth();
      return;
    }
    if (currentMode === 'week') {
      renderWeek();
      return;
    }
    renderList();
  }

  modeButtons.forEach((button) => {
    button.addEventListener('click', () => {
      toggleMode(button.dataset.calendarModeButton || 'month');
    });
  });

  prevButton.addEventListener('click', () => {
    shiftRange(-1);
    render();
  });

  nextButton.addEventListener('click', () => {
    shiftRange(1);
    render();
  });

  toggleMode('month');
})();
