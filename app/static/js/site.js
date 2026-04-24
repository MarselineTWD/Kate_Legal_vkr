(function () {
  const chat = document.getElementById('floating-chat');
  const closeBtn = document.getElementById('floating-chat-close');
  if (!chat || !closeBtn) return;

  if (!sessionStorage.getItem('chat_shown')) {
    window.setTimeout(() => {
      chat.classList.add('show');
      sessionStorage.setItem('chat_shown', '1');
    }, 45000);
  }

  closeBtn.addEventListener('click', () => chat.classList.remove('show'));
})();

(function () {
  const bindStatusForms = (root = document) => {
    const forms = root.querySelectorAll('.js-status-form');
    if (!forms.length) return;

    forms.forEach((form) => {
      if (form.dataset.statusBound === '1') return;
      form.dataset.statusBound = '1';

      form.addEventListener('submit', async (event) => {
        event.preventDefault();

        const submitButton = form.querySelector('button[type="submit"]');
        const row = form.closest('[data-status-row]') || form.closest('tr') || form.closest('.ops-task-card');
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
  };

  bindStatusForms();
  window.bindStatusForms = bindStatusForms;
})();

(function () {
  const parseTargets = (value) => String(value || '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
  const scrollLatestMessages = (scope = document) => {
    const threads = scope.querySelectorAll('.ops-chat-thread, .client-chat-thread, .comment-thread');
    threads.forEach((thread) => {
      thread.scrollTop = thread.scrollHeight;
    });
  };

  const restoreViewPanels = () => {
    const roots = document.querySelectorAll('[data-view-root]');
    roots.forEach((root) => {
      const buttons = Array.from(root.querySelectorAll('[data-view-button]'));
      const panelScope = root.closest('[data-list-filter-root]') || root;
      const panels = Array.from(panelScope.querySelectorAll('[data-view-panel]'));
      if (!buttons.length || !panels.length) return;

      const activeButton = buttons.find((button) => button.classList.contains('is-active'));
      const urlView = new URLSearchParams(window.location.search).get('tab');
      const viewName = (activeButton && activeButton.dataset.viewButton)
        || (urlView || '').trim()
        || root.dataset.defaultView
        || buttons[0].dataset.viewButton
        || panels[0].dataset.viewPanel
        || '';

      buttons.forEach((button) => {
        const active = button.dataset.viewButton === viewName;
        button.classList.toggle('is-active', active);
        button.setAttribute('aria-pressed', String(active));
      });

      panels.forEach((panel) => {
        panel.hidden = panel.dataset.viewPanel !== viewName;
      });
    });
  };

  document.addEventListener('submit', async (event) => {
    const form = event.target.closest('form[data-partial-refresh-form]');
    if (!form) return;
    event.preventDefault();

    const submitButton = form.querySelector('button[type="submit"]');
    const originalText = submitButton ? submitButton.textContent : '';
    if (submitButton) {
      submitButton.disabled = true;
      submitButton.textContent = 'Сохраняем...';
    }

    try {
      const response = await fetch(form.action, {
        method: (form.method || 'POST').toUpperCase(),
        body: new FormData(form),
      });

      if (!response.ok) {
        throw new Error('partial refresh failed');
      }

      const html = await response.text();
      const parser = new DOMParser();
      const nextDoc = parser.parseFromString(html, 'text/html');
      const selectors = parseTargets(form.dataset.refreshTargets);
      let replacedCount = 0;

      selectors.forEach((selector) => {
        const currentNode = document.querySelector(selector);
        const nextNode = nextDoc.querySelector(selector);
        if (!currentNode || !nextNode) return;
        currentNode.replaceWith(nextNode);
        nextNode.classList.add('status-row-updated');
        window.setTimeout(() => nextNode.classList.remove('status-row-updated'), 900);
        if (typeof window.bindStatusForms === 'function') {
          window.bindStatusForms(nextNode);
        }
        scrollLatestMessages(nextNode);
        replacedCount += 1;
      });

      if (!replacedCount) {
        window.location.assign(response.url || window.location.href);
        return;
      }
      restoreViewPanels();
      window.requestAnimationFrame(() => scrollLatestMessages(document));
    } catch (error) {
      window.alert('Не удалось обновить блоки страницы. Попробуйте еще раз.');
      window.location.reload();
      return;
    } finally {
      if (submitButton) {
        submitButton.disabled = false;
        submitButton.textContent = originalText;
      }
    }
  });

  window.requestAnimationFrame(() => scrollLatestMessages(document));
})();

(function () {
  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-add-lawyer-select]');
    if (!button) return;

    const form = button.closest('[data-lawyer-picker-form]');
    const source = form ? form.querySelector('[data-lawyer-source]') : null;
    const container = form ? form.querySelector('[data-extra-lawyer-selects]') : null;
    if (!source || !container) return;

    const nextNumber = container.querySelectorAll('select[name="team_lawyer_ids"]').length + 2;
    const row = document.createElement('label');
    row.className = 'intake-action-row';
    const label = document.createElement('span');
    label.textContent = `Юрист ${nextNumber} -`;
    const select = source.cloneNode(true);
    select.removeAttribute('id');
    select.removeAttribute('data-lawyer-source');
    select.name = 'team_lawyer_ids';
    select.required = false;
    select.disabled = false;
    select.selectedIndex = 0;
    row.appendChild(label);
    row.appendChild(select);
    container.appendChild(row);
  });
})();

(function () {
  const board = document.querySelector('[data-kanban-board]');
  if (!board) return;

  const columns = board.querySelectorAll('[data-stage-column]');
  const cards = board.querySelectorAll('[data-case-card]');
  let activeCard = null;

  const syncColumn = (column) => {
    const placeholder = column.querySelector('.muted');
    const count = column.querySelector('.kanban-column__count');
    const cardsInColumn = column.querySelectorAll('[data-case-card]').length;
    if (placeholder) {
      placeholder.style.display = cardsInColumn ? 'none' : '';
    }
    if (count) {
      count.textContent = String(cardsInColumn);
    }
  };

  const saveStage = async (card, column) => {
    const form = card.querySelector('.js-kanban-stage-form');
    const input = form ? form.querySelector('input[name="stage"]') : null;
    if (!form || !input) return true;

    const nextStage = column.dataset.stageValue;
    if (!nextStage || input.value === nextStage) return true;

    const originalValue = input.value;
    input.value = nextStage;
    const formData = new FormData();
    formData.set('stage', nextStage);

    try {
      const response = await fetch(form.action, {
        method: 'POST',
        body: formData,
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
      activeCard = null;
      card.classList.remove('dragging');
      columns.forEach((column) => column.classList.remove('drag-target'));
    });
  });

  columns.forEach((column) => {
    syncColumn(column);

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
      column.classList.remove('drag-target');

      const previousColumn = activeCard.closest('[data-stage-column]');
      column.appendChild(activeCard);
      if (previousColumn) syncColumn(previousColumn);
      syncColumn(column);

      const saved = await saveStage(activeCard, column);
      if (!saved && previousColumn) {
        previousColumn.appendChild(activeCard);
        syncColumn(previousColumn);
        syncColumn(column);
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
          actionCell.innerHTML = '<span class="notification-item__hint">Уже просмотрено</span>';
        }
        if (row) {
          row.classList.remove('is-unread');
          row.classList.add('status-row-updated');
          window.setTimeout(() => row.classList.remove('status-row-updated'), 1200);
        }
        if (typeof payload.unread === 'number' && unreadNode) {
          unreadNode.textContent = String(payload.unread);
        }
      } catch (error) {
        if (button) {
          button.disabled = false;
          button.textContent = originalText;
        }
        window.alert('Не удалось отметить уведомление как прочитанное.');
        return;
      }

      if (button) {
        button.disabled = false;
        button.textContent = originalText;
      }
    });
  });
})();

(function () {
  const modal = document.getElementById('portal-confirm-modal');
  if (!modal) return;

  const titleNode = modal.querySelector('[data-confirm-title]');
  const textNode = modal.querySelector('[data-confirm-text]');
  const submitButton = modal.querySelector('[data-confirm-submit]');
  const closeButtons = modal.querySelectorAll('[data-confirm-close]');
  let activeForm = null;

  const closeModal = () => {
    modal.hidden = true;
    document.body.classList.remove('is-modal-open');
    activeForm = null;
  };

  const openModal = (form) => {
    activeForm = form;
    if (titleNode) {
      titleNode.textContent = form.dataset.confirmTitle || 'Подтвердите действие';
    }
    if (textNode) {
      textNode.textContent = form.dataset.confirmText || 'Действие будет выполнено без возможности быстрого отката.';
    }
    if (submitButton) {
      submitButton.textContent = form.dataset.confirmSubmitLabel || 'Подтвердить';
    }
    modal.hidden = false;
    document.body.classList.add('is-modal-open');
  };

  document.addEventListener('submit', (event) => {
    const form = event.target.closest('[data-confirm-form]');
    if (!form) return;
    if (form.dataset.confirmApproved === '1') {
      delete form.dataset.confirmApproved;
      return;
    }
    event.preventDefault();
    openModal(form);
  });

  closeButtons.forEach((button) => button.addEventListener('click', closeModal));

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !modal.hidden) {
      closeModal();
    }
  });

  submitButton?.addEventListener('click', () => {
    if (!activeForm) return;
    activeForm.dataset.confirmApproved = '1';
    if (typeof activeForm.requestSubmit === 'function') {
      activeForm.requestSubmit();
    } else {
      activeForm.submit();
    }
    closeModal();
  });
})();

(function () {
  const shell = document.querySelector('[data-clients-shell]');
  if (!shell) return;

  const directory = shell.querySelector('[data-clients-directory]');
  const chatPage = shell.querySelector('[data-client-chat-page]');
  const backButton = shell.querySelector('[data-client-chat-back]');
  const titleNode = document.getElementById('client-chat-title');
  const metaNode = document.getElementById('client-chat-meta');
  const openButtons = shell.querySelectorAll('[data-client-chat-open]');
  const overviewCasesTotal = document.getElementById('overview-cases-total');
  const overviewCasesActive = document.getElementById('overview-cases-active');
  const overviewTasksActive = document.getElementById('overview-tasks-active');
  const overviewTasksOverdue = document.getElementById('overview-tasks-overdue');
  const overviewClientType = document.getElementById('overview-client-type');
  const overviewOrganizationLabel = document.getElementById('overview-client-organization-label');
  const overviewOrganization = document.getElementById('overview-client-organization');
  const overviewAddress = document.getElementById('overview-client-address');
  const overviewProblemLabel = document.getElementById('overview-client-problem-label');
  const overviewProblem = document.getElementById('overview-client-problem');
  const overviewNotesLabel = document.getElementById('overview-client-notes-label');
  const overviewNotes = document.getElementById('overview-client-notes');
  const overviewPassport = document.getElementById('overview-client-passport');
  const overviewRequisitesLabel = document.getElementById('overview-client-requisites-label');
  const overviewRequisites = document.getElementById('overview-client-requisites');
  const overviewClientRequests = document.getElementById('overview-client-requests');
  const overviewTaskCaseSelect = document.getElementById('overview-task-case-select');
  const overviewNewTaskReturnTo = document.getElementById('overview-new-task-return-to');
  const overviewTaskForm = overviewTaskCaseSelect ? overviewTaskCaseSelect.closest('form') : null;
  if (!directory || !chatPage || !backButton || !titleNode || !metaNode) return;

  const ACTIVE_CHAT_STORAGE_KEY = 'clients-active-chat-id';
  let activeClientId = '';

  const escapeHtml = (value) => String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');

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

  const setChatMode = (enabled) => {
    shell.classList.toggle('clients-shell--chat-open', enabled);
    directory.hidden = enabled;
    chatPage.hidden = !enabled;
    chatPage.style.display = enabled ? '' : 'none';
  };

  const renderOverview = (payload) => {
    const overview = payload.overview || {};
    if (overviewCasesTotal) overviewCasesTotal.textContent = String(overview.cases_total || 0);
    if (overviewCasesActive) overviewCasesActive.textContent = String(overview.cases_active || 0);
    if (overviewTasksActive) overviewTasksActive.textContent = String(overview.tasks_active || 0);
    if (overviewTasksOverdue) overviewTasksOverdue.textContent = String(overview.tasks_overdue || 0);

    const isOrganization = payload.client.client_type === 'ORGANIZATION';
    if (overviewClientType) {
      overviewClientType.textContent = isOrganization ? 'Юрлицо' : 'Физлицо';
    }
    if (overviewOrganizationLabel) overviewOrganizationLabel.hidden = !isOrganization;
    if (overviewOrganization) {
      overviewOrganization.hidden = !isOrganization;
      overviewOrganization.textContent = isOrganization ? (payload.client.organization_name || '-') : '';
    }
    if (overviewAddress) overviewAddress.textContent = payload.client.address || '-';
    const showProblemDetails = payload.client.show_problem_details !== false;
    if (overviewProblemLabel) overviewProblemLabel.hidden = !showProblemDetails;
    if (overviewProblem) {
      overviewProblem.hidden = !showProblemDetails;
      overviewProblem.textContent = showProblemDetails ? (payload.client.problem_summary || '-') : '';
    }
    if (overviewNotesLabel) overviewNotesLabel.hidden = !showProblemDetails;
    if (overviewNotes) {
      overviewNotes.hidden = !showProblemDetails;
      overviewNotes.textContent = showProblemDetails ? (payload.client.notes || '-') : '';
    }
    if (overviewPassport) overviewPassport.textContent = payload.client.passport_details || '-';
    if (overviewRequisitesLabel) overviewRequisitesLabel.hidden = !isOrganization;
    if (overviewRequisites) {
      overviewRequisites.hidden = !isOrganization;
      overviewRequisites.textContent = isOrganization ? (payload.client.organization_requisites || '-') : '';
    }
  };

  const renderClientActions = (payload) => {
    const actions = payload.actions || {};
    const returnTo = actions.return_to || '/clients';
    const caseOptions = actions.case_options || [];
    const requests = actions.intake_requests || [];

    if (overviewNewTaskReturnTo) overviewNewTaskReturnTo.value = returnTo;

    if (overviewTaskCaseSelect) {
      overviewTaskCaseSelect.innerHTML = '';
      const placeholder = document.createElement('option');
      placeholder.value = '';
      placeholder.textContent = 'Выберите дело';
      overviewTaskCaseSelect.appendChild(placeholder);

      caseOptions.forEach((item) => {
        const option = document.createElement('option');
        option.value = String(item.id);
        option.textContent = item.label;
        overviewTaskCaseSelect.appendChild(option);
      });

      const hasCases = caseOptions.length > 0;
      overviewTaskCaseSelect.disabled = !hasCases;
      if (overviewTaskForm) {
        const submit = overviewTaskForm.querySelector('button[type="submit"]');
        if (submit) submit.disabled = !hasCases;
      }
    }

    if (!overviewClientRequests) return;
    overviewClientRequests.innerHTML = '';
    if (!requests.length) {
      overviewClientRequests.innerHTML = '<div class="workspace-list__empty">Обращения пока не созданы</div>';
      return;
    }

    const requestCards = requests.map((item) => {
      const intakeBadgeClass = item.intake_approved
        ? 'status-badge--success'
        : item.intake_status === 'WITHDRAWN' || item.intake_status === 'CLOSED'
          ? 'status-badge--surface'
          : 'status-badge--warning';
      const badges = [
        `<span class="status-badge ${intakeBadgeClass}">${escapeHtml(item.intake_status_label)}</span>`,
      ];
      if (item.is_consultation) {
        badges.push('<span class="status-badge status-badge--info">Консультация</span>');
      }

      const bestRecommendation = item.recommendations && item.recommendations.length
        ? `<div class="ops-state-note"><strong>Рекомендация TOPSIS:</strong> ${escapeHtml(item.recommendations[0].full_name)}${item.recommendations[0].specialization ? `, ${escapeHtml(item.recommendations[0].specialization)}` : ''}. Балл: ${escapeHtml(item.recommendations[0].score_label)}.</div>`
        : '<div class="ops-state-note">TOPSIS не нашел профильную рекомендацию. Будет выбран лучший доступный юрист.</div>';

      const adminComment = item.admin_comment
        ? `<div class="ops-state-note"><strong>Комментарий администратора:</strong> ${escapeHtml(item.admin_comment)}</div>`
        : '';
      const assignedLawyers = Array.isArray(item.assigned_lawyers) ? item.assigned_lawyers : [];
      const documents = Array.isArray(item.documents) ? item.documents : [];
      const documentsBlock = documents.length
        ? `
          <div class="intake-documents">
            <span>Документы заявки</span>
            <div class="intake-documents__list">
              ${documents.map((document) => `
                <a href="/documents/${document.id}/view" class="intake-documents__item" target="_blank" rel="noopener">
                  <strong>${escapeHtml(document.name)}</strong>
                  <small>${escapeHtml(document.created_at)}${document.description ? ` • ${escapeHtml(document.description)}` : ''}</small>
                </a>
              `).join('')}
            </div>
          </div>
        `
        : '<div class="intake-documents intake-documents--empty"><span>Документы заявки</span><p>Документы по этой заявке пока не загружены.</p></div>';

      const lawyersBlock = assignedLawyers.length
        ? `
          <div class="intake-lawyers">
            <span>Юридическая команда</span>
            <div class="intake-lawyers__list">
              ${assignedLawyers.map((lawyer) => `
                <div class="intake-lawyers__item">
                  <div class="intake-lawyers__copy">
                    <strong>${escapeHtml(lawyer.full_name)}</strong>
                    <small>${lawyer.is_responsible ? 'Ответственный юрист' : (escapeHtml(lawyer.specialization || 'Юрист по делу'))}</small>
                  </div>
                  <form method="post" action="/cases/${item.id}/lawyers/${lawyer.id}/remove" data-confirm-form data-confirm-title="Удалить юриста из обращения?" data-confirm-text="Юрист будет исключен из команды этого обращения и потеряет доступ к карточке.">
                    <input type="hidden" name="return_to" value="${escapeHtml(returnTo)}" />
                    <button type="submit" class="portal-button portal-button--danger-ghost">Удалить юриста</button>
                  </form>
                </div>
              `).join('')}
            </div>
          </div>
        `
        : '';

      const actionForms = payload.viewer && payload.viewer.role === 'ADMIN' && !item.intake_approved && item.intake_status === 'PENDING_REVIEW'
        ? `
          ${bestRecommendation}
          <form method="post" action="/admin/intake/${item.id}/accept" class="ops-inline-form intake-action-form">
            <input type="hidden" name="return_to" value="${escapeHtml(returnTo)}" />
            <div class="ops-state-note">Юрист будет назначен автоматически по TOPSIS после одобрения обращения.</div>
            <label class="intake-action-row">
              <span>Приоритет -</span>
              <select name="priority">
                <option value="LOW"${item.priority === 'LOW' ? ' selected' : ''}>Низкий</option>
                <option value="MEDIUM"${item.priority === 'MEDIUM' ? ' selected' : ''}>Средний</option>
                <option value="HIGH"${item.priority === 'HIGH' ? ' selected' : ''}>Высокий</option>
              </select>
            </label>
            <button type="submit">Принять</button>
          </form>
          <form method="post" action="/admin/intake/${item.id}/clarify" class="ops-inline-form intake-action-form">
            <input type="hidden" name="return_to" value="${escapeHtml(returnTo)}" />
            <label class="intake-action-row">
              <span>Исправления -</span>
              <input type="text" name="comment" placeholder="Что нужно доработать" required />
            </label>
            <button type="submit">Вернуть на доработку</button>
          </form>
          <form method="post" action="/admin/intake/${item.id}/close" class="ops-inline-form intake-action-form">
            <input type="hidden" name="return_to" value="${escapeHtml(returnTo)}" />
            <label class="intake-action-row">
              <span>Отказ -</span>
              <input type="text" name="comment" placeholder="Причина отказа" />
            </label>
            <button type="submit">Отказать</button>
          </form>
        `
        : '';

      const deleteCaseForm = payload.viewer && payload.viewer.role === 'ADMIN'
        ? `
          <form method="post" action="/cases/${item.id}/delete" class="intake-delete-form" data-confirm-form data-confirm-title="Удалить обращение?" data-confirm-text="Будут удалены карточка обращения, связанные задачи, документы и переписка по этой карточке.">
            <input type="hidden" name="return_to" value="${escapeHtml(returnTo)}" />
            <button type="submit" class="portal-button portal-button--danger-ghost">Удалить обращение</button>
          </form>
        `
        : '';

      const compactMeta = `
        <div class="client-request-row">
          <div class="client-request-row__main">
            <strong>${escapeHtml(item.case_number)}</strong>
            <span>${escapeHtml(item.title)}</span>
          </div>
          <div class="client-request-row__meta">
            <span>${escapeHtml(item.opened_at)}</span>
            <span>${escapeHtml(item.intake_status_label)}</span>
            <span>${escapeHtml(item.category)}</span>
          </div>
        </div>
      `;

      return `
        <details class="client-request-disclosure">
          <summary class="client-request-disclosure__summary">
            ${compactMeta}
          </summary>
          <article class="ops-data-list__item">
            <div class="ops-inline-meta">
              <strong>${escapeHtml(item.case_number)} — ${escapeHtml(item.title)}</strong>
              ${badges.join('')}
            </div>
            <div class="ops-meta-grid intake-compact-meta">
              <div class="ops-meta-item"><span>Дата</span><strong>${escapeHtml(item.opened_at)}</strong></div>
              <div class="ops-meta-item"><span>Категория</span><strong>${escapeHtml(item.category)}</strong></div>
              <div class="ops-meta-item"><span>Связь</span><strong>${escapeHtml(item.preferred_contact_method_label)}</strong></div>
              <div class="ops-meta-item"><span>Юристы</span><strong>${escapeHtml(item.team_lawyer_names || item.responsible_lawyer_name)}</strong></div>
            </div>
            <div class="intake-description">
              <span>Описание</span>
              <p>${escapeHtml(item.description || 'Описание обращения пока не заполнено.')}</p>
            </div>
            ${documentsBlock}
            ${lawyersBlock}
            ${adminComment}
            ${actionForms}
            ${deleteCaseForm}
          </article>
        </details>
      `;
    }).join('');

    overviewClientRequests.innerHTML = `
      <details class="client-requests-disclosure">
        <summary class="client-requests-disclosure__summary">
          <div>
            <strong>Обращения клиента</strong>
            <small>Разверните список, чтобы посмотреть карточки и действия по каждой заявке.</small>
          </div>
          <span class="client-requests-disclosure__count">${requests.length}</span>
        </summary>
        <div class="client-requests-disclosure__body">
          ${requestCards}
        </div>
      </details>
    `;
  };

  const loadChat = async (clientId) => {
    const response = await fetch(`/clients/${clientId}/chat`, {
      headers: {
        'X-Requested-With': 'XMLHttpRequest',
      },
    });
    if (!response.ok) {
      throw new Error('client chat load failed');
    }
    const payload = await response.json();
    titleNode.textContent = `Карточка клиента: ${payload.client.name}`;
    const metaParts = [];
    if (payload.client.email) metaParts.push(payload.client.email);
    if (payload.client.phone) metaParts.push(payload.client.phone);
    metaNode.textContent = metaParts.join(' • ');
    renderOverview(payload);
    renderClientActions(payload);
  };

  const closeChat = () => {
    setChatMode(false);
    activeClientId = '';
    clearStoredClientId();
  };

  const openChatByClientId = async (clientId, clientName = 'Клиент') => {
    if (!clientId) return;
    activeClientId = String(clientId);
    storeClientId(activeClientId);
    titleNode.textContent = `Карточка клиента: ${clientName}`;
    metaNode.textContent = '';
    setChatMode(true);
    try {
      await loadChat(activeClientId);
    } catch (error) {
      overviewClientRequests.innerHTML = '<div class="workspace-list__empty">Не удалось загрузить карточку клиента.</div>';
    }
  };

  openButtons.forEach((button) => {
    button.addEventListener('click', async () => {
      await openChatByClientId(button.dataset.clientId || '', button.dataset.clientName || 'Клиент');
    });
  });

  backButton.addEventListener('click', () => {
    closeChat();
  });

  const initialClientId = shell.dataset.initialChatClientId || '';
  setChatMode(Boolean(initialClientId));
  if (initialClientId) {
    openChatByClientId(initialClientId);
  }
})();

(function () {
  const modal = document.getElementById('client-document-delete-modal');
  const list = document.querySelector('[data-documents-list]');
  const forms = document.querySelectorAll('[data-document-delete-form]');
  if (!modal || !list || !forms.length) return;

  const closeButtons = modal.querySelectorAll('[data-document-delete-close]');
  const confirmButton = modal.querySelector('[data-document-delete-confirm]');
  let activeForm = null;

  const closeModal = () => {
    modal.hidden = true;
    document.body.classList.remove('is-modal-open');
    activeForm = null;
  };

  const openModal = (form) => {
    activeForm = form;
    modal.hidden = false;
    document.body.classList.add('is-modal-open');
  };

  forms.forEach((form) => {
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      openModal(form);
    });
  });

  closeButtons.forEach((button) => button.addEventListener('click', closeModal));

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !modal.hidden) {
      closeModal();
    }
  });

  confirmButton?.addEventListener('click', async () => {
    if (!activeForm) return;

    try {
      const response = await fetch(activeForm.action, {
        method: 'POST',
        headers: {
          'X-Requested-With': 'XMLHttpRequest',
          Accept: 'application/json',
        },
      });
      if (!response.ok) {
        throw new Error('delete failed');
      }
      const row = activeForm.closest('[data-document-row-id]');
      if (row) {
        row.remove();
      }
      closeModal();
    } catch (error) {
      window.location.reload();
    }
  });
})();

