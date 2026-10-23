(() => {
  'use strict';
  const names = { drive: 'Google Drive', onedrive: 'OneDrive', dropbox: 'Dropbox' };
  let api = null, state = null, timer = null, active = false, busy = false, shown = 50;
  let history = [], pendingPlan = null;
  const selected = new Set();
  const $ = id => document.getElementById(id);
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const bytes = n => {
    if (!Number.isFinite(Number(n)) || n < 0) return 'Size unavailable';
    const unit = n ? Math.min(4, Math.floor(Math.log(n) / Math.log(1024))) : 0;
    return `${(n / (1024 ** unit)).toLocaleString(undefined, { maximumFractionDigits: 1 })} ${['B', 'KB', 'MB', 'GB', 'TB'][unit]}`;
  };
  const running = () => Boolean(state?.scan?.running || state?.connection?.running || state?.action?.running);
  const locked = () => busy || running() || Boolean(api?.isLocalBusy?.());
  function schedule() {
    clearTimeout(timer); timer = null;
    if (active && running()) timer = setTimeout(() => refresh().catch(report), 900);
  }
  function report(error) { api?.error?.(error?.message || 'The cloud action could not be completed.'); }
  async function refresh() {
    if (!api) return;
    state = await api.request('/api/cloud/state');
    if (!running()) {
      const saved = await api.request('/api/cloud/history');
      history = saved.operations || [];
    }
    render(); schedule();
  }
  async function action(path, data = {}) {
    if (busy) return;
    busy = true; render();
    try {
      state = await api.request(path, data);
      if (!running()) history = (await api.request('/api/cloud/history')).operations || [];
      render(); schedule();
      return state;
    } catch (error) { report(error); }
    finally { busy = false; render(); }
  }
  function providerChanged() {
    $('cloud-google-options').hidden = $('cloud-provider').value !== 'drive';
    $('cloud-client-secret').value = '';
  }
  function renderAccounts() {
    const select = $('cloud-account');
    const previous = select.value;
    const signature = JSON.stringify(state?.accounts || []);
    if (select.dataset.signature === signature) return;
    select.dataset.signature = signature;
    select.replaceChildren();
    const placeholder = el('option', 'Choose a connected account'); placeholder.value = ''; select.append(placeholder);
    for (const account of state?.accounts || []) {
      const option = el('option', `${account.label} · ${account.providerName || names[account.provider]}`);
      option.value = account.id; select.append(option);
    }
    if (previous && [...select.options].some(o => o.value === previous)) select.value = previous;
    else if (state?.accounts?.length === 1) select.value = state.accounts[0].id;
  }
  function renderQuestion() {
    const question = state?.connection?.question;
    const box = $('cloud-question');
    box.hidden = !question;
    if (!question) { box.dataset.signature = ''; return; }
    const signature = JSON.stringify(question);
    if (box.dataset.signature === signature) return;
    box.dataset.signature = signature;
    $('cloud-question-help').textContent = question.help || 'Choose an option to finish connecting.';
    const answerHost = $('cloud-answer-host'); answerHost.replaceChildren();
    let input;
    if (question.choices?.length) {
      input = document.createElement('select');
      for (const choice of question.choices) {
        const option = el('option', choice.label || choice.value); option.value = choice.value; input.append(option);
      }
      if (!question.exclusive) {
        const custom = el('option', 'Enter another value'); custom.value = '__custom__'; input.append(custom);
      }
      input.value = question.default || input.options[0]?.value || '';
      if (!input.value && input.options.length) input.selectedIndex = 0;
      input.addEventListener('change', () => { $('cloud-custom-answer').hidden = input.value !== '__custom__'; });
    } else {
      input = document.createElement('input'); input.type = 'text'; input.value = question.default || '';
    }
    input.id = 'cloud-answer'; input.setAttribute('aria-label', 'Cloud connection answer');
    answerHost.append(input);
    $('cloud-custom-answer').hidden = true;
  }
  function renderGroups() {
    const scan = state?.scan || {};
    const host = $('cloud-results');
    const currentIds = new Set((scan.groups || []).flatMap(group => group.files.map(file => file.id)));
    for (const id of selected) if (!currentIds.has(id)) selected.delete(id);
    const signature = JSON.stringify([scan.groups, shown]);
    if (host.dataset.signature === signature) return;
    host.dataset.signature = signature; host.replaceChildren();
    const groups = scan.groups || [];
    if (!groups.length) {
      host.append(el('p', scan.phase === 'complete' ? 'No matching checksum groups were found in this folder.' : 'Connect an account, choose a cloud folder, then click Scan cloud.', 'cloud-empty'));
    }
    groups.slice(0, shown).forEach((group, index) => {
      const details = el('details', undefined, 'cloud-match');
      const summary = el('summary');
      summary.append(el('strong', `Group ${index + 1} · ${group.files.length} files`),
        el('span', `${group.possible ? 'Possible match · ' : 'Server checksum match · '}${group.hashType} · ${bytes(group.duplicateBytes)} in extra copies`));
      details.append(summary);
      if (group.possible) details.append(el('p', 'QuickXor is a weak checksum. These files may be different; open and inspect them before making any changes.', 'cloud-warning'));
      for (const file of group.files) {
        const row = el('div', undefined, 'cloud-file');
        const checkbox = el('input'); checkbox.type = 'checkbox'; checkbox.checked = selected.has(file.id);
        checkbox.className = 'cloud-file-check'; checkbox.dataset.cloudFile = file.id;
        checkbox.setAttribute('aria-label', `Select ${file.path}`); checkbox.disabled = locked();
        checkbox.addEventListener('change', () => {
          if (checkbox.checked) selected.add(file.id); else selected.delete(file.id);
          renderManagement();
        });
        const info = el('div');
        info.append(el('strong', file.name), el('span', file.path),
          el('small', `${bytes(file.size)}${file.modified ? ' · ' + file.modified : ''}`));
        const button = el('button', 'Open in cloud', 'button button-secondary'); button.type = 'button';
        button.addEventListener('click', async () => {
          if (locked()) return;
          try { await api.request('/api/cloud/open', { id: file.id }); }
          catch (error) { report(error); }
        });
        row.append(checkbox, info, button); details.append(row);
      }
      host.append(details);
    });
    $('cloud-more').hidden = groups.length <= shown;
  }
  function renderManagement() {
    if (!$('cloud-selection-count')) return;
    $('cloud-selection-count').textContent = `${selected.size.toLocaleString()} files selected`;
    $('cloud-preview-action').disabled = locked() || !selected.size;
    $('cloud-select-copies').disabled = locked() || !state?.scan?.groups?.length;
    $('cloud-clear-selection').disabled = locked() || !selected.size;
    $('cloud-action-kind').disabled = locked();
    $('cloud-destination').disabled = locked();
    $('cloud-destination-label').hidden = $('cloud-action-kind').value === 'trash';
    $('cloud-destination').hidden = $('cloud-action-kind').value === 'trash';
    const account = state?.accounts?.find(a => a.id === state?.scan?.accountId);
    $('cloud-write-notice').hidden = !account || account.writeEnabled === true;
    document.querySelectorAll('[data-cloud-file]').forEach(checkbox => {
      checkbox.disabled = locked(); checkbox.checked = selected.has(checkbox.dataset.cloudFile);
    });
    const host = $('cloud-history');
    const signature = JSON.stringify([history, locked()]);
    if (host.dataset.signature === signature) return;
    host.dataset.signature = signature; host.replaceChildren();
    if (!history.length) host.append(el('p', 'Confirmed cloud moves will appear here. Cloud trash uses a recovery folder in your own account.', 'cloud-muted'));
    for (const record of history.slice(0, 30)) {
      const details = el('details', undefined, 'cloud-match');
      const summary = el('summary'); summary.append(el('strong', `${record.accountLabel} · ${record.action} · ${record.moved} available to restore`), el('span', record.created));
      details.append(summary);
      for (const file of record.files || []) {
        const row = el('div', undefined, 'cloud-history-path');
        row.append(el('strong', file.source), el('span', `→ ${file.destination}`), el('small', file.status));
        details.append(row);
      }
      if (record.moved) {
        const restore = el('button', 'Review restoration', 'button secondary small'); restore.type = 'button'; restore.disabled = locked();
        restore.addEventListener('click', () => showConfirmation(record, true));
        const actions = el('div', undefined, 'cloud-history-actions'); actions.append(restore); details.append(actions);
      }
      host.append(details);
    }
  }
  function showConfirmation(plan, restore = false) {
    pendingPlan = { plan, restore };
    $('cloud-confirm-title').textContent = restore ? 'Restore these cloud files?' : 'Move these cloud files?';
    $('cloud-confirm-description').textContent = restore
      ? `${plan.accountLabel}: files will return to their original paths. Occupied paths are never overwritten.`
      : `${plan.accountLabel} · ${plan.providerName}: ${plan.files.length} files, ${bytes(plan.totalSize)}. ${plan.action === 'trash' ? 'Files move to a recoverable cloud folder. They are not permanently deleted.' : 'A separate batch folder keeps the destinations together. Undo is available in Cloud move history.'}`;
    $('cloud-confirm-warning').hidden = !plan.possibleMatches;
    const host = $('cloud-confirm-files'); host.replaceChildren();
    for (const file of plan.files || []) {
      if (restore && !['moved', 'uncertain', 'restoring'].includes(file.status)) continue;
      const row = el('div', undefined, 'cloud-history-path');
      row.append(el('strong', restore ? file.destination : file.source), el('span', '→ ' + (restore ? file.source : file.destination)));
      host.append(row);
    }
    $('cloud-confirm-accept').textContent = restore ? 'Restore files' : plan.action === 'trash' ? 'Move to cloud trash' : 'Move cloud files';
    $('cloud-action-dialog').showModal();
  }
  async function previewAction() {
    if (locked() || !selected.size) return;
    busy = true; render();
    try {
      const plan = await api.request('/api/cloud/action-plan', { ids: [...selected], action: $('cloud-action-kind').value, destination: $('cloud-destination').value.trim() });
      showConfirmation(plan);
    } catch (error) { report(error); }
    finally { busy = false; render(); }
  }
  function render() {
    if (!$('cloud-status')) return;
    const available = state?.available !== false;
    const connection = state?.connection || {};
    const scan = state?.scan || {};
    renderAccounts(); renderQuestion(); renderGroups(); renderManagement();
    $('cloud-unavailable').hidden = available;
    $('cloud-status').textContent = state?.action?.phase && state.action.phase !== 'ready'
      ? `${state.action.message}${state.action.running ? ` (${state.action.done} / ${state.action.total})` : ''}`
      : connection.phase && !['ready', 'connected', 'cancelled'].includes(connection.phase)
      ? connection.message : scan.message || connection.message || 'Cloud access starts only when you connect an account or request a scan.';
    $('cloud-total').textContent = Number(scan.totalFiles || 0).toLocaleString();
    $('cloud-hashed').textContent = Number(scan.hashedFiles || 0).toLocaleString();
    $('cloud-skipped').textContent = Number(scan.skippedFiles || 0).toLocaleString();
    $('cloud-groups-count').textContent = Number(scan.groupCount || 0).toLocaleString();
    $('cloud-truncated').hidden = !scan.truncated;
    $('cloud-stop').hidden = !scan.running;
    $('cloud-stop').disabled = busy || scan.phase === 'stopping';
    $('cloud-stop').textContent = scan.phase === 'stopping' ? 'Stopping…' : 'Stop cloud scan';
    $('cloud-cancel-connect').hidden = !['authorizing', 'question', 'cancelling'].includes(connection.phase);
    $('cloud-cancel-connect').disabled = busy || connection.phase === 'cancelling';
    $('cloud-continue').disabled = busy || running();
    ['cloud-provider', 'cloud-label', 'cloud-client-id', 'cloud-client-secret', 'cloud-connect'].forEach(id => { $(id).disabled = !available || locked() || Boolean(connection.question); });
    ['cloud-account', 'cloud-path', 'cloud-recursive', 'cloud-browse', 'cloud-root'].forEach(id => { $(id).disabled = !available || locked() || Boolean(connection.question); });
    $('cloud-scan').disabled = !available || locked() || !$('cloud-account').value || Boolean(connection.question);
    $('cloud-browse').disabled ||= !$('cloud-account').value;
    $('cloud-scan').textContent = scan.running ? 'Scanning cloud…' : 'Scan cloud';
    $('cloud-refresh').disabled = busy;
    document.querySelectorAll('[data-cloud-folder]').forEach(button => { button.disabled = locked(); });
  }
  async function browse(path) {
    if (locked() || !$('cloud-account').value) return;
    busy = true; render();
    try {
      const result = await api.request('/api/cloud/folders', { accountId: $('cloud-account').value, path });
      $('cloud-path').value = result.path || '';
      const host = $('cloud-folders'); host.replaceChildren(); host.hidden = false;
      const description = el('p', `Folders in ${result.path || 'account root'}. Selecting a folder does not start a scan.`, 'cloud-muted'); host.append(description);
      if (result.path) {
        const up = el('button', '↑ Parent folder', 'button button-secondary'); up.type = 'button'; up.dataset.cloudFolder = '';
        up.addEventListener('click', () => browse(result.path.split('/').slice(0, -1).join('/'))); host.append(up);
      }
      for (const folder of result.folders || []) {
        const button = el('button', folder.name, 'button button-secondary cloud-folder-button'); button.type = 'button'; button.dataset.cloudFolder = folder.path;
        button.addEventListener('click', () => browse(folder.path)); host.append(button);
      }
      if (!result.folders?.length) host.append(el('p', 'There are no subfolders here. You can scan the folder selected above.', 'cloud-muted'));
      if (result.truncated) host.append(el('p', 'Only the first 5,000 folders are shown. Enter a more specific folder path above.', 'cloud-warning'));
    } catch (error) { report(error); }
    finally { busy = false; render(); }
  }
  function init(config) {
    if (api) return;
    api = config;
    const host = $('cloud-workspace');
    if (!host) return;
    host.innerHTML = `
      <div class="cloud-intro"><h2>Cloud duplicate review</h2><p>Find matching files in Google Drive, OneDrive and Dropbox using server checksums. Your media stays in the cloud.</p></div>
      <p id="cloud-unavailable" class="cloud-warning" hidden>The cloud connector is missing. Use the complete Media Review release folder.</p>
      <div class="cloud-grid">
        <section class="cloud-card"><h3>Connect an account</h3><p class="cloud-muted">Sign in on the provider’s website. Account passwords never pass through this app.</p>
          <label for="cloud-provider">Cloud service</label><select id="cloud-provider"><option value="drive">Google Drive</option><option value="onedrive">OneDrive</option><option value="dropbox">Dropbox</option></select>
          <label for="cloud-label">Account label</label><input id="cloud-label" maxlength="80" placeholder="Personal, Work…" autocomplete="off">
          <div id="cloud-google-options"><p class="cloud-muted">Google Drive requires your Google Desktop OAuth application details. Enable the Drive API and add your account as a test user if the application is in testing.</p>
            <label for="cloud-client-id">Google OAuth client ID</label><input id="cloud-client-id" maxlength="1024" autocomplete="off" placeholder="…apps.googleusercontent.com">
            <label for="cloud-client-secret">Google OAuth client secret</label><input id="cloud-client-secret" type="password" maxlength="1024" autocomplete="new-password">
            <p class="cloud-muted">This connection allows file management. Scans read metadata only; files move only after a separate preview and confirmation. Connection details are encrypted for your Windows account.</p>
          </div>
          <div class="cloud-actions"><button id="cloud-connect" type="button" class="button button-primary">Connect account</button><button id="cloud-cancel-connect" type="button" class="button button-secondary" hidden>Cancel connection</button></div>
          <div id="cloud-question" class="cloud-question" hidden><p id="cloud-question-help"></p><div id="cloud-answer-host"></div><input id="cloud-custom-answer" aria-label="Other cloud connection answer" maxlength="2048" hidden><button id="cloud-continue" type="button" class="button button-primary">Continue connection</button></div>
        </section>
        <section class="cloud-card"><h3>Choose where to scan</h3>
          <label for="cloud-account">Connected account</label><select id="cloud-account"><option value="">Choose a connected account</option></select>
          <label for="cloud-path">Cloud folder path</label><input id="cloud-path" maxlength="2048" placeholder="Leave blank for account root" autocomplete="off">
          <div class="cloud-actions"><button id="cloud-browse" type="button" class="button button-secondary">Browse folders</button><button id="cloud-root" type="button" class="button button-secondary">Account root</button></div>
          <label class="cloud-check"><input id="cloud-recursive" type="checkbox" checked> Include cloud subfolders</label>
          <div class="cloud-actions"><button id="cloud-scan" type="button" class="button button-primary">Scan cloud</button><button id="cloud-stop" type="button" class="button button-secondary" hidden>Stop cloud scan</button><button id="cloud-refresh" type="button" class="button button-secondary">Refresh status</button></div>
          <div id="cloud-folders" class="cloud-folders" hidden></div>
        </section>
      </div>
      <p id="cloud-status" role="status" aria-live="polite" class="cloud-status"></p>
      <div class="cloud-summary"><div><strong id="cloud-total">0</strong><span>Files listed</span></div><div><strong id="cloud-hashed">0</strong><span>Files with checksums</span></div><div><strong id="cloud-skipped">0</strong><span>Files without usable checksums</span></div><div><strong id="cloud-groups-count">0</strong><span>Matching groups</span></div></div>
      <p class="cloud-notice">Cloud scanning reads names, sizes and server checksums. Every file move needs a separate preview and confirmation. Cloud trash is a recovery folder inside your account; it keeps the file bytes and does not free cloud storage. QuickXor matches may be different files. Visual, audio, OCR and archive-content analysis apply to local scans.</p>
      <section class="cloud-card cloud-management" aria-label="Manage selected cloud files"><h3>Manage selected files</h3><p id="cloud-write-notice" class="cloud-warning" hidden>This account was connected for read-only review. Connect it again with this version to enable confirmed moves.</p>
        <div class="cloud-selection-bar"><strong id="cloud-selection-count">0 files selected</strong><button id="cloud-select-copies" type="button" class="button secondary small">Select extra copies</button><button id="cloud-clear-selection" type="button" class="button secondary small">Clear selection</button></div>
        <label for="cloud-action-kind">Action</label><select id="cloud-action-kind"><option value="trash">Move to recoverable cloud trash</option><option value="move">Move to another folder</option><option value="type">Organize by file type</option><option value="year">Organize by modified year</option></select>
        <label id="cloud-destination-label" for="cloud-destination" hidden>Existing destination folder (blank for account root)</label><input id="cloud-destination" maxlength="2048" placeholder="Photos/Organized" autocomplete="off" hidden>
        <p class="cloud-muted">Select up to 100 files. Keep at least one file in each group when using cloud trash. All destinations appear in the preview.</p><button id="cloud-preview-action" class="button button-primary" type="button" disabled>Preview cloud move</button>
      </section>
      <p id="cloud-truncated" class="cloud-warning" hidden>This result reached a safety limit. Choose a smaller cloud folder for a complete review.</p>
      <div id="cloud-results"></div><button id="cloud-more" type="button" class="button button-secondary" hidden>Show more groups</button>
      <section class="cloud-history-section"><h3>Cloud move history</h3><div id="cloud-history"></div></section>
      <dialog id="cloud-action-dialog" class="confirm-dialog cloud-action-dialog" aria-labelledby="cloud-confirm-title"><h2 id="cloud-confirm-title"></h2><p id="cloud-confirm-description"></p><p id="cloud-confirm-warning" class="cloud-warning" hidden>This selection includes weak QuickXor matches. Files may be different. Inspect them in the cloud before confirming. This action preserves the selected files in a recoverable destination.</p><div id="cloud-confirm-files" class="cloud-confirm-files" tabindex="0" aria-label="Cloud file move preview"></div><div class="dialog-actions"><button id="cloud-confirm-cancel" class="button secondary" type="button">Cancel</button><button id="cloud-confirm-accept" class="button primary" type="button">Move cloud files</button></div></dialog>`;
    $('cloud-provider').addEventListener('change', providerChanged);
    $('cloud-connect').addEventListener('click', async () => {
      if (locked()) return;
      const provider = $('cloud-provider').value;
      const data = { provider, label: $('cloud-label').value.trim(),
        clientId: provider === 'drive' ? $('cloud-client-id').value.trim() : '',
        clientSecret: provider === 'drive' ? $('cloud-client-secret').value.trim() : '' };
      try { await action('/api/cloud/connect', data); }
      finally { $('cloud-client-secret').value = ''; }
    });
    $('cloud-cancel-connect').addEventListener('click', () => action('/api/cloud/cancel'));
    $('cloud-continue').addEventListener('click', () => {
      const answer = $('cloud-answer');
      if (!answer) return;
      action('/api/cloud/answer', { answer: answer.value === '__custom__' ? $('cloud-custom-answer').value : answer.value });
    });
    $('cloud-account').addEventListener('change', () => { $('cloud-path').value = ''; $('cloud-folders').hidden = true; render(); });
    $('cloud-path').addEventListener('input', () => { $('cloud-folders').hidden = true; });
    $('cloud-browse').addEventListener('click', () => browse($('cloud-path').value.trim()));
    $('cloud-root').addEventListener('click', () => { $('cloud-path').value = ''; $('cloud-folders').hidden = true; });
    $('cloud-scan').addEventListener('click', () => {
      if (locked()) return;
      shown = 50;
      selected.clear();
      action('/api/cloud/scan', { accountId: $('cloud-account').value, path: $('cloud-path').value.trim(), recursive: $('cloud-recursive').checked });
    });
    $('cloud-stop').addEventListener('click', () => action('/api/cloud/stop'));
    $('cloud-refresh').addEventListener('click', () => refresh().catch(report));
    $('cloud-more').addEventListener('click', () => { shown += 50; renderGroups(); });
    $('cloud-action-kind').addEventListener('change', renderManagement);
    $('cloud-preview-action').addEventListener('click', previewAction);
    $('cloud-clear-selection').addEventListener('click', () => { selected.clear(); renderManagement(); });
    $('cloud-select-copies').addEventListener('click', () => {
      selected.clear();
      for (const group of state?.scan?.groups || []) for (const file of group.files.slice(1)) if (selected.size < 100) selected.add(file.id);
      renderManagement();
    });
    $('cloud-confirm-cancel').addEventListener('click', () => { $('cloud-action-dialog').close(); pendingPlan = null; });
    $('cloud-confirm-accept').addEventListener('click', async () => {
      if (!pendingPlan || locked()) return;
      const pending = pendingPlan; pendingPlan = null; $('cloud-action-dialog').close();
      await action(pending.restore ? '/api/cloud/action-restore' : '/api/cloud/action-execute', pending.restore ? { operationId: pending.plan.id, confirmed: true } : { planId: pending.plan.id, confirmed: true });
      selected.clear(); renderManagement();
    });
    window.addEventListener('pagehide', () => { clearTimeout(timer); timer = null; });
    providerChanged(); render();
  }
  async function show(visible) {
    active = Boolean(visible);
    if (!active) { clearTimeout(timer); timer = null; return; }
    await refresh().catch(report);
  }
  window.MediaReviewCloud = { init, show, refresh, setBusy: render };
})();
