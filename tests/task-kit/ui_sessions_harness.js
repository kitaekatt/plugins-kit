// Runs the dashboard's app.js against a fake DOM and prints how working-session registrations render, as JSON.
// Used by test_dashboard_sessions.py.
// Usage: node ui_sessions_harness.js <app.js> <dashboard.json> <recheck-response.json> <task-key>
//   rows:    the collapsed task list (the inline markers)
//   open:    the same list with <task-key> expanded (the task details)
//   notice:  the banner area after a Re-check whose response is <recheck-response.json>
//   (then the first dismissable registration is dismissed)
//   posts:   every state-changing request the page sent
'use strict';
const fs = require('fs');
const vm = require('vm');
const [, , appPath, payloadPath, recheckPath, taskKey] = process.argv;
const payload = JSON.parse(fs.readFileSync(payloadPath, 'utf8'));
const recheck = JSON.parse(fs.readFileSync(recheckPath, 'utf8'));

function makeEl(id) {
  const el = {
    id, innerHTML: '', textContent: '', value: '', className: '', attrs: {}, scrollTop: 0,
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    classes: new Set(),
    classList: {
      toggle(name, on) { if (on === undefined ? !el.classes.has(name) : on) el.classes.add(name); else el.classes.delete(name); },
    },
    addEventListener() {},
    querySelectorAll() { return []; },
    style: { setProperty() {} },
  };
  return el;
}
const els = {};
['serverChip', 'status', 'banners', 'projectSelect', 'main', 'search', 'sortSeg']
  .forEach((id) => { els[id] = makeEl(id); });
const docListeners = {};
const document = {
  getElementById: (id) => els[id],
  querySelector: () => ({ focus() {}, scrollIntoView() {} }),
  addEventListener: (t, f) => { (docListeners[t] = docListeners[t] || []).push(f); },
  visibilityState: 'visible',
  createElement: () => makeEl('x'),
  body: { appendChild() {} },
  execCommand: () => true,
};
const requests = [];
function fakeFetch(url, opts) {
  requests.push({ url, method: opts.method || 'GET', body: opts.body || null });
  if (opts.method === 'POST') {
    return Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve(recheck) });
  }
  return Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve(payload) });
}
const context = {
  document, location: { hash: '#test-token' }, fetch: fakeFetch,
  setTimeout: () => 1, clearTimeout() {}, navigator: {}, console,
};
const flush = () => new Promise((r) => setImmediate(r));
function click(attrs) {
  const target = makeEl('target');
  target.attrs = attrs;
  target.closest = () => target;
  (docListeners.click || []).forEach((f) => f({ target, stopPropagation() {} }));
}

(async () => {
  vm.runInNewContext(fs.readFileSync(appPath, 'utf8'), context);
  await flush();
  const report = { rows: els.main.innerHTML };
  click({ 'data-action': 'toggletask', 'data-key': taskKey });
  report.open = els.main.innerHTML;
  click({ 'data-action': 'recheck', 'data-key': taskKey });
  await flush();
  await flush();
  report.notice = els.main.innerHTML + els.banners.innerHTML;
  // Dismiss the first dismissable (unverified) registration, as the button would.
  const task = payload.tasks.filter((t) => t.key === taskKey)[0];
  const far = (task.sessions || []).filter((x) => x.dismissable)[0];
  if (far) {
    click({ 'data-action': 'dismisssession', 'data-key': taskKey, 'data-host': far.host, 'data-sid': far.session_id,
      'data-reg': far.registered_at });
    await flush();
  }
  report.posts = requests.filter((r) => r.method === 'POST');
  console.log(JSON.stringify(report));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
