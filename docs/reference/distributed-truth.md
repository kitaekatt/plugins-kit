# Distributed truth

How task-kit, and plugins-kit tooling generally, handles shared state across the
user's machines. Maintainer and agent facing. Each principle is a rule, a
one-line reason, and its task-kit instance.

## 1. Git is the single source of truth for shared state

Machine-local storage holds only (a) information private to that machine
(credentials, its own process observations taken at read time) and (b)
transient coordination with no information content (an OS lock that exists only
while held). Never keep a cached copy of a shared fact.

Why: a local copy of a shared fact is a second truth that can disagree with git,
and the machines have no other way to reconcile.

Instance: task-kit keeps no working-session index and no publish log. Shared
session state is committed under `dev/tasks/.sessions/`.

## 2. One writer per file

Each machine writes only its own files (`dev/tasks/.sessions/<host>.yaml`).
Another machine contributes by writing to its own file, never by editing
someone else's. The publisher pushes only its own files.

Why: no two machines edit one file, so there are no merge conflicts.

## 3. Verify only what you can observe

A machine verifies its own claims: pid plus the exact process create time. A
claim from another machine is `unverified`, shown visually differentiated (a
tint), with its host and age. Do not guess, and do not hide it.

Why: a remote process cannot be observed, and a stale guess shown as fact is
worse than an honest "unverified".

Instance: the dashboard tints other-host sessions. Future: a task-kit service
running on every machine may answer "is this valid", which upgrades a claim from
`unverified` to confirmed. Nothing depends on that service existing.

## 4. Never prevent an action because of an unverifiable state

Only a verified fact may block. An unverified fact may be shown, never enforced.

Why: a blocker nobody can check strands the user behind state they cannot clear.

Instance: only this machine's verified-running session holds the dashboard. A
foreign session record never blocks `/task work`.

## 5. A human's input is information

A dismissal is a committed fact in the dismisser's own file: who, when, and the
target (host, session, `registered_at`). Every reader honors it. It is
authoritative even over a process that looks alive.

Why: the human knows what the machines cannot observe, and that knowledge must
reach every machine through git like any other fact.

Instance: dismissing an unverified field in the dashboard writes a tombstone and
commits it; all machines then hide that record.

## 6. A human's cross-machine view is a valid signal

The human sees every machine; no single machine does. So a contribution a
human makes from any machine (a dismissal) is valid information on its own,
and the owning machine applies it without re-checking or overruling it. The
owner removing the dismissed record from its file at its next write is
housekeeping that keeps one writer per file (principle 2), not arbitration;
the tombstone is dropped once its target is gone.

Why: the human has visibility across machines that no machine has, so their
signal outranks any one machine's local view.

## 7. Commit locally first, publish eventually, report always

Commit a record before any network access. Offline means committed but
unpublished; a later publish drains it. Report every failure where it happens;
never swallow one.

Why: a record survives a network failure, and a silent failure becomes silent
data loss.

## 8. Lifetimes follow intent, not processes

A task registration lasts from `/task work` to `/task hand-off`. A harness exit
or crash does not end it. A human interruption is the human's to clean up
(`task sessions release`).

Why: leaving a harness is not abandoning the task, and process liveness is not
the user's intent.

## 9. No dependency on any service running

Every read and write works from the CLI and git alone. The dashboard is a view.

Why: a feature that needs a daemon fails whenever the daemon is down, and the
user does not want the dashboard to be load-bearing.

## Open edge

A dead session on another machine stays shown as `unverified` until a human
dismisses it or its owner syncs. This is accepted: showing an unverified record
costs a tint, and removing it on a guess breaks principles 3 and 4.

## Rulings

User rulings, 2026-10-21, from which these principles derive:

> if the data isn't in the task file, wherever it goes still needs to be commited, because we run this dashboard on multiple machines

> I don't like any dependency on the dashboard running

> i mentioned including the process so we can verify local machines. For an external machine i agree for now that can be unverified. Eventually we will make it so the task-kit service is running on all the machines all the time, and then you can ask the other machine if it's valid or not, but that's for the future

> If you exit a harness I don't think that should count as interrupting working on a task.

> We never prevent an action because of an unverifiable state

> We should be able to dismiss fields that  are unverified. Unverified can be shown through a tint or other visual differentiation

> No it definitely changes it in git. Why wouldn't it. We need a way of reconciling truth that is git.

> The human dismissing is contribution of information

> I don't think there should be anything machine specific except for private information specific to this machine

> Why should I privately cache that you are working on a task you aren't?

> I am not convinced we need anything besides global state tracked into git.

> 6 is just a valid information signal as the user has visibility across machines.

Request: "let's just have documented principles for how we handle truth in a distributed environment."
