(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const key = value => String(value ?? '');
  const app = { state: null, photos: new Map(), active: null, keeper: null, candidate: null, keeperSelectedByUser: false, filter: 'all', mediaFilter: 'all', imageCategory: 'all', search: '', view: 'review', busy: false, loading: true, failed: false, pendingDelete: null, pendingBulkDelete: null, bulkPlanning: false, bulkMoving: false, poll: null, toastTimer: null, undoId: null, listSignature: '', trashLoading: false, libraryBusy: false, libraryResult: null, libraryRequest: null, libraryDirty: false, libraryMessage: '', librarySignature: '' };
  const photoUrl = (id, size = 'preview') => `/api/photo/${encodeURIComponent(id)}?size=${size}`;
  const mediaUrl = id => `/api/media/${encodeURIComponent(id)}`;
  const mediaType = photo => photo?.mediaType || 'image';
  const mediaLabel = photo => ({ image: 'Image', video: 'Video', audio: 'Audio', other: 'File' }[mediaType(photo)] || 'File');
  const categoryLabels = { screenshot: 'Screenshots', downloaded: 'Downloaded', text: 'Text images', other: 'Other images' };
  const categoryTagLabels = { screenshot: 'Screenshot', downloaded: 'Downloaded', text: 'Text' };
  const imageCategories = photo => mediaType(photo) === 'image' && Array.isArray(photo?.imageCategories)
    ? photo.imageCategories.filter(category => Object.hasOwn(categoryTagLabels, category)) : [];
  const matchesCategory = photo => app.imageCategory === 'all' || Boolean(photo && mediaType(photo) === 'image'
    && (app.imageCategory === 'other' ? imageCategories(photo).length === 0 : imageCategories(photo).includes(app.imageCategory)));
  const groupFileLabel = photos => {
    const types = new Set(photos.map(mediaType));
    const type = types.size === 1 ? mediaType(photos[0]) : 'other';
    return { image: 'images', video: 'videos', audio: 'audio files', other: 'files' }[type] || 'files';
  };
  const duration = photo => {
    const seconds = Number(photo?.duration);
    if (!Number.isFinite(seconds) || seconds <= 0) return '';
    const total = Math.round(seconds);
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
  };
  const bytes = value => {
    const n = Number(value);
    if (!Number.isFinite(n) || n < 0) return 'Size unavailable';
    if (n === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const unit = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
    return `${(n / (1024 ** unit)).toLocaleString(undefined, { maximumFractionDigits: unit > 1 ? 1 : 0 })} ${units[unit]}`;
  };
  const dimensions = p => Number(p?.width) > 0 && Number(p?.height) > 0 ? `${p.width.toLocaleString()} × ${p.height.toLocaleString()}` : 'Dimensions unavailable';
  const pixels = p => Number(p?.width || 0) * Number(p?.height || 0);
  const metadata = photo => [mediaLabel(photo), mediaType(photo) === 'image' || mediaType(photo) === 'video' ? dimensions(photo) : '', duration(photo), bytes(photo.size), photo.format || ''].filter(Boolean);
  const kindLabel = kind => kind === 'similar' ? 'Visually similar' : kind === 'pixels' ? 'Same image' : 'Exact duplicate';
  const members = group => (group?.photos || []).map(id => app.photos.get(key(id))).filter(Boolean);
  const matchingMembers = group => members(group).filter(matchesCategory);
  const rankedMembers = group => members(group).sort((a, b) => pixels(b) - pixels(a) || Number(b.size || 0) - Number(a.size || 0));
  const defaultKeeper = group => {
    const ranked = rankedMembers(group);
    return (app.imageCategory !== 'all' && ranked.find(photo => !matchesCategory(photo))) || ranked[0];
  };
  const candidateOptions = group => app.imageCategory === 'all' ? members(group)
    : matchingMembers(group).filter(photo => key(photo.id) !== app.keeper);
  const canRemoveCandidate = () => {
    const group = activeGroup(), candidate = app.photos.get(app.candidate);
    return Boolean(group && candidate && key(candidate.id) !== app.keeper && matchesCategory(candidate)
      && members(group).some(photo => key(photo.id) === key(candidate.id)));
  };
  const allGroups = () => (app.state?.groups || []).filter(g => members(g).length >= 2);
  const groups = () => allGroups().filter(g => {
    if (app.filter === 'exact' && g.kind === 'similar') return false;
    if (app.filter === 'similar' && g.kind !== 'similar') return false;
    if (app.mediaFilter !== 'all' && !members(g).some(p => mediaType(p) === app.mediaFilter)) return false;
    if (app.imageCategory !== 'all') {
      if (!['all', 'image'].includes(app.mediaFilter)) return false;
      if (!matchingMembers(g).length) return false;
    }
    return !app.search || members(g).some(p => `${p.name} ${p.path}`.toLowerCase().includes(app.search));
  });
  const activeGroup = () => groups().find(g => key(g.id) === app.active);
  function setImage(element, url, alt) {
    if (element.getAttribute('src') !== url) element.src = url;
    element.alt = alt;
  }
  async function request(path, body) {
    let response;
    try {
      response = await fetch(path, body === undefined ? { cache: 'no-store' } : {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Review-Token': app.state?.token || '' }, body: JSON.stringify(body)
      });
    } catch (_) { throw new Error('Media Review is not responding. Restart the app, then try again.'); }
    let result;
    try { result = await response.json(); } catch (_) { throw new Error('Media Review returned an unexpected response. Please try again.'); }
    if (!response.ok || result.ok === false) {
      const failure = new Error(result.message || result.error || 'The action could not be completed.');
      failure.result = result;
      throw failure;
    }
    return result;
  }
  function error(message) { $('error-text').textContent = message; $('error-banner').hidden = false; }
  function hideError() { $('error-banner').hidden = true; }
  function toast(message, undoId = null) {
    clearTimeout(app.toastTimer);
    app.undoId = undoId;
    $('toast-text').textContent = message;
    $('toast-undo').hidden = undoId == null;
    $('toast-undo').disabled = false;
    $('toast').hidden = false;
    app.toastTimer = setTimeout(() => { $('toast').hidden = true; }, undoId == null ? 6000 : 16000);
  }
  function setBusy(busy) {
    app.busy = busy;
    const scanning = Boolean(app.state?.scanning);
    const unavailable = busy || scanning || app.libraryBusy;
    ['browse-button', 'folder-path', 'recursive', 'include-all-files', 'threshold', 'confirm-delete'].forEach(id => { $(id).disabled = unavailable; });
    $('scan-button').disabled = unavailable || app.loading || app.failed || !$('folder-path').value.trim();
    $('delete-button').disabled = unavailable || !canRemoveCandidate();
    const visibleGroups = groups();
    $('bulk-delete-button').disabled = unavailable || app.loading || app.failed || !visibleGroups.length;
    $('bulk-delete-button').querySelector('span').textContent = app.bulkPlanning ? 'Preparing review…' : 'Move all copies to trash';
    const categoryScope = app.imageCategory === 'all' ? '' : `${categoryLabels[app.imageCategory]} copies only. `;
    $('bulk-delete-scope').textContent = visibleGroups.length
      ? `${categoryScope}Applies to ${visibleGroups.length.toLocaleString()} visible ${visibleGroups.length === 1 ? 'group' : 'groups'}. A file is kept in every group.`
      : `${categoryScope}Applies to the visible matches. A file is kept in every group.`;
    $('confirm-bulk-delete').disabled = unavailable || !app.pendingBulkDelete?.count;
    $('confirm-bulk-delete').textContent = app.bulkMoving ? 'Moving copies…' : `Move ${Number(app.pendingBulkDelete?.count || 0).toLocaleString()} ${app.pendingBulkDelete?.count === 1 ? 'copy' : 'copies'} to trash`;
    $('scan-button').querySelector('span').textContent = scanning ? 'Scanning…' : 'Scan folder';
    $('confirm-delete').textContent = busy ? 'Moving…' : 'Move to trash';
    $('keeper-select').disabled = unavailable || !activeGroup();
    $('candidate-select').disabled = unavailable || !activeGroup() || candidateOptions(activeGroup()).length < 2;
    document.querySelectorAll('[data-restore]').forEach(button => { button.disabled = unavailable; });
    const searchable = !unavailable && !app.loading && !app.failed && Boolean(app.state?.hasScanned && app.state?.root);
    $('library-search-button').disabled = !searchable || !$('library-query').value.trim();
    $('library-search-button').querySelector('span').textContent = app.libraryBusy ? 'Searching…' : 'Search';
    ['library-query', 'library-mode', 'library-media-type', 'library-category'].forEach(id => { $(id).disabled = unavailable || app.loading || app.failed; });
    $('library-previous').disabled = !searchable || app.libraryDirty || !app.libraryResult || Number(app.libraryResult.offset || 0) <= 0;
    $('library-next').disabled = !searchable || app.libraryDirty || !app.libraryResult?.hasMore;
    document.querySelectorAll('[data-library-preview], [data-show-file]').forEach(button => { button.disabled = unavailable || app.libraryDirty; });
  }
  function stopPlayback(root = document, unload = false) {
    root.querySelectorAll('video, audio').forEach(player => {
      player.pause();
      if (unload) { player.removeAttribute('src'); player.load(); }
    });
  }
  function chooseGroup(id) {
    stopPlayback();
    const next = groups().find(g => key(g.id) === key(id));
    app.active = next ? key(next.id) : null;
    app.keeper = null; app.candidate = null; app.keeperSelectedByUser = false;
    renderReview();
    const chosen = [...$('group-list').querySelectorAll('[data-group]')].find(b => b.dataset.group === app.active);
    chosen?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }
  function moveGroup(direction) {
    const filtered = groups();
    const index = filtered.findIndex(g => key(g.id) === app.active);
    const nextIndex = Math.max(0, Math.min(filtered.length - 1, index + direction));
    if (filtered[nextIndex]) chooseGroup(filtered[nextIndex].id);
  }
  function ensureSelection() {
    const filtered = groups();
    let group = filtered.find(g => key(g.id) === app.active);
    if (!group) { group = filtered[0]; app.active = group ? key(group.id) : null; app.keeper = null; app.candidate = null; app.keeperSelectedByUser = false; }
    if (!group) return;
    const sorted = rankedMembers(group);
    if (!sorted.some(p => key(p.id) === app.keeper)) app.keeperSelectedByUser = false;
    if (!app.keeperSelectedByUser) app.keeper = key(defaultKeeper(group).id);
    const candidates = sorted.filter(p => key(p.id) !== app.keeper && matchesCategory(p));
    if (!candidates.some(p => key(p.id) === app.candidate)) app.candidate = candidates.length ? key(candidates[0].id) : null;
  }
  function renderGroupList() {
    const filtered = groups();
    $('group-total').textContent = `${filtered.length} ${filtered.length === 1 ? 'group' : 'groups'}`;
    const signature = JSON.stringify([app.active, app.filter, app.mediaFilter, app.imageCategory, app.search, app.loading, app.failed, app.state?.hasScanned, app.state?.scanning, filtered.map(g => [g.id, g.kind, g.photos])]);
    if (signature === app.listSignature) return;
    app.listSignature = signature;
    if (!filtered.length) {
      $('group-list').innerHTML = `<p class="group-list-empty">${app.loading ? 'Loading the app…' : app.state?.scanning ? 'Looking for matches…' : !app.state?.hasScanned ? 'Choose a folder, then click Scan folder.' : app.search || app.filter !== 'all' || app.mediaFilter !== 'all' || app.imageCategory !== 'all' ? 'No matches for this filter.' : 'No duplicate groups to review.'}</p>`;
      return;
    }
    $('group-list').innerHTML = filtered.map((g, index) => {
      const photos = members(g);
      const preview = app.imageCategory === 'all' ? photos[0] : matchingMembers(g)[0];
      const scope = app.imageCategory === 'all' ? `${photos.length} ${groupFileLabel(photos)}` : `${matchingMembers(g).length} matching · ${photos.length} files`;
      return `<button type="button" class="group-item${key(g.id) === app.active ? ' active' : ''}" data-group="${escape(g.id)}" aria-pressed="${key(g.id) === app.active}" aria-label="Group ${index + 1}, ${escape(kindLabel(g.kind))}, ${escape(scope)}"><img class="group-thumbnail" src="${photoUrl(preview.id, 'thumb')}" alt="" loading="lazy"><span class="group-item-text"><strong>Group ${String(index + 1).padStart(2, '0')}</strong><span>${escape(scope)}</span><span class="group-badge${g.kind === 'similar' ? ' similar' : ''}">${escape(kindLabel(g.kind))}</span></span>${icon('arrow')}</button>`;
    }).join('');
  }
  function renderEmpty() {
    const noFilter = !app.search && app.filter === 'all' && app.mediaFilter === 'all' && app.imageCategory === 'all';
    const title = app.failed ? 'Unable to load Media Review' : app.loading ? 'Opening Media Review…' : app.state?.scanning ? 'Scanning for matching files' : !app.state?.hasScanned ? 'Ready to scan' : noFilter ? 'No duplicates found' : 'No matching groups';
    const description = app.failed ? 'Restart Media Review, then try again.' : app.loading ? 'Files are checked only when you click Scan folder.' : app.state?.scanning ? 'Files are being checked and images compared. Results will appear when the scan finishes.' : !app.state?.hasScanned ? 'Select a folder using Browse, then click Scan folder. Compare matching images, videos, audio and other files here.' : noFilter ? 'No duplicates were found with these settings. Select another folder or adjust the image similarity threshold, then scan again.' : app.imageCategory !== 'all' && !['all', 'image'].includes(app.mediaFilter) ? 'Image categories apply to images. Choose Images or All types in File type to review this category.' : 'Change the filter or search for a different filename.';
    $('comparison-empty').querySelector('h2').textContent = title;
    $('comparison-empty').querySelector('p').textContent = description;
    $('retry-button').hidden = !app.failed;
  }
  function renderMedia(side, photo) {
    const button = $(`${side}-preview-button`);
    const host = $(`${side}-playback`);
    const mode = photo.previewMode || mediaType(photo);
    const image = mode === 'image' && photo.previewAvailable !== false;
    button.hidden = !image;
    host.hidden = image;
    if (image) {
      if (host.dataset.mediaId) { stopPlayback(host, true); host.innerHTML = ''; delete host.dataset.mediaId; }
      setImage($(`${side}-preview`), photoUrl(photo.id), `${side === 'keeper' ? 'Image selected to keep' : 'Image being compared'}: ${photo.name}`);
      button.disabled = false;
      return;
    }
    const signature = `${key(photo.id)}:${mode}`;
    if (host.dataset.mediaId === signature) return;
    stopPlayback(host, true);
    host.dataset.mediaId = signature;
    const title = `${mediaLabel(photo)} · ${photo.format || photo.mime || 'Unknown format'}`;
    if (mode === 'video' || mode === 'audio') {
      const label = `${side === 'keeper' ? 'File to keep' : 'File to compare'}: ${photo.name}`;
      host.className = `playback-preview ${mode}-preview`;
      host.innerHTML = mode === 'video'
        ? `<video controls preload="none" playsinline poster="${photoUrl(photo.id)}" src="${mediaUrl(photo.id)}" aria-label="${escape(label)}"></video><p class="playback-hint">Press Play. Playback preview: first 2 minutes.<br>Preparing the preview may take a moment.</p><p class="playback-error" role="status" hidden></p>`
        : `<div class="audio-art"><img src="${photoUrl(photo.id)}" alt=""><strong>${escape(title)}</strong></div><audio controls preload="none" src="${mediaUrl(photo.id)}" aria-label="${escape(label)}"></audio><p class="playback-hint">Press Play. Playback preview: first 2 minutes.<br>Preparing the preview may take a moment.</p><p class="playback-error" role="status" hidden></p>`;
      const player = host.querySelector(mode);
      player.addEventListener('error', () => {
        const message = host.querySelector('.playback-error');
        message.textContent = 'This file cannot be played here. Exact duplicates can still be reviewed using the filename and location.';
        message.hidden = false;
        host.querySelector('.playback-hint').hidden = true;
      });
    } else {
      host.className = 'playback-preview fallback-preview';
      host.innerHTML = `<img class="fallback-art" src="${photoUrl(photo.id)}" alt=""><strong>${escape(title)}</strong><p>Preview unavailable for this format.</p><small>Exact duplicates can still be reviewed using the filename and location.</small>`;
    }
  }
  function renderPhoto(side, photo, group) {
    const select = $(`${side}-select`);
    if (!photo) {
      select.innerHTML = '<option value="">No eligible copy</option>';
      select.disabled = true;
      select.title = 'Choose a different file to keep to compare a matching copy.';
      $(`${side}-preview-button`).hidden = true;
      $(`${side}-preview`).removeAttribute('src');
      const host = $(`${side}-playback`);
      stopPlayback(host, true);
      host.hidden = false;
      host.className = 'playback-preview fallback-preview';
      host.dataset.mediaId = `no-candidate:${app.imageCategory}`;
      host.innerHTML = `${icon('check')}<strong>No matching copy to remove</strong><p>The matching image is selected to keep.</p><small>Choose another keeper to compare a matching copy.</small>`;
      $(`${side}-meta`).textContent = '';
      $(`${side}-path`).textContent = '';
      $(`${side}-path`).title = '';
      $(`${side}-issue`).hidden = true;
      $(`${side}-issue`).textContent = '';
      return;
    }
    const value = key(photo.id);
    const list = side === 'candidate' ? candidateOptions(group) : members(group);
    const html = list.map(p => `<option value="${escape(p.id)}">${escape(p.name)}</option>`).join('');
    if (select.innerHTML !== html) select.innerHTML = html;
    select.value = value;
    select.disabled = list.length < 2 || app.busy;
    select.title = photo.name;
    renderMedia(side, photo);
    $(`${side}-meta`).innerHTML = metadata(photo).map(value => `<span>${escape(value)}</span>`).join('<span class="meta-dot">•</span>')
      + imageCategories(photo).map(category => `<span class="image-category-tag">${escape(categoryTagLabels[category])}</span>`).join('');
    $(`${side}-path`).textContent = photo.path || '';
    $(`${side}-path`).title = photo.path || '';
    $(`${side}-issue`).hidden = !photo.issue;
    $(`${side}-issue`).textContent = photo.issue || '';
  }
  function renderReview() {
    ensureSelection();
    renderGroupList();
    const incompleteText = [...app.photos.values()].some(photo => mediaType(photo) === 'image' && ['unavailable', 'error'].includes(photo.imageTextStatus));
    $('image-category-guide').textContent = app.imageCategory === 'text' && incompleteText
      ? 'Text detection is unavailable for some images. Check the previews.'
      : app.imageCategory === 'other' && incompleteText ? 'Images without suggested labels. Text detection is unavailable for some images, so labels can be incomplete.'
      : 'Suggested image labels. Check the previews before removing copies.';
    const group = activeGroup();
    $('comparison-content').hidden = !group;
    $('comparison-empty').hidden = Boolean(group);
    if (!group) { stopPlayback(); renderEmpty(); setBusy(app.busy); return; }
    const filtered = groups();
    const index = filtered.findIndex(g => key(g.id) === app.active);
    const keeper = app.photos.get(app.keeper);
    const candidate = app.photos.get(app.candidate);
    $('match-group-number').textContent = `GROUP ${index + 1} OF ${filtered.length}`;
    $('match-badge').className = `badge ${group.kind === 'similar' ? 'similar' : 'exact'}`;
    $('match-badge').textContent = kindLabel(group.kind);
    $('comparison-title').textContent = group.kind === 'similar' ? 'Compare similar images' : 'Compare duplicate files';
    $('match-explanation').textContent = group.kind === 'similar' ? 'These images look similar. Review details, framing and quality before removing a copy.' : group.kind === 'pixels' ? 'These files contain the same image pixels. Their filenames or encoding may differ.' : 'These files have identical contents. Choose the filename and location you want to keep.';
    $('previous-group').disabled = index <= 0;
    $('next-group').disabled = index >= filtered.length - 1;
    $('skip-group').disabled = index >= filtered.length - 1;
    renderPhoto('keeper', keeper, group); renderPhoto('candidate', candidate, group);
    $('keeper-recommendation').hidden = !(mediaType(keeper) === 'image' && pixels(keeper) > pixels(candidate));
    $('candidate-format').textContent = candidate?.format || '';
    $('image-category-scope').hidden = app.imageCategory === 'all';
    $('image-category-scope').textContent = !candidate
      ? `The ${categoryLabels[app.imageCategory]?.toLowerCase() || 'matching image'} selection is kept. Choose another keeper to review a matching copy. No copy is selected for removal.`
      : `${categoryLabels[app.imageCategory]}: only matching copies can be moved to trash. ${matchesCategory(keeper) ? 'Your chosen keeper is preserved.' : 'The keeper is outside this category and stays in its original folder.'}`;
    const photos = app.imageCategory === 'all' ? members(group) : candidateOptions(group);
    const showStrip = app.imageCategory === 'all' ? photos.length > 2 : photos.length > 1;
    $('group-photo-strip').hidden = !showStrip;
    if (showStrip) {
      $('group-photo-strip').innerHTML = `<span class="strip-label">${app.imageCategory === 'all' ? 'Other copies' : 'Matching copies'}</span>${photos.map(p => `<button class="strip-photo${key(p.id) === app.keeper ? ' keep' : key(p.id) === app.candidate ? ' compare' : ''}" type="button" data-candidate="${escape(p.id)}" title="${escape(p.name)}" aria-label="Compare ${escape(p.name)}" aria-pressed="${key(p.id) === app.candidate}"><img src="${photoUrl(p.id, 'thumb')}" alt="" loading="lazy"></button>`).join('')}`;
    }
    setBusy(app.busy);
  }
  function highlightedSnippet(text, query) {
    const content = String(text || '').slice(0, 180);
    const words = [...new Set(String(query || '').match(/[\p{L}\p{N}][\p{L}\p{N}\p{M}_-]*/gu) || [])]
      .sort((a, b) => b.length - a.length).slice(0, 20);
    if (!words.length) return escape(content);
    const pattern = new RegExp(words.map(word => word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'giu');
    let html = '', position = 0;
    for (const match of content.matchAll(pattern)) {
      html += escape(content.slice(position, match.index)) + `<mark>${escape(match[0])}</mark>`;
      position = match.index + match[0].length;
    }
    return html + escape(content.slice(position));
  }
  function invalidateLibrary(message = 'The library changed. Click Search to update your results.') {
    if (!app.libraryResult && !app.libraryRequest) return;
    app.libraryResult = null;
    app.libraryRequest = null;
    app.libraryDirty = false;
    app.libraryMessage = message;
    app.librarySignature = '';
    if ($('library-preview-dialog').open) $('library-preview-dialog').close();
    renderLibrary();
  }
  function renderLibrary() {
    const result = app.libraryResult;
    const rows = Array.isArray(result?.results) ? result.results : [];
    const ready = Boolean(app.state?.hasScanned && app.state?.root && !app.state?.scanning);
    const total = Number(result?.total || 0);
    $('library-results-title').textContent = result ? `Results for “${result.query || app.libraryRequest?.query || ''}”` : 'Your scanned library';
    $('library-result-count').textContent = result ? `${total.toLocaleString()} ${total === 1 ? 'file' : 'files'}` : ready ? `${app.photos.size.toLocaleString()} scanned files` : '';
    $('library-status').textContent = app.libraryBusy ? 'Searching your scanned files…' : app.libraryDirty && result ? 'Search options changed. Click Search to update these results.' : app.libraryMessage;
    const messages = [];
    if (result) {
      const mode = app.libraryRequest?.mode || result.mode || 'all';
      if (mode === 'all' || mode === 'text') messages.push(`Text indexed in ${Number(result.textIndexedImages || 0).toLocaleString()} images. Recognition depends on image quality and installed Windows OCR languages.`);
      if (mode === 'all' || mode === 'visual') {
        messages.push(result.visualAvailable
          ? `Objects and scenes indexed in ${Number(result.visualIndexedImages || 0).toLocaleString()} images. Visual suggestions can be approximate; check the preview.`
          : result.visualReason || 'Object and scene search is unavailable. Text and filename search remain available.');
      }
      if (Array.isArray(result.notes)) messages.push(...result.notes.filter(note => typeof note === 'string'));
    }
    $('library-index-note').hidden = !messages.length;
    $('library-index-note').querySelector('span').textContent = messages.join(' ');
    const signature = JSON.stringify([result?.revision, result?.query, result?.offset, rows.map(photo => [photo.id, photo.matches, photo.snippet]), app.libraryBusy]);
    if (signature !== app.librarySignature) {
      app.librarySignature = signature;
      $('library-results').innerHTML = rows.map(photo => {
        const matches = Array.isArray(photo.matches) ? photo.matches : [];
        const badges = matches.map(match => ({ text: ['text', 'Text match'], filename: ['filename', 'Filename match'], visual: ['visual', 'Visual suggestion'] }[match]))
          .filter(Boolean).map(([kind, label]) => `<span class="library-match ${kind}">${escape(label)}</span>`).join('');
        const snippet = photo.snippet ? `<p class="library-snippet">${highlightedSnippet(photo.snippet, result.query || app.libraryRequest?.query)}</p>` : '';
        return `<article class="library-card"><button class="library-thumbnail" type="button" data-library-preview="${escape(photo.id)}" aria-label="Preview ${escape(photo.name)}"><img src="${photoUrl(photo.id, 'thumb')}" alt="" loading="lazy"><span>${icon('expand')}Preview</span></button><div class="library-card-content"><div class="library-match-list">${badges}<span class="library-file-type">${escape(mediaLabel(photo))}</span></div><h3 title="${escape(photo.name)}">${escape(photo.name)}</h3><p class="library-file-path" title="${escape(photo.path || photo.name)}">${escape(photo.path || photo.name)}</p><p class="library-file-meta">${escape(metadata(photo).join(' · '))}</p>${snippet}<div class="library-card-actions"><button class="button secondary small" type="button" data-library-preview="${escape(photo.id)}">${icon('expand')}Preview</button><button class="button secondary small" type="button" data-show-file="${escape(photo.id)}">${icon('folder')}Show in folder</button></div></div></article>`;
      }).join('');
    }
    $('library-empty').hidden = rows.length > 0;
    $('library-empty-title').textContent = app.libraryBusy ? 'Searching your library' : app.loading ? 'Opening Media Review…' : app.state?.scanning ? 'Scanning your files' : !ready ? 'Scan a folder to search' : result ? 'No matching files' : app.libraryMessage ? 'Run your search again' : 'Find a file by what is inside';
    $('library-empty-description').textContent = app.libraryBusy ? 'Results will appear here when your search finishes.' : app.state?.scanning ? 'Wait for the current scan to finish, then enter a word or an image description.' : !ready ? 'Choose a folder above, then click Scan folder. Search includes every scanned file, even files without duplicates.' : result ? 'Try a different word or description, or choose a wider file type or image category. Text recognition and visual suggestions may miss some images.' : 'Search words in images, describe an object or scene, or look up a filename. Every scanned file is included, even if it has no duplicates.';
    $('library-pagination').hidden = !result || total <= 0;
    const offset = Number(result?.offset || 0);
    $('library-page-summary').textContent = result ? `${(offset + 1).toLocaleString()}–${(offset + rows.length).toLocaleString()} of ${total.toLocaleString()} files` : '';
    setBusy(app.busy);
  }
  function libraryOptions(offset = 0) {
    return { query: $('library-query').value.trim(), mode: $('library-mode').value, mediaType: $('library-media-type').value, category: $('library-category').value, offset, limit: 50 };
  }
  async function searchLibrary(offset = 0, paging = false) {
    if (app.libraryBusy || app.busy || app.loading || app.failed || app.state?.scanning || !app.state?.hasScanned || !app.state?.root) return;
    const body = paging && app.libraryRequest ? { ...app.libraryRequest, offset } : libraryOptions(offset);
    if (!body.query) { $('library-query').focus(); return; }
    const revision = app.state?.revision;
    hideError(); stopPlayback(); app.libraryBusy = true; app.libraryMessage = ''; app.libraryResult = null;
    app.libraryDirty = false; app.librarySignature = ''; app.libraryRequest = body; renderLibrary();
    try {
      const result = await request('/api/search', body);
      if (app.state?.scanning || (revision != null && app.state?.revision !== revision)
        || (result.revision != null && app.state?.revision != null && result.revision !== app.state.revision)) {
        invalidateLibrary('The library changed while searching. Click Search to try again.');
        return;
      }
      app.libraryResult = result;
      // The state map includes every scanned file, including files without duplicate groups.
      for (const photo of result.results || []) if (!app.photos.has(key(photo.id))) app.photos.set(key(photo.id), photo);
    } catch (e) {
      app.libraryMessage = e.message;
      app.libraryRequest = null;
      error(e.message);
    } finally { app.libraryBusy = false; renderLibrary(); }
  }
  function changeLibraryOptions() {
    app.libraryDirty = Boolean(app.libraryResult);
    renderLibrary();
  }
  function openLibraryPreview(id) {
    const photo = app.photos.get(key(id));
    if (!photo || app.libraryBusy || app.state?.scanning) return;
    stopPlayback();
    if (mediaType(photo) === 'image' && photo.previewMode !== 'fallback' && photo.previewAvailable !== false) { openImage(photo); return; }
    $('library-preview-title').textContent = photo.name;
    $('library-preview-details').textContent = metadata(photo).join(' · ');
    $('library-preview-path').textContent = photo.path || photo.name;
    const host = $('library-preview-body');
    const mode = photo.previewMode || mediaType(photo);
    if ((mode === 'video' || mode === 'audio') && photo.previewAvailable !== false) {
      host.className = `library-preview-body ${mode}`;
      const player = mode === 'video' ? `<video controls preload="none" playsinline poster="${photoUrl(photo.id)}" src="${mediaUrl(photo.id)}" aria-label="Preview ${escape(photo.name)}"></video>`
        : `<img class="library-audio-art" src="${photoUrl(photo.id)}" alt=""><audio controls preload="none" src="${mediaUrl(photo.id)}" aria-label="Preview ${escape(photo.name)}"></audio>`;
      host.innerHTML = `${player}<p class="playback-hint">Press Play. Playback preview: first 2 minutes.<br>Preparing the preview may take a moment.</p><p class="playback-error" role="status" hidden></p>`;
      host.querySelector(mode).addEventListener('error', () => {
        const note = host.querySelector('.playback-error');
        note.textContent = 'This file cannot be played here. Use Show in folder to open it with another application.';
        note.hidden = false;
        host.querySelector('.playback-hint').hidden = true;
      });
    } else {
      host.className = 'library-preview-body fallback';
      host.innerHTML = `<img class="library-audio-art" src="${photoUrl(photo.id)}" alt=""><h3>Preview unavailable for this format</h3><p>Use Show in folder to find this file and open it with another application.</p>`;
    }
    $('library-preview-dialog').showModal();
  }
  async function showLibraryFile(id, button) {
    if (app.busy || app.libraryBusy || app.state?.scanning) return;
    hideError(); button.disabled = true;
    try { await request('/api/show-file', { id }); }
    catch (e) { error(e.message); }
    finally { setBusy(app.busy); }
  }
  function renderState() {
    const state = app.state;
    if (!state) { renderReview(); return; }
    $('nav-group-count').textContent = allGroups().length;
    $('nav-trash-count').textContent = state.trashCount ?? 0;
    $('stat-photos').textContent = state.hasScanned || state.scanning ? Number(state.stats?.total ?? state.photos?.length ?? 0).toLocaleString() : '—';
    $('stat-exact').textContent = state.hasScanned ? Number(state.stats?.exactGroups ?? allGroups().filter(g => g.kind !== 'similar').length).toLocaleString() : '—';
    $('stat-similar').textContent = state.hasScanned ? Number(state.stats?.similarGroups ?? allGroups().filter(g => g.kind === 'similar').length).toLocaleString() : '—';
    $('stat-space').textContent = state.hasScanned ? bytes(state.stats?.duplicateBytes ?? 0) : '—';
    if (document.activeElement !== $('folder-path') && !$('folder-path').dataset.edited) $('folder-path').value = state.root || '';
    $('scan-progress').hidden = !state.scanning;
    const progress = state.progress || {};
    const total = Number(progress.total || 0), done = Number(progress.done || 0);
    $('scan-label').textContent = progress.phase ? `${progress.phase.charAt(0).toUpperCase()}${progress.phase.slice(1)}…` : 'Scanning your files…';
    $('scan-numbers').textContent = total ? `${Math.min(done, total).toLocaleString()} / ${total.toLocaleString()}` : '';
    $('progress-fill').style.width = total ? `${Math.min(100, (done / total) * 100)}%` : '30%';
    $('progress-fill').parentElement.classList.toggle('indeterminate', !total);
    const issues = state.errors || [];
    $('scan-errors').hidden = !issues.length;
    $('scan-errors-summary').textContent = `${issues.length} ${issues.length === 1 ? 'file could' : 'files could'} not be fully read — view details`;
    $('scan-errors-list').innerHTML = issues.map(e => `<div class="scan-error-item"><strong>${escape(e.name || 'File')}</strong><p>${escape(e.message || 'Could not read this file.')}</p></div>`).join('');
    if (app.libraryResult && state.revision != null && app.libraryResult.revision != null && state.revision !== app.libraryResult.revision) invalidateLibrary();
    renderReview();
    renderLibrary();
  }
  async function refreshState() {
    clearTimeout(app.poll);
    try {
      const previousScanning = app.state?.scanning;
      app.state = await request('/api/state');
      app.photos = new Map((app.state.photos || []).map(p => [key(p.id), p]));
      app.loading = false; app.failed = false;
      if (previousScanning && !app.state.scanning) { toast(`Scan complete. ${allGroups().length} matching ${allGroups().length === 1 ? 'group' : 'groups'} found.`); app.listSignature = ''; }
      renderState();
      if (app.state.scanning) app.poll = setTimeout(refreshState, 850);
    } catch (e) {
      app.loading = false; app.failed = true; app.listSignature = '';
      error(e.message); renderReview();
      if (app.state?.scanning) app.poll = setTimeout(refreshState, 3000);
    }
  }
  function switchView(view) {
    stopPlayback();
    app.view = view;
    for (const name of ['review', 'library', 'trash']) {
      const selected = view === name;
      $(`${name}-view`).hidden = !selected;
      $(`${name}-nav`).classList.toggle('active', selected);
      if (selected) $(`${name}-nav`).setAttribute('aria-current', 'page');
      else $(`${name}-nav`).removeAttribute('aria-current');
    }
    const heading = {
      review: ['Duplicate media review', 'Find matching files, compare previews and choose which copy to keep.'],
      library: ['Search your media library', 'Find scanned files by words in images, objects and scenes, or their filenames.'],
      trash: ['Recently deleted', 'Review removed files and restore them to their original folders.']
    }[view];
    $('page-title').textContent = heading[0];
    $('page-subtitle').textContent = heading[1];
    if (view === 'trash') loadTrash();
    if (view === 'library') renderLibrary();
  }
  async function loadTrash() {
    if (app.trashLoading) return;
    app.trashLoading = true;
    $('refresh-trash').disabled = true;
    $('trash-list').innerHTML = '<div class="trash-empty"><p>Loading recently deleted files…</p></div>';
    try {
      const result = await request('/api/trash');
      const items = result.items || [];
      if (!items.length) $('trash-list').innerHTML = `<div class="trash-empty">${icon('restore')}<h3>No removed files</h3><p>Copies moved to trash appear here.<br>Restore them to their original folders when needed.</p></div>`;
      else $('trash-list').innerHTML = items.map(item => {
        const date = item.deletedAt ? new Date(item.deletedAt) : null;
        const deleted = date && !Number.isNaN(date.getTime()) ? ` · Deleted ${date.toLocaleString()}` : '';
        const preview = typeof item.previewUrl === 'string' && item.previewUrl.startsWith('/') ? item.previewUrl : '';
        return `<article class="trash-item">${preview ? `<img src="${escape(preview)}" alt="" loading="lazy">` : `<span class="stat-icon neutral">${icon('photo')}</span>`}<div class="trash-item-content"><strong>${escape(item.name)}</strong><p>${escape(item.originalPath)}</p><span>${escape(mediaLabel(item))} · ${escape(bytes(item.size))}${escape(deleted)}</span></div><button type="button" class="button secondary small" data-restore="${escape(item.id)}">${icon('restore')}Restore file</button></article>`;
      }).join('');
      setBusy(app.busy);
    } catch (e) { $('trash-list').innerHTML = '<div class="trash-empty"><p>Could not load recently deleted files. Use Refresh to try again.</p></div>'; error(e.message); }
    finally { app.trashLoading = false; $('refresh-trash').disabled = false; }
  }
  async function restore(id) {
    if (app.busy || app.state?.scanning) return;
    setBusy(true); hideError(); $('toast-undo').disabled = true;
    try {
      await request('/api/restore', { id });
      await refreshState();
      toast('File restored to its original folder.');
      if (app.view === 'trash') await loadTrash();
    } catch (e) { error(e.message); $('toast-undo').disabled = false; }
    finally { setBusy(false); }
  }
  function bulkScopeLabel() {
    const labels = [app.filter === 'exact' ? 'Exact matches' : app.filter === 'similar' ? 'Similar images' : 'All matches'];
    if (app.mediaFilter !== 'all') labels.push($('media-filter').selectedOptions[0].textContent);
    if (app.imageCategory !== 'all') labels.push(`${categoryLabels[app.imageCategory]} copies only`);
    if (app.search) labels.push(`Search: “${$('search').value.trim()}”`);
    return labels.join(' · ');
  }
  function renderBulkPlan(plan, scope, includesSimilar, imageCategory) {
    const count = Number(plan.count || 0), groupCount = Number(plan.groupCount || 0);
    $('bulk-delete-description').textContent = `${scope}. Review ${count.toLocaleString()} ${count === 1 ? 'copy' : 'copies'} from ${groupCount.toLocaleString()} visible matching ${groupCount === 1 ? 'group' : 'groups'} before moving them to recoverable trash.`;
    document.querySelector('#bulk-delete-dialog .dialog-note').textContent = imageCategory === 'all'
      ? 'Your selected keeper is preserved. Other groups keep their highest-resolution or largest file. Overlapping groups may keep additional files. Restore removed copies from Recently deleted.'
      : 'Only copies in the selected image category are included. Your selected keeper is preserved. Other groups keep a file outside this category when available. Overlapping groups may keep additional files. Restore removed copies from Recently deleted.';
    $('bulk-similar-warning').hidden = !includesSimilar;
    $('bulk-file-count').textContent = count.toLocaleString();
    $('bulk-total-size').textContent = bytes(plan.totalBytes || 0);
    $('bulk-keep-count').textContent = Number(plan.keepCount || 0).toLocaleString();
    $('bulk-empty-plan').hidden = count > 0;
    $('bulk-files-heading').hidden = !count;
    $('bulk-file-list').innerHTML = (plan.files || []).map(file => `<article class="bulk-file-item">${icon('trash')}<div><strong>${escape(file.name)}</strong><p>${escape(file.path)}</p><small>Keeping: ${escape(file.keepName)}${file.keepPath ? `<br>${escape(file.keepPath)}` : ''}</small></div><span>${escape(bytes(file.size))}</span></article>`).join('');
    $('bulk-kept-list').innerHTML = (plan.kept || []).map(file => `<article class="bulk-file-item kept">${icon('check')}<div><strong>${escape(file.name)}</strong><p>${escape(file.path)}</p></div><span>${escape(bytes(file.size))}</span></article>`).join('');
    $('bulk-plan-list').scrollTop = 0;
  }
  async function openBulkDelete() {
    if (app.busy || app.loading || app.failed || app.state?.scanning) return;
    const visibleGroups = groups();
    if (!visibleGroups.length) return;
    const scope = bulkScopeLabel();
    const imageCategory = app.imageCategory;
    const includesSimilar = visibleGroups.some(group => group.kind === 'similar');
    const body = { groupIds: visibleGroups.map(group => group.id) };
    const current = activeGroup();
    if (app.imageCategory !== 'all') {
      body.candidateIds = [...new Set(visibleGroups.flatMap(group => matchingMembers(group).map(photo => key(photo.id))))];
      body.keepers = Object.fromEntries(visibleGroups.map(group => [group.id, key(defaultKeeper(group).id)]));
    }
    if (current && app.keeper != null) body.keepers = { ...body.keepers, [current.id]: app.keeper };
    stopPlayback(); hideError(); app.bulkPlanning = true; setBusy(true);
    try {
      const plan = await request('/api/bulk-delete-plan', body);
      app.pendingBulkDelete = plan;
      renderBulkPlan(plan, scope, includesSimilar, imageCategory);
      $('bulk-delete-dialog').showModal();
      $('cancel-bulk-delete').focus();
    } catch (e) { error(e.message); await refreshState(); }
    finally { app.bulkPlanning = false; setBusy(false); }
  }
  async function reportBulkResult(result) {
    if (result.state) {
      app.state = result.state;
      app.photos = new Map((app.state.photos || []).map(photo => [key(photo.id), photo]));
      app.loading = false; app.failed = false; app.listSignature = '';
      renderState();
    } else await refreshState();
    const moved = Number(result.deletedCount || 0), failures = result.failures || [];
    toast(`${moved.toLocaleString()} ${moved === 1 ? 'copy moved' : 'copies moved'} to trash. Restore copies from Recently deleted.`);
    if (failures.length) {
      const details = failures.slice(0, 3).map(file => `${file.name || 'File'}: ${file.error || file.message || 'Could not move this file.'}`).join(' ');
      error(`${moved.toLocaleString()} ${moved === 1 ? 'copy was' : 'copies were'} moved. ${failures.length.toLocaleString()} ${failures.length === 1 ? 'file could' : 'files could'} not be moved. ${details}${failures.length > 3 ? ' Review the remaining matches before trying again.' : ''}`);
    }
    if (app.view === 'trash') await loadTrash();
  }
  async function confirmBulkDelete() {
    const plan = app.pendingBulkDelete;
    if (app.busy || app.state?.scanning || !plan?.planId || !plan.count) return;
    hideError(); app.bulkMoving = true; setBusy(true); $('cancel-bulk-delete').disabled = true;
    try {
      const result = await request('/api/bulk-delete', { planId: plan.planId, confirmed: true });
      $('bulk-delete-dialog').close();
      await reportBulkResult(result);
    } catch (e) {
      $('bulk-delete-dialog').close();
      if (Number(e.result?.deletedCount || 0) > 0) await reportBulkResult(e.result);
      else { await refreshState(); error(`${e.message} Review the updated matches and prepare a new removal plan.`); }
    } finally { app.bulkMoving = false; $('cancel-bulk-delete').disabled = false; setBusy(false); }
  }
  function openImage(photo) {
    if (!photo || mediaType(photo) !== 'image' || photo.previewMode === 'fallback' || photo.previewAvailable === false) return;
    $('image-title').textContent = photo.name;
    $('image-details').textContent = `${dimensions(photo)} · ${bytes(photo.size)}`;
    setImage($('full-image'), photoUrl(photo.id, 'full'), photo.name);
    $('image-stage').classList.remove('actual-size');
    $('zoom-button').setAttribute('aria-pressed', 'false');
    $('zoom-button').textContent = 'Actual size';
    $('image-dialog').showModal();
  }
  $('review-nav').addEventListener('click', () => switchView('review'));
  $('library-nav').addEventListener('click', () => switchView('library'));
  $('trash-nav').addEventListener('click', () => switchView('trash'));
  $('library-search-form').addEventListener('submit', e => { e.preventDefault(); });
  $('library-search-button').addEventListener('click', () => searchLibrary());
  $('library-query').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); searchLibrary(); } });
  $('library-query').addEventListener('input', changeLibraryOptions);
  ['library-mode', 'library-media-type', 'library-category'].forEach(id => $(id).addEventListener('change', changeLibraryOptions));
  $('library-previous').addEventListener('click', () => searchLibrary(Math.max(0, Number(app.libraryResult?.offset || 0) - Number(app.libraryResult?.limit || 50)), true));
  $('library-next').addEventListener('click', () => searchLibrary(Number(app.libraryResult?.offset || 0) + Number(app.libraryResult?.limit || 50), true));
  $('library-results').addEventListener('click', e => {
    const preview = e.target.closest('[data-library-preview]');
    const folder = e.target.closest('[data-show-file]');
    if (preview && !preview.disabled) openLibraryPreview(preview.dataset.libraryPreview);
    else if (folder && !folder.disabled) showLibraryFile(folder.dataset.showFile, folder);
  });
  $('close-library-preview').addEventListener('click', () => $('library-preview-dialog').close());
  $('library-preview-dialog').addEventListener('close', () => { stopPlayback($('library-preview-body'), true); $('library-preview-body').innerHTML = ''; });
  $('error-close').addEventListener('click', hideError);
  $('retry-button').addEventListener('click', () => { app.loading = true; app.failed = false; hideError(); renderReview(); refreshState(); });
  $('threshold').addEventListener('input', () => { $('threshold-output').textContent = `${$('threshold').value}%`; });
  const scanSettings = document.querySelector('.scan-settings');
  $('options-button').addEventListener('click', () => { $('settings-slot').appendChild(scanSettings); $('settings-dialog').showModal(); });
  $('close-settings').addEventListener('click', () => $('settings-dialog').close());
  $('settings-dialog').addEventListener('close', () => { document.querySelector('.sidebar').insertBefore(scanSettings, document.querySelector('.sidebar-bottom')); });
  $('folder-path').addEventListener('input', () => { $('folder-path').dataset.edited = 'true'; setBusy(app.busy); });
  $('browse-button').addEventListener('click', async () => {
    if (app.busy || app.state?.scanning) return;
    hideError(); setBusy(true);
    try {
      const result = await request('/api/pick-folder', {});
      const path = result.path || result.root;
      if (path) { $('folder-path').value = path; $('folder-path').dataset.edited = 'true'; $('scan-button').focus(); }
    } catch (e) { error(e.message); }
    finally { setBusy(false); }
  });
  $('scan-button').addEventListener('click', async () => {
    if (app.busy || app.state?.scanning) return;
    const path = $('folder-path').value.trim();
    if (!path) { error('Choose a folder first.'); $('folder-path').focus(); return; }
    hideError(); stopPlayback(); setBusy(true);
    try {
      await request('/api/scan', { path, recursive: $('recursive').checked, threshold: Number($('threshold').value), includeAllFiles: $('include-all-files').checked });
      invalidateLibrary('A new scan started. Wait for it to finish, then click Search.');
      delete $('folder-path').dataset.edited;
      app.active = null; app.keeper = null; app.candidate = null; app.keeperSelectedByUser = false; app.listSignature = '';
      switchView('review'); await refreshState();
    } catch (e) { error(e.message); }
    finally { setBusy(false); }
  });
  document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => {
    stopPlayback();
    app.filter = button.dataset.filter;
    document.querySelectorAll('[data-filter]').forEach(b => { b.classList.toggle('selected', b === button); b.setAttribute('aria-pressed', String(b === button)); });
    renderReview();
  }));
  $('media-filter').addEventListener('change', () => { stopPlayback(); app.mediaFilter = $('media-filter').value; renderReview(); });
  $('image-category-filter').addEventListener('change', () => { stopPlayback(); app.imageCategory = $('image-category-filter').value; renderReview(); });
  $('search').addEventListener('input', () => { stopPlayback(); app.search = $('search').value.trim().toLowerCase(); renderReview(); });
  $('group-list').addEventListener('click', e => { const button = e.target.closest('[data-group]'); if (button) chooseGroup(button.dataset.group); });
  $('previous-group').addEventListener('click', () => moveGroup(-1));
  $('next-group').addEventListener('click', () => moveGroup(1));
  $('skip-group').addEventListener('click', () => moveGroup(1));
  $('keeper-select').addEventListener('change', () => {
    stopPlayback();
    const old = app.keeper; app.keeper = $('keeper-select').value; app.keeperSelectedByUser = true;
    if (app.keeper === app.candidate) app.candidate = old;
    renderReview();
  });
  function chooseCandidate(id) {
    const photo = app.photos.get(key(id));
    if (!photo || !matchesCategory(photo) || !members(activeGroup()).some(member => key(member.id) === key(id))
      || (app.imageCategory !== 'all' && key(id) === app.keeper)) return;
    stopPlayback();
    const old = app.candidate; app.candidate = key(id);
    if (app.candidate === app.keeper) { app.keeper = old; app.keeperSelectedByUser = true; }
    renderReview();
  }
  $('candidate-select').addEventListener('change', () => chooseCandidate($('candidate-select').value));
  $('group-photo-strip').addEventListener('click', e => { const button = e.target.closest('[data-candidate]'); if (button) chooseCandidate(button.dataset.candidate); });
  $('keeper-preview-button').addEventListener('click', () => openImage(app.photos.get(app.keeper)));
  $('candidate-preview-button').addEventListener('click', () => openImage(app.photos.get(app.candidate)));
  $('close-image').addEventListener('click', () => $('image-dialog').close());
  $('zoom-button').addEventListener('click', () => {
    const actual = $('image-stage').classList.toggle('actual-size');
    $('zoom-button').setAttribute('aria-pressed', String(actual));
    $('zoom-button').textContent = actual ? 'Fit to screen' : 'Actual size';
  });
  $('delete-button').addEventListener('click', () => {
    if (app.busy || app.state?.scanning) return;
    const group = activeGroup(), candidate = app.photos.get(app.candidate), keeper = app.photos.get(app.keeper);
    if (!group || !candidate || !keeper || !canRemoveCandidate()) return;
    stopPlayback();
    app.pendingDelete = { id: candidate.id, groupId: group.id, keepId: keeper.id };
    setImage($('delete-preview'), photoUrl(candidate.id, 'thumb'), candidate.name);
    $('delete-filename').textContent = candidate.name; $('delete-filesize').textContent = metadata(candidate).join(' · ');
    $('delete-keeper').textContent = keeper.name;
    $('delete-dialog').showModal();
    $('cancel-delete').focus();
  });
  $('cancel-delete').addEventListener('click', () => $('delete-dialog').close());
  $('delete-dialog').addEventListener('close', () => { app.pendingDelete = null; });
  $('bulk-delete-button').addEventListener('click', openBulkDelete);
  $('cancel-bulk-delete').addEventListener('click', () => { if (!app.bulkMoving) $('bulk-delete-dialog').close(); });
  $('bulk-delete-dialog').addEventListener('close', () => { app.pendingBulkDelete = null; setBusy(app.busy); });
  $('confirm-bulk-delete').addEventListener('click', confirmBulkDelete);
  $('confirm-delete').addEventListener('click', async () => {
    if (!app.pendingDelete || app.busy) return;
    const pending = { ...app.pendingDelete };
    const previousGroups = groups();
    const index = previousGroups.findIndex(g => key(g.id) === key(pending.groupId));
    hideError(); setBusy(true); $('cancel-delete').disabled = true;
    try {
      const result = await request('/api/delete', pending);
      $('delete-dialog').close();
      await refreshState();
      if (!groups().some(g => key(g.id) === key(pending.groupId))) {
        const next = groups()[Math.min(index, groups().length - 1)];
        if (next) chooseGroup(next.id);
      }
      const trashId = result.trashId ?? result.item?.id ?? result.deleted?.id;
      toast('File moved to Recently deleted.', trashId ?? null);
    } catch (e) { $('delete-dialog').close(); error(e.message); await refreshState(); }
    finally { $('cancel-delete').disabled = false; setBusy(false); }
  });
  $('refresh-trash').addEventListener('click', loadTrash);
  $('trash-list').addEventListener('click', e => { const button = e.target.closest('[data-restore]'); if (button) restore(button.dataset.restore); });
  $('toast-undo').addEventListener('click', () => { if (app.undoId != null) restore(app.undoId); });
  $('toast-close').addEventListener('click', () => { clearTimeout(app.toastTimer); $('toast').hidden = true; });
  document.addEventListener('keydown', e => {
    if (app.view !== 'review' || document.querySelector('dialog[open]') || e.altKey || e.ctrlKey || e.metaKey || ['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON'].includes(document.activeElement?.tagName)) return;
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); moveGroup(e.key === 'ArrowLeft' ? -1 : 1); }
  });
  for (const id of ['delete-dialog', 'bulk-delete-dialog', 'image-dialog', 'library-preview-dialog']) $(id).addEventListener('click', e => {
    if (e.target !== $(id)) return;
    const rect = $(id).getBoundingClientRect();
    if (e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom) { if (id === 'image-dialog' || id === 'library-preview-dialog' || !app.busy) $(id).close(); }
  });
  $('delete-dialog').addEventListener('cancel', e => { if (app.busy) e.preventDefault(); });
  $('bulk-delete-dialog').addEventListener('cancel', e => { if (app.busy) e.preventDefault(); });
  window.addEventListener('beforeunload', () => { stopPlayback(); clearTimeout(app.poll); clearTimeout(app.toastTimer); });
  renderReview();
  renderLibrary();
  refreshState();
})();
