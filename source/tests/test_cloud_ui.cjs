const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const file = require('path').join(__dirname, '..', 'static', 'cloud.js');
const code = fs.readFileSync(file, 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
const clean = () => ({ available: true, accounts: [{ id: 'generated-account', provider: 'onedrive', providerName: 'OneDrive', label: 'Generated account' }],
  connection: { running: false, phase: 'ready', message: '', question: null },
  scan: { running: false, phase: 'ready', groups: [], totalFiles: 0, hashedFiles: 0, skippedFiles: 0, groupCount: 0 } });

function harness() {
  const elements = new Map(), all = new Set(), timers = new Map(), windowHandlers = new Map(); let timerId = 0;
  class Element {
    constructor(tag = 'div') { this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.handlers = {}; this.hidden = false; this.disabled = false; this.checked = false; this._value = ''; this._text = ''; this.attrs = {}; all.add(this); }
    set id(value) { this._id = value; elements.set(value, this); }
    get id() { return this._id || ''; }
    set textContent(value) { this._text = String(value); this.children = []; }
    get textContent() { return this._text + this.children.map(c => c.textContent).join(' '); }
    showModal() { this.open = true; }
    close() { this.open = false; }
    get options() { return this.tagName === 'SELECT' ? this.children.filter(c => c.tagName === 'OPTION') : []; }
    set selectedIndex(index) { this._value = this.options[index]?.value || ''; }
    get value() { return this._value; }
    set value(value) { value = String(value); this._value = this.tagName === 'SELECT' && !this.options.some(o => o.value === value) ? '' : value; }
    set innerHTML(html) {
      this.children = []; this._text = ''; let lastSelect = null;
      for (const match of html.matchAll(/<([a-z][\w-]*)([^>]*)>([^<]*)/gi)) {
        const [, tag, attrs, text] = match; const node = new Element(tag);
        const id = /\bid="([^"]+)"/.exec(attrs)?.[1]; if (id) node.id = id;
        const value = /\bvalue="([^"]*)"/.exec(attrs)?.[1]; if (value !== undefined) node.value = value;
        node.hidden = /\bhidden\b/.test(attrs); node.disabled = /\bdisabled\b/.test(attrs); node.checked = /\bchecked\b/.test(attrs);
        node._text = text.trim();
        if (tag === 'select') lastSelect = node;
        if (tag === 'option' && lastSelect) { lastSelect.append(node); if (!lastSelect._value) lastSelect._value = node.value; }
        else this.children.push(node);
      }
    }
    append(...nodes) { this.children.push(...nodes); if (this.tagName === 'SELECT' && !this._value && this.options.length) this._value = this.options[0].value; }
    replaceChildren(...nodes) { this.children = [...nodes]; this._text = ''; if (this.tagName === 'SELECT') this._value = this.options[0]?.value || ''; }
    addEventListener(name, fn) { (this.handlers[name] ||= []).push(fn); }
    setAttribute(name, value) { this.attrs[name] = value; }
    async emit(name, extra = {}) { for (const fn of this.handlers[name] || []) await fn({ target: this, preventDefault() {}, ...extra }); }
  }
  const host = new Element(); host.id = 'cloud-workspace';
  const get = id => { const found = elements.get(id); assert(found, 'Missing DOM id ' + id); return found; };
  const calls = [], errors = []; const h = { get, calls, errors, timers, all, state: clean(), localBusy: false, pending: null };
  const document = { getElementById: id => elements.get(id) || null, createElement: tag => new Element(tag),
    querySelectorAll: selector => selector === '[data-cloud-folder]' ? [...all].filter(e => Object.hasOwn(e.dataset, 'cloudFolder')) : selector === '[data-cloud-file]' ? [...all].filter(e => Object.hasOwn(e.dataset, 'cloudFile')) : [] };
  const context = vm.createContext({ document, console, window: { addEventListener(name, fn) { windowHandlers.set(name, fn); } },
    setTimeout(fn, delay) { const id = ++timerId; timers.set(id, { fn, delay }); return id; }, clearTimeout(id) { timers.delete(id); },
    Number, Math, String, Object, JSON, Array, Boolean });
  vm.runInContext(code, context);
  h.api = context.window.MediaReviewCloud;
  h.api.init({ error: message => errors.push(message), toast() {}, isLocalBusy: () => h.localBusy,
    async request(path, body) {
      calls.push({ path, body });
      if (h.pending) return h.pending(path, body);
      if (path === '/api/cloud/history') return { operations: h.history || [] };
      if (path === '/api/cloud/action-plan') return { id: 'plan-generated', accountLabel: 'Generated account', providerName: 'OneDrive', action: body.action, totalSize: 12, possibleMatches: true, files: body.ids.map(id => ({ id, source: 'Photos/copy.jpg', destination: '.MediaReviewTrash/batch/copy.jpg' })) };
      if (path === '/api/cloud/action-execute' || path === '/api/cloud/action-restore') h.state.action = { running: true, phase: 'moving', done: 0, total: 1, message: 'Moving selected cloud file' };
      if (path === '/api/cloud/folders') return { path: body.path, folders: body.path ? [] : [{ name: 'Photos', path: 'Photos' }] };
      if (path === '/api/cloud/connect') h.state.connection = { running: true, phase: 'authorizing', message: 'Sign in in the browser.', question: null };
      if (path === '/api/cloud/scan') h.state.scan = { ...h.state.scan, running: true, phase: 'listing', message: 'Reading cloud metadata.' };
      if (path === '/api/cloud/stop') h.state.scan = { ...h.state.scan, running: true, phase: 'stopping', message: 'Stoppingâ€¦' };
      if (path === '/api/cloud/answer') h.state.connection = { running: false, phase: 'connected', message: 'Connected.', question: null };
      return structuredClone(h.state);
    } });
  h.windowHandlers = windowHandlers;
  return h;
}

(async () => {
  let checks = 0;
  const h = harness();
  assert.equal(h.calls.length, 0); checks++;
  assert.equal(h.get('cloud-preview-action').disabled, true); checks++;
  await h.api.show(true);
  assert.deepEqual(h.calls.map(call => call.path), ['/api/cloud/state', '/api/cloud/history']); checks++;
  assert(!h.calls.some(call => ['/api/cloud/scan', '/api/cloud/connect'].includes(call.path))); checks++;
  h.state.scan = { ...h.state.scan, accountId: 'generated-account', phase: 'complete', groups: [{ id: 'group', possible: true, hashType: 'QuickXor', duplicateBytes: 12,
    files: [{ id: 'keeper', name: 'Original', path: 'Photos/original.jpg', size: 12 }, { id: 'copy', name: '<script>literal</script>', path: 'Photos/copy.jpg', size: 12 }] }] };
  await h.api.refresh();
  assert.equal(h.get('cloud-write-notice').hidden, false); checks++;
  h.state.accounts[0].writeEnabled = true; await h.api.refresh();
  assert.equal(h.get('cloud-write-notice').hidden, true); checks++;
  assert(h.get('cloud-results').textContent.includes('<script>literal</script>')); checks++;
  await h.get('cloud-select-copies').emit('click');
  assert.equal(h.get('cloud-selection-count').textContent, '1 files selected'); checks++;
  assert.equal(h.get('cloud-preview-action').disabled, false); checks++;
  await h.get('cloud-preview-action').emit('click');
  const plan = h.calls.find(call => call.path === '/api/cloud/action-plan');
  assert.deepEqual([...plan.body.ids], ['copy']); checks++;
  assert.equal(h.get('cloud-action-dialog').open, true); checks++;
  assert(h.get('cloud-confirm-files').textContent.includes('.MediaReviewTrash/batch/copy.jpg')); checks++;
  assert.equal(h.get('cloud-confirm-warning').hidden, false); checks++;
  assert(!h.calls.some(call => call.path === '/api/cloud/action-execute')); checks++;
  await h.get('cloud-confirm-cancel').emit('click');
  assert.equal(h.get('cloud-action-dialog').open, false); checks++;
  assert(!h.calls.some(call => call.path === '/api/cloud/action-execute')); checks++;
  await h.get('cloud-preview-action').emit('click'); await h.get('cloud-confirm-accept').emit('click');
  const execute = h.calls.find(call => call.path === '/api/cloud/action-execute');
  assert.equal(execute.body.planId, 'plan-generated'); assert.equal(execute.body.confirmed, true); checks++;
  assert.equal(h.get('cloud-preview-action').disabled, true); checks++;
  assert(h.timers.size > 0); checks++;
  h.state.action.running = false; h.state.action.phase = 'complete';
  h.history = [{ id: 'history-generated', action: 'trash', accountLabel: 'Generated account', moved: 1, created: '2026-01-01', files: [{ source: 'Photos/copy.jpg', destination: '.MediaReviewTrash/batch/copy.jpg', status: 'moved' }] }];
  await h.api.refresh();
  assert.equal(h.timers.size, 0); checks++;
  const restore = [...h.all].filter(e => e.tagName === 'BUTTON' && e.textContent === 'Review restoration').at(-1);
  assert(restore); await restore.emit('click');
  assert.equal(h.get('cloud-confirm-title').textContent, 'Restore these cloud files?'); checks++;
  assert(!h.calls.some(call => call.path === '/api/cloud/action-restore')); checks++;
  await h.get('cloud-confirm-accept').emit('click');
  const restoreCall = h.calls.find(call => call.path === '/api/cloud/action-restore');
  assert.equal(restoreCall.body.operationId, 'history-generated'); assert.equal(restoreCall.body.confirmed, true); checks++;
  h.state.action.running = false; h.localBusy = true; await h.api.refresh();
  const before = h.calls.length; await h.get('cloud-preview-action').emit('click');
  assert.equal(h.calls.length, before); checks++;
  assert.deepEqual(h.errors, []); checks++;
  await h.api.show(false);
  assert.equal(h.timers.size, 0); checks++;
  console.log(JSON.stringify({ checks, passed: true }));
})().catch(error => { console.error(error); process.exitCode = 1; });
