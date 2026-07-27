"""Self-repair for the malformed "chimera" record in Claude Code's plugin registry.

THE DEFECT
----------
``~/.claude/plugins/installed_plugins.json`` can end up holding TWO records
under one ref:

    {"scope": "user", "projectPath": "D:\\dev\\env-config", "version": "0.45.0", ...}
    {"scope": "user",                                       "version": "0.52.0", ...}

The first is malformed: a *user*-scope record carrying a ``projectPath``. Claude
Code's trust/adoption flow writes it when a plugin is enabled in a tracked
PROJECT ``.claude/settings.json`` while the plugin wants user scope. A later
``claude plugin install <ref> --scope user`` does not match it and APPENDS
rather than replacing, leaving two records where healthy plugins have one.

Claude Code's own loader picks ``entries[0]``, so the stale record is the one
that decides which cache dir a plugin loads from. For bootstrap that is fatal
(the old engine runs forever while its log claims it updated); for any other
plugin it silently pins old code. The rule below is therefore applied to EVERY
ref in the registry, not just bootstrap.

WHAT IT DOES -- deliberately narrow
-----------------------------------
Per ref:

    >1 records, at least one scope=="user" WITHOUT projectPath, and one or
    more scope=="user" WITH projectPath
        -> drop the projectPath-bearing user record(s)

Explicitly NOT touched:
  * ``scope: "project"`` records -- a genuine per-project install is legitimate.
  * ``version`` / ``installPath`` -- this removes a malformed duplicate, it does
    not choose which version wins. The surviving record is well-formed
    Claude-Code-authored data.
  * Any ref where no healthy user record would survive: better to leave a
    machine wedged than to deregister its plugin entirely.

Ordering note: Claude Code reads the registry and loads plugins at startup,
BEFORE SessionStart hooks fire. A repair therefore takes effect on the NEXT
session, not the one that performs it.

Historically shipped as the standalone ``bootstrap-stuck-fix`` plugin, which a
wedged machine needed because it had no prior version to be stuck on. Bootstrap
carries the repair natively from 0.62.0.
"""

import json
import os
import shutil

BACKUP_SUFFIX = ".registry-repair.bak"


def default_registry_path(home=None):
    """Absolute path to Claude Code's plugin registry."""
    home = home or os.path.expanduser("~")
    return os.path.join(home, ".claude", "plugins", "installed_plugins.json")


def _entries_container(data):
    """Return the dict holding per-ref records, or None if unrecognized.

    Tolerates both ``{"plugins": {...}}`` and a bare top-level mapping, matching
    the registry shapes seen in the wild.
    """
    if not isinstance(data, dict):
        return None
    plugins = data.get("plugins")
    if isinstance(plugins, dict):
        return plugins
    return data if all(isinstance(k, str) for k in data) else None


def _plan_ref(records):
    """Plan one ref's records. Returns ``(keep, dropped)``; ``([], [])`` = no-op."""
    if not isinstance(records, list) or len(records) < 2:
        return [], []

    def is_user(r):
        return isinstance(r, dict) and r.get("scope") == "user"

    healthy = [r for r in records if is_user(r) and not r.get("projectPath")]
    malformed = [r for r in records if is_user(r) and r.get("projectPath")]

    # Refuse to act unless a well-formed user record will survive.
    if not healthy or not malformed:
        return [], []

    keep = [r for r in records if r not in malformed]
    return keep, malformed


def plan_repair(data):
    """Decide what to remove, registry-wide. Pure -- no I/O, no mutation.

    Returns ``{ref: (keep, dropped)}`` holding only the refs that need repair.
    An empty dict means healthy or unrecognized; callers must treat that as a
    no-op, not an error.
    """
    container = _entries_container(data)
    if container is None:
        return {}
    plan = {}
    for ref, records in container.items():
        keep, dropped = _plan_ref(records)
        if dropped:
            plan[ref] = (keep, dropped)
    return plan


def apply_repair(path, backup=True):
    """Repair the registry at *path* in place. Returns ``{ref: [dropped records]}``.

    Writes atomically so a crash or a concurrent Claude Code write can never
    leave a truncated registry. Only writes when records are actually dropped:
    the registry's mtime arms the SessionStart cooldown's registry-change
    bypass, so a no-op rewrite every pass would re-arm a full bootstrap pass
    every session. An unreadable or unparseable registry is not ours to repair
    -- this runs on every session start and must never break one.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}

    plan = plan_repair(data)
    if not plan:
        return {}

    container = _entries_container(data)
    for ref, (keep, _dropped) in plan.items():
        container[ref] = keep

    if backup:
        try:
            shutil.copy2(path, path + BACKUP_SUFFIX)
        except OSError:
            pass  # A missing backup is not worth aborting the repair.

    from .atomic_write import write_atomic
    try:
        write_atomic(path, json.dumps(data, indent=2) + "\n")
    except OSError:
        return {}

    return {ref: dropped for ref, (_keep, dropped) in plan.items()}


def describe_repair(dropped_by_ref):
    """One-line summary of an apply_repair result, or "" when nothing was dropped."""
    if not dropped_by_ref:
        return ""
    parts = []
    total = 0
    for ref in sorted(dropped_by_ref):
        records = dropped_by_ref[ref]
        total += len(records)
        versions = ", ".join(str(r.get("version")) for r in records)
        parts.append(f"{ref} [{versions}]")
    return (
        f"registry: dropped {total} malformed record(s) - "
        f"{'; '.join(parts)} - takes effect next session"
    )
