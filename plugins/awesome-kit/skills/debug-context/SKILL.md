---
name: debug-context
skill-type: technique-skill
description: Use when the user asks why the agent did something while present in chat -- answer from context first, verify in the background. Do NOT use for ordinary work.
---

# debug-context

Answer the user's question about your own behavior NOW, from what is already
in context, with every load-bearing claim marked. Then verify in the
background and reconcile. The user is present; every foreground tool call
before the answer is time they are sitting there watching a spinner.

Presence (`user-present` / `user-afk`) is defined once, in the consumer's own
CLAUDE.md; this skill is the present-mode procedure for one question shape.
If no governing context defines presence, treat the user as user-present
unless they dismissed you or have gone quiet; otherwise treat them as
user-afk.

```yaml
technique_skill:
  _schema_version: "1"
  identity: Answer a question about the agent's own behavior immediately from context with premises marked, then verify in the background and reconcile.
  scope:
    covers:
      - "questions of the shape: why did you do X, why did that appear, what were you following -- about this session or a prior one"
      - reading the loaded skill text, the injected rules, and the session's own history as evidence
      - launching background gatherers for the follow-ups the answer makes likely
      - reconciling gathered evidence against the answer already given
    excludes:
      - implementation work the question leads to (route it normally once the cause is settled)
      - the user-afk case, where a foreground investigation costs nobody anything
      - debugging code the agent did not write (ordinary debugging)
  techniques:
    - id: answer_then_verify
      name: Answer from context, verify in the background
      keywords: [why did you, debug context, what were you following, which rule, which version, present, answer first, premise marked]
      goal: The user has a correct-as-far-as-known answer within one turn and a verified one shortly after, without losing the conversation to a tool-call stretch.
      steps:
        - n: 1
          action: Answer now, from context only. No tool calls before the answer.
          detail: |
            State the mechanism as you understand it. Mark each load-bearing claim
            `established:` (you can point at the text in context -- the skill body
            that was loaded, the rule that was injected, the message that said it)
            or `hypothesis:` (you are inferring it). The labels are the ones
            orchestrate's brief contract uses; do not grade confidence numerically.
            If you genuinely cannot answer without a read, say what one read would
            settle it and make that single call -- one, not a hunt.
        - n: 2
          action: Name the likely follow-ups and launch background gatherers for them, one line each.
          detail: |
            The answer usually implies two or three things the user will ask next:
            which version was actually loaded, what the current rule says, when it
            changed, what the prior session did. Launch a background agent (or a
            single backgrounded command) per gatherer and say, per line, what it
            will confirm or refute. Do not run these in the foreground; do not
            wait for them. End the turn.
        - n: 3
          action: When gatherers return, reconcile explicitly against the answer already given.
          detail: |
            For each marked claim report confirmed / refuted / untested. A refuted
            claim gets a one-line correction, not a re-derivation. Do not restate
            the parts that held.
        - n: 4
          action: End with what changed in your understanding, and hand the cause back to normal routing.
          detail: |
            If the cause implies a fix, that is ordinary work -- route it through
            orchestrate or do it inline by the usual rules; this skill's job ended
            when the cause was settled.
      gotchas:
        - The failure this skill exists to prevent is three or four foreground tool calls hunting for context before the user hears anything. If you notice yourself doing that, stop, answer with what you have, and move the hunt to step 2.
        - A version skew is the most common cause of "I followed a rule you removed" -- a skill body is injected once per session from the cache version at that moment, and a mid-session plugin update does not re-inject it. Check the base-directory line at the top of the loaded skill before assuming the rule you read is the current one.
        - Answering from context is not answering from memory. Quote the text in context that you followed; if you cannot point at it, that claim is a hypothesis.
```
