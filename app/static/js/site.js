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
  const forms = document.querySelectorAll('.js-status-form');
  if (!forms.length) return;

  forms.forEach((form) => {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();

      const submitButton = form.querySelector('button[type="submit"]');
      const row = form.closest('[data-status-row]') || form.closest('tr') || form.closest('.ops-task-card');
      const labelCell = row ? row.querySelector('[data-status-label]') : null;
      const formData = new FormData(form);
      const originalText = submitButton ? submitButton.textContent : '';

      if (submitButton) {
        submitButton.disabled = true;
        submitButton.textContent = 'РЎРѕС…СЂР°РЅСЏРµРј...';
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
          submitButton.textContent = 'РЎРѕС…СЂР°РЅРµРЅРѕ';
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
        window.alert('РќРµ СѓРґР°Р»РѕСЃСЊ РѕР±РЅРѕРІРёС‚СЊ СЃС‚Р°С‚СѓСЃ. РџРѕРїСЂРѕР±СѓР№С‚Рµ РµС‰Рµ СЂР°Р·.');
      }
    });
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
      window.alert('РќРµ СѓРґР°Р»РѕСЃСЊ РїРµСЂРµРјРµСЃС‚РёС‚СЊ РґРµР»Рѕ. РџРѕРїСЂРѕР±СѓР№С‚Рµ РµС‰Рµ СЂР°Р·.');
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
        button.textContent = 'РЎРѕС…СЂР°РЅСЏРµРј...';
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
          statusCell.textContent = 'РџСЂРѕС‡РёС‚Р°РЅРѕ';
        }
        if (actionCell) {
          actionCell.innerHTML = '<span class="notification-item__hint">РЈР¶Рµ РїСЂРѕСЃРјРѕС‚СЂРµРЅРѕ</span>';
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
        window.alert('РќРµ СѓРґР°Р»РѕСЃСЊ РѕС‚РјРµС‚РёС‚СЊ СѓРІРµРґРѕРјР»РµРЅРёРµ РєР°Рє РїСЂРѕС‡РёС‚Р°РЅРЅРѕРµ.');
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
  const overviewClientType = document.getElementById('overview-client-type');
  const overviewOrganization = document.getElementById('overview-client-organization');
  const overviewAddress = document.getElementById('overview-client-address');
  const overviewProblem = document.getElementById('overview-client-problem');
  const overviewNotes = document.getElementById('overview-client-notes');
  const overviewPassport = document.getElementById('overview-client-passport');
  const overviewRequisites = document.getElementById('overview-client-requisites');
  const overviewCases = document.getElementById('overview-recent-cases');
  const overviewTasks = document.getElementById('overview-upcoming-tasks');
  const overviewDocuments = document.getElementById('overview-recent-documents');
  const overviewManageCases = document.getElementById('overview-manage-cases');
  const overviewTaskCaseSelect = document.getElementById('overview-task-case-select');
  const overviewNewCaseClientId = document.getElementById('overview-new-case-client-id');
  const overviewNewCaseReturnTo = document.getElementById('overview-new-case-return-to');
  const overviewNewTaskReturnTo = document.getElementById('overview-new-task-return-to');
  const overviewTaskForm = overviewTaskCaseSelect ? overviewTaskCaseSelect.closest('form') : null;
  const messageInput = form ? form.querySelector('textarea[name="message"]') : null;
  if (!directory || !chatPage || !backButton || !form || !thread || !titleNode || !metaNode) return;

  const ACTIVE_CHAT_STORAGE_KEY = 'clients-active-chat-id';
  const CHAT_DRAFT_STORAGE_PREFIX = 'clients-chat-draft:';
  let activeClientId = '';

  const readStoredDraft = (clientId) => {
    if (!clientId) return '';
    try {
      return window.localStorage.getItem(`${CHAT_DRAFT_STORAGE_PREFIX}${clientId}`) || '';
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
      empty.textContent = 'РЎРѕРѕР±С‰РµРЅРёР№ РїРѕРєР° РЅРµС‚.';
      thread.appendChild(empty);
      return;
    }

    messages.forEach((item) => {
      const card = document.createElement('article');
      card.className = `client-chat-item ${item.is_from_client ? 'client-chat-item--client' : 'client-chat-item--staff'}`;

      const head = document.createElement('div');
      head.className = 'client-chat-item__meta';
      head.textContent = `${item.author} вЂў ${item.created_at}`;

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
      empty.textContent = 'РќРµС‚ РґР°РЅРЅС‹С…';
      container.appendChild(empty);
      return;
    }

    items.forEach((item) => {
      const li = document.createElement('li');
      li.textContent = renderText(item);
      container.appendChild(li);
    });
  };

  const renderOverview = (payload) => {
    const overview = payload.overview || {};
    if (overviewCasesTotal) overviewCasesTotal.textContent = String(overview.cases_total || 0);
    if (overviewCasesActive) overviewCasesActive.textContent = String(overview.cases_active || 0);
    if (overviewTasksActive) overviewTasksActive.textContent = String(overview.tasks_active || 0);
    if (overviewTasksOverdue) overviewTasksOverdue.textContent = String(overview.tasks_overdue || 0);

    if (overviewClientType) {
      overviewClientType.textContent = payload.client.client_type === 'ORGANIZATION' ? 'Р®СЂР»РёС†Рѕ' : 'Р¤РёР·Р»РёС†Рѕ';
    }
    if (overviewOrganization) overviewOrganization.textContent = payload.client.organization_name || '-';
    if (overviewAddress) overviewAddress.textContent = payload.client.address || '-';
    if (overviewProblem) overviewProblem.textContent = payload.client.problem_summary || '-';
    if (overviewNotes) overviewNotes.textContent = payload.client.notes || '-';
    if (overviewPassport) overviewPassport.textContent = payload.client.passport_details || '-';
    if (overviewRequisites) overviewRequisites.textContent = payload.client.organization_requisites || '-';

    renderOverviewList(
      overviewCases,
      overview.recent_cases || [],
      (item) => {
        const specialization = item.responsible_lawyer_specialization ? `, ${item.responsible_lawyer_specialization}` : '';
        return `${item.case_number}: ${item.title} (${item.stage}, ${item.deadline}) вЂў ${item.responsible_lawyer_name}${specialization}`;
      },
    );
    renderOverviewList(
      overviewTasks,
      overview.upcoming_tasks || [],
      (item) => `${item.due_date} вЂў ${item.case_number} вЂў ${item.title} (${item.status})`,
    );
    renderOverviewList(
      overviewDocuments,
      overview.recent_documents || [],
      (item) => `${item.created_at} вЂў ${item.case_number} вЂў ${item.name}`,
    );
  };

  const renderClientActions = (payload) => {
    const actions = payload.actions || {};
    const returnTo = actions.return_to || '/clients';
    const caseOptions = actions.case_options || [];
    const cases = actions.cases || [];

    if (overviewNewCaseClientId) overviewNewCaseClientId.value = String(payload.client.id || '');
    if (overviewNewCaseReturnTo) overviewNewCaseReturnTo.value = returnTo;
    if (overviewNewTaskReturnTo) overviewNewTaskReturnTo.value = returnTo;

    if (overviewTaskCaseSelect) {
      overviewTaskCaseSelect.innerHTML = '';
      const placeholder = document.createElement('option');
      placeholder.value = '';
      placeholder.textContent = 'Р’С‹Р±РµСЂРёС‚Рµ РґРµР»Рѕ';
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

    if (!overviewManageCases) return;
    overviewManageCases.innerHTML = '';
    if (!cases.length) {
      const empty = document.createElement('li');
      empty.className = 'workspace-list__empty';
      empty.textContent = 'РќРµС‚ РґРµР» РґР»СЏ СЂРµРґР°РєС‚РёСЂРѕРІР°РЅРёСЏ';
      overviewManageCases.appendChild(empty);
      return;
    }

    const priorities = [
      { value: 'LOW', label: 'РќРёР·РєРёР№' },
      { value: 'MEDIUM', label: 'РЎСЂРµРґРЅРёР№' },
      { value: 'HIGH', label: 'Р’С‹СЃРѕРєРёР№' },
    ];

    cases.forEach((item) => {
      const listItem = document.createElement('li');

      const head = document.createElement('div');
      head.className = 'ops-inline-meta';
      head.textContent = `${item.case_number} вЂў ${item.title}`;

      const note = document.createElement('div');
      note.className = 'ops-secondary';
      const lawyerInfo = item.responsible_lawyer_specialization
        ? `${item.responsible_lawyer_name} (${item.responsible_lawyer_specialization})`
        : item.responsible_lawyer_name;
      note.textContent = `РљР°С‚РµРіРѕСЂРёСЏ: ${item.category || '-'} вЂў Р”РµРґР»Р°Р№РЅ: ${item.deadline || 'Р‘РµР· РґРµРґР»Р°Р№РЅР°'} вЂў РћС‚РІРµС‚СЃС‚РІРµРЅРЅС‹Р№: ${lawyerInfo}`;

      const formEl = document.createElement('form');
      formEl.method = 'post';
      formEl.action = `/cases/${item.id}/edit`;
      formEl.className = 'ops-inline-form';

      const hiddenFields = [
        ['title', item.title || ''],
        ['category', item.category || 'РћР±С‰РµРµ'],
        ['description', item.description || ''],
        ['deadline', item.deadline_input || ''],
        ['responsible_lawyer_id', String(item.responsible_lawyer_id || '')],
        ['return_to', returnTo],
      ];
      hiddenFields.forEach(([name, value]) => {
        const input = document.createElement('input');
        input.type = 'hidden';
        input.name = name;
        input.value = value;
        formEl.appendChild(input);
      });

      const prioritySelect = document.createElement('select');
      prioritySelect.name = 'priority';
      priorities.forEach((priority) => {
        const option = document.createElement('option');
        option.value = priority.value;
        option.textContent = priority.label;
        if ((item.priority || 'MEDIUM') === priority.value) option.selected = true;
        prioritySelect.appendChild(option);
      });
      formEl.appendChild(prioritySelect);

      const saveButton = document.createElement('button');
      saveButton.type = 'submit';
      saveButton.textContent = 'РЎРѕС…СЂР°РЅРёС‚СЊ РїСЂРёРѕСЂРёС‚РµС‚';
      formEl.appendChild(saveButton);

      const openLink = document.createElement('a');
      openLink.className = 'portal-button portal-button--ghost';
      openLink.href = `/cases/${item.id}`;
      openLink.textContent = 'РћС‚РєСЂС‹С‚СЊ РґРµР»Рѕ';

      listItem.appendChild(head);
      listItem.appendChild(note);
      listItem.appendChild(formEl);
      listItem.appendChild(openLink);
      overviewManageCases.appendChild(listItem);
    });
  };

  const loadChat = async (clientId) => {
    thread.innerHTML = '<p class="workspace-list__empty">Р—Р°РіСЂСѓР¶Р°РµРј РїРµСЂРµРїРёСЃРєСѓ...</p>';
    const response = await fetch(`/clients/${clientId}/chat`, {
      headers: {
        'X-Requested-With': 'XMLHttpRequest',
      },
    });
    if (!response.ok) {
      throw new Error('client chat load failed');
    }
    const payload = await response.json();
    titleNode.textContent = `Р§Р°С‚: ${payload.client.name}`;
    const metaParts = [];
    if (payload.client.email) metaParts.push(payload.client.email);
    if (payload.client.phone) metaParts.push(payload.client.phone);
    metaNode.textContent = metaParts.join(' вЂў ');
    renderMessages(payload.messages || []);
    renderOverview(payload);
    renderClientActions(payload);
  };

  const closeChat = () => {
    setChatMode(false);
    form.reset();
    activeClientId = '';
    clearStoredClientId();
  };

  const openChatByClientId = async (clientId, clientName = 'РљР»РёРµРЅС‚') => {
    if (!clientId) return;
    activeClientId = String(clientId);
    storeClientId(activeClientId);
    titleNode.textContent = `Р§Р°С‚: ${clientName}`;
    metaNode.textContent = '';
    form.action = `/clients/${activeClientId}/chat`;
    setChatMode(true);
    if (messageInput) {
      messageInput.value = readStoredDraft(activeClientId);
    }
    try {
      await loadChat(activeClientId);
    } catch (error) {
      thread.innerHTML = '<p class="workspace-list__empty">РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ С‡Р°С‚.</p>';
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
      await openChatByClientId(button.dataset.clientId || '', button.dataset.clientName || 'РљР»РёРµРЅС‚');
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
      window.alert('Р’РІРµРґРёС‚Рµ С‚РµРєСЃС‚ СЃРѕРѕР±С‰РµРЅРёСЏ.');
      return;
    }
    if (!formData.get('is_from_client')) {
      formData.set('is_from_client', 'false');
    }

    if (submitButton) {
      submitButton.disabled = true;
      submitButton.textContent = 'РћС‚РїСЂР°РІР»СЏРµРј...';
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
      renderClientActions(payload);
      clearDraft(activeClientId);
      form.reset();
    } catch (error) {
      window.alert('РќРµ СѓРґР°Р»РѕСЃСЊ РѕС‚РїСЂР°РІРёС‚СЊ СЃРѕРѕР±С‰РµРЅРёРµ. РџРѕРїСЂРѕР±СѓР№С‚Рµ РµС‰Рµ СЂР°Р·.');
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

