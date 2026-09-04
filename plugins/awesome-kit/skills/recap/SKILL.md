---
name: recap
description: Use when the user hits /recap -- what got done plus blocking decisions, terse text, NO AskUserQuestion. Do NOT use to summarize a document or codebase.
---

# recap

The user just read a wall of text and hit `/recap`. That is a complaint, not a
request for more. Tell them what got done, at an appropriate level of detail,
plus the decisions blocking progress, in as few lines as the content allows.

**A long `/recap` response is a failed `/recap` response.** The skill exists
because the previous turn was too long; reproducing it in miniature defeats the
point.

## Two phases

### Phase 1 -- on `/recap`: text only, NO AskUserQuestion

Emit exactly two sections. No preamble, no closing offer to help.

```
What got done
- <accomplishment, one line each>

Decisions I need from you
1. <question> -- <option A> / <option B>   (rec: <A>)
2. ...
```

Do NOT call AskUserQuestion in this phase, even though the output names
decisions. The user asked to READ first. Interviewing them here removes the
option of answering three things in one sentence of prose, which is what an
engaged user wants and what the tool is worst at.

If nothing is genuinely pending: say `No decisions pending.` on one line and
stop. Do not manufacture a decision to fill the section.

### Phase 2 -- on a neutral reply: interview

Trigger: the next user message is a bare acknowledgement carrying no content --
`ok`, `k`, `sure`, `fine`, `yeah`, `yep`, `right`, `mm`, `go on`, `continue`,
a lone emoji. Anything with substance is a real answer; treat it as one and
proceed with the work instead.

A neutral reply signals low engagement, so make answering nearly free:

- ONE AskUserQuestion call carrying every pending decision (max 4). Never drip
  one question per turn.
- Each question maps to a numbered decision from phase 1 -- same order, no new
  ones introduced at interview time.
- Concrete options, not "how would you like to proceed". Recommended option
  first, labelled `(Recommended)`.
- Options state consequences, not restatements of the label.

## What "what got done" means

A summary of what has been accomplished -- outcomes, not activity. It is NOT
the user's stakes, preferences, or working agreements reflected back at them;
they asked what happened, not what they said.

Pitch the detail between two failures: a blow-by-blow log (every command,
every file) and a summary so high it says nothing. Test: every line names a
result the reader could act on; cut any that would not change their next move.

If little has happened recently, summarize back further -- widen the window
to cover more of the session rather than padding a thin slice.

## Selecting decisions

A decision earns a slot only if it is BLOCKING -- different answers produce
materially different work, and you cannot pick a defensible default yourself.

Everything else is not a decision:

- resolvable from the codebase, the conversation, or a convention -> resolve it
- has an obvious default -> take it, and disclose it in one keyword-form line
  (`defaults: depth=basic, scope=subtree`) so it can be corrected
- already decided earlier in the conversation -> do not re-ask; re-asking a
  settled question reads as not having listened

Cap at 4. More than four blocking decisions means the work is under-scoped;
name the top four and say so in one line.

```yaml
technique_skill:
  _schema_version: "1"
  identity: Distill an over-long agent response into what was accomplished, at an appropriate level of detail, plus the genuinely blocking decisions, as terse text, escalating to a batched AskUserQuestion interview only when the user replies without content.
  scope:
    covers:
      - reducing a wall-of-text turn to what was accomplished, at an appropriate level of detail
      - naming which pending decisions are actually blocking, and which have defaults
      - escalating to one batched AskUserQuestion interview on a contentless reply
    excludes:
      - summarizing a document, a codebase, or a diff (that is ordinary summarization)
      - reflecting the user's stakes, preferences, or working agreements back at them
      - deciding the blocking questions on the user's behalf
  techniques:
    - id: distill_and_escalate
      name: Distill, then interview only if disengaged
      keywords: [recap, tldr, too long, what do you need from me, distill, pending decisions, neutral reply, batched interview]
      goal: The user learns what got done in a few lines and answers only what genuinely needs them.
      steps:
        - n: 1
          action: Emit "What got done" -- accomplishments at an appropriate level of detail, outcomes not activity; if the recent slice is thin, widen the window.
        - n: 2
          action: Emit "Decisions I need from you" -- blocking decisions only, numbered, options named inline, max 4. NO AskUserQuestion in this phase.
        - n: 3
          action: Stop. No preamble, no offer of further help.
        - n: 4
          action: If the next reply is a bare acknowledgement, issue ONE AskUserQuestion carrying every pending decision, recommended option first.
        - n: 5
          action: If the next reply has substance, treat it as the answer and resume the work.
      gotchas:
        - A long /recap response is a failed one -- the invocation was a complaint about length.
        - Do not call AskUserQuestion in phase 1; the user asked to read, and prose lets them answer several things in one sentence.
        - Do not reflect the user's stated preferences or constraints back at them; they asked what happened, not what they said.
        - Do not manufacture decisions to populate the section; "No decisions pending." is a valid and common result.
        - Do not re-ask something already settled earlier in the conversation.
        - Take obvious defaults rather than asking, but disclose each accepted default in keyword form so it can be corrected.
  anti_patterns:
    - id: work_log_as_summary
      name: Answering /recap with a blow-by-blow log
      keywords: [work log, activity log, every command, every file, altitude, too granular]
      why_it_seems_right: "Listing everything that was done feels like the complete and honest account."
      why_it_is_wrong: "The failure is altitude, not subject. A mechanical log of commands and files re-delivers the verbosity that triggered the invocation and buries the outcomes in it."
      alternative: "Summarize what was accomplished at the level a reader could act on: results and their state, not the steps that produced them."
    - id: interview_on_first_invocation
      name: Reaching for AskUserQuestion in phase 1
      keywords: [askuserquestion too early, tool instead of text, premature interview]
      why_it_seems_right: "Decisions are pending and there is a purpose-built tool for collecting them."
      why_it_is_wrong: "The invocation asked to read. A tool call forces one-at-a-time structured picks and removes the option of answering three decisions in a single sentence of prose."
      alternative: "Name the decisions as numbered text; escalate to the tool only on a contentless reply."
```
