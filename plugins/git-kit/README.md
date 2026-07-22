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
- **CLAUDE.md-convention-aware review.** A dedicated reviewer checks the diff
  against ancestor CLAUDE.md files, and may flag a project-rule violation
  only when it can quote the rule verbatim.
- **Path-scoped submit gates.** `Submit gate:` blocks authored in CLAUDE.md
  files become a confirmation checklist when the range touches their scope.
  Advisory, not enforcement.
- **Cost routing.** A `data_only` profile handles docs/config-only diffs with
  fewer, cheaper reviewers; the full `code` profile runs Opus-grade reviewers
  where semantic reasoning pays off.

Output is rendered markdown in chat. Nothing is written to disk, no PR
comment is posted.

## How it differs from the built-in /code-review

The native review is post-hoc and PR-oriented: it looks at work that already
exists as a PR, and it is git-only. This is a local pre-submit gate -- it
reviews what you are about to push, defends each finding through an
independent validator pass, and remembers your prior decisions across
re-reviews of the same change. Use native review for someone else's merged
or in-flight PR; use this before your own work leaves the machine.

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
