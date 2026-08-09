"""Bootstrap script: install claude-ui-kit's default statusLine into settings.json.

Behavior:
- If no statusLine is configured in any settings.json layer, install ours into
  the user-global `~/.claude/settings.json`. The statusLine is a user-level
  preference, not a project-level one — installing per-project meant every
  ephemeral cwd Claude was launched in (eval tmp dirs, etc.) got a stray
  `.claude/settings.local.json` written into it. This happens even if a
  declined record exists from an earlier, now-gone foreign statusLine -- see
  the edge-case comment in install() for why a decline does not carry forward
  into an empty slot.
- If the existing statusLine is already claude-ui-kit's (matches our path
  prefix), refresh it to point at the current installed location. This handles
  plugin upgrades and reinstalls transparently — wherever it was found.
- If the existing statusLine is something else (foreign), the script never
  overwrites it without consent, but it also does not nag forever:
    - No declined record yet -> surface a fix-all failure whose agent_msg
      instructs the agent to ask the user ONCE, via AskUserQuestion, to keep
      their own statusLine or switch to claude-ui-kit's. KEEP -> the agent
      writes a declined record (see DECLINED_RECORD_RELPATH below) and this
      conflict is never surfaced again. SWITCH -> the agent updates
      statusLine.command directly, same as always.
    - A declined record already exists -> stay silent (log_ok only). The
      record is per-user, lives in the plugin's own data dir (never
      settings.json or bootstrap.json), and captures what was declined in
      favor of, plus a schema version and date, so it is auditable and
      migratable rather than a bare boolean.
    - The declined record suppresses the PROMPT, never the capability: a user
      who later says "replace my status line" (or clearly equivalent) can
      still switch at any time -- nothing here gates that natural-language
      path.

The script is idempotent: re-running on every SessionStart is a no-op once
installed or once declined.

The command must be machine-INDEPENDENT
---------------------------------------
`~/.claude/settings.json` is a single file that many machines may share (it is
git-tracked in some setups). We rewrite `statusLine.command` there on every
SessionStart, so if that command embeds absolute paths, every machine rewrites
the line to its own home and interpreter and they clobber each other forever --
the file is permanently dirty and committing from one box breaks the others.
Claude Code offers no way out of writing here: a plugin's own settings.json
supports only the `agent` and `subagentStatusLine` keys, not `statusLine`.

So the emitted command must be one string that is identical everywhere and
resolves per-machine at run time:

    bash ~/.claude/plugins/data/<marketplace>/claude-ui-kit/scripts/statusline.sh

Why this form:

- `~`, not an absolute home. Claude Code documents `~` in `statusLine.command`
  as expanding on every platform, Windows included. Expansion happens in the
  shell, so the STORED string stays identical across machines. It is
  deliberately unquoted -- bash does not tilde-expand inside quotes -- which is
  safe because the result of tilde expansion is not field-split, so a home
  directory containing spaces still survives.
- `bash`, resolved from PATH, not an absolute interpreter. Claude Code runs the
  status line through Git Bash on Windows (or PowerShell when Git Bash is
  absent), and through a shell on POSIX, so a bare interpreter token resolves.
- The path points at the plugin DATA dir, which carries no version segment --
  unlike the cache dir (.../cache/<mkt>/claude-ui-kit/<version>/), which would
  churn the command on every upgrade.

The `bash` prefix is belt-and-braces. Under Git Bash a bare `.sh` path would
work on its own (the shebang runs it); the prefix additionally survives the
command being handed to `cmd.exe`, which cannot execute a bare `.sh` -- it
file-associates it, yielding empty stdout and a blank bar. Keeping the prefix
means this works under either invocation model without having to be right about
which one Claude Code uses.

Self-heal: an existing command is recognized as ours via the `/claude-ui-kit/`
substring, so any older absolute-path form (including the previous
`"<bash.exe>" "<abs path>"` wrapping) is rewritten to the portable form by the
refresh branch in install(). Every machine converges to the identical string on
its next SessionStart with no manual edits, and settings.json then stops going
dirty.
"""

import json
import sys
from datetime import date
from pathlib import Path
from typing import Optional, Tuple


PLUGIN_NAME = "claude-ui-kit"
INSTALLED_SCRIPT_RELPATH = "scripts/statusline.sh"
CUSTOMIZED_FLAG = "customized.flag"

# Per-user record of a declined statusLine takeover, written by the AGENT (not
# this script -- the script runs unattended inside a bootstrap hook and cannot
# prompt) into the plugin's own data dir. Never settings.json or
# bootstrap.json: this is install-flow state, not a user preference or a
# dependency declaration. A schema version + what was declined in favor of +
# a date makes the record auditable and migratable, unlike a bare boolean --
# see _read_declined_record for why content, not just presence, matters.
DECLINED_RECORD_RELPATH = "statusline_declined.json"
DECLINED_RECORD_SCHEMA_VERSION = 1

# Interpreter token for the emitted command. A bare name resolved from PATH,
# never an absolute path -- see the module docstring on machine independence.
_INTERPRETER = "bash"


def install(ctx) -> None:
    # The /statusline skill writes this marker when the user customizes, so the
    # install script stays quiet on subsequent SessionStarts instead of nagging
    # about a "conflicting" statusLine the user intentionally chose.
    if (Path(ctx.data_dir) / CUSTOMIZED_FLAG).exists():
        ctx.log_ok("statusline: user customized (skipping)")
        return

    installed_script = _resolve_installed_script(ctx.data_dir)
    if installed_script is None:
        ctx.log("statusline: FAILED - synced script not found at "
                f"{Path(ctx.data_dir) / INSTALLED_SCRIPT_RELPATH}")
        return

    expected_command = _build_command(installed_script)

    # Use the engine's canonical project_dir (Claude Code's launch CWD).
    # Never walk up looking for .claude/ — Claude Code itself does not, so any
    # parent .claude/ we'd find is a directory Claude Code never reads. Older
    # versions of this script walked up and silently wrote into the wrong file
    # (and worse, created stray .claude/ dirs that polluted sibling projects).
    project_dir_str = getattr(ctx, "project_dir", None)
    project_root = Path(project_dir_str).resolve() if project_dir_str else None

    # Search layers from highest to lowest precedence so the user-visible
    # statusLine is the one we compare against. Per-project layers are checked
    # so we can refresh an existing claude-ui-kit install in-place, but new
    # installs always land in the user-global layer.
    candidate_paths = []
    if project_root is not None:
        candidate_paths.append(project_root / ".claude" / "settings.local.json")
        candidate_paths.append(project_root / ".claude" / "settings.json")
    # Do NOT target the user-level ~/.claude/settings.local.json. This is by
    # design, not a quirk: `.local.json` is a PROJECT-scope concept (it exists
    # so a contributor can override a checked-in project settings.json without
    # committing personal preferences). The documented settings hierarchy has
    # exactly three physical scopes -- user settings.json, project
    # settings.json, project settings.local.json -- and no user-level .local
    # variant, for ANY key. A file written there is inert; the bar stays blank
    # no matter how many times we refresh it. (Confirmed empirically 2026-06;
    # confirmed against the settings docs 2026-07, after the inert file was
    # created a second time by someone reaching for the obvious-looking fix.)
    # So settings.json is the only place this can go -- which is exactly why
    # the command written there must be machine-independent (module docstring).
    candidate_paths.append(Path.home() / ".claude" / "settings.json")

    existing = _find_existing_statusline(candidate_paths)

    if existing is None:
        # Edge case, decided explicitly: no statusLine exists at all, but a
        # declined record may be sitting in the data dir from an earlier
        # session where the user chose to KEEP a different foreign statusLine.
        # We install ours here anyway, rather than treating the old decline as
        # a standing "never install yours". Justification: the decline was
        # scoped to "keep MY statusline", not "never offer claude-ui-kit" --
        # the user never expressed an opinion about the no-statusline case,
        # because it didn't exist yet when they answered. Silently staying
        # blank forever would be a worse default than installing, and the
        # capability to switch was never meant to expire (see the module
        # docstring's "declined record suppresses the PROMPT, never the
        # capability" note -- this is that same principle applied to the
        # empty-slot case). A stale record is cleared below since it no
        # longer describes anything real.
        target = Path.home() / ".claude" / "settings.json"
        if _refuse_unparseable(ctx, target):
            return
        _write_statusline(target, expected_command)
        _clear_declined_record(ctx.data_dir)
        ctx.log(f"statusline: installed to {_posix(target)}")
        return

    settings_path, current_command = existing

    if current_command == expected_command:
        ctx.log_ok("statusline: already installed (no-op)")
        return

    if _is_ours(current_command):
        # Our statusLine, but the stored command differs from what we'd emit
        # now. Two causes, both remediated by rewriting to expected_command:
        #   1. Plugin path moved (upgrade, scope change).
        #   2. The command is a LEGACY absolute form written by an older version
        #      of this installer -- machine-specific, so each machine rewrote it
        #      and they clobbered each other. Rewriting to the portable form is
        #      the one-time migration that ends the churn (module docstring).
        if _refuse_unparseable(ctx, settings_path):
            return
        _write_statusline(settings_path, expected_command)
        if not _is_portable(current_command):
            ctx.log(
                f"statusline: migrated machine-specific command in "
                f"{_posix(settings_path)} to the portable form"
            )
        else:
            ctx.log(f"statusline: refreshed path in {_posix(settings_path)}")
        return

    # User has a foreign statusLine. Don't touch it without explicit consent.
    # But don't ask forever either: a declined record from an earlier session
    # means the user already answered, so stay silent (verbose-only ok entry,
    # never a failure/action entry) rather than re-surfacing the same conflict
    # on every bootstrap pass. See DECLINED_RECORD_RELPATH above for why the
    # record lives in the plugin's own data dir with a real schema.
    declined = _read_declined_record(ctx.data_dir)
    if declined is not None:
        ctx.log_ok(
            f"statusline: foreign statusLine in {_posix(settings_path)} "
            f"previously declined (on {declined.get('declined_date', 'unknown date')}), "
            f"not re-asking"
        )
        return

    declined_record_path = _posix(Path(ctx.data_dir) / DECLINED_RECORD_RELPATH)
    ctx.add_failure(
        "statusline_conflict",
        settings_path=_posix(settings_path),
        existing_command=current_command,
        new_command=expected_command,
        user_msg=(
            f"claude-ui-kit found an existing statusLine in "
            f"{_posix(settings_path)} and will not overwrite it without your "
            f"say-so."
        ),
        agent_msg=(
            f"The user has a foreign (non-claude-ui-kit) statusLine configured "
            f"in {_posix(settings_path)} with command: {current_command}\n"
            f"This has never been asked about before (no declined record at "
            f"{declined_record_path}). Ask the user ONCE, using the "
            f"AskUserQuestion tool, whether to keep their existing statusLine "
            f"or switch to claude-ui-kit's default. Offer exactly two options:\n"
            f"  1. Keep my existing statusLine\n"
            f"  2. Switch to claude-ui-kit's statusLine\n"
            f"If they choose to KEEP: write "
            f"{declined_record_path} (creating parent directories as needed) "
            f"with this exact JSON shape, so the conflict is never surfaced "
            f"again:\n"
            f'  {{"schema_version": {DECLINED_RECORD_SCHEMA_VERSION}, '
            f'"declined_command": {json.dumps(current_command)}, '
            f'"declined_date": "{date.today().isoformat()}"}}\n'
            f"Do NOT modify {_posix(settings_path)} in this case.\n"
            f"If they choose to SWITCH: update {_posix(settings_path)} so "
            f"that statusLine.command = {expected_command} (keep "
            f"statusLine.type = 'command'); do not write a declined record.\n"
            f"This declined record only suppresses future automatic prompts -- "
            f"it never blocks the switch. If the user later says 'replace my "
            f"status line' (or clearly equivalent intent) in ANY future "
            f"session, honor it immediately by updating "
            f"{_posix(settings_path)} the same way, regardless of any "
            f"declined record on disk."
        ),
    )


def _read_declined_record(data_dir: str) -> Optional[dict]:
    """Return the declined-record dict if one exists and parses, else None.

    Presence alone (not content validity) is what suppresses the prompt --
    a malformed record still means an agent deliberately wrote *something*
    there in response to a KEEP answer, and re-asking would contradict that.
    Parsed content is used only for the log message when available.
    """
    path = Path(data_dir) / DECLINED_RECORD_RELPATH
    if not path.is_file():
        return None
    data = _load_json(path)
    return data if isinstance(data, dict) else {}


def _clear_declined_record(data_dir: str) -> None:
    """Remove a stale declined record once it no longer describes anything.

    Called when install() lands a fresh install into a now-empty statusLine
    slot (see the edge-case comment at that call site) -- the record's
    ``declined_command`` describes a statusLine that is gone, so leaving it
    around would be a harmless but misleading audit trail.
    """
    path = Path(data_dir) / DECLINED_RECORD_RELPATH
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _resolve_installed_script(data_dir: str) -> Optional[Path]:
    p = Path(data_dir) / INSTALLED_SCRIPT_RELPATH
    return p if p.is_file() else None


def _tilde_path(p: Path) -> str:
    """Spell `p` relative to `~` when it lives under the home directory.

    This is what keeps the emitted command identical across machines: the stored
    string carries no home prefix, and the shell Claude Code runs it through
    expands `~` locally. Falls back to the absolute posix path when `p` is not
    under home (nothing else is portable, and a working absolute command beats a
    broken relative one).
    """
    try:
        relative = p.relative_to(Path.home())
    except ValueError:
        return _posix(p)
    return "~/" + _posix(relative)


def _build_command(installed_script: Path) -> str:
    """The statusLine command string to write into settings.

    One spelling for every platform: a PATH-resolved interpreter plus a
    `~`-relative script path. See the module docstring for why each half is
    required and why the path is deliberately unquoted.
    """
    return f"{_INTERPRETER} {_tilde_path(installed_script)}"


def _is_portable(command: str) -> bool:
    """True if `command` is already the machine-independent form.

    Used only to describe what happened in the log -- rewriting an absolute
    legacy command (the old `"<bash.exe>" "<abs path>"` wrapping, or a bare
    absolute `.sh`) is the migration that stops settings.json churning, and is
    worth saying out loud rather than reporting as a routine path refresh.
    """
    return command.startswith(f"{_INTERPRETER} ~/")


def _find_existing_statusline(paths) -> Optional[Tuple[Path, str]]:
    """Return (path, command) of the highest-precedence layer with a statusLine."""
    for path in paths:
        data = _load_json(path)
        if not isinstance(data, dict):
            continue
        sl = data.get("statusLine")
        if isinstance(sl, dict) and isinstance(sl.get("command"), str):
            return path, sl["command"]
    return None


def _is_ours(command: str) -> bool:
    return f"/{PLUGIN_NAME}/" in command.replace("\\", "/")


def _refuse_unparseable(ctx, settings_path: Path) -> bool:
    """Refuse to write over a settings file that exists but cannot be parsed.

    _load_json returns None for both "missing" and "malformed"; writing in the
    malformed case would replace the user's entire settings file with just our
    statusLine block, destroying every other setting. Surface a fix-all
    failure instead and leave the file untouched.
    """
    if not settings_path.is_file() or _load_json(settings_path) is not None:
        return False
    ctx.add_failure(
        "statusline_settings_unparseable",
        settings_path=_posix(settings_path),
        user_msg=(
            f"claude-ui-kit could not parse {_posix(settings_path)} (invalid "
            f"JSON) and will not modify it. Fix the file's JSON syntax, then "
            f"re-run bootstrap to install the status line."
        ),
        agent_msg=(
            f"{_posix(settings_path)} exists but is not valid JSON. DO NOT "
            f"overwrite or truncate it -- it may contain settings the user "
            f"wants to keep. Help the user repair the JSON syntax; once it "
            f"parses, the statusLine install proceeds on the next bootstrap "
            f"run."
        ),
    )
    return True


def _write_statusline(settings_path: Path, command: str) -> None:
    data = _load_json(settings_path) or {}
    if not isinstance(data, dict):
        data = {}
    data["statusLine"] = {"type": "command", "command": command}
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _load_json(path: Path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _posix(p: Path) -> str:
    return str(p).replace("\\", "/")
