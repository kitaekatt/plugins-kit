# task-kit task metadata: questions and answers -- design plan

Status: design only (unit `summary-on-commit-plan`, round 3, 2026-10-21). No code changed.
Code facts are as of plugins-kit dev `d6bb301a` (task-kit 0.13.0, llm-kit 0.3.2, bootstrap
0.158.3); tasks-repo measurements are at tasks `ed36ca41`. Paths are relative to
`plugins/task-kit/skills/task/scripts/` unless they start with `plugins/`, `tests/`, `docs/`
or `~`.

## Question

How should task-kit produce each task's orientation summary so the dashboard detail view can
orient first (what the task is, where it stands, what is next)? The user widened it: the summary
is one instance of durable task metadata, organised as questions and answers, computed by the
cheapest capable answerer, kept by a per-machine task-kit service the user turns on or off, with
a separate switch for paid usage.

## Binding rulings (user, verbatim)

> Generated summary. Maybe this can be tied to the git commit process somehow with a script
> that calls a local qwen relay falling back to luna med.

> one thing that I think is a helpful concept to consider is we could as a feature offer taking
> a task and upgrading it to a newer schema version. These upgrades could include llm analysis
> to fill out metadata that would make it easier to display rich task information in a task.
> We have the technology to do this the qwen38-5090 endpoint. I am imagining a world where when
> we update the task schema we just automatically update all the tasks to the new schema.

> we can make use of the llm-kit capabilities to do this the default can be luna med/haiku med,
> but we can prefer our two qwen38 endpoints

> so think not just in terms of what we know about tasks today but include what we can known
> about them with cheap llm calls that durably encode those insights as metadata.

> sure i imagine these are things we'll iterate on. But the core idea is questions and answers
> computed via meta-data.

Round 2:

> I don't think we need to directly associate computation and commit out of sync means out of
> sync.

> I was thinking of this more as a service that is on or off and why not have a checkbox for
> spend usage

> Index: even cheaper than qwen is answered by script

The user stated no preference on the first question set (round 2).

Round 3 (option picks and text, verbatim):

> Per machine (Recommended)

> Yes, all repos

> Dashboard only

> You know sure let's do Claude code sessions start it as well

> Regenerate should focus on the most recently modified tasks first for regeneration

Distributed-truth rulings this design leans on (`docs/reference/distributed-truth.md`):

> if the data isn't in the task file, wherever it goes still needs to be commited, because we
> run this dashboard on multiple machines

> I don't like any dependency on the dashboard running

> I don't think there should be anything machine specific except for private information
> specific to this machine

> i mentioned including the process so we can verify local machines. For an external machine i
> agree for now that can be unverified. Eventually we will make it so the task-kit service is
> running on all the machines all the time, and then you can ask the other machine if it's valid
> or not, but that's for the future

## Recommendation in brief

1. **Primitive.** A QUESTION is declared data: id, version, the input readers it uses, an
   answer schema, the statuses it applies to, and its answerers. Its ANSWER carries provenance:
   inputs fingerprint, as-of commit, answerer, attempts. The catalog is data (shipped files plus
   a fleet layer); a question that uses existing readers and answerers needs no code.
2. **Cheapest capable answerer first, enforced in phases.** Script (deterministic; computed on
   read, never stored), then a free-only model phase (llm-kit registry entries with
   `billing: unmetered`: `qwen38-5090`, `qwen38-m5`), then a paid-only model phase (`luna`,
   `haiku`, effort medium) only when this machine's spend switch is on. Provenance names the
   answerer.
3. **Out of sync means out of sync.** No coupling to commits. An answer is current when its
   stored fingerprint equals the fingerprint of the question definition plus its inputs;
   otherwise it is shown stale with its as-of commit.
4. **Storage.** Model answers live in `<task>/answers/<question id>/<host id>.yaml`, committed,
   one writer per file, with the machine-fingerprint ownership check working sessions use.
   Every machine chooses the same winner, independent of configuration and clocks.
5. **The task-kit service.** One background process per machine, not the dashboard process, with
   two per-machine switches: `service` and `spend` (dashboard checkboxes, CLI equivalents).
   Started by the dashboard launcher and by Claude Code SessionStart (startup, resume, clear);
   stopped through an identity-checked control protocol. Each pass answers a bounded batch, most
   recently modified tasks first, computing outside the repository lock and committing and
   publishing only its own answer files under it. Off: answers go stale visibly; `task.py
   answers refresh` does a pass by hand.
6. **Schema versions.** v1 versions the question catalog only; task-format migrations are
   deferred until a structural change exists (D5).
7. **Model chain through llm-kit.** One declaration in preference order; tiers are applied as
   `exclude`; a task error or rejected answer re-selects with every attempted entry excluded.
8. **First questions:** `orientation` and `decisions` (model); `activity` and `task_commits`
   (script).

## Premise outcomes

| Premise | Outcome | Evidence |
|---|---|---|
| task-kit generates a one-line `task.summary` with Codex Luna at medium effort, triggered by `review`, with fingerprint and provenance | CONFIRMED, with a correction: provenance is a date only | `summary_ops.py:22-29`, `:121-155`, `:267-275`; `task.py:353-355` |
| Writing a summary does not change the fingerprint | CONFIRMED | `listing.py:26`, `listing.py:92-109`; `discovery.py:189-214` |
| Bookkeeping log lines are harmless to freshness | REFUTED | the detail-view mockups' sample task has 22 of 25 dated log lines reading "update: refresh (no field edits)" (`docs/planning/task-kit-dashboard/detail-view/index.html`); each enters the fingerprint (`listing.py:92-109`) |
| log.md timestamps can order tasks across machines | REFUTED | `discovery.log_timestamp` writes local time with no offset (`discovery.py:220-222`); edits to CLAUDE.md or plan.md need not append an entry |
| Task folders are committed only in the private tasks repo | CONFIRMED | `dev/tasks -> /Users/christina/Dev/tasks/plugins-kit`; plugins-kit ignores `dev/tasks/` (`docs/reference/task-folders.md`) |
| Who commits in the tasks repo, and how often | MEASURED | Writers: agents (tasks-repo `CLAUDE.md` "Always push"), repo-sync `auto_commit: true`, `session_publish.py:330`. Counts (git 2.54.0, tasks HEAD `ed36ca41`): `git -C ~/Dev/tasks log --no-merges --since=2026-10-14T00:00:00-05:00 --until=2026-10-21T00:00:00-05:00 --oneline` = 477 commits; with `--name-only`, grouping paths of three or more components by their first two (project/stub) and ignoring `.sessions`: 462 touched one task folder, 11 touched 2 to 6, 1 touched 16, 3 touched none. 75 `task.yaml` files |
| llm-kit expresses an ordered declaration with fallback | CONFIRMED, with two limits | `llm_kit.dispatch.declaration.run(names, RunRequest, exclude=, max_attempts=, reachability_cache=)` (`plugins/llm-kit/lib/llm_kit/dispatch/declaration.py:870-907`); CLI `llm-kit completion complete --models a,b,c --effort medium` (`cli/completion.py:303-311`, `cli/_common.py:69-79`); `llm-kit models describe <ids> --caller process`. Measured on this Mac: `describe qwen38-5090 qwen38-m5 luna haiku --caller process` lists all four usable in declared order, and with `--requirements '{"params":["effort"]}'` all four stay usable. Limits: `run` moves on only on a classified halt or launch failure (`declaration.py:1036-1043`), excluding halted entries only within one call (`:953-955`); per-entry effort exists only in `llm_kit.jobs` (`jobs/model.py:26-30`) |
| Free versus paid, and the provider, are knowable from llm-kit | CONFIRMED | `EndpointEntry.billing_mode` (`registry/model_endpoints.py:160`, `:464-510`); `qwen38-5090`, `qwen38-m5` declare `billing: {mode: unmetered}` (`~/.claude/config/model-endpoints.yaml`); shipped entries carry `family` (`luna`: `openai`, `haiku`: `anthropic`, `registry/models.py:104-105` and the claude entries below them) |
| The two qwen38 endpoints | CONFIRMED | `qwen38-5090` (`http://5090w.localdomain:8080/v1`, 262144 context), `qwen38-m5` (`http://m5pro.localdomain:8000/v1`, 32768 context); naming them bypasses the front door `qwen38`, whose third tier is DeepSeek Flash on OpenRouter (local-llm-domain `SKILL.md`, "Front door") |
| Standing authorization covers sending task files to OpenAI and Anthropic | REFUTED as a blanket claim; resolved by the user's explicit consent through the spend switch (round 3, "Yes, all repos") | `private_ai_review_and_consultation` limits exclude credentials, secrets and personal data (`~/.claude/USER-PREFERENCES.md`) |
| Host-named files need a collision check | CONFIRMED | `machine.py:132-140`; `sessions.py:454-461`, `:598-607` |
| The session publisher can publish answer files | REFUTED as built | it commits and pushes only this machine's session file, checked by path (`session_publish.py:136-139`, `:394-417`); it holds a per-repository lock for a whole publish (`:307-310`, `:454-456`) and waiters give up after 20 s (`:51`) |
| A lock probe identifies the holder | REFUTED | `oslock.try_lock` returns only held or free (`oslock.py:30-44`); identity needs a record, as the dashboard's `server.json` holds PID, creation time and a nonce (`docs/planning/task-kit-dashboard/architecture.md`, "Server discovery, port, and code identity"); `procstamp.py` gives the exact process stamp |
| The existing SessionStart script can start the service | REFUTED | `hooks/sessionstart/session-start.sh:56-83` exits before Python unless a session registration is found, and runs only for resume and clear |
| A task format version exists | CONFIRMED | `schemas.py:23`; `validate.py:471-475`; `types.py:45`; findings gate `work` |
| What the mockups need | READ | `detail-view/index.html` "Data the concepts use": a generated summary "with as-of commit, time, model and freshness" in all four concepts; concept C shows "as of d6bb301a + task record 7cd92a28", the model, and "Waiting on you" decisions with defaults |
| A commit made inside post-commit is safe | MEASURED NARROWLY; not load-bearing (no hook) | git 2.54.0, macOS, one scratch repo; concurrency, Windows, recovery and publisher interaction not measured |
| The dashboard shows summary staleness | REFUTED | `dashboard/inventory.py:250`, `dashboard/snapshot.py:144,422`, `dashboard/static/app.js:850` |
| Windows runs the detached service | UNTESTED | the dashboard detaches through `procspawn` (`dashboard/launcher.py:184-191`); U9 measures it |
| Latency per answerer | UNTESTED | U3 measures it |
| An llm-kit `OutputContract` keeps the transports selectable | UNTESTED | U3 checks, else task-kit validates the answer itself |

## D1. The primitive: questions and answers

**Question file.** Shipped: `plugins/task-kit/skills/task/questions/<id>.yaml`, listed in
`questions/catalog.yaml` (`version: 1`, active ids). Fleet layer:
`~/.claude/config/task-kit/questions/<id>.yaml`; a file with a shipped id replaces it;
`retired: true` retires it. Unknown keys, an unknown reader or script, or a bad schema are errors
naming the file.

```yaml
_schema_version: "1"
id: orientation
version: 1
title: Orientation
applies_to: [active, blocked, deferred, closed]
inputs: [task, claude_md, plan_md, items, activity]
max_input_chars: 40000      # must be >= the sum of the readers' limits
script: null                # script answerer tried first; null for none
prompt: |                   # null for a script-only question
  From the task files below, answer for someone opening this task: what it is,
  where it stands, and the single next action. Return one JSON object.
answer:                     # JSON schema subset
  type: object
  required: [headline, state, next]
  additionalProperties: false
  properties:
    headline: {type: string, maxLength: 100}
    state: {type: string, maxLength: 200}
    next: {type: string, maxLength: 160}
grounding: []               # fields that must quote the inputs verbatim
models: null                # null: the configured chain; else a declaration overriding it
effort: null                # null: the configured effort
```

**Input readers** are code. Each returns UTF-8 text with LF newlines and trailing whitespace
stripped per line, cut at its fixed limit with a final `[truncated]` line.

| Reader | Reads | Limit | Excludes |
|---|---|---|---|
| `task` | the task.yaml task block, canonical YAML | 4000 | `_schema_version`; legacy `summary`, `summary_fingerprint`, `summary_updated` |
| `claude_md` | CLAUDE.md | 12000 | -- |
| `plan_md` | plan.md | 12000 | -- |
| `items` | parsed `task_items` (id, title, state, priority, note) | 6000 | -- |
| `activity` | dated log.md entries, newest first | 6000 | summary-maintenance entries and `refresh (no field edits)` |
| `task_commits` (v1, script use) | `git log` of the task folder | -- | -- |
| `code_commits` (v2) | the project's log after the previous as-of code commit; needs a task-root to checkout map | -- | -- |
| `answer:<qid>` (v2) | another question's chosen answer | -- | -- |

**Fingerprint:** sha256 of `json.dumps({"question": <digest>, "inputs": [[name, text], ...]},
sort_keys=True, ensure_ascii=True, separators=(",", ":"))`, inputs in declared order. The
question digest is sha256 of the same canonical JSON of the definition (id, version, inputs,
limits, script, prompt, answer schema, grounding). Models, effort and switches are excluded.

**Model answer record**, `<task>/answers/<question id>/<host id>.yaml`:

```yaml
_schema_version: "1"
host: studio                    # equals the file name
machine: 3f9a0c1d2e4b5a6c       # machine.fingerprint, 16 hex
os_instance: macos
question: orientation
question_version: 1
answer:
  headline: Generate each task's orientation for the dashboard detail view
  state: Design plan in its third round; no implementation unit has started
  next: Start the U1 interface freeze
inputs_fingerprint: <64 hex>
as_of:
  task_commit: <sha>            # tasks-repo HEAD whose content was read
  code_commit: null             # set only by questions reading code inputs
answerer:
  kind: model                   # model | script
  entry: qwen38-5090
  tier: free                    # free | paid
  effort: medium
  attempts:                     # every entry tried, in order
    - {entry: qwen38-5090, outcome: completed}
generated_at: "2026-10-21T19:05:12-05:00"   # informational; never used to choose
```

**Script answers** are computed on read and never stored: they are reproducible from git and
the clock, so a file would add commits and could only go stale. Provenance:
`answerer: {kind: script, name: <script>}`, `as_of.task_commit` = HEAD at read time.

**Choosing among model records** (any host). Every machine applies the same rule, using nothing
machine-local:

| State | Rule |
|---|---|
| `current` | records whose fingerprint equals the current one; winner: lowest host id, then lowest machine fingerprint. Any current record answers the same question over the same inputs and passed the same validation |
| `stale` | no current record; candidates: the ancestry-maximal set (records whose `as_of.task_commit` is not a proper ancestor of another record's, by `git merge-base --is-ancestor`); winner: lowest host id, then fingerprint. A record's as-of commit precedes the commit that added the record, so it is present whenever the record is; an unreachable one (rewritten history) ranks below every reachable one |
| `missing` | no record |
| `invalid` | a file that does not parse, fails the schema, or whose `host` differs from its file name: a note naming the file, never a finding that gates `work`; valid records still count |
| `not-applicable` | status outside `applies_to` |

**Answer validation** (task-kit side): one JSON object matching the schema; values single-line
and non-empty; typographic punctuation converted to ASCII (dashes to `--`, curly quotes,
ellipsis to `...`, arrows to `->`), other non-ASCII rejected; each `grounding` field must
contain a whitespace-normalised verbatim substring of the inputs. A rejected answer is a task
error for the chain (D2).

## D2. Answerers, tiers and spend

Order for one question on one task: (1) its `script`, when the script returns an answer;
(2) the free phase; (3) the paid phase, only when spend is on. Granularity is the question: a
need that mixes a script fact and a model judgment is two questions, the second reading the
first (`answer:<qid>`, v2).

**Script answerers** are named functions in `task_system/answer_scripts.py`:
`answer(ctx: ScriptContext) -> Mapping | None` (None: cannot answer; the next tier runs).
`ScriptContext` holds the task folder, the repository top, a per-read git log of the repository
(one `git log --topo-order --name-only` per read, grouped by folder, so a 75-task snapshot
costs one git process), and the read time. v1 scripts: `activity` (days since the last dated
activity, last entry), `task_commits` (the folder's latest commits: sha, date, subject).

**Free or paid** comes from llm-kit's registry: an entry is free when its `billing_mode` is
`unmetered`; every other entry (harness entries, metered transports, invalid metadata) is paid.
This is the fleet convention ("a tier is paid unless the registry declares `billing.mode:
unmetered`", local-llm-domain `SKILL.md`, "Acceptance runs").

**Spend is the consent** (user, round 3: "Yes, all repos"). With `answers.spend: true` on this
machine, paid answerers may answer for every repository the service covers. The checkbox label
and the CLI output of `task-kit service spend on` state, in these words or plainer: "Paid
answerers will run. The declared inputs of each question (task.yaml, CLAUDE.md, plan.md, items
and log entries of every task in every repository on this machine) are sent unredacted to
<providers> and may contain sensitive material." `<providers>` is the registry `family` of each
configured paid entry (OpenAI for `luna`, Anthropic for `haiku`), or the entry id when no family
is declared. No redaction is built: nothing in the repository detects secrets, and a pattern
pass would read as protection it does not give. Free entries are owner-hosted LAN endpoints.

**Dispatch, in two phases.** Phase 1 (always): `declaration.run(models, request,
requirements={"guarantees": ["filesystem-write", "shell-exec", "subagent-spawn"]},
exclude=E | PAID, max_attempts=<remaining free entries>, reachability_cache=<per pass>)`, where
`request = RunRequest(system=<prompt>, prompt=<inputs>, options=BackendOptions(effort=<effort>,
timeout_s=min(remaining, 180), client_id="task-kit:answers"))`. Phase 2 (spend on, phase 1
produced no accepted answer): the same with `exclude=E | FREE`. Within a phase llm-kit chooses
the entry (declared order, pace among paced entries, reachability, quota) and moves on by itself
after a halt or launch failure. On `RUN_FAILED` or a rejected answer, task-kit adds every entry
in `result.attempts` to `E` permanently, records each attempt, and calls `run` again while the
phase has entries left. The phases make free-before-paid hold whatever order the declaration
lists. Precedent for re-selection: code-review lanes re-select through describe with `--exclude`
and disclose every route (`plugins/CLAUDE.md` register, "An unexplained lane failure re-selects
through describe, and every re-selection is disclosed"). One `effort` reaches every entry (all
four admit `params: ["effort"]`, measured).

**llm-kit is optional.** Without it (absent or too old), model answering refuses with the
install or update command (distinct messages, probing the newest call shape used), and the
dashboard discloses "model answers unavailable: <reason>". Script answers and every other verb
work without it.

## D3. Storage and ownership

- Per question, per host, in the task folder, committed. Principle 2: two machines answering
  one question write different files and merge cleanly.
- Ownership, as working sessions do it: a machine writes only `<host id>.yaml` files carrying
  its own `machine` fingerprint; it refuses, naming the file, to write a same-named file
  carrying another fingerprint (two machines share a host id: set `sessions.host` on one); it
  reads such a file as another machine's.
- Deletion (`answers prune`, own files of retired questions): ownership is checked on the
  committed preimage, the content the file had before the deleting commit. The publisher applies
  the same rule (U5), so a deletion of another machine's file is never published.
- Why not task.yaml keys (where `summary` lives at `d6bb301a`): `state_ops.update`
  re-serialises task.yaml and appends a log line (`state_ops.py:206-208`), rewriting two
  agent-edited files per refresh, and one shared file is the conflict principle 2 rules out.
- No loop: no reader reads `answers/` (only `answer:<qid>`, by declaration), and writing an
  answer appends no log line.
- Answers describe committed content: a task folder with uncommitted changes outside
  `answers/` is skipped and named in status.
- Other machines' files are never edited or deleted.

## D4. The task-kit service

**What it is.** `task-kit service`, one background process per machine, the first duty of the
task-kit service the user expects on every machine (session verification, in
`working-sessions-plan.md` "Future direction", is a later duty). It is not the dashboard
process: the dashboard is a view (principle 9, "I don't like any dependency on the dashboard
running"), answering must run where no dashboard is open, and closing the dashboard must not
stop it. Nothing depends on it running: off, answers go stale visibly and `task.py answers
refresh` does a pass by hand.

**Switches, per machine** (user, round 3). `answers.service` and `answers.spend` are read only
from `~/.claude/task.local.yaml`; the fleet layer setting either is an error naming the file;
both default to false. The dashboard shows each as a checkbox (spend with the D2 statement);
CLI: `task-kit service on|off`, `task-kit service spend on|off`. A toggle writes only that key;
the writer refuses, naming the file, when a round trip would drop comments.

**Starting** (user, round 3: the dashboard and Claude Code sessions; no OS autostart).
`task-kit service ensure` is called by the dashboard launcher and by an added async SessionStart
script `hooks/sessionstart/service-ensure.sh` for the `startup`, `resume` and `clear` sources.
It is a separate hooks.json entry, so the sessions script's fast path
(`session-start.sh:56-83`) stays as it is. `ensure`: switch off, exit 0; singleton lock
(`<data dir>/answers/service.lock`) free, start the service detached through `procspawn`,
stdout and stderr appended to `<data dir>/answers/service.log` (the error channel
`plugins/CLAUDE.md` requires of a detached process); lock held and `service.json` identifies a
live process running the current code, exit 0; identified but older code, stop it, then start;
lock held but the holder cannot be identified, log it and do nothing (never signal an
unidentified process).

**Identity and control.** After taking the lock the service writes `<data dir>/answers/
service.json` atomically: pid, exact process stamp (`procstamp.py`), instance nonce, code
version (task-kit version and code hash, as the dashboard's code identity), started time, and
`phase` (`idle`, `prepare`, `answering`, `commit`), updated at each transition. `task-kit
service stop` (and `off`, which first writes `answers.service: false`): read `service.json`;
verify pid and stamp; write `<data dir>/answers/service.stop` carrying the nonce; wait up to 30 s
for the lock to free. The service checks for the request every second while idle and at each
safe point, and exits only between phases. If the lock is still held and the identity verifies,
`stop` escalates to a process-tree terminate (psutil, as the dashboard's shutdown does) only
while `phase` is `idle` or `answering`; during `prepare` or `commit` it keeps waiting, so a git
operation is never killed mid-write. A stale `service.json` (stamp mismatch) is reported, never
acted on. The service also re-reads its switch every pass and exits at the next safe point when
it is off.

**One pass, per task repository in scope, every `poll_s` seconds** (repositories holding the
task roots discovery finds). The repository lock is the session publisher's
(`session_publish.lock_path`), and is never held across a model call:
1. Skip the repository (status note) when HEAD is detached or a git operation is in progress:
   `MERGE_HEAD`, `CHERRY_PICK_HEAD`, `REVERT_HEAD`, `REBASE_HEAD`, `rebase-merge/`,
   `rebase-apply/`, `sequencer/` (via `git rev-parse --git-path`).
2. **Prepare (locked, `phase: prepare`).** Take the lock (bounded wait; busy: skip the pass).
   Commit this machine's dirty or untracked own answer files (ownership checked, `git commit
   --only -- <paths>`) before any freshness check, so an answer written but never committed is
   not skipped as current. Compute pending locally: eligible, clean task folders; applicable
   model-tier questions whose script does not answer and whose chosen record is missing or stale.
   If pending work exists or own commits are unpublished, publish (fetch, integrate, push own
   files) and recompute pending. Snapshot each pending item's folder and fingerprint. Order the
   pending tasks most recently modified first (below) and keep the first `batch_size` items.
   Release the lock.
3. **Answer (unlocked, `phase: answering`).** For each kept item, up to 4 in parallel: read
   inputs, recompute the fingerprint (changed since the snapshot: drop as superseded), dispatch
   through D2, validate.
4. **Commit (locked, `phase: commit`).** Take the lock (bounded wait; busy: keep the results in
   memory and retry this step at the next tick, up to the next pass, then drop them as
   unwritten). Revalidate each result (folder still clean, fingerprint unchanged; otherwise drop
   it as superseded), write own files atomically, commit `--only` own paths, publish. Release.

Publishing holds the lock for the publisher's window, as session publication already does
(`session_publish.py:454-456`); a session publish that waits past its 20 s is reported busy and
publishes at the next publish, which is its existing contract. An idle pass reads files and
computes fingerprints only and touches the network only when there is work or something to
publish. Agents' git commands do not take the task-kit lock; a collision surfaces as git's own
`index.lock` error in one of them, and the next pass's prepare step repairs the service side.

**Most recently modified first** (user, round 3). A task's modification rank is the position,
in one `git log --topo-order --name-only` of the repository (the pass's shared log), of the
newest commit touching any path in its folder outside `answers/`; a smaller position is newer.
Ties, and folders with no such commit, fall back to the folder path. Chosen over log.md dates
because commits cover every input file and answers describe committed content, the order comes
from the commit graph rather than any machine's clock, and log.md stamps carry no timezone
(`discovery.py:220-222`). With a full batch the oldest tasks wait for a later pass.
`task.py answers refresh` uses the same order.

**Publication.** U5 widens the publisher's own-files rule from "this machine's session file" to
"this machine's session file and its answer files". An answer path qualifies when its content
carries this machine's fingerprint; for a deletion, when its preimage did (the blob in the
deleting commit's first parent, or at the upstream tracking ref for the net diff). Every other
refusal stays (foreign local commits, foreign diff).

**Status.** `task-kit service status` prints the switches, whether the service runs (identity
from `service.json` verified against the live process), its phase, pending and unpublished
counts per repository (computed from git), and the log path. The dashboard shows the same.

**Rejected triggers.**

| Option | Verdict | Why |
|---|---|---|
| post-commit hook (synchronous or detached) | Rejected | user ruling: "out of sync means out of sync" |
| pre-commit hook | Rejected | network before the record is committed (principle 7) |
| inside the dashboard server | Rejected | principle 9 and the ruling on dashboard dependency |
| bootstrap pass | Rejected | bootstrap provisions dependencies; a model workload would lengthen every session start |
| OS login autostart | Not in v1 | user, round 3: started by the dashboard and Claude Code sessions |
| `task.py answers refresh` | Kept | the by-hand path, same engine |

## D5. Schema versions

- **v1 versions the question catalog.** `questions/catalog.yaml` `version: 1` names the active
  questions; answering is the per-version upgrade: idempotent (a current answer is never
  recomputed), safe on any machine. A task's coverage ("catalog 1: 3 of 4 current") is computed,
  never stored; a task whose answers failed stays visibly on its old coverage.
- **Task-format migrations are deferred** until a structural change exists. When one does: a
  deterministic, idempotent migrator `N-1 -> N` that writes byte-identical files on any
  machine; validate learns version N one release before any migrator writes it (findings gate
  `work`); and it needs its own publication contract, because the publisher's own-files rule
  cannot push shared `task.yaml` files.
- **Un-upgraded folders keep every verb's contract.** No verb requires an answer; `work` and
  `validate` never gate on answers; `list --format json` serves `summary` from the orientation
  answer, else from legacy `task.summary` (labelled `summary_source: legacy`); `update
  --summary` keeps writing `task.summary` and notes when an orientation answer supersedes it.

## D6. Question catalog (ranked by value per cost)

| # | Question | Answerer | Inputs | Cost | Stale when | Free-tier safe | Mockup surface | Ships |
|---|---|---|---|---|---|---|---|---|
| 1 | orientation: headline, state, next | model | task, claude_md, plan_md, items, activity | one call, about 3k-10k tokens in | its inputs change | yes (fits qwen38-m5's 32k) | summary block in A, B, C, D | v1 |
| 2 | task-record commits | script | task_commits | the pass's shared git log | never stored | n/a | "Latest task-record commits" in A, B, C, D | v1 |
| 3 | activity age and last entry | script | activity, clock | none | never stored | n/a | C "Picking it back up"; list row | v1 |
| 4 | decisions waiting on the user: item, question, default (only when stated), evidence quote; at most 5 | model | items, plan_md, claude_md, activity; active and blocked only | one call, short output | those inputs change | yes | C "Waiting on you", "Accept the defaults"; header markers | v1 |
| 5 | item state history | script | task_items across task commits | git show per commit touching plan.md | never stored | n/a | D timeline | v2 |
| 6 | current item of a working session | script (item id in the newest commit), then model | items, task_commits | small | a task commit | yes | C working card | v2 |
| 7 | code commits belonging to the task | model | task, items, code_commits | one call, about 2k-5k tokens | project commits after the stored as-of code commit | yes | "Latest code commits" in A, B, C, D | v2 |
| 8 | blocker kind and one-line risk | model | task, items, activity | small | as orientation | yes | header markers; list tint | v2 |
| 9 | item-to-event links | script, then model | items, task_commits, activity | medium | task commits | yes | D timeline filter | v3 |
| 10 | related tasks | model | answer:orientation of every task | one call, about 75 headlines | any headline changes (fans out) | yes | B fact rail | v3 |
| 11 | remaining size | model | items, plan_md | small | plan.md changes | yes | B fact rail | v3 |

Not questions: the hand-off note (a convention `/task hand-off` writes; C).

## D7. Failure reporting

| Situation | Answers | Reported |
|---|---|---|
| free phase fails, spend on, paid phase succeeds | written; `attempts` lists every entry | service log and status |
| spend off, free entries down | unchanged: stale or missing | status: "N waiting: no free answerer usable" |
| llm-kit absent or too old | model answers unchanged; script answers served | status and dashboard name the remedy |
| uncommitted task changes | that task skipped | status names the folder |
| git operation in progress or detached HEAD | repository skipped | status note |
| lock busy at prepare | pass skipped | log line |
| lock busy at commit | results held, retried at the next tick, then dropped | log line |
| task changed during answering | result dropped as superseded | log line |
| commit fails (`index.lock`) | written, uncommitted; the next prepare step commits it | log line |
| publish refused or offline | committed, unpublished | status shows the count and the publisher's message |
| host id collision | not written | refusal naming the file and the `sessions.host` remedy |
| stop finds an unidentifiable lock holder | nothing signalled | `stop` reports it |
| service off | stale over time, visibly | status "service off" |

The only fallback is the declared chain within the tiers the switches allow; every attempt is
recorded.

## Contract changes

- `task.py review` (default `--generate-missing-summaries`) answers the orientation question
  through the engine (tiers and switches apply) and commits only this machine's answer files; it
  no longer writes task.yaml summary keys or a log line.
- `list --format json`: adds `answers`; `summary` from the orientation answer, else legacy;
  `summary_status` gains `invalid` and `not-applicable`; `schema_version` `"2"`.
- `update --summary`: unchanged write; a note when superseded.
- Added: `task.py answers refresh|status|prune`; `task-kit service
  on|off|spend|status|stop|ensure|run`.
- `session_publish` publishes this machine's answer files too (ownership by content or
  preimage).
- task-kit hooks.json gains an async SessionStart entry (`startup`, `resume`, `clear`) running
  `hooks/sessionstart/service-ensure.sh`.
- `plugins/CLAUDE.md` "Who talks to an LLM" row for task-kit: `llm_kit.dispatch.declaration.run`
  and the registry's `billing_mode` and `family`.

## Submit gates

- **Razor.** Configurable with defaults: `answers.service` (false, per machine),
  `answers.spend` (false, per machine), `answers.models` (`[luna, haiku]`), `answers.effort`
  (medium), `answers.poll_s` (60), `answers.batch_size` (4), the question catalog (shipped plus
  fleet files). Hardcoded, test fails: answer record layout and schema, reader and script
  vocabulary (code), the free/paid rule (llm-kit's registry convention), most-recently-modified
  order (a user ruling). The git-privilege register entry is extended to name answer commits
  (OP-7). `[luna, haiku]` lists a Codex and a Claude entry
  (`tests/repo-scripts/test_shipped_model_families.py`); `qwen38-*` ids are fleet-private and
  never shipped (OP-1).
- **Write paths.** Answer files: project-durable (committed task data). Service lock, log,
  `service.json`, `service.stop`: user-scoped (`<data dir>/answers/`). Switch toggles:
  user-scoped (`~/.claude/task.local.yaml`). None derived from the script's own location.
- **Agent directives.** SKILL.md text (U9) states the tiers, the switches and the spend
  statement, not "Codex Luna at medium effort".
- **Optional dependency.** llm-kit: REFUSE for model answering, DEGRADE with disclosure in the
  dashboard.

## Implementation units

**U1 starts with an interface freeze** (`task_system/answers_types.py` plus golden fixtures
under `tests/task-kit/fixtures/answers/`: one task folder, its expected bundle text and
fingerprint). U2, U3 and U5 start once it lands.

```python
@dataclass(frozen=True)
class Question:
    id: str; version: int; title: str
    applies_to: tuple[str, ...]; inputs: tuple[str, ...]; max_input_chars: int
    script: str | None; prompt: str | None
    answer_schema: Mapping[str, Any]          # read-only mapping
    grounding: tuple[str, ...]
    models: tuple[str, ...] | None; effort: str | None
    source: Path; digest: str                 # sha256 hex

@dataclass(frozen=True)
class InputBundle:
    task_id: str
    texts: tuple[tuple[str, str], ...]        # (reader, canonical text), declared order
    truncated: tuple[str, ...]
    def prompt_text(self) -> str: ...         # "=== <reader> ===\n<text>\n", in order

@dataclass(frozen=True)
class Answerer:
    kind: str                                 # "model" | "script"
    entry: str | None; name: str | None; tier: str | None; effort: str | None
    attempts: tuple[tuple[str, str], ...]     # (entry, outcome)

RunFn = Callable[..., Any]   # declaration.run(names, request, *, project_root, requirements,
                             #   exclude, max_attempts, reachability_cache) -> RunResult
# deadline: absolute time.monotonic() seconds, or None
```

Module interfaces: `questions.load_catalog(shipped_dir, fleet_dir=None) -> Catalog`;
`question_inputs.read_inputs(folder, block, question) -> InputBundle`,
`inputs_fingerprint(question, bundle) -> str`; `answer_scripts.answer(name, ctx)`;
`answer_store.write_answer(folder, qid, machine, record)`, `chosen_answer(folder, question,
current_fp, repo_top) -> AnswerState`, `coverage(...)`, `modification_order(repo_top, folders,
log) -> list[Path]`; `answers_config.load(...) -> AnswersConfig`; `answer_chain.ask(question,
bundle, config, *, deadline=None, reachability_cache=None, run=None) -> Asked(answer,
answerer)`; `answer_engine.run_pass(repo_top, machine, config, *, budget, should_stop) ->
PassReport`; `session_publish.repo_lock(top, data_dir, wait_s)`, `publish_holding_lock(top,
machine)`.

| Unit | Scope | Owns | Depends on |
|---|---|---|---|
| U1 catalog, inputs, scripts | interface freeze and fixtures first; question loading, layers, retire; readers with limits and exclusions; digest and fingerprint; `activity` and `task_commits` scripts; the four v1 question files | `task_system/answers_types.py`; `task_system/questions.py`; `task_system/question_inputs.py`; `task_system/answer_scripts.py`; `plugins/task-kit/skills/task/questions/`; `tests/task-kit/fixtures/answers/`; `tests/task-kit/test_task_questions.py`; `tests/task-kit/test_task_question_inputs.py`; `tests/task-kit/test_task_answer_scripts.py` | none |
| U2 store and readers | record format, ownership refusal (write and delete), choice rules, states, coverage, modification order; `listing.py` answers, script answers on read, legacy read compatibility, projection `"2"` | `task_system/answer_store.py`; `task_system/listing.py`; `tests/task-kit/test_task_answer_store.py`; listing tests asserting summary fields | U1 freeze |
| U3 config and chain | config layers, per-machine switches and their writer, spend statement text; free/paid and provider names from the registry; two-phase dispatch; persistent re-selection; answer validation incl. grounding; llm-kit probe; latency of each entry on 10 real tasks; OutputContract check | `task_system/answers_config.py`; `task_system/answer_chain.py`; `tests/task-kit/test_task_answers_config.py`; `tests/task-kit/test_task_answer_chain.py`; `tests/repo-scripts/test_shipped_model_families.py` | U1 freeze |
| U4 engine and CLI | one pass (D4 steps 1-4) with phases and revalidation, ordering and batch; callable by the service and `answers refresh`; `answers status|prune`; CLI module; launcher routing | `task_system/answer_engine.py`; `task_system/answers_cli.py`; `plugins/task-kit/bin/task_kit_entrypoint.py`; `tests/task-kit/test_task_answer_engine.py` | U2, U3, U5 |
| U5 publisher | `repo_lock`, `publish_holding_lock`; own answer files by content, deletions by preimage | `task_system/session_publish.py`; `tests/task-kit/test_task_sessions_publish.py` | U1 freeze |
| U6 service | `task-kit service` lifecycle: singleton lock, `service.json` identity and phase, stop request and identity-checked escalation, ensure (incl. code-version restart), detached run with log, poll loop over U4; the `service-ensure.sh` SessionStart entry | `task_system/answer_service.py`; `plugins/task-kit/hooks/hooks.json`; `plugins/task-kit/hooks/sessionstart/service-ensure.sh`; `tests/task-kit/test_task_answer_service.py`; `tests/task-kit/test_task_service_ensure_hook.py`; the allowlist entry in `tests/repo-scripts/test_python_invocation_standard.py` | U4 |
| U7 task.py, review, validate | `answers` verb dispatch; `review` through U4; `update --summary` note; review HTML; answer notes non-gating in validate; `summary_ops.py` reduced to the review adapter | `task.py`; `task_system/summary_ops.py`; `task_system/review_html.py`; `task_system/validate.py`; `tests/task-kit/test_task_answers_review.py` and review/update/validate tests asserting summary text | U2, U4 |
| U8 dashboard | answers with answerer, as-of commit, stale marker; decisions with defaults; service and spend checkboxes (spend statement), running state and phase, pending counts; launcher calls `service ensure`; layout from the picked mockup | `task_system/dashboard/{inventory,snapshot,server,launcher}.py`; `task_system/dashboard/static/{app.js,app.css}`; `plugins/task-kit/skills/task/references/dashboard.md`; `tests/task-kit/test_dashboard_answers.py`; `tests/task-kit/test_dashboard_ui_contract.py` | U2, U6; mockup pick |
| U9 docs, Windows gate, release | SKILL.md; README; design doc; `plugins/CLAUDE.md` (LLM table row, register entry); version bump; Windows smoke over ssh (service detaches, survives the starting session, stops through the protocol, passes run); claudx smoke; git-code-review; md-domain lanes; publish on the user's go-signal | `plugins/task-kit/skills/task/SKILL.md`; `plugins/task-kit/{README.md,.claude-plugin/plugin.json,pyproject.toml}`; `plugins/task-kit/skills/task/design/task-system-design.md`; `plugins/CLAUDE.md` | U1 to U8 |
| U10 fleet wiring (claude-settings) | `config/task-kit.yaml`: `answers.models: [qwen38-5090, qwen38-m5, luna, haiku]`, `effort: medium` (switches stay per machine) | claude-settings `config/task-kit.yaml` | U9 published |

Order: U1 freeze; then U1 rest, U2, U3, U5 in parallel; U4; U6 and U7 in parallel; U8 once U2
and U6 land and a mockup is picked; U9; U10.

## Checks (each shown to fail by the stated revert)

- **U1** A fleet file replaces a shipped id; `retired: true` drops it; an unknown reader or
  script is an error naming the file (revert: skip silently -> red). A `refresh (no field
  edits)` line leaves the orientation fingerprint unchanged (revert the exclusion -> changed,
  red). The golden fixture's bundle text and fingerprint match byte for byte. `activity` changes
  with the clock and is never written to disk.
- **U2** Writing an answer changes no fingerprint (revert: hash `answers/` -> stale at once,
  red). A file named for this host carrying another fingerprint is refused and left
  byte-identical (revert the check -> overwritten, red). Two current records yield the same
  winner under two different effective model declarations and swapped `generated_at` values
  (revert to a configuration- or time-based rule -> flips, red). Among stale records, the one
  with the descendant as-of commit wins. `answers prune` refuses to delete a file whose preimage
  carries another fingerprint. Legacy `task.summary` is served, labelled.
- **U3** A fake halt on entry A then `RUN_FAILED` on entry B never returns to A (revert:
  exclude only `result.entry` -> A retried, red). With a declaration listing `luna` before
  `qwen38-5090` and spend on, `qwen38-5090` is dispatched first (revert the phases -> `luna`
  first, red). With spend off, no paid entry is dispatched. `effort: medium` reaches luna.
  Ungrounded default rejected. The spend statement names the providers from the registry.
  Families guard red without `haiku`.
- **U4** (scratch repos) Three tasks committed in order A, B, C, all pending, `batch_size: 2`:
  the pass answers C and B, not A (revert the order -> A and B, red). While a fake model call
  blocks, `session_publish.publish` takes the repository lock within its 20 s (revert: hold the
  lock across answering -> BUSY, red). A task edited and committed during answering is dropped
  as superseded at commit. A pass after a failed commit commits the dirty own file before
  freshness checks (revert the step order -> stays uncommitted, red). A separately staged path
  stays staged. `REBASE_HEAD` or `sequencer/` skips the repository. A task holding another
  host's current answer is not recomputed.
- **U5** Publishes a commit holding only this host's answer files; refuses one holding another
  host's file, a file named for this host with another fingerprint, or a deletion whose preimage
  carries another fingerprint (revert the preimage check -> pushed, red).
- **U6** A second `ensure` with the service running exits without starting another; `stop`
  with a stale `service.json` (stamp mismatch) signals nothing (revert the stamp check -> a
  reused pid is terminated, red); `stop` during `phase: commit` waits instead of terminating;
  `off` stops the process; the detached process's stderr reaches the log (revert to `/dev/null`
  -> the forced failure is invisible, red); a `resume` hook input with no session registrations
  still runs `ensure` (revert to the sessions fast path -> not started, red); `ensure` restarts a
  service whose code version is older.
- **U7** `review` leaves task.yaml and log.md byte-identical (revert to `state_ops.update` ->
  changed, red); an invalid answer file never gates `work`.
- **U8** A stale answer shows its marker and as-of commit; the spend checkbox shows the
  statement; toggling a checkbox changes the effective switch.
- **U9** Windows: the service runs detached after the starting session ends and stops through
  the protocol, or ship with the gap stated.

## Questions for the user

None open. Switch scope, consent and starting were answered in round 3; the first question set
is this plan's call.

## Blockers and outside dependencies

- None for U1 to U9.
- U10 changes the claude-settings repository, outside plugins-kit.
- Publishing task-kit needs the user's go-signal (root `CLAUDE.md`).
- The Windows check needs ssh access to a Windows fleet machine.
