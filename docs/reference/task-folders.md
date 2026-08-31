# Task folder linkage

This reference supports the task-folder linkage rule in the root `CLAUDE.md`.
Read it for missing task folders or unexpected task CLI results. It covers a
checkout at `$DEVROOT/plugins-kit`. The root `.gitignore` guard remains
load-bearing. This document repeats the guard so readers do not mistake the
link for an ordinary directory.

## Link model

`$DEVROOT/plugins-kit/dev/tasks` is not a directory in the public repository.
It is a directory junction on Windows or a symlink on other systems. The link
points into the `plugins-kit/` subdirectory of the private tasks repository.
The private tasks repository versions task folders, not this public repository.
This keeps task state durable and keeps it out of the history other people pull.

There is one task set per project, shared by every clone. Two clones of
plugins-kit link to the same directory, so a task folder is visible from
whichever checkout is in use. Task state follows the project, not the
checkout. Two concurrent sessions in two clones can therefore edit the same
task folder, just as two sessions in one checkout can.

A fresh clone has no link. Nothing in the public repository creates it, so
`dev/tasks` is absent until the junction or symlink is made. An absent task
root is a missing link, not a missing task.

## Task CLI contract and durable outputs

The `awesome-kit:task` `dev/tasks` contract, "version control is the record",
applies to the linked task root. It was not in effect while the task root was
ignored. The `durable_outputs` rule still applies to anything that must outlive
the task. A specification belongs in the repository it describes at authoring
time. The task folder is not a reason to make a document hard to find. A
document nobody can find is not durable.

The root `.gitignore` carries `dev/tasks/`, and that entry is load-bearing: it
stops this repository's git from traversing the link and staging private task
content into the public repository. Do not remove the entry or add a
`!dev/tasks` negation.

## Task CLI misreporting bug

The task CLI misreported this linked setup until the fix in awesome-kit
0.35.0. It discovered the tasks repository correctly with
`git -C <folder> rev-parse --show-toplevel`, then passed the logical path
through the link as a pathspec against the resolved root. Git exited 128 with
"is outside repository", and the caught error surfaced as
`version-control state unverified: ... is not in a git repo`.

The result was a note rather than a blocking warning, so `list`, `work`, and
`validate` behaved normally. `archive`, however, silently degraded to the
`vcs_ignored` disposition. That is the one disposition where the folder is the
only copy and `delete` is unrecoverable. A false negative that reached the one
unrecoverable path is why the defect mattered more than the note suggested.

Every git-invoking helper in `validate.py` and `location_ops.py` resolves the
folder before it passes the path to git. A helper that takes a repository root
resolves that root too.

The count was corrected during the fix: the four call sites originally
identified were real but not exhaustive. Eight functions had the pattern, and
the live archive bug was in one of the four that had NOT been named.
