# Presence model

How Claude behaves when the user is present, away (afk) or has handed work
over (autonomous). This file is the single source of the model; the skills
`presence-kit:afk`, `presence-kit:autonomous` and `presence-kit:recap` and the
hooks defer to it. Every behavior below has an id and can be switched off; the
catalog, defaults and file format are in `configuring.md` (same directory).

## Before applying anything

Run this first, in the same turn. It prints the resolved configuration as JSON
(`behaviors`: id to bool; `values`: `afk_quiet_minutes`, `departure_words`,
`waiter_model`, `progress_minutes`):

    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" config

A behavior id that is `false` does not apply: skip the rule that names it. If
the command prints an `{"error": ...}` object, tell the user its message (it
names the file and key) and apply no presence rule this turn.

Hook lines read `presence-kit [<ids>] prompt <prompt id>: ...`. They come from
this plugin's two hooks (`hooks/hooks.json`): the UserPromptSubmit prompt hook
(`scripts/presence.py prompt`) and the Stop hook (`scripts/presence.py stop`),
and each line names the behavior ids that produced it. The prompt hook speaks
before you answer: while the user is afk it says so (the away line, plus the
instruction to give the return recap when `recap-on-return` is on), and when
the prompt contains a word from `departure_words` it sets the user afk itself
and tells you so (section Presence). The Stop hook detects a return at the end
of the turn and records it (section Return).

## Presence

The user is `present` or `afk`.

- Afk starts when the user declares it or when no message of the user's own
  arrives for `afk_quiet_minutes` (`afk-on-quiet`, decided by the Stop hook).
  The user declares it (`afk-on-departure`) by typing `/presence-kit:afk`, by a
  word from `departure_words`, or by leaving in words you recognize as leaving
  (the backup when no listed word was used; you invoke `presence-kit:afk`).
- A listed word is acted on by the prompt hook, before you answer: it matches
  the word on its own in any case, so "AFK." and "(afk)" count while
  `afk-on-departure`, `presence-kit:afk`, `afk_since` and a backticked `afk` do
  not. It writes the afk state itself and tells you in a line that begins "set
  the user afk". It does no inference beyond the word, so a prompt that quotes
  or discusses the word, or an automatic prompt, can set afk by mistake. That
  line gives you the correction: when the user is not leaving or the prompt is
  not the user's own message, run `state set present` (section Commands) and
  tell the user once in one line. The hook skips a prompt that starts with
  `/presence-kit:`, `<task-notification>` or `<agent-message from=` (a subagent
  report or another session's message). It also skips a prompt that exactly
  equals (after trimming) the prompt of a live session cron or `/loop`, which
  the Stop hook records after each turn. Two gaps remain: a cron created and
  fired before any Stop is not recorded yet, and a user typing a cron's exact
  prompt is treated as automatic. Any other automatic prompt carries no marker
  the hook can read, so your correction is the safeguard there. A skip says
  nothing about who sent the prompt.
- Already afk, a prompt with a listed word keeps the user afk: the line begins
  "kept the user afk" and no return recap is asked for that prompt. If that is
  wrong, because the user is back and only mentions the word, undo it the same
  way (and, with `recap-on-return` on, the line also tells you to give the
  return recap).
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
no return recap runs. The prompt that set (or kept) the user afk is not a return.

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
agent (section Waiter agent) plus a Monitor on the shipped watcher (section
Watcher) for progress. While owned work runs
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

## Watcher

`scripts/watch.py` is the Monitor's command for a non-agent job. Arm a Monitor
on it instead of writing a watcher script of your own: its cadence is fixed, so
the user is not flooded with progress lines. It prints one line per event and
runs until its done condition holds or it is killed:

    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/watch.py" --path <file-or-dir> [--path ...] [--done-output <task output file> | --done-file <path> --done-pattern <regex>]

Every `--poll-seconds` (default 300) it takes the newest modification time
across the `--path` values: a file's own mtime, or the newest mtime of any file
under a directory (`.git` directories are skipped). A missing path counts for
nothing. The done file is watched too. `--stall-minutes` (default 20) and
`--progress-minutes` (default: the `progress_minutes` value, 20) change the
cadence for one run. It never writes a file.

| Line | Meaning |
|---|---|
| `PROGRESS <elapsed>m: last change <age>m ago in <path>` | every `--progress-minutes`, changed or not; `no watched path exists yet` replaces the tail while no watched path exists |
| `STALL: no change in watched paths for <N>m` | the newest mtime is at least `--stall-minutes` old; printed once per quiet episode, re-armed by any newer mtime; the watcher keeps running |
| `DONE: <line>` | `--done-output`: the last non-empty line is `[exited with code N]`; `--done-file`: the pattern matches a line; the watcher then exits 0 |

A `STALL` line reports a fact and prescribes nothing: what to do about the job
is your decision. An invalid configuration or bad arguments print to stderr and
exit 2.

## Commands

All take `--session ID`; the default is `CLAUDE_CODE_SESSION_ID`. Each prints one
JSON object. A `{"skipped": ...}` reply means stop: change nothing and tell the
user once. `--by user` only when the user typed `/presence-kit:afk` or
`/presence-kit:autonomous` themselves; otherwise `--by model`, which is skipped
when the matching id is off. `state set normal` and `state set present` take no
`--by`.

    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" config
    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" state get
    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" state set afk --by user|model
    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" state set autonomous --by user|model --goal "<granted goal>"
    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" state set normal
    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" state set present
    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" recap-done return|afk-ending|autonomous-done <prompt id>

`state set afk` is governed by `afk-on-departure`, `state set autonomous` by
`autonomous-on-handover`. `state set normal` revokes authority and leaves
presence unchanged. `state set present` undoes afk: presence is present, the
pending return and the hook's prompt record are cleared, `afk_since` is kept,
and no behavior id gates it; it is the correction for a wrong departure match
and leaves authority alone. `recap-done` is what the Stop hook checks after a recap:
recap-shaped prose without it satisfies nothing.
