# task-kit

File-backed task tracking for Claude Code: a task owns a folder, documents
reference it, and every verb names its task explicitly.

## What it does

The `task` skill drives one CLI over task folders (`tmp/<stub>` for
ephemeral work, `dev/tasks/<stub>` for version-controlled work): create,
list, show, validate, work, review, summarize, close, reopen, archive,
delete, move, and answers (which rewrites every `task_list` reference to the moved
folder). The skill routes a natural-language request ("start a task for X",
"what tasks are open here?", "archive the closet task") to the right verb and
states what the agent, rather than the script, must do with the output.

The skill's entry point, verb contracts, and design documents are in
[skills/task/SKILL.md](skills/task/SKILL.md) and
[skills/task/design/task-system-design.md](skills/task/design/task-system-design.md).

## The task view

task-kit has no dashboard of its own. [Databench](https://github.com/kitaekatt/databench) provides the task view: its
tasks module lists every configured project's tasks, each task's items and its working-session registrations, from the
facts `task-kit export` prints as JSON. The export contract is
[skills/task/references/task-export-protocol.md](skills/task/references/task-export-protocol.md). task-kit works the
same whether or not databench is installed.

An async SessionStart hook runs `databench ensure` for `startup`, `resume` and `clear` when databench is on PATH. The hook
never blocks the session or opens a browser; Databench's `regeneration.enabled` setting decides whether ensure starts the server.

```
task-kit               run `databench --view tasks` (exit 1, with a message, when databench is not on PATH)
task-kit export ...    print the task facts as JSON (the interface databench reads)
task-kit manage ...    serve full task input and revision-checked semantic mutations
task-kit stop          stop a dashboard server an earlier task-kit version started (--force: even with jobs running)
```

`/task dashboard` in Claude Code runs bare `task-kit`. `task-kit stop` is a transition aid and goes in a later minor.
The additive manager interface is documented in [task-manage-protocol.md](skills/task/references/task-manage-protocol.md).
It reads the earlier dashboard's `dashboard/server.json` in task-kit's data directory and signals the process only
when its PID and creation time match; exit codes: 0 stopped or nothing to stop, 1 error, 2 usage error, 3 task-kit's venv is not provisioned, 5 jobs are
running, 6 refused inside a dashboard run. The earlier dashboard's runtime data stays where it is; task-kit deletes none of it.

bootstrap puts the launcher on your PATH (`~/.claude/plugins/data/plugins-kit/task-kit/bin`;
on Windows the User PATH), so a new terminal finds `task-kit` after the first
bootstrap pass that provisions this version.

Queued work is the `queue` capability of the skill: it hands a clarified unit to a background worker per
awesome-kit:orchestrate.

## Task answers

A task carries answers to a catalog of questions (orientation, decisions, activity, task commits). The questions are
declared data under `skills/task/questions/`; a fleet layer in `~/.claude/config/task-kit.yaml` can add to them. An answer
is current while the fingerprint it stored equals the fingerprint of the question and its inputs, and stale after the task
changes. Model answers are files under the task's `answers/` folder, one per machine, committed and pushed with the task
folder's repository; script answers (`activity`, `task_commits`) are computed from git history when read and never stored.
`task-kit export` carries each task's `answers` and `coverage`, and the `list` and `review` verbs show the orientation
answer's headline in place of `task.summary`.

```
task-kit answers status  --format json --protocol N   what is pending; writes nothing
task-kit answers refresh --format json --protocol N   answer pending questions, commit and publish this machine's answer files
task-kit answers prune   --format json --protocol N   delete this machine's files of questions the catalog no longer holds
```

`--project DIR` (absolute, repeatable) on `refresh`, `status` and `prune` scopes the pass to the task folders of exactly those
project checkouts, wherever their `dev/tasks` link resolves; `--parent` keeps its meaning, and given with `--project` the two are
intersected. `task-kit answers protocols --format json` prints `{"protocols": [1], "options": [...]}` so a caller can check that
`project` is among the options before using it.

`/task review` runs the same engine for the orientation question. The models are the config key `answers.models`
(`~/.claude/config/task-kit.yaml`, section `answers:`; shipped default `[luna, haiku]`, `effort: medium`); `local`
entries run first. A `subscription` or `metered` entry runs only when llm-kit's optional consent layer allows it for
scope `task-kit` (`llm-kit consent set [--scope task-kit] --subscription|--metered on|off`; the global policy is a
ceiling and a disabled `local` class stays disabled) or the caller passes `--allow` with the classes (`local`,
`subscription`, `metered`), which replaces the layer. A malformed consent file stops answering with an error naming
the file. Model answering uses the `katsoft-llm-kit` package that task-kit's environment declares, and refuses
when `llm_kit` is not importable (the environment has not been provisioned; run `bootstrap run`); every other verb works
without it. The command contract is in
[skills/task/references/task-export-protocol.md](skills/task/references/task-export-protocol.md).

## Working sessions

Inside a Claude Code session, `/task work <ref>` also registers that session as working the task. The registration is a
record in `<task root>/.sessions/<host>.yaml`, one file per machine, committed and pushed with the task folder's own
repository (a `tmp/` task stays local). Publishing commits the file on the checked-out branch, then fetches the upstream and
fast-forwards or merges it into the checked-out HEAD and working tree, as a pull would (a merge that stops on conflicts is
aborted), and pushes only when the net change from the upstream is session files. It never force-pushes, rebases, stashes or
switches branches. `sessions: {record: false}` turns it off. `/task hand-off` releases it (`task sessions release <ref>`) after committing and pushing the task folder, because publishing pushes only session-file changes. A registration lasts
from work until hand-off: exiting Claude Code, a crash or a resume does not end it, and nothing removes a record because its
process died. An interrupted session is cleaned up by a person with `task sessions release <ref> --all-local`. Registration adds `session:` lines on stderr and changes neither the output nor the exit
code of `work`; a refusal (an unattended dashboard run, `sessions.record: false`, a folder git ignores or cannot publish) is a
`session: not registered: <reason>` line with nothing written.

A record is `running` or `not running` on the machine that wrote it (decided by the exact process id plus start stamp) and
`unverified` on any other machine. `list`, `show` and `status` show the registrations of a task, read-only and offline.

```
task sessions [list] [--scope project|all] [--format text|json]   registrations and this machine's unpublished files
task sessions release <ref> [--all-local]                         remove the calling session's registration (all of this machine's with --all-local), then publish
task sessions publish                                             publish this machine's session files that are committed but not pushed
```

The export carries each task's registrations (`tasks[].sessions[]`), so databench shows them.

`~/.claude/task.local.yaml` takes `sessions: {record: false}` to turn registration off and `sessions: {host: <id>}` to
replace the derived host id. Registration is refused on Windows: the process verification (`CLAUDE_PID` ancestry and the
process start stamp) has not been measured there, and `work` says so on a `session:` line. The contract is in
`skills/task/SKILL.md`.

## Install

```
/plugin marketplace add kitaekatt/plugins-kit
/plugin install task-kit@plugins-kit
```

task-kit declares dependencies on `bootstrap` (venv provisioning),
`skills-kit` (the `skills_kit_lib` schema engine the task CLI imports), and
`awesome-kit` (the `work` verb emits `Skill(awesome-kit:orchestrate)` as its
always-required baseline skill). Installing task-kit installs all three.

## Try this first

Ask for a task: "start a task for tidying the closet", or invoke the skill
as `task-kit:task`.

## When not to use it

Claude Code's native task tracking (TaskCreate / TodoWrite) is a different
system; task-kit is for work that outlives a session.
