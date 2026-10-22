/* Task dashboard UI.
 *
 * Plain JavaScript, no framework and no build step. It talks only to the
 * server that served it: every request is a relative /api/v1/ path carrying the
 * per-instance token that arrived in the URL fragment. State-changing calls go
 * through send(method, path, body). Every write to a task folder (Queue, Retry,
 * Re-check, Queue all, Add item, Defer, Undefer) carries the expected_revision
 * the page last saw; New task is judged on its input alone, and Cancel run,
 * Remove from queue and Save run options act on a run record, not on the task
 * (the task revision moves while a run works). An action the server does not
 * back (Pause worker, or any action an older server lacks) is drawn disabled
 * through laterButton(), so it is visible and cannot be clicked. Queue, Retry,
 * Queue all and Add and queue also stay disabled, with the server's reason,
 * whenever the server reports autonomous runs unavailable; Cancel run does not
 * depend on llm-kit. Authoring (Add item, Defer, Undefer) is disabled with the
 * reason while a Claude Code session holds the task or a run works it.
 *
 * Forms render inline. What a person has typed lives in S.drafts, outside the
 * markup, so the poll's re-render never loses it; the focused field and its
 * selection are restored by data-fk after each render.
 *
 * One flat task list across projects: a row per task (priority, title,
 * <repo>:<task>, item count, age) with a needs-you marker and an in-flight
 * marker; clicking a row opens its stage map (one at a time). Everything else
 * renders inline, full width, under its parent: the selected item's detail
 * and the task's details sit below the stage map, and a run's result and log
 * open in place in Recent activity. Nothing opens beside the list.
 *
 * The page is served under a CSP that forbids inline styles and scripts, so no
 * markup carries a style attribute: dynamic values travel as data-* attributes
 * and are applied through the CSSOM after each render (applyDynamic).
 */
(function () {
'use strict';

var TOKEN = (location.hash || '').replace(/^#/, '');
var VISIBLE_POLL_MS = 2000;
var HIDDEN_POLL_MS = 15000;

var S = {
  snap: null,          // last dashboard payload
  project: '',         // project (repository) filter (id)
  sort: 'priority',    // one of SORTS; exactly one is active
  openTask: null,      // the one expanded task key "checkout|task"
  q: '',               // text filter
  sel: null,           // selected item key "checkout|task|item"; its detail shows in the open task
  runOpen: {},         // run id -> true while its result and log are shown inline
  runLog: {},          // run id -> { text, cursor }
  runHelpers: {},      // run id -> its endpoint helper calls (an array), or { error }
  notice: null,        // { kind, title, text } shown with the banners after an action
  detail: {},          // item key -> detail payload
  open: {},            // <details> open state by data-open key (all start closed)
  resolve: {},         // keys whose command box is expanded
  drafts: { nt: {}, ai: {}, opt: {} },  // what a person has typed: New task, Add item (by task), run options (by item)
  newOpen: false,      // the New task form is open
  addOpen: null,       // the task key whose Add item form is open
  formErr: {},         // form key -> the last refusal shown inside that form
  busy: {},            // action key -> true while its request is in flight
  conn: 'connecting',  // connecting | ok | offline | auth
  connDetail: '',
  renderedRev: null,
  polling: false,
  refreshNext: false,
  timer: null
};

/* ================= helpers ================= */
var SVG = {
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  play: '<path d="M8 5.5v13l11-6.5z"/>',
  pause: '<path d="M9 6v12M15 6v12"/>',
  alert: '<path d="M12 6.5v7M12 17.5h.01"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h8"/>',
  term: '<path d="M4 6l6 6-6 6M12 19h8"/>',
  retry: '<path d="M20 12a8 8 0 1 1-2.4-5.7M20 4v5h-5"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="1.5"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  tri: '<path d="M12 4l9.5 16.5h-19z"/><path d="M12 10v4.5M12 17.5h.01"/>',
  spin: '<path d="M12 3a9 9 0 1 0 9 9"/>',
  eye: '<path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12z"/><circle cx="12" cy="12" r="2.5"/>'
};
function ic(name, cls) {
  return '<svg class="ic ' + (cls || '') + '" viewBox="0 0 24 24" aria-hidden="true">' + SVG[name] + '</svg>';
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}
function $(id) { return document.getElementById(id); }
function pad(n) { return (n < 10 ? '0' : '') + n; }
function parseWhen(ts) {
  if (!ts) return null;
  var m = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?/.exec(ts);
  if (!m) return null;
  return new Date(+m[1], +m[2] - 1, +m[3], +(m[4] || 12), +(m[5] || 0)).getTime();
}
function ago(ts) {
  var t = parseWhen(ts);
  if (t == null) return 'no activity';
  var m = Math.floor((Date.now() - t) / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return m + ' min ago';
  var h = Math.floor(m / 60);
  if (h < 48) return h + 'h ago';
  return Math.floor(h / 24) + 'd ago';
}
/* Same age-dimming rule as the static review page. */
function brightness(ts) {
  var t = parseWhen(ts);
  if (t == null) return 0.33;
  var hrs = (Date.now() - t) / 3600000;
  if (hrs <= 4) return 1;
  if (hrs >= 120) return 0.33;
  return 1 - ((hrs - 4) / 116) * 0.67;
}
/* Working-session registrations. Their timestamps are UTC ("...Z"), unlike the task log's local times. A registration
 * that is verified running on this machine holds the task; every other one is only shown and blocks nothing. */
function agoUtc(ts) {
  var t = Date.parse(ts);
  if (isNaN(t)) return 'age unknown';
  var m = Math.floor((Date.now() - t) / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return m + ' min ago';
  var h = Math.floor(m / 60);
  if (h < 48) return h + 'h ago';
  return Math.floor(h / 24) + 'd ago';
}
function sinceUtc(ts) {
  var t = Date.parse(ts);
  if (isNaN(t)) return 'an unknown time';
  var d = new Date(t), n = new Date();
  var clock = pad(d.getHours()) + ':' + pad(d.getMinutes());
  if (d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate()) return clock;
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + clock;
}
function sessionText(s) {
  if (s.state === 'running') return 'Claude Code session working (' + s.host + ', since ' + sinceUtc(s.registered_at) + ')';
  if (s.state === 'not running') return 'not running (' + s.host + ', ' + agoUtc(s.registered_at) + ')';
  return 'on ' + s.host + ', ' + agoUtc(s.registered_at);
}
/* The state is not left to the word "unverified" in the marker: the tint says it, and the title and the aria-label spell it out. */
function sessionLabel(s) { return sessionText(s) + (s.state === 'unverified' ? ', unverified' : ''); }
function sessionClass(s) { return s.state === 'running' ? 'on' : s.state === 'not running' ? 'off' : 'far'; }
function sessionTitle(s) {
  var what = s.state === 'running' ? 'Verified running on this machine: the dashboard starts no run on this task until it hands off.' :
    s.state === 'not running' ? 'This machine\'s record, but its process is not running. Shown only: it blocks nothing, and stays until /task hand-off or task sessions release.' :
    'Unverified: another machine\'s record, which this machine cannot verify. Shown only: it blocks nothing, and Dismiss hides it here.';
  return what + ' Session ' + s.session + (s.checkout ? ', checkout ' + s.checkout : '') + ', last recorded ' + agoUtc(s.recorded_at) + '.' +
    (s.task_state && s.task_state !== 'open' && s.task_state !== 'unknown' ? ' The task is ' + s.task_state + '.' : '');
}
function sessionsOf(task) { return task.sessions || []; }
function holdingSession(task) {
  var all = sessionsOf(task);
  for (var i = 0; i < all.length; i++) if (all[i].holds) return all[i];
  return null;
}
var MAX_SESSION_MARKS = 3;
function sessionMarksHtml(task) {
  var all = sessionsOf(task), h = '', i;
  for (i = 0; i < all.length && i < MAX_SESSION_MARKS; i++) {
    h += '<span class="mk sess ' + sessionClass(all[i]) + '" title="' + esc(sessionTitle(all[i])) + '" aria-label="' + esc(sessionLabel(all[i])) + '">' + esc(sessionText(all[i])) + '</span>';
  }
  if (all.length > MAX_SESSION_MARKS) {
    h += '<span class="mk sess far" title="' + esc(all.slice(MAX_SESSION_MARKS).map(sessionLabel).join('; ')) + '">+' + (all.length - MAX_SESSION_MARKS) + ' more</span>';
  }
  return h;
}
function sessionRowsHtml(task) {
  var all = sessionsOf(task);
  if (!all.length) return '';
  return '<dt>Working sessions</dt><dd>' + all.map(function (s) {
    return '<span class="sess-line ' + sessionClass(s) + '" title="' + esc(sessionTitle(s)) + '" aria-label="' + esc(sessionLabel(s)) + '">' + esc(sessionText(s)) + ' <span class="muted">(session ' +
      esc(s.session) + (s.checkout ? ', ' + esc(s.checkout) : '') + (s.task_state && s.task_state !== 'open' && s.task_state !== 'unknown' ? ', task ' + esc(s.task_state) : '') + ')</span>' +
      (s.dismissable ? ' <button type="button" class="pill" data-action="dismisssession" data-key="' + esc(tkey(task)) + '" data-host="' + esc(s.host) + '" data-sid="' + esc(s.session_id) +
        '" data-reg="' + esc(s.registered_at) + '" title="Dismiss this registration. This machine records the dismissal in its own session file and publishes it to git, so every dashboard hides the record and the owning machine drops it at its next write. No other machine file is edited; a new registration shows again.">Dismiss</button>' : '') + '</span>';
  }).join('<br>') + '</dd>';
}

/* A task is addressed by the checkout it is read from (two checkouts of one
 * repository may each hold a task with the same path). The repository is the
 * project; the task is SHOWN as <repo>:<stub>, the full path stays the id. */
function ikey(task, item) { return task.checkout + '|' + task.id + '|' + item.id; }
function tkey(task) { return task.checkout + '|' + task.id; }
function identOf(repo, taskPath) { return repo + ':' + String(taskPath).split('/').pop(); }
function identHtml(repo, taskPath) {
  return '<span class="ident mono" title="' + esc(taskPath) + '"><span class="repo">' + esc(repo) + '</span>:' +
    esc(String(taskPath).split('/').pop()) + '</span>';
}
function findTask(key) {
  var tasks = (S.snap && S.snap.tasks) || [];
  for (var i = 0; i < tasks.length; i++) if (tkey(tasks[i]) === key) return tasks[i];
  return null;
}
function splitIKey(k) { var p = k.split('|'); return { project: p[0], task: p[1], item: p[2] }; }
function findItem(k) {
  var parts = splitIKey(k), task = findTask(parts.project + '|' + parts.task);
  if (!task) return null;
  for (var i = 0; i < task.items.length; i++) if (task.items[i].id === parts.item) return { task: task, item: task.items[i] };
  return null;
}

/* Actions that change shared state. They are drawn, disabled, with a title that
 * says when they arrive. The id after data-later names the action. */
var LATER_NOTE = ' arrives in a later increment';
function laterButton(label, cls, icon, id) {
  return '<button type="button" class="btn ' + cls + '" disabled aria-disabled="true" data-later="' + id + '" title="' +
    esc(label + LATER_NOTE) + '">' + (icon ? ic(icon) : '') + esc(label) + '</button>';
}
function laterPill(label, id) {
  return '<button type="button" class="pill" disabled aria-disabled="true" data-later="' + id + '" title="' +
    esc(label + LATER_NOTE) + '">' + esc(label) + '</button>';
}

function caps() { return (S.snap && S.snap.capabilities) || {}; }
function runCap() { return caps().run || { available: false, reason: '' }; }
function canDo(action) {
  var m = caps().mutations || {};
  return !!(m.available && (m.actions || []).indexOf(action) >= 0);
}
function liveButton(label, cls, icon, action, key) {
  return '<button type="button" class="btn ' + cls + '" data-action="' + action + '" data-key="' + esc(key) + '">' +
    (icon ? ic(icon) : '') + esc(label) + '</button>';
}
/* Queue and Retry start unattended work, so they need the llm-kit edge too. */
function runButton(label, cls, icon, action, key) {
  if (canDo(action) && runCap().available) return liveButton(label, cls, icon, action, key);
  var why = runCap().available ? '' : runCap().reason;
  return '<button type="button" class="btn ' + cls + '" disabled aria-disabled="true" data-later="' + action + '" title="' +
    esc(why || (label + LATER_NOTE)) + '">' + (icon ? ic(icon) : '') + esc(label) + '</button>';
}
function runPill(label, action, key) {
  if (canDo(action) && runCap().available) {
    return '<button type="button" class="pill" data-action="' + action + '" data-key="' + esc(key) + '">' + esc(label) + '</button>';
  }
  return '<button type="button" class="pill" disabled aria-disabled="true" data-later="' + action + '" title="' +
    esc(runCap().reason || (label + LATER_NOTE)) + '">' + esc(label) + '</button>';
}

/* Why authoring (Add item, Defer, Undefer) is off for a task right now, or '' when it is not. */
function authoringBlock(task) {
  var holder = holdingSession(task);
  if (holder) return 'Claude Code session ' + holder.session + ' is working this task on ' + holder.host + '; make the change there, or wait for its hand-off.';
  if (task.held) return 'A Claude Code session is working this task; make the change there, or Re-check when it is done.';
  if (task.run_active) return 'A dashboard run is working this task; wait for it to finish, or cancel it.';
  return '';
}
/* A task-folder authoring control: live when the server backs it and nothing works the task, otherwise
 * drawn disabled with the reason (idle) or the later-increment note (not backed). */
function authControl(label, cls, icon, action, key, block) {
  if (!canDo(action)) return laterButton(label, cls, icon, action);
  if (block) {
    return '<button type="button" class="btn ' + cls + '" disabled aria-disabled="true" data-idle="' + action + '" title="' + esc(block) + '">' +
      (icon ? ic(icon) : '') + esc(label) + '</button>';
  }
  return liveButton(label, cls, icon, action, key);
}
function draftOf(scope, id) {
  var bucket = S.drafts[scope] || (S.drafts[scope] = {});
  return bucket[id] || (bucket[id] = {});
}
function dval(scope, id, field, fallback) {
  var d = (S.drafts[scope] || {})[id];
  return d && d[field] !== undefined ? d[field] : (fallback === undefined ? '' : fallback);
}
function fieldId(scope, id, field) { return 'f_' + [scope, id, field].join('_').replace(/[^A-Za-z0-9_-]/g, '_'); }
function dataAttrs(scope, id, field) {
  return 'id="' + esc(fieldId(scope, id, field)) + '" data-draft="' + scope + '" data-id="' + esc(id) + '" data-field="' + field +
    '" data-fk="' + esc(scope + ':' + id + ':' + field) + '"';
}
function textField(scope, id, field, label, extra) {
  return '<div class="field"><label for="' + esc(fieldId(scope, id, field)) + '">' + esc(label) + '</label><input class="f" type="text" ' +
    dataAttrs(scope, id, field) + ' value="' + esc(dval(scope, id, field)) + '" autocomplete="off"' + (extra || '') + '></div>';
}
function areaField(scope, id, field, label) {
  return '<div class="field"><label for="' + esc(fieldId(scope, id, field)) + '">' + esc(label) + '</label><textarea ' +
    dataAttrs(scope, id, field) + '>' + esc(dval(scope, id, field)) + '</textarea></div>';
}
function selectField(scope, id, field, label, options, fallback, extra) {
  var cur = dval(scope, id, field, fallback);
  return '<div class="field"><label for="' + esc(fieldId(scope, id, field)) + '">' + esc(label) + '</label><select ' + dataAttrs(scope, id, field) + (extra || '') + '>' +
    options.map(function (o) { return '<option value="' + esc(o[0]) + '"' + (o[0] === cur ? ' selected' : '') + '>' + esc(o[1]) + '</option>'; }).join('') +
    '</select></div>';
}
var PRIORITIES = [['P1', 'P1'], ['P2', 'P2'], ['P3', 'P3']];

var RUN_LABEL = {
  queued: 'Queued', starting: 'Starting', running: 'Running', cancelling: 'Cancelling', accepted: 'Accepted', blocked: 'Blocked',
  failed: 'Failed', timeout: 'Timed out', cancelled: 'Cancelled', crashed: 'Crashed', skipped: 'Dropped', removed: 'Removed'
};
/* "Queued #n": the entry's place in the queue, in the order the scheduler walks it. */
function runLabel(r) {
  return r.state === 'queued' && r.queue_position ? 'Queued #' + r.queue_position : (RUN_LABEL[r.state] || r.state);
}
/* Why a queued entry is not starting now ("waiting: <detail>"), or '' when nothing holds it back. */
function waitingText(r) {
  return r.state === 'queued' && r.waiting && r.waiting.detail ? 'waiting: ' + r.waiting.detail : '';
}
/* The sub-agent lines of the run controls. Off: why, and the command that turns them on when a characterization is what is
 * missing. On: the limits, when and on which Claude Code they were checked, when the check expires, and what Claude Code
 * documents as outside the two limits. The per-binary denial check (cli_check) adds its own sentence; only `ignored` refuses runs. */
var RECHECK_CODES = ['no-record', 'corrupt', 'stale-binary', 'stale-version', 'probe-version', 'expired', 'check-failed'];
function dayOf(stamp) { return stamp ? String(stamp).slice(0, 10) : '?'; }
function sentenceList(items) {
  if (items.length < 2) return items.join('');
  return items.slice(0, -1).join(', ') + ', and ' + items[items.length - 1];
}
function subagentsHtml() {
  var sub = (caps().subagents || {}).claude;
  if (!sub) return '';
  var chk = sub.cli_check || {}, extra = '', on = sub.state === 'on', head;
  if (chk.state === 'ignored') extra = ' Claude runs are refused: the installed Claude Code does not honor the denials (' + (chk.detail || '') + ').';
  else if (chk.state === 'observed') extra = ' Checked on this Claude Code: the denials take effect.';
  else if (chk.state === 'checking') extra = ' Checking the installed Claude Code...';
  else if (chk.state === 'unobserved' || chk.state === 'vacuous') extra = ' Not observed: ' + (chk.detail || 'the check did not conclude') + '.';
  if (on) {
    var lim = sub.limits || {}, bin = sub.binary || {};
    head = 'Native sub-agents: on (at most ' + lim.concurrent + ' at once, no nesting; checked ' + dayOf(sub.checked_at) + ' on ' +
      (bin.version || 'this Claude Code') + ', recheck by ' + dayOf(sub.expires_at) + ').';
  } else {
    head = 'Native sub-agents: off -- ' + (sub.reason || 'denied by the dashboard floor') + '.';
  }
  var html = '<p class="hint" data-subagents="' + (on ? 'on' : 'off') + '">' + esc(head + extra) + '</p>';
  if (on) {
    html += '<p class="hint" data-subagents-disclosure="1">' + esc('Claude Code enforces these per session; ' + sentenceList(sub.residuals || []) + '. ' +
      (sub.closed || []).join(' ')) + '</p>';
  } else if (sub.command && RECHECK_CODES.indexOf(sub.code) >= 0) {
    html += cmdBox({ commands: [{ label: 'Turn native sub-agents on', command: sub.command }] });
  }
  return html;
}
/* A started run's recorded sub-agent decision, as one row of its Latest run card. */
function runSubagentsRow(run) {
  var sub = run && run.subagents;
  if (!sub || !sub.state) return '';
  var lim = sub.limits || {};
  return '<dt>Sub-agents</dt><dd data-run-subagents="' + esc(sub.state) + '">' +
    esc(sub.state === 'on' ? 'on (at most ' + lim.concurrent + ' at once, no nesting)' : 'off -- ' + (sub.reason || 'denied')) + '</dd>';
}
/* The endpoint helper line of the run controls: which helpers this repository's runs are offered (consent is by repository). */
function helpersHtml(task) {
  var names = (task && task.helpers) || [];
  return '<p class="hint" data-helpers="' + (names.length ? 'offered' : 'none') + '">Endpoint helpers for this repository: ' +
    (names.length ? esc(names.join(', ')) : 'none configured') + '.</p>';
}
/* A run's endpoint helper calls: helper, entry, outcome, duration, characters, tokens and cost when reported. */
function helperCallsHtml(id) {
  var calls = S.runHelpers[id];
  if (!calls) return '';
  var head = '<div class="sec-title">Endpoint helper calls</div>';
  if (calls.error) return head + '<p class="hint">Unavailable: ' + esc(calls.error) + '</p>';
  if (!calls.length) return '';
  return head + calls.map(function (c) {
    var t0 = Date.parse(c.reserved_at), t1 = Date.parse(c.ended_at), parts = [];
    parts.push(c.helper + (c.entry ? ' (' + c.entry + ')' : ''));
    parts.push(c.state === 'completed' ? 'answered' : c.state + (c.status && c.status !== c.state ? ': ' + c.status : ''));
    if (!isNaN(t0) && !isNaN(t1)) parts.push(((t1 - t0) / 1000).toFixed(1) + ' s');
    parts.push(c.input_chars + ' characters in' + (c.output_chars != null ? ', ' + c.output_chars + ' out' : ''));
    var u = c.usage || {}, tok = u.total_tokens || ((u.input_tokens || 0) + (u.output_tokens || 0));
    if (tok) parts.push(tok + ' tokens');
    if (c.reported_cost_usd != null) parts.push('$' + Number(c.reported_cost_usd).toFixed(4));
    return '<div class="mini-run" data-helper-call="' + esc(String(c.id)) + '"><span class="t">' + esc(parts.join(' - ')) + '</span>' +
      (c.detail ? '<span class="m">' + esc(c.detail.split('\n')[0]) + '</span>' : '') + '</div>';
  }).join('');
}
function isLiveRun(r) { return !!r && (r.state === 'queued' || r.state === 'starting' || r.state === 'running' || r.state === 'cancelling'); }
/* Cancel run: live whenever the server backs it and the run can still be cancelled; a run already
 * cancelling shows a disabled "Cancelling" so a second click cannot be mistaken for progress. With no
 * run (or a server that does not back it) the button is drawn disabled. */
function cancelControl(run, cls) {
  if (run && run.state === 'queued' && canDo('remove')) return liveButton('Remove from queue', cls, 'close', 'remove', run.id);
  if (run && run.state === 'cancelling') {
    return '<button type="button" class="btn ' + cls + '" disabled aria-disabled="true" data-idle="cancel" title="The run is being stopped">' +
      ic('stop') + 'Cancelling</button>';
  }
  if (canDo('cancel') && run && run.cancellable) return liveButton('Cancel run', cls, 'stop', 'cancel', run.id);
  if (canDo('cancel')) {
    return '<button type="button" class="btn ' + cls + '" disabled aria-disabled="true" data-idle="cancel" title="No run is queued or running here">' +
      ic('stop') + 'Cancel run</button>';
  }
  return laterButton('Cancel run', cls, 'stop', 'cancel');
}

var STATE_LABEL = {
  'available': 'Available', 'in-flight': 'In flight', 'blocked': 'Needs you',
  'deferred': 'Deferred', 'unknown': 'Unknown state'
};
var STATE_COLOR = {
  'available': 'var(--accent)', 'in-flight': 'var(--run)', 'blocked': 'var(--warn)',
  'deferred': 'var(--muted)', 'unknown': 'var(--bad)'
};
var NEED_KIND = { 'task-blocked': 'Task blocked', 'item-blocked': 'Needs decision', 'invalid': 'Cannot start' };

/* ================= data ================= */
function query() {
  var parts = [];
  if (S.project) parts.push('project=' + encodeURIComponent(S.project));
  if (S.q) parts.push('q=' + encodeURIComponent(S.q));
  if (S.refreshNext) parts.push('refresh=1');
  return parts.length ? '?' + parts.join('&') : '';
}
function api(path) {
  return fetch(path, { headers: { 'X-Task-Kit-Token': TOKEN }, cache: 'no-store' }).then(function (res) {
    if (res.status === 401) { var e = new Error('auth'); e.auth = true; throw e; }
    if (!res.ok) {
      return res.json().then(function (b) {
        throw new Error((b && b.error && b.error.message) || ('HTTP ' + res.status));
      }, function () { throw new Error('HTTP ' + res.status); });
    }
    return res.json();
  });
}
function send(method, path, body) {
  return fetch(path, {
    method: method,
    headers: { 'X-Task-Kit-Token': TOKEN, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    cache: 'no-store'
  }).then(function (res) {
    return res.json().then(function (b) { return { status: res.status, body: b }; },
      function () { return { status: res.status, body: {} }; });
  });
}
function fetchRunLog(id) {
  var cur = (S.runLog[id] || { cursor: 0 }).cursor, first = !S.runLog[id];
  return api('/api/v1/runs/' + encodeURIComponent(id) + '/log?after=' + cur).then(function (r) {
    var old = S.runLog[id] ? S.runLog[id].text : '';
    S.runLog[id] = { text: (old + r.text).slice(-20000), cursor: r.cursor };
    if (S.runOpen[id] && (first || r.text)) render();
  }, function (e) { S.runLog[id] = { text: '(log unavailable: ' + e.message + ')', cursor: 0 }; if (S.runOpen[id]) render(); });
}
/* The endpoint helper calls of a run (GET /api/v1/runs/<id>): the server's own record, never the worker's. */
function fetchRunHelpers(id) {
  return api('/api/v1/runs/' + encodeURIComponent(id)).then(function (d) {
    var calls = d.helper_calls || [], old = S.runHelpers[id];
    S.runHelpers[id] = calls;
    if (S.runOpen[id] && (!old || old.error || JSON.stringify(old) !== JSON.stringify(calls))) render();
  }, function (e) { S.runHelpers[id] = { error: e.message }; if (S.runOpen[id]) render(); });
}
function fetchDetail(k) {
  var p = splitIKey(k);
  return api('/api/v1/items/' + encodeURIComponent(p.project) + '/' + encodeURIComponent(p.task) + '/' + encodeURIComponent(p.item))
    .then(function (d) { S.detail[k] = d; render(); }, function (e) {
      S.detail[k] = { error: e.message };
      render();
    });
}
function poll() {
  if (S.polling) return Promise.resolve();
  S.polling = true;
  var url = '/api/v1/dashboard' + query();
  S.refreshNext = false;
  return api(url).then(function (snap) {
    var changed = !S.snap || snap.revision !== S.renderedRev || S.conn !== 'ok' ||
      JSON.stringify(snap.filters) !== JSON.stringify((S.snap || {}).filters);
    S.snap = snap; S.conn = 'ok'; S.connDetail = '';
    if (changed) {
      S.renderedRev = snap.revision;
      render();
      if (S.sel) fetchDetail(S.sel);
    } else {
      renderStatus();
    }
    Object.keys(S.runOpen).forEach(function (id) { if (S.runOpen[id]) { fetchRunLog(id); fetchRunHelpers(id); } });
  }, function (e) {
    S.conn = e.auth ? 'auth' : 'offline'; S.connDetail = e.message;
    renderBanners(); renderStatus();
  }).then(function () { S.polling = false; schedule(); });
}
function schedule() {
  if (S.timer) clearTimeout(S.timer);
  S.timer = setTimeout(poll, document.visibilityState === 'hidden' ? HIDDEN_POLL_MS : VISIBLE_POLL_MS);
}
function pollNow() {
  if (S.timer) clearTimeout(S.timer);
  poll();
}

/* ================= rendering ================= */
function renderBanners() {
  var h = '';
  if (S.snap && runCap().available) {
    h += '<div class="callout info"><strong>Autonomous runs are on</strong><p>Queue starts one unattended Claude run for one item; ' +
      'a blocked run marks its task as needing you, and Cancel run stops it and everything it started. New task, Add item and Defer ' +
      'change the task folder only while no Claude Code session works that task and no run does. Pause worker is shown disabled: it ' +
      'arrives in a later increment. &ldquo;Resolve in Claude Code&rdquo; only shows commands to copy.</p></div>';
  } else if (S.snap) {
    h += '<div class="callout info"><strong>Autonomous runs are unavailable</strong><p>' + esc(runCap().reason || 'This server cannot start runs.') +
      ' Viewing tasks, New task, Add item, Defer, Re-check and &ldquo;Resolve in Claude Code&rdquo; still work.</p></div>';
  }
  if (S.notice) {
    h += '<div class="callout ' + esc(S.notice.kind) + '"><strong>' + esc(S.notice.title) + '</strong><p>' + esc(S.notice.text) + '</p></div>';
  }
  if (S.conn === 'auth') {
    h += '<div class="callout bad"><strong>' + (TOKEN ? 'This page no longer matches the server' : 'This page has no access token') + '</strong><p>' +
      (TOKEN ? 'The dashboard server restarted, so this page\'s token is stale. ' : 'The token travels in the address after #. ') +
      'Run <span class="mono">task-kit</span> to open the dashboard.</p></div>';
  } else if (S.conn === 'offline') {
    h += '<div class="callout bad"><strong>Cannot reach the dashboard server</strong><p>' + esc(S.connDetail) +
      ' Retrying. If it stopped, run <span class="mono">task-kit</span>.</p></div>';
  }
  var errs = (S.snap && S.snap.errors) || [];
  errs.forEach(function (e) {
    h += '<div class="callout bad"><strong>Discovery problem (' + esc(e.code) + ')</strong><p>' + esc(e.message) + '</p></div>';
  });
  $('banners').innerHTML = h;
}
function renderNewTaskButton() {
  var b = $('newTaskBtn');
  if (!b || !S.snap) return;
  var live = canDo('newtask');
  if (live) {
    b.removeAttribute('disabled'); b.removeAttribute('aria-disabled'); b.removeAttribute('data-later');
    b.setAttribute('data-action', 'newtask'); b.setAttribute('aria-expanded', String(S.newOpen));
    b.setAttribute('title', 'Create a task in dev/tasks of a project');
  }
}
function renderStatus() {
  var chip = $('serverChip');
  var label = { connecting: 'connecting', ok: 'online', offline: 'offline', auth: 'token mismatch' }[S.conn];
  chip.textContent = label;
  chip.className = 'chip' + (S.conn === 'ok' ? '' : ' badchip');
  if (!S.snap) { $('status').textContent = ''; return; }
  var t = S.snap.totals;
  $('status').innerHTML = '<b>' + t.projects + '</b> project(s) - <b>' + t.tasks + '</b> task(s) - <b>' + t.needs + '</b> need you';
}
function renderProjectSelect() {
  var sel = $('projectSelect'), ps = S.snap.projects;
  var sig = ps.map(function (p) { return p.id + ':' + p.name + ':' + p.counts.open + ':' + p.checkouts.join(','); }).join('|') + '#' + S.project;
  if (sel.getAttribute('data-sig') === sig) return;
  sel.setAttribute('data-sig', sig);
  var h = '<option value="">All projects</option>';
  ps.forEach(function (p) {
    h += '<option value="' + esc(p.id) + '"' + (p.id === S.project ? ' selected' : '') + ' title="Checkouts: ' + esc(p.checkouts.join(', ')) + '">' +
      esc(p.name) + ' (' + p.counts.open + ' open)</option>';
  });
  sel.innerHTML = h;
  sel.value = S.project;
}

/* ---- sorting: exactly one is active ---- */
function needN(t) { return (t.needs || []).length; }
function isRunning(it) { return it.ui_state === 'in-flight' || !!(it.run && (it.run.state === 'running' || it.run.state === 'starting' || it.run.state === 'cancelling')); }
function runN(t) { return t.items.filter(isRunning).length; }
function queN(t) { return t.items.filter(function (it) { return !isRunning(it) && it.run && it.run.state === 'queued'; }).length; }
function priOf(t) { return t.priority || 'P9'; }
function lastOf(t) { var w = parseWhen(t.last_activity); return w == null ? -1 : w; }
function priCmp(a, b) { return priOf(a) < priOf(b) ? -1 : priOf(a) > priOf(b) ? 1 : 0; }
function byLatest(a, b) { return lastOf(b) - lastOf(a); }
function byPriority(a, b) { return priCmp(a, b) || byLatest(a, b); }
function tieBreak(a, b) { return a.title.toLowerCase() < b.title.toLowerCase() ? -1 : a.title.toLowerCase() > b.title.toLowerCase() ? 1 : (tkey(a) < tkey(b) ? -1 : tkey(a) > tkey(b) ? 1 : 0); }
var SORTS = [
  { id: 'priority', label: 'Priority', desc: 'priority, then latest activity', cmp: byPriority },
  { id: 'needs', label: 'Needs you first', desc: 'tasks with items waiting on you first, then priority',
    cmp: function (a, b) { return ((needN(b) > 0) - (needN(a) > 0)) || byPriority(a, b); } },
  { id: 'progress', label: 'In progress first', desc: 'tasks with a job in flight first, then priority',
    cmp: function (a, b) { return ((runN(b) > 0) - (runN(a) > 0)) || byPriority(a, b); } },
  { id: 'latest', label: 'Latest activity', desc: 'most recently touched first', cmp: byLatest }
];
function activeSort() {
  for (var i = 0; i < SORTS.length; i++) if (SORTS[i].id === S.sort) return SORTS[i];
  return SORTS[0];
}
function sorted(tasks) {
  var cmp = activeSort().cmp;
  return tasks.slice().sort(function (a, b) { return cmp(a, b) || tieBreak(a, b); });
}
function renderSort() {
  $('sortSeg').innerHTML = SORTS.map(function (x) {
    return '<button type="button" data-action="sort" data-v="' + x.id + '" data-fk="sort:' + x.id + '" aria-pressed="' + (S.sort === x.id) + '">' + esc(x.label) + '</button>';
  }).join('');
}
function chipPri(p) { return p ? '<span class="pri ' + esc(p) + '">' + esc(p) + '</span>' : ''; }

function elapsed(ts) {
  var t = parseWhen(ts);
  if (t == null) return '';
  var m = Math.max(0, Math.floor((Date.now() - t) / 60000));
  return m < 1 ? 'under a minute' : m < 60 ? m + ' min' : Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
}
function renderRuns() {
  var list = S.snap.runs || [];
  if (!list.length) return '';
  var h = '<div class="sec-title">Now running <span class="count">' + list.length +
    ' - one unattended run per checkout; queued runs wait their turn</span></div><div class="run-grid">';
  list.forEach(function (r) {
    h += '<div class="run-card"><div class="proj"><span>' + esc(identOf(r.project_name, r.task)) + '</span><span class="live">' +
      esc(runLabel(r)) + '</span></div><div class="ttl">' + esc(r.item_title) + '</div>' +
      '<div class="sub">' + esc(r.worker || '') + (r.entry ? ' (' + esc(r.entry) + ')' : '') +
      (r.started_at ? ' - running for ' + esc(elapsed(r.started_at)) : ' - ' + esc(waitingText(r) || 'waiting to start')) +
      (r.phase ? ' - ledger ' + esc(r.phase) : '') + '</div>' +
      '<div class="row"><button type="button" class="btn sm" data-action="togglerun" data-key="' + esc(r.id) + '" aria-expanded="' + !!S.runOpen[r.id] + '">' + ic('eye') + (S.runOpen[r.id] ? 'Hide log' : 'Log') + '</button>' +
      cancelControl(r, 'sm danger') + '</div>' + (S.runOpen[r.id] ? runDetailHtml(r) : '') + '</div>';
  });
  return h + '</div>';
}
function renderRecent() {
  var rows = S.snap.recent_runs || [];
  if (!rows.length) return '';
  var h = '<div class="sec-title">Recent runs <span class="count">newest first; click a run for its result and log</span></div>' +
    '<table class="runs-table"><thead><tr><th>Ended</th><th>Item</th><th>Result</th><th></th></tr></thead><tbody>';
  rows.forEach(function (r) {
    h += '<tr class="click" data-action="togglerun" data-key="' + esc(r.id) + '" aria-expanded="' + !!S.runOpen[r.id] + '"><td class="muted when">' + esc(ago(r.ended_at)) + '</td>' +
      '<td>' + esc(r.item_title) + '<div class="muted mono fs11">' + esc(identOf(r.project_name, r.task)) + '</div></td>' +
      '<td><span class="chip' + (r.state === 'accepted' ? '' : ' warnchip') + '">' + esc(RUN_LABEL[r.state] || r.state) + '</span> ' +
      '<span class="muted fs12">' + esc(r.code || '') + '</span></td><td class="muted fs12">' + (S.runOpen[r.id] ? 'hide' : 'details') + '</td></tr>';
    if (S.runOpen[r.id]) h += '<tr class="run-detail"><td colspan="4">' + runDetailHtml(r) + '</td></tr>';
  });
  return h + '</tbody></table>';
}

function needKey(n) { return n.item ? n.checkout + '|' + n.task + '|' + n.item : n.checkout + '|' + n.task; }
function needsHtml(task) {
  var list = task.needs || [];
  if (!list.length) return '';
  var h = '<div class="need-list">';
  list.forEach(function (n) {
    var k = needKey(n), action = n.item ? 'resolvesel' : 'resolvetask';
    h += '<div class="need-row"><div class="kind">' + ic('alert') + esc(NEED_KIND[n.kind] || n.kind) + '</div>' +
      '<div class="ttl">' + esc(n.title) + '</div>' +
      '<div class="why" title="' + esc(n.detail) + '">' + esc(n.detail) + '</div><div class="acts">' +
      '<button type="button" class="btn sm warn" data-action="' + action + '" data-key="' + esc(k) + '">' + ic('term') + 'Resolve in Claude Code</button>' +
      recheckButton(n, 'sm') + '</div></div>';
  });
  return h + '</div>';
}
function recheckButton(n, size) {
  var key = needKey(n);
  if (canDo('recheck')) return liveButton('Re-check', size, 'retry', 'recheck', key);
  return laterButton('Re-check', size, 'retry', 'recheck');
}

function iconFor(item) {
  switch (item.ui_state) {
    case 'in-flight': return ic('play', 'fill');
    case 'blocked': return ic('alert');
    case 'deferred': return ic('pause');
    case 'unknown': return ic('alert');
    default: return ic('play');
  }
}
function nodeHtml(task, item) {
  var k = ikey(task, item), sel = S.sel === k, sub = '';
  switch (item.ui_state) {
    case 'available':
      if (isLiveRun(item.run)) {
        sub = '<span class="t-run">' + esc(runLabel(item.run).toLowerCase()) + '</span>';
      } else if (item.run && item.run.retryable) {
        sub = '<span class="t-warn">' + esc((RUN_LABEL[item.run.state] || item.run.state).toLowerCase()) + '</span>';
      } else {
        sub = runPill('Queue', 'queue', ikey(task, item));
      }
      break;
    case 'in-flight': sub = '<span class="t-run">in flight</span>'; break;
    case 'blocked': sub = '<span class="t-warn">needs you</span>'; break;
    case 'deferred': sub = 'deferred'; break;
    default: sub = '<span class="t-warn">' + esc(item.state) + '</span>';
  }
  var label = item.title + ' - ' + (STATE_LABEL[item.ui_state] || item.state);
  return '<div class="node st-' + esc(item.ui_state) + (sel ? ' sel' : '') + '">' +
    '<button type="button" class="bubble" data-action="select" data-key="' + esc(k) + '" aria-label="' + esc(label) +
    '" aria-pressed="' + sel + '" title="' + esc(label) + '">' + iconFor(item) + '</button>' +
    '<div class="nlabel" title="' + esc(item.title) + '">' + esc(item.title) + '</div><div class="nsub">' + sub + '</div></div>';
}
function findingsHtml(task) {
  var f = task.findings, all = f.errors.concat(f.warnings, f.item_errors), h = '';
  if (all.length) {
    h += '<details class="findings"><summary>' + all.length + ' validation finding(s)</summary><ul>' +
      all.map(function (m) { return '<li>' + esc(m) + '</li>'; }).join('') + '</ul></details>';
  }
  // Information only: the dashboard never commits, so this does not block queueing, acceptance or Re-check.
  (f.info || []).forEach(function (m) { h += '<p class="hint finfo">Not a blocker: ' + esc(m) + '</p>'; });
  return h;
}
function markersHtml(task) {
  var need = needN(task), run = runN(task), q = queN(task), h = '';
  if (need) h += '<span class="mk need" title="Waiting on you: blocked items and blocked or invalid tasks">' + ic('tri') + need + (need === 1 ? ' needs you' : ' need you') + '</span>';
  if (run) h += '<span class="mk run" title="A job is running on this task">' + ic('spin', 'spin') + run + ' in flight</span>';
  if (q) h += '<span class="mk q" title="Waiting in the queue">' + q + ' queued</span>';
  return h + sessionMarksHtml(task);
}
function taskBody(task) {
  var avail = task.items.filter(function (i) { return i.ui_state === 'available' && !isLiveRun(i.run); }).length;
  var open = task.section === 'open';
  var nodes = task.items.map(function (it) { return nodeHtml(task, it); }).join('');
  if (open) nodes += addNodeHtml(task);
  var empty = task.items.length ? '' :
    '<p class="muted fs12 mb0">' + (task.has_items_block ? 'No open items.' : 'No task_items block in plan.md (a pre-contract folder).') + '</p>';
  var ident = task.repo + ':' + task.stub;
  var sel = S.sel && findItem(S.sel);
  return '<div class="tbody"><div class="tmeta">' +
    '<dl class="idl"><dt>Identifier</dt><dd><button type="button" class="task-id-copy" data-action="copy" data-text="' + esc(ident) + '" title="Copy identifier">' + esc(ident) + '</button></dd>' +
    '<dt>Task path</dt><dd><button type="button" class="task-id-copy" data-action="copy" data-text="' + esc(task.id) + '" title="Copy task path">' + esc(task.id) + '</button></dd>' +
    '<dt>Working in</dt><dd class="mono">' + esc(task.working_root) + '</dd>' + sessionRowsHtml(task) + '</dl>' +
    '<div class="tacts">' + (open && avail > 1 ? runButton('Queue all available (' + avail + ')', 'sm', null, 'queueall', tkey(task)) : '') + '</div></div>' +
    needsHtml(task) +
    (nodes ? '<div class="pipeline" role="list">' + nodes + '</div>' : '') + empty +
    (S.addOpen === tkey(task) ? addItemFormHtml(task) : '') +
    (sel && tkey(sel.task) === tkey(task) ? itemDetailHtml(sel.task, sel.item) : '') +
    taskDetailsHtml(task) + '</div>';
}
function addNodeHtml(task) {
  var block = authoringBlock(task), label = 'Add an item to ' + task.title;
  if (!canDo('additem')) {
    return '<div class="node addnode"><button type="button" class="bubble" disabled aria-disabled="true" data-later="additem" title="Add item' + LATER_NOTE +
      '" aria-label="' + esc(label + ' (arrives in a later increment)') + '">' + ic('plus') + '</button><div class="nlabel muted">Add item</div><div class="nsub"></div></div>';
  }
  if (block) {
    return '<div class="node addnode"><button type="button" class="bubble" disabled aria-disabled="true" data-idle="additem" title="' + esc(block) +
      '" aria-label="' + esc(label + ' (' + block + ')') + '">' + ic('plus') + '</button><div class="nlabel muted">Add item</div><div class="nsub"></div></div>';
  }
  return '<div class="node addnode"><button type="button" class="bubble" data-action="additem" data-key="' + esc(tkey(task)) + '" data-fk="add:' + esc(tkey(task)) +
    '" aria-expanded="' + (S.addOpen === tkey(task)) + '" aria-label="' + esc(label) + '" title="Add an item">' + ic('plus') +
    '</button><div class="nlabel muted">Add item</div><div class="nsub"></div></div>';
}
/* The Add item form: inline under the stage map, in the same slot an item's detail uses. */
function addItemFormHtml(task) {
  var tk = tkey(task), err = S.formErr['ai:' + tk];
  var canQueue = canDo('queue') && runCap().available;
  var queueBtn = canQueue
    ? '<button type="button" class="btn primary" data-action="addsubmit" data-key="' + esc(tk) + '" data-queue="1">' + ic('play') + 'Add and queue</button>'
    : '<button type="button" class="btn primary" disabled aria-disabled="true" data-idle="addqueue" title="' + esc(runCap().reason || 'Queue is not available') + '">' + ic('play') + 'Add and queue</button>';
  return '<div class="formcard" data-form="additem"><div class="idhead"><h3>Add an item to ' + esc(task.title) + '</h3></div>' +
    '<div class="cols2"><div>' + textField('ai', tk, 'title', 'Title', ' aria-required="true"') +
    selectField('ai', tk, 'priority', 'Priority', PRIORITIES, 'P2') + '</div><div>' +
    areaField('ai', tk, 'details', 'Details (the worker\'s instructions)') + '</div></div>' +
    '<p class="hint">Details are written to plan.md under a heading for the item. Add and queue needs them: they are what the run is told to do.</p>' +
    '<div class="err-msg" role="alert">' + esc(err || '') + '</div>' +
    '<div class="acts">' + queueBtn +
    '<button type="button" class="btn" data-action="addsubmit" data-key="' + esc(tk) + '" data-queue="0">Add only</button>' +
    '<button type="button" class="btn" data-action="additem" data-key="' + esc(tk) + '">Cancel</button></div></div>';
}
/* The New task form: inline above the task list. */
function newTaskFormHtml() {
  var err = S.formErr.nt, projects = (S.snap && S.snap.projects) || [];
  var fallback = S.project || (projects[0] && projects[0].id) || '';
  var opts = projects.map(function (p) { return [p.id, p.name]; });
  return '<div class="formcard" data-form="newtask"><div class="idhead"><h3>New task</h3>' +
    '<span class="hint">Created in dev/tasks of the project\'s working checkout, as <span class="mono">task init</span> would.</span></div>' +
    '<div class="cols2"><div>' + selectField('nt', '', 'project', 'Project', opts, fallback) +
    textField('nt', '', 'title', 'Title', ' aria-required="true"') + selectField('nt', '', 'priority', 'Priority', PRIORITIES, 'P2') + '</div><div>' +
    textField('nt', '', 'itemtitle', 'First item (optional)') + areaField('nt', '', 'details', 'First item details') + '</div></div>' +
    '<div class="err-msg" role="alert">' + esc(err || '') + '</div>' +
    '<div class="acts"><button type="button" class="btn primary" data-action="createtask">' + ic('plus') + 'Create task</button>' +
    '<button type="button" class="btn" data-action="newtask">Cancel</button></div></div>';
}
function rowHtml(task) {
  var k = tkey(task), open = S.openTask === k, need = needN(task), run = runN(task);
  var cls = 'trow' + (open ? ' open' : '') + (need ? ' has-need' : run ? ' has-run' : '') + (task.section === 'deferred' ? ' deferred' : '');
  var state = task.section === 'open' ? '' : '<span class="chip' + (task.classification === 'invalid' ? ' badchip' : '') + '">' + esc(task.classification) + '</span>';
  var n = task.items.length;
  return '<div class="' + cls + '" data-row="' + esc(k) + '" data-age="' + brightness(task.last_activity).toFixed(2) + '">' +
    '<button type="button" class="row-main" data-action="toggletask" data-key="' + esc(k) + '" data-fk="row:' + esc(k) + '" aria-expanded="' + open + '" title="' + esc(task.summary || task.title) + '">' +
    '<span class="chev" aria-hidden="true"></span>' + (task.priority ? '<span class="pri ' + esc(task.priority) + '">' + esc(task.priority) + '</span>' : '<span class="pri">-</span>') +
    '<span class="c-title"><span class="ttl">' + esc(task.title) + '</span>' + identHtml(task.repo, task.id) + state + '</span>' +
    '<span class="c-marks">' + markersHtml(task) + '</span>' +
    '<span class="c-meta"><span>' + n + ' item' + (n === 1 ? '' : 's') + '</span><span>' + esc(ago(task.last_activity)) + '</span></span></button>' +
    (open ? taskBody(task) : '') + '</div>';
}
function foldSection(key, title, count, body, openByDefault, note) {
  var isOpen = S.open[key] === undefined ? !!openByDefault : S.open[key];
  return '<details class="sec" data-open="' + esc(key) + '"' + (isOpen ? ' open' : '') + '><summary>' + esc(title) +
    ' <span class="count">' + count + (note ? ' - ' + esc(note) : '') + '</span></summary><div class="sec-body">' + body + '</div></details>';
}
function rowList(tasks) { return '<div class="tlist">' + tasks.map(rowHtml).join('') + '</div>'; }
function renderTaskList() {
  var tasks = S.snap.tasks, srt = activeSort();
  var open = sorted(tasks.filter(function (t) { return t.section === 'open'; }));
  var def = sorted(tasks.filter(function (t) { return t.section === 'deferred'; }));
  var closed = tasks.filter(function (t) { return t.section === 'closed'; });  // newest activity first, as served
  var other = sorted(tasks.filter(function (t) { return t.section === 'other'; }));
  var legend = '<div class="listbar"><div class="legend">Markers:<span class="mk need">' + ic('tri') + 'needs you</span> blocked items or tasks' +
    '<span class="mk run">' + ic('spin', 'spin') + 'in flight</span> a job is running</div></div>';
  var h = foldSection('open', 'Open tasks', open.length, legend + (open.length ? rowList(open) :
    '<p class="nomatch">' + (tasks.length ? 'No open tasks match the current filter.' : 'No tasks match the current filter.') + '</p>'),
    true, 'sorted by ' + srt.label + ': ' + srt.desc);
  if (def.length) h += foldSection('deferred', 'Deferred tasks', def.length, rowList(def), false);
  if (closed.length) h += foldSection('closed', 'Closed tasks', closed.length, rowList(closed), false);
  if (other.length) h += foldSection('other', 'Other (invalid, remote, orphaned)', other.length, rowList(other), false);
  if (S.snap.hidden_closed_tasks) h += '<p class="hidden-note">' + S.snap.hidden_closed_tasks + ' older closed task(s) are not listed.</p>';
  return h;
}
function renderLog() {
  var rows = S.snap.activity;
  if (!rows.length) return '';
  var h = '<div class="sec-title">Task log <span class="count">newest log.md entries across the listed tasks</span></div>';
  h += '<table class="runs-table activity-table"><thead><tr><th>When</th><th>Task</th><th>Entry</th></tr></thead><tbody>';
  rows.forEach(function (r) {
    h += '<tr class="click" data-action="selecttask" data-key="' + esc(r.checkout + '|' + r.task) + '"><td class="muted when">' + esc(ago(r.timestamp)) + '</td>' +
      '<td>' + esc(r.task_title) + '<div class="muted mono fs11">' + esc(identOf(r.project_name, r.task)) + '</div></td>' +
      '<td>' + esc(r.detail) + '</td></tr>';
  });
  return h + '</tbody></table>';
}
function renderActivity() {
  var live = (S.snap.runs || []).length, recent = (S.snap.recent_runs || []).length, log = S.snap.activity.length;
  var counts = live + ' running, ' + recent + ' recent run(s), ' + log + ' log entr' + (log === 1 ? 'y' : 'ies');
  var body = renderRuns() + renderRecent() + renderLog();
  return '<details class="sec" data-open="activity"' + (S.open.activity ? ' open' : '') + '><summary>Recent activity <span class="count">' + counts +
    '</span></summary><div class="sec-body">' + (body || '<p class="empty">No runs or dated log entries yet.</p>') + '</div></details>';
}
/* Resolve in Claude Code: exactly two boxes, each with its own copy button. */
function cmdBox(handoff) {
  var rows = handoff.commands.map(function (c) {
    return '<div class="cmdrow"><div class="cmdmain"><div class="cmdlabel">' + esc(c.label) + '</div><code>' + esc(c.command) + '</code></div>' +
      '<button type="button" class="btn sm" data-action="copy" data-text="' + esc(c.command) + '">' + ic('copy') + 'Copy</button></div>';
  }).join('');
  return '<div class="cmdbox">' + rows + '</div>';
}
function planBlock(detail) {
  var ps = detail.plan_section;
  if (ps.error) return '<div class="callout bad"><strong>plan.md could not be read</strong><p>' + esc(ps.error) + '</p></div>';
  if (ps.status === 'found') {
    return '<div class="card"><div class="secbody">' + esc(ps.body) + (ps.truncated ? '\n[truncated]' : '') + '</div></div>' +
      '<p class="hint">' + esc(ps.heading) + ' in ' + esc(ps.source) + '</p>';
  }
  var why = { missing: 'No section for this item', empty: 'The section is empty', duplicate: 'More than one section matches this item' }[ps.status] || ps.status;
  return '<div class="callout"><strong>' + esc(why) + '</strong><p>An autonomous run needs exactly one non-empty section headed with the item id ' +
    '(for example <span class="mono">### ' + esc(detail.item.id) + ' -- title</span>) in ' + esc(ps.source) + '.</p></div>';
}
function logPre(id) {
  var log = S.runLog[id];
  return '<pre class="logtail">' + esc(log ? log.text : 'Loading...') + '</pre>';
}
/* A run's result, controls and log, shown in place (Recent activity and item detail). */
function runDetailHtml(run) {
  var h = '<div class="run-inline">';
  if (run.code || run.detail) h += '<div class="callout"><strong>' + esc(run.code || run.state) + '</strong><p>' + esc(run.detail || '') + '</p></div>';
  h += '<div class="acts">';
  if (run.retryable) h += runButton('Retry', '', 'retry', 'retry', run.id + '|' + run.checkout + '|' + run.task);
  if (run.state === 'blocked' || run.state === 'crashed') h += '<span class="hint">Resolve it in Claude Code, then Re-check the item.</span>';
  if (isLiveRun(run)) h += cancelControl(run, 'sm danger');
  if (run.state === 'cancelled') h += '<span class="hint">Stopped on request. Partial work may be in the shared tree; look at it before you Retry.</span>';
  return h + '</div>' + helperCallsHtml(run.id) + '<div class="sec-title">Run log</div>' + logPre(run.id) + '</div>';
}
function updatesHtml(task) {
  return task.updates.length ? task.updates.map(function (u) {
    return '<div class="mini-run"><span class="m">' + esc(u.date + (u.time ? ' ' + u.time : '')) + '</span><span class="t">' + esc(u.detail) + '</span></div>';
  }).join('') : '<p class="muted fs13">No dated log entries.</p>';
}
/* Task details: why it is blocked, the summary, findings, the log, and the commands that open it in Claude Code. */
function taskDetailsHtml(task) {
  var key = 'T:' + tkey(task), left = '';
  if (task.classification === 'blocked' || task.classification === 'invalid') {
    var rec = (task.needs || []).filter(function (n) { return !n.item; })[0];
    left += '<div class="callout"><strong>' + (task.classification === 'blocked' ? 'This task is blocked' : 'This task does not validate') + '</strong><p>' +
      esc(rec ? rec.detail : task.findings.errors[0] || '') + '</p></div>';
  }
  left += '<div class="sec-title">Summary</div><p class="fs13">' + (task.summary ? esc(task.summary) : '<span class="muted">No summary recorded.</span>') + '</p>';
  left += '<div class="acts"><button type="button" class="btn warn" data-action="resolve" data-key="' + esc(key) + '" aria-expanded="' + !!S.resolve[key] + '">' + ic('term') + 'Resolve in Claude Code</button></div>';
  if (S.resolve[key]) left += cmdBox(task.handoff);
  left += findingsHtml(task);
  return '<div class="tdetails"><div class="sec-title">Task details</div><div class="cols2"><div>' + left + '</div><div><div class="sec-title">Recent log entries</div>' +
    updatesHtml(task) + '</div></div></div>';
}
/* The run options of one item. An available item keeps them as a draft that rides on Queue; a queued item shows its
 * entry's options and saves a change at once; once a run has started they are fixed and drawn disabled. */
function runOptionsHtml(task, it, live) {
  var key = ikey(task, it), run = it.run, queued = !!run && run.state === 'queued';
  var kv = '<dl class="kv mb0">';
  if (!canDo('runoptions')) {
    return kv + '<dt>Worker</dt><dd><span class="seg" role="group" aria-label="Worker"><button type="button" disabled aria-disabled="true" data-later="runoptions">Claude</button>' +
      '<button type="button" disabled aria-disabled="true" data-later="runoptions">Codex</button></span></dd>' +
      '<dt>Effort</dt><dd><select disabled aria-label="Effort" data-later="runoptions"><option>Default</option></select></dd>' +
      '<dt>Runs in</dt><dd class="mono fs12">' + esc(task.working_root) + '</dd></dl>';
  }
  var ro = caps().run_options || {}, workers = ro.workers || {}, defaults = ro.defaults || {};
  var editable = (it.ui_state === 'available' && !live) || queued;
  var d = (S.drafts.opt || {})[key] || {};
  var shown = (queued || live) && run ? run : {};
  var worker = d.worker || shown.worker || defaults.worker || 'claude';
  var effort = d.effort !== undefined ? d.effort : (shown.effort || '');
  var minutes = d.minutes !== undefined ? d.minutes : (shown.timeout_s ? String(Math.round(shown.timeout_s / 6) / 10) : '');
  var save = queued ? ' data-optsave="1"' : '', off = editable ? '' : ' disabled aria-disabled="true"';
  var claude = workers.claude || { available: false, efforts: [], reason: 'No run options from the server.' };
  var codex = workers.codex || { available: false, efforts: [], reason: 'No run options from the server.' };
  function seg(name, info) {
    var usable = editable && info.available, pressed = worker === name;
    return '<button type="button" ' + (usable ? 'data-action="optworker" data-v="' + name + '" data-key="' + esc(key) + '"' :
      'disabled aria-disabled="true" data-idle="optworker"') + ' aria-pressed="' + pressed + '"' +
      (info.available ? '' : ' title="' + esc(info.reason || 'Not available') + '"') + '>' + (name === 'claude' ? 'Claude' : 'Codex') + '</button>';
  }
  var efforts = ((workers[worker] || claude).efforts || []);
  var effortOpts = '<option value="">Default' + (defaults.effort ? ' (' + esc(defaults.effort) + ')' : '') + '</option>' +
    efforts.map(function (e) { return '<option value="' + esc(e) + '"' + (e === effort ? ' selected' : '') + '>' + esc(e) + '</option>'; }).join('');
  var timeoutHint = defaults.timeout_s ? String(Math.round(defaults.timeout_s / 60)) : '';
  return kv + '<dt>Worker</dt><dd><span class="seg" role="group" aria-label="Worker">' + seg('claude', claude) + seg('codex', codex) + '</span></dd>' +
    '<dt>Effort</dt><dd><select ' + dataAttrs('opt', key, 'effort') + save + off + ' aria-label="Effort">' + effortOpts + '</select></dd>' +
    '<dt>Time limit</dt><dd><input class="f narrow" type="number" min="1" step="1" ' + dataAttrs('opt', key, 'minutes') + save + off +
    ' aria-label="Time limit in minutes" placeholder="' + esc(timeoutHint) + '" value="' + esc(minutes) + '"> minutes</dd>' +
    '<dt>Runs in</dt><dd class="mono fs12">' + esc(task.working_root) + '</dd></dl>' +
    '<div class="err-msg" role="alert">' + esc(S.formErr['opt:' + key] || '') + '</div>' +
    '<p class="hint">' + (queued ? 'Saved to the queued entry as you change it.' : editable ? 'Used when you press Queue.' : 'Fixed once a run has started.') + '</p>';
}
/* Item detail: shown under the stage map of the task that holds the selected item. */
function itemDetailHtml(task, it) {
  var key = ikey(task, it), detail = S.detail[key], ui = it.ui_state;
  var left = '<div class="badges"><span class="badge" data-color="' + esc(ui) + '">' + esc(STATE_LABEL[ui] || it.state) + '</span>' +
    chipPri(it.priority) + '<span class="chip mono">' + esc(it.id) + '</span></div>';
  if (ui === 'blocked') {
    left += '<div class="callout"><strong>Waiting for you</strong><p>' + esc(it.note || 'The item is blocked on you; no note was recorded.') + '</p></div>';
  } else if (ui === 'in-flight') {
    left += '<div class="callout info"><strong>In flight</strong><p>' + esc(it.note || 'This item is being worked in Claude Code.') + '</p></div>';
  } else if (ui === 'deferred') {
    left += '<div class="callout"><strong>Deferred</strong><p>' + esc(it.note || 'Not eligible to run until it is made available again.') + '</p></div>';
  } else if (ui === 'unknown') {
    left += '<div class="callout bad"><strong>Unknown item state</strong><p>State ' + esc(it.state) + ' is not in the task vocabulary; <span class="mono">task validate</span> reports it.</p></div>';
  } else if (it.note) {
    left += '<p class="fs13">' + esc(it.note) + '</p>';
  }
  left += '<div class="acts"><button type="button" class="btn warn" data-action="resolve" data-key="' + esc(key) + '" aria-expanded="' + !!S.resolve[key] + '">' + ic('term') + 'Resolve in Claude Code</button>';
  var live = isLiveRun(it.run);
  var block = authoringBlock(task);
  if (ui === 'available' && !live) left += runButton('Queue', 'primary', 'play', 'queue', key) + authControl('Defer', '', null, 'defer', key, block);
  if (ui === 'available' && live) left += '<span class="chip">' + esc(runLabel(it.run)) + '</span>';
  if (ui === 'available' && it.run && it.run.retryable) left += runButton('Retry', '', 'retry', 'retry', it.run.id + '|' + task.checkout + '|' + task.id);
  if (ui === 'deferred') left += authControl('Undefer', 'primary', null, 'undefer', key, block);
  if (ui === 'blocked') left += recheckButton({ item: it.id, project: task.project, checkout: task.checkout, task: task.id }, '');
  left += '</div>';
  if (S.resolve[key]) left += detail && detail.handoff ? cmdBox(detail.handoff) : (task.handoff ? cmdBox(task.handoff) : '');
  left += '<div class="sec-title">Run controls</div><div class="card">' + runOptionsHtml(task, it, live) + subagentsHtml() + helpersHtml(task) +
    '<div class="acts">' + cancelControl(live ? it.run : null, 'sm danger') + '</div>' +
    '<p class="hint">' + (canDo('runoptions')
      ? 'Worker, effort and time limit belong to one queue entry; what you leave empty uses the machine defaults from task.local.yaml. '
      : 'Worker, effort and time limit arrive in a later increment: a run uses the machine defaults from task.local.yaml. ') +
    'Cancel run stops the run and everything it started; a run also ends by itself or at its time limit.</p></div>';
  var right = '';
  if (it.run) {
    right += '<div class="sec-title">Latest run</div><div class="card"><dl class="kv mb0"><dt>State</dt><dd>' + esc(RUN_LABEL[it.run.state] || it.run.state) +
      esc(waitingText(it.run) ? ' -- ' + waitingText(it.run) : '') +
      '</dd><dt>Result</dt><dd>' + esc(it.run.code || '') + (it.run.detail ? ' - ' + esc(it.run.detail) : '') + '</dd>' + runSubagentsRow(it.run) + '</dl>' +
      '<div class="acts"><button type="button" class="btn sm" data-action="togglerun" data-key="' + esc(it.run.id) + '" aria-expanded="' + !!S.runOpen[it.run.id] + '">' + ic('eye') +
      (S.runOpen[it.run.id] ? 'Hide run log' : 'Run log') + '</button></div>' + (S.runOpen[it.run.id] ? helperCallsHtml(it.run.id) + logPre(it.run.id) : '') + '</div>';
  }
  right += '<div class="sec-title">plan.md section</div>';
  if (!detail) right += '<p class="muted fs13">Loading...</p>';
  else if (detail.error) right += '<div class="callout bad"><strong>Could not load the item</strong><p>' + esc(detail.error) + '</p></div>';
  else right += planBlock(detail);
  return '<div class="idetail" data-item="' + esc(key) + '"><div class="idhead"><span class="mono fs12 muted">' + esc(task.repo + ':' + task.stub) + '</span>' +
    '<h3>' + esc(it.title) + '</h3><span class="grow"></span><button type="button" class="btn sm" data-action="select" data-key="' + esc(key) + '" aria-label="Close item details">' + ic('close') + '</button></div>' +
    '<div class="cols2"><div>' + left + '</div><div>' + right + '</div></div></div>';
}
function applyDynamic(root) {
  var nodes = root.querySelectorAll('[data-age]'), i;
  for (i = 0; i < nodes.length; i++) nodes[i].style.setProperty('--age', nodes[i].getAttribute('data-age'));
  nodes = root.querySelectorAll('.badge[data-color]');
  for (i = 0; i < nodes.length; i++) {
    var c = nodes[i].getAttribute('data-color');
    nodes[i].style.setProperty('--c', STATE_COLOR[c] || 'var(--' + c + ')');
  }
}
function render(opts) {
  if (!S.snap) return;
  var ae = document.activeElement, fk = ae && ae.getAttribute ? ae.getAttribute('data-fk') : null;
  var caret = fk && typeof ae.selectionStart === 'number' ? [ae.selectionStart, ae.selectionEnd] : null;
  renderBanners(); renderStatus(); renderProjectSelect(); renderSort(); renderNewTaskButton();
  var main = $('main');
  main.innerHTML = (S.newOpen ? newTaskFormHtml() : '') + renderTaskList() + renderActivity();
  applyDynamic(main);
  if (fk) {
    var again = document.querySelector('[data-fk="' + fk.replace(/"/g, '\\"') + '"]');
    if (again) {
      again.focus({ preventScroll: true });
      if (caret && typeof again.setSelectionRange === 'function') again.setSelectionRange(caret[0], caret[1]);
    }
  }
  if (opts && opts.scrollTo) {
    var row = document.querySelector('[data-row="' + opts.scrollTo.replace(/"/g, '\\"') + '"]');
    if (row) row.scrollIntoView({ block: 'nearest' });
  }
}

/* ================= actions ================= */
function copyText(el) {
  var txt = el.getAttribute('data-text'), orig = el.innerHTML;
  function done(ok) { el.textContent = ok ? 'Copied' : 'Copy failed'; setTimeout(function () { el.innerHTML = orig; }, 1400); }
  function legacy() {
    var ta = document.createElement('textarea');
    ta.value = txt; ta.setAttribute('readonly', '');
    document.body.appendChild(ta); ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    ta.remove(); done(ok);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(txt).then(function () { done(true); }, legacy);
  else legacy();
}
function selectItem(k, withResolve) {
  var p = splitIKey(k);
  S.openTask = p.project + '|' + p.task;
  S.sel = k;
  if (withResolve) S.resolve[k] = true;
  render({ scrollTo: withResolve ? S.openTask : null });
  fetchDetail(k);
}
/* A bubble toggles its item detail: select, swap to another bubble, or close the selected one. */
function toggleItem(k) {
  if (S.sel === k) { S.sel = null; render(); } else selectItem(k, false);
}
/* Open a task in the main list (one at a time), reveal its group and scroll it into view. */
function openTaskInList(k, withResolve) {
  var task = findTask(k);
  if (!task) return;
  if (S.sel && S.sel.split('|').slice(0, 2).join('|') !== k) S.sel = null;
  S.openTask = k;
  S.open[task.section] = true;
  if (withResolve) S.resolve['T:' + k] = true;
  render({ scrollTo: k });
}
function errorText(r) {
  var e = (r.body && r.body.error) || {};
  var text = e.message || ('HTTP ' + r.status);
  if (e.code === 'partial-write' && e.written) text += ' Changed: ' + e.written.join(', ') + '. Not changed: ' + (e.not_written || []).join(', ') + '.';
  return text;
}
function actionResult(label, r) {
  if (r.status >= 200 && r.status < 300) {
    S.notice = null;
    return;
  }
  var e = (r.body && r.body.error) || {};
  S.notice = { kind: r.status === 409 ? 'info' : 'bad', title: label + ' was not done' + (e.blocked ? ' - the task is now blocked' : ''), text: errorText(r) };
}
/* Re-check reports working-session registrations and never releases one. */
function recheckSessionsNotice(r, task) {
  var found = r.status >= 200 && r.status < 300 && r.body && r.body.sessions;
  if (!found || !found.length) return;
  S.notice = { kind: 'info', title: 'Re-check done; working-session registrations remain',
    text: 'Re-check does not release a registration. Still registered: ' + found.map(sessionText).join('; ') +
      '. A registration ends at /task hand-off, or with: task sessions release ' + task.id + ' --all-local' };
}
/* Dismiss records, in this machine's own session file, a dismissal of another machine's unverified registration, then commits and pushes it (dismiss_and_publish); every reader hides the record. No other machine's file is edited. */
function doDismissSession(el) {
  var p = splitIKey(el.getAttribute('data-key') + '|-');
  send('POST', '/api/v1/sessions/dismiss', { checkout: p.project, task: p.task, host: el.getAttribute('data-host'),
    session_id: el.getAttribute('data-sid'), registered_at: el.getAttribute('data-reg') })
    .then(function (r) { actionResult('Dismiss', r); render(); pollNow(); });
}
function enc(x) { return encodeURIComponent(x); }
/* The run options of an item as the server wants them: only what the person set. Null (with a message) when one is unusable. */
function optionsOf(key) {
  var d = (S.drafts.opt || {})[key] || {}, out = {};
  if (d.worker) out.worker = d.worker;
  if (d.effort) out.effort = d.effort;
  if (d.minutes !== undefined && String(d.minutes).trim() !== '') {
    var m = Number(d.minutes);
    if (!(m > 0)) return null;
    out.timeout_s = Math.round(m * 60);
  }
  return out;
}
function doQueue(el) {
  var key = el.getAttribute('data-key'), p = splitIKey(key), f = findItem(key);
  if (!f) return;
  var options = optionsOf(key), body = { expected_revision: f.task.revision };
  if (options === null) { S.formErr['opt:' + key] = 'The time limit must be a positive number of minutes.'; render(); return; }
  S.formErr['opt:' + key] = null;
  if (Object.keys(options).length) body.options = options;
  send('POST', '/api/v1/items/' + enc(p.project) + '/' + enc(p.task) + '/' + enc(p.item) + '/queue', body)
    .then(function (r) {
      actionResult('Queue', r);
      if (r.status >= 200 && r.status < 300) delete S.drafts.opt[key];
      pollNow();
    });
}
function doRecheck(el) {
  var k = el.getAttribute('data-key'), p = splitIKey(k), task = findTask(p.project + '|' + p.task);
  if (!task) return;
  send('POST', '/api/v1/items/' + enc(p.project) + '/' + enc(p.task) + '/' + enc(p.item || '-') + '/recheck',
    { expected_revision: task.revision }).then(function (r) {
      actionResult('Re-check', r);
      recheckSessionsNotice(r, task);
      render(); // the poll renders only when the snapshot changed, and a notice is not part of it
      pollNow();
    });
}
function doRetry(el) {
  var parts = el.getAttribute('data-key').split('|'), task = findTask(parts[1] + '|' + parts[2]);
  if (!task) return;
  send('POST', '/api/v1/runs/' + enc(parts[0]) + '/retry', { expected_revision: task.revision })
    .then(function (r) { actionResult('Retry', r); pollNow(); });
}
function doCancel(el) {
  send('POST', '/api/v1/runs/' + enc(el.getAttribute('data-key')) + '/cancel', {})
    .then(function (r) { actionResult('Cancel', r); pollNow(); });
}
function doRemove(el) {
  send('DELETE', '/api/v1/queue/' + enc(el.getAttribute('data-key')), {})
    .then(function (r) { actionResult('Remove from queue', r); pollNow(); });
}
function doQueueAll(el) {
  var task = findTask(el.getAttribute('data-key'));
  if (!task) return;
  send('POST', '/api/v1/tasks/' + enc(task.checkout) + '/' + enc(task.id) + '/queue', { expected_revision: task.revision })
    .then(function (r) { actionResult('Queue all', r); pollNow(); });
}
function doItemState(el, state) {
  var key = el.getAttribute('data-key'), p = splitIKey(key), f = findItem(key);
  if (!f) return;
  send('PATCH', '/api/v1/items/' + enc(p.project) + '/' + enc(p.task) + '/' + enc(p.item), { expected_revision: f.task.revision, state: state })
    .then(function (r) { actionResult(state === 'deferred' ? 'Defer' : 'Undefer', r); pollNow(); });
}
/* New task: open or close the inline form. */
function toggleNewTask() {
  S.newOpen = !S.newOpen;
  S.formErr.nt = null;
  render();
  if (S.newOpen) { var first = document.querySelector('[data-fk="nt::title"]'); if (first) first.focus({ preventScroll: false }); }
}
function doCreateTask() {
  var d = draftOf('nt', ''), title = (d.title || '').trim(), itemTitle = (d.itemtitle || '').trim(), details = (d.details || '').trim();
  var projects = (S.snap && S.snap.projects) || [];
  var project = d.project || S.project || (projects[0] && projects[0].id) || '';
  var fail = function (m) { S.formErr.nt = m; render(); };
  if (!title) return fail('Give the task a title.');
  if (!project) return fail('There is no project to create the task in.');
  if (details && !itemTitle) return fail('Give the first item a title, or clear its details.');
  var body = { title: title, priority: d.priority || 'P2' };
  if (itemTitle) body.first_item = { title: itemTitle, details: details };
  if (S.busy.nt) return;
  S.busy.nt = true;
  send('POST', '/api/v1/projects/' + enc(project) + '/tasks', body).then(function (r) {
    S.busy.nt = false;
    if (r.status === 201) {
      S.drafts.nt = {}; S.newOpen = false; S.formErr.nt = null;
      if (r.body.task) { S.openTask = tkey(r.body.task); S.open.open = true; S.sel = null; }
      S.notice = r.body.followup_error
        ? { kind: 'info', title: 'The task was created, but its priority or first item was not added', text: r.body.followup_error.message }
        : null;
    } else {
      S.formErr.nt = errorText(r);
    }
    pollNow();
    render();
  });
}
function toggleAddItem(el) {
  var key = el.getAttribute('data-key');
  S.addOpen = S.addOpen === key ? null : key;
  S.formErr['ai:' + key] = null;
  S.openTask = key;
  render();
  if (S.addOpen) { var first = document.querySelector('[data-fk="ai:' + key + ':title"]'); if (first) first.focus({ preventScroll: true }); }
}
function doAddItem(el) {
  var key = el.getAttribute('data-key'), queue = el.getAttribute('data-queue') === '1', task = findTask(key);
  if (!task || S.busy['ai:' + key]) return;
  var d = draftOf('ai', key), details = (d.details || '').trim();
  if (!(d.title || '').trim()) { S.formErr['ai:' + key] = 'Give the item a title.'; render(); return; }
  if (queue && !details) { S.formErr['ai:' + key] = 'Add and queue needs details: they are the worker\'s instructions.'; render(); return; }
  S.formErr['ai:' + key] = null;
  S.busy['ai:' + key] = true;
  send('POST', '/api/v1/tasks/' + enc(task.checkout) + '/' + enc(task.id) + '/items',
    { expected_revision: task.revision, title: d.title.trim(), priority: d.priority || 'P2', details: details, queue: queue })
    .then(function (r) {
      S.busy['ai:' + key] = false;
      if (r.status >= 200 && r.status < 300) {
        delete S.drafts.ai[key]; S.addOpen = null; S.formErr['ai:' + key] = null;
        S.notice = r.body.queue_error
          ? { kind: 'info', title: 'The item was added, but it was not queued', text: r.body.queue_error.message }
          : null;
      } else {
        S.formErr['ai:' + key] = errorText(r);
      }
      pollNow();
      render();
    });
}
/* Save the options of a queued entry. The controls show the entry's own values until the person changes one. */
function saveRunOptions(key) {
  var f = findItem(key);
  if (!f || !f.item.run || f.item.run.state !== 'queued') return;
  var run = f.item.run, d = (S.drafts.opt || {})[key] || {}, options = {};
  var worker = d.worker || run.worker, effort = d.effort !== undefined ? d.effort : run.effort;
  var minutes = d.minutes !== undefined ? d.minutes : (run.timeout_s ? run.timeout_s / 60 : '');
  if (worker) options.worker = worker;
  if (effort) options.effort = effort;
  if (String(minutes).trim() !== '') {
    var m = Number(minutes);
    if (!(m > 0)) { S.formErr['opt:' + key] = 'The time limit must be a positive number of minutes.'; render(); return; }
    options.timeout_s = Math.round(m * 60);
  }
  var p = splitIKey(key);
  S.formErr['opt:' + key] = null;
  send('PUT', '/api/v1/items/' + enc(p.project) + '/' + enc(p.task) + '/' + enc(p.item) + '/run-options', { options: options })
    .then(function (r) {
      if (r.status >= 200 && r.status < 300) { delete S.drafts.opt[key]; S.formErr['opt:' + key] = null; } else { S.formErr['opt:' + key] = errorText(r); }
      pollNow();
      render();
    });
}
function setWorker(el) {
  var key = el.getAttribute('data-key');
  draftOf('opt', key).worker = el.getAttribute('data-v');
  var f = findItem(key);
  if (f && f.item.run && f.item.run.state === 'queued') saveRunOptions(key); else render();
}
function toggleTask(el) {
  var k = el.getAttribute('data-key'), was = S.openTask;
  S.openTask = was === k ? null : k;
  if (S.sel) {
    var p = splitIKey(S.sel);
    if (S.openTask !== p.project + '|' + p.task) S.sel = null;
  }
  render({ scrollTo: S.openTask });
}
function toggleRun(id) {
  S.runOpen[id] = !S.runOpen[id];
  render();
  if (S.runOpen[id]) { fetchRunLog(id); fetchRunHelpers(id); }
}
var ACTIONS = {
  queue: doQueue,
  recheck: doRecheck,
  dismisssession: doDismissSession,
  retry: doRetry,
  cancel: doCancel,
  remove: doRemove,
  queueall: doQueueAll,
  defer: function (el) { doItemState(el, 'deferred'); },
  undefer: function (el) { doItemState(el, 'available'); },
  newtask: toggleNewTask,
  createtask: doCreateTask,
  additem: toggleAddItem,
  addsubmit: doAddItem,
  optworker: setWorker,
  togglerun: function (el) { toggleRun(el.getAttribute('data-key')); },
  select: function (el) { toggleItem(el.getAttribute('data-key')); },
  selecttask: function (el) { openTaskInList(el.getAttribute('data-key'), false); },
  resolvesel: function (el) { selectItem(el.getAttribute('data-key'), true); },
  resolvetask: function (el) { openTaskInList(el.getAttribute('data-key'), true); },
  resolve: function (el) { var k = el.getAttribute('data-key'); S.resolve[k] = !S.resolve[k]; render(); },
  toggletask: toggleTask,
  sort: function (el) { S.sort = el.getAttribute('data-v'); render(); },
  copy: function (el) { copyText(el); },
  refresh: function () { S.refreshNext = true; pollNow(); }
};
document.addEventListener('click', function (e) {
  var el = e.target.closest('[data-action]');
  if (!el || el.disabled) return;
  var fn = ACTIONS[el.getAttribute('data-action')];
  if (!fn) return;
  e.stopPropagation();
  fn(el);
});
/* What a person types is kept in S.drafts, not in the markup, so a re-render (the poll) never loses it. */
function recordDraft(e) {
  var el = e.target;
  if (!el || !el.getAttribute || !el.getAttribute('data-draft')) return;
  draftOf(el.getAttribute('data-draft'), el.getAttribute('data-id') || '')[el.getAttribute('data-field')] = el.value;
}
document.addEventListener('input', recordDraft);
document.addEventListener('change', function (e) {
  recordDraft(e);
  var el = e.target;
  if (el && el.getAttribute && el.getAttribute('data-optsave') === '1') saveRunOptions(el.getAttribute('data-id'));
});
document.addEventListener('toggle', function (e) {
  var k = e.target.getAttribute && e.target.getAttribute('data-open');
  if (k) S.open[k] = e.target.open;
}, true);
document.addEventListener('visibilitychange', function () {
  if (document.visibilityState === 'visible') pollNow(); else schedule();
});
$('projectSelect').addEventListener('change', function (e) { S.project = e.target.value; pollNow(); });
var searchTimer = null;
$('search').addEventListener('input', function (e) {
  S.q = e.target.value.trim();
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(pollNow, 200);
});

renderStatus();
renderBanners();
renderSort();
if (!TOKEN) {
  S.conn = 'auth';
  renderBanners(); renderStatus();
} else {
  poll();
}
})();
