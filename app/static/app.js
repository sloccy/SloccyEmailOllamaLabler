// ---- Sync account selects ----
function syncAccountSelects() {
  const src = document.getElementById('prompt-filter-account');
  const accountOptions = [...src.querySelectorAll('option')].slice(1).map(o => o.outerHTML).join('');
  const newPrompt = document.getElementById('new-prompt-account');
  if (newPrompt) newPrompt.innerHTML = '<option value="">All accounts (global)</option>' + accountOptions;
}

// ---- Toast (Bootstrap Toast) ----
function toast(msg, type = 'success') {
  const el = document.getElementById('toast');
  const body = document.getElementById('toast-body');
  el.className = 'toast align-items-center border-0 text-bg-' + (type === 'error' ? 'danger' : type === 'warning' ? 'warning' : 'success');
  body.textContent = msg;
  bootstrap.Toast.getOrCreateInstance(el, { delay: 3500 }).show();
}

// ---- Navigation ----
function setActivePage(page, el) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('[data-page]').forEach(n => n.classList.remove('active'));
  document.getElementById('page-' + page).classList.add('active');
  if (el) el.classList.add('active');
  if (location.hash !== '#' + page) history.replaceState(null, '', '#' + page);
}

window.addEventListener('DOMContentLoaded', () => {
  const page = location.hash.replace('#', '') || 'dashboard';
  const nav = document.querySelector(`[data-page="${page}"]`);
  if (nav) setActivePage(page, nav);
});

window.addEventListener('hashchange', () => {
  const page = location.hash.replace('#', '') || 'dashboard';
  const nav = document.querySelector(`[data-page="${page}"]`);
  if (nav) setActivePage(page, nav);
});

// ---- Export prompts ----
function exportPrompts() {
  const accountId = document.getElementById('prompt-filter-account')?.value || '';
  window.location.href = accountId
    ? `/api/prompts/export?account_id=${accountId}&name=${encodeURIComponent(accountId)}`
    : `/api/prompts/export?name=all`;
}

// ---- Download logs ----
function downloadLogs() {
  const start = document.getElementById('log-dl-start').value;
  const end = document.getElementById('log-dl-end').value;
  if (!start || !end) { toast('Select a start and end time.', 'error'); return; }
  if (start >= end) { toast('Start must be before end.', 'error'); return; }
  const toUTC = s => new Date(s).toISOString().replace('T', ' ').slice(0, 19);
  window.location.href = `/api/logs/download?start=${encodeURIComponent(toUTC(start))}&end=${encodeURIComponent(toUTC(end))}`;
}

// ---- Drag to reorder (SortableJS) ----
let _promptsSortable = null;

document.getElementById('prompts-list').addEventListener('htmx:afterSwap', function() {
  _promptsSortable?.destroy();
  _promptsSortable = new Sortable(this, {
    handle: '.drag-handle',
    draggable: '.card[data-id]',
    animation: 150,
    ghostClass: 'drag-ghost',
    async onEnd() {
      const list = document.getElementById('prompts-list');
      const orderedIds = [...list.querySelectorAll('.card[data-id]')].map(c => parseInt(c.dataset.id));
      const resp = await fetch('/api/prompts/reorder', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ordered_ids: orderedIds }),
      });
      if (!resp.ok) {
        toast('Failed to save order.', 'error');
        const accountId = document.getElementById('prompt-filter-account')?.value || '';
        htmx.ajax('GET', accountId ? `/fragments/prompts?account_id=${accountId}` : '/fragments/prompts', { target: '#prompts-list', swap: 'innerHTML' });
      }
    },
  });
});

// ---- Builder SSE ----
function generatePrompt() {
  const desc = document.getElementById('builder-description').value.trim();
  if (!desc) { toast('Describe the emails first.', 'error'); return; }
  const btn = document.getElementById('btn-generate');
  btn.disabled = true; btn.textContent = 'Generating...'; btn.classList.add('btn-generating');
  document.getElementById('builder-result').style.display = 'block';
  document.getElementById('builder-thinking').textContent = '';
  document.getElementById('builder-instruction').value = '';
  const container = document.getElementById('builder-sse-container');
  container.setAttribute('sse-connect', '/api/prompts/generate-stream?description=' + encodeURIComponent(desc));
  htmx.process(container);
  container._genTimeout = setTimeout(() => {
    const b = document.getElementById('btn-generate');
    if (b.disabled) {
      b.disabled = false; b.textContent = '◆ Generate Instruction'; b.classList.remove('btn-generating');
      document.getElementById('btn-use-prompt').disabled = false;
      toast('Generation timed out. Try again.', 'error');
    }
  }, 120000);
}

document.body.addEventListener('htmx:sseClose', function(e) {
  if (e.detail && e.detail.elt && e.detail.elt.id === 'builder-sse-container') {
    clearTimeout(e.detail.elt._genTimeout);
    const btn = document.getElementById('btn-generate');
    btn.disabled = false; btn.textContent = '◆ Generate Instruction'; btn.classList.remove('btn-generating');
    document.getElementById('btn-use-prompt').disabled = false;
  }
});

function useBuilderInstruction() {
  const instruction = document.getElementById('builder-instruction').value.trim();
  if (!instruction) return;
  const promptsNav = document.querySelector('[data-page="prompts"]');
  setActivePage('prompts', promptsNav);
  const list = document.getElementById('prompts-list');
  const doPopulate = () => {
    const panel = document.getElementById('add-prompt-panel');
    if (panel.classList.contains('d-none')) panel.classList.remove('d-none');
    const ta = document.getElementById('new-prompt-instructions');
    if (ta) { ta.value = instruction; ta.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
  };
  if (list && list.querySelector('.card, .empty')) {
    doPopulate();
  } else {
    list.addEventListener('htmx:afterSwap', function handler() {
      list.removeEventListener('htmx:afterSwap', handler);
      doPopulate();
    });
  }
}

// ---- Copy OAuth URL ----
function copyAuthUrl(btn) {
  const urlBox = btn.previousElementSibling;
  const url = urlBox.dataset.url;
  if (!url) return;
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(url).then(() => toast('Link copied to clipboard.'));
  } else {
    const ta = document.createElement('textarea');
    ta.value = url;
    ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus(); ta.select();
    try { document.execCommand('copy'); toast('Link copied to clipboard.'); }
    catch (e) { toast('Copy failed — select and copy the link manually.', 'error'); }
    document.body.removeChild(ta);
  }
}

// ---- Config import ----
function handleConfigImport(input) {
  const file = input.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  input.value = '';
  fetch('/api/config/import', { method: 'POST', body: fd })
    .then(r => r.json())
    .then(data => {
      const el = document.getElementById('import-result');
      el.style.display = '';
      if (data.error) {
        const err = document.createElement('div');
        err.className = 'small text-danger';
        err.textContent = data.error;
        el.replaceChildren(err);
        return;
      }
      const s = data.summary;
      const ok = document.createElement('div');
      ok.className = 'small text-muted';
      ok.textContent = `Import complete — accounts: +${s.accounts.added} (${s.accounts.skipped} skipped), ` +
        `prompts: +${s.prompts.added} (${s.prompts.skipped} skipped), ` +
        `settings: +${s.settings.added} (${s.settings.skipped} skipped), ` +
        `retention: +${s.retention.added} (${s.retention.skipped} skipped).`;
      el.replaceChildren(ok);
      if (window.htmx) htmx.trigger(document.body, 'showToast',
        { message: 'Configuration imported.', type: 'success' });
    })
    .catch(() => {
      const el = document.getElementById('import-result');
      el.style.display = '';
      const fail = document.createElement('div');
      fail.className = 'small text-danger';
      fail.textContent = 'Import failed. Check the file and try again.';
      el.replaceChildren(fail);
    });
}

// ---- Dashboard poller status ----
document.body.addEventListener('htmx:afterSwap', function(e) {
  if (e.detail.target.id !== 'dashboard-content') return;
  const el = e.detail.target.querySelector('[data-poller-running]');
  if (!el) return;
  const running = JSON.parse(el.dataset.pollerRunning);
  document.getElementById('pollerDot').classList.toggle('active', running);
  document.getElementById('pollerLabel').textContent = running ? 'running' : 'stopped';
});

// ---- HX-Trigger event handlers ----
document.body.addEventListener('showToast', function(e) {
  const { message, type } = e.detail || {};
  if (message) toast(message, type || 'success');
});

document.body.addEventListener('closeOAuthPanel', function() {
  document.getElementById('add-account-panel').classList.add('d-none');
  const step2 = document.getElementById('oauth-step-2-body');
  const title = document.createElement('div'); title.className = 'fw-medium mb-2'; title.textContent = 'Open the link and approve access';
  const hint = document.createElement('div'); hint.className = 'small text-muted mb-2'; hint.textContent = 'Click "Generate Link" first.';
  const wrap = document.createElement('div'); wrap.className = 'flex-grow-1';
  wrap.append(title, hint);
  step2.replaceChildren(wrap);
  document.getElementById('oauth-step-1').classList.remove('done');
});
