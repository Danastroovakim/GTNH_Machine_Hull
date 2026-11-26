// GTNH Machine Hull v7.0 — 100% Local Frontend Controller (zero CDN dependencies)

let appInitialized = false;
let currentView = 'console';
let currentBackupTab = 'mod';
let isServerConfigured = false;

let cmdHistory = [];
let historyIdx = -1;
let statusInFlight = false;
let backupsInFlight = false;
const MAX_LOG_NODES = 1500;

let modBackupsCache = [];
let panelBackupsCache = [];
let clientBackupsCache = [];
let selectedBackupKey = null;

// Pack Updater state
let updaterProfileLoaded = false;
let updaterMode = 'client';
let serverSourceMode = 'zip';
let updaterProtectedConfigs = [];
let updaterServerModItems = [];
let updaterScanValid = false;
let updaterBusy = false;

window.onerror = function (msg, url, line) {
  showToast('JS ошибка: ' + msg + ' (строка ' + line + ')', 'error');
};

function escapeHtml(s) {
  return String(s || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function showToast(text, kind = 'info', duration = 3800) {
  const stack = document.getElementById('toast-stack');
  if (!stack) return;
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = text;
  stack.appendChild(el);
  setTimeout(() => {
    el.style.opacity = '0';
    el.style.transition = 'opacity 0.2s';
    setTimeout(() => el.remove(), 220);
  }, duration);
}

// =========================================================================
// BOOTSTRAP
// =========================================================================
async function bootApplication() {
  if (appInitialized) return;
  if (!window.pywebview || !window.pywebview.api || typeof window.pywebview.api.get_config !== 'function') {
    return;
  }
  appInitialized = true;
  try {
    const meta = await window.pywebview.api.notify_ui_ready();
    document.getElementById('app-ver-pill').textContent = 'v' + (meta.version || '7.0');
    const modeLabel = meta.is_portable ? 'Portable (рядом с программой)' : 'AppData';
    document.getElementById('data-dir-text').textContent = (meta.data_dir || '') + '  •  ' + modeLabel;

    await syncConfigToUI();
    await loadUpdaterProfileToUI();
    checkStatus();
    refreshBackups(true);

    setInterval(checkStatus, 2500);
    setInterval(() => refreshBackups(false), 30000);
  } catch (e) {
    appInitialized = false;
    console.error('Boot error:', e);
  }
}

window.addEventListener('pywebviewready', bootApplication);
setInterval(bootApplication, 250);

document.addEventListener('DOMContentLoaded', () => {
  const cmdInput = document.getElementById('cmd-input');
  if (cmdInput) {
    cmdInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        sendCmd();
      } else if (e.key === 'ArrowUp') {
        if (cmdHistory.length > 0 && historyIdx < cmdHistory.length - 1) {
          historyIdx++;
          cmdInput.value = cmdHistory[cmdHistory.length - 1 - historyIdx];
        }
      } else if (e.key === 'ArrowDown') {
        if (historyIdx > 0) {
          historyIdx--;
          cmdInput.value = cmdHistory[cmdHistory.length - 1 - historyIdx];
        } else {
          historyIdx = -1;
          cmdInput.value = '';
        }
      }
    });
  }

  for (const id of ['upd-new-prot-cfg', 'set-new-prot-cfg']) {
    const el = document.getElementById(id);
    if (el) {
      el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          addProtectedConfig(id);
        }
      });
    }
  }
});

// =========================================================================
// NAVIGATION
// =========================================================================
function switchView(view) {
  currentView = view;
  document.querySelectorAll('.nav-item').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.view === view);
  });
  for (const v of ['console', 'backups', 'updater', 'configs', 'settings']) {
    document.getElementById('view-' + v).classList.toggle('hidden', v !== view);
  }
  if (view === 'backups') refreshBackups(true);
  if (view === 'configs') loadConfigsStudio();
  if (view === 'settings') syncConfigToUI();
  if (view === 'updater' && !updaterProfileLoaded) loadUpdaterProfileToUI();
}

function handleServerPillClick() {
  switchView(isServerConfigured ? 'console' : 'settings');
}

function openClientUpdaterDirect() {
  switchView('updater');
  setUpdaterMode('client', false);
}

function openDataDir() {
  window.pywebview.api.open_data_dir();
}

// =========================================================================
// GLOBAL PROGRESS BAR & UPDATER LIVE PROGRESS
// =========================================================================
window.onTaskProgress = function (info) {
  const gp = document.getElementById('global-progress');
  const gpLabel = document.getElementById('gp-label');
  const gpDetail = document.getElementById('gp-detail');
  const gpPct = document.getElementById('gp-pct');
  const gpBar = document.getElementById('gp-bar');
  const gpDismiss = document.getElementById('gp-dismiss');

  const sbTask = document.getElementById('sidebar-task');
  const sbLabel = document.getElementById('sb-task-label');
  const sbPct = document.getElementById('sb-task-pct');
  const sbBar = document.getElementById('sb-task-bar');

  const pct = typeof info.pct === 'number' ? Math.max(0, Math.min(100, info.pct)) : null;
  const pctStr = pct !== null ? pct + '%' : '...';

  gp.classList.remove('hidden');
  gpLabel.textContent = info.label || 'Выполнение операции';
  gpDetail.textContent = info.detail || '';
  gpPct.textContent = pctStr;
  gpBar.style.width = (pct !== null ? pct : 60) + '%';
  gpBar.className = 'progress-fill' + (info.state === 'done' ? ' ok' : info.state === 'error' ? ' danger' : '');

  sbTask.classList.remove('hidden');
  sbLabel.textContent = info.label || 'Операция';
  sbPct.textContent = pctStr;
  sbBar.style.width = (pct !== null ? pct : 60) + '%';
  sbBar.className = gpBar.className;

  if (info.state === 'done' || info.state === 'error') {
    gpDismiss.classList.remove('hidden');
    if (info.state === 'done') {
      showToast(info.label + (info.detail ? ': ' + info.detail : ''), 'ok');
      setTimeout(() => {
        sbTask.classList.add('hidden');
      }, 4000);
    } else {
      showToast(info.label + ': ' + (info.detail || 'ошибка'), 'error', 6000);
    }
  } else {
    gpDismiss.classList.add('hidden');
  }

  // Mirror inside Pack Updater Step 4 card
  if (info.task === 'updater') {
    const card = document.getElementById('upd-progress-card');
    card.classList.remove('hidden');
    document.getElementById('upd-prog-title').textContent = info.label || 'Обновление...';
    document.getElementById('upd-prog-pct').textContent = pctStr;
    const bar = document.getElementById('upd-prog-bar');
    bar.style.width = (pct !== null ? pct : 60) + '%';
    bar.className = gpBar.className;
    document.getElementById('upd-prog-detail').textContent = info.detail || '';
    updateStepperState(info.state === 'done' ? 4 : 4, info.state === 'done');
  }
};

function hideGlobalProgress() {
  document.getElementById('global-progress').classList.add('hidden');
  document.getElementById('sidebar-task').classList.add('hidden');
}

// =========================================================================
// LOGS & TELEMETRY
// =========================================================================
window.addLogBatch = function (logs) {
  const container = document.getElementById('log-container');
  const updLog = document.getElementById('upd-live-log');
  if (!container) return;

  const onlyWarn = document.getElementById('filter-warn').checked;
  const searchVal = (document.getElementById('log-search').value || '').toLowerCase();
  const isNearBottom = container.scrollHeight - container.scrollTop <= container.clientHeight + 80;

  const fragment = document.createDocumentFragment();
  const updFrag = document.createDocumentFragment();
  let addedUpd = false;

  for (const log of logs) {
    const div = document.createElement('div');
    div.dataset.type = log.type || 'info';
    div.className = 'log-line ' + (log.type || 'info');
    const msgLower = String(log.msg || '').toLowerCase();
    if ((onlyWarn && (log.type === 'info' || log.type === 'mod')) || (searchVal && !msgLower.includes(searchVal))) {
      div.classList.add('hidden');
    }
    div.textContent = log.msg;
    fragment.appendChild(div);

    if (updLog && (log.source === 'UPDATER' || log.source === 'CLIENT-UPD' || log.source === 'SERVER-UPD')) {
      const udiv = document.createElement('div');
      udiv.className = 'log-line ' + (log.type || 'info');
      udiv.textContent = log.msg;
      updFrag.appendChild(udiv);
      addedUpd = true;
    }
  }

  container.appendChild(fragment);
  while (container.childNodes.length > MAX_LOG_NODES) {
    container.removeChild(container.firstChild);
  }
  if (isNearBottom) container.scrollTop = container.scrollHeight;

  if (addedUpd && updLog) {
    updLog.appendChild(updFrag);
    while (updLog.childNodes.length > 250) {
      updLog.removeChild(updLog.firstChild);
    }
    updLog.scrollTop = updLog.scrollHeight;
  }
};

function applyFilter() {
  const onlyWarn = document.getElementById('filter-warn').checked;
  document.getElementById('filter-warn-label').classList.toggle('active', onlyWarn);
  const searchVal = (document.getElementById('log-search').value || '').toLowerCase();
  const nodes = document.getElementById('log-container').children;
  for (const n of nodes) {
    const textMatch = !searchVal || n.textContent.toLowerCase().includes(searchVal);
    const typeMatch = !(onlyWarn && (n.dataset.type === 'info' || n.dataset.type === 'mod'));
    n.classList.toggle('hidden', !(textMatch && typeMatch));
  }
}

function clearLogs() {
  document.getElementById('log-container').innerHTML = '';
}

function copyLogs() {
  const lines = Array.from(document.getElementById('log-container').children)
    .filter((n) => !n.classList.contains('hidden'))
    .map((n) => n.textContent);
  navigator.clipboard.writeText(lines.join('\n'));
  showToast('Логи скопированы в буфер обмена', 'ok', 2200);
}

function changeLogSource() {
  const mode = document.getElementById('log-source-select').value;
  clearLogs();
  window.pywebview.api.set_log_source(mode);
}

async function checkStatus() {
  if (statusInFlight || !window.pywebview || !window.pywebview.api) return;
  statusInFlight = true;
  try {
    const res = await window.pywebview.api.get_status();
    isServerConfigured = Boolean(res.configured);
    document.getElementById('onboarding-banner').classList.toggle('hidden', isServerConfigured);

    const sbDot = document.getElementById('sb-dot');
    const sbName = document.getElementById('sb-server-name');
    const sbState = document.getElementById('sb-status-text');
    const sbWs = document.getElementById('sb-ws-chip');
    const wsBadge = document.getElementById('ws-badge');

    if (!isServerConfigured) {
      sbName.textContent = 'Локальный режим';
      sbState.textContent = 'API не настроен';
      sbDot.className = 'status-dot';
      sbWs.textContent = 'LOCAL';
      sbWs.className = 'ws-chip';
      wsBadge.textContent = 'NO API';
      wsBadge.className = 'ws-chip';
    } else {
      sbName.textContent = res.server_name || 'GTNH Server';
      const st = (res.state || 'offline').toLowerCase();
      const stateRu = { running: 'Запущен', starting: 'Запуск...', stopping: 'Остановка...', offline: 'Остановлен' }[st] || st.toUpperCase();
      sbState.textContent = stateRu;
      sbDot.className = 'status-dot ' + (st === 'running' ? 'running' : (st === 'starting' || st === 'stopping') ? 'transition' : 'offline');

      if (res.ws) {
        sbWs.textContent = 'WS';
        sbWs.className = 'ws-chip live';
        wsBadge.textContent = 'WS LIVE';
        wsBadge.className = 'ws-chip live';
      } else {
        sbWs.textContent = 'POLL';
        sbWs.className = 'ws-chip';
        wsBadge.textContent = 'LOG POLL';
        wsBadge.className = 'ws-chip';
      }
    }

    document.getElementById('stat-tps').textContent = res.tps || '20.0 TPS';
    document.getElementById('stat-lag').textContent = 'Лаг-спайк: ' + (res.lag || '0ms (0t)');
    const pBadge = document.getElementById('stat-players-badge');
    pBadge.textContent = (res.players || 0) + ' онлайн';
    pBadge.title = res.player_list && res.player_list.length ? 'Игроки: ' + res.player_list.join(', ') : 'Нет игроков онлайн';

    document.getElementById('stat-cpu').textContent = res.cpu || '0.0%';
    document.getElementById('stat-cpu-lim').textContent = (res.cpu_limit || 100) + '% max';
    document.getElementById('bar-cpu').style.width = (res.cpu_pct || 0) + '%';

    document.getElementById('stat-ram').textContent = res.ram || '0.0 GiB';
    document.getElementById('stat-ram-lim').textContent = res.ram_limit || '—';
    const ramBar = document.getElementById('bar-ram');
    ramBar.style.width = (res.ram_pct || 0) + '%';
    ramBar.className = 'progress-fill ' + ((res.ram_pct || 0) > 85 ? 'danger' : 'ok');

    document.getElementById('stat-disk').textContent = res.disk || '0.0 GiB';
    document.getElementById('stat-disk-lim').textContent = res.disk_limit || '—';
    const diskBar = document.getElementById('bar-disk');
    diskBar.style.width = (res.disk_pct || 0) + '%';
    diskBar.className = 'progress-fill ' + ((res.disk_pct || 0) > 88 ? 'danger' : '');
  } finally {
    statusInFlight = false;
  }
}

function power(sig) {
  window.pywebview.api.send_power(sig);
  setTimeout(checkStatus, 1400);
}

function confirmKill() {
  if (confirm('Принудительно убить процесс сервера (KILL)? Несохранённые чанки могут быть потеряны.')) {
    power('kill');
  }
}

function insertCmd(cmd, autoSend = false) {
  const input = document.getElementById('cmd-input');
  input.value = cmd;
  input.focus();
  if (autoSend) sendCmd();
}

async function sendCmd() {
  const input = document.getElementById('cmd-input');
  const val = input.value.trim();
  if (!val) return;
  cmdHistory.push(val);
  historyIdx = -1;
  input.value = '';
  await window.pywebview.api.send_command(val);
}

function runDiag() {
  switchView('console');
  window.pywebview.api.run_diagnostics();
  refreshBackups(true);
}

// =========================================================================
// BACKUPS & RESTORE (MOD / PANEL / CLIENT)
// =========================================================================
function switchBackupTab(tab) {
  currentBackupTab = tab;
  for (const t of ['mod', 'panel', 'client']) {
    document.getElementById('btab-' + t).classList.toggle('active', t === tab);
    document.getElementById('backup-bar-' + t).classList.toggle('hidden', t !== tab);
  }
  renderBackups();
}

window.refreshBackups = async function (force = false) {
  if ((backupsInFlight && !force) || !window.pywebview || !window.pywebview.api) return;
  backupsInFlight = true;
  try {
    const [modItems, panelItems, clientItems] = await Promise.all([
      window.pywebview.api.get_mod_backups(),
      window.pywebview.api.get_panel_backups(),
      window.pywebview.api.get_client_backups(''),
    ]);
    modBackupsCache = modItems || [];
    panelBackupsCache = panelItems || [];
    clientBackupsCache = clientItems || [];

    document.getElementById('bcount-mod').textContent = modBackupsCache.length;
    document.getElementById('bcount-panel').textContent = panelBackupsCache.length;
    document.getElementById('bcount-client').textContent = clientBackupsCache.length;
    document.getElementById('nav-backup-count').textContent =
      modBackupsCache.length + panelBackupsCache.length + clientBackupsCache.length;

    renderBackups();
  } finally {
    backupsInFlight = false;
  }
};

function renderBackups() {
  const list = document.getElementById('backups-list');
  if (!list) return;
  list.innerHTML = '';

  if (currentBackupTab === 'mod') {
    if (!isServerConfigured) {
      list.innerHTML = '<div class="empty-state">Подключите сервер Bisquit.Host в разделе «Подключение», чтобы видеть серверные архивы.</div>';
      return;
    }
    if (!modBackupsCache.length) {
      list.innerHTML = '<div class="empty-state">Архивы в /backups не найдены.</div>';
      return;
    }
    modBackupsCache.forEach((b, idx) => {
      const badge = b.is_partial
        ? '<span class="badge badge-warn">Приват / Overlay</span>'
        : '<span class="badge badge-ok">Полный мир</span>';
      const el = document.createElement('div');
      el.className = 'backup-item' + (selectedBackupKey === 'mod:' + b.filepath ? ' selected' : '');
      el.innerHTML = `
        <div class="backup-item-main">
          <div class="backup-item-title">
            <span class="backup-item-name mono" title="${escapeHtml(b.filepath)}">${escapeHtml(b.display_name)}</span>
            ${badge}
          </div>
          <div class="backup-item-meta">
            <span class="mono" style="color:var(--ok);">${escapeHtml(b.size)}</span>
            <span>${escapeHtml(b.pretty_date)} МСК</span>
          </div>
        </div>
        <div class="backup-actions">
          <button class="btn btn-sm" onclick="inspectModIdx(${idx})" title="Рентген-анализ содержимого">Рентген</button>
          <button class="btn btn-sm" onclick="dlModIdx(${idx})" title="Скачать">Скачать</button>
          <button class="btn btn-sm btn-primary" onclick="openRestorePanelMod(${idx})">Откат</button>
          <button class="btn btn-sm btn-ghost" onclick="delModIdx(${idx})" title="Удалить с сервера">&times;</button>
        </div>`;
      list.appendChild(el);
    });
  } else if (currentBackupTab === 'panel') {
    if (!isServerConfigured) {
      list.innerHTML = '<div class="empty-state">Подключите сервер Bisquit.Host, чтобы управлять облачными снапшотами.</div>';
      return;
    }
    if (!panelBackupsCache.length) {
      list.innerHTML = '<div class="empty-state">Облачных снапшотов панели пока нет.</div>';
      return;
    }
    panelBackupsCache.forEach((b, idx) => {
      const statusBadge = b.status === 'ok'
        ? '<span class="badge badge-ok">Готов</span>'
        : b.status === 'pending'
          ? '<span class="badge badge-warn">Создаётся...</span>'
          : '<span class="badge badge-danger">Ошибка</span>';
      const lockLabel = b.locked ? 'Защищён' : 'Замок';
      const el = document.createElement('div');
      el.className = 'backup-item' + (selectedBackupKey === 'panel:' + b.uuid ? ' selected' : '');
      el.innerHTML = `
        <div class="backup-item-main">
          <div class="backup-item-title">
            <span class="backup-item-name">${escapeHtml(b.name)}</span>
            ${statusBadge}
            ${b.locked ? '<span class="badge badge-accent">Locked</span>' : ''}
          </div>
          <div class="backup-item-meta">
            <span class="mono" style="color:var(--info);">${escapeHtml(b.size)}</span>
            <span>${escapeHtml(b.pretty_date)} МСК</span>
          </div>
        </div>
        <div class="backup-actions">
          <button class="btn btn-sm" onclick="toggleLockPanelIdx(${idx})">${lockLabel}</button>
          <button class="btn btn-sm" onclick="dlPanelIdx(${idx})">Скачать</button>
          <button class="btn btn-sm btn-primary" onclick="openRestorePanelCloud(${idx})">Откат</button>
          <button class="btn btn-sm btn-ghost" onclick="delPanelIdx(${idx})">&times;</button>
        </div>`;
      list.appendChild(el);
    });
  } else {
    if (!clientBackupsCache.length) {
      list.innerHTML = '<div class="empty-state">Локальные бэкапы клиента (_gtnh_client_backup_*) не найдены. Убедитесь, что путь к инстансу Prism указан в «Обновлении сборки» или «Подключении».</div>';
      return;
    }
    clientBackupsCache.forEach((b, idx) => {
      const el = document.createElement('div');
      el.className = 'backup-item' + (selectedBackupKey === 'client:' + b.name ? ' selected' : '');
      el.innerHTML = `
        <div class="backup-item-main">
          <div class="backup-item-title">
            <span class="backup-item-name mono">${escapeHtml(b.name)}</span>
            <span class="badge badge-info">${b.mods_count} модов</span>
          </div>
          <div class="backup-item-meta">
            <span class="mono" style="color:var(--ok);">${escapeHtml(b.size)}</span>
            <span>${escapeHtml(b.pretty_date)}</span>
            <span class="faint">${escapeHtml((b.contents || []).join(', '))}</span>
          </div>
        </div>
        <div class="backup-actions">
          <button class="btn btn-sm btn-primary" onclick="openRestoreClientPanel(${idx})">Откатить клиент</button>
          <button class="btn btn-sm btn-ghost" onclick="delClientBackupIdx(${idx})" title="Удалить локальный бэкап">&times;</button>
        </div>`;
      list.appendChild(el);
    });
  }
}

function triggerModBackup() { window.pywebview.api.trigger_mod_backup(); }
function compressWorldNow() { window.pywebview.api.compress_world_archive(); }
function createPanelBackup() { window.pywebview.api.create_panel_backup(); }

function inspectModIdx(idx) {
  const b = modBackupsCache[idx];
  if (!b) return;
  selectedBackupKey = 'mod:' + b.filepath;
  renderBackups();
  const panel = document.getElementById('backup-detail-panel');
  panel.innerHTML = `<div class="empty-state">Выполняется рентген-анализ <span class="mono">${escapeHtml(b.filename)}</span>...</div>`;
  window.pywebview.api.inspect_mod_backup(b.filepath);
}

window.showInspectReport = function (rep) {
  const panel = document.getElementById('backup-detail-panel');
  const lvlBadge = rep.has_level_dat
    ? '<span class="badge badge-ok">level.dat найден</span>'
    : '<span class="badge badge-danger">level.dat ОТСУТСТВУЕТ</span>';
  const vBadge = rep.virtual_read
    ? '<span class="badge badge-info">Мгновенное чтение ZIP</span>'
    : '<span class="badge badge-neutral">Песочница</span>';

  const pDims = rep.personal_dims || [];
  const persBlock = pDims.length
    ? `<div class="callout callout-ok"><strong>PersonalSpace найдены (${pDims.length}):</strong><br>${pDims.map(escapeHtml).join('<br>')}</div>`
    : `<div class="callout callout-warn">Личные измерения PersonalSpace не входят в этот архив. При откате в режиме Overlay текущие личные измерения на сервере сохранятся.</div>`;

  const activeHtml = (rep.active_dimensions || []).map((d) => `<li>${escapeHtml(d)}</li>`).join('');
  const emptyList = rep.empty_dimensions || [];
  const emptyHtml = emptyList.length
    ? `<div class="faint" style="margin-top:6px;font-size:11px;">Пустые измерения без .mca (${emptyList.length}): ${escapeHtml(emptyList.join(', '))}</div>`
    : '';

  panel.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
      <h3 class="card-title">Рентген архива</h3>
      <div style="display:flex;gap:5px;">${lvlBadge}${vBadge}</div>
    </div>
    <div class="detail-block">
      <div class="mono" style="font-weight:600;color:var(--text);word-break:break-all;">${escapeHtml(rep.filename)}</div>
      <div class="muted" style="margin-top:4px;">${escapeHtml(rep.layout)}</div>
      ${rep.su_meta ? `<div class="faint mono" style="margin-top:4px;font-size:10.5px;">${escapeHtml(rep.su_meta)}</div>` : ''}
    </div>
    ${persBlock}
    <div class="detail-block">
      <div class="detail-block-title">Активные измерения (${(rep.active_dimensions || []).length} из ${rep.total_dimensions})</div>
      <ul class="detail-list">${activeHtml}</ul>
      ${emptyHtml}
    </div>
    <div class="detail-block">
      <div class="detail-block-title">Данные модов и приватов (${(rep.extras || []).length})</div>
      <div style="color:var(--ok);font-size:11.5px;line-height:1.5;">${rep.extras && rep.extras.length ? escapeHtml(rep.extras.join(' • ')) : 'Только базовые файлы мира'}</div>
    </div>
    ${rep.dat_files && rep.dat_files.length ? `
    <div class="detail-block">
      <div class="detail-block-title">Ключевые файлы состояния (${rep.dat_files.length})</div>
      <div class="mono" style="color:var(--info);font-size:11px;line-height:1.5;">${escapeHtml(rep.dat_files.join(' • '))}</div>
    </div>` : ''}
  `;
};

function openRestorePanelMod(idx) {
  const b = modBackupsCache[idx];
  if (!b) return;
  selectedBackupKey = 'mod:' + b.filepath;
  renderBackups();
  const panel = document.getElementById('backup-detail-panel');
  const desc = b.is_partial
    ? `Выбран частичный бэкап (${b.filename}, ${b.size}). Принудительно выбран безопасный режим Overlay: обновятся только регионы базы, приваты и карта жил, остальные измерения останутся нетронутыми.`
    : `Выбран полный архив мира (${b.filename}, ${b.size}). Рекомендуется Overlay или полная замена папки World.`;

  panel.innerHTML = `
    <h3 class="card-title">Безопасный откат сервера</h3>
    <div class="detail-block">${escapeHtml(desc)}</div>
    <label class="radio-card">
      <input type="radio" name="rmode" id="rmode-overlay" value="overlay" checked>
      <div>
        <div class="toggle-title" style="color:var(--ok);">Overlay (Рекомендуется)</div>
        <div class="toggle-desc">Накладывает .mca регионы, ServerUtilities и VisualProspecting поверх мира, не удаляя остальные измерения.</div>
      </div>
    </label>
    <label class="radio-card ${b.is_partial ? 'disabled' : ''}">
      <input type="radio" name="rmode" id="rmode-replace" value="replace" ${b.is_partial ? 'disabled' : ''}>
      <div>
        <div class="toggle-title" style="color:var(--danger);">Clean Replace (Полная замена World)</div>
        <div class="toggle-desc">Полностью заменяет папку World содержимым архива (только для полных бэкапов).</div>
      </div>
    </label>
    <label class="toggle-card">
      <input type="checkbox" id="restore-safety-chk" checked>
      <div>
        <div class="toggle-title">Создать страховочный снапшот</div>
        <div class="toggle-desc">Сжать текущий мир перед началом отката</div>
      </div>
    </label>
    <div style="display:flex;gap:8px;margin-top:auto;">
      <button class="btn btn-primary" style="flex:1;justify-content:center;" onclick="execRestoreModIdx(${idx})">Запустить откат</button>
    </div>
  `;
}

function execRestoreModIdx(idx) {
  const b = modBackupsCache[idx];
  if (!b) return;
  const replaceEl = document.getElementById('rmode-replace');
  const mode = replaceEl && replaceEl.checked ? 'replace' : 'overlay';
  const safety = document.getElementById('restore-safety-chk')?.checked ?? true;
  window.pywebview.api.restore_mod_backup(b.filepath, mode, safety);
  showToast('Запущен откат сервера (' + mode.toUpperCase() + ')', 'warn');
}

function openRestorePanelCloud(idx) {
  const b = panelBackupsCache[idx];
  if (!b) return;
  selectedBackupKey = 'panel:' + b.uuid;
  renderBackups();
  const panel = document.getElementById('backup-detail-panel');
  panel.innerHTML = `
    <h3 class="card-title">Откат облачного снапшота</h3>
    <div class="detail-block">
      Сервер будет остановлен и восстановлен из облачного снапшота панели <strong>${escapeHtml(b.name)}</strong> (${escapeHtml(b.size)}, ${escapeHtml(b.pretty_date)} МСК).
    </div>
    <button class="btn btn-primary" onclick="execRestorePanelIdx(${idx})">
      Восстановить снапшот
    </button>
  `;
}

function execRestorePanelIdx(idx) {
  const b = panelBackupsCache[idx];
  if (!b) return;
  window.pywebview.api.restore_panel_backup(b.uuid);
  showToast('Запущено восстановление облачного снапшота', 'warn');
}

function openRestoreClientPanel(idx) {
  const b = clientBackupsCache[idx];
  if (!b) return;
  selectedBackupKey = 'client:' + b.name;
  renderBackups();
  const panel = document.getElementById('backup-detail-panel');
  panel.innerHTML = `
    <h3 class="card-title">Откат клиента Prism Launcher</h3>
    <div class="detail-block">
      <div class="mono" style="font-weight:600;">${escapeHtml(b.name)}</div>
      <div class="muted" style="margin-top:4px;">Размер: ${escapeHtml(b.size)} • Модов: ${b.mods_count} • Дата: ${escapeHtml(b.pretty_date)}</div>
      <div class="faint" style="margin-top:6px;">Состав: ${escapeHtml((b.contents || []).join(', '))}</div>
    </div>
    <div class="callout callout-ok">
      Откат вернёт <code>mods/</code>, <code>config/</code>, <code>resourcepacks/</code>, <code>mmc-pack.json</code>${b.has_patches ? ', <code>patches/</code>' : ''}${b.has_libraries ? ', <code>libraries/</code>' : ''}.<br>
      Ваши миры (<code>saves/</code>), карты (<code>journeymap/</code>, <code>visualprospecting/</code>), <code>options.txt</code> и <code>instance.cfg</code> останутся нетронутыми.
    </div>
    <button class="btn btn-primary" onclick="execRestoreClientIdx(${idx})">Откатить клиент из этой копии</button>
  `;
}

async function execRestoreClientIdx(idx) {
  const b = clientBackupsCache[idx];
  if (!b) return;
  const res = await window.pywebview.api.restore_client_backup(b.name, '');
  if (!res.success && res.error) {
    showToast(res.error, 'error', 5000);
  }
}

function dlModIdx(idx) { const b = modBackupsCache[idx]; if (b) window.pywebview.api.download_mod_backup(b.filepath); }
async function delModIdx(idx) {
  const b = modBackupsCache[idx];
  if (b && confirm('Безвозвратно удалить архив ' + b.filename + ' с сервера?')) {
    await window.pywebview.api.delete_mod_backup(b.filepath);
    refreshBackups(true);
  }
}
function dlPanelIdx(idx) { const b = panelBackupsCache[idx]; if (b) window.pywebview.api.download_panel_backup(b.uuid); }
async function delPanelIdx(idx) {
  const b = panelBackupsCache[idx];
  if (b && confirm('Удалить облачный снапшот ' + b.name + '?')) {
    await window.pywebview.api.delete_panel_backup(b.uuid);
    refreshBackups(true);
  }
}
async function toggleLockPanelIdx(idx) {
  const b = panelBackupsCache[idx];
  if (b) { await window.pywebview.api.toggle_lock_panel_backup(b.uuid); refreshBackups(true); }
}
async function delClientBackupIdx(idx) {
  const b = clientBackupsCache[idx];
  if (b && confirm('Удалить локальный бэкап клиента ' + b.name + ' (' + b.size + ')?')) {
    const res = await window.pywebview.api.delete_client_backup(b.name, '');
    if (res.success) showToast('Локальный бэкап удалён', 'ok');
    refreshBackups(true);
  }
}
window.onRestoreComplete = function () { refreshBackups(true); checkStatus(); };

// =========================================================================
// PACK UPDATER
// =========================================================================
async function loadUpdaterProfileToUI() {
  updaterProfileLoaded = true;
  const prof = await window.pywebview.api.get_updater_profile();
  document.getElementById('upd-prism-dir').value = prof.prism_instance_dir || '';
  document.getElementById('set-prism-dir').value = prof.prism_instance_dir || '';
  document.getElementById('upd-client-zip').value = prof.last_client_zip || '';
  document.getElementById('upd-server-zip').value = prof.last_server_zip || '';
  document.getElementById('upd-server-url').value = prof.last_server_url || '';

  updaterProtectedConfigs = Array.isArray(prof.protected_configs) ? [...prof.protected_configs] : [];
  renderProtectedConfigs();

  // Default to client-only if server API is not configured yet
  const defaultMode = isServerConfigured ? (prof.update_mode || 'both') : 'client';
  setUpdaterMode(defaultMode, false);
  setServerSourceMode(prof.server_source_mode || 'zip', false);
}

function updateStepperState(activeStep, allDone = false) {
  for (let i = 1; i <= 4; i++) {
    const el = document.getElementById('step-' + i);
    if (!el) continue;
    el.classList.remove('active', 'done');
    if (allDone || i < activeStep) el.classList.add('done');
    else if (i === activeStep) el.classList.add('active');
  }
}

function setUpdaterMode(mode, resetScan = true) {
  updaterMode = mode;
  for (const m of ['client', 'both', 'server']) {
    document.getElementById('umode-' + m).classList.toggle('active', m === mode);
  }
  document.getElementById('updater-client-block').classList.toggle('hidden', mode === 'server');
  document.getElementById('updater-server-block').classList.toggle('hidden', mode === 'client');
  if (resetScan) invalidateUpdaterScan();
}

function setServerSourceMode(srcMode, resetScan = true) {
  serverSourceMode = srcMode;
  document.getElementById('usrc-zip').classList.toggle('active', srcMode === 'zip');
  document.getElementById('usrc-url').classList.toggle('active', srcMode === 'url');
  document.getElementById('upd-srv-zip-row').classList.toggle('hidden', srcMode !== 'zip');
  document.getElementById('upd-srv-url-row').classList.toggle('hidden', srcMode !== 'url');
  if (resetScan) invalidateUpdaterScan();
}

function invalidateUpdaterScan() {
  updaterScanValid = false;
  document.getElementById('btn-upd-execute').disabled = true;
  document.getElementById('upd-analysis-section').classList.add('hidden');
  updateStepperState(2);
}

async function discoverPrism(targetPrefix) {
  const box = document.getElementById(targetPrefix + '-prism-discover');
  box.classList.remove('hidden');
  box.innerHTML = '<div style="padding:10px;color:var(--text-secondary);">Поиск инстансов Prism Launcher / MultiMC...</div>';
  const res = await window.pywebview.api.discover_prism_instances();
  const list = (res && res.instances) || [];
  if (!list.length) {
    box.innerHTML = '<div style="padding:10px;color:var(--text-muted);">Инстансы Prism Launcher / MultiMC не найдены автоматически. Укажите папку вручную кнопкой «Выбрать».</div>';
    return;
  }
  box.innerHTML = list.map((inst, idx) => `
    <div class="discover-item" onclick="selectDiscoveredPrism('${targetPrefix}', ${idx})">
      <div style="min-width:0;">
        <div style="font-weight:600;display:flex;align-items:center;gap:6px;">
          <span>${escapeHtml(inst.name)}</span>
          ${inst.is_gtnh ? '<span class="badge badge-accent">GTNH</span>' : ''}
          <span class="badge badge-neutral mono">${escapeHtml(inst.version)}</span>
        </div>
        <div class="mono faint" style="font-size:11px;margin-top:2px;word-break:break-all;">${escapeHtml(inst.path)}</div>
      </div>
      <span class="mono muted" style="font-size:11px;flex-shrink:0;">${inst.mods_count} модов</span>
    </div>
  `).join('');
  window._lastDiscoveredPrism = list;
}

async function selectDiscoveredPrism(targetPrefix, idx) {
  const list = window._lastDiscoveredPrism || [];
  const chosen = list[idx];
  if (!chosen) return;
  document.getElementById('upd-prism-dir').value = chosen.path;
  document.getElementById('set-prism-dir').value = chosen.path;
  document.getElementById(targetPrefix + '-prism-discover').classList.add('hidden');
  await window.pywebview.api.save_updater_profile({ prism_instance_dir: chosen.path });
  invalidateUpdaterScan();
  refreshBackups(true);
  showToast('Выбран инстанс: ' + chosen.name, 'ok');
}

async function browsePrismDir() {
  const res = await window.pywebview.api.browse_prism_dir();
  if (res && res.success && res.path) {
    document.getElementById('upd-prism-dir').value = res.path;
    document.getElementById('set-prism-dir').value = res.path;
    invalidateUpdaterScan();
    refreshBackups(true);
  }
}

async function browsePrismFromSettings() {
  await browsePrismDir();
}

async function browseClientZip() {
  const res = await window.pywebview.api.browse_client_zip();
  if (res && res.success && res.path) {
    document.getElementById('upd-client-zip').value = res.path;
    invalidateUpdaterScan();
  }
}

async function browseServerZip() {
  const res = await window.pywebview.api.browse_server_zip();
  if (res && res.success && res.path) {
    document.getElementById('upd-server-zip').value = res.path;
    invalidateUpdaterScan();
  }
}

function renderProtectedConfigs() {
  for (const boxId of ['upd-prot-cfg-list', 'set-prot-cfg-list']) {
    const box = document.getElementById(boxId);
    if (!box) continue;
    if (!updaterProtectedConfigs.length) {
      box.innerHTML = '<span class="faint" style="font-size:11.5px;">Список пуст — стандартные конфиги обновятся из архива (кастомные конфиги сохраняются через Overlay Merge).</span>';
      continue;
    }
    box.innerHTML = updaterProtectedConfigs.map((cfg, i) => `
      <span class="chip">
        <span>config/${escapeHtml(cfg)}</span>
        <button onclick="removeProtectedConfig(${i})" title="Удалить">&times;</button>
      </span>
    `).join('');
  }
}

async function addProtectedConfig(inputId) {
  const inp = document.getElementById(inputId);
  let val = (inp.value || '').trim().replace(/\\/g, '/').replace(/^\/+/, '');
  if (val.toLowerCase().startsWith('config/')) val = val.slice(7);
  val = val.replace(/\/+$/, '');
  if (!val || val.includes('..')) return;
  if (!updaterProtectedConfigs.includes(val)) {
    updaterProtectedConfigs.push(val);
    await window.pywebview.api.save_updater_profile({ protected_configs: updaterProtectedConfigs });
    renderProtectedConfigs();
  }
  inp.value = '';
}

async function removeProtectedConfig(idx) {
  updaterProtectedConfigs.splice(idx, 1);
  await window.pywebview.api.save_updater_profile({ protected_configs: updaterProtectedConfigs });
  renderProtectedConfigs();
}

async function applyClientVisualPreset() {
  const preset = ['angelica.cfg', 'angelica-options.json', 'shaders.properties', 'chromatictooltips.cfg'];
  let changed = false;
  for (const p of preset) {
    if (!updaterProtectedConfigs.includes(p)) {
      updaterProtectedConfigs.push(p);
      changed = true;
    }
  }
  if (changed) {
    await window.pywebview.api.save_updater_profile({ protected_configs: updaterProtectedConfigs });
    renderProtectedConfigs();
    showToast('Добавлен пресет защиты Angelica / Шейдеры', 'ok');
  }
}

async function analyzePackUpdate() {
  const btn = document.getElementById('btn-upd-scan');
  btn.disabled = true;
  btn.textContent = 'Сканирование архивов и сравнение модов...';
  try {
    const res = await window.pywebview.api.analyze_pack_update({
      mode: updaterMode,
      prism_instance_dir: document.getElementById('upd-prism-dir').value,
      client_zip_path: document.getElementById('upd-client-zip').value,
      server_source_mode: serverSourceMode,
      server_zip_path: document.getElementById('upd-server-zip').value,
      server_zip_url: document.getElementById('upd-server-url').value,
    });
    renderUpdaterAnalysis(res);
  } catch (e) {
    renderUpdaterAnalysis({ success: false, errors: ['Ошибка сканирования: ' + e], warnings: [] });
  } finally {
    btn.disabled = false;
    btn.textContent = 'Сканировать и сравнить файлы';
  }
}

function renderUpdaterAnalysis(rep) {
  const sec = document.getElementById('upd-analysis-section');
  sec.classList.remove('hidden');
  updateStepperState(3);

  const errBox = document.getElementById('upd-errors-box');
  const warnBox = document.getElementById('upd-warnings-box');

  if (rep.errors && rep.errors.length) {
    errBox.classList.remove('hidden');
    errBox.innerHTML = rep.errors.map((e) => `<div>• ${escapeHtml(e)}</div>`).join('');
  } else {
    errBox.classList.add('hidden');
  }

  if (rep.warnings && rep.warnings.length) {
    warnBox.classList.remove('hidden');
    warnBox.innerHTML = rep.warnings.map((w) => `<div>• ${escapeHtml(w)}</div>`).join('');
  } else {
    warnBox.classList.add('hidden');
  }

  const cards = [];
  if (rep.client) {
    const c = rep.client;
    const md = c.mod_diff;
    const lockBadge = c.is_locked
      ? '<span class="badge badge-danger">Клиент запущен!</span>'
      : '<span class="badge badge-ok">Готов к обновлению</span>';
    const updHtml = md.updated_mods.length
      ? `<details class="clean-details"><summary>Обновляемые штатные моды (${md.updated_mods.length})</summary>
           <div class="details-box">${md.updated_mods.map((u) => `<div><span style="color:var(--danger);">${escapeHtml(u.old)}</span> → <span style="color:var(--ok);">${escapeHtml(u.new)}</span></div>`).join('')}</div>
         </details>` : '';
    const clHtml = c.changelog_preview
      ? `<details class="clean-details"><summary>Фрагмент ${escapeHtml(c.target_changelog || 'changelog')}</summary>
           <pre class="details-box" style="white-space:pre-wrap;margin:6px 0 0;">${escapeHtml(c.changelog_preview)}</pre>
         </details>` : '';

    cards.push(`
      <div class="card">
        <div class="card-header" style="margin-bottom:8px;">
          <h3 class="card-title">Клиент Prism Launcher</h3>
          ${lockBadge}
        </div>
        <div class="mono" style="font-size:15px;font-weight:600;">
          <span class="muted">${escapeHtml(c.current_version)}</span>
          <span style="color:var(--accent);margin:0 6px;">→</span>
          <span style="color:var(--ok);">${escapeHtml(c.target_version)}</span>
        </div>
        <div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:6px;">
          <span class="badge badge-warn">${md.updated_mods.length} обнов.</span>
          <span class="badge badge-ok">+${md.new_added_count} новых</span>
          <span class="badge badge-neutral">${md.unchanged_count} без изм.</span>
          <span class="badge badge-info">${md.custom_mods.length} кастом</span>
        </div>
        <div class="faint" style="font-size:11px;margin-top:8px;">
          Ресурспаки: ${c.archive_rp_count} в архиве, ${c.custom_resourcepacks.length} своих • Ассетов в mods/: ${c.mod_asset_count || 0} • patches/libraries: ${c.has_patches || c.has_libraries ? 'да' : 'нет'}
        </div>
        ${updHtml}${clHtml}
      </div>
    `);
  }

  if (rep.server) {
    const s = rep.server;
    const md = s.mod_diff;
    const srcBadge = s.source_mode === 'url'
      ? '<span class="badge badge-warn">/files/pull</span>'
      : '<span class="badge badge-info">/files/upload</span>';
    const updHtml = md.updated_mods.length
      ? `<details class="clean-details"><summary>Обновляемые штатные моды (${md.updated_mods.length})</summary>
           <div class="details-box">${md.updated_mods.map((u) => `<div><span style="color:var(--danger);">${escapeHtml(u.old)}</span> → <span style="color:var(--ok);">${escapeHtml(u.new)}</span></div>`).join('')}</div>
         </details>` : '';
    const clHtml = s.changelog_preview
      ? `<details class="clean-details"><summary>Фрагмент ${escapeHtml(s.target_changelog || 'changelog')}</summary>
           <pre class="details-box" style="white-space:pre-wrap;margin:6px 0 0;">${escapeHtml(s.changelog_preview)}</pre>
         </details>` : '';

    cards.push(`
      <div class="card">
        <div class="card-header" style="margin-bottom:8px;">
          <h3 class="card-title">Сервер Bisquit.Host</h3>
          ${srcBadge}
        </div>
        <div class="mono" style="font-size:15px;font-weight:600;">
          <span class="muted">${escapeHtml(s.current_version)}</span>
          <span style="color:var(--accent);margin:0 6px;">→</span>
          <span style="color:var(--ok);">${escapeHtml(s.target_version)}</span>
        </div>
        <div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:6px;">
          <span class="badge badge-warn">${md.updated_mods.length} обнов.</span>
          <span class="badge badge-ok">+${md.new_added_count} новых</span>
          <span class="badge badge-neutral">${md.unchanged_count} без изм.</span>
          <span class="badge badge-accent">${md.custom_mods.length} кандидатов</span>
        </div>
        <div class="faint" style="font-size:11px;margin-top:8px;">
          java9args.txt: ${s.has_live_java9 ? 'защищён (новый → java9args.txt.new)' : 'будет установлен из архива'} • Авто-откат при сбое включён
        </div>
        ${updHtml}${clHtml}
      </div>
    `);
  }
  document.getElementById('upd-version-summary').innerHTML = cards.join('');

  // Client custom lists
  const cBox = document.getElementById('upd-client-custom-box');
  if (rep.client) {
    cBox.classList.remove('hidden');
    const cMods = rep.client.mod_diff.custom_mods || [];
    const cModsEl = document.getElementById('upd-client-mods-list');
    if (!cMods.length) {
      cModsEl.innerHTML = '<div class="faint" style="padding:6px;">Кастомных клиентских модов не найдено.</div>';
    } else {
      cModsEl.innerHTML = cMods.map((m) => {
        const badges = [];
        if (m.in_profile) badges.push('<span class="badge badge-ok">Профиль</span>');
        if (m.is_disabled) badges.push('<span class="badge badge-neutral">.disabled</span>');
        else if (!m.in_profile) badges.push('<span class="badge badge-warn">Кастом</span>');
        if (m.conflict_with) badges.push(`<span class="badge badge-danger" title="В архиве уже есть мод с тем же ModID">В архиве: ${escapeHtml(m.conflict_with)}</span>`);
        return `
          <label class="check-row" data-search="${escapeHtml(m.rel_path.toLowerCase())}">
            <div class="check-row-left">
              <input type="checkbox" class="upd-chk-client-mod" data-rel="${escapeHtml(m.rel_path)}" ${m.checked ? 'checked' : ''}>
              <span class="check-row-name" title="${escapeHtml(m.rel_path)}">${escapeHtml(m.rel_path)}</span>
              ${badges.join('')}
            </div>
            <span class="mono faint" style="font-size:11px;">${escapeHtml(m.size_str)}</span>
          </label>
        `;
      }).join('');
    }

    const cRps = rep.client.custom_resourcepacks || [];
    const cRpEl = document.getElementById('upd-client-rps-list');
    if (!cRps.length) {
      cRpEl.innerHTML = '<div class="faint" style="padding:6px;">Пользовательских ресурспаков не найдено.</div>';
    } else {
      cRpEl.innerHTML = cRps.map((rp) => `
        <label class="check-row">
          <div class="check-row-left">
            <input type="checkbox" class="upd-chk-client-rp" data-name="${escapeHtml(rp.name)}" ${rp.checked ? 'checked' : ''}>
            <span class="check-row-name">${escapeHtml(rp.name)}</span>
            ${rp.in_profile ? '<span class="badge badge-ok">Профиль</span>' : '<span class="badge badge-info">Свой</span>'}
          </div>
          <span class="mono faint" style="font-size:11px;">${escapeHtml(rp.size_str)}</span>
        </label>
      `).join('');
    }
  } else {
    cBox.classList.add('hidden');
  }

  // Server custom mods
  const sBox = document.getElementById('upd-server-custom-box');
  if (rep.server) {
    sBox.classList.remove('hidden');
    updaterServerModItems = (rep.server.mod_diff.custom_mods || []).map((m) => ({ ...m }));
    renderServerCustomModsList();
  } else {
    sBox.classList.add('hidden');
    updaterServerModItems = [];
  }

  const canProceed = Boolean(rep.success && (!rep.errors || rep.errors.length === 0));
  updaterScanValid = canProceed;
  document.getElementById('btn-upd-execute').disabled = !canProceed || updaterBusy;
  document.getElementById('upd-footer-hint').innerHTML = canProceed
    ? '<span style="color:var(--ok);font-weight:600;">Проверка пройдена.</span> Отметьте нужные кастомные файлы и запустите обновление.'
    : '<span style="color:var(--danger);font-weight:600;">Исправьте ошибки выше</span> и повторите сканирование.';

  sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderServerCustomModsList() {
  const el = document.getElementById('upd-server-mods-list');
  if (!updaterServerModItems.length) {
    el.innerHTML = '<div class="faint" style="padding:6px;">Кастомных серверных модов не найдено. Вы можете добавить локальный .jar кнопкой выше.</div>';
    return;
  }
  el.innerHTML = updaterServerModItems.map((m, idx) => {
    const badges = [];
    if (m.in_profile) badges.push('<span class="badge badge-ok">Профиль</span>');
    else if (m.source === 'local_extra') badges.push('<span class="badge badge-info">Локальный → Upload</span>');
    else if (m.unverified) badges.push('<span class="badge badge-neutral">На сервере (без Range)</span>');
    else badges.push('<span class="badge badge-warn">Кастом сервера</span>');
    if (m.is_disabled) badges.push('<span class="badge badge-neutral">.disabled</span>');
    if (m.conflict_with) badges.push(`<span class="badge badge-danger">В архиве: ${escapeHtml(m.conflict_with)}</span>`);
    return `
      <label class="check-row" data-search="${escapeHtml(m.rel_path.toLowerCase())}">
        <div class="check-row-left">
          <input type="checkbox" class="upd-chk-server-mod" data-idx="${idx}" ${m.checked ? 'checked' : ''} onchange="updaterServerModItems[${idx}].checked = this.checked">
          <span class="check-row-name" title="${escapeHtml(m.rel_path)}">${escapeHtml(m.rel_path)}</span>
          ${badges.join('')}
        </div>
        <span class="mono faint" style="font-size:11px;">${escapeHtml(m.size_str || '')}</span>
      </label>
    `;
  }).join('');
}

function filterCheckList(containerId, query) {
  const q = (query || '').trim().toLowerCase();
  const container = document.getElementById(containerId);
  if (!container) return;
  Array.from(container.children).forEach((row) => {
    if (!row.dataset || row.dataset.search === undefined) return;
    row.classList.toggle('hidden', Boolean(q && !row.dataset.search.includes(q)));
  });
}

async function addExtraServerMods() {
  const res = await window.pywebview.api.browse_extra_server_mods();
  if (res && res.success && res.files && res.files.length) {
    for (const f of res.files) {
      const existing = updaterServerModItems.find((x) => x.rel_path.toLowerCase() === f.name.toLowerCase());
      if (existing) {
        existing.local_path = f.path;
        existing.checked = true;
        existing.source = 'local_extra';
      } else {
        updaterServerModItems.push({
          rel_path: f.name,
          name: f.name,
          local_path: f.path,
          size_str: f.size_str,
          in_profile: false,
          is_disabled: false,
          checked: true,
          source: 'local_extra',
          conflict_with: '',
        });
      }
    }
    renderServerCustomModsList();
  }
}

function toggleAllUpdaterChecks(kind, state) {
  if (kind === 'client-mod') {
    document.querySelectorAll('.upd-chk-client-mod').forEach((el) => { el.checked = state; });
  } else if (kind === 'client-rp') {
    document.querySelectorAll('.upd-chk-client-rp').forEach((el) => { el.checked = state; });
  } else if (kind === 'server-mod') {
    updaterServerModItems.forEach((item) => { item.checked = state; });
    document.querySelectorAll('.upd-chk-server-mod').forEach((el) => { el.checked = state; });
  }
}

async function executePackUpdate() {
  if (!updaterScanValid || updaterBusy) return;

  const confirmedClientMods = Array.from(document.querySelectorAll('.upd-chk-client-mod:checked'))
    .map((el) => el.dataset.rel).filter(Boolean);
  const confirmedResourcepacks = Array.from(document.querySelectorAll('.upd-chk-client-rp:checked'))
    .map((el) => el.dataset.name).filter(Boolean);
  const confirmedServerMods = updaterServerModItems
    .filter((item) => item.checked)
    .map((item) => ({ rel_path: item.rel_path, local_path: item.local_path || '' }));

  const payload = {
    mode: updaterMode,
    prism_instance_dir: document.getElementById('upd-prism-dir').value,
    client_zip_path: document.getElementById('upd-client-zip').value,
    server_source_mode: serverSourceMode,
    server_zip_path: document.getElementById('upd-server-zip').value,
    server_zip_url: document.getElementById('upd-server-url').value,
    confirmed_client_mods: confirmedClientMods,
    confirmed_resourcepacks: confirmedResourcepacks,
    confirmed_server_mods: confirmedServerMods,
    protected_configs: updaterProtectedConfigs,
    create_server_safety: document.getElementById('upd-srv-safety').checked,
    auto_start_server: document.getElementById('upd-srv-autostart').checked,
  };

  document.getElementById('upd-live-log').innerHTML = '';
  document.getElementById('upd-progress-card').classList.remove('hidden');
  updateStepperState(4);

  const res = await window.pywebview.api.execute_pack_update(payload);
  if (!res.success && res.error) {
    showToast(res.error, 'error', 5000);
  }
}

window.setUpdaterBusy = function (busy) {
  updaterBusy = busy;
  document.getElementById('nav-updater-busy').classList.toggle('hidden', !busy);
  document.getElementById('btn-upd-execute').disabled = busy || !updaterScanValid;
  document.getElementById('btn-upd-scan').disabled = busy;
};

window.onPackUpdateComplete = function (res) {
  window.setUpdaterBusy(false);
  refreshBackups(true);
  checkStatus();
  if (res && res.ok) {
    showToast('Обновление сборки успешно завершено!', 'ok', 5000);
  }
};

// =========================================================================
// CONFIG STUDIO
// =========================================================================
function switchCfgTab(tab) {
  for (const t of ['su', 'props', 'jvm']) {
    document.getElementById('ctab-' + t).classList.toggle('active', t === tab);
    document.getElementById('cbody-' + t).classList.toggle('hidden', t !== tab);
  }
}

async function loadConfigsStudio() {
  const all = await window.pywebview.api.get_all_configs();
  const configured = Boolean(all && all.configured);
  document.getElementById('cfg-unconfigured').classList.toggle('hidden', configured);
  document.getElementById('cfg-studio-body').classList.toggle('hidden', !configured);
  if (!configured) return;

  if (all.su && all.su.found) {
    document.getElementById('su-enable').checked = all.su.enable_backups;
    document.getElementById('su-chunkload').checked = all.su.chunk_loading;
    document.getElementById('su-claimed').checked = all.su.only_claimed;
    document.getElementById('su-regions').checked = all.su.entire_regions;
    document.getElementById('su-online').checked = all.su.need_online_players;
    document.getElementById('su-timer').value = all.su.backup_timer;
    document.getElementById('su-keep').value = all.su.backups_to_keep;
  }
  if (all.props && all.props.found) {
    document.getElementById('pr-view').value = all.props.view_distance;
    document.getElementById('pr-pause').value = all.props.pause_empty;
    document.getElementById('pr-maxp').value = all.props.max_players;
    document.getElementById('pr-diff').value = String(all.props.difficulty);
    document.getElementById('pr-whitelist').checked = all.props.whitelist;
    document.getElementById('pr-pvp').checked = all.props.pvp;
  }
  if (all.jvm && all.jvm.found) {
    document.getElementById('jvm-text').value = all.jvm.content;
  }
}

function applyG1GCPreset() {
  const ta = document.getElementById('jvm-text');
  const lines = ta.value.split('\n').filter((l) => !l.trim().startsWith('-XX:'));
  const g1 = [
    '-XX:+UseG1GC',
    '-XX:+UnlockExperimentalVMOptions',
    '-XX:MaxGCPauseMillis=100',
    '-XX:G1NewSizePercent=20',
    '-XX:G1ReservePercent=20',
    '-XX:G1HeapRegionSize=32M',
    '-XX:InitiatingHeapOccupancyPercent=35',
    '-XX:+DisableExplicitGC',
    '-XX:+UseStringDeduplication',
  ];
  ta.value = [...lines, ...g1].join('\n');
}

async function saveAllConfigs() {
  const res = await window.pywebview.api.save_all_configs({
    su: {
      enable_backups: document.getElementById('su-enable').checked,
      chunk_loading: document.getElementById('su-chunkload').checked,
      only_claimed: document.getElementById('su-claimed').checked,
      entire_regions: document.getElementById('su-regions').checked,
      need_online_players: document.getElementById('su-online').checked,
      backup_timer: document.getElementById('su-timer').value,
      backups_to_keep: document.getElementById('su-keep').value,
    },
    props: {
      view_distance: document.getElementById('pr-view').value,
      pause_empty: document.getElementById('pr-pause').value,
      max_players: document.getElementById('pr-maxp').value,
      difficulty: document.getElementById('pr-diff').value,
      whitelist: document.getElementById('pr-whitelist').checked,
      pvp: document.getElementById('pr-pvp').checked,
    },
    jvm_content: document.getElementById('jvm-text').value,
  });
  if (res && res.success) showToast('Конфиги успешно сохранены на сервере', 'ok');
  else showToast('Ошибка сохранения: ' + ((res && res.error) || 'неизвестно'), 'error');
}

// =========================================================================
// SETTINGS & CONNECTION
// =========================================================================
async function syncConfigToUI() {
  const cfg = await window.pywebview.api.get_config();
  document.getElementById('cfg-url').value = cfg.panel_url || 'https://mgr.bisquit.host';
  document.getElementById('cfg-key').value = cfg.api_key || '';
  document.getElementById('cfg-id').value = cfg.server_id || '';
  document.getElementById('cfg-max').value = cfg.max_panel_backups || 3;

  isServerConfigured = Boolean(cfg.is_configured);
  const badge = document.getElementById('settings-conn-badge');
  badge.textContent = isServerConfigured ? 'Подключение настроено' : 'Не настроено';
  badge.className = 'badge ' + (isServerConfigured ? 'badge-ok' : 'badge-neutral');
}

function toggleApiKeyVis() {
  const inp = document.getElementById('cfg-key');
  inp.type = inp.type === 'password' ? 'text' : 'password';
}

async function testConnection() {
  const btn = document.getElementById('btn-test-conn');
  const box = document.getElementById('conn-test-result');
  btn.disabled = true;
  btn.textContent = 'Проверка...';
  box.className = 'callout callout-info';
  box.textContent = 'Отправка read-only запроса к Bisquit.Host API...';
  try {
    const res = await window.pywebview.api.test_connection(
      document.getElementById('cfg-url').value,
      document.getElementById('cfg-key').value,
      document.getElementById('cfg-id').value
    );
    if (res && res.success) {
      box.className = 'callout callout-ok';
      box.innerHTML = `<strong>Подключение успешно!</strong> Сервер: <code>${escapeHtml(res.name)}</code> • RAM: ${res.memory_mb || '?'} MiB • Диск: ${res.disk_mb || '?'} MiB • CPU: ${res.cpu_limit || '?'}% • Облачных слотов: ${res.backup_slots ?? '?'}`;
    } else {
      box.className = 'callout callout-danger';
      box.textContent = (res && res.error) || 'Не удалось подключиться к серверу.';
    }
  } catch (e) {
    box.className = 'callout callout-danger';
    box.textContent = 'Ошибка проверки: ' + e;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Проверить подключение';
  }
}

async function saveSettings() {
  const res = await window.pywebview.api.save_config(
    document.getElementById('cfg-url').value,
    document.getElementById('cfg-key').value,
    document.getElementById('cfg-id').value,
    document.getElementById('cfg-max').value
  );
  if (res && res.success) {
    await syncConfigToUI();
    showToast('Настройки подключения сохранены', 'ok');
    checkStatus();
    refreshBackups(true);
  }
}
