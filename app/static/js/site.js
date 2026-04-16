(function () {
  const chat = document.getElementById('floating-chat');
  const closeBtn = document.getElementById('floating-chat-close');
  if (!chat || !closeBtn) return;

  if (!sessionStorage.getItem('chat_shown')) {
    setTimeout(() => {
      chat.classList.add('show');
      sessionStorage.setItem('chat_shown', '1');
    }, 45000);
  }

  closeBtn.addEventListener('click', () => chat.classList.remove('show'));
})();

(function () {
  const forms = document.querySelectorAll('.js-status-form');
  if (!forms.length) return;

  forms.forEach((form) => {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();

      const submitButton = form.querySelector('button[type="submit"]');
      const row = form.closest('tr');
      const labelCell = row ? row.querySelector('[data-status-label]') : null;
      const formData = new FormData(form);
      const originalText = submitButton ? submitButton.textContent : '';

      if (submitButton) {
        submitButton.disabled = true;
        submitButton.textContent = 'Сохраняем...';
      }

      try {
        const response = await fetch(form.action, {
          method: 'POST',
          body: formData,
          headers: {
            'X-Requested-With': 'XMLHttpRequest',
          },
        });

        if (!response.ok) {
          throw new Error('status update failed');
        }

        const payload = await response.json();
        if (labelCell && payload.label) {
          labelCell.textContent = payload.label;
        }

        if (row) {
          row.classList.add('status-row-updated');
          window.setTimeout(() => row.classList.remove('status-row-updated'), 1400);
        }

        if (submitButton) {
          submitButton.textContent = 'Сохранено';
          window.setTimeout(() => {
            submitButton.textContent = originalText;
            submitButton.disabled = false;
          }, 900);
        }
      } catch (error) {
        if (submitButton) {
          submitButton.textContent = originalText;
          submitButton.disabled = false;
        }
        window.alert('Не удалось обновить статус. Попробуйте еще раз.');
      }
    });
  });
})();

(function () {
  const tables = document.querySelectorAll('.table-card table');
  if (!tables.length || window.matchMedia('(max-width: 700px)').matches) return;

  const STORAGE_VERSION = 'v5';
  const MIN_COLUMN_WIDTH = 76;

  const readStoredWidth = (key) => {
    try {
      return Number(window.localStorage.getItem(key) || 0);
    } catch (error) {
      return 0;
    }
  };

  const writeStoredWidth = (key, value) => {
    try {
      window.localStorage.setItem(key, String(Math.round(value)));
    } catch (error) {
      void error;
    }
  };

  tables.forEach((table, tableIndex) => {
    const headRow = table.tHead && table.tHead.rows.length ? table.tHead.rows[0] : null;
    if (!headRow) return;

    const headers = Array.from(headRow.cells);
    if (!headers.length) return;

    table.classList.add('resizable-table');

    const widthCache = headers.map((header, headerIndex) => {
      const rawKeyBase = table.dataset.resizeKey || `${window.location.pathname}:${tableIndex}`;
      const keyBase = `${STORAGE_VERSION}:${rawKeyBase}`;
      const storageKey = `table-col-width:${keyBase}:${headerIndex}`;
      const measuredWidth = Math.ceil(header.getBoundingClientRect().width) || MIN_COLUMN_WIDTH;
      const hardLocked = header.classList.contains('is-column-fixed');
      const minLocked = header.classList.contains('is-column-locked');
      const explicitMinWidth = Number(header.dataset.colMinWidth || 0);
      const minWidth = Math.max(
        explicitMinWidth || 0,
        minLocked ? measuredWidth : MIN_COLUMN_WIDTH,
        MIN_COLUMN_WIDTH,
      );
      const storedWidth = hardLocked ? 0 : readStoredWidth(storageKey);
      return {
        header,
        storageKey,
        hardLocked,
        minLocked,
        minWidth,
        storedWidth,
        width: Math.max(storedWidth || measuredWidth, minWidth),
      };
    });

    let colgroup = table.querySelector('colgroup[data-resizable-columns]');
    if (colgroup) {
      colgroup.remove();
    }
    colgroup = document.createElement('colgroup');
    colgroup.dataset.resizableColumns = '1';
    table.insertBefore(colgroup, table.firstChild);

    const cols = widthCache.map((item) => {
      const col = document.createElement('col');
      col.style.width = `${item.width}px`;
      colgroup.appendChild(col);
      return col;
    });

    const getWidth = (index) => parseFloat(cols[index].style.width || '0') || widthCache[index].width || MIN_COLUMN_WIDTH;

    const getMinWidth = (index) => widthCache[index].minWidth;

    const getContainerWidth = () => (table.parentElement ? table.parentElement.clientWidth : 0);

    const applyTableWidth = () => {
      const totalWidth = cols.reduce((sum, col) => sum + (parseFloat(col.style.width || '0') || 0), 0);
      table.style.width = `${Math.ceil(totalWidth)}px`;
    };

    const setWidth = (index, value) => {
      const safeWidth = Math.max(getMinWidth(index), Math.round(value));
      widthCache[index].width = safeWidth;
      cols[index].style.width = `${safeWidth}px`;
    };

    const persistWidths = (...indexes) => {
      indexes.forEach((index) => {
        if (index < 0 || widthCache[index].hardLocked) return;
        writeStoredWidth(widthCache[index].storageKey, getWidth(index));
      });
    };

    const persistAllWidths = () => {
      persistWidths(...widthCache.map((_, index) => index));
    };

    const fitToContainer = () => {
      const containerWidth = getContainerWidth();
      if (!containerWidth) return;

      const widths = widthCache.map((_, index) => getWidth(index));
      const minWidths = widthCache.map((_, index) => getMinWidth(index));
      const unlockedIndexes = widthCache
        .map((item, index) => (item.hardLocked ? -1 : index))
        .filter((index) => index >= 0);

      if (!unlockedIndexes.length) {
        applyTableWidth();
        return;
      }

      let totalWidth = widths.reduce((sum, value) => sum + value, 0);

      if (totalWidth > containerWidth) {
        let overflow = totalWidth - containerWidth;
        while (overflow > 0.5) {
          const shrinkable = unlockedIndexes.filter((index) => widths[index] - minWidths[index] > 0.5);
          if (!shrinkable.length) break;

          const available = shrinkable.reduce((sum, index) => sum + (widths[index] - minWidths[index]), 0);
          if (available <= 0) break;

          let reduced = 0;
          shrinkable.forEach((index) => {
            const capacity = widths[index] - minWidths[index];
            const portion = (capacity / available) * overflow;
            const delta = Math.min(capacity, portion);
            widths[index] -= delta;
            reduced += delta;
          });

          if (reduced <= 0.1) break;
          overflow -= reduced;
        }
      } else if (totalWidth < containerWidth) {
        const extra = containerWidth - totalWidth;
        const bonus = extra / unlockedIndexes.length;
        unlockedIndexes.forEach((index) => {
          widths[index] += bonus;
        });
      }

      widths.forEach((value, index) => {
        setWidth(index, value);
      });
      applyTableWidth();
    };

    headers.forEach((header) => {
      header.querySelector('.col-resizer')?.remove();
    });

    const findCompanionIndex = (headerIndex) => {
      for (let index = headerIndex + 1; index < widthCache.length; index += 1) {
        if (!widthCache[index].hardLocked) {
          return index;
        }
      }
      for (let index = headerIndex - 1; index >= 0; index -= 1) {
        if (!widthCache[index].hardLocked) {
          return index;
        }
      }
      return -1;
    };

    headers.forEach((header, headerIndex) => {
      if (widthCache[headerIndex].hardLocked) return;

      const handle = document.createElement('span');
      handle.className = 'col-resizer';
      handle.setAttribute('aria-hidden', 'true');
      header.appendChild(handle);

      handle.addEventListener('mousedown', (event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        event.stopPropagation();

        const companionIndex = findCompanionIndex(headerIndex);
        const startX = event.clientX;
        const startWidth = getWidth(headerIndex);
        const startCompanionWidth = companionIndex >= 0 ? getWidth(companionIndex) : 0;
        const minWidth = getMinWidth(headerIndex);
        const minCompanionWidth = companionIndex >= 0 ? getMinWidth(companionIndex) : 0;

        const onMove = (moveEvent) => {
          const delta = moveEvent.clientX - startX;

          if (companionIndex >= 0) {
            let currentWidth = startWidth + delta;
            let companionWidth = startCompanionWidth - delta;

            if (currentWidth < minWidth) {
              currentWidth = minWidth;
              companionWidth = startCompanionWidth + (startWidth - minWidth);
            }

            if (companionWidth < minCompanionWidth) {
              companionWidth = minCompanionWidth;
              currentWidth = startWidth + (startCompanionWidth - minCompanionWidth);
            }

            setWidth(headerIndex, currentWidth);
            setWidth(companionIndex, companionWidth);
          } else {
            let currentWidth = startWidth + delta;
            const containerWidth = getContainerWidth();
            if (containerWidth) {
              const otherColumnsWidth = widthCache.reduce(
                (sum, _, index) => (index === headerIndex ? sum : sum + getWidth(index)),
                0,
              );
              const maxWidth = Math.max(minWidth, containerWidth - otherColumnsWidth);
              currentWidth = Math.min(currentWidth, maxWidth);
            }
            setWidth(headerIndex, currentWidth);
          }

          applyTableWidth();
        };

        const onUp = () => {
          document.body.classList.remove('is-resizing-columns');
          window.removeEventListener('mousemove', onMove);
          window.removeEventListener('mouseup', onUp);
          fitToContainer();
          persistAllWidths();
        };

        document.body.classList.add('is-resizing-columns');
        window.addEventListener('mousemove', onMove);
        window.addEventListener('mouseup', onUp);
      });
    });

    fitToContainer();
    window.addEventListener('resize', fitToContainer);
  });
})();

(function () {
  const board = document.querySelector('[data-kanban-board]');
  if (!board) return;

  const columns = board.querySelectorAll('[data-stage-column]');
  const cards = board.querySelectorAll('[data-case-card]');
  let activeCard = null;

  const syncEmptyState = (column) => {
    const placeholder = column.querySelector('.muted');
    const hasCards = column.querySelectorAll('[data-case-card]').length > 0;
    const count = column.querySelector('.kanban-column__count');
    if (placeholder) {
      placeholder.style.display = hasCards ? 'none' : '';
    }
    if (count) {
      count.textContent = String(column.querySelectorAll('[data-case-card]').length);
    }
  };

  const saveStage = async (card, column) => {
    const form = card.querySelector('.js-kanban-stage-form');
    const input = form ? form.querySelector('input[name="stage"]') : null;
    if (!form || !input) return false;

    const nextStage = column.dataset.stageValue;
    if (!nextStage || input.value === nextStage) return true;

    const originalValue = input.value;
    input.value = nextStage;

    const payload = new FormData();
    payload.set('stage', nextStage);

    try {
      const response = await fetch(form.action, {
        method: 'POST',
        body: payload,
        headers: {
          'X-Requested-With': 'XMLHttpRequest',
        },
      });

      if (!response.ok) {
        throw new Error('kanban update failed');
      }

      card.dataset.currentStage = nextStage;
      card.classList.add('status-row-updated');
      window.setTimeout(() => card.classList.remove('status-row-updated'), 1200);
      return true;
    } catch (error) {
      input.value = originalValue;
      window.alert('Не удалось переместить дело. Попробуйте еще раз.');
      return false;
    }
  };

  cards.forEach((card) => {
    card.addEventListener('dragstart', () => {
      activeCard = card;
      card.classList.add('dragging');
    });

    card.addEventListener('dragend', () => {
      card.classList.remove('dragging');
      columns.forEach((column) => column.classList.remove('drag-target'));
      activeCard = null;
    });
  });

  columns.forEach((column) => {
    syncEmptyState(column);

    column.addEventListener('dragover', (event) => {
      if (!activeCard) return;
      event.preventDefault();
      column.classList.add('drag-target');
    });

    column.addEventListener('dragleave', () => {
      column.classList.remove('drag-target');
    });

    column.addEventListener('drop', async (event) => {
      if (!activeCard) return;
      event.preventDefault();
      const previousColumn = activeCard.closest('[data-stage-column]');
      column.classList.remove('drag-target');
      column.appendChild(activeCard);
      if (previousColumn) {
        syncEmptyState(previousColumn);
      }
      syncEmptyState(column);

      const saved = await saveStage(activeCard, column);
      if (!saved && previousColumn) {
        previousColumn.appendChild(activeCard);
        syncEmptyState(previousColumn);
        syncEmptyState(column);
      }
    });
  });
})();

(function () {
  const forms = document.querySelectorAll('.js-notification-read-form');
  if (!forms.length) return;

  const unreadNode = document.getElementById('notifications-unread-count');

  forms.forEach((form) => {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();

      const button = form.querySelector('button[type="submit"]');
      const row = form.closest('[data-notification-row]');
      const statusCell = row ? row.querySelector('[data-notification-status]') : null;
      const actionCell = row ? row.querySelector('[data-notification-action]') : null;
      const originalText = button ? button.textContent : '';

      if (button) {
        button.disabled = true;
        button.textContent = 'Сохраняем...';
      }

      try {
        const response = await fetch(form.action, {
          method: 'POST',
          headers: {
            'X-Requested-With': 'XMLHttpRequest',
          },
        });

        if (!response.ok) {
          throw new Error('notification update failed');
        }

        const payload = await response.json();

        if (statusCell) {
          statusCell.textContent = 'Прочитано';
        }

        if (actionCell) {
          actionCell.textContent = '-';
        }

        if (typeof payload.unread === 'number' && unreadNode) {
          unreadNode.textContent = String(payload.unread);
        }

        if (row) {
          row.classList.add('status-row-updated');
          window.setTimeout(() => row.classList.remove('status-row-updated'), 1400);
        }
      } catch (error) {
        if (button) {
          button.disabled = false;
          button.textContent = originalText;
        }
        window.alert('Не удалось отметить уведомление как прочитанное. Попробуйте еще раз.');
        return;
      }

      if (button) {
        button.disabled = false;
      }
    });
  });
})();

(function () {
  const modal = document.getElementById('client-chat-modal');
  const form = document.getElementById('client-chat-form');
  const thread = document.getElementById('client-chat-thread');
  const titleNode = document.getElementById('client-chat-title');
  const metaNode = document.getElementById('client-chat-meta');
  const openButtons = document.querySelectorAll('[data-client-chat-open]');
  if (!modal || !form || !thread || !titleNode || !metaNode || !openButtons.length) return;

  const closeButtons = modal.querySelectorAll('[data-client-chat-close]');
  let activeClientId = '';

  const renderMessages = (messages) => {
    thread.innerHTML = '';
    if (!messages.length) {
      const empty = document.createElement('p');
      empty.className = 'workspace-list__empty';
      empty.textContent = 'Сообщений пока нет.';
      thread.appendChild(empty);
      return;
    }

    messages.forEach((item) => {
      const card = document.createElement('article');
      card.className = `client-chat-item ${item.is_from_client ? 'client-chat-item--client' : 'client-chat-item--staff'}`;

      const head = document.createElement('div');
      head.className = 'client-chat-item__meta';
      head.textContent = `${item.author} • ${item.created_at}`;

      const body = document.createElement('p');
      body.className = 'client-chat-item__text';
      body.textContent = item.message;

      card.appendChild(head);
      card.appendChild(body);
      thread.appendChild(card);
    });

    thread.scrollTop = thread.scrollHeight;
  };

  const loadChat = async (clientId) => {
    thread.innerHTML = '<p class="workspace-list__empty">Загружаем переписку...</p>';
    const response = await fetch(`/clients/${clientId}/chat`, {
      headers: {
        'X-Requested-With': 'XMLHttpRequest',
      },
    });
    if (!response.ok) {
      throw new Error('client chat load failed');
    }
    const payload = await response.json();
    titleNode.textContent = `Чат: ${payload.client.name}`;
    const metaParts = [];
    if (payload.client.email) metaParts.push(payload.client.email);
    if (payload.client.phone) metaParts.push(payload.client.phone);
    metaNode.textContent = metaParts.join(' • ');
    renderMessages(payload.messages || []);
  };

  const closeModal = () => {
    modal.hidden = true;
    document.body.classList.remove('is-modal-open');
    form.reset();
    activeClientId = '';
  };

  const openModal = async (button) => {
    activeClientId = button.dataset.clientId || '';
    titleNode.textContent = `Чат: ${button.dataset.clientName || 'Клиент'}`;
    metaNode.textContent = '';
    form.action = `/clients/${activeClientId}/chat`;
    modal.hidden = false;
    document.body.classList.add('is-modal-open');
    try {
      await loadChat(activeClientId);
    } catch (error) {
      thread.innerHTML = '<p class="workspace-list__empty">Не удалось загрузить чат.</p>';
    }
  };

  openButtons.forEach((button) => {
    button.addEventListener('click', async () => {
      await openModal(button);
    });
  });

  closeButtons.forEach((button) => {
    button.addEventListener('click', closeModal);
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !modal.hidden) {
      closeModal();
    }
  });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!activeClientId) return;

    const submitButton = form.querySelector('button[type="submit"]');
    const originalText = submitButton ? submitButton.textContent : '';
    const formData = new FormData(form);
    if (!formData.get('message') || !String(formData.get('message')).trim()) {
      window.alert('Введите текст сообщения.');
      return;
    }
    if (!formData.get('is_from_client')) {
      formData.set('is_from_client', 'false');
    }

    if (submitButton) {
      submitButton.disabled = true;
      submitButton.textContent = 'Отправляем...';
    }

    try {
      const response = await fetch(form.action, {
        method: 'POST',
        body: formData,
        headers: {
          'X-Requested-With': 'XMLHttpRequest',
        },
      });
      if (!response.ok) {
        throw new Error('client chat save failed');
      }
      const payload = await response.json();
      renderMessages(payload.messages || []);
      form.reset();
    } catch (error) {
      window.alert('Не удалось отправить сообщение. Попробуйте еще раз.');
    } finally {
      if (submitButton) {
        submitButton.disabled = false;
        submitButton.textContent = originalText;
      }
    }
  });
})();

(function () {
  const shell = document.querySelector('[data-clients-shell]');
  if (!shell) return;

  const directory = shell.querySelector('[data-clients-directory]');
  const chatPage = shell.querySelector('[data-client-chat-page]');
  const backButton = shell.querySelector('[data-client-chat-back]');
  const form = document.getElementById('client-chat-form');
  const thread = document.getElementById('client-chat-thread');
  const titleNode = document.getElementById('client-chat-title');
  const metaNode = document.getElementById('client-chat-meta');
  const openButtons = shell.querySelectorAll('[data-client-chat-open]');
  const overviewCasesTotal = document.getElementById('overview-cases-total');
  const overviewCasesActive = document.getElementById('overview-cases-active');
  const overviewTasksActive = document.getElementById('overview-tasks-active');
  const overviewTasksOverdue = document.getElementById('overview-tasks-overdue');
  const overviewInvoicesTotal = document.getElementById('overview-invoices-total');
  const overviewInvoicesUnpaid = document.getElementById('overview-invoices-unpaid');
  const overviewAddress = document.getElementById('overview-client-address');
  const overviewNotes = document.getElementById('overview-client-notes');
  const overviewCases = document.getElementById('overview-recent-cases');
  const overviewTasks = document.getElementById('overview-upcoming-tasks');
  const overviewInvoices = document.getElementById('overview-recent-invoices');
  const messageInput = form ? form.querySelector('textarea[name="message"]') : null;
  if (!directory || !chatPage || !backButton || !form || !thread || !titleNode || !metaNode) return;

  const ACTIVE_CHAT_STORAGE_KEY = 'clients-active-chat-id';
  const CHAT_DRAFT_STORAGE_PREFIX = 'clients-chat-draft:';
  let activeClientId = '';

  const readStoredClientId = () => {
    try {
      return window.localStorage.getItem(ACTIVE_CHAT_STORAGE_KEY) || '';
    } catch (error) {
      return '';
    }
  };

  const storeClientId = (clientId) => {
    try {
      window.localStorage.setItem(ACTIVE_CHAT_STORAGE_KEY, String(clientId));
    } catch (error) {
      void error;
    }
  };

  const clearStoredClientId = () => {
    try {
      window.localStorage.removeItem(ACTIVE_CHAT_STORAGE_KEY);
    } catch (error) {
      void error;
    }
  };

  const readStoredDraft = (clientId) => {
    if (!clientId) return '';
    try {
      return window.localStorage.getItem(`${CHAT_DRAFT_STORAGE_PREFIX}${clientId}`) || '';
    } catch (error) {
      return '';
    }
  };

  const storeDraft = (clientId, value) => {
    if (!clientId) return;
    const key = `${CHAT_DRAFT_STORAGE_PREFIX}${clientId}`;
    try {
      if (value) {
        window.localStorage.setItem(key, value);
      } else {
        window.localStorage.removeItem(key);
      }
    } catch (error) {
      void error;
    }
  };

  const clearDraft = (clientId) => {
    if (!clientId) return;
    try {
      window.localStorage.removeItem(`${CHAT_DRAFT_STORAGE_PREFIX}${clientId}`);
    } catch (error) {
      void error;
    }
  };

  const setChatMode = (enabled) => {
    shell.classList.toggle('clients-shell--chat-open', enabled);
    directory.hidden = enabled;
    chatPage.hidden = !enabled;
    chatPage.style.display = enabled ? '' : 'none';
  };

  const renderMessages = (messages) => {
    thread.innerHTML = '';
    if (!messages.length) {
      const empty = document.createElement('p');
      empty.className = 'workspace-list__empty';
      empty.textContent = 'Сообщений пока нет.';
      thread.appendChild(empty);
      return;
    }

    messages.forEach((item) => {
      const card = document.createElement('article');
      card.className = `client-chat-item ${item.is_from_client ? 'client-chat-item--client' : 'client-chat-item--staff'}`;

      const head = document.createElement('div');
      head.className = 'client-chat-item__meta';
      head.textContent = `${item.author} • ${item.created_at}`;

      const body = document.createElement('p');
      body.className = 'client-chat-item__text';
      body.textContent = item.message;

      card.appendChild(head);
      card.appendChild(body);
      thread.appendChild(card);
    });

    thread.scrollTop = thread.scrollHeight;
  };

  const renderOverviewList = (container, items, renderText) => {
    if (!container) return;
    container.innerHTML = '';
    if (!items || !items.length) {
      const empty = document.createElement('li');
      empty.className = 'workspace-list__empty';
      empty.textContent = 'Нет данных';
      container.appendChild(empty);
      return;
    }
    items.forEach((item) => {
      const li = document.createElement('li');
      li.textContent = renderText(item);
      container.appendChild(li);
    });
  };

  const currency = new Intl.NumberFormat('ru-RU', {
    style: 'currency',
    currency: 'RUB',
    maximumFractionDigits: 2,
  });

  const renderOverview = (payload) => {
    const overview = payload.overview || {};
    if (overviewCasesTotal) overviewCasesTotal.textContent = String(overview.cases_total || 0);
    if (overviewCasesActive) overviewCasesActive.textContent = String(overview.cases_active || 0);
    if (overviewTasksActive) overviewTasksActive.textContent = String(overview.tasks_active || 0);
    if (overviewTasksOverdue) overviewTasksOverdue.textContent = String(overview.tasks_overdue || 0);
    if (overviewInvoicesTotal) overviewInvoicesTotal.textContent = String(overview.invoices_total || 0);
    if (overviewInvoicesUnpaid) overviewInvoicesUnpaid.textContent = String(overview.invoices_unpaid || 0);

    if (overviewAddress) overviewAddress.textContent = payload.client.address || '-';
    if (overviewNotes) overviewNotes.textContent = payload.client.notes || '-';

    renderOverviewList(
      overviewCases,
      overview.recent_cases || [],
      (item) => `${item.case_number}: ${item.title} (${item.stage}, ${item.deadline})`,
    );
    renderOverviewList(
      overviewTasks,
      overview.upcoming_tasks || [],
      (item) => `${item.due_date} • ${item.case_number} • ${item.title} (${item.status})`,
    );
    renderOverviewList(
      overviewInvoices,
      overview.recent_invoices || [],
      (item) => `${item.number} • ${currency.format(Number(item.amount || 0))} • ${item.status} • до ${item.due_date}`,
    );
  };

  const loadChat = async (clientId) => {
    thread.innerHTML = '<p class="workspace-list__empty">Загружаем переписку...</p>';
    const response = await fetch(`/clients/${clientId}/chat`, {
      headers: {
        'X-Requested-With': 'XMLHttpRequest',
      },
    });
    if (!response.ok) {
      throw new Error('client chat load failed');
    }
    const payload = await response.json();
    titleNode.textContent = `Чат: ${payload.client.name}`;
    const metaParts = [];
    if (payload.client.email) metaParts.push(payload.client.email);
    if (payload.client.phone) metaParts.push(payload.client.phone);
    metaNode.textContent = metaParts.join(' • ');
    renderMessages(payload.messages || []);
    renderOverview(payload);
  };

  const closeChat = () => {
    setChatMode(false);
    form.reset();
    activeClientId = '';
    clearStoredClientId();
  };

  const openChatByClientId = async (clientId, clientName = 'Клиент') => {
    if (!clientId) return;
    activeClientId = String(clientId);
    storeClientId(activeClientId);
    titleNode.textContent = `Чат: ${clientName}`;
    metaNode.textContent = '';
    form.action = `/clients/${activeClientId}/chat`;
    setChatMode(true);
    if (messageInput) {
      messageInput.value = readStoredDraft(activeClientId);
    }
    try {
      await loadChat(activeClientId);
    } catch (error) {
      thread.innerHTML = '<p class="workspace-list__empty">Не удалось загрузить чат.</p>';
    }
  };

  if (messageInput) {
    messageInput.addEventListener('input', () => {
      if (!activeClientId) return;
      storeDraft(activeClientId, messageInput.value);
    });
  }

  openButtons.forEach((button) => {
    button.addEventListener('click', async () => {
      await openChatByClientId(button.dataset.clientId || '', button.dataset.clientName || 'Клиент');
    });
  });

  backButton.addEventListener('click', () => {
    closeChat();
  });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!activeClientId) return;

    const submitButton = form.querySelector('button[type="submit"]');
    const originalText = submitButton ? submitButton.textContent : '';
    const formData = new FormData(form);
    if (!formData.get('message') || !String(formData.get('message')).trim()) {
      window.alert('Введите текст сообщения.');
      return;
    }
    if (!formData.get('is_from_client')) {
      formData.set('is_from_client', 'false');
    }

    if (submitButton) {
      submitButton.disabled = true;
      submitButton.textContent = 'Отправляем...';
    }

    try {
      const response = await fetch(form.action, {
        method: 'POST',
        body: formData,
        headers: {
          'X-Requested-With': 'XMLHttpRequest',
        },
      });
      if (!response.ok) {
        throw new Error('client chat save failed');
      }
      const payload = await response.json();
      renderMessages(payload.messages || []);
      renderOverview(payload);
      clearDraft(activeClientId);
      form.reset();
    } catch (error) {
      window.alert('Не удалось отправить сообщение. Попробуйте еще раз.');
    } finally {
      if (submitButton) {
        submitButton.disabled = false;
        submitButton.textContent = originalText;
      }
    }
  });

  const initialClientId = shell.dataset.initialChatClientId || '';
  setChatMode(Boolean(initialClientId));
  if (initialClientId) {
    openChatByClientId(initialClientId);
  }
})();

(function () {
  const modal = document.getElementById('case-modal');
  const form = document.getElementById('case-modal-form');
  const commentForm = document.getElementById('case-comment-form');
  if (!modal || !form || !commentForm) return;

  const titleNode = document.getElementById('case-modal-title');
  const numberNode = document.getElementById('case-modal-number');
  const clientNode = document.getElementById('case-modal-client');
  const openedAtNode = document.getElementById('case-modal-opened-at');
  const stageNode = document.getElementById('case-modal-stage');
  const priorityChip = document.getElementById('case-modal-priority-chip');
  const riskBadge = document.getElementById('case-risk-badge');
  const riskList = document.getElementById('case-risk-list');
  const aiSummary = document.getElementById('case-ai-summary');
  const aiCategory = document.getElementById('case-ai-category');
  const aiSteps = document.getElementById('case-ai-steps');
  const aiDocs = document.getElementById('case-ai-docs');
  const aiSignals = document.getElementById('case-ai-signals');
  const commentsThread = document.getElementById('case-comments-thread');
  const closeButtons = modal.querySelectorAll('[data-case-modal-close]');
  const priorityClasses = ['case-chip--low', 'case-chip--medium', 'case-chip--high'];
  const badgeClasses = ['workspace-badge--low', 'workspace-badge--medium', 'workspace-badge--high'];
  let activeCard = null;

  const renderList = (container, items, classNameBuilder) => {
    container.innerHTML = '';
    if (!items.length) {
      const empty = document.createElement('li');
      empty.className = 'workspace-list__empty';
      empty.textContent = 'Нет данных';
      container.appendChild(empty);
      return;
    }

    items.forEach((item) => {
      const li = document.createElement('li');
      li.textContent = typeof item === 'string' ? item : item.text;
      if (classNameBuilder) {
        li.className = classNameBuilder(item);
      }
      container.appendChild(li);
    });
  };

  const renderSignals = (signals) => {
    aiSignals.innerHTML = '';
    signals.forEach((signal) => {
      const chip = document.createElement('span');
      chip.className = 'workspace-signal';
      chip.textContent = signal;
      aiSignals.appendChild(chip);
    });
  };

  const renderComments = (comments) => {
    commentsThread.innerHTML = '';
    if (!comments.length) {
      const empty = document.createElement('p');
      empty.className = 'workspace-list__empty';
      empty.textContent = 'Комментариев пока нет.';
      commentsThread.appendChild(empty);
      return;
    }

    comments.forEach((comment) => {
      const card = document.createElement('article');
      card.className = 'comment';

      const meta = document.createElement('div');
      meta.className = 'comment__meta';

      const author = document.createElement('strong');
      author.textContent = comment.author;
      meta.appendChild(author);

      const right = document.createElement('div');
      right.style.display = 'flex';
      right.style.gap = '8px';
      right.style.flexWrap = 'wrap';

      const type = document.createElement('span');
      type.className = 'comment__type';
      type.textContent = comment.is_internal ? 'Внутренний' : 'Клиентский';
      right.appendChild(type);

      const date = document.createElement('span');
      date.textContent = comment.created_at;
      right.appendChild(date);

      meta.appendChild(right);
      card.appendChild(meta);

      const message = document.createElement('p');
      message.className = 'comment__message';
      message.textContent = comment.message;
      card.appendChild(message);

      commentsThread.appendChild(card);
    });
  };

  const renderWorkspace = (payload) => {
    riskBadge.textContent = payload.risk.label;
    riskBadge.classList.remove(...badgeClasses);
    riskBadge.classList.add(`workspace-badge--${payload.risk.level}`);
    renderList(riskList, payload.risk.items, (item) => `workspace-list__item--${item.level}`);

    aiSummary.textContent = payload.ai.summary;
    aiCategory.textContent = payload.ai.predicted_category;
    renderList(aiSteps, payload.ai.next_steps);
    renderList(aiDocs, payload.ai.recommended_documents);
    renderSignals(payload.ai.signals || []);
    renderComments(payload.comments || []);
  };

  const loadWorkspace = async (caseId) => {
    riskBadge.textContent = 'Загрузка...';
    riskBadge.classList.remove(...badgeClasses);
    riskBadge.classList.add('workspace-badge--medium');
    riskList.innerHTML = '<li class="workspace-list__empty">Собираем сигналы по делу...</li>';
    aiSummary.textContent = 'Готовим рекомендации...';
    aiCategory.textContent = '-';
    aiSteps.innerHTML = '<li class="workspace-list__empty">Нет данных</li>';
    aiDocs.innerHTML = '<li class="workspace-list__empty">Нет данных</li>';
    aiSignals.innerHTML = '';
    commentsThread.innerHTML = '<p class="workspace-list__empty">Загружаем обсуждение...</p>';

    const response = await fetch(`/cases/${caseId}/workspace`, {
      headers: {
        'X-Requested-With': 'XMLHttpRequest',
      },
    });
    if (!response.ok) {
      throw new Error('workspace load failed');
    }
    const payload = await response.json();
    renderWorkspace(payload);
  };

  const closeModal = () => {
    modal.hidden = true;
    document.body.classList.remove('is-modal-open');
    activeCard = null;
    commentForm.reset();
  };

  const openModal = async (card) => {
    activeCard = card;
    const stageColumn = card.closest('[data-stage-column]');
    const stageTitle = stageColumn ? stageColumn.querySelector('h3') : null;

    numberNode.textContent = card.dataset.caseNumber || 'Дело';
    titleNode.textContent = card.dataset.caseTitle || 'Карточка дела';
    clientNode.textContent = card.dataset.caseClient || '';
    openedAtNode.textContent = card.dataset.caseOpenedAt || '-';
    stageNode.textContent = stageTitle ? stageTitle.textContent : '';
    priorityChip.textContent = card.dataset.casePriorityLabel || 'Средний';
    priorityChip.classList.remove(...priorityClasses);
    priorityChip.classList.add(`case-chip--${(card.dataset.casePriority || 'medium').toLowerCase()}`);

    form.action = `/cases/${card.dataset.caseId}/edit`;
    form.elements.title.value = card.dataset.caseTitle || '';
    form.elements.category.value = card.dataset.caseCategory || '';
    form.elements.description.value = card.dataset.caseDescription || '';
    form.elements.deadline.value = card.dataset.caseDeadlineInput || '';
    form.elements.priority.value = card.dataset.casePriority || 'MEDIUM';
    form.elements.responsible_lawyer_id.value = card.dataset.caseLawyerId || '';
    commentForm.action = `/cases/${card.dataset.caseId}/comments`;

    modal.hidden = false;
    document.body.classList.add('is-modal-open');

    try {
      await loadWorkspace(card.dataset.caseId);
    } catch (error) {
      riskBadge.textContent = 'Ошибка';
      riskList.innerHTML = '<li class="workspace-list__empty">Не удалось загрузить анализ по делу.</li>';
      commentsThread.innerHTML = '<p class="workspace-list__empty">Не удалось загрузить комментарии.</p>';
    }
  };

  const syncCard = (card, payload) => {
    const cardTitle = card.querySelector('[data-case-title-text]');
    const cardDescription = card.querySelector('[data-case-description-text]');
    const cardLawyer = card.querySelector('[data-case-lawyer-text]');
    const cardDeadline = card.querySelector('[data-case-deadline-text]');
    const chip = card.querySelector('[data-case-priority-chip]');

    card.dataset.caseTitle = payload.title;
    card.dataset.caseCategory = payload.category;
    card.dataset.caseDescription = payload.description;
    card.dataset.casePriority = payload.priority;
    card.dataset.casePriorityLabel = payload.priority_label;
    card.dataset.caseDeadline = payload.deadline;
    card.dataset.caseDeadlineInput = payload.deadline_input;
    card.dataset.caseLawyerId = String(payload.responsible_lawyer_id || '');
    card.dataset.caseLawyerName = payload.responsible_lawyer_name;

    if (cardTitle) cardTitle.textContent = payload.title;
    if (cardDescription) cardDescription.textContent = payload.description;
    if (cardLawyer) cardLawyer.textContent = payload.responsible_lawyer_name;
    if (cardDeadline) cardDeadline.textContent = payload.deadline;
    if (chip) {
      chip.textContent = payload.priority_label;
      chip.classList.remove(...priorityClasses);
      chip.classList.add(`case-chip--${(payload.priority || 'MEDIUM').toLowerCase()}`);
    }

    priorityChip.textContent = payload.priority_label;
    priorityChip.classList.remove(...priorityClasses);
    priorityChip.classList.add(`case-chip--${(payload.priority || 'MEDIUM').toLowerCase()}`);
  };

  document.querySelectorAll('[data-case-open]').forEach((button) => {
    button.addEventListener('click', async () => {
      const card = button.closest('[data-case-card]');
      if (card) {
        await openModal(card);
      }
    });
  });

  closeButtons.forEach((button) => {
    button.addEventListener('click', closeModal);
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !modal.hidden) {
      closeModal();
    }
  });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!activeCard) return;

    const submitButton = form.querySelector('button[type="submit"]');
    const originalText = submitButton ? submitButton.textContent : '';
    if (submitButton) {
      submitButton.disabled = true;
      submitButton.textContent = 'Сохраняем...';
    }

    try {
      const response = await fetch(form.action, {
        method: 'POST',
        body: new FormData(form),
        headers: {
          'X-Requested-With': 'XMLHttpRequest',
        },
      });
      if (!response.ok) {
        throw new Error('case edit failed');
      }

      const payload = await response.json();
      if (payload.case) {
        syncCard(activeCard, payload.case);
      }
      await loadWorkspace(activeCard.dataset.caseId);
      activeCard.classList.add('status-row-updated');
      window.setTimeout(() => activeCard.classList.remove('status-row-updated'), 1200);
    } catch (error) {
      window.alert('Не удалось сохранить карточку дела. Попробуйте еще раз.');
    } finally {
      if (submitButton) {
        submitButton.disabled = false;
        submitButton.textContent = originalText;
      }
    }
  });

  commentForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!activeCard) return;

    const submitButton = commentForm.querySelector('button[type="submit"]');
    const originalText = submitButton ? submitButton.textContent : '';
    const commentData = new FormData(commentForm);
    if (!commentData.get('message') || !String(commentData.get('message')).trim()) {
      window.alert('Введите текст комментария.');
      return;
    }
    if (!commentData.get('is_internal')) {
      commentData.set('is_internal', 'false');
    }

    if (submitButton) {
      submitButton.disabled = true;
      submitButton.textContent = 'Отправляем...';
    }

    try {
      const response = await fetch(commentForm.action, {
        method: 'POST',
        body: commentData,
        headers: {
          'X-Requested-With': 'XMLHttpRequest',
        },
      });
      if (!response.ok) {
        throw new Error('comment add failed');
      }
      const payload = await response.json();
      renderWorkspace(payload);
      commentForm.reset();
      commentForm.elements.is_internal.checked = true;
    } catch (error) {
      window.alert('Не удалось отправить комментарий. Попробуйте еще раз.');
    } finally {
      if (submitButton) {
        submitButton.disabled = false;
        submitButton.textContent = originalText;
      }
    }
  });
})();
