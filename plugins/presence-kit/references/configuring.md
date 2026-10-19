# Configuring presence-kit

Every presence-kit behavior has an id and is on by default. You can switch any
behavior off, and change three values, in a small JSON file. Nothing else is
configurable. The model these settings control is in `presence.md` (same
directory).

To see what is in force right now, run:

    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" config

It prints the resolved `behaviors`, the resolved `values` and the three
`layers` with their paths and whether each file exists. Add `--project-dir DIR`
to resolve for another project (default: `CLAUDE_PROJECT_DIR`, else the current
directory).

## Layers

Three files, lowest first. An absent file is skipped, not an error.

| Layer | Path | Use |
|---|---|---|
| `defaults` | `defaults/presence-kit.json` inside the plugin | shipped; all behaviors on; do not edit |
| `user` | `~/.claude/config/presence-kit.json` (`$CLAUDE_CONFIG_DIR/config/presence-kit.json` when `CLAUDE_CONFIG_DIR` is set) | your own preferences, every project |
| `project` | `<project>/.claude/presence-kit.json` | a project's preferences; commit it to share |

Each file is checked alone, then merged: objects merge key by key and the later
layer wins; lists and single values replace. A project file can switch a
behavior back on that your user file switched off.

## Format

UTF-8 JSON with no byte-order mark. JSON has no comments, so a key that starts
with `$comment` is ignored at every level, and you can use it for notes.

    {
      "$comment": "quieter returns on this project",
      "behaviors": {"recap-on-return": false},
      "values": {"afk_quiet_minutes": 15}
    }

- Top-level keys: `behaviors` and `values` (both optional).
- A behavior takes `true`, `false`, `"on"` or `"off"` (spaces and case do not
  matter in the strings). `false` or `"off"` disables it; `true` or `"on"`
  re-enables one that a lower layer disabled.
- A value takes the type in the Values table.

Configuration never falls back: an error in any file is reported (see Errors),
presence behavior stays unavailable until the file is fixed, and no state
changes.

## Behaviors

All default to `true`. Off means the behavior does not apply.

| Id | Acts in | When on | When off |
|---|---|---|---|
| `afk-on-departure` | `presence-kit:afk` (the prompt-hook half is not in this release) | Claude recognizing you are leaving enters afk | Claude entering afk on its own is skipped; your own `/presence-kit:afk` still works |
| `afk-on-quiet` | Stop hook | No message of your own for `afk_quiet_minutes` enters afk | You stay present however long you are quiet |
| `autonomous-on-handover` | `presence-kit:autonomous` | An explicit hand-over enters autonomous | Claude does not enter autonomous on its own; your own `/presence-kit:autonomous` still works |
| `recap-on-return` | Stop hook, `presence.md` (the prompt-hook half is not in this release) | Your first message after afk gets the return recap | You become present with no return recap and no nudge |
| `waiting-guard` | Stop hook, `presence.md` | While owned work runs, non-agent jobs get a waiter agent and the turn ends with `Waiting on <what>` | No waiting check |
| `ending-recap` | Stop hook | An away turn with nothing running ends with one recap (in autonomy: or keeps working until the goal is done) | No ending recap is demanded |
| `terse-while-away` | `presence.md` | Interim messages are short while you are away | Messages keep normal length |
| `park-questions` | `presence.md` | Questions wait for the recap while independent work continues | Claude asks as it would when you are present |
| `notify-for-input` | `presence.md` | A push notification goes out only when your input is needed | The rule does not apply |

## Values

| Name | Type | Default | Accepted |
|---|---|---|---|
| `afk_quiet_minutes` | integer | `10` | a positive integer (not a boolean) |
| `departure_words` | list of strings | `["afk"]` | no effect in this release (read by the prompt hook, which arrives later); a non-empty list; each entry letters, digits or underscore only; matched as a whole word in any case |
| `waiter_model` | string | `"haiku"` | a non-empty model name without spaces, passed to the Agent tool; not checked against a list |

`departure_words` replaces the whole list: `["afk", "brb"]` keeps `afk`;
`["brb"]` drops it.

## Errors

An invalid file is a loud error that never blocks a prompt. The text is
`<file path>: <detail>`. Hooks show it to you as a message and change nothing;
`presence.py config` prints it as `{"error": "config", "kind": ..., "message":
...}` and exits 1. Fix the file named in the message. Kinds:

### unreadable
The file cannot be read or is not UTF-8. Detail: `cannot read -- <reason>`.

### malformed-json
The file is empty or not valid JSON. Detail: `malformed JSON -- <reason>`.

### root-not-object
The top level is not an object. Detail: `config root must be a JSON object, got <type>`.

### unknown-key
A top-level key other than `behaviors`, `values` or `$comment...`. Detail:
`unknown key '<key>'; allowed: behaviors, values`.

### section-not-object
`behaviors` or `values` is not an object. Detail: `'<key>' must be an object, got <type>`.

### unknown-behavior
A behavior id not in the Behaviors table. Detail: `unknown behavior id '<key>'; valid ids: <ids>`.

### bad-behavior-value
A behavior set to something other than `true`, `false`, `"on"` or `"off"`.
Detail: `behavior '<key>' accepts only true, false, "on" or "off"; got <value>`.

### unknown-value
A value name not in the Values table. Detail: `unknown value '<key>'; valid names: <names>`.

### bad-value-type
A value that fails its check. Detail: `value '<key>' must be <expected>; got <value>`, where
expected is `a positive integer` (`afk_quiet_minutes`), `a non-empty list of
words (letters, digits, underscore)` (`departure_words`) or `a non-empty model
name without spaces` (`waiter_model`).

### defaults-incomplete
The shipped defaults file lacks a behavior or value. Detail: `shipped defaults
must define <behavior|value> '<key>'`. This is a plugin defect, not yours;
reinstall or update presence-kit.

## Hook messages

Every hook line has the form `presence-kit [<ids>] prompt <prompt id>: <text>
See references/configuring.md.`, so it names the behavior ids that produced it
and where to switch them off. `config` is not a behavior: it marks a
configuration error. The Stop hook (`presence.py stop`) acts only in an attended
session. The UserPromptSubmit prompt hook (`presence.py prompt`) is not part of
this release; it arrives in a later one, and P1-P5 below are its messages, so
none is emitted yet.

| Code | Reaches | Ids | Text |
|---|---|---|---|
| P1 | Claude, while afk | `terse-while-away,park-questions,notify-for-input` | the user counts as away since `<afk_since>`, with ` [goal: <goal>]` when autonomous. Apply the away behaviors in references/presence.md that are on. |
| P2 | Claude, while afk | `recap-on-return` | if this prompt is the user's own message, not a scheduled task, agent report, task notification or another session's message, invoke `presence-kit:recap return <prompt id>` before answering. |
| P3 | Claude, while present | `afk-on-departure` | the prompt contains the departure word "`<word>`"; if this is the user saying they are leaving, invoke `presence-kit:afk`. |
| P4 | you | `config` | the error text, then: Presence behavior is unavailable until the file is fixed. |
| P5 | Claude | `config` | presence behavior is unavailable because its configuration is invalid; apply no presence-kit rule this turn. |
| S1 | you (Stop hook) | `config` | same as P4 |
| R | Claude, Stop nudge | `recap-on-return` | the user is back; invoke `presence-kit:recap return <prompt id>`. |
| C | Claude, Stop nudge | `waiting-guard` | add a waiter agent; the terminal shows idle. |
| W | Claude, Stop nudge | `waiting-guard` | end with `Waiting on <what>`. |
| E (autonomous) | Claude, Stop nudge | `ending-recap` | autonomous: if `<goal>` is done, invoke `presence-kit:recap autonomous-done <prompt id>`, else keep working. |
| E (afk) | Claude, Stop nudge | `ending-recap` | invoke `presence-kit:recap afk-ending <prompt id>`. |
| N1 | you, once per session | `afk-on-quiet,recap-on-return` | skipped quiet-afk and return detection: the transcript has no user entry for this prompt yet. |
| N2 | you, once per session | `afk-on-quiet,recap-on-return` | skipped quiet-afk and return detection: the prompt's transcript entry has no valid timestamp or turnOrigin. |
| N3 | you, once per session | `afk-on-quiet,recap-on-return` | skipped quiet-afk and return detection: the prompt's turnOrigin is not a known value. |
| N4 | you, once per session | `waiting-guard,ending-recap` | skipped waiting and ending checks: the Stop payload's background_tasks is missing. (or: ... is not a list of objects.) |
| N5 | you, once per session | `waiting-guard,ending-recap` | skipped owned-work checks: the Stop payload's session_crons is missing. (or: ... is not a list.) |
| S6 | you | the condition's id | unresolved after 3 nudges: `<label>`. Labels: `return recap not run`; `no waiter agent covering the job`; ``turn did not end with `Waiting on <what>` ``; `no ending recap or goal check`. |

The Stop hook nudges at most three times per prompt, with every unmet
condition in one nudge. After the third, it lets the turn end, shows S6, and
the unresolved items appear under "Waiting on you" in the next recap.
