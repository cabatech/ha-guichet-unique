(function () {
  'use strict';

  const $service = document.getElementById('filter-service');
  const $q       = document.getElementById('filter-q');
  const $from    = document.getElementById('filter-from');
  const $to      = document.getElementById('filter-to');
  const $rows    = Array.from(document.querySelectorAll('#table-inscriptions tbody tr'));
  const $counter = document.getElementById('counter');

  function apply() {
    const svc   = ($service.value || 'all').toLowerCase();
    const q     = ($q.value || '').toLowerCase().trim();
    const from  = $from.value || '';
    const to    = $to.value || '';
    let visible = 0;

    for (const row of $rows) {
      const dSvc = (row.dataset.service || '').toLowerCase();
      const dQ   = (row.dataset.q || '').toLowerCase();
      const dDay = row.dataset.jour || '';
      const okSvc = (svc === 'all') || (dSvc === svc);
      const okQ   = !q || dQ.indexOf(q) !== -1;
      const okFrom = !from || (dDay >= from);
      const okTo   = !to || (dDay <= to);
      const show = okSvc && okQ && okFrom && okTo;
      row.classList.toggle('hidden', !show);
      if (show) visible += 1;
    }

    $counter.textContent = visible + ' entrée(s)';
  }

  [$service, $q, $from, $to].forEach((el) => {
    if (!el) return;
    el.addEventListener('input', apply);
    el.addEventListener('change', apply);
  });
})();
