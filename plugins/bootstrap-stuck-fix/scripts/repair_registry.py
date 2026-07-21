"""One-shot repair for the bootstrap "stuck version" wedge.

THE DEFECT
----------
Claude Code's plugin registry (``~/.claude/plugins/installed_plugins.json``)
can end up holding TWO records under ``bootstrap@plugins-kit``:

    {"scope": "user", "projectPath": "D:\\dev\\env-config", "version": "0.45.0", ...}
    {"scope": "user",                                       "version": "0.52.0", ...}

The first is malformed: a *user*-scope record carrying a ``projectPath``. It is
written by Claude Code's own trust/adoption flow when a plugin is enabled in a
tracked PROJECT ``.claude/settings.json`` while bootstrap wants user scope.
Later, ``claude plugin install <ref> --scope user`` writes a user/no-projectPath
record, does not match the chimera, and APPENDS rather than replacing -- leaving
two records where healthy plugins have one.

WHY IT IS UNRECOVERABLE WITHOUT THIS SCRIPT
-------------------------------------------
Every registry reader picks ``entries[0]``, which is the stale record:

  * Claude Code's loader runs the SessionStart hook from the OLD cache dir, so
    no newer engine code ever executes.
  * bootstrap's ``check_plugin_version`` sees the old version, "updates" the
    OTHER record, and logs ``updated [old -> new]`` every session -- a lie that
    makes the machine look healthy, so nobody investigates.
  * ``harvest``, whose entire job is "run the newest engine on disk", is
    blinded by the same first-entry pick and concludes it is already current.

So a fix shipped in a newer bootstrap can never run on an affected machine.
That is why this repair ships as a SEPARATE, dependency-free plugin: a brand-new
plugin has no prior version to be wedged on, so it installs and runs current
code on the first session.

Ordering note: Claude Code reads the registry and loads plugins at startup,
BEFORE SessionStart hooks fire. This repair therefore takes effect on the NEXT
session, not the one that runs it.

WHAT IT DOES -- deliberately narrow
-----------------------------------
Only this exact shape is repaired, and only for the bootstrap plugin:

    same ref, >1 records, at least one scope=="user" WITHOUT projectPath,
    and one or more scope=="user" WITH projectPath
        -> drop the projectPath-bearing user record(s)

Explicitly NOT touched:
  * ``scope: "project"`` records -- a genuine per-project install is legitimate.
  * Version fields, installPath, or anything that would FORCE a version. This
    removes a malformed duplicate; it does not choose which version wins. The
    surviving record is well-formed Claude-Code-authored data.
  * Any ref other than bootstrap, and any registry whose shape is unexpected.

If no user/no-projectPath record survives, nothing is removed -- better to leave
the machine wedged than to deregister its bootstrap entirely.

This is a TEMPORARY remediation plugin, intended to be withdrawn once the known
user population has run it. Do not copy its distribution pattern (enablement via
tracked project settings) for durable plugins -- that pattern is what triggers
the malformed record in the first place.
"""

import json
import os
import shutil
import tempfile

TARGET_REF = "bootstrap@plugins-kit"


def registry_path(home=None):
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


def plan_repair(data, ref=TARGET_REF):
    """Decide what to remove. Pure function -- no I/O, no mutation.

    Returns ``(keep, dropped)`` where *dropped* is the list of malformed records
    to remove. ``dropped == []`` means the registry is healthy or unrecognized;
    callers must treat that as a no-op, not an error.
    """
    container = _entries_container(data)
    if container is None:
        return None, []

    records = container.get(ref)
    if not isinstance(records, list) or len(records) < 2:
        return None, []

    def is_user(r):
        return isinstance(r, dict) and r.get("scope") == "user"

    def has_project_path(r):
        return bool(r.get("projectPath"))

    healthy = [r for r in records if is_user(r) and not has_project_path(r)]
    malformed = [r for r in records if is_user(r) and has_project_path(r)]

    # Refuse to act unless a well-formed user record will survive.
    if not healthy or not malformed:
        return None, []

    keep = [r for r in records if r not in malformed]
    return keep, malformed


def apply_repair(path, ref=TARGET_REF, backup=True):
    """Repair *path* in place. Returns the list of dropped records.

    Writes atomically (temp file in the same directory + ``os.replace``) so a
    crash or a concurrent Claude Code write can never leave a truncated
    registry. Idempotent: a clean registry is left byte-identical and untouched.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        # Unreadable or unparseable: not ours to repair. Stay silent -- this
        # runs on every session start and must never break one.
        return []

    keep, dropped = plan_repair(data, ref=ref)
    if not dropped:
        return []

    container = _entries_container(data)
    container[ref] = keep

    if backup:
        try:
            shutil.copy2(path, path + ".bootstrap-stuck-fix.bak")
        except OSError:
            pass  # A missing backup is not worth aborting the repair.

    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return []

    return dropped


def marker_path(home=None):
    """Where the repair records that it fired.

    Without this there is no way to tell whether the campaign actually reached
    anyone before the plugin is withdrawn.
    """
    home = home or os.path.expanduser("~")
    return os.path.join(home, ".claude", "bootstrap-stuck-fix.json")


def main(argv=None):
    """Entry point. Prints a one-line summary only when it actually repaired.

    Always exits 0: this runs on every session start, and a remediation plugin
    that can break a session is worse than the wedge it fixes.
    """
    argv = argv or []
    dry_run = "--dry-run" in argv

    # --registry lets tests and manual runs target a copy. Without it there is
    # no way to exercise this end to end without touching the real registry:
    # on Windows os.path.expanduser reads USERPROFILE, so overriding $HOME in a
    # shell does NOT sandbox it (learned the hard way).
    path = None
    if "--registry" in argv:
        idx = argv.index("--registry")
        if idx + 1 < len(argv):
            path = argv[idx + 1]
    path = path or registry_path()
    if dry_run:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            print("bootstrap-stuck-fix: registry unreadable, nothing to do")
            return 0
        _, dropped = plan_repair(data)
    else:
        dropped = apply_repair(path)

    if not dropped:
        return 0

    versions = ", ".join(str(r.get("version")) for r in dropped)
    verb = "would drop" if dry_run else "dropped"
    print(
        f"bootstrap-stuck-fix: {verb} {len(dropped)} malformed "
        f"registry record(s) [{versions}] -- takes effect next session"
    )

    if not dry_run:
        try:
            marker = os.path.join(os.path.dirname(path), "bootstrap-stuck-fix.json") \
                if path != registry_path() else marker_path()
            with open(marker, "w", encoding="utf-8") as fh:
                json.dump({"dropped": dropped}, fh, indent=2)
                fh.write("\n")
        except OSError:
            pass

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
