# Work and hand-off

Load `work` for initialization and session registration. Load `hand_off` only
after the complete user-invocation gate in the capability record passes.

## work

Operation signature: `task.py work <ref> [--init] [--root PATH]`.

### Step 1: validate and register

Run the work verb. It validates first: ANY error OR warning blocks, exits
non-zero, writes findings to stderr, and emits no initialization block. A
remote ref cannot be worked locally. A missing folder is a mistyped path: run
list rather than creating it. `--init` explicitly scaffolds and works a ref
the user meant to promote.

After the gate passes inside a Claude Code session, work registers this
session and publishes the record. It commits this machine's file under the
task root's `.sessions/`, fetches upstream, and fast-forwards or merges it into
the checked-out HEAD and working tree. A merge stopped by conflicts is aborted.
It pushes only when the net upstream change is session files. It never
force-pushes, rebases, stashes, or switches branches. The setting
`sessions: {record: false}` in `~/.claude/task.local.yaml` disables recording.

Registration adds `session:` lines on stderr without changing stdout or exit
status. Relay every line in one sentence. A `session: kept:` line names another
session's release command; run it only when the user asks. Registration lasts
until hand-off releases it. Harness exit or crash does not end it; it appears
`not running` locally and `unverified` elsewhere.

### Step 2: invoke and dispatch immediately

On success the script prints ONE initialization block: a
`== task init ... ==` header, one `Skill(skill: "<name>")` line per required
skill, optional `agent_hint: <type>`, and a closing
`== then: start ... ==` directive.

Invoke EVERY emitted Skill line via the Skill tool immediately, in printed
order, before any other tool use. The emitted list is the whole required set;
there is nothing to remember elsewhere. Then obey the closing directive and
START in the same turn. Route planning and implementation through
orchestrate's rendered policy and honor eligible agent_hint types. The script
only emits the block; acting on it is the agent's job.

Do not skip any emitted Skill invocation. The list is the SINGLE source of
the required-skill set: baseline plus task-declared, merged script-side.
Orchestrate is emitted even for trivial work and has the same force as direct
invocation. Delegate every work-product implementation unit, including small
code, tests, scripts, configuration, and documentation. Task CLI verbs,
read-only listings, and existing coordination or verification scripts are not
by themselves work-product implementation.

Work is a START, not a checkpoint. Say the folder's orientation line and begin
the first concrete action in the SAME turn. Do not wait for "go" on an
unblocked task. Only a real blocker or a decision claimed for the user stops
the turn.

The failure pattern is treating the initialization block as informational:
invoking only task-declared skills, skipping orchestrate, and implementing
inline. Invoke every line, then follow the dispatch directive.

## hand_off

The gate remains in the capability record because it must run before this
reference is loaded. Do not pass `hand-off` to task.py: it is not a CLI verb
and is also the task type name. The script operations are update and
sessions release, plus sessions publish when the release reports
`not published`.

### Step 2: rotate and update

Run the update rotation, starting with pre-contract conversion when needed.
Then run update to append the dated log entry and re-validate. Findings block;
fix forward until clean.

### Step 3: self-verify

Read CLAUDE.md and plan.md cold, as the next agent, and fix anything a cold
reader cannot resolve.

### Step 4: publish task state and release

Release is the last state change, after update is clean and self-verification
is complete. In a git task root, first commit AND push the task folder under
the standing task-record authorization. Otherwise release can print
`committed; not published: unpushed commits outside this machine's session
records` because it publishes only when the net upstream change is session
files.

Then run the release operation for `<ref>`. In a reference document, the
agent-shell form is:

```bash
"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" \
  "${TASK_KIT_ROOT:?requires a bootstrap engine pass; run bootstrap run}/skills/task/scripts/task.py" \
  sessions release <ref>
```

If release reports `not published`, push the task root, then run the sessions
publish operation. Relay its `session:` line in one sentence. If it exits
non-zero, relay the error and still end with the baton.

When relaunch-kit:restart invoked hand-off, its restart request runs after
this step and before the baton.

### Step 5: baton

End the turn with exactly two lines:

```text
CWD: <project root>
Continue: /task work <CWD-relative task-folder path>
```

The script emits neither line. They belong only to the user-invoked hand-off
turn. The baton contract is defined by the communication framework at
`../../../../awesome-kit/skills/verbose-updates/references/communication-framework.md`.

Never hand off without being asked. A natural stopping point, long turn, or
update that looks final does not authorize packaging, self-verification, an
offer to hand off, or a baton. Finish normally unless the user invoked it.

## Orchestration boundary

Orchestrate owns planning placement, worker routing, optional delegation
economics, and autonomy edges. Main-thread planning requires both best-suited
model fit under rendered policy and sufficient verified, decision-relevant
context; otherwise delegate it. Keep optional coordination or read-only
investigation in main only when its model is best and total delegation cost
exceeds direct work. None of those choices authorizes main-thread work-product
implementation. Name the model explicitly per orchestrate's tiers.
