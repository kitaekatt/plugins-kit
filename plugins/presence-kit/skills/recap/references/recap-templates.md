# Recap templates and content rules

## Phase 1 -- the recap: text only, NO AskUserQuestion

Emit the sections the row calls for and nothing else. No preamble, no closing
offer to help.

Bare, user present:

```
What got done
- <accomplishment, one line each>

Decisions I need from you
1. <question> -- <option A> / <option B>   (rec: <A>)
2. ...
```

`return` and `afk-ending`:

```
Finished
- <result, one line each>

Decided for you
- <call you made while they were away> -- <what you chose>

Waiting on you
1. <question> -- <option A> / <option B>   (rec: <A>)

Still running
- <job> -- <what it waits on>
```

"Still running" appears only on a `return` while owned work runs. An
`afk-ending` has nothing owned, so it has no such section. Write every line so
a reader who saw none of the work understands it.

`autonomous-done`:

```
Outcome (goal: <the stored goal>)
- <what was done against the goal, one line each>

Decisions I made
- <each load-bearing decision> -- <why>

Waiting on you
1. <question> -- <option A> / <option B>   (rec: <A>)
```

Do NOT call AskUserQuestion in this phase, even though the output names
decisions. The reader asked to READ first. Interviewing them here removes the
option of answering three things in one sentence of prose, which is what an
engaged user wants and what the tool is worst at.

If nothing is genuinely pending: say `No decisions pending.` on one line and
stop. Do not manufacture a decision to fill the section.

## What "what got done" means

A summary of what has been accomplished -- outcomes, not activity. It is NOT
the user's stakes, preferences, or working agreements reflected back at them;
they asked what happened, not what they said.

Pitch the detail between two failures: a blow-by-blow log (every command,
every file) and a summary so high it says nothing. Test: every line names a
result the reader could act on; cut any that would not change their next move.

If little has happened in the window, summarize back further -- widen the
window to cover more of the session rather than padding a thin slice.

## Selecting decisions

A decision earns a slot only if it is BLOCKING -- different answers produce
materially different work, and you cannot pick a defensible default yourself.

Everything else is not a decision:

- resolvable from the codebase, the conversation, or a convention -> resolve it
- has an obvious default -> take it, and disclose it in one keyword-form line
  (`defaults: depth=basic, scope=subtree`) so it can be corrected
- already decided earlier in the conversation -> do not re-ask; re-asking a
  settled question reads as not having listened

An away row still lists every parked question and every `unresolved` entry in
"Waiting on you".

## Anti-patterns

- **Answering with a blow-by-blow log.** Listing everything that was done feels
  like the complete and honest account. The failure is altitude, not subject:
  a mechanical log of commands and files re-delivers the verbosity the recap
  exists to cut and buries the outcomes in it. Summarize what was accomplished
  at the level a reader could act on: results and their state, not the steps
  that produced them.
- **Reaching for AskUserQuestion in Phase 1.** Decisions are pending and there
  is a purpose-built tool for collecting them, but the reader asked to read. A
  tool call forces one-at-a-time structured picks and removes the option of
  answering three decisions in a single sentence of prose. Name the decisions
  as numbered text; escalate to the tool only on a contentless reply.
