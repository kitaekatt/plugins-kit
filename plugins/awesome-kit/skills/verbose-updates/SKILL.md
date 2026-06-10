---
name: verbose-updates
author: christina
skill-type: technique-skill
description: Use when detailed context tracking is needed (context-switching, background work). Do NOT use for standard reporting.
disable-model-invocation: true
---

# Verbose Updates

When the user invokes this skill, they are signaling that they are context-switching, not paying close attention to every turn, or otherwise unable to carry conversation state in their head. Following the communication protocol below is what keeps progress moving in that state. Failure to follow it produces slow progress, wasted tokens, and user frustration -- the user has to re-orient on every reply, push back on under-specified hand-offs, and re-teach the agent what it should already be doing.

The base assumption is that the user only reads end-of-turn messages -- they are the auto-loaded surface for turn-level communication (see `../task/references/communication-framework.md`). Mid-turn narration and intermediate tool calls are on-demand at best; nothing important may live exclusively there.

This skill operationalizes the turn-level side of the communication framework. The shared glossary -- `work-unit`, `auto-loaded vs on-demand context`, `three-part end-of-turn template`, `State A / State B`, `orientation moment`, `self-contained briefs`, `provenance triad`, `argument-based invocation modes` -- is canonical at `../task/references/communication-framework.md`. Definitions live there; this file describes only how the protocol applies them.

The protocol is built around one central rule: every end-of-turn reply uses the framework's three-part template. The other rules support that template by specifying what each of its parts must contain.

The template depends on the agent maintaining awareness of the current `work-unit`. If you cannot name the current work-unit, you cannot write the template -- ask the user to clarify it before continuing.

## Contract

The load-bearing contract; the prose below is the detailed protocol the steps reference.

```yaml
technique_skill:
  _schema_version: "1"
  trigger_model: user-only
  identity: Communicate turn-level progress to a context-switched user via the framework's three-part end-of-turn template.
  scope:
    covers:
      - composing every end-of-turn reply as What changed / Where it sits / Required user action
      - classifying the turn as State A (action required) or State B (all work complete)
      - splitting the reply at work-unit boundaries
      - argument modes (e.g. strategy) that replace the default template
    excludes:
      - standard reporting when the user is tracking closely (do not over-apply)
      - project-specific protocols (audit logs, provenance shape) which live in the project plan
  techniques:
    - id: end-of-turn-update
      name: Write the three-part end-of-turn update
      keywords: [end of turn, what changed, where it sits, required action, state a state b]
      goal: Leave a context-switched user fully re-oriented from the end-of-turn message alone, with exactly one required action or a verified State B.
      steps:
        - n: 1
          action: Name the current work-unit. If you cannot name it, ask the user to clarify before writing anything else.
        - n: 2
          action: "Write the What-changed part -- name each artifact and the action, measured against the work-unit, not the previous turn. On the first update after invocation, summarize the substantive work of the preceding 1-3 turns (the orientation moment), not the skill invocation itself."
        - n: 3
          action: "Write the Where-it-sits part -- re-anchor the change in the broader in-flight state (the CL, the atom sequence, the plan). If the change is standalone, say so explicitly."
        - n: 4
          action: "Write the Required-user-action part -- enumerate every requested item against live state, then classify. State A names one concrete next decision in one sentence; State B uses the exact phrase 'all requested work complete - ready to end session' only when every item verifies as done."
          on_failure: A false State B silently abandons work; when unsure after the enumeration, you are in State A and owe a concrete action. If you have more work, do not end the turn -- do the work.
        - n: 5
          action: At a work-unit boundary, emit two back-to-back updates (close the finished unit, then open the next), each using the full template. Do not ask permission to move to the next unit -- the plan authorizes it.
      gotchas:
        - The ordered-step heuristic counts explanatory numbered lists as procedure steps; the real procedure is the three-part template, not those lists.
        - Auto mode authorizes execution, not chasing already-done work or fabricating next steps -- when the next decision is the user's, surface it and stop.
        - Every artifact gets its absolute or project-relative path; never make the user remember which file is the current surface.
  anti_patterns:
    - id: permission-to-proceed
      name: Asking permission to move to the next work-unit
      keywords: [permission gate, should i proceed, next phase, round trip]
      why_it_seems_right: Confirming the transition feels safe and collaborative.
      why_it_is_wrong: The plan or phase sequence already authorizes the transition; the gate costs a round-trip the user did not need to pay.
      alternative: Move forward by default; surface only a genuine decision (scope ambiguity, risk, missing input) in the next update's Required-user-action slot.
    - id: stacked-deliverables
      name: Re-issuing unrequested work while-I-am-at-it
      keywords: [stacked deliverable, scope creep, extra work, single action]
      why_it_seems_right: Doing more than asked looks proactive.
      why_it_is_wrong: It violates the template's single-required-action rule and produces stacked-deliverable replies a distracted user cannot parse.
      alternative: When the user asks for X, deliver X; park any Y in the plan, not the chat.
```

## The end-of-turn template (required)

Every reply that ends a turn -- whether parking work, handing back for a decision, or just confirming a small change -- uses the framework's three-part template: **What changed / Where it sits / Required user action**. The framework defines the parts; this section specifies what each part must contain at the operational level.

The template is not optional and not negotiable. Skipping a part means the user has to ask for it, which is exactly the friction this skill exists to prevent.

### What changed

Name the artifact and the action. Vague is wrong:

- BAD: "Done."
- BAD: "Renamed the file."
- BAD: "Fixed it."
- GOOD: "Renamed `.claude/skills/<old-name>/SKILL.md` -> `.claude/skills/<new-name>/SKILL.md` and added it to the default p4 CL."
- GOOD: "Locked `Fluffalo` as a sourceless atom in `.claude/skills/localization-domain/sourceless_glossary.yaml`."

If multiple things changed, list them, but each line still names the artifact.

*What changed* is measured against the current work-unit, not the previous turn. A turn that advances the unit produces a substantive *What changed*; a turn that merely confirms or clarifies still names what was confirmed in work-unit terms (e.g. "Confirmed `flow` is not in the glossary"); a turn whose answer does not relate to the work-unit at all is evidence either that the unit is unclear (ask) or that the work is not done (keep going).

**Special case: the first update after the skill is invoked.** This is an `orientation moment` (see framework). The act of invoking the skill is technically the current turn, but the user is re-orienting against work that already happened -- the meaningful question they bring is "where are we?", not "what did the skill invocation just do?" So the first *What changed* after invocation summarizes the substantive work of the preceding 1-3 turns (whatever it takes to anchor the user in the current state), not the trivial fact that a skill was invoked. Name the artifacts that changed, the decisions that landed, and any handoffs in flight, exactly as those updates would have read if the verbose template had been active all along.

### Where it sits

How does this change relate to the rest of the in-flight work? The user has been tracking a broader state -- a CL of staged files, a sequence of atoms locked, a decomposition pass mid-flight, a plan with parked decisions. The reply re-anchors the user in that broader state every time:

- "This is the 14th file in the default p4 CL alongside the loc-ops glossary work."
- "Bucket regenerated; composed_count went from 2,822 to 2,836 (+14 new compositional decompositions)."
- "Skill rename only; no other references to the old name in `.claude/` or `tmp/`."
- "Test suite still at 818 passing."

When the change is genuinely standalone, say so explicitly ("standalone change, no in-flight context"). Don't leave the user guessing.

### Required user action

The framework defines `State A` and `State B` as the only two end-of-turn states. This section specifies the operational rules:

**State A -- user action required.** Name one concrete next decision, file to review, or question in one sentence.

- GOOD: "Required action: confirm whether to ship the staged 14 files as one Mix CL or split the skill change off."
- BAD: a numbered list of three open questions (the user picks one; the others rot).
- BAD: silence (the user has to ask).
- BAD: "Required action: none. Continuing on Phase 3.2." -- declaring no action required while implicitly handing off more work to yourself. If you are continuing, the turn is not over.

If two decisions genuinely need to surface, pick the more urgent and park the second in the project plan with a note, not in the chat.

**State B -- all requested work complete - ready to end session.** Use the exact phrase. The phrase is heavy on purpose -- before claiming State B, pause and enumerate every item the user has requested across the active session. Include every direct request, every parked decision the agent surfaced and the user answered, every plan-defined unit the agent committed to completing this turn, and every artifact the agent said it would produce. Then verify each item against live state, not memory.

A distracted user trusts the classification. A false State B claim leaves requested work undone, and the user has no signal to push back. If the user discussed three things and the agent did two, the agent is in State A -- the third item is the required action, not a forgotten one. The cost of being wrong on State B is silently abandoned work; the gravity of the phrase is what makes the introspection step honest.

The classification is binary. If the agent is not confidently and verifiably in State B after the enumeration, it is in State A and owes the user a concrete action. If you have more work, do not end the turn; do the work.

## Transitioning between work-units

In a multi-work-unit context -- a plan with phases, a sequence of tasks, a staged migration -- turns frequently span work-unit boundaries. When a single turn both completes one unit and begins the next, the reply contains TWO end-of-turn updates back-to-back, each following the full template:

1. The first update describes the completion of the just-finished work-unit. *What changed* is the closing-out summary of that unit's progress. *Where it sits* notes that the unit is complete. *Required user action* names the next unit in the plan, explicitly.
2. The second update describes whatever work was already accomplished moving into the next work-unit (often partial -- the agent picked up the next unit, did some work, and now ends the turn). *What changed* is what advanced in the new unit. *Where it sits* anchors to the new unit. *Required user action* is the next decision within that unit.

Splitting the reply this way keeps each update scoped to a single work-unit, so a user reading only end-of-turn messages can track unit boundaries cleanly.

Asking the user for permission to move on to the next work-unit is an anti-pattern. The plan or phase sequence already authorizes the transition -- moving forward is the default. "Should I proceed to the next phase?" forces the user to confirm work that does not need confirmation, and the cost is one round-trip the user did not need to pay. If the next unit raises a genuine decision (a scope ambiguity, an unforeseen risk, a missing input), surface that decision in the second update's *Required user action* slot directly; do not use it as a permission gate on the transition itself.

## Supporting rules

These specify what the template parts must contain. They are not numbered as the user-facing surface -- the template is the surface; these are implementation notes.

### Always name specific file paths

Every artifact mentioned gets its absolute or project-relative path. "I updated the plan" -> name the plan path. "I wrote a draft" -> name the draft path. "I edited the config" -> name the config path. "Review the bucket" -> name the bucket file.

The agent already knows the path; making the user remember which artifact is the current surface is exactly the burden this skill removes.

### Self-contained briefs (always, not just at boundaries)

Whenever the skill is invoked, treat every turn as if the user is reading cold -- the framework's `self-contained briefs` principle, applied per-turn. Do not say "as I mentioned earlier"; do not reference prior turns by index; do not assume the user remembers a decision from three turns ago. The plan, handoff, and ticket are the durable substrates; the live conversation is not.

If a system-reminder describes resumed state ("queued next concrete action is X"), verify against live state before acting on it. Stale carry-forward is common -- restate the actual next decision and explicitly flag the stale note rather than re-doing already-completed work.

### Auto mode means execute, not chase

Auto mode authorizes work without asking. It does NOT authorize spinning on already-done work, fabricating next steps when the real next step is a user decision, or doing extra work the user did not request. When the next decision is genuinely the user's (architectural choice, policy decision, audit verdict), surface it concisely in the *Required user action* slot and stop.

When the user asks for X, deliver X. Do not also re-issue Y "while I'm at it" -- that produces stacked-deliverable replies that violate the template's single-required-action rule.

### Capture provenance, not just outcomes

When recording a decision in a durable artifact (project plan, decision log, audit log), use the framework's `provenance triad`: surface / finding / follow-up. Outcomes alone are not enough; the next agent who picks up cold cannot reconstruct what surface revealed the smell. The project's plan / handoff / decision-log is the right home for these -- chat memory is not durable.

## Argument-based invocation modes

`/verbose-updates <arg>` switches from the default end-of-turn template to a focused report shape selected by the argument. Default invocation (no argument) continues to use the standard three-part template above. The framework's `argument-based invocation modes` rule applies: each mode names the question it answers, defines reply shape and length, and states whether it replaces or supplements the default.

Recognized arguments:

### `strategy` -- goal + trajectory check

Answers exactly three questions, in this order, as a single short reply:

1. **What is our current goal?** State the work-unit in one sentence -- the falsifiable thing we are trying to make true.
2. **Are we on track to hit that goal?** Yes / no / partially, with the evidence in one sentence (what's done vs what remains; any blockers).
3. **Are we doing what will most quickly help us achieve our goal?** Yes / no, with one sentence on the next-action choice -- if no, name the action that would be faster and why we are not doing it.

Keep the whole reply tight (3-6 sentences total). This is a sanity check the user is asking the agent to run on itself; verbose elaboration defeats the purpose. If any of the three answers is genuinely unclear, say so explicitly rather than padding.

Replaces the standard three-part end-of-turn template for this turn.

### Adding new argument modes

Per the framework's rule, each new mode must: (a) name the specific question the argument answers, (b) define the shape and length of the reply, (c) state whether it replaces or supplements the default template. Keep modes orthogonal to each other -- the user should never have to decide which of two overlapping modes to invoke.

## When NOT to apply

These rules are about reply shape, not about ignoring the user. Do not over-apply:

- If the user explicitly asks for a structured report, deliver one.
- Project-specific protocols (audit logs, surface/finding/follow-up shape, domain terminology) live in the project plan; this skill is the generic substrate they extend.
