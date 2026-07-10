/* Sparky Requests mini app */
(function () {
  'use strict';

  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
  }

  const STATUS_LABELS = {
    available: { text: '✅ Available', cls: 'badge-available' },
    partially_available: { text: '✅ Available', cls: 'badge-available' },
    approved: { text: '🗓️ Uploads when available', cls: 'badge-approved' },
    requested: { text: '⏳ Requested', cls: 'badge-requested' },
    denied: { text: '❌ Not available', cls: 'badge-denied' },
  };

  const el = {
    tabs: document.querySelectorAll('.type-tab'),
    input: document.getElementById('search-input'),
    searchBtn: document.getElementById('search-btn'),
    hint: document.getElementById('search-hint'),
    empty: document.getElementById('empty-state'),
    loading: document.getElementById('loading-state'),
    loadingText: document.getElementById('loading-text'),
    errorState: document.getElementById('error-state'),
    errorMessage: document.getElementById('error-message'),
    retryBtn: document.getElementById('retry-btn'),
    results: document.getElementById('results'),
    resultsCount: document.getElementById('results-count'),
    resultsList: document.getElementById('results-list'),
    toast: document.getElementById('toast'),
  };

  let currentType = 'movie';
  let lastQuery = '';
  let toastTimer = null;

  // ---------- helpers ----------

  function haptic(kind) {
    try {
      if (!tg || !tg.HapticFeedback) return;
      if (kind === 'success' || kind === 'error' || kind === 'warning') {
        tg.HapticFeedback.notificationOccurred(kind);
      } else {
        tg.HapticFeedback.impactOccurred('light');
      }
    } catch (e) { /* haptics unsupported on some platforms */ }
  }

  function showToast(message) {
    el.toast.textContent = message;
    el.toast.classList.remove('hidden');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.toast.classList.add('hidden'), 2600);
  }

  function setView(view) {
    el.empty.classList.toggle('hidden', view !== 'empty');
    el.loading.classList.toggle('hidden', view !== 'loading');
    el.errorState.classList.toggle('hidden', view !== 'error');
    el.results.classList.toggle('hidden', view !== 'results');
  }

  function showError(message) {
    el.errorMessage.textContent = message;
    setView('error');
    haptic('error');
  }

  async function api(path, options) {
    const res = await fetch(path, Object.assign({
      headers: {
        'Content-Type': 'application/json',
        'X-Telegram-Init-Data': (tg && tg.initData) || '',
      },
    }, options, {
      headers: Object.assign({
        'Content-Type': 'application/json',
        'X-Telegram-Init-Data': (tg && tg.initData) || '',
      }, (options && options.headers) || {}),
    }));
    let data;
    try {
      data = await res.json();
    } catch (e) {
      throw new Error('Unexpected server response. Please try again.');
    }
    if (!res.ok || data.ok === false) {
      const err = new Error(data.error || 'Request failed. Please try again.');
      err.payload = data;
      throw err;
    }
    return data;
  }

  function confirmDialog(message) {
    return new Promise((resolve) => {
      if (tg && tg.showConfirm) {
        try {
          tg.showConfirm(message, resolve);
          return;
        } catch (e) { /* fall through to window.confirm */ }
      }
      resolve(window.confirm(message));
    });
  }

  // ---------- rendering ----------

  // 'YYYY-MM-DD' -> '31 Mar' (or '31 Mar 2027' when it isn't this year)
  function formatExpected(iso) {
    const parts = iso.split('-');
    const date = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
    if (isNaN(date.getTime())) return '';
    const opts = { day: 'numeric', month: 'short' };
    if (date.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
    return date.toLocaleDateString(undefined, opts);
  }

  function renderBadge(status, expectedDate) {
    const info = STATUS_LABELS[status];
    if (!info) return '';
    let text = info.text;
    if (status === 'approved' && expectedDate) {
      const when = formatExpected(expectedDate);
      if (when) text = '🗓️ Expected ' + when;
    }
    return '<span class="badge ' + info.cls + '">' + escapeHtml(text) + '</span>';
  }

  function escapeHtml(s) {
    const div = document.createElement('div');
    div.textContent = s == null ? '' : String(s);
    return div.innerHTML;
  }

  function renderCard(item, index) {
    const card = document.createElement('div');
    card.className = 'card';
    card.dataset.id = item.id;
    card.dataset.index = index;

    const typeEmoji = item.type === 'movie' ? '🎬' : '📺';
    const posterHtml = item.poster
      ? '<img class="poster" src="' + escapeHtml(item.poster) + '" alt="" loading="lazy" onerror="this.outerHTML=\'<div class=poster>' + typeEmoji + '</div>\'">'
      : '<div class="poster">' + typeEmoji + '</div>';

    card.innerHTML =
      posterHtml +
      '<div class="card-body">' +
        '<div class="card-title">' + escapeHtml(item.title) +
          (item.year ? ' <span class="year">(' + item.year + ')</span>' : '') +
        '</div>' +
        '<div class="card-meta">' +
          (item.rating ? '<span class="rating">⭐ ' + item.rating.toFixed(1) + '</span>' : '') +
          renderBadge(item.status, item.expectedDate) +
        '</div>' +
        '<p class="overview">' + escapeHtml(item.overview) + '</p>' +
        '<div class="card-actions">' +
          (item.canRequest ? '<button class="btn btn-request">Request</button>' : '') +
          (item.imdbUrl ? '<a class="imdb-link" href="' + escapeHtml(item.imdbUrl) + '" target="_blank" rel="noopener">IMDb ↗</a>' : '<span class="imdb-slot"></span>') +
        '</div>' +
      '</div>';

    // Tap card to expand/collapse the overview (and lazily fetch IMDb link)
    card.addEventListener('click', function (e) {
      if (e.target.closest('.btn-request') || e.target.closest('a')) return;
      const expanding = !card.classList.contains('expanded');
      card.classList.toggle('expanded');
      if (expanding) {
        haptic('light');
        loadDetails(card, item);
      }
    });

    const requestBtn = card.querySelector('.btn-request');
    if (requestBtn) {
      requestBtn.addEventListener('click', function () {
        requestItem(item, card, requestBtn);
      });
    }

    return card;
  }

  function renderResults(results) {
    el.resultsList.innerHTML = '';
    results.forEach(function (item, i) {
      el.resultsList.appendChild(renderCard(item, i));
    });
    const noun = currentType === 'movie' ? 'movie' : 'TV show';
    el.resultsCount.textContent = results.length + ' ' + noun + (results.length === 1 ? '' : 's') + ' found for "' + lastQuery + '"';
    setView('results');
  }

  // ---------- actions ----------

  async function search() {
    const query = el.input.value.trim();
    if (!query) {
      el.input.focus();
      return;
    }

    lastQuery = query;
    haptic('light');
    el.input.blur();
    el.loadingText.textContent = query.toLowerCase().includes('imdb.com')
      ? 'Looking up IMDb link…'
      : 'Searching ' + (currentType === 'movie' ? 'movies' : 'TV shows') + '…';
    setView('loading');

    let data;
    try {
      data = await api('/api/search', {
        method: 'POST',
        body: JSON.stringify({ type: currentType, query: query }),
      });
    } catch (e) {
      showError(e.message);
      return;
    }

    if (!data.results.length) {
      showError('No ' + (currentType === 'movie' ? 'movies' : 'TV shows') + ' found for "' + query + '". Try a different title, or check the Movies/TV Shows tab.');
      return;
    }

    renderResults(data.results);
  }

  async function requestItem(item, card, button) {
    haptic('light');
    const ok = await confirmDialog('Request "' + item.title + (item.year ? ' (' + item.year + ')' : '') + '"?');
    if (!ok) return;

    button.disabled = true;
    button.textContent = 'Requesting…';

    try {
      const data = await api('/api/request', {
        method: 'POST',
        body: JSON.stringify({ type: item.type, id: item.id }),
      });
      haptic('success');
      item.canRequest = false;
      item.status = data.autoApproved ? 'approved' : 'requested';
      button.remove();
      const meta = card.querySelector('.card-meta');
      meta.insertAdjacentHTML('beforeend', renderBadge(item.status, item.expectedDate));
      let approvedMsg = '✅ Approved — will upload when available';
      if (item.expectedDate) {
        const when = formatExpected(item.expectedDate);
        if (when) approvedMsg = '✅ Approved — expected ' + when;
      }
      showToast(data.autoApproved ? approvedMsg : '✅ Request submitted — pending approval');
    } catch (e) {
      if (e.payload && e.payload.error === 'already_handled') {
        haptic('warning');
        item.canRequest = false;
        item.status = e.payload.status;
        button.remove();
        card.querySelector('.card-meta').insertAdjacentHTML('beforeend', renderBadge(item.status, item.expectedDate));
        const availableStatuses = ['available', 'partially_available'];
        showToast('This title is already ' + (availableStatuses.includes(e.payload.status) ? 'available' : 'requested') + '.');
      } else {
        haptic('error');
        button.disabled = false;
        button.textContent = 'Request';
        showToast('❌ ' + e.message);
      }
    }
  }

  async function loadDetails(card, item) {
    if (item.imdbUrl || item._detailsLoaded) return;
    item._detailsLoaded = true;
    try {
      const data = await api('/api/details?type=' + item.type + '&id=' + encodeURIComponent(item.id));
      if (data.item && data.item.imdbUrl) {
        item.imdbUrl = data.item.imdbUrl;
        const slot = card.querySelector('.imdb-slot');
        if (slot) {
          slot.outerHTML = '<a class="imdb-link" href="' + escapeHtml(item.imdbUrl) + '" target="_blank" rel="noopener">IMDb ↗</a>';
        }
      }
    } catch (e) { /* details are best-effort; the card already works without them */ }
  }

  // ---------- wiring ----------

  el.tabs.forEach(function (tab) {
    tab.addEventListener('click', function () {
      if (tab.dataset.type === currentType) return;
      haptic('light');
      currentType = tab.dataset.type;
      el.tabs.forEach(function (t) { t.classList.toggle('active', t === tab); });
      el.input.placeholder = currentType === 'movie'
        ? 'Title, title + year, or IMDb link…'
        : 'TV show title or IMDb link…';
      // Clear the previous query so switching tabs starts a fresh search
      el.input.value = '';
      lastQuery = '';
      setView('empty');
    });
  });

  el.searchBtn.addEventListener('click', search);
  el.input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') search();
  });
  el.retryBtn.addEventListener('click', search);

  // Auto-detect pasted IMDb links and search immediately
  el.input.addEventListener('paste', function () {
    setTimeout(function () {
      if (el.input.value.toLowerCase().includes('imdb.com')) search();
    }, 50);
  });
})();
