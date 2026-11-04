# relaunch-kit

Restart Claude Code for real. `clauder` runs Claude Code in your terminal and
starts it again in the same terminal when a session asks to be restarted. The
`/restart` skill is how a session asks.

Plain `claude` is never wrapped or changed. A session that was not started
through `clauder` cannot restart itself; it does every other step of `/restart`
and tells you what it skipped.

## Install

```
/plugin marketplace add kitaekatt/plugins-kit
/plugin install relaunch-kit
```

The `bootstrap` plugin copies the launcher into the plugin data directory and
adds that directory's `bin` to your PATH. No shell function, profile edit or
execution-policy change is involved:

| Shell | What finds `clauder` |
|---|---|
| zsh, bash, Git Bash | the extensionless `clauder` script, through PATH |
| PowerShell 5.1, PowerShell 7, cmd | `clauder.cmd`, through PATH and PATHEXT |

On macOS bootstrap adds an `export PATH=` line to `.zshrc` and `.bashrc`. On
Windows it prepends the directory to your user Path. A terminal that was open
before the first install needs a new tab to see the change.

## The `clauder` command

```
clauder [claude flags] [prompt]
```

`clauder` takes the same arguments as `claude`. It finds the real `claude` on
PATH, skipping its own directory, and runs it with your terminal attached. When
the session ends, `clauder` looks for a restart request from that session:

- No request: `clauder` exits with claude's exit code.
- A request to resume: it starts `claude` again with your flags and
  `--resume <session id>`.
- A request to hand off: it starts a new `claude` session with your flags and
  the continuation prompt the session supplied.

The relaunch keeps every flag you gave except the session selectors (`-c`,
`--continue`, `-r`, `--resume`, `--session-id`, `--fork-session`, `--from-pr`)
and the positional prompt, which the relaunch replaces. `clauder` prints one
line per relaunch, for example
`[clauder] relaunching (hand-off): /task work dev/tasks/x`.

`clauder` starts `claude` once and does not loop when:

- stdin or stdout is not a terminal;
- you pass `-p`/`--print`, `--bg`/`--background`, `--desktop`, `--cloud`, `-h`
  or `-v`;
- you run a claude subcommand.

A loop that restarts in under 20 seconds four times in a row stops with a
message that names the cause. Set `RELAUNCH_KIT_CLAUDE` to an absolute path to
run a specific `claude` binary.

## `clauder --relay`

`clauder --relay [claude flags]` starts the session with the llm-kit relay
channel loaded, so messages pushed through the relay reach it and it can reply
without a permission dialog. The option is clauder's own: it is removed from the
command line before `claude` sees it, and every relaunch adds the same flags
and variables again. It is not in `claude --help`.

It adds these to the `claude` command line:

```
--mcp-config <llm-kit>/channel/mcp.json
--dangerously-load-development-channels server:relay
--allowedTools mcp__relay__reply
```

and these to its environment, which `mcp.json` expands: `BOOTSTRAP_PYTHON` (your
value when set, else the interpreter running `clauder`) and
`LLM_KIT_CHANNEL_SCRIPT` (the absolute path of `relay_channel.py`).

This needs the `llm-kit` plugin and is refused without it: `clauder` exits with
status 2 and starts nothing, and the message tells you whether llm-kit is not
installed (`claude plugin install llm-kit@plugins-kit`) or too old to ship the
channel (`claude plugin update llm-kit@plugins-kit`). relaunch-kit does not
depend on llm-kit; only this option does. `clauder` finds llm-kit in this order:

1. `LLM_KIT_CHANNEL_SCRIPT`, when you set it to an absolute path of a
   `relay_channel.py` (its `mcp.json` is read beside it).
2. Under `claudx`, the llm-kit working copy the session loaded
   (`CLAUDE_PLUGIN_TEST_DIRS`). The plugin cache is the installed release there,
   so it is never used; start `claudx --all` to load llm-kit.
3. The newest llm-kit version in `~/.claude/plugins/cache/plugins-kit/llm-kit/`.

`--relay` needs an interactive session. It is refused, not passed on, with
`-p`, `--bg` and the other modes listed above, with a claude subcommand, and
when stdin or stdout is not a terminal.

Claude Code asks you to confirm loading a development channel on every launch,
including each relaunch. A relaunch under `--relay` therefore waits at that
dialog until it is answered.

The restart skill (`skills/restart/SKILL.md`, technique `relay_launch`) carries
the same facts for the session, so it can explain a refusal or a waiting
relaunch.

## `/restart` and `/restart hand-off`

`/restart` stops new work, lets running background jobs finish, and checks that
nothing blocks a restart. Then it asks the session's `request_restart` tool
for a relaunch.

| Command | Result |
|---|---|
| `/restart` | This session exits and `clauder` resumes the same session by id. It waits for you; no continuation prompt is sent. Use it after a plugin or bootstrap change. |
| `/restart hand-off [<task ref>]` | The session first runs the task-kit hand-off steps for the task, then exits. `clauder` starts a new session with `/task work <ref>`. |

The exit comes about 3 seconds after the turn ends. Typing anything, or an
interrupted turn, cancels it. The session does not call the tool, and so
blocks the restart, when a job is still running, a hand-off step failed, a
question for you is pending, or you asked for a manual restart. In each case
it names the reason and says "Ready to Restart" or "Ready to Hand Off".

`/relaunch-check` exercises automatic restarts in a row with no model call.
Run it under `clauder` to confirm the whole path works in your terminal.
Under plain `claude` it reports `no-launcher`.

## Under plain `claude`

A session started with `claude` has no launcher to restart it. `/restart` still
does every other step: it stops work, runs the hand-off steps when you asked
for a hand-off, and prints the baton (the working directory and the
`/task work <ref>` line). It then reports what it skipped and why, with the
command to run next, for example `claude --resume <id>`. It copies nothing to
the clipboard and fills no prompt.

## Windows notes

- **cmd quote refusal.** `clauder.cmd` hands your arguments to a launcher that
  re-reads the raw command line. It refuses, and names the argument, when cmd
  and the program parser would read it differently: a double quote inside an
  argument, an unterminated quote, or a backslash run before a closing quote.
  When `claude` itself is a `.cmd` or `.bat` file (an npm install), an argument
  that contains `"`, `%`, a carriage return, a line feed or a NUL cannot pass
  through cmd safely and is refused the same way. Quote the argument, or run
  `claude` directly for that one command. `&`, `|`, `<`, `>`, `^` and `(` `)`
  must be inside double quotes in cmd, as for any batch file.
- **mintty without ConPTY.** Git Bash in a mintty window without ConPTY support
  gives a native Windows program pipes instead of a terminal. `clauder` sees
  that stdin and stdout are not a terminal and passes through: claude runs
  once and never restarts. Use Windows Terminal, or enable ConPTY support in
  Git Bash, when you want automatic restarts there.

## Where it keeps state

The session and the launcher meet through a small file per launch in
`~/.claude/plugins/data/plugins-kit/relaunch-kit/relaunch/`. The launcher
creates a random token, gives it to its child session, and acts only on the
file for that token. Files left by closed terminals are removed after 24
hours.
