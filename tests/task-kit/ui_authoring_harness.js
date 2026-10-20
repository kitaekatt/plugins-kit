// Runs the dashboard's app.js against a fake DOM and drives the authoring forms and controls; prints what it
// rendered and every state-changing request it sent, as JSON. Used by test_dashboard_ui_contract.py.
// Usage: node ui_authoring_harness.js <app.js> <dashboard.json> <scenario.json>
// scenario.json: { taskKey, otherTask, availItem, deferredItem, queuedItem, queuedRun, projectId }
'use strict';
const fs = require('fs');
const vm = require('vm');
const [, , appPath, payloadPath, scenarioPath] = process.argv;
const payload = JSON.parse(fs.readFileSync(payloadPath, 'utf8'));
const sc = JSON.parse(fs.readFileSync(scenarioPath, 'utf8'));

function makeEl(id) {
  const el = {
    id, innerHTML: '', textContent: '', value: '', className: '', attrs: {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    removeAttribute(k) { delete this.attrs[k]; },
    classes: new Set(),
    classList: { toggle(name, on) { if (on) el.classes.add(name); else el.classes.delete(name); } },
    addEventListener() {},
    querySelectorAll() { return []; },
    style: { setProperty() {} },
  };
  return el;
}
const els = {};
['serverChip', 'status', 'banners', 'projectSelect', 'main', 'search', 'sortSeg', 'newTaskBtn']
  .forEach((id) => { els[id] = makeEl(id); });
els.newTaskBtn.attrs = { disabled: '', 'aria-disabled': 'true', 'data-later': 'newtask' };
const docListeners = {};
const focused = [];
const carets = [];
const document = {
  getElementById: (id) => els[id],
  querySelector: (sel) => ({
    focus() { focused.push(sel); },
    scrollIntoView() {},
    setSelectionRange(a, b) { carets.push([sel, a, b]); },
  }),
  addEventListener: (t, f) => { (docListeners[t] = docListeners[t] || []).push(f); },
  visibilityState: 'visible',
  activeElement: null,
  createElement: () => makeEl('x'),
  body: { appendChild() {} },
  execCommand: () => true,
};
const requests = [];
function fakeFetch(url, opts) {
  const method = opts.method || 'GET';
  requests.push({ url, headers: opts.headers, method, body: opts.body || null });
  if (method !== 'GET') {
    const sent = opts.body ? JSON.parse(opts.body) : {};
    if (/\/projects\/[^/]+\/tasks$/.test(url)) {
      const task = payload.tasks.filter((t) => t.key !== sc.taskKey)[0];
      return Promise.resolve({ status: 201, ok: true, json: () => Promise.resolve({ task, revision: 'r', path: 'dev/tasks/x' }) });
    }
    if (/\/items$/.test(url)) {
      return Promise.resolve({ status: sc.addStatus || 201, ok: true, json: () => Promise.resolve(sc.addBody || {}) });
    }
    return Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve({}) });
  }
  const body = url.indexOf('/log') > 0 ? { text: '', cursor: 0 } :
    url.indexOf('/api/v1/items/') === 0 ? sc.detail : payload;
  return Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve(body) });
}
const timers = [];
const context = {
  document, location: { hash: '#test-token' }, fetch: fakeFetch,
  setTimeout: (f, ms) => { timers.push({ f, ms }); return timers.length; },
  clearTimeout() {}, navigator: {}, console,
};
const flush = () => new Promise((r) => setImmediate(r));
function click(attrs) {
  const target = makeEl('target');
  target.attrs = attrs;
  target.closest = () => target;
  (docListeners.click || []).forEach((f) => f({ target, stopPropagation() {} }));
}
function typeInto(attrs, value, extra) {
  const target = Object.assign(makeEl('field'), extra || {});
  target.attrs = attrs;
  target.value = value;
  (docListeners.input || []).forEach((f) => f({ target }));
  return target;
}
function change(attrs, value) {
  const target = typeInto(attrs, value);
  (docListeners.change || []).forEach((f) => f({ target }));
}
const draftAttrs = (scope, id, field) => ({ 'data-draft': scope, 'data-id': id, 'data-field': field, 'data-fk': `${scope}:${id}:${field}` });
async function pollWith(mutate) {
  mutate(payload);
  payload.revision = 'rev-' + Math.random();
  timers[timers.length - 1].f();
  await flush();
}
const sent = (from) => requests.slice(from).filter((r) => r.method !== 'GET');

(async () => {
  vm.runInNewContext(fs.readFileSync(appPath, 'utf8'), context);
  await flush();
  const report = {};
  report.newBtn = Object.assign({}, els.newTaskBtn.attrs);

  // ---- New task: open, type, survive a re-render with focus and caret, create
  click({ 'data-action': 'newtask' });
  report.newOpen = els.main.innerHTML;
  const titleAttrs = draftAttrs('nt', '', 'title');
  const field = typeInto(titleAttrs, 'Fix the flaky build', { selectionStart: 4, selectionEnd: 4 });
  document.activeElement = field;
  change(draftAttrs('nt', '', 'priority'), 'P1');
  typeInto(draftAttrs('nt', '', 'itemtitle'), 'Reproduce it');
  typeInto(draftAttrs('nt', '', 'details'), 'Run it twice.');
  await pollWith(() => {});
  report.newAfterPoll = els.main.innerHTML;
  report.focusAfterPoll = focused.slice();
  report.caretAfterPoll = carets.slice();
  document.activeElement = null;
  let at = requests.length;
  click({ 'data-action': 'createtask' });
  await flush(); await flush();
  report.newSent = sent(at);
  report.newAfterCreate = els.main.innerHTML;

  // ---- New task refusals stay in the form
  click({ 'data-action': 'newtask' });
  at = requests.length;
  click({ 'data-action': 'createtask' });
  report.newEmptyTitle = els.main.innerHTML;
  report.newEmptyTitleSent = sent(at);
  click({ 'data-action': 'newtask' });

  // ---- Add item
  click({ 'data-action': 'toggletask', 'data-key': sc.taskKey });
  report.openTask = els.main.innerHTML;
  click({ 'data-action': 'additem', 'data-key': sc.taskKey });
  report.addOpen = els.main.innerHTML;
  typeInto(draftAttrs('ai', sc.taskKey, 'title'), 'Write the docs');
  typeInto(draftAttrs('ai', sc.taskKey, 'details'), '');
  at = requests.length;
  click({ 'data-action': 'addsubmit', 'data-key': sc.taskKey, 'data-queue': '1' });
  report.addNoDetails = els.main.innerHTML;
  report.addNoDetailsSent = sent(at);
  typeInto(draftAttrs('ai', sc.taskKey, 'details'), 'Explain it.');
  at = requests.length;
  click({ 'data-action': 'addsubmit', 'data-key': sc.taskKey, 'data-queue': '1' });
  await flush(); await flush();
  report.addSent = sent(at);
  report.addAfter = els.main.innerHTML;

  // ---- Add item that the server refuses keeps the form and what was typed
  sc.addStatus = 409;
  sc.addBody = { error: { code: 'held', message: 'a Claude Code session is working this task' } };
  click({ 'data-action': 'additem', 'data-key': sc.taskKey });
  typeInto(draftAttrs('ai', sc.taskKey, 'title'), 'Kept title');
  click({ 'data-action': 'addsubmit', 'data-key': sc.taskKey, 'data-queue': '0' });
  await flush(); await flush();
  report.addRefused = els.main.innerHTML;
  sc.addStatus = 201; sc.addBody = {};
  click({ 'data-action': 'additem', 'data-key': sc.taskKey });  // close it again

  // ---- Defer, Undefer, Queue all
  click({ 'data-action': 'select', 'data-key': sc.availItem });
  await flush();
  report.availDetail = els.main.innerHTML;
  at = requests.length;
  click({ 'data-action': 'defer', 'data-key': sc.availItem });
  await flush();
  report.deferSent = sent(at);
  click({ 'data-action': 'select', 'data-key': sc.deferredItem });
  await flush();
  report.deferredDetail = els.main.innerHTML;
  at = requests.length;
  click({ 'data-action': 'undefer', 'data-key': sc.deferredItem });
  await flush();
  report.undeferSent = sent(at);
  at = requests.length;
  click({ 'data-action': 'queueall', 'data-key': sc.taskKey });
  await flush();
  report.queueAllSent = sent(at);

  // ---- Run options on an available item ride on Queue
  click({ 'data-action': 'select', 'data-key': sc.availItem });
  await flush();
  report.optionsAvailable = els.main.innerHTML;
  change(draftAttrs('opt', sc.availItem, 'effort'), 'high');
  typeInto(draftAttrs('opt', sc.availItem, 'minutes'), '15');
  at = requests.length;
  click({ 'data-action': 'queue', 'data-key': sc.availItem });
  await flush();
  report.queueSent = sent(at);
  typeInto(draftAttrs('opt', sc.availItem, 'minutes'), '0');
  at = requests.length;
  click({ 'data-action': 'queue', 'data-key': sc.availItem });
  report.badMinutes = els.main.innerHTML;
  report.badMinutesSent = sent(at);
  typeInto(draftAttrs('opt', sc.availItem, 'minutes'), '');

  // ---- A queued entry: its options save as they change; Remove takes it out
  click({ 'data-action': 'select', 'data-key': sc.queuedItem });
  await flush();
  report.optionsQueued = els.main.innerHTML;
  at = requests.length;
  change(Object.assign(draftAttrs('opt', sc.queuedItem, 'effort'), { 'data-optsave': '1' }), 'low');
  await flush();
  report.saveSent = sent(at);
  at = requests.length;
  click({ 'data-action': 'remove', 'data-key': sc.queuedRun });
  await flush();
  report.removeSent = sent(at);

  // ---- A task a session holds, and a task a run works: authoring is drawn off with the reason
  const task = payload.tasks.filter((t) => t.key === sc.taskKey)[0];
  await pollWith(() => { task.held = true; });
  report.held = els.main.innerHTML;
  await pollWith(() => { task.held = false; task.run_active = true; });
  report.runActive = els.main.innerHTML;
  console.log(JSON.stringify(report));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
