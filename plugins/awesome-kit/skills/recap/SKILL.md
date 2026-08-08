---
name: recap
description: Distill an over-long agent response into what the user cares about and which decisions are actually pending, as terse text with NO AskUserQuestion. If the user then answers neutrally ("ok", "sure", "k"), switch to an efficient batched AskUserQuestion interview over exactly those pending decisions. Do NOT use for summarizing a document, a codebase, or a work log.
---

# recap

The user just read a wall of text and hit `/recap`. That is a complaint, not a
request for more. Give them their own stakes back, plus the decisions blocking
progress, in as few lines as the content allows.

**A long `/recap` response is a failed `/recap` response.** The skill exists
because the previous turn was too long; reproducing it in miniature defeats the
point.

## Two phases

### Phase 1 -- on `/recap`: text only, NO AskUserQuestion

Emit exactly two sections. No preamble, no closing offer to help.

```
What you care about
- <their stake, one line each>

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

## What "what you care about" means

Their expressed stakes -- constraints they set, preferences they stated,
things they pushed back on, what they said they were optimizing for. Ordered
by how recently and how forcefully they said it.

It is NOT a summary of what you did. A work log is the failure mode here: it
reads as a status report, answers a question nobody asked, and is exactly the
verbosity that triggered the invocation.

Prefer their words over your paraphrase. If they said "keep it terse", write
`terse`, not `concise communication style`.

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
  identity: Distill an over-long agent response into the user's own stakes plus the genuinely blocking decisions, as terse text, escalating to a batched AskUserQuestion interview only when the user replies without content.
  scope:
    covers:
      - reducing a wall-of-text turn to the user's expressed stakes
      - naming which pending decisions are actually blocking, and which have defaults
      - escalating to one batched AskUserQuestion interview on a contentless reply
    excludes:
      - summarizing a document, a codebase, or a diff (that is ordinary summarization)
      - reporting what the agent did (a work log is the failure mode, not the goal)
      - deciding the blocking questions on the user's behalf
  techniques:
    - id: distill_and_escalate
      name: Distill, then interview only if disengaged
      keywords: [recap, tldr, too long, what do you need from me, distill, pending decisions, neutral reply, batched interview]
      goal: The user regains their own thread in a few lines and answers only what genuinely needs them.
      steps:
        - n: 1
          action: Emit "What you care about" -- their expressed stakes in their words, most recent and most forceful first.
        - n: 2
          action: Emit "Decisions I need from you" -- blocking decisions only, numbered, options named inline, max 4. NO AskUserQuestion in this phase.
        - n: 3
          action: Stop. No preamble, no offer of further help, no restatement of the work.
        - n: 4
          action: If the next reply is a bare acknowledgement, issue ONE AskUserQuestion carrying every pending decision, recommended option first.
        - n: 5
          action: If the next reply has substance, treat it as the answer and resume the work.
      gotchas:
        - A long /recap response is a failed one -- the invocation was a complaint about length.
        - Do not call AskUserQuestion in phase 1; the user asked to read, and prose lets them answer several things in one sentence.
        - Do not summarize your own work. Their stakes, not your activity.
        - Do not manufacture decisions to populate the section; "No decisions pending." is a valid and common result.
        - Do not re-ask something already settled earlier in the conversation.
        - Take obvious defaults rather than asking, but disclose each accepted default in keyword form so it can be corrected.
  anti_patterns:
    - id: work_log_as_summary
      name: Answering /recap with a status report
      keywords: [work log, status report, what I did, activity summary]
      why_it_seems_right: "Recapping the turn feels like the honest summary of what just happened."
      why_it_is_wrong: "The user asked what THEY care about and what is needed from them. A recap answers neither, and re-delivers the verbosity that triggered the invocation."
      alternative: "Lead with their expressed stakes in their own words; mention your work only where it changes a pending decision."
    - id: interview_on_first_invocation
      name: Reaching for AskUserQuestion in phase 1
      keywords: [askuserquestion too early, tool instead of text, premature interview]
      why_it_seems_right: "Decisions are pending and there is a purpose-built tool for collecting them."
      why_it_is_wrong: "The invocation asked to read. A tool call forces one-at-a-time structured picks and removes the option of answering three decisions in a single sentence of prose."
      alternative: "Name the decisions as numbered text; escalate to the tool only on a contentless reply."
```
