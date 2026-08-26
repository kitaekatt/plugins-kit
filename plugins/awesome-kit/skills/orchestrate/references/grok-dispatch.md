# Grok dispatch mechanics

The exact flags, command shape, and traps for launching a unit on the Grok CLI
backend. Load this when you have already been ASKED for Grok and are composing
the launch -- the rendered policy carries the summary; this carries the detail.

Rendered policy: `scripts/orchestration_guidance.py`. Backend record and the
one-line command: `defaults/orchestration.yaml`, `backends[id: grok]`.

## When this backend runs at all

Only when the user names it. Grok is not a routing target, so no shape row
resolves here; its backend block opens
with a `Selection.` line saying so. A user who says "grok" means this backend
at `-m grok-4.6`.

`grok models` on the machine this was written against offers `grok-4.6`
(default) and `grok-4.5`. Only `grok-4.6` is sanctioned. Pass `-m grok-4.6`
explicitly rather than relying on the default -- the default is a property of
the install and can move under you, and `--version` (which is what detection
runs) says nothing about which models the account can reach.

## The one invocation

```text
grok --prompt-file <ABSOLUTE brief> \
  -m grok-4.6 \
  --cwd <ABSOLUTE root> \
  --always-approve \
  --no-subagents \
  --output-format json \
  > <ABSOLUTE result.json> 2> <ABSOLUTE log>
```

  `--prompt-file <FILE>`
                 Headless single-turn, brief read from a file. A long brief
                 full of quotes and backticks cannot go through a shell
                 argument intact.
  `-p, --single` The OTHER single-turn form, taking the prompt as its own
                 value. It is an ALTERNATIVE to `--prompt-file`, not a
                 companion: `grok -p --prompt-file X` fails at launch with
                 `a value is required for '--single <PROMPT>'`, because
                 `--prompt-file` is consumed as `-p`'s value.
  `-m grok-4.6`  The only sanctioned model.
  `--cwd <DIR>`  The working directory. Pass it explicitly; a `cd` in a
                 backgrounded Bash call does not persist.
  `--always-approve`
                 REQUIRED for a backgrounded unit. Without it the agent stops
                 at the first tool prompt and waits for an answer nobody is
                 there to give. (Product name for the `bypassPermissions`
                 mode; `--yolo` is an alias.)
  `--no-subagents`
                 Keeps the unit a worker. Grok can fan out on its own, which
                 from inside an orchestration means an orchestrator dispatching
                 an orchestrator.
  `--output-format json`
                 Structured result on stdout. Default is `plain`.

Per-unit knobs on top of that shape:

  `--effort <low|medium|high|xhigh>`
                 grok-4.6's advertised menu. The CLI's canonical scale also
                 names `none`, `minimal` and `max`, but a model accepts only
                 the levels its own menu advertises, and grok-4.6 does not
                 offer `max`. Do not carry `max` over from the policy's effort
                 scale, which is written for the other backends.
  `--json-schema <FILE|JSON>`
                 Constrains the final message to a JSON Schema. Implies
                 `--output-format json`.
  `--max-turns <N>`
                 Agentic turn cap. Headless-only.
  `--deny <RULE>`
                 Hard block, and see below -- on Windows it is the only one
                 available. Deny wins over allow and over `--always-approve`.
  `--disallowed-tools <TOOLS>` / `--tools <TOOLS>`
                 Tool allow/deny by name. Both headless-only; in the TUI they
                 are warned about and ignored.

Launch each unit as its own Bash tool call with `run_in_background: true`.

## Collecting

Read `.text` from the result JSON -- that is the final message, and usually the
only thing that belongs in the orchestrating context. The object also carries
`stopReason`, `sessionId`, `usage`, `num_turns` and `total_cost_usd`.

Check `stopReason`. A unit that hit `--max-turns` still exits 0, with a partial
answer that reads like a finished one. Exit status tells you the process ended,
not that the work happened -- verify a file-writing unit against the actual
diff.

There is no `-o` flag; the result comes back on stdout, which is why the
invocation redirects it to a file and sends stderr somewhere else. Do not merge
the two streams into one file: the JSON has to parse.

## There is no sandbox on Windows

`--sandbox <PROFILE>` is enforced with Landlock on Linux and Seatbelt on macOS.
Windows is not in the enforcement table. The default profile is `off` on every
platform -- unrestricted filesystem read, write, and network.

Two consequences, and the second is the trap:

- A grok unit on Windows can write anywhere the invoking user can. `--cwd` is
  where it starts, not a boundary. Brief accordingly, and do not treat a root
  as containment.
- An unknown profile name is accepted silently there rather than refusing to
  start -- `grok --sandbox bogus -p hi` runs normally. So a typo'd profile
  reads exactly like a working sandbox. Pass no `--sandbox` at all rather than
  one that cannot enforce; `--deny` rules are the real block, and per the CLI's
  own permissions guide they hold even under `--always-approve` ("Deny always
  wins over allow and over always-approve's normal pass-through", and
  always-approve "short-circuits this pipeline after step 2: `deny` rules,
  hooks, and `ask` rules that match a shell command's segments still apply") --
  `~/.grok/docs/user-guide/22-permissions-and-safety.md`, which ships with the
  CLI.

On Linux and macOS the built-in profiles are `workspace` (read anywhere, write
CWD + temp + `~/.grok/`), `devbox`, `read-only`, and `strict`. Child-process
network blocking under `read-only` / `strict` is Linux-only.

**On those platforms, add one.** The sanctioned command above omits `--sandbox`
because it must be correct on Windows too, where the flag is theatre. On Linux
or macOS append `--sandbox workspace` for a writing unit, or `--sandbox
read-only` for one that only reads -- there the kernel enforces it, and the
omission above is a portability floor rather than a recommendation. Pin it for
every unit by putting it in the backend's `command:` in your user or project
`orchestration.yaml` layer.

## Isolation

`-w/--worktree` does NOT create a worktree under headless -- the flag is there
for interactive sessions. Parallel grok writers therefore share the tree unless
you give them separate ones yourself:

```bash
git worktree add -b wt/<unit> ../<repo>-<unit>-wt master
```

then point that unit's `--cwd` at the worktree's absolute path. A fresh
worktree holds only TRACKED files, so gitignored paths (a repo venv, a staged
`tmp/`) are absent -- either give that unit the main tree or say in the brief
where those live.

## It arrives carrying your instructions

Grok is a second Claude-compatible harness, not a bare worker. `grok inspect`
reports what it discovers for a directory; in a repo like this one that is the
user-level and project `CLAUDE.md`, `.claude/rules/`, the `.claude/settings.json`
permission rules, and the installed Claude Code skills -- `orchestrate` among
them.

That is mostly a benefit: the unit inherits the project's conventions without
being told them. Two things follow anyway.

- Brief it to do the work, not to route it. It has an orchestration skill and a
  subagent facility, and a brief that reads like a mandate to coordinate will
  get coordination. `--no-subagents` removes the facility; the brief has to
  remove the ambition.
- The inherited material is not free. It is tens of thousands of tokens before
  the brief is read. `--system-prompt-override` and `--verbatim` exist if you
  need a unit that does not inherit it -- at the cost of the repo conventions
  that make the output fit.

## Follow-up, and what it does NOT give you

Grok keeps sessions: the result JSON carries a `sessionId`, and
`--resume <ID>` / `--continue` reopen it. That is a real advantage over a bare
one-shot CLI -- a finished unit can be extended in place, keeping its context,
instead of being re-briefed from scratch.

It is NOT a channel to a unit that is still running. The `sessionId` arrives in
the result JSON, which the unit writes when it EXITS, and `--resume` reopens a
session that has ended. So the orchestrate procedure's rule stands unchanged
here: a constraint that changes while a grok unit is in flight does not reach
it, and that unit is cancelled and relaunched, or its result treated as
pre-change and re-verified. Say which you did.
