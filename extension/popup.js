const TYPE_LABELS = {
  GENERATE_IMAGE:           'GEN IMAGE',
  REGENERATE_IMAGE:         'REGEN IMAGE',
  EDIT_IMAGE:               'EDIT IMAGE',
  GENERATE_CHARACTER_IMAGE: 'GEN REF',
  REGENERATE_CHARACTER_IMAGE: 'REGEN REF',
  EDIT_CHARACTER_IMAGE:     'EDIT REF',
  GENERATE_VIDEO:           'GEN VIDEO',
  GENERATE_VIDEO_REFS:      'GEN VIDEO FROM REFS',
  UPSCALE_VIDEO:            'UPSCALE VIDEO',
  GEN_IMG:                  'GEN IMAGE',
  GEN_VID:                  'GEN VIDEO',
  GEN_VID_REF:              'GEN VIDEO FROM REFS',
  UPSCALE:                  'UPSCALE VIDEO',
  TRACKING:                 'TRACKING',
  URL_REFRESH:              'URL REFRESH',
};

function formatType(type) {
  if (!type) return '—';
  return TYPE_LABELS[type] || type.slice(0, 12).toUpperCase();
}

function formatTime(iso) {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    const ss = String(d.getSeconds()).padStart(2, '0');
    return `${hh}:${mm}:${ss}`;
  } catch {
    return '—';
  }
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function badgeHtml(status) {
  if (status === 'COMPLETED' || status === 'success') {
    return '<span class="badge badge-ok">&#10003; done</span>';
  } else if (status === 'FAILED' || status === 'failed' || (typeof status === 'number' && status >= 400)) {
    return '<span class="badge badge-fail">&#10007; fail</span>';
  } else if (status === 'PROCESSING') {
    return '<span class="badge badge-proc">&#9203; gen...</span>';
  } else {
    return '<span class="badge badge-proc">&#9203; sent</span>';
  }
}

function renderLog(entries) {
  const list = document.getElementById('log-list');
  const countEl = document.getElementById('log-count');

  if (!entries || entries.length === 0) {
    list.innerHTML = '<div class="log-empty">No requests yet</div>';
    countEl.textContent = '0';
    return;
  }

  countEl.textContent = entries.length;

  list.innerHTML = entries.map((entry, i) => {
    const shortId = entry.id ? String(entry.id).slice(0, 8) : '—';
    const type = formatType(entry.type || entry.method);
    const time = formatTime(entry.time || entry.timestamp);
    const status = entry.status || 'pending';
    const error = entry.error || '';

    const urlDisplay = entry.url
      ? `<div class="detail-section">
           <div class="detail-label">URL</div>
           <div class="detail-value url" title="${escHtml(entry.url)}">${escHtml(entry.url)}</div>
         </div>`
      : '';

    const payloadDisplay = entry.payloadSummary
      ? `<div class="detail-section">
           <div class="detail-label">Payload</div>
           <div class="detail-value">${escHtml(entry.payloadSummary)}</div>
         </div>`
      : '';

    const responseDisplay = entry.responseSummary
      ? `<div class="detail-section">
           <div class="detail-label">Response${entry.httpStatus ? ` (${entry.httpStatus})` : ''}</div>
           <div class="detail-value">${escHtml(entry.responseSummary)}</div>
         </div>`
      : '';

    const errorDisplay = error
      ? `<div class="detail-section">
           <div class="detail-label">Error</div>
           <div class="detail-value detail-error">${escHtml(error)}</div>
         </div>`
      : '';

    const hasDetails = entry.url || entry.payloadSummary || entry.responseSummary || error;

    return `<div class="entry" data-idx="${i}">
      <div class="entry-row">
        <span class="entry-id">${escHtml(shortId)}</span>
        <span class="entry-type">${escHtml(type)}</span>
        <span class="entry-time">${escHtml(time)}</span>
        ${badgeHtml(status)}
        ${hasDetails ? '<span class="expand-icon">&#9654;</span>' : '<span class="expand-icon" style="visibility:hidden">&#9654;</span>'}
      </div>
      ${hasDetails ? `<div class="entry-details">${urlDisplay}${payloadDisplay}${responseDisplay}${errorDisplay}</div>` : ''}
    </div>`;
  }).join('');

  // Toggle expand on row click
  list.querySelectorAll('.entry-row').forEach((row) => {
    row.addEventListener('click', () => {
      const entry = row.closest('.entry');
      if (entry.querySelector('.entry-details')) {
        entry.classList.toggle('open');
      }
    });
  });
}

document.getElementById('btn-panel').addEventListener('click', () => {
  chrome.windows.getCurrent((win) => {
    chrome.sidePanel.open({ windowId: win.id });
  });
});

const settings = document.getElementById('settings');
const remoteEnabled = document.getElementById('remote-enabled');
const controllerUrl = document.getElementById('controller-url');
const workerId = document.getElementById('worker-id');
const workerToken = document.getElementById('worker-token');
const settingsStatus = document.getElementById('settings-status');

function setSettingsStatus(text, kind = '') {
  settingsStatus.textContent = text;
  settingsStatus.className = kind;
}

async function loadRemoteSettings() {
  const { remoteWorker = {} } = await chrome.storage.local.get('remoteWorker');
  remoteEnabled.checked = remoteWorker.enabled === true;
  controllerUrl.value = remoteWorker.controllerUrl || '';
  workerId.value = remoteWorker.workerId || '';
  workerToken.value = '';
  if (remoteWorker.token) workerToken.placeholder = 'Saved token — leave blank to keep';
}

document.getElementById('btn-settings').addEventListener('click', async () => {
  settings.classList.toggle('open');
  if (settings.classList.contains('open')) await loadRemoteSettings();
});

document.getElementById('settings-save').addEventListener('click', async () => {
  const { remoteWorker: existing = {} } = await chrome.storage.local.get('remoteWorker');
  const config = {
    enabled: remoteEnabled.checked,
    controllerUrl: controllerUrl.value.trim().replace(/\/$/, ''),
    workerId: workerId.value.trim(),
    token: workerToken.value.trim() || existing.token || '',
  };
  if (config.enabled && (!/^https:\/\//.test(config.controllerUrl) || !config.token || !config.workerId)) {
    setSettingsStatus('Remote mode needs HTTPS URL, Worker ID, and token.', 'error');
    return;
  }
  try {
    const result = await chrome.runtime.sendMessage({ type: 'SET_REMOTE_WORKER_CONFIG', config });
    if (!result?.ok) throw new Error(result?.error || 'Unable to save settings');
    workerToken.value = '';
    workerToken.placeholder = 'Saved token — leave blank to keep';
    setSettingsStatus(config.enabled ? 'Saved. Reconnecting to FlowCommand…' : 'Saved. Local mode enabled.', 'ok');
  } catch (error) {
    setSettingsStatus(error.message || 'Unable to save settings', 'error');
  }
});

chrome.runtime.sendMessage({ type: 'REQUEST_LOG' }, (data) => {
  if (chrome.runtime.lastError) return;
  if (data && data.log) renderLog(data.log);
});
