---
name: recap
description: "Use when the user asks for a recap, returns from afk, or an afk or autonomous run ends: a recap fit to its trigger. Do NOT use to summarize documents or code."
argument-hint: "[return|afk-ending|autonomous-done <pid>]"
skill-type: technique-skill
---

# recap

A recap shaped by what triggered it. The model (presence, return, while away)
is `${CLAUDE_PLUGIN_ROOT}/references/presence.md`; this skill says what to
write for each trigger and records the away recaps so the Stop hook can see
them.

Arguments: $ARGUMENTS

The arguments are empty (a bare invocation) or `<trigger> <pid>`. The pid is the
prompt id that a presence-kit hook line or nudge names (`prompt <id>`); use it
exactly as given. Never invent one.

**A long recap is a failed recap.** A bare recap exists because the previous
turn was too long; reproducing it in miniature defeats the point. An away recap
is read cold by someone who saw none of the work: outcomes, not activity, in as
few lines as the content allows.

## Trigger rows

A bare invocation reads `state get`: presence afk selects a `return` row
(`greeted` picks which), presence present selects the first row.

| Trigger | Window | Phase 1 | Ends with |
|---|---|---|---|
| bare, user present | recent turns, widened if thin | "What got done", "Decisions I need from you" (text only) | nothing (not an away row) |
| `return`, `greeted` false | since `afk_since` | finished, decided for you, waiting on you, and still running when owned work runs | `presence.py recap-done return <pid>` |
| `return`, `greeted` true | -- | none; the standing recap is not repeated | `presence.py recap-done return <pid>` |
| `afk-ending` (nothing owned) | since `afk_since` | finished, decided for you, waiting on you; read cold | `presence.py recap-done afk-ending <pid>` |
| `autonomous-done` | since `goal_since` | outcome against `goal`, each load-bearing decision, waiting on you | `presence.py recap-done autonomous-done <pid>` |

One recap per ending: `autonomous-done` also closes the afk ending. A recap
without its `recap-done` call satisfies nothing; the call, not the prose, is
what the Stop hook checks.

```yaml
technique_skill:
  _schema_version: "1"
  identity: Write the recap that fits the trigger, record every away recap with recap-done, and interview only after a contentless reply.
  scope:
    covers:
      - a bare recap from a present user
      - the return recap, the afk ending recap and the autonomous-done recap
    excludes:
      - summarizing a document, a codebase, or a diff (that is ordinary summarization)
      - reflecting the user's stakes, preferences, or working agreements back at them
      - deciding the blocking questions on the user's behalf
  techniques:
    - id: recap_by_trigger
      name: Recap by trigger
      keywords: [recap, return recap, afk ending, autonomous done, what got done, pending decisions, neutral reply, batched interview]
      goal: The reader learns what finished, what was decided for them and what waits on them, in the shape the trigger calls for, and an away recap is recorded.
      arguments:
        - name: trigger
          required: false
          description: "`return`, `afk-ending` or `autonomous-done`; omit for a bare invocation."
        - name: pid
          required: false
          description: The prompt id the hook line or nudge names; required with a trigger.
      steps:
        - n: 1
          action: >-
            Read the configuration before anything else. An id that is off
            does not apply.
          tool: '"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLAUDE_PLUGIN_ROOT}/scripts/presence.py" config'
          expected: One JSON object with `behaviors`, `values` and `layers`.
          on_failure: >-
            An `{"error": "config", ...}` object: report its `message` once. If
            you invoked the skill yourself, end here with no state change. If
            the user typed the command, write the recap but skip the
            `recap-done` call, and say so in one line.
        - n: 2
          action: >-
            Only when you invoked the skill yourself: end without changing
            state when the row's id is off, which is `recap-on-return` for
            `return` and `ending-recap` for `afk-ending` and
            `autonomous-done`. The user's own `/presence-kit:recap` always
            runs.
        - n: 3
          action: >-
            Read the session state: `afk_since`, `goal`, `goal_since`,
            `greeted` and `unresolved` feed the window, the row and the
            "Waiting on you" section. A bare invocation takes its row from
            `presence` (see Trigger rows) and takes the pid from this turn's
            presence-kit hook line.
          tool: '"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLAUDE_PLUGIN_ROOT}/scripts/presence.py" state get'
          expected: '`{"exists": true|false, "state": {...}}`; `exists` false means the defaults (present, nothing recorded).'
        - n: 4
          action: >-
            Write Phase 1 for the row, over its window. "Waiting on you" lists
            every `unresolved` entry and every parked question. Write it before
            step 5, which clears `unresolved` on a return.
        - n: 5
          action: >-
            Every away row ends here, after the recap text: record it with the
            row's trigger and the pid, then carry on with the user's message.
            If no hook line gave a prompt id, do not invent one: skip this call
            and say so in one line.
          tool: '"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLAUDE_PLUGIN_ROOT}/scripts/presence.py" recap-done <trigger> <pid>'
          expected: '`{"recorded": "<trigger>", "prompt_id": "<pid>", "state": {...}}`.'
          on_failure: "An `{\"error\": ...}` reply means the recap is not recorded; report its `message` once."
        - n: 6
          action: >-
            If the next user message is a contentless reply, run Phase 2. This
            follows every row, `greeted` included.
      gotchas:
        - A return with `greeted` true gets no Phase 1 text, but still gets the `recap-done return <pid>` call and, after a contentless reply, Phase 2.
        - Recap-shaped prose without `recap-done` satisfies nothing; the Stop hook asks again.
  anti_patterns:
    - id: work_log_as_summary
      name: Answering with a blow-by-blow log
      keywords: [work log, activity log, every command, every file, altitude, too granular]
      why_it_seems_right: "Listing everything that was done feels like the complete and honest account."
      why_it_is_wrong: "The failure is altitude, not subject. A mechanical log of commands and files re-delivers the verbosity the recap exists to cut and buries the outcomes in it."
      alternative: "Summarize what was accomplished at the level a reader could act on: results and their state, not the steps that produced them."
    - id: interview_in_phase_1
      name: Reaching for AskUserQuestion in Phase 1
      keywords: [askuserquestion too early, tool instead of text, premature interview]
      why_it_seems_right: "Decisions are pending and there is a purpose-built tool for collecting them."
      why_it_is_wrong: "The reader asked to read. A tool call forces one-at-a-time structured picks and removes the option of answering three decisions in a single sentence of prose."
      alternative: "Name the decisions as numbered text; escalate to the tool only on a contentless reply."
```

## Two phases

### Phase 1 -- the recap: text only, NO AskUserQuestion

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

### Phase 2 -- on a neutral reply: interview

Trigger: the next user message is a bare acknowledgement carrying no content --
`ok`, `k`, `sure`, `fine`, `yeah`, `yep`, `right`, `mm`, `go on`, `continue`,
a lone emoji. Anything with substance is a real answer; treat it as one and
proceed with the work instead.

A neutral reply signals low engagement, so make answering nearly free:

- ONE AskUserQuestion call carrying every pending decision (max 4). Never drip
  one question per turn.
- Each question maps to a numbered decision from Phase 1 (or, after a `return`
  with `greeted` true, from the standing recap's "Waiting on you") -- same
  order, no new ones introduced at interview time.
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

The Phase 2 interview takes at most 4. An away row still lists every parked
question and every `unresolved` entry in "Waiting on you"; if more than four
are blocking, the work is under-scoped: interview the top four and say in one
line that the rest stay listed.
