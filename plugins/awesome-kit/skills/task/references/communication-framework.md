# Communication framework

How the human and the agent communicate across turns and sessions. This is the canonical glossary; the two skills that operationalize it -- `/verbose-updates` (turn-level) and the `task` skill (session-level, via hand-off folders; the evolved `/hand-off`) -- both reference it. When a term defined here appears in either skill, the definition lives here; the skill describes only how the protocol applies the term.

The framework references `/knowledge-encoding` (in plugins-kit:skills-kit), which is the meta-skill for evolving the framework itself.

## Glossary

### work-unit

The current goal, project phase, or task the user and agent are jointly working through. The work-unit is the durable anchor every communication artifact references -- *what changed* expresses progress against it, *where it sits* anchors changes to it, *required user action* surfaces the next step within it or the question of whether it is complete.

A work-unit has natural scope (phase or project), not session scope. Sessions come and go; the work-unit outlives them. The hand-off (task) folder slug names the work-unit, not the session.

If the work-unit is unnamed, no template can be written; ask the user to clarify before continuing.

### auto-loaded vs on-demand context

The two gates that govern when content reaches the agent.

- **Auto-loaded** -- present without action. Three instances in this framework:
  - End-of-turn messages (the user reads only these under `/verbose-updates`).
  - `CLAUDE.md` of a hand-off folder (Claude Code auto-loads when cwd is the folder).
  - Files `CLAUDE.md` directs the agent to read on turn 1 (notably `plan.md`).
- **On-demand** -- loaded only when the agent has a reason. Everything else.

The discipline across both skills is keeping the auto-loaded surface tight. Anything that does not need to be in the agent's head at the relevant trigger belongs in an on-demand artifact.

### three-part end-of-turn template

Every reply that ends a turn -- under `/verbose-updates` -- has THREE parts, in order:

- **What changed** -- the specific action taken in this turn, with concrete artifacts named.
- **Where it sits** -- how this change relates to the broader in-flight work the user is tracking (the work-unit, the staged CL, the plan with parked decisions).
- **Required user action** -- exactly one of the two states defined below.

The template is the auto-loaded surface for turn-level communication. Skipping a part means the user has to ask for it, which is the friction the framework exists to prevent.

### State A / State B (required user action)

A binary classification of the end-of-turn state. Exactly two options.

- **State A -- user action required.** Name one concrete next decision, file to review, or question in one sentence. Default for any turn that ends mid-work-unit.
- **State B -- all requested work complete - ready to end session.** Every item the user has requested is done; use the exact phrase verbatim. The phrase is heavy on purpose: before claiming State B, enumerate every requested item against live state (not memory) and verify each.

If the agent has more work to do, it is in State A. "Required action: none. Continuing..." is a third-option violation: continuing means the turn is not over.

### orientation moment

A turn where the agent re-anchors against work that already happened, rather than performing new work. Two instances in this framework:

- **First update after `/verbose-updates` invocation** -- *what changed* summarizes the substantive work of the preceding 1-3 turns, not the trivial fact of the skill invocation.
- **Turn 1 of a fresh session resumed from a hand-off (task) folder** -- per the opening-response protocol in `CLAUDE.md`, the agent restates the current goal, names the first concrete action, and flags blockers before picking up tools.

Both share the same shape: orient first, then act. The opening-response protocol is the explicit text the agent must produce on session resume; the framework's general rule is *do not silently jump to tool use after a context jump*.

### self-contained briefs

Every turn is readable cold. No "as I mentioned earlier"; no references to prior turn indices; no assumption the user remembers a decision from three turns ago. The plan, hand-off folder, and durable artifacts are the durable substrates; the live conversation is not.

Corollary: if a system-reminder describes resumed state ("queued next concrete action is X"), verify against live state before acting on it. Stale carry-forward is common -- restate the actual next decision and flag the stale note explicitly.

### hand-off baton

The explicit transfer signal at the end of a hand-off reply (the turn that packages a task folder for a fresh session) -- a two-line block ending the response. For a task folder it names the working directory and the resume command:

```
CWD: <project root directory>
Continue: /task work <CWD-relative task-folder path>
```

The user copies the `Continue:` line, opens a new session, and pastes it as the first user message; `/task work` makes the folder the current task and loads its context. The `CWD:` line names the directory the path is relative to (paths stay CWD-relative, never absolute, so the baton works across machines). Without the baton, the user has to compose the instruction themselves.

### provenance triad

When recording a decision in a durable artifact (project plan, decision log, audit log), three fields suffice:

- **Surface** -- what was reviewed (file, slice, grep, probe, conversation moment).
- **Finding** -- the specific observation that triggered the decision.
- **Follow-up** -- the resulting action, decision-log entry reference, or upstream-fix task.

Outcomes alone are not enough; the next agent picking up cold cannot reconstruct what surface revealed the smell. Chat memory is not durable.

### argument-based invocation modes

When a skill in this framework is invoked with an argument (`/<skill> <arg>`), it may switch from its default behavior to a focused report shape selected by the argument. Default (no argument) keeps default behavior. Each mode must (a) name the specific question the argument answers, (b) define the reply shape and length, (c) state whether it replaces or supplements the default.

Modes must be orthogonal -- the user should never have to decide which of two overlapping modes to invoke.

## Principles

- **Context is a budget.** Auto-loaded surface is paid every session (for files) or every turn (for messages). On-demand corpus is free until read. Spend the budget on what is needed at the trigger; rotate the rest out.
- **The work-unit is the anchor.** Both skills assume the agent maintains awareness of the current work-unit. If you cannot name it, the framework's other rules cannot be applied.
- **Rotate forward, not backward.** The auto-loaded surface (`plan.md`, end-of-turn messages) is a moving window. Details move in as work approaches and out as it completes; the surface never accumulates as a record.
- **Conversations are ephemeral; the workspace persists.** What lives on is the durable artifact -- plan, hand-off folder, decision log. Per `/knowledge-encoding`, design the artifact to make future sessions smarter, not just to close the current task.
- **Match the document's format.** When extending an artifact (plan, log, skill), match its existing structure rather than inventing your own. Anti-patterns, glossary entries, decision-log entries each have a shape; preserve it.

## How the skills use this framework

- **`/verbose-updates`** operationalizes the three-part template, State A/B, and orientation-moment-after-invocation for end-of-turn communication. It is the per-turn protocol.
- **The `task` skill** (the evolved `/hand-off`) operationalizes auto-loaded vs on-demand, the hand-off baton, and orientation-moment-on-resume for cross-session communication via hand-off task folders (see its `references/handoff-template.md`). It is the per-session-boundary protocol; the template's *Communication protocol* section sets `/verbose-updates` as the default for the next agent's turns.
- **`/knowledge-encoding`** is the meta-skill: when an insight emerges that should evolve the framework itself (a new term, a new principle, a new anti-pattern), it directs the encoding back into this document or its referencing skills.

## Extending the framework

Treat additions to this glossary as encoding decisions per `/knowledge-encoding`. Before adding a term, verify it is genuinely shared across both operational skills (or about to be). If it lives in only one skill, it stays in the skill, not here. The framework's value is the canonical reference; expanding it with skill-specific concepts defeats that.
