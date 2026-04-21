(function () {
  const roots = document.querySelectorAll('[data-view-root]');
  if (!roots.length) return;

  roots.forEach((root) => {
    const buttons = Array.from(root.querySelectorAll('[data-view-button]'));
    const panels = Array.from(root.querySelectorAll('[data-view-panel]'));
    if (!buttons.length || !panels.length) return;

    const applyView = (viewName) => {
      buttons.forEach((button) => {
        const active = button.dataset.viewButton === viewName;
        button.classList.toggle('is-active', active);
        button.setAttribute('aria-pressed', String(active));
      });

      panels.forEach((panel) => {
        panel.hidden = panel.dataset.viewPanel !== viewName;
      });
    };

    buttons.forEach((button) => {
      button.addEventListener('click', () => {
        applyView(button.dataset.viewButton || panels[0].dataset.viewPanel || '');
      });
    });

    applyView(root.dataset.defaultView || buttons[0].dataset.viewButton || panels[0].dataset.viewPanel || '');
  });
})();

(function () {
  const roots = document.querySelectorAll('[data-list-filter-root]');
  if (!roots.length) return;

  roots.forEach((root) => {
    const items = Array.from(root.querySelectorAll('[data-filter-item]'));
    const searchInput = root.querySelector('[data-filter-search]');
    const selects = Array.from(root.querySelectorAll('[data-filter-select]'));
    const counters = Array.from(root.querySelectorAll('[data-filter-counter]'));

    if (!items.length) return;

    const normalize = (value) => String(value || '').trim().toLowerCase();

    const apply = () => {
      const searchTerm = normalize(searchInput ? searchInput.value : '');
      let visibleCount = 0;

      items.forEach((item) => {
        const haystack = normalize(item.dataset.search || item.textContent || '');
        const matchesSearch = !searchTerm || haystack.includes(searchTerm);

        const matchesSelects = selects.every((select) => {
          const key = select.dataset.filterSelect || '';
          const expected = normalize(select.value);
          if (!key || !expected) return true;
          return normalize(item.dataset[key]) === expected;
        });

        const isVisible = matchesSearch && matchesSelects;
        item.classList.toggle('is-hidden-by-filter', !isVisible);
        item.hidden = !isVisible;
        if (isVisible) visibleCount += 1;
      });

      counters.forEach((counter) => {
        counter.textContent = String(visibleCount);
      });
    };

    searchInput?.addEventListener('input', apply);
    selects.forEach((select) => select.addEventListener('change', apply));
    apply();
  });
})();
