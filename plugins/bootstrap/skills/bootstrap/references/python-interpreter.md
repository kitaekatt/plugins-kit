# Python interpreter variables

Bootstrap exports two environment variables so nothing that needs Python has
to guess a name on PATH: on Windows `python3` is usually absent and `python`
may be a Microsoft Store stub, a stray Anaconda install, or any stranger's
interpreter. Contract, minimum version `0.120.0` (`MIN_VERSION` in
`interpreter_env.py`):

- **`BOOTSTRAP_PYTHON`** -- the bootstrap-owned interpreter. On a machine
  bootstrap provisioned itself, this is the deterministic standalone install
  under `~/.local/share/python-standalone/` (Windows:
  `~/.local/share/python-standalone/python/python.exe`; elsewhere:
  `~/.local/bin/python3`, a symlink the SessionStart hook maintains).
- **`BOOTSTRAP_PROJECT_PYTHON`** -- the current project's own interpreter: a
  `.venv` found by walking up from the working directory, or an active
  `$VIRTUAL_ENV`; falling through to the same value as `BOOTSTRAP_PYTHON` when
  neither is found. A project can opt OUT of this detection entirely with
  `"project_python": false` in its layered manifest, in which case this name
  is not set at all for that project -- see "Opting out of project interpreter
  detection" below.

Call-site expressions (verbatim from `interpreter_env.py`; copy these, do not
retype them):

    # Project code -- prefers the project's own venv, falls back to bootstrap's:
    "${BOOTSTRAP_PROJECT_PYTHON:-${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}}"

    # Bootstrap/plugin machinery and stdlib-only glue -- always the bootstrap interpreter:
    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}"

The `:?` form fails the command loudly, with `requires bootstrap >= 0.120.0`
in the error, on an engine that predates the export -- never silently falling
through to whatever `python` happens to resolve on PATH.

## Default vs. forced

Two different questions get two different expressions, and mixing them up is
the most common mistake:

- **Project calls default.** Code that runs against a project -- a script a
  developer invokes from their own checkout, a documented human command, a
  project's own tooling -- uses the nested form. It DEFAULTS to that
  project's venv, then to the bootstrap interpreter, and is never forced past
  that: if neither exists, the `:?` still fires, but only after the project
  venv had its chance.
- **Bootstrap's own code is forced.** A script that IS bootstrap (a
  SessionStart hook, a lever, a Claude Code hook script bootstrap ships) must
  run under the SAME interpreter every time, regardless of what venv happens
  to sit in the current directory. These call sites use the bare
  `BOOTSTRAP_PYTHON` form, or -- where the variable itself might not exist
  yet, because the script IS the thing that produces it -- the deterministic
  standalone path first, with the variable accepted only as a fallback (see
  "Why the SessionStart hook writes the session block" below).

A shipped plugin's own scripts that need the PLUGIN's venv (not the
project's) are a third, unrelated case: launch under `BOOTSTRAP_PYTHON` and
let the script `reexec_under_plugin_venv` into its own venv. See
`plugins/CLAUDE.md`, "Shared-lib scripts must re-exec under the plugin venv".

## Skill preload commands

A skill's `!` preload (`` !`cmd` ``, or a ```` ```! ```` block) cannot use
either variable. Claude Code runs the preload before the skill renders and
refuses any preload command that contains a shell expansion ("Contains
expansion"), so the whole skill fails. Only the names Claude Code substitutes
itself, such as `${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_SESSION_ID}`, may appear.
Launch Python in a preload as:

    !`uv run --no-project python "${CLAUDE_PLUGIN_ROOT}/scripts/<script>.py" $ARGUMENTS`

Commands the agent runs itself (step text, `tool:` values, "run ..." lines)
still use the variable forms above.

## Where each value comes from

| Surface | `BOOTSTRAP_PYTHON` | `BOOTSTRAP_PROJECT_PYTHON` | Provided by |
|---|---|---|---|
| Engine process, full pass, from pass start | yes | yes: normative rule without manifest knowledge at start; manifest-aware once the layered manifest loads; verified after the project venv step; recorded per project | Engine |
| Engine process, always lane (throttled sessions) | yes | yes: the per-project record if one exists, else the normative rule | Engine |
| `bootstrap run` / `bootstrap profile set` (terminal CLI) | yes | yes, as in a full pass for the exact working directory, except that the record is neither read nor written (`--project-key _global_`) -- the CLI cannot reproduce the hook's project key from a native cwd | Engine, launched by the Bootstrap CLI |
| The fix queue (`fix_runner.py`) | yes (the runner's own interpreter) | popped -- not carried into the queue; the nested call-site form degrades correctly to `BOOTSTRAP_PYTHON` | Fix runner |
| Claude session Bash tool calls -- every session, including throttled and `--resume` | yes (written by the SessionStart hook prelude, before any skip gate) | yes: the per-project record if present, else the normative rule from `$PWD`; the engine's own verified value is never overridden (the prelude writes only names that are still absent) | SessionStart hook |
| A Bash tool call after `cd` to a different project, inside one session | the session's value (unchanged) | the session's value (unchanged) -- see "Gaps not covered" below | -- |
| Terminals: bash on Linux/macOS/Git Bash; zsh on macOS | persisted (`~/.bashrc`; `~/.zshrc` too on macOS) | a per-directory hook, sourced from the same rc files | Shell integration |
| Terminals: zsh on Linux; login-only bash profiles | not covered (see "Gaps not covered") | not covered | -- |
| Terminals: PowerShell 5.1 / pwsh 7 on Windows | the registry (`HKCU\Environment`) | a per-directory hook, ONLY when a PowerShell profile already exists (bootstrap never creates one) | Shell integration |
| Terminals: pwsh on macOS/Linux | not covered (profile writes are Windows-only) | not covered | -- |
| Terminals: `cmd.exe` | the registry | none -- there is no per-directory hook for `cmd.exe`; the nested form degrades to `BOOTSTRAP_PYTHON` | -- |
| Claude Code hook scripts bootstrap ships (not the SessionStart hook itself) | inherited from Claude Code's own process environment only, if any | same, or unset | forced form, deterministic path first |
| git hooks, CI, cron, IDE-launched processes | rc files are not sourced; the registry still applies on Windows | unset unless the project's own tooling sets it | forced form, deterministic path first |
| The SessionStart hook itself | producer -- it computes the deterministic path, it does not read the variable | producer | -- |

## In your project

Prefer the nested form so your script keeps working inside the project's own
venv when there is one, and still runs on a machine that has none.

**bash / zsh**

    python_bin="${BOOTSTRAP_PROJECT_PYTHON:-${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}}"
    "$python_bin" -m pytest

**PowerShell (5.1 or pwsh 7)**

    $pythonBin = if ($env:BOOTSTRAP_PROJECT_PYTHON) { $env:BOOTSTRAP_PROJECT_PYTHON }
                 elseif ($env:BOOTSTRAP_PYTHON) { $env:BOOTSTRAP_PYTHON }
                 else { throw "requires bootstrap >= 0.120.0" }
    & $pythonBin -m pytest

**cmd.exe** (no per-directory hook -- see the table above; this always runs
the bootstrap interpreter, never a project venv)

    if defined BOOTSTRAP_PROJECT_PYTHON (
        set "PYTHON_BIN=%BOOTSTRAP_PROJECT_PYTHON%"
    ) else if defined BOOTSTRAP_PYTHON (
        set "PYTHON_BIN=%BOOTSTRAP_PYTHON%"
    ) else (
        echo requires bootstrap ^>= 0.120.0 1>&2 & exit /b 1
    )
    "%PYTHON_BIN%" -m pytest

**Python, via `os.environ`** (a script that needs to launch a Python
subprocess rather than being launched as one)

    import os
    python_bin = os.environ.get("BOOTSTRAP_PROJECT_PYTHON") or os.environ["BOOTSTRAP_PYTHON"]

## Opting out of project interpreter detection

A project's own layered manifest (`<project>/.claude/bootstrap.json` or
`<project>/.claude/bootstrap.local.json`) may declare `project_python`, with
exactly one accepted value:

    { "project_python": false }

The key is honoured only in those project files. In the user layer
(`~/.claude/bootstrap.json`) it is ignored with a log note, and a terminal
never reads it at the home directory. A terminal (the shell hook,
`bootstrap python`) walks up from its working directory; the first directory
that opts out or holds a venv decides.

This is an opt-OUT, not a way to point `BOOTSTRAP_PROJECT_PYTHON` at a
specific path -- no other value is accepted. Once set, bootstrap stops trying
to detect a project interpreter for that project entirely: no `.venv`
walk-up, no `$VIRTUAL_ENV` check, no per-project record file. As a result
`BOOTSTRAP_PROJECT_PYTHON` is **not set at all** for that project -- not in
the engine, not in a Claude session, not in a terminal -- rather than falling
back to `BOOTSTRAP_PYTHON`'s value. `BOOTSTRAP_PYTHON` itself is unaffected.

Use this when a project manages its own interpreter selection by other means
and bootstrap's detection would be wrong or redundant for it; the project's
own tooling and documented commands are then responsible for choosing an
interpreter without bootstrap's help.

## Opting out of persistence and the shell hook

Two independent opt-outs live under the top-level `interpreter_env` key,
read from the USER layers only (`~/.claude/bootstrap.json` and its profile
chain -- a project layer setting this key is ignored, with a note; per-machine
choices belong in the per-user file, not a committed project manifest):

    {
      "interpreter_env": {
        "persist": true,
        "shell_hook": true
      }
    }

Both default to `true`.

- `persist: false` stops bootstrap from writing `BOOTSTRAP_PYTHON` to shell rc
  files or the Windows registry, and removes a previously written value.
- `shell_hook: false` stops bootstrap from adding the per-directory
  `BOOTSTRAP_PROJECT_PYTHON` hook to shell rc files and PowerShell profiles,
  and removes a previously added line.

Neither opt-out affects the engine's own process-scoped export, the
SessionStart hook's per-session write, or the per-project record file --
those are not persistence in the rc-file/registry sense and have no opt-out.

## PowerShell profiles: existing profiles only, never created

Bootstrap never creates a PowerShell profile file. If `$PROFILE` does not
already exist, `BOOTSTRAP_PROJECT_PYTHON` is not available in that shell's
per-directory hook -- only the registry-persisted `BOOTSTRAP_PYTHON` is. Two
independent reasons: PowerShell 5.1's default `Restricted` execution policy
makes a bootstrap-created profile error on every new window, and a session
running under `BOOTSTRAP_SKIP_REGISTRY`-style isolation must never write to a
real user profile.

To get the per-directory hook, create a profile yourself and allow local
scripts to run, once, per machine:

    if (!(Test-Path -Path $PROFILE)) { New-Item -ItemType File -Path $PROFILE -Force }
    Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

The next bootstrap pass adds the dot-source line to the profile it finds.

## Gaps not covered

- **zsh on Linux, and login-only bash profiles.** Bootstrap's rc-file writer
  only touches the files it always has: `~/.bashrc` everywhere, and
  `~/.zshrc` on macOS. A Linux zsh user, or a bash setup that reads only a
  login profile, gets neither name in a fresh terminal. Add the line by hand,
  once, to whichever file that shell actually sources:

      [ -f "$HOME/.claude/plugins/data/plugins-kit/bootstrap/shell/project-python.sh" ] && . "$HOME/.claude/plugins/data/plugins-kit/bootstrap/shell/project-python.sh"

- **pwsh on macOS/Linux.** Profile writes are Windows-only; neither name is
  set in a cross-platform PowerShell terminal outside Windows.
- **A Bash tool call that `cd`s to a different project, inside one Claude
  session.** The names the SessionStart hook wrote for the session's starting
  project stay in effect after a `cd` -- they are not re-resolved per
  directory change inside a single session. The gap is solvable: Claude
  Code fires a `CwdChanged` hook on a Bash-tool `cd`, and the environment file
  that hook writes is sourced after the SessionStart file, so it can replace
  the session's value. Bootstrap registers no `CwdChanged` hook, so the gap
  stands.

## Why not `${python}`

Bootstrap manifests support `${plugin_root}`, `${data_dir}`, and similar
variable substitution (see manifest-reference.md, "Variable Expansion"), but
there is no `${python}` among them and there will not be one. A `tools[].check`
/ `install` command and an `env.json` `env_checks[].check` / `fix` command are
opaque shell strings the engine hands to `bash -c` unsubstituted -- manifest
variable expansion never reaches them. `BOOTSTRAP_PYTHON` and
`BOOTSTRAP_PROJECT_PYTHON` are ordinary environment variables instead, visible
to any command the shell runs, with no substitution step required.

## Why the SessionStart hook writes the session block

An engine pass only runs to completion on a full session; most sessions are
throttled by the per-project cooldown, or resumed, and never invoke the
engine at all. If the names were exported only by the engine process, a Bash
tool call in a throttled or resumed session would see neither one. The
SessionStart hook prelude therefore computes both names itself -- the
deterministic path for `BOOTSTRAP_PYTHON`, the normative resolution rule
(an opt-out, `$VIRTUAL_ENV`, the per-project record, the `.venv` walk-up,
then the deterministic path) for `BOOTSTRAP_PROJECT_PYTHON`, writing no
project line for an opted-out project -- and writes whichever names are still absent from
the session's environment file, before any skip gate runs. A full pass that
does run afterwards writes its own, verified values; the prelude never
overwrites a name that is already set, so the engine's answer always wins
when there is one.
