(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let hooks, pending = false, plan = null, page = 0;
  const labels = {'rename':'Rename files', 'empty-folders':'Remove empty folders', 'hardlinks':'Replace copies with hard links', 'recycle':'Send exact copies to Windows Recycle Bin', 'purge-trash':'Empty app trash permanently', 'recycle-trash':'Move app trash to Windows Recycle Bin'};
  const bytes = n => { n = Number(n || 0); const i = n ? Math.min(4, Math.floor(Math.log(n) / Math.log(1024))) : 0; return `${(n / 1024 ** i).toLocaleString(undefined, {maximumFractionDigits:1})} ${['B','KB','MB','GB','TB'][i]}`; };
  const request = (path, body = {}) => hooks.request(path, body);
  const busy = () => pending || Boolean(hooks.isBusy?.());
  function setBusy(value) {
    const locked = Boolean(value || pending);
    document.querySelectorAll('#tools-view button[data-tool], #tools-view #tools-apply-keepers, #tools-view #tools-report').forEach(b => { b.disabled = locked || !hooks.getState()?.hasScanned || Boolean(b.dataset.tool && hooks.getState()?.catalogOnly); });
    $('tools-confirm').disabled = locked || !plan?.count || (plan.permanent && $('tools-delete-word').value !== 'DELETE');
    document.querySelectorAll('[data-tool-undo]').forEach(b => { b.disabled = locked || Boolean(hooks.getState()?.catalogOnly); });
  }
  function renderPlan() {
    const start = page * 50, files = plan?.files || [];
    $('tools-plan-title').textContent = labels[plan.kind];
    $('tools-plan-summary').textContent = `${plan.count.toLocaleString()} ${plan.kind === 'empty-folders' ? 'folders' : 'files'} · ${bytes(plan.totalSize)}`;
    $('tools-plan-note').textContent = plan.note || (plan.permanent ? 'These recovery files will be deleted permanently. They cannot be restored from Media Review.' : plan.kind.includes('recycle') ? 'Restore files later from Windows Recycle Bin. Windows may show its own confirmation.' : 'Only the paths in this preview are included. Existing files will not be overwritten. Undo is available in File tools history.');
    $('tools-plan-list').innerHTML = files.slice(start, start + 50).map(item => `<article><strong>${esc(item.name)}</strong><code>${esc(item.source)}</code>${item.destination ? `<span>New name</span><code>${esc(item.destination)}</code>` : ''}${item.keeper ? `<span>Keeping</span><code>${esc(item.keeper.source)}</code>` : ''}</article>`).join('') || '<p>No eligible files or folders were found.</p>';
    $('tools-plan-page').textContent = files.length ? `${start + 1}–${Math.min(start + 50, files.length)} of ${files.length}` : '0 files';
    $('tools-plan-previous').disabled = page === 0;
    $('tools-plan-next').disabled = start + 50 >= files.length;
    $('tools-permanent-confirm').hidden = !plan.permanent;
    $('tools-confirm').textContent = plan.permanent ? 'Delete permanently' : labels[plan.kind];
    $('tools-confirm').classList.toggle('danger', Boolean(plan.permanent || plan.kind.includes('recycle')));
    setBusy(busy());
  }
  async function history() {
    if (!hooks.getState()?.hasScanned || busy()) return;
    try {
      const result = await request('/api/tools/history');
      $('tools-history').innerHTML = (result.history || []).map(item => `<article><div><strong>${esc(labels[item.kind] || item.kind)}</strong><p>${esc(new Date(item.createdAt).toLocaleString())} · ${Number(item.completed || 0).toLocaleString()} completed${item.pending ? ` · ${item.pending} interrupted` : ''}</p></div>${item.undoAvailable ? `<button class="button secondary small" data-tool-undo="${esc(item.id)}" type="button">Undo</button>` : '<span>Completed</span>'}</article>`).join('') || '<p class="tools-muted">Your file operations will appear here.</p>';
    } catch (error) { $('tools-history').textContent = error.message; }
  }
  async function preview(kind) {
    if (busy()) return;
    pending = true; setBusy(true); $('tools-status').textContent = 'Preparing a preview…';
    try {
      plan = await request('/api/tools/plan', {kind, template:$('tools-rename-template').value, start:Number($('tools-rename-start').value), rule:$('tools-keep-rule').value, preferredFolder:$('tools-preferred-folder').value, formats:$('tools-formats').value});
      page = 0; $('tools-delete-word').value = ''; renderPlan(); $('tools-plan-dialog').showModal(); $('tools-status').textContent = '';
    } catch (error) { hooks.error(error.message); $('tools-status').textContent = error.message; }
    finally { pending = false; setBusy(hooks.isBusy?.()); }
  }
  async function confirm() {
    if (!plan?.count || busy() || (plan.permanent && $('tools-delete-word').value !== 'DELETE')) return;
    pending = true; setBusy(true); $('tools-confirm').textContent = 'Working…';
    try {
      const result = await request('/api/tools/execute', {planId:plan.planId, confirmed:true});
      $('tools-plan-dialog').close(); plan = null;
      if (result.state) await hooks.loadState(result.state);
      const failures = result.failures || [];
      $('tools-status').textContent = `${Number(result.completed || 0).toLocaleString()} completed.${failures.length ? ` ${failures.length} could not be completed: ${failures.slice(0,3).map(x => `${x.name}: ${x.error}`).join('; ')}` : ''}`;
      hooks.toast(`${Number(result.completed || 0).toLocaleString()} file operations completed.`);
    } catch (error) {
      const result = error.result;
      if (result?.state) {
        $('tools-plan-dialog').close(); plan = null;
        await hooks.loadState(result.state);
        $('tools-status').textContent = `${Number(result.completed || 0).toLocaleString()} completed. ${(result.failures || []).map(item => `${item.name}: ${item.error}`).slice(0,3).join('; ')}`;
      } else $('tools-status').textContent = error.message;
      hooks.error(error.message);
    }
    finally { pending = false; setBusy(hooks.isBusy?.()); await history(); }
  }
  function init(api) {
    hooks = api;
    let mount = $('tools-view');
    if (!mount) { mount = document.createElement('section'); mount.id = 'tools-view'; mount.hidden = true; document.querySelector('main').append(mount); }
    mount.classList.add('file-tools-view');
    mount.innerHTML = `<div class="tools-intro"><h2>File tools</h2><p>Preview changes before applying them to the local files in your current scan. Protected folders and offline catalogs stay read-only.</p></div>
      <div class="tools-grid">
        <section class="tool-card"><h3>Keep rules</h3><p>Choose which copy to keep across duplicate groups. Protected originals take priority.</p><label>Keep preference<select id="tools-keep-rule"><option value="quality">Highest resolution, then largest file</option><option value="newest">Newest modified date</option><option value="oldest">Oldest modified date</option><option value="folder">Preferred folder</option><option value="format">Preferred file formats</option><option value="largest">Largest file</option><option value="smallest">Smallest file</option></select></label><label>Preferred folder<input id="tools-preferred-folder" placeholder="Full folder path" autocomplete="off"></label><label>Format priority<input id="tools-formats" placeholder="e.g. raw, png, jpg" autocomplete="off"></label><button id="tools-apply-keepers" class="button secondary" type="button">Apply keep rule to review</button><p id="tools-keepers-status" role="status"></p></section>
        <section class="tool-card"><h3>Batch rename</h3><p>Rename ordinary local files from this scan. Preview every destination first.</p><label>Naming template<input id="tools-rename-template" value="{date}_{counter:04d}" maxlength="200" autocomplete="off"></label><label>Start numbering at<input id="tools-rename-start" type="number" value="1" min="0" max="999999999" step="1"></label><small>Available: {name}, {date}, {year}, {ext}, {counter:04d}. Date uses file modified date. The original extension is kept automatically.</small><button class="button secondary" data-tool="rename" type="button">Preview names</button></section>
        <section class="tool-card"><h3>Save a review report</h3><p>Export duplicate groups, matching scores, file locations and sizes as a CSV file for Excel.</p><button id="tools-report" class="button secondary" type="button">Export CSV report</button></section>
        <section class="tool-card"><h3>Empty folders</h3><p>Find empty folders inside your scanned locations, including folders left after moving files. The scan roots are preserved.</p><button class="button secondary" data-tool="empty-folders" type="button">Preview empty folders</button></section>
        <section class="tool-card"><h3>Hard links</h3><p>Replace exact copies on the same filesystem with links to the selected original. This frees duplicate storage while retaining each filename.</p><small>Linked paths share content: editing one edits the others. Undo creates separate copies again. Keep rules above choose the original.</small><button class="button secondary" data-tool="hardlinks" type="button">Preview hard links</button></section>
        <section class="tool-card"><h3>Windows Recycle Bin</h3><p>Send exact duplicate copies to the Windows Recycle Bin, keeping one original from every group. Keep rules above apply.</p><button class="button secondary" data-tool="recycle" type="button">Preview copies to recycle</button></section>
        <section class="tool-card"><h3>Finish cleaning app trash</h3><p>Files in Recently deleted still occupy disk space. Move them to the Windows Recycle Bin, or permanently empty app trash.</p><div class="tool-actions"><button class="button secondary" data-tool="recycle-trash" type="button">Preview transfer to Recycle Bin</button><button class="button danger" data-tool="purge-trash" type="button">Preview permanent removal</button></div></section>
      </div><p id="tools-status" class="tools-status" role="status" aria-live="polite"></p><section class="tools-history-section"><div class="tools-history-heading"><h3>File tools history</h3><button id="tools-history-refresh" class="button secondary small" type="button">Refresh</button></div><div id="tools-history"><p class="tools-muted">Scan folders or open a scan to review its file operations.</p></div></section>`;
    const dialog = document.createElement('dialog'); dialog.id = 'tools-plan-dialog'; dialog.className = 'tools-plan-dialog'; dialog.setAttribute('aria-labelledby','tools-plan-title');
    dialog.innerHTML = `<h2 id="tools-plan-title"></h2><p id="tools-plan-summary"></p><p id="tools-plan-note"></p><div id="tools-plan-list" tabindex="0" role="region" aria-label="Files and destinations"></div><div class="tools-pagination"><span id="tools-plan-page"></span><button id="tools-plan-previous" class="button secondary small" type="button">Previous</button><button id="tools-plan-next" class="button secondary small" type="button">Next</button></div><label id="tools-permanent-confirm" hidden>Type DELETE to confirm permanent removal<input id="tools-delete-word" autocomplete="off" spellcheck="false"></label><div class="dialog-actions"><button id="tools-cancel" class="button secondary" type="button">Cancel</button><button id="tools-confirm" class="button primary" type="button">Confirm</button></div>`;
    document.body.append(dialog);
    mount.addEventListener('click', event => { const button = event.target.closest('[data-tool]'); if (button && !button.disabled) preview(button.dataset.tool); });
    $('tools-plan-previous').addEventListener('click', () => { if(page > 0) {page--; renderPlan();} });
    $('tools-plan-next').addEventListener('click', () => { if((page + 1) * 50 < plan.files.length) {page++; renderPlan();} });
    $('tools-delete-word').addEventListener('input', () => setBusy(busy()));
    $('tools-cancel').addEventListener('click', () => { if (!pending) dialog.close(); });
    dialog.addEventListener('cancel', event => { if (pending) event.preventDefault(); });
    $('tools-confirm').addEventListener('click', confirm);
    $('tools-history-refresh').addEventListener('click', history);
    $('tools-apply-keepers').addEventListener('click', async () => {
      if (busy()) return;
      pending = true; setBusy(true);
      try { const result = await request('/api/tools/keepers', {rule:$('tools-keep-rule').value, preferredFolder:$('tools-preferred-folder').value, formats:$('tools-formats').value});
        hooks.applyKeepers?.(result.keepers || result); $('tools-keepers-status').textContent = 'Keep rule applied. Review the selected original before removing copies.'; hooks.toast('Keep choices updated.');
      } catch(error) { hooks.error(error.message); }
      finally { pending = false; setBusy(hooks.isBusy?.()); }
    });
    $('tools-report').addEventListener('click', async () => {
      if (busy()) return;
      pending = true; setBusy(true);
      try {
        if (hooks.exportReport) await hooks.exportReport();
        else { const response = await fetch('/api/report.csv', {cache:'no-store'}); if(!response.ok) throw new Error('The CSV report could not be exported.'); const blob = await response.blob(); const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = 'Media Review duplicates.csv'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 30000); }
        hooks.toast('CSV report exported.');
      } catch(error) { hooks.error(error.message); }
      finally {pending = false; setBusy(hooks.isBusy?.());}
    });
    $('tools-history').addEventListener('click', async event => {
      const button = event.target.closest('[data-tool-undo]'); if(!button || busy()) return;
      if(!window.confirm('Undo this file operation? Renamed files return to their original paths; hard links become independent copies. Existing files will not be overwritten.')) return;
      pending = true; setBusy(true);
      try {const result = await request('/api/tools/undo',{operationId:button.dataset.toolUndo,confirmed:true}); if(result.state) await hooks.loadState(result.state); $('tools-status').textContent = `${result.completed || 0} changes undone.${result.failures?.length ? ` ${result.failures.length} conflicts remain; existing files were preserved.` : ''}`;}
      catch(error) {
        if(error.result?.state) {
          await hooks.loadState(error.result.state);
          $('tools-status').textContent = `${Number(error.result.completed || 0).toLocaleString()} changes undone. ${(error.result.failures || []).slice(0,3).map(item => `${item.name}: ${item.error}`).join('; ')}`;
        }
        hooks.error(error.message);
      }
      finally {pending = false;setBusy(hooks.isBusy?.());await history();}
    });
    setBusy(hooks.isBusy?.());
  }
  window.MediaReviewFileTools = {init,setBusy,show:history,refresh:history,isBusy:()=>pending};
})();
