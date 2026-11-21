# git-kit

A pre-push review gate that defends every finding and remembers what you declined.

## What it does

`/git-code-review` runs a multi-agent review of local git changes before you
push. It auto-detects the range from workspace state (mid-merge, mid-rebase,
`@{upstream}..HEAD`, or an origin-main fallback), partitions the diff into
chunks on disk, and fans out 2-3 parallel reviewer subagents per chunk by
review profile. The mechanisms that make it more than "ask a model about a
diff":

- **Per-issue adversarial validators.** Every candidate finding is re-checked
  by an independent subagent that cannot see who raised it. Unconfirmed
  findings are dropped silently -- what renders is the confirmed set.
- **Declined-findings ledger.** Findings you dismissed are recorded (keyed on
  a normalized anchor, not line numbers), so re-reviewing the same change at
  the same baseline does not re-raise them. Serious findings are never
  suppressed, and the ledger goes stale on its own when the baseline moves.
- **CLAUDE.md-convention-aware review (AGENTS.md where a directory has no CLAUDE.md).** A dedicated reviewer checks the diff
  against ancestor CLAUDE.md files, and may flag a project-rule violation
  only when it can quote the rule verbatim.
- **Path-scoped submit gates.** `Submit gate:` blocks authored in CLAUDE.md
  (or AGENTS.md) files become a confirmation checklist when the range touches their scope.
  Advisory, not enforcement.
- **Cost routing.** A `data_only` profile handles docs/config-only diffs with
  two Sonnet reviewers (CLAUDE.md compliance, surface-level bugs) and Sonnet
  validators. The full `code` profile runs Sonnet for CLAUDE.md compliance
  and Opus for the two deep-reasoning roles (diff-only bugs, introduced-code
  review), with Opus validating bug findings and Sonnet validating
  compliance findings. These assignments are the defaults; ask Claude to use
  a different model to override them.

Output is rendered markdown in chat; no PR comment is posted. The diff
bundle (chunks, pre-images, `bundle.json`) is transient scratch under the
plugin's data root, discarded per review. Findings you decline persist in a
durable ledger keyed on the change, so re-reviewing the same change at the
same baseline does not re-raise them.

## How it differs from the built-in /code-review

The native review is post-hoc and PR-oriented: it looks at work that already
exists as a PR, and it is git-only. This is a local pre-submit gate -- it
reviews what you are about to push, defends each finding through an
independent validator pass, and remembers your prior decisions across
re-reviews of the same change. Use native review for someone else's merged
or in-flight PR; use this before your own work leaves the machine.

Claude Code's native /code-review reads ancestor CLAUDE.md files too, so the
standards `skills-kit` authors and audits improve any reviewer that reads
them, this one and the native one alike. That is git-kit's place in an
authoring -> auditing -> review path: `skills-kit` (in this marketplace)
authors and audits the CLAUDE.md standards, git-kit reviews git changes
against them per-change, and `p4-kit` does the same for Perforce.

## Install

```
/plugin marketplace add kitaekatt/plugins-kit
/plugin install git-kit
```

The `bootstrap` plugin is installed automatically as a dependency and
provisions git-kit's environment on the first session start (silently, when
healthy -- no output means it worked).

## Try this first

With uncommitted work or a branch ahead of its upstream:

```
/git-code-review
```

It will state the range it inferred before spawning anything; pass an
explicit range (`<a>..<b>`, `--staged`, `--working`) if it guessed wrong.

## Prerequisites

- `git` (bootstrap installs it on Windows if missing).
- `gh` is provisioned by bootstrap for the GitHub-side features (auth,
  optional org membership); the review itself only needs git.

## Opt-in worktree lifecycle

Worktree management is off by default. A repository can enable a shared ready
pool through approved settings in its tracked `.claude/bootstrap.json`.
The lifecycle requires a usable git-kit installation enabled at user scope.
Installation alone does not enable repository worktree management.

| Installation scenario | Lifecycle result |
|---|---|
| Usable, enabled user scope | Works after repository approval. |
| Project scope only | Refuses with "install git-kit at user level" and the install command. |
| Not installed | Not applicable: no git-kit lifecycle commands or hooks. |

`worktrees.py configure --propose` shows defaults and a project-fit proposal.
`configure --apply <approved.json>` writes the approved repository settings.
The proposal command does not write settings. The disk floor comes exclusively
from user settings and defaults to 10 percent.

For an enabled repository, `take --agent <id> --session <id> [--task <id>]`
claims a ready worktree at `<root>/<repo>/<agent-id>`.
`git_kit.worktrees.root` uses bootstrap's layered settings and defaults to
`~/.worktrees`. Lifecycle records, journals, logs, and locks live under
`~/.claude/plugins/data/plugins-kit/git-kit/worktrees/<repo>/`.
A take completes mapped setup and path-bound setup before it returns success.
An empty pool blocks the take with exit 4 and requests maintenance.
A failed take keeps its claim, files, and error log.

`parked [--session <id> | --task <id>]` lists waiting work, including taskless
entries. Take shows this session's and task's parked entries in full and counts
the rest. `adopt --id <worktree-id> --agent <id> --session <id>` atomically
claims a parked entry, preserves its branch and files, and completes setup at
the agent path. A lost adoption reports the winning holder. Live or uncertain
process evidence blocks adoption. Failed setup keeps the adopting claim.

`check-task-close --task <id> --json` returns `allowed` and `parked` fields.
A configured task-kit pre-close command can use this result to block close
and archive while the task has parked work.

`discard --id <worktree-id> --propose` identifies the target and work at risk.
`discard --id <worktree-id> --apply <approved.json>` applies explicit user
direction for that proposal. Changed evidence, a holder, or a live or uncertain
process blocks discard. `--force` supplies no authority. The record retains
the identified work, user direction, and outcome.

Repository `after_take` and `before_release` settings contain command lists.
Git-kit records their results, logs, process identities, and opaque resource
claims. A script error keeps the holder. Project scripts own servers, ports,
and public routes. The [lifecycle reference](skills/worktrees/references/pool.md#project-extensions)
defines the script protocol.

Agents commit with plain `git commit`. After a full push, the caller can run
`release --claim <id>` from outside the claimed directory. A partial push keeps
the claim. A release error reports separately from the successful push.
`status --json` reports current Git observations, holders, errors, disk, and
maintenance. Maintenance reconciles interrupted operations and reuses safe
worktrees before it builds a deficit. It preserves branches and unique ignored data.

Commands, settings, records, safety evidence, and the library call for push
integration: [Lifecycle reference](skills/worktrees/references/pool.md).
The legacy `new`, positional `take <task-name>`, and raw `remove` paths refuse mutation.
The supported `take --agent ... --session ... --task ...` form accepts task metadata.
Existing namespaces require migration before lifecycle mutation.

## Background review

By default a review runs in the background on a frozen snapshot, so the
session stays free and the agent can keep editing the same files while
reviewers read the frozen copy.

1. `snapshot_review.py freeze --paths <p>...` (or `--patch F` / `--staged`)
   records this session's change as a commit under
   `refs/git-kit/review/<sha12>`. It is built in a private index and never
   touches the shared index or the working tree. It writes no scratch copy.
2. The background runner runs `prepare_review.py --snapshot <sha>`, which
   writes the snapshot's full tree as a scratch copy under the git-kit data
   directory. Reviewers read only that copy. The runner never asks the
   user, edits, commits or lands; it returns findings and questions to the
   main session, then `drop <sha> --tree-only` removes the scratch copy.
3. Fixes are reviewed as a delta: freeze again and review with
   `--base <previous snapshot>`.
4. `snapshot_review.py land <sha> -F <msgfile>` commits exactly the reviewed
   snapshot (same tree, or the same patch when HEAD moved meanwhile). Edits
   made after the freeze stay uncommitted, and other sessions' changes are
   not included. It then removes the ref and the bundle directory.
   `drop <sha>` abandons a snapshot without committing.

Pass `--inline` to run the review in the foreground against the live working
tree instead. Snapshot refs are local only. `snapshot_review.py prune`
removes snapshot refs older than 7 days (`--older-than-days N` changes the
age).

## When not to use it

- **Trivial single-file mechanical changes.** The pipeline has a triviality
  guard for typo-sized doc edits, but the run still costs tokens; a version
  bump does not need a review fan-out.
- **Post-hoc review of someone else's merged work or an existing PR by
  URL.** That is what the native /code-review is for; this skill works only
  against the local working copy and refs.
- **Very large branches on a budget.** Reviewers scale as (roles x chunks)
  and each finding gets a validator. Profiles reduce the cost for data-only
  diffs, but a big code diff is genuinely token-expensive.
