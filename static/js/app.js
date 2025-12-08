// Cüe-Ligans App Master JS
// All global behaviors, utilities, and page-specific logic

// --- Toast System ---
function showToast(message, type = 'info') {
  let toast = document.getElementById('globalToast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'globalToast';
    toast.className = 'toast align-items-center text-bg-dark border-0 position-fixed bottom-0 end-0 m-4';
    toast.style.zIndex = 1080;
    toast.innerHTML = `<div class="d-flex"><div class="toast-body"></div><button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button></div>`;
    document.body.appendChild(toast);
  }
  toast.querySelector('.toast-body').textContent = message;
  toast.className = `toast align-items-center text-bg-${type} border-0 position-fixed bottom-0 end-0 m-4`;
  const bsToast = bootstrap.Toast.getOrCreateInstance(toast);
  bsToast.show();
}

// --- Navbar Active Link ---
function setActiveNav() {
  const path = window.location.pathname;
  document.querySelectorAll('.navbar-nav .nav-link').forEach(link => {
    if (link.getAttribute('href') === path) {
      link.classList.add('active');
    } else {
      link.classList.remove('active');
    }
  });
}

// --- Table Filter ---
function filterTable(inputSelector, tableSelector) {
  const input = document.querySelector(inputSelector);
  const table = document.querySelector(tableSelector);
  if (!input || !table) return;
  input.addEventListener('input', function() {
    const val = this.value.toLowerCase();
    table.querySelectorAll('tbody tr').forEach(row => {
      row.style.display = row.textContent.toLowerCase().includes(val) ? '' : 'none';
    });
  });
}

// --- Table Sort ---
function sortTableByColumn(table, columnIndex, asc = true) {
  const dir = asc ? 1 : -1;
  const rows = Array.from(table.querySelectorAll('tbody tr'));
  rows.sort((a, b) => {
    const aText = a.children[columnIndex].textContent.trim();
    const bText = b.children[columnIndex].textContent.trim();
    return aText.localeCompare(bText, undefined, {numeric: true}) * dir;
  });
  rows.forEach(row => table.querySelector('tbody').appendChild(row));
}

// --- Chart.js Animations ---
function initCharts() {
  if (window.Chart) {
    Chart.defaults.color = '#f8f9fa';
    Chart.defaults.plugins.legend.labels.color = '#b3b3b3';
    Chart.defaults.plugins.tooltip.backgroundColor = '#222';
    // Redraw on resize
    window.addEventListener('resize', () => {
      document.querySelectorAll('canvas').forEach(c => {
        if (c.chart) c.chart.resize();
      });
    });
  }
}

// --- Accordion ---
function initAccordions() {
  document.querySelectorAll('.accordion').forEach(acc => {
    acc.addEventListener('show.bs.collapse', function(e) {
      acc.querySelectorAll('.accordion-item').forEach(item => {
        item.classList.remove('active');
      });
      e.target.closest('.accordion-item').classList.add('active');
    });
  });
}

// --- Skeleton Loader ---
function showSkeletons(selector) {
  document.querySelectorAll(selector).forEach(el => {
    el.classList.add('skeleton');
  });
}
function hideSkeletons(selector) {
  document.querySelectorAll(selector).forEach(el => {
    el.classList.remove('skeleton');
  });
}

// --- Modal Reset ---
function initModals() {
  document.querySelectorAll('.modal').forEach(modal => {
    modal.addEventListener('hidden.bs.modal', function() {
      this.querySelectorAll('form').forEach(f => f.reset());
    });
  });
}

// --- Smooth Scroll ---
function enableSmoothScroll() {
  document.querySelectorAll('a[href^="#"]').forEach(link => {
    link.addEventListener('click', function(e) {
      const target = document.querySelector(this.getAttribute('href'));
      if (target) {
        e.preventDefault();
        target.scrollIntoView({behavior:'smooth'});
      }
    });
  });
}

// --- Past Session Filters ---
function initPastSessionFilters() {
  const cards = Array.from(document.querySelectorAll('.season-card'));
  if (!cards.length) return;
  const sessionButtons = Array.from(document.querySelectorAll('.past-session-filter'));
  const sessionSelect = document.getElementById('seasonSessionFilter');
  const fmtSelect = document.getElementById('seasonFmtFilter');
  const typeSelect = document.getElementById('seasonTypeFilter');

  function applyFilters(sessionVal = 'all', formatVal = 'all', typeVal = 'all') {
    const formatKey = formatVal.toLowerCase();
    const typeKey = typeVal.toLowerCase();
    cards.forEach(card => {
      const cardSession = (card.dataset.sessionKey || card.dataset.season || '').toLowerCase();
      const cardFormat = (card.dataset.format || '').toLowerCase();
      const statRows = Array.from(card.querySelectorAll('.stat-row'));
      const matchesSession = sessionVal === 'all' || cardSession === sessionVal.toLowerCase();
      const matchesFormat = formatKey === 'all' || cardFormat === formatKey;
      const matchesType = typeKey === 'all' || statRows.some(row => (row.dataset.label || '').toLowerCase() === typeKey);
      card.classList.toggle('d-none', !(matchesSession && matchesFormat && matchesType));
    });
  }

  function refreshState() {
    const sessionVal = sessionSelect ? sessionSelect.value : 'all';
    const formatVal = fmtSelect ? fmtSelect.value : 'all';
    const typeVal = typeSelect ? typeSelect.value : 'all';
    applyFilters(sessionVal, formatVal, typeVal);
    sessionButtons.forEach(btn => {
      btn.classList.toggle('active', btn.dataset.session === sessionVal);
    });
  }

  sessionButtons.forEach(btn => {
    btn.addEventListener('click', () => {
      const sessionVal = btn.dataset.session || 'all';
      if (sessionSelect) {
        sessionSelect.value = sessionVal;
      }
      refreshState();
    });
  });

  if (sessionSelect) sessionSelect.addEventListener('change', refreshState);
  if (fmtSelect) fmtSelect.addEventListener('change', refreshState);
  if (typeSelect) typeSelect.addEventListener('change', refreshState);

  refreshState();
}

// --- Init All ---
document.addEventListener('DOMContentLoaded', function() {
  setActiveNav();
  initCharts();
  initAccordions();
  initModals();
  enableSmoothScroll();
  initPastSessionFilters();
});
