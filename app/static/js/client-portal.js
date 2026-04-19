(function () {
  const body = document.body;
  const openButton = document.querySelector('[data-client-sidebar-toggle]');
  const sidebar = document.getElementById('client-sidebar');
  const closeButtons = document.querySelectorAll('[data-client-sidebar-close]');
  const desktopQuery = window.matchMedia('(min-width: 1101px)');
  if (!body.classList.contains('client-portal') || !openButton || !closeButtons.length || !sidebar) return;

  const isDesktop = () => desktopQuery.matches;
  const isSidebarOpenMobile = () => body.classList.contains('is-client-sidebar-open');
  const isSidebarCollapsedDesktop = () => body.classList.contains('is-client-sidebar-collapsed');
  const syncControls = () => {
    const expanded = isDesktop() ? String(!isSidebarCollapsedDesktop()) : String(isSidebarOpenMobile());
    openButton.setAttribute('aria-expanded', expanded);
    sidebar.setAttribute('aria-hidden', String(!(expanded === 'true')));
  };
  const closeSidebarMobile = () => {
    body.classList.remove('is-client-sidebar-open');
    syncControls();
  };
  const openSidebarMobile = () => {
    body.classList.add('is-client-sidebar-open');
    syncControls();
  };
  const closeSidebarDesktop = () => {
    body.classList.add('is-client-sidebar-collapsed');
    syncControls();
  };
  const openSidebarDesktop = () => {
    body.classList.remove('is-client-sidebar-collapsed');
    syncControls();
  };
  const toggleSidebar = () => {
    if (isDesktop()) {
      if (isSidebarCollapsedDesktop()) {
        openSidebarDesktop();
        return;
      }
      closeSidebarDesktop();
      return;
    }
    if (isSidebarOpenMobile()) {
      closeSidebarMobile();
      return;
    }
    openSidebarMobile();
  };
  const applyInitialState = () => {
    if (isDesktop()) {
      body.classList.remove('is-client-sidebar-open');
      syncControls();
      return;
    }
    body.classList.remove('is-client-sidebar-collapsed');
    closeSidebarMobile();
  };

  openButton.addEventListener('click', toggleSidebar);
  closeButtons.forEach((button) =>
    button.addEventListener('click', () => {
      if (isDesktop()) {
        closeSidebarDesktop();
        return;
      }
      closeSidebarMobile();
    })
  );
  desktopQuery.addEventListener('change', (event) => {
    if (event.matches) {
      body.classList.remove('is-client-sidebar-open');
      body.classList.remove('is-client-sidebar-collapsed');
      syncControls();
      return;
    }
    body.classList.remove('is-client-sidebar-collapsed');
    closeSidebarMobile();
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      if (isDesktop()) {
        closeSidebarDesktop();
        return;
      }
      closeSidebarMobile();
    }
  });

  applyInitialState();
})();

(function () {
  const zones = document.querySelectorAll('[data-upload-zone]');
  if (!zones.length) return;

  zones.forEach((zone) => {
    const input = zone.querySelector('input[type="file"]');
    const label = zone.querySelector('[data-upload-label]');
    const pending = zone.querySelector('[data-upload-pending]');
    const submitButton = zone.querySelector('[data-upload-submit]');
    if (!input || !label) return;

    const renderPending = (files) => {
      if (!pending) return;
      if (!files || !files.length) {
        pending.hidden = true;
        pending.innerHTML = '';
        return;
      }
      const items = Array.from(files)
        .map((file) => `<li class="upload-zone__pending-item">${file.name}</li>`)
        .join('');
      pending.innerHTML = items;
      pending.hidden = false;
    };

    const updateLabel = (files) => {
      if (!files || !files.length) {
        label.textContent = input.multiple ? 'Файлы не выбраны' : 'Файл не выбран';
        renderPending(files);
        if (submitButton) submitButton.disabled = true;
        return;
      }
      if (files.length === 1) {
        label.textContent = files[0].name;
        renderPending(files);
        if (submitButton) submitButton.disabled = false;
        return;
      }
      label.textContent = `Выбрано файлов: ${files.length}`;
      renderPending(files);
      if (submitButton) submitButton.disabled = false;
    };

    input.addEventListener('change', () => updateLabel(input.files));

    ['dragenter', 'dragover'].forEach((eventName) => {
      zone.addEventListener(eventName, (event) => {
        event.preventDefault();
        zone.classList.add('is-dragover');
      });
    });

    ['dragleave', 'dragend', 'drop'].forEach((eventName) => {
      zone.addEventListener(eventName, (event) => {
        event.preventDefault();
        zone.classList.remove('is-dragover');
      });
    });

    zone.addEventListener('drop', (event) => {
      const files = event.dataTransfer ? event.dataTransfer.files : null;
      if (!files || !files.length) return;
      try {
        input.files = files;
      } catch (error) {
        void error;
      }
      updateLabel(files);
    });
  });
})();

(function () {
  const groups = document.querySelectorAll('[data-filter-group]');
  if (!groups.length) return;

  groups.forEach((group) => {
    const scope = group.closest('.portal-card') || group.parentElement || document;
    const buttons = Array.from(group.querySelectorAll('[data-filter-button]'));
    const items = Array.from(scope.querySelectorAll('[data-filter-item]'));
    if (!buttons.length || !items.length) return;

    const applyFilter = (value) => {
      buttons.forEach((button) => {
        const active = button.dataset.filterButton === value;
        button.classList.toggle('is-active', active);
        button.setAttribute('aria-pressed', String(active));
      });

      items.forEach((item) => {
        const tags = (item.dataset.filterTags || '').split(/\s+/).filter(Boolean);
        item.hidden = value !== 'all' && !tags.includes(value);
      });
    };

    buttons.forEach((button) => {
      button.addEventListener('click', () => applyFilter(button.dataset.filterButton || 'all'));
    });

    applyFilter(group.dataset.filterDefault || buttons[0].dataset.filterButton || 'all');
  });
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

  modal.addEventListener('click', (event) => {
    if (event.target === modal) closeModal();
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !modal.hidden) closeModal();
  });

  confirmButton?.addEventListener('click', async () => {
    if (!activeForm) return;
    const requestUrl = activeForm.getAttribute('action');
    if (!requestUrl) {
      closeModal();
      return;
    }
    confirmButton.disabled = true;
    try {
      const response = await fetch(requestUrl, {
        method: 'POST',
        headers: {
          'X-Requested-With': 'XMLHttpRequest',
          Accept: 'application/json',
        },
      });
      if (!response.ok) {
        throw new Error('delete_failed');
      }
      const row = activeForm.closest('[data-document-row-id]');
      if (row) row.remove();
      if (!list.querySelector('[data-document-row-id]')) {
        list.innerHTML = `
          <div class="portal-empty">
            <div class="portal-empty__icon">
              <svg viewBox="0 0 24 24" aria-hidden="true"><use href="#icon-file"></use></svg>
            </div>
            <h3>Документов пока нет</h3>
            <p>После загрузки здесь появится удобный список файлов с комментариями, датами и быстрым скачиванием.</p>
          </div>
        `;
      }
      closeModal();
    } catch (error) {
      void error;
      window.location.reload();
    } finally {
      confirmButton.disabled = false;
    }
  });
})();
