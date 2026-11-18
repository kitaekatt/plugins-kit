# Configuring presence-kit

Every presence-kit behavior has an id and is on by default. You can switch any
behavior off, and change four values, in a small JSON file. Nothing else is
configurable. The model these settings control is in `presence.md` (same
directory).

To see what is in force right now, run:

    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" config

It prints the resolved `behaviors`, the resolved `values` and the three
`layers` with their paths and whether each file exists, plus `ignored`: the
unknown behavior ids the user and project files set (see Format). Add `--project-dir DIR`
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
- A behavior id this plugin does not know, in your user or project file, is
  ignored (a file written for another version of the plugin must not switch
  presence off). It is listed under `ignored` by `presence.py config`, and one
  notice per session names it and the valid ids (N6). The shipped defaults file
  stays strict, and a bad value for a known id is still an error.

Configuration never falls back: any other error in a file is reported (see
Errors), presence behavior stays unavailable until the file is fixed, and no
state changes.

## Behaviors

All default to `true`. Off means the behavior does not apply.

| Id | Acts in | When on | When off |
|---|---|---|---|
| `afk-on-departure` | prompt hook, `presence-kit:afk` | A word from `departure_words` in your message sets you afk, and Claude is told so it can undo a wrong match; Claude recognizing you are leaving enters afk too | The hook ignores the words and Claude entering afk on its own is skipped; your own `/presence-kit:afk` still works |
| `afk-on-quiet` | Stop hook | No message of your own for `afk_quiet_minutes` enters afk | You stay present however long you are quiet |
| `autonomous-on-handover` | `presence-kit:autonomous` | An explicit hand-over enters autonomous | Claude does not enter autonomous on its own; your own `/presence-kit:autonomous` still works |
| `recap-on-return` | prompt hook, Stop hook, `presence.md` | Your first message after afk gets the return recap | You become present with no return recap and no nudge |
| `waiting-guard` | Stop hook, `presence.md` | While owned work runs, non-agent jobs get a waiter agent (`presence-kit:waiter`) so the terminal shows work; a task the harness armed itself, and a job a subagent started, are not owned work; the waiting nudge is said once per prompt | No waiting check; the way to stop the waiting talk |
| `liveness-watch` | Stop hook, `presence.md` | A running owned job (agent work included) with no running Monitor gets one nudge: arm `watch.py --task <id>`, or re-arm it when the Monitor ended while the job still runs; the nudge is said once per prompt | No watch check |
| `ending-recap` | Stop hook | An away turn with nothing running ends with one recap (in autonomy: or keeps working until the goal is done) | No ending recap is demanded |
| `terse-while-away` | `presence.md` | Interim messages are short while you are away | Messages keep normal length |
| `park-questions` | `presence.md` | Questions wait for the recap while independent work continues | Claude asks as it would when you are present |
| `notify-for-input` | `presence.md` | A push notification goes out only when your input is needed | The rule does not apply |

## Values

| Name | Type | Default | Accepted |
|---|---|---|---|
| `afk_quiet_minutes` | integer | `10` | a positive integer (not a boolean) |
| `departure_words` | list of strings | `["afk"]` | a non-empty list; each entry letters, digits or underscore only; read by the prompt hook, which matches an entry on its own in any case: "AFK." and "(afk)" match; a longer name such as `afk-on-departure` or `afk_since`, `presence-kit:afk`, a backticked `afk`, and a message starting with `/presence-kit:` or `<task-notification>` do not |
| `waiter_model` | string | `"haiku"` | a non-empty model name without spaces, passed to the Agent tool; not checked against a list |
| `progress_minutes` | integer | `0` | zero or a positive integer (not a boolean); the interval of the watcher's `PROGRESS` lines (`presence.md`, section Watcher), where `0` means no `PROGRESS` lines; the watcher's `--progress-minutes` flag overrides it |

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
A behavior id not in the Behaviors table, in the shipped defaults file only
(the user and project files ignore it, see Format). Detail: `unknown behavior id
'<key>'; valid ids: <ids>`.

### bad-behavior-value
A behavior set to something other than `true`, `false`, `"on"` or `"off"`.
Detail: `behavior '<key>' accepts only true, false, "on" or "off"; got <value>`.

### unknown-value
A value name not in the Values table. Detail: `unknown value '<key>'; valid names: <names>`.

### bad-value-type
A value that fails its check. Detail: `value '<key>' must be <expected>; got <value>`, where
expected is `a positive integer` (`afk_quiet_minutes`), `a non-negative integer (0 turns
PROGRESS lines off)` (`progress_minutes`), `a non-empty list of
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
configuration error. Both hooks act only in an attended session. The
UserPromptSubmit prompt hook (`presence.py prompt`) sends P1-P5 below; the Stop
hook (`presence.py stop`) sends S1, R, C, M, E, N1-N6 and S6. The prompt hook writes state only
when a departure word sets you afk (P3) or when it records the N6 notice; everything else it says
is read-only.

| Code | Reaches | Ids | Text |
|---|---|---|---|
| P1 | Claude, while afk (including right after P3), not on an automatic prompt | `terse-while-away,park-questions,notify-for-input` | the user counts as away since `<afk_since>`, with ` [goal: <goal>]` when autonomous. Apply the away behaviors in references/presence.md that are on. |
| P2 | Claude, while afk, except on the prompt P3 acted on and on an automatic prompt | `recap-on-return` | if this prompt is the user's own message, not a scheduled task, agent report, task notification or another session's message, invoke `presence-kit:recap return <prompt id>` before answering. |
| P3 | Claude, when the hook acted on a departure word | `afk-on-departure` | set the user afk because the prompt contains the departure word "`<word>`". If that is wrong (the word is quoted or discussed, or the prompt is not the user's own message), run `<state set present command>` and tell the user once in one line; otherwise invoke `presence-kit:afk` to cover running work. When you were already afk the text begins "kept the user afk" instead, and with `recap-on-return` on adds that, if the wrong match was your own message, the return recap is due. P3 comes first, then P1. |
| P4 | you | `config` | the error text, then: Presence behavior is unavailable until the file is fixed. |
| P5 | Claude | `config` | presence behavior is unavailable because its configuration is invalid; apply no presence-kit rule this turn. |
| S1 | you (Stop hook) | `config` | same as P4 |
| R | Claude, Stop nudge | `recap-on-return` | the user is back; invoke `presence-kit:recap return <prompt id>`. |
| C | Claude, Stop nudge | `waiting-guard` | add a waiter agent; the terminal shows idle. Counted as running non-agent work: `<id> (<type>, <status>): <description>` per task. |
| M | Claude, Stop nudge | `liveness-watch` | arm a liveness watch: a Monitor on `watch.py --task <id>` for each job you own (it reports a stall and the job's end). Then `No liveness watch on: <id (type, status): description per job>.` and/or `Liveness watch ended while the job still runs, re-arm it on: <jobs>.` |
| E (autonomous) | Claude, Stop nudge | `ending-recap` | autonomous: if `<goal>` is done, invoke `presence-kit:recap autonomous-done <prompt id>`, else keep working. |
| E (afk) | Claude, Stop nudge | `ending-recap` | invoke `presence-kit:recap afk-ending <prompt id>`. |
| N1 | you, once per session | `afk-on-quiet,recap-on-return` | skipped quiet-afk and return detection: the transcript has no user entry for this prompt yet. |
| N2 | you, once per session | `afk-on-quiet,recap-on-return` | skipped quiet-afk and return detection: the prompt's transcript entry has no valid timestamp or turnOrigin. |
| N3 | you, once per session | `afk-on-quiet,recap-on-return` | skipped quiet-afk and return detection: the prompt's turnOrigin is not a known value. |
| N4 | you, once per session | `waiting-guard,ending-recap` | skipped waiting and ending checks: the Stop payload's background_tasks is missing. (or: ... is not a list of objects.) |
| N5 | you, once per session | `waiting-guard,ending-recap` | skipped owned-work checks: the Stop payload's session_crons is missing. (or: ... is not a list.) |
| N6 | you, once per session (from whichever hook runs first) | `config` | ignored unknown behavior id `'<key>'` in `<file>`; valid ids: `<ids>`. |
| S6 | you | the condition's id | unresolved after 3 nudges: `<label>`. Labels: `return recap not run`; `no waiter agent covering the job`; `no running liveness watch on an owned job`; `no ending recap or goal check`. |

An automatic prompt is a prompt the user did not type: one that starts with
`/presence-kit:`, `<task-notification>` or `<agent-message from=`, or that
exactly equals a live session cron's or `/loop`'s prompt. The prompt hook reads
no departure word in it and says neither P1 nor P2 on it.

Owned work, for C and E, is every listed background task that has not
finished and every session cron. Two kinds of listed task are not owned work.
A task whose `status` is `completed`, `failed` or `killed` is finished: the
harness keeps listing it until it notifies and evicts it, and a finished agent
does not cover a running job either. A `monitor` task whose description starts
with `live updates for artifact ` is an Artifact live subscription the harness
armed (on publish, on attach, on resume or on a watch request); the agent
cannot usefully stop it. Any other status counts, including `paused`,
`pending`, a missing status and an unrecognized one, and a monitor with any
other description counts. The C nudge lists the tasks it counted
(description cut to about 80 characters), so a wrongly counted task can be
named.

The Stop hook nudges at most three times per prompt, with every unmet
condition in one nudge. After the third, it lets the turn end, shows S6, and
the unresolved items appear under "Waiting on you" in the next recap.
