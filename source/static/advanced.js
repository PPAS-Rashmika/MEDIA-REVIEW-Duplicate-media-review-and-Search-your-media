(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  let api, pending = false, sessions = [], loaded = false;
  const defaults = {extensions: [], excludePatterns: [], minSize: null, maxSize: null, modifiedFrom: '', modifiedTo: '', minWidth: 0, minHeight: 0,
    audioMatching: false, audioThreshold: 90, archives: false, metadataMatching: false, metadataTimeTolerance: 2, metadataGpsTolerance: 0, metadataSameCamera: true};
  const fields = ['filter-extensions', 'filter-exclude', 'filter-min-size', 'filter-max-size', 'filter-from', 'filter-to', 'filter-width', 'filter-height',
    'match-audio', 'match-audio-threshold', 'match-archives', 'match-metadata', 'metadata-time', 'metadata-distance', 'metadata-camera'];
  const n = (id, fallback = 0) => $(id).value.trim() === '' ? fallback : Number($(id).value);
  function readOptions() {
    for (const id of fields) if (!$(id).checkValidity()) { $(id).reportValidity(); throw new Error('Check the highlighted scan option.'); }
    const options = {
      extensions: $('filter-extensions').value.split(/[,;\s]+/).map(s => s.trim()).filter(Boolean),
      excludePatterns: $('filter-exclude').value.split('\n').map(s => s.trim()).filter(Boolean),
      minSize: n('filter-min-size', null) === null ? null : Math.round(n('filter-min-size') * 1048576),
      maxSize: n('filter-max-size', null) === null ? null : Math.round(n('filter-max-size') * 1048576),
      modifiedFrom: $('filter-from').value, modifiedTo: $('filter-to').value,
      minWidth: n('filter-width'), minHeight: n('filter-height'), audioMatching: $('match-audio').checked,
      audioThreshold: n('match-audio-threshold', 90), archives: $('match-archives').checked,
      metadataMatching: $('match-metadata').checked, metadataTimeTolerance: n('metadata-time', 2),
      metadataGpsTolerance: n('metadata-distance'), metadataSameCamera: $('metadata-camera').checked
    };
    if (options.minSize !== null && options.maxSize !== null && options.minSize > options.maxSize) throw new Error('Minimum file size must not exceed maximum file size.');
    if (options.modifiedFrom && options.modifiedTo && options.modifiedFrom > options.modifiedTo) throw new Error('The first date must not be after the last date.');
    return options;
  }
  function applyOptions(value = {}) {
    const o = {...defaults, ...value};
    $('filter-extensions').value = (o.extensions || []).join(', ');
    $('filter-exclude').value = (o.excludePatterns || []).join('\n');
    $('filter-min-size').value = o.minSize === null ? '' : o.minSize / 1048576;
    $('filter-max-size').value = o.maxSize === null ? '' : o.maxSize / 1048576;
    $('filter-from').value = o.modifiedFrom || ''; $('filter-to').value = o.modifiedTo || '';
    $('filter-width').value = o.minWidth || ''; $('filter-height').value = o.minHeight || '';
    $('match-audio').checked = o.audioMatching; $('match-audio-threshold').value = o.audioThreshold;
    $('match-archives').checked = o.archives; $('match-metadata').checked = o.metadataMatching;
    $('metadata-time').value = o.metadataTimeTolerance; $('metadata-distance').value = o.metadataGpsTolerance;
    $('metadata-camera').checked = o.metadataSameCamera; summary();
  }
  function summary() {
    let o;
    try { o = readOptions(); } catch (_) { $('scan-options-summary').textContent = 'Advanced options need attention. Open Filters & matching.'; return; }
    const labels = [];
    if (o.extensions.length) labels.push(`${o.extensions.length} file extensions`);
    if (o.excludePatterns.length) labels.push(`${o.excludePatterns.length} exclusions`);
    if (o.minSize !== null || o.maxSize !== null) labels.push('Size filter');
    if (o.modifiedFrom || o.modifiedTo) labels.push('Date filter');
    if (o.minWidth || o.minHeight) labels.push('Image dimensions');
    if (o.audioMatching) labels.push('Audio fingerprints');
    if (o.archives) labels.push('Archive contents');
    if (o.metadataMatching) labels.push('Metadata suggestions');
    $('scan-options-summary').textContent = labels.length ? `Next scan: ${labels.join(' · ')}` : 'Standard scan · no advanced filters';
  }
  function setBusy(busy) {
    if (!api) return;
    for (const id of fields) $(id).disabled = Boolean(busy);
    $('reset-advanced').disabled = Boolean(busy);
    const state = api.getState();
    $('save-session').disabled = Boolean(busy || pending || !state?.hasScanned);
    $('session-name').disabled = Boolean(busy || pending);
    $('refresh-sessions').disabled = Boolean(pending);
    document.querySelectorAll('[data-load-session]').forEach(button => { button.disabled = Boolean(busy || pending); });
    $('sessions-current').textContent = state?.hasScanned
      ? `${Number(state.stats?.total || 0).toLocaleString()} files in the current ${state.scanStopped ? 'partial ' : ''}scan${state.scanning ? ' · scanning in progress' : ''}`
      : 'Complete or stop a folder scan to save its results.';
  }
  function renderSessions() {
    $('session-list').innerHTML = sessions.length ? sessions.map(item => {
      const date = new Date(item.savedAt);
      const when = Number.isNaN(date.getTime()) ? '' : date.toLocaleString();
      return `<article class="session-card"><div class="session-art"><svg><use href="#i-restore"/></svg></div><div class="session-details"><h3>${esc(item.name || 'Saved scan')}</h3><p>${esc(item.root)}</p><span>${Number(item.files || 0).toLocaleString()} files · ${esc(when)}${item.stopped ? ' · Partial results' : ''}</span></div><button class="button secondary small" type="button" data-load-session="${esc(item.id)}">Open scan</button><button class="button secondary small" type="button" data-open-catalog="${esc(item.id)}">Open catalog</button></article>`;
    }).join('') : '<div class="session-empty"><h3>No saved scans yet</h3><p>Save your current scan above to continue reviewing it later.</p></div>';
    setBusy(api.isBusy());
  }
  async function showSessions() {
    if (pending) return;
    pending = true; setBusy(api.isBusy()); $('session-status').textContent = 'Loading saved scans…';
    try { const result = await api.request('/api/sessions/list', {}); sessions = result.sessions || []; loaded = true; renderSessions(); $('session-status').textContent = ''; }
    catch (e) { $('session-status').textContent = 'Saved scans could not be loaded. Use Refresh to try again.'; api.error(e.message); }
    finally { pending = false; setBusy(api.isBusy()); }
  }
  function init(hooks) {
    api = hooks;
    $('advanced-settings-slot').innerHTML = `
      <fieldset class="advanced-group"><legend>Scan filters</legend><p>Leave a field empty to include all values. Exclusions apply before files are analyzed.</p>
        <label class="wide-option" for="filter-extensions"><span>Include extensions</span><input id="filter-extensions" maxlength="1024" placeholder="jpg, png, mp4, mp3" autocomplete="off"><small>Empty means every supported file type allowed by the scan settings.</small></label>
        <label class="wide-option" for="filter-exclude"><span>Exclude folders or patterns</span><textarea id="filter-exclude" rows="3" maxlength="4096" placeholder="Backups/**&#10;*.tmp"></textarea><small>One relative folder or wildcard pattern per line.</small></label>
        <div class="advanced-grid"><label for="filter-min-size"><span>Minimum size (MiB)</span><input id="filter-min-size" type="number" min="0" max="1000000000" step="any" placeholder="No minimum"></label><label for="filter-max-size"><span>Maximum size (MiB)</span><input id="filter-max-size" type="number" min="0" max="1000000000" step="any" placeholder="No maximum"></label>
        <label for="filter-from"><span>Modified from</span><input id="filter-from" type="date"></label><label for="filter-to"><span>Modified through</span><input id="filter-to" type="date"></label>
        <label for="filter-width"><span>Minimum image width (px)</span><input id="filter-width" type="number" min="0" max="200000" step="1" placeholder="Any width"></label><label for="filter-height"><span>Minimum image height (px)</span><input id="filter-height" type="number" min="0" max="200000" step="1" placeholder="Any height"></label></div><p>Dimension filters include only images. Archive member size uses the uncompressed file size.</p>
      </fieldset>
      <fieldset class="advanced-group"><legend>Additional matching</legend>
        <label class="feature-check"><input id="match-audio" type="checkbox"><span><strong>Compare audio fingerprints</strong><small>Find similar recordings across formats using up to the first 30 minutes. Fingerprint matches are suggestions; listen before removing files.</small></span></label>
        <label class="inline-option" for="match-audio-threshold"><span>Audio similarity (%)</span><input id="match-audio-threshold" type="number" min="70" max="99" step="1" value="90"></label>
        <label class="feature-check"><input id="match-archives" type="checkbox"><span><strong>Scan inside archives</strong><small>Read ZIP, RAR, 7z and TAR archives, including compressed TAR. Members can be compared, searched and previewed; they stay inside the archive. Nested, encrypted or unsafe entries are skipped.</small></span></label>
        <label class="feature-check"><input id="match-metadata" type="checkbox"><span><strong>Suggest matches from image metadata</strong><small>Compare capture time and camera information. These suggestions can include different photos taken close together.</small></span></label>
        <div class="advanced-grid"><label for="metadata-time"><span>Capture time tolerance (seconds)</span><input id="metadata-time" type="number" min="0" max="3600" step="1" value="2"></label><label for="metadata-distance"><span>Maximum GPS distance (meters)</span><input id="metadata-distance" type="number" min="0" max="100000" step="1" value="0"><small>0 means do not require GPS.</small></label></div>
        <label class="feature-check"><input id="metadata-camera" type="checkbox" checked><span>Require matching camera make and model</span></label>
      </fieldset>`;
    $('sessions-view').innerHTML = `<div class="session-save"><div><h2>Keep your scan for later</h2><p id="sessions-current"></p></div><form id="save-session-form" class="session-save-form"><label for="session-name"><span>Scan name</span><input id="session-name" maxlength="100" placeholder="e.g. Family photos — October" autocomplete="off"></label><button id="save-session" class="button primary" type="submit" disabled>Save current scan</button></form><p class="session-privacy">Saved scans stay on this computer. They include file locations, recognized text and search information; the original files are not copied. Opening a scan checks for missing or changed files.</p></div><div class="session-list-heading"><h2>Your saved scans</h2><button id="refresh-sessions" class="button secondary small" type="button">Refresh</button></div><p id="session-status" role="status" aria-live="polite"></p><div id="session-list"></div>`;
    applyOptions();
    for (const id of fields) $(id).addEventListener('change', summary);
    $('reset-advanced').addEventListener('click', () => { if (!api.isBusy()) applyOptions(); });
    $('apply-advanced').addEventListener('click', () => { try { readOptions(); summary(); $('settings-dialog').close(); } catch (e) { api.error(e.message); } });
    $('refresh-sessions').addEventListener('click', showSessions);
    $('save-session-form').addEventListener('submit', async event => {
      event.preventDefault(); if (pending || api.isBusy() || !api.getState()?.hasScanned) return;
      pending = true; setBusy(true); $('session-status').textContent = 'Saving current scan…';
      try { const result = await api.request('/api/sessions/save', {name: $('session-name').value.trim() || 'Saved scan'}); sessions = result.sessions || []; loaded = true; renderSessions(); $('session-status').textContent = 'Scan saved on this computer.'; api.toast('Scan saved. Open it again from Saved scans.'); }
      catch (e) { $('session-status').textContent = 'The scan could not be saved.'; api.error(e.message); }
      finally { pending = false; setBusy(api.isBusy()); }
    });
    $('session-list').addEventListener('click', async event => {
      const button = event.target.closest('[data-load-session]'); if (!button || button.disabled || pending || api.isBusy()) return;
      pending = true; setBusy(true); $('session-status').textContent = 'Checking saved files…';
      try { const result = await api.request('/api/sessions/load', {id: button.dataset.loadSession}); const state = result.state || result;
        applyOptions(state.scanOptions || {}); await api.loadState(state); const report = state.sessionLoad || result.sessionLoad;
        api.toast(report?.skippedFiles ? `Saved scan opened. ${report.skippedFiles} missing or changed files were skipped.` : 'Saved scan opened. No new scan was started.'); $('session-status').textContent = ''; }
      catch (e) { $('session-status').textContent = 'The saved scan could not be opened.'; api.error(e.message); }
      finally { pending = false; setBusy(api.isBusy()); }
    });
  }
  window.MediaReviewAdvanced = {init, readOptions, applyOptions, setBusy, showSessions};
})();
