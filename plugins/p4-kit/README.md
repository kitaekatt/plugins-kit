# p4-kit

AI code review for pending Perforce changelists -- before you submit.

## What it does

`/p4-code-review` runs a multi-agent review of a pending CL directly in
conversation. It resolves the CL number (or lists your pending CLs), fetches
the diff -- **auto-shelving the CL if it has no shelf**, since a shelf is the
only way to get shelved-file content -- partitions it into chunks on disk,
and fans out 2-3 parallel reviewer subagents per chunk by review profile.
The mechanisms that make it more than "ask a model about a diff":

- **Per-issue adversarial validators.** Every candidate finding is re-checked
  by an independent subagent that cannot see who raised it. Unconfirmed
  findings are dropped; only the confirmed set renders.
- **Declined-findings ledger.** Findings you dismissed for this CL are
  recorded (keyed on a normalized anchor, not line numbers), so a re-review
  at the same baseline does not re-raise them. Reshelving or editing the CL
  invalidates the baseline and findings resurface; serious findings are
  never suppressed.
- **CLAUDE.md-convention-aware review.** A dedicated reviewer checks the diff
  against ancestor CLAUDE.md files and may flag a project-rule violation
  only when it can quote the rule verbatim.
- **Path-scoped submit gates.** `Submit gate:` blocks in CLAUDE.md files
  become a pre-submit confirmation checklist when the CL touches their
  scope. Advisory, not enforcement.
- **Cost routing.** A `data_only` profile reviews docs/config-only CLs with
  two Sonnet reviewers (CLAUDE.md compliance, surface-level bugs) and Sonnet
  validators. The full `code` profile runs Sonnet for CLAUDE.md compliance
  and Opus for the two deep-reasoning roles (diff-only bugs,
  introduced-code review), with Opus validating bug findings and Sonnet
  validating compliance findings. These assignments are the defaults; ask
  Claude to use a different model to override them.
- **Fingerprint-checked cleanup.** When the review auto-created the shelf, it
  deletes it afterward -- but only if the live shelf still exactly matches
  the recorded fingerprint. If you reshelved or edited in the meantime, the
  cleanup is a silent no-op; your work is never touched.

Output is rendered markdown in chat. Nothing is written to disk or Swarm.

## How it differs from the built-in /code-review

The native review is post-hoc, PR-oriented, and git-only. This is a local
pre-submit gate for Perforce -- a workflow the built-in review does not
support at all -- with independent validation of every finding and memory of
what you already declined.

The native /code-review reads ancestor CLAUDE.md files too, so the standards
`skills-kit` authors and audits improve any reviewer that reads them, this
one and the native one alike. That is p4-kit's place in an authoring ->
auditing -> review path: `skills-kit` (in this marketplace) authors and
audits the CLAUDE.md standards, p4-kit reviews pending Perforce changelists
against them per-change, and `git-kit` does the same for git.

## Install

```
/plugin marketplace add kitaekatt/plugins-kit
/plugin install p4-kit
```

The `bootstrap` plugin is installed automatically as a dependency and
provisions p4-kit on the first session start (silently, when healthy -- no
output means it worked). Bootstrap will also install the `p4` CLI if it is
missing.

## Try this first

From a directory inside a Perforce workspace, with a pending CL:

```
/p4-code-review
```

It lists your pending changelists and asks which to review; pass a CL number
to skip the prompt.

## Prerequisites

- A working `p4` client and workspace: valid login and a `.p4config` (or
  equivalent P4PORT/P4CLIENT/P4USER environment) resolvable from the
  project directory. Bootstrap installs the `p4` binary, not your server
  connection.
- The CL must be **pending**, not submitted. The review may create and later
  delete a shelf for the CL (with the fingerprint guard above).

## When not to use it

- **Trivial single-file mechanical changes.** The pipeline has a triviality
  guard for typo-sized doc edits, but the run still costs tokens.
- **Submitted CLs or someone else's merged work.** The skill reviews pending
  CLs only; for git-hosted PRs use the native /code-review.
- **Very large CLs on a budget.** Reviewers scale as (roles x chunks) and
  each finding gets a validator. Profiles cut the cost for data-only CLs,
  but a big code CL is genuinely token-expensive.
