# The task view

Load this when the `dashboard` capability of the task skill is routed and the question goes beyond launching it.

task-kit has no dashboard of its own. Databench provides the task view: its tasks module reads the facts task-kit exports and shows every configured project's tasks, each task's items and its working-session registrations. The export is the whole interface between the two; its contract is [task-export-protocol.md](task-export-protocol.md), with the JSON Schema in `task-export.schema.json`. task-kit works the same whether or not databench is installed.

## Commands

- `task-kit` with no command runs `databench --view tasks`. When `databench` is not on PATH it exits 1 and says databench provides the task view and where to get it. `/task dashboard` runs this command.
- `task-kit export ...` writes the task facts as JSON on stdout. It is the interface databench calls, and it writes nothing.
- `task-kit stop [--force]` stops a dashboard server that an earlier task-kit version started. It reads that server's `dashboard/server.json` in task-kit's data directory, signals the process only when its PID and creation time match the record, and refuses while the server has jobs running unless `--force`. Exit codes: 0 stopped or nothing to stop, 1 error, 2 usage error, 3 task-kit's venv is not provisioned, 5 jobs are running, 6 refused inside a dashboard run. `stop` is a transition aid and is removed in a later minor.

Any other command exits 2 and names these three.

## What the retirement leaves alone

The earlier dashboard's runtime data (`dashboard/` in task-kit's data directory: its database, run directories and `server.json`) is not deleted by task-kit. Delete it yourself when you no longer need it. The `dashboard:` settings in `~/.claude/task.local.yaml` are not read; `sessions:` and `project_directories` still are.

Queueing work is the `queue` capability of the task skill: it hands the unit to a background worker per awesome-kit:orchestrate. `/task work` registers the session without any dashboard handshake.
