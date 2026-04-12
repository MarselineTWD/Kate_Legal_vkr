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
  if (!tables.length) return;

  tables.forEach((table, tableIndex) => {
    const headers = Array.from(table.querySelectorAll('thead th'));
    if (!headers.length) return;

    table.classList.add('resizable-table');

    const syncTableWidth = () => {
      const containerWidth = table.parentElement ? table.parentElement.clientWidth : table.clientWidth;
      const totalWidth = headers.reduce((sum, item) => sum + Math.max(110, item.offsetWidth), 0);
      table.style.width = `${Math.max(containerWidth, totalWidth)}px`;
    };

    headers.forEach((header, headerIndex) => {
      if (header.querySelector('.col-resizer')) return;

      const storedWidth = (() => {
        try {
          return Number(window.localStorage.getItem(`table-width-${tableIndex}-${headerIndex}`) || 0);
        } catch (error) {
          return 0;
        }
      })();
      const initialWidth = Math.max(110, storedWidth || header.offsetWidth);
      header.style.width = `${initialWidth}px`;

      const handle = document.createElement('span');
      handle.className = 'col-resizer';
      handle.setAttribute('aria-hidden', 'true');
      header.appendChild(handle);

      handle.addEventListener('mousedown', (event) => {
        event.preventDefault();
        const startX = event.clientX;
        const startWidth = header.offsetWidth;
        const minWidth = 110;

        const onMove = (moveEvent) => {
          const nextWidth = Math.max(minWidth, startWidth + moveEvent.clientX - startX);
          header.style.width = `${nextWidth}px`;
          syncTableWidth();
          try {
            window.localStorage.setItem(`table-width-${tableIndex}-${headerIndex}`, String(nextWidth));
          } catch (error) {
            void error;
          }
        };

        const onUp = () => {
          document.removeEventListener('mousemove', onMove);
          document.removeEventListener('mouseup', onUp);
          document.body.classList.remove('is-resizing-columns');
        };

        document.body.classList.add('is-resizing-columns');
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
      });
    });

    syncTableWidth();
    window.addEventListener('resize', syncTableWidth);
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
