#!/usr/bin/env python3
"""Single-session bootstrap update harvest (Part 2 of the single-session update
protocol).

Runs on UserPromptSubmit (via hooks/userpromptsubmit/bootstrap-display.sh). When
Claude Code's auto-updater has fetched a NEWER bootstrap mid-session -- the new
code is on disk and installed_plugins.json points at it, but the SessionStart
hook already ran the OLD engine and won't re-fire this session -- this harvests
the new engine by launching it once, so a published update converges in ONE
session instead of two restarts.

Stdlib-only and cheap by design: the common (no-update) path is two reads + a
version compare. Only when installed_version > engine_ran_version does it detach
a full pass. The launched engine stamps engine_ran_version = its OWN version on
completion (engine._main), which is the loop guard -- it can never re-trigger
itself. A per-installed-version launch marker (harvest_launched_version) caps
relaunches at one while the engine converges.

CAVEAT (inherent, documented, NOT solved here): the RUNNING bootstrap must
already contain this harvest hook to harvest a newer version. The version that
FIRST ships the protocol cannot harvest itself -- that one transition still needs
the old two-restart path. Single-session convergence applies to every update
AFTER this ships.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

# Make `bootstrap_lib.*` importable whether this file is imported as a MODULE
# (tests, in-process) or executed as a SCRIPT (the UserPromptSubmit hook runs
# `python harvest.py`). Under script execution __package__ is None, so RELATIVE
# imports (`from .stamps import ...`) raise "attempted relative import with no
# known parent package" — which made run_harvest throw and the hook silently
# no-op (the harvest never fired in production). Putting the plugin root on
# sys.path lets every import below use the absolute `bootstrap_lib.*` form, which
# resolves in BOTH contexts. engine.py's top-level imports are light (stdlib +
# atomic_write), so reusing its semver parser stays cheap.
_PLUGIN_ROOT = str(Path(__file__).resolve().parent.parent)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)
from bootstrap_lib.engine import _parse_semver


def _default_registry() -> str:
    """``~/.claude/plugins/installed_plugins.json`` using the same HOME resolution
    as the rest of bootstrap (HOME preferred, ``~`` expansion as fallback)."""
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".claude", "plugins", "installed_plugins.json")


def _cache_installed_bootstrap(registry_path: str, marketplace: str) -> Tuple[str, str]:
    """Registry-v2 fallback: derive bootstrap's installed ``(version,
    install_path)`` from the cache layout when the registry has no entry.

    Newer Claude Code keeps installed_plugins.json at ``{"plugins": {}}`` for
    marketplace installs; the cache dir with the highest version under
    ``<plugins>/cache/<marketplace>/bootstrap/`` IS the code Claude Code loads
    next session, so it is the installed version for harvest purposes.
    Returns ``("", "")`` when nothing resolvable exists (e.g. test registries
    in a tmp dir with no cache sibling).
    """
    cache_root = os.path.join(os.path.dirname(os.path.abspath(registry_path)), "cache")
    if marketplace:
        marketplaces = [marketplace]
    else:
        try:
            marketplaces = sorted(os.listdir(cache_root))
        except OSError:
            return "", ""
    for mkt in marketplaces:
        plugin_dir = os.path.join(cache_root, mkt, "bootstrap")
        try:
            versions = [
                d for d in os.listdir(plugin_dir)
                if os.path.isdir(os.path.join(plugin_dir, d))
            ]
        except OSError:
            continue
        if versions:
            version = max(versions, key=_parse_semver)
            return version, os.path.join(plugin_dir, version)
    return "", ""


def read_installed_bootstrap(registry_path: str, marketplace: str) -> Tuple[str, str]:
    """Return ``(version, install_path)`` for the installed bootstrap plugin, or
    ``("", "")`` on any miss.

    The registry entry key looks like ``"bootstrap@plugins-kit"`` mapping to
    either a dict or a list of per-scope dicts (each with ``version`` /
    ``installPath``). Robust to both shapes; falls back to the first key whose
    name part is ``bootstrap`` when the marketplace-qualified ref isn't present,
    then to the cache layout (``_cache_installed_bootstrap``) when the registry
    records nothing at all (Claude Code registry v2).
    """
    version, path = _registry_installed_bootstrap(registry_path, marketplace)
    if version and path:
        return version, path
    return _cache_installed_bootstrap(registry_path, marketplace)


def _registry_installed_bootstrap(registry_path: str, marketplace: str) -> Tuple[str, str]:
    """The registry half of ``read_installed_bootstrap`` -- ``("", "")`` on any miss."""
    try:
        with open(registry_path) as f:
            plugins = json.load(f).get("plugins", {})
    except (OSError, ValueError):
        return "", ""
    if not isinstance(plugins, dict):
        return "", ""

    entry = plugins.get(f"bootstrap@{marketplace}") if marketplace else None
    if entry is None:
        entry = plugins.get("bootstrap")
    if entry is None:
        for key, val in plugins.items():
            if str(key).split("@", 1)[0] == "bootstrap":
                entry = val
                break
    if entry is None:
        return "", ""

    rec = None
    if isinstance(entry, dict):
        rec = entry
    elif isinstance(entry, list):
        for e in entry:
            if isinstance(e, dict) and e.get("installPath"):
                rec = e
                break
    if not isinstance(rec, dict):
        return "", ""
    return rec.get("version", "") or "", rec.get("installPath", "") or ""


def should_harvest(installed_version: str, ran_version: str) -> bool:
    """True when the installed engine is STRICTLY NEWER than the one that last
    executed a pass -- the only case the new code is on disk but never ran this
    session. A missing ran_version parses as ``(0, 0, 0)``."""
    if not installed_version:
        return False
    return _parse_semver(installed_version) > _parse_semver(ran_version or "0")


def launch_new_engine(install_path: str, project_dir: str, data_dir: str) -> bool:
    """Detach a full bootstrap pass via the NEW engine's session-bootstrap.sh.

    Invoked by ``install_path`` (the registry's installPath for the new version),
    NOT via ``${CLAUDE_PLUGIN_ROOT}`` -- which is bound to the OLD version dir for
    this session. We clear the per-project cooldown first so the new
    session-bootstrap.sh's throttle gate doesn't skip the pass, then launch it
    detached with empty stdin (its session_id guard is inert without stdin) and
    output suppressed. The engine writes its user-facing output to
    bootstrap_display.pending, surfaced by the NEXT UserPromptSubmit -- exactly
    the same background/pending-file mode SessionStart uses.

    Returns True if the launch was started, False if the script is missing or the
    spawn failed (best-effort; a failed harvest just defers to the next session).
    """
    sb = Path(install_path) / "hooks" / "sessionstart" / "session-bootstrap.sh"
    if not sb.is_file():
        return False

    # Force the pass: deleting this project's cooldown stamp makes the new
    # session-bootstrap.sh's `[ -f "$_COOLDOWN_FILE" ]` gate false -> it runs
    # (and re-stamps the cooldown itself). Routed through the same project-scope
    # stamp the shell uses, so the path matches exactly.
    try:
        from bootstrap_lib.stamps import project_stamp
        project_stamp(data_dir, "last_run_epoch", project_dir or "").clear()
    except Exception:
        pass  # a stale stamp at worst lets the -nt gate still bypass; never fatal

    # Detach so the spawned pass outlives this short-lived hook process.
    popen_kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if project_dir:
        popen_kwargs["cwd"] = project_dir
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    else:  # Windows: detach from the parent console/job
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    try:
        subprocess.Popen(["bash", str(sb)], **popen_kwargs)
    except OSError:
        return False
    return True


def run_harvest(
    data_dir: str,
    project_dir: str,
    registry_path: str,
    marketplace: str,
) -> Optional[str]:
    """Decide + (maybe) launch a harvest. Returns a short status string when a
    launch happened (for logging), else ``None``. Never raises -- best-effort,
    runs on every prompt.

    Common path is two reads (registry version + engine_ran_version stamp) and a
    compare; the launch marker and engine spawn only happen on a genuine update.
    """
    from bootstrap_lib.stamps import global_stamp

    installed_version, install_path = read_installed_bootstrap(registry_path, marketplace)
    if not installed_version or not install_path:
        return None

    # Transient import-crash retry (fires even WITHOUT a version bump). A partial
    # plugin cache download can race the SessionStart hook: the engine imports a
    # first-party submodule that has not landed yet and crashes. engine._defer_
    # transient_retry marks a retry pending and stays silent; we relaunch the pass
    # here, once per crash (the launched guard is voided by each crash), until a
    # completed pass clears the markers (engine._main). Checked before the version
    # gate because the crashing and installed versions are usually equal.
    if global_stamp(data_dir, "import_retry_pending").read():
        launched = global_stamp(data_dir, "import_retry_launched")
        if launched.read():
            return None  # a retry pass is already in flight; no double-spawn
        launched.write("1")
        if not launch_new_engine(install_path, project_dir, data_dir):
            return None
        return (
            f"import-retry: relaunched bootstrap {installed_version} after a "
            "transient import crash"
        )

    ran_version = global_stamp(data_dir, "engine_ran_version").read()
    if not should_harvest(installed_version, ran_version):
        return None

    # Loop-guard belt-and-suspenders: only launch once per installed version, so
    # several prompts in the seconds before the harvested engine stamps
    # engine_ran_version don't spawn concurrent passes. The primary guard remains
    # engine_ran_version (above); once the engine completes, installed == ran and
    # this branch is never reached again.
    launched_stamp = global_stamp(data_dir, "harvest_launched_version")
    if launched_stamp.read() == installed_version:
        return None
    launched_stamp.write(installed_version)

    if not launch_new_engine(install_path, project_dir, data_dir):
        return None
    return (
        f"harvest: launched bootstrap {installed_version} engine "
        f"(engine_ran_version was {ran_version or 'none'})"
    )


def _log_launch(data_dir: str, status: str) -> None:
    """Append a single audit line to bootstrap.log on a launch (never on a no-op
    prompt). Honors the 'every action logs its outcome' principle without adding
    per-prompt cost. Best-effort."""
    try:
        from bootstrap_lib.log import write_log_block
        write_log_block(data_dir, "bootstrap harvest", [status])
    except Exception:
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap single-session update harvest")
    parser.add_argument("--data-dir", required=True, help="bootstrap data dir")
    parser.add_argument("--project-dir", default="", help="project root (CWD at prompt time)")
    parser.add_argument("--marketplace", default="", help="bootstrap's marketplace name")
    parser.add_argument("--registry", default="", help="installed_plugins.json path")
    args = parser.parse_args(argv)

    registry = args.registry or _default_registry()
    try:
        status = run_harvest(args.data_dir, args.project_dir, registry, args.marketplace)
    except Exception:
        return 0  # best-effort: a harvest failure must never break the prompt
    if status:
        _log_launch(args.data_dir, status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
