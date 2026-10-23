(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let hooks, pending = false, row = 0, folderSignature = '', pairSignature = '', syncing = false;
  const optionFields = ['scan-cache', 'match-transforms', 'match-crop', 'match-video', 'video-threshold', 'match-audio-tags', 'audio-tag-fields', 'match-folders', 'comparison-scope'];
  const roles = value => `<option value="normal" ${value === 'normal' ? 'selected' : ''}>Review &amp; manage</option><option value="protected" ${value === 'protected' ? 'selected' : ''}>Protected originals</option><option value="reference" ${value === 'reference' ? 'selected' : ''}>Reference library</option>`;
  function addRoot(path = '', role = 'normal') {
    const item = document.createElement('div'); item.className = 'workspace-root'; item.dataset.rootRow = String(++row);
    item.innerHTML = `<input class="extra-root-path" aria-label="Additional folder path" placeholder="Folder or drive path" autocomplete="off" spellcheck="false" value="${esc(path)}"><select class="extra-root-role" aria-label="Folder role">${roles(role)}</select><button class="button secondary small" data-browse-root type="button">Browse</button><button class="icon-button" data-remove-root type="button" aria-label="Remove folder">×</button>`;
    $('additional-roots').append(item); setBusy(hooks.isBusy());
  }
  function readRoots() {
    const entries = [{path: $('folder-path').value.trim(), role: $('primary-root-role').value},
      ...Array.from(document.querySelectorAll('.workspace-root')).map(item => ({path:item.querySelector('input').value.trim(), role:item.querySelector('select').value}))];
    return entries.map(item => {
      if (!item.path) throw new Error('Choose a path for every scan folder, or remove the empty row.');
      return {path:item.path, protected:item.role !== 'normal', reference:item.role === 'reference'};
    });
  }
  function readOptions() {
    if (!$('scan-cache')) return {};
    if (!$('video-threshold').checkValidity()) throw new Error('Video similarity must be between 80 and 99.');
    return {scanCache:$('scan-cache').checked, imageTransforms:$('match-transforms').checked, cropMatching:$('match-crop').checked,
      videoMatching:$('match-video').checked, videoThreshold:Number($('video-threshold').value),
      audioTagMatching:$('match-audio-tags').checked, audioTagFields:$('audio-tag-fields').value.split(','),
      folderMatching:$('match-folders').checked, comparisonScope:$('comparison-scope').value};
  }
  function applyState(state) {
    const roots = state.roots || (state.root ? [{path:state.root}] : []);
    $('additional-roots').replaceChildren();
    $('primary-root-role').value = roots[0]?.reference ? 'reference' : roots[0]?.protected ? 'protected' : 'normal';
    roots.slice(1).forEach(item => addRoot(item.path, item.reference ? 'reference' : item.protected ? 'protected' : 'normal'));
    const options = state.scanOptions || {};
    for (const [id,key] of [['scan-cache','scanCache'],['match-transforms','imageTransforms'],['match-crop','cropMatching'],['match-video','videoMatching'],['match-audio-tags','audioTagMatching'],['match-folders','folderMatching']]) $(id).checked = options[key] ?? (key === 'scanCache');
    $('video-threshold').value = options.videoThreshold ?? 92;
    $('comparison-scope').value = options.comparisonScope || 'all';
    const fields = (options.audioTagFields || ['artist','title','album']).join(',');
    if (Array.from($('audio-tag-fields').options).some(item => item.value === fields)) $('audio-tag-fields').value = fields;
  }
  function setBusy(value) {
    if (!hooks) return;
    const locked = Boolean(value || pending);
    document.querySelectorAll('#workspace-setup input,#workspace-setup select,#workspace-setup button,#v3-matching input,#v3-matching select').forEach(element => {element.disabled = locked;});
    $('resume-interrupted').disabled = locked;
    $('check-interrupted').disabled = locked;
    $('compare-images').disabled = locked || !hooks.getPair().every(photo => photo?.mediaType === 'image' && photo.previewAvailable && !photo.offline);
  }
  async function loadCatalogs() {
    if (pending || hooks.isBusy()) return;
    pending = true; setBusy(true);
    try {
      const result = await hooks.request('/api/sessions/list', {});
      $('catalog-choices').innerHTML = (result.sessions || []).map(item => `<label class="catalog-choice"><input type="checkbox" value="${esc(item.id)}"><span><strong>${esc(item.name)}</strong><small>${esc(item.root)} · ${Number(item.files).toLocaleString()} indexed files</small></span></label>`).join('') || '<p>No saved scans yet. Save a scan to use it as a drive catalog.</p>';
    } catch(error) {hooks.error(error.message);}
    finally {pending = false; setBusy(hooks.isBusy());}
  }
  const catalogIds = () => Array.from(document.querySelectorAll('#catalog-choices input:checked')).map(input => input.value);
  async function checkInterrupted(quiet = false) {
    try {
      const result = await hooks.request('/api/scan/interrupted');
      $('interrupted-scan').hidden = !result.checkpoint;
      $('interrupted-description').textContent = result.checkpoint ? `Unfinished scan: ${result.checkpoint.roots.map(item => item.path).join(' · ')}` : '';
      if (!quiet && !result.checkpoint) hooks.toast('No unfinished scan was found.');
    } catch(error) {if (!quiet) hooks.error(error.message);}
  }
  function renderFolders() {
    const groups = hooks.getState()?.folderGroups || [];
    const signature = JSON.stringify(groups);
    if (signature === folderSignature) return;
    folderSignature = signature;
    $('folder-matches').hidden = !groups.length;
    $('folder-match-count').textContent = `${groups.length.toLocaleString()} matching folder ${groups.length === 1 ? 'group' : 'groups'}`;
    $('folder-match-list').innerHTML = groups.slice(0,100).map(group => `<article><strong>${Number(group.fileCount).toLocaleString()} files in each folder</strong>${group.folders.map(folder => `<div><code>${esc(folder.path)}</code>${folder.reference || folder.protected ? '<span class="folder-protected">Protected</span>' : ''}</div>`).join('')}</article>`).join('');
  }
  const timeLabel = value => `${Math.floor(value / 60)}:${String(Math.floor(value % 60)).padStart(2,'0')}`;
  function renderPair() {
    if (!hooks) return;
    const pair = hooks.getPair();
    const signature = pair.map(photo => photo?.id || '').join(':');
    if (signature === pairSignature) return;
    pairSignature = signature;
    const timelines = pair.map(photo => photo?.videoTimeline || []);
    $('synchronized-timeline').hidden = !timelines.every(items => items.length);
    $('timeline-position').value = '0';
    $('timeline-rows').innerHTML = timelines.every(items => items.length) ? timelines.map((items,index) => `<div class="timeline-row"><strong>${esc(pair[index].name)}</strong><div>${items.map((frame,i) => `<button type="button" data-timeline-index="${i}" title="${esc(timeLabel(frame.time))}"><img src="${esc(frame.thumbnail)}" alt="Frame at ${esc(timeLabel(frame.time))}" loading="lazy"><span>${timeLabel(frame.time)}</span></button>`).join('')}</div></div>`).join('') : '';
    selectFrame(0);
  }
  function selectFrame(value) {
    const rows = document.querySelectorAll('.timeline-row');
    rows.forEach(row => {
      const buttons = Array.from(row.querySelectorAll('button'));
      const selected = Math.round(value / 100 * Math.max(0,buttons.length - 1));
      buttons.forEach((button,index) => button.classList.toggle('timeline-selected', index === selected));
    });
    const videos = Array.from(document.querySelectorAll('.photo-pair video'));
    const pair = hooks.getPair();
    if (videos.length === 2 && videos.every(video => Number.isFinite(video.duration))) videos.forEach((video,index) => {
      const frames = pair[index]?.videoTimeline || [];
      const frame = frames[Math.round(value / 100 * Math.max(0,frames.length - 1))];
      if (frame && frame.time < video.duration) video.currentTime = frame.time;
      else video.pause();
    });
  }
  function render() {
    if (!hooks) return;
    renderFolders(); renderPair(); setBusy(hooks.isBusy());
    const state = hooks.getState();
    $('catalog-mode-note').hidden = !state?.catalogOnly;
    $('cache-summary').textContent = state?.hasScanned && !state.scanning ? `${Number(state.stats?.cacheHits || 0).toLocaleString()} file analyses reused from cache` : '';
    if (state?.interruptedScan && !state.scanning) $('interrupted-scan').hidden = false;
    else if (state?.hasScanned && !state?.scanning && !state?.scanStopped) $('interrupted-scan').hidden = true;
  }
  function showImageCompare() {
    const pair = hooks.getPair();
    if (!pair.every(photo => photo?.mediaType === 'image' && photo.previewAvailable && !photo.offline)) return;
    $('linked-image-grid').innerHTML = pair.map(photo => `<section><strong>${esc(photo.name)}</strong><div class="linked-image-pane"><img src="/api/photo/${encodeURIComponent(photo.id)}?size=full" alt="${esc(photo.name)}" draggable="false"></div></section>`).join('');
    $('linked-zoom').value = '100';
    const panes = Array.from(document.querySelectorAll('.linked-image-pane'));
    panes.forEach((pane,index) => {
      pane.addEventListener('scroll', () => {
        if (syncing) return;
        syncing = true;
        const other = panes[1-index];
        other.scrollLeft = pane.scrollLeft / Math.max(1,pane.scrollWidth-pane.clientWidth) * (other.scrollWidth-other.clientWidth);
        other.scrollTop = pane.scrollTop / Math.max(1,pane.scrollHeight-pane.clientHeight) * (other.scrollHeight-other.clientHeight);
        requestAnimationFrame(() => {syncing = false;});
      });
      let drag;
      pane.addEventListener('pointerdown',event => {drag={x:event.clientX,y:event.clientY,left:pane.scrollLeft,top:pane.scrollTop}; pane.setPointerCapture(event.pointerId);});
      pane.addEventListener('pointermove',event => {if(drag){pane.scrollLeft=drag.left+drag.x-event.clientX;pane.scrollTop=drag.top+drag.y-event.clientY;}});
      pane.addEventListener('pointerup',() => {drag=null;});
      pane.addEventListener('pointercancel',() => {drag=null;});
    });
    $('linked-images-dialog').showModal();
  }
  function init(api) {
    hooks = api;
    const setup = document.createElement('details'); setup.id = 'workspace-setup'; setup.className = 'workspace-setup';
    setup.innerHTML = `<summary>Folders &amp; comparison setup <span>Multiple locations · Protected originals · Offline catalogs</span></summary><div class="workspace-setup-body"><label class="primary-role"><span>Primary folder role</span><select id="primary-root-role">${roles('normal')}</select></label><div id="additional-roots"></div><button id="add-scan-root" class="button secondary small" type="button">+ Add folder or drive</button><p>Protected originals are compared but cannot be renamed, moved or removed. Reference mode compares other folders against your reference library. Selected folders must be separate, without one inside another.</p><details class="catalog-selector"><summary>Compare with saved drive catalogs</summary><p>Use a saved scan as a read-only snapshot, even when its drive is disconnected. Catalog files cannot authorize removing their only available copy.</p><button id="refresh-catalogs" class="button secondary small" type="button">Choose catalogs</button><div id="catalog-choices"></div></details></div>`;
    document.querySelector('.scan-start-guide').after(setup);
    const recovery = document.createElement('div'); recovery.className = 'workspace-recovery';
    recovery.innerHTML = `<button id="check-interrupted" class="button secondary small" type="button">Find unfinished scan</button><span id="cache-summary"></span><div id="interrupted-scan" hidden><div><strong>Continue an unfinished scan</strong><p id="interrupted-description">An unfinished scan is available.</p><small>Completed file analyses are reused. File discovery and comparisons are refreshed before review.</small></div><button id="resume-interrupted" class="button primary small" type="button">Resume unfinished scan</button></div><p id="catalog-mode-note" hidden>Offline catalog mode — file details and matching are available. Reconnect the drive and scan it to preview or change files.</p>`;
    setup.after(recovery);
    const fieldset = document.createElement('fieldset'); fieldset.id = 'v3-matching'; fieldset.className = 'advanced-group';
    fieldset.innerHTML = `<legend>Library comparison</legend><label class="feature-check"><input id="scan-cache" type="checkbox" checked><span><strong>Reuse completed file analysis</strong><small>Speed up repeat scans and continue unfinished scans. Changed files are analyzed again.</small></span></label><label class="feature-check"><input id="match-transforms" type="checkbox"><span>Compare rotated and flipped photos</span></label><label class="feature-check"><input id="match-crop" type="checkbox"><span>Compare cropped photo copies <small>Uses a bounded crop search and can increase scan time.</small></span></label><label class="feature-check"><input id="match-video" type="checkbox"><span><strong>Compare similar videos</strong><small>Compare distributed frames across the video, including reencoded copies. Results are suggestions to review.</small></span></label><label class="inline-option"><span>Video similarity (%)</span><input id="video-threshold" type="number" min="80" max="99" value="92"></label><label class="feature-check"><input id="match-audio-tags" type="checkbox"><span>Match music by audio tags</span></label><label class="wide-option"><span>Music tags to match</span><select id="audio-tag-fields"><option value="artist,title,album">Artist, title and album</option><option value="artist,title">Artist and title</option><option value="title">Title</option></select></label><label class="feature-check"><input id="match-folders" type="checkbox"><span>Find complete duplicate folders</span></label><label class="wide-option"><span>Compare files</span><select id="comparison-scope"><option value="all">Within and across all selected folders</option><option value="cross-root">Between different selected folders only</option><option value="reference">Against reference library folders only</option></select></label>`;
    $('advanced-settings-slot').append(fieldset);
    const toolbar = document.createElement('div'); toolbar.className = 'comparison-extra-tools';
    toolbar.innerHTML = '<button id="compare-images" class="button secondary small" type="button" disabled>Linked image comparison</button>';
    document.querySelector('.photo-pair').before(toolbar);
    const timeline = document.createElement('section'); timeline.id = 'synchronized-timeline'; timeline.hidden = true; timeline.className = 'synchronized-timeline';
    timeline.innerHTML = '<h3>Synchronized timeline</h3><p>Review corresponding sampled positions across both videos. The slider also aligns loaded playback previews.</p><input id="timeline-position" type="range" min="0" max="100" value="0" aria-label="Position in both videos"><div id="timeline-rows"></div>';
    document.querySelector('.photo-pair').after(timeline);
    const folders = document.createElement('section'); folders.id = 'folder-matches'; folders.hidden = true; folders.className = 'folder-matches';
    folders.innerHTML = '<h2>Duplicate folders</h2><p id="folder-match-count"></p><p>These folders have complete matching contents and structure. Review their files above before choosing copies to remove.</p><div id="folder-match-list"></div>';
    $('review-view').append(folders);
    const dialog = document.createElement('dialog'); dialog.id = 'linked-images-dialog'; dialog.className = 'linked-images-dialog';
    dialog.innerHTML = '<div class="linked-toolbar"><h2>Linked image comparison</h2><label>Zoom <input id="linked-zoom" type="range" min="100" max="400" step="10" value="100"></label><button id="close-linked-images" class="button secondary small" type="button">Close</button></div><p>Zoom both images together. Drag or scroll either image to move both views.</p><div id="linked-image-grid"></div>';
    document.body.append(dialog);
    $('add-scan-root').addEventListener('click',() => {if (!hooks.isBusy() && !pending) addRoot();});
    $('additional-roots').addEventListener('click',async event => {
      const root = event.target.closest('.workspace-root'); if (!root || pending || hooks.isBusy()) return;
      if (event.target.closest('[data-remove-root]')) {root.remove(); return;}
      if (!event.target.closest('[data-browse-root]')) return;
      pending = true; setBusy(true);
      try {const result=await hooks.request('/api/pick-folder',{});if(result.path)root.querySelector('input').value=result.path;}
      catch(error){hooks.error(error.message);} finally{pending=false;setBusy(hooks.isBusy());}
    });
    $('refresh-catalogs').addEventListener('click',loadCatalogs);
    $('check-interrupted').addEventListener('click',() => checkInterrupted());
    $('resume-interrupted').addEventListener('click',async () => {
      if (pending || hooks.isBusy()) return;
      pending=true;setBusy(true);
      try {const state=await hooks.request('/api/scan/interrupted-resume',{});await hooks.loadState(state);hooks.switchView('review');hooks.toast('Continuing the unfinished scan using completed file analysis.');}
      catch(error){hooks.error(error.message);} finally{pending=false;setBusy(hooks.isBusy());}
    });
    $('timeline-position').addEventListener('input',event=>selectFrame(Number(event.target.value)));
    $('timeline-rows').addEventListener('click',event=>{const button=event.target.closest('[data-timeline-index]');if(button){const count=button.parentElement.children.length;const value=Number(button.dataset.timelineIndex)/Math.max(1,count-1)*100;$('timeline-position').value=String(value);selectFrame(value);}});
    $('compare-images').addEventListener('click',showImageCompare);
    $('linked-zoom').addEventListener('input',event=>{document.querySelectorAll('.linked-image-pane img').forEach(image=>{image.style.width=`${event.target.value}%`;image.style.maxWidth='none';});});
    $('close-linked-images').addEventListener('click',()=>dialog.close());
    $('reset-advanced').addEventListener('click',()=>{if(!hooks.isBusy())applyState({roots:readRoots(),scanOptions:{}});});
    document.body.addEventListener('click',async event=>{
      const button=event.target.closest('[data-open-catalog]');if(!button||pending||hooks.isBusy())return;
      pending=true;setBusy(true);
      try{const state=await hooks.request('/api/sessions/load',{id:button.dataset.openCatalog,offline:true});await hooks.loadState(state);hooks.switchView('review');hooks.toast('Offline catalog opened for read-only review.');}
      catch(error){hooks.error(error.message);}finally{pending=false;setBusy(hooks.isBusy());}
    });
    checkInterrupted(true);
  }
  window.MediaReviewWorkspace = {init,readRoots,readOptions,applyState,setBusy,render,renderPair,catalogIds,isBusy:()=>pending};
})();
