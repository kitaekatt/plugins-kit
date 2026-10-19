# presence-kit

Presence-kit decides how Claude behaves when you are at the keyboard, away
(afk), or have handed work over (autonomous): when it asks, how it waits for
background work, and what it tells you when you return. It is opinionated and
opt-in by installation. Every behavior has an id and can be switched off.

## What it does

- **Tracks presence.** You are present or afk. Afk starts when you say so (the
  word `afk` by default), when Claude recognizes you are leaving, or after ten
  minutes (by default) without a message of your own. Your first message after
  afk makes you present again. A scheduled task, agent report or another session's message is
  never counted as you.
- **Tracks authority.** Normal or autonomous. Autonomous starts only on an
  explicit hand-over, records the goal, and ends when you revoke it or the goal
  is done.
- **Recaps to fit the moment.** On your return, when an away turn ends with
  nothing running, and when an autonomous goal is done, you get a recap shaped
  for that trigger.
- **Guards waiting.** While background work runs, non-agent jobs get a waiter
  agent so the terminal shows work, and the turn ends with `Waiting on <what>`.
- **Keeps away turns quiet.** Short interim messages, questions parked for the
  recap, and a push notification only when your input is needed.

Slash commands:

| Command | Does |
|---|---|
| `/presence-kit:afk` | You are stepping away |
| `/presence-kit:autonomous` | Hand work over to finish without you |
| `/presence-kit:recap` | A recap now, or the one that fits the trigger |

Bare `/recap` stays Claude Code's built-in.

## Install

    /plugin marketplace add kitaekatt/plugins-kit
    /plugin install presence-kit

Or declare `{"ref": "plugins-kit:presence-kit"}` in the `plugins` array of
`~/.claude/bootstrap.json` to have bootstrap install it on every machine.

The plugin depends on `bootstrap` (requires 0.120.0 or later), which provisions
the Python interpreter the hooks and scripts run on; there is nothing else to
install. The hooks act only in an attended, interactive session.

## Configure

Every behavior is on by default. Turn any off, and change three values, in
`~/.claude/config/presence-kit.json` or `<project>/.claude/presence-kit.json`.
The layers, file format, every behavior id and value, the error messages and
the hook messages are in [references/configuring.md](references/configuring.md).

## How it works

[docs/how-it-works.html](docs/how-it-works.html) is the visual explainer. The
agent-facing model, which the skills and hooks defer to, is
[references/presence.md](references/presence.md).
