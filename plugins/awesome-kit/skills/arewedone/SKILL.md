---
name: arewedone
description: Use when the user asks whether this agent's session work is complete or what remains. Do NOT use for other sessions' work.
---

# Are We Done?

Audit the entire available session and close outstanding authorized work before
reporting. This procedure grants no new authority and creates no requirement for
the user to say "go" again.

The stopping rule is completion. Work is done when the requested outcome is
implemented, verified, reviewed where review applies, and published where
publication was authorized. A check is not a boundary: finishing an audit, a
test run, or a review does not end the work, and the work that check exposes
continues. An incidental problem is not scope: a problem that does not prevent
the requested outcome does not expand the task. Stop early only when the user
pauses or redirects, or when further progress needs a specific user decision,
unavailable access, or an external change.

## 0. Choose the mode

Evaluate this gate before step 1. Active mode applies only when every condition
holds:

- Work this agent launched and owns is still running: a background job, a
  delegated agent, a workflow, or a session cron.
- Its result is the only thing the requested outcome still waits on.
- No other actionable work remains this turn: nothing to implement, verify,
  review, commit, or publish independently of that result.

**Active mode.** State in one line what is running, then make the last line
"Waiting on <what is running>". Skip the ledger, the buckets, and the verdict
line.

Ending a turn is not waiting on yourself. A turn that stops with no owned work
running is not active mode, whatever work was in progress when it stopped. When
any condition fails, including actionable work that merely sits beside a running
job, use finished mode.

**Finished mode.** Run steps 1-4. Report only what the last message did not
already say. When nothing is new, the report is the verdict line alone.

Choose the ending by what is running. "Waiting on" fits only while owned work is
still running. "All done" fits when nothing owned is running, which covers a
Done verdict from step 4 and a turn with nothing pending.

## 1. Build the session ledger

Inspect this session's edits, commits, publications, configuration writes,
migrations, launched jobs, and verification results. Add explicit user requests,
later scope changes, assistant commitments, and open items of the assigned task.
Audit the whole available session rather than only the latest exchange.

Inspect only the assigned task's live-item surface. Other sessions' tasks,
uncommitted changes, and background work are outside this ledger. A foreign
change enters only when it actually blocks an operation this session must perform;
describe the blocked operation, not the other session's work.

Completion includes required validation, review, and authorized publication.
A clean working tree alone does not prove that external writes, migrations,
reviews, or jobs succeeded. Confirm the state relevant to each ledger entry.

Completion criterion: every requested outcome and lasting action has a verified
result or an identified gap.

## 2. Establish which gaps prevent completion

For each gap, name the action needed and the task-relevant consequence of leaving
it unresolved. Support the consequence with a requirement, an observed failure,
or a concrete causal path. An observation with no such consequence is not work.

A problem discovered while doing this session's work belongs in the audit when
it prevents the requested outcome. Investigate that connection before deciding
whether it is remaining work. Reading a known-problem list or receiving a hook,
bootstrap, or system report does not create ownership. Pre-existing conditions
this session neither changed nor was asked to repair stay outside the audit
unless they block its requested outcome.

An unavailable tool with a successful authorized fallback leaves no incomplete
check. Recovering the unavailable capability is separate work unless the outcome
requires it. Installed versions, config placement, endpoint availability, and
cache retention are not work merely because they look unusual; require an
observed task failure or a tracked rule whose violation prevents the outcome.

Scratch directories and their contents are working surface, not remaining work
or suppressed findings. Other ephemeral findings with a real follow-up are kept
out of the work order and counted once under Suppressed. Ephemerality requires
regenerability from tracked sources or preservation elsewhere; an ignored path
alone is insufficient. Resolve its owning repository if that is the only evidence.
Preserve untracked evidence, backups, and run records. Delete only within the
user's authorization, never merely to make status clean.

Completion criterion: every surviving gap has an owned outcome, a concrete
consequence, and a next action; incidental observations have been excluded.

## 3. Close authorized gaps

Execute work covered by the user's request and the authorizations visible in this
session's context, in dependency order: investigate, implement, verify, review,
commit, and publish as applicable. Stage explicit owned paths and preserve
unrelated work.

When the implementation is uncertain, gather evidence and establish a correct
approach before making changes. A low-confidence implementation is a reason to
investigate, not a reason to stop the audit. Routine implementation choices
remain with the agent.

Apply the stopping rule when an action requires a user decision, access, or an
external change. Preserve explicit publication, deletion, and other gates; an
audit grants no authority to bypass them. Complete independent work while a
required dependency is pending. Report a failed attempt honestly and distinguish
it from a successful recovery; choose subsequent attempts from new evidence
rather than an arbitrary retry count.

Return newly discovered gaps to step 2 and repeat until the authorized outcome
is verified or every remaining item has a specific unresolved dependency.

Completion criterion: no authorized, actionable work is left merely for another
"go"; every unfinished item states why the agent cannot progress independently.

## 4. Confirm and report

Confirm results against the state left by step 3: owned working-tree changes,
remote refs, active jobs, review findings, and relevant external state. Report
unavailable evidence precisely rather than treating recollection as verification.

Use only non-empty buckets:

- **Cleanup I took care of:** completed actions in the past tense, with a checkable
  result such as a commit, remote ref, migration check, or review finding count.
- **Remaining work:** required actions blocked by a named dependency. State the
  exact decision, access, or external change needed and the action it would enable.
- **Gotchas:** unresolved questions affecting the requested outcome that require
  user judgment or evidence the agent cannot obtain. State the problem, the
  proposed direction, and that dependency. Implementation uncertainty alone
  belongs in step 3.

Derive the verdict from the confirmed results:

- **Done:** all requested outcomes were verified and both outstanding-work
  buckets are empty. A successful recovery is not an unresolved failure.
- **Not done:** an owned requirement remains unmet; name its actual blocker.
- **Cannot verify:** the outcome may be complete but a specific required state
  inspection is unavailable. Identify the missing evidence.

A suppressed ephemeral finding does not change the verdict. Emit its count on
one line only when nonzero; scratch contents are excluded from that count.
When nothing actionable remains, optionally propose transcript-only insights
worth encoding and their owning documents. A proposal is not a completion blocker.

```text
<Done | Not done | Cannot verify>. <What this session owned and audited.>

Cleanup I took care of:
- <Completed action> -- <verified result>.

Remaining work:
- <Required action> -- Blocked by: <specific dependency and what it enables>.

Gotchas:
- <Unresolved question> -- Proposed direction: <follow-up>; needs <dependency>.

Suppressed: <count> ephemeral <short labels> -- ask to see them.

Worth making durable (only when nothing actionable remains):
- <Insight> -- <owning document>.
```

Omit each empty bucket, the zero-count Suppressed line, and empty durability
proposals. Report only what the last message did not already say; when nothing
is new, give the verdict line alone. Keep the report concise; it records
completed work and real blockers rather than inviting renewed authorization for
work already requested.

## Harness descriptor

Keep [agents/openai.yaml](agents/openai.yaml) aligned with this procedure. Its
default prompt must request active mode (one line naming what runs, then a last
line "Waiting on <what is running>" in place of "All done", with no ledger or
buckets, only when owned running work is all that remains and nothing else is
actionable), the "All done" ending when nothing owned is running, finished mode
(whole-session auditing, closure of authorized gaps, verified outcomes, named
blockers, omission of empty buckets, the single nonzero suppressed-count line,
and only what the last message did not already say), and the inline stopping
rule. It must not introduce another approval boundary.
