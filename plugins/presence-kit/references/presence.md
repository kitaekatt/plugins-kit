# Presence model

How Claude behaves when the user is present, away (afk) or has handed work
over (autonomous). This file is the single source of the model; the skills
`presence-kit:afk`, `presence-kit:autonomous` and `presence-kit:recap` and the
hooks defer to it. Every behavior below has an id and can be switched off; the
catalog, defaults and file format are in `configuring.md` (same directory).

## Before applying anything

Run this first, in the same turn. It prints the resolved configuration as JSON
(`behaviors`: id to bool; `values`: `afk_quiet_minutes`, `departure_words`,
`waiter_model`):

    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" config

A behavior id that is `false` does not apply: skip the rule that names it. If
the command prints an `{"error": ...}` object, tell the user its message (it
names the file and key) and apply no presence rule this turn.

Hook lines read `presence-kit [<ids>] prompt <prompt id>: ...`. They come from
this plugin's Stop hook (`hooks/hooks.json`, `scripts/presence.py stop`), and
each names the behavior ids that produced it. The UserPromptSubmit prompt hook
is not part of this release; it arrives in a later one. Until then
`departure_words` and the prompt-hook half of `afk-on-departure` and
`recap-on-return` have no effect, and no hook line announces a departure word
or a return before you answer.

## Presence

The user is `present` or `afk`.

- Afk starts when the user declares it (`afk-on-departure`: you recognize they
  are leaving, or they type `/presence-kit:afk`; no hook acts on a word from
  `departure_words` in this release) or when no message of
  the user's own arrives for `afk_quiet_minutes` (`afk-on-quiet`, decided by the
  Stop hook).
- A scheduled task, agent report, task notification or another session's
  message is not the user. You classify who sent a prompt; a hook line only
  suggests it.
- "go" authorizes work. It never means afk.
- The stored state is in `state get` (section Commands): `presence`,
  `afk_since`, `goal`, `goal_since`.

## Authority

Authority is `normal` or `autonomous`.

- Autonomous starts only on an explicit hand-over (`autonomous-on-handover`) or
  when the user types `/presence-kit:autonomous`. `/loop`, routines, `claude -p`
  and `--bg` never start it.
- It records the granted goal in state (`goal`, `goal_since`) and implies afk.
- It ends on the user's revocation (`state set normal`) or when the goal is
  done (`presence-kit:recap autonomous-done <prompt id>`).
- It grants only the stated goal. Autonomous mode changes no permission prompt,
  auto-mode decision, publication gate or prohibition.

## Return

A message of the user's own while afk always makes them present; the Stop hook
records it. With `recap-on-return` on, give the return recap before you answer
when you can tell the message is the user's own: invoke
`presence-kit:recap return <prompt id>` with the id a hook nudge gave. When the
Stop hook finds no return recap for the prompt, its nudge asks for one. With no
id in view, invoke `presence-kit:recap` bare; it reads state. With `recap-on-return` off, the user is present again and
no return recap runs.

## Modes

| Mode | You decide | You ask the user |
|---|---|---|
| Any | implementation, routing, verification, in-scope fixes, git the user has authorized, cheap-to-reverse calls | -- |
| Present | as above | decisions the user has claimed, product purpose, costly ambiguity about intent |
| Afk | as present | the same questions, parked for the recap; independent work continues |
| Autonomous | everything not gated; each load-bearing call gets a line in the recap | nothing beyond what a gate requires |
| User only | never: permissions, auto mode, publication gates and standing prohibitions hold in every mode | -- |

Where `awesome-kit:orchestrate` is installed, its consultation policy owns
seat routing for a load-bearing call; this plugin does not change that policy.

## Waiting rule

`waiting-guard`, all modes. Agent and workflow work shows "working" by itself.
Non-agent work (a shell job, a Codex CLI job, an external job) needs a waiter
agent (section Waiter agent) plus a Monitor for progress. While owned work runs
or a session cron is pending, end the turn with the last line
`Waiting on <what>`, where `<what>` names the work. Never hold the main session
with a foreground wait. The Stop hook checks this and nudges at most three
times per prompt. With `waiting-guard` off, none of this applies.

## While away

While the state is afk, and each only when its id is on:

- `terse-while-away`: keep interim messages short; nobody reads them until the
  user returns. Off: write messages at normal length.
- `park-questions`: do not ask mid-run; park the question for the recap and
  continue independent work. Off: ask as you would when present.
- `notify-for-input`: send a push notification only when the user's input is
  needed. Off: this rule does not apply.

Nothing is dropped: the recap carries every result, decision, question and
running job.

## Waiter agent

Covers one non-agent job. Start a background Agent whose model is the
`waiter_model` value from `presence.py config`. Its brief names the job and the
`--stall-minutes` you derive from it (the longest quiet period the job can
show). The agent runs this until exit 0, 2 or 3, reports one line, and changes
nothing; its completion wakes the main session:

    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/wait.py" --output <task output file> --stall-minutes <n>

Use `--file <path> --pattern <regex>` instead of `--output` to wait for a
pattern in a file. `--slice-seconds <s>` defaults to 540.

| Exit | Meaning |
|---|---|
| 0 | done: prints the `[exited with code N]` trailer or the match |
| 3 | stalled: no size or mtime change for n minutes |
| 4 | slice over, job still live: run the command again |
| 2 | bad arguments, or no file within the stall window |

## Commands

All take `--session ID`; the default is `CLAUDE_CODE_SESSION_ID`. Each prints one
JSON object. A `{"skipped": ...}` reply means stop: change nothing and tell the
user once. `--by user` only when the user typed `/presence-kit:afk` or
`/presence-kit:autonomous` themselves; otherwise `--by model`, which is skipped
when the matching id is off.

    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" config
    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" state get
    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" state set afk --by user|model
    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" state set autonomous --by user|model --goal "<granted goal>"
    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" state set normal
    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" recap-done return|afk-ending|autonomous-done <prompt id>

`state set afk` is governed by `afk-on-departure`, `state set autonomous` by
`autonomous-on-handover`. `state set normal` revokes authority and leaves
presence unchanged. `recap-done` is what the Stop hook checks after a recap:
recap-shaped prose without it satisfies nothing.
