"""One-shot repair for the bootstrap "wrong update scope" wedge.

THE DEFECT
----------
bootstrap reads the scope it WANTS from the project manifest::

    {"ref": "plugins-kit:bootstrap", "enabled": true, "scope": "user"}

and passes that to ``claude plugin update <ref> --scope user``. But the plugin
is actually installed at whatever scope the registry records -- commonly::

    {"scope": "project", "projectPath": "C:\\dev\\<project>", "version": "0.57.0"}

The CLI resolves the plugin by scope, does not find it at ``user``, and refuses::

    Failed to update plugin "bootstrap@plugins-kit":
    Plugin "bootstrap" is not installed at scope user

Updating is orthogonal to scope: the plugin needs updating where it LIVES, not
where the manifest wishes it lived. Asking for the manifest's scope is the bug.

WHY IT IS UNRECOVERABLE WITHOUT THIS SCRIPT
-------------------------------------------
The failure is self-masking in the worst way: it blocks the delivery of its own
fix. A corrected bootstrap can be published, but the affected machine can never
install it, because installing it is the exact operation that fails. The machine
reports the same error every session, forever, and stays on the old engine --
so every LATER bootstrap fix is stranded behind this one too.

``ensure_registry_scope`` cannot help. It deliberately refuses to touch any
record carrying ``projectPath``, because stamping a scope onto one manufactures
the user-scope+projectPath chimera that repair_registry.py exists to clean up
(claude-code#79892). That refusal is correct -- the registry record here is
well-formed and a genuine per-project install is legitimate. There is nothing
to repair IN the registry; the wedge is in which scope bootstrap asks for.

Hence this ships in the same separate, dependency-free plugin: it has no prior
version to be wedged on, so it runs current code on the first session.

WHAT IT DOES -- deliberately narrow
-----------------------------------
    exactly one bootstrap record, installed version < marketplace version
        -> `claude plugin update bootstrap@plugins-kit --scope <recorded scope>`

Explicitly NOT done:
  * No registry edit. The record is well-formed; this reads it, never writes it.
  * No version is forced or chosen -- the CLI picks, exactly as on a healthy
    machine. This only corrects the scope the request is made at.
  * Nothing when >1 record exists: that is repair_registry.py's duplicate-record
    defect, and acting on an ambiguous registry could deregister a bootstrap.
  * Nothing when already current, so the common path costs two local file reads
    and spawns no subprocess.

This is a TEMPORARY remediation, withdrawn with the rest of this plugin once the
known user population has run it.
"""

import json
import os
import shutil
import subprocess

TARGET_REF = "bootstrap@plugins-kit"
MARKETPLACE = "plugins-kit"
PLUGIN_NAME = "bootstrap"
_UPDATE_TIMEOUT = 180


def marketplace_manifest_path(home=None):
    """Absolute path to the marketplace clone's plugin listing."""
    home = home or os.path.expanduser("~")
    return os.path.join(
        home, ".claude", "plugins", "marketplaces", MARKETPLACE,
        ".claude-plugin", "marketplace.json",
    )


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _version_tuple(value):
    """Parse a dotted version into a comparable tuple; non-numeric parts as 0."""
    parts = []
    for chunk in str(value).split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def latest_version(manifest):
    """Version the marketplace lists for bootstrap, or None if unreadable."""
    if not isinstance(manifest, dict):
        return None
    plugins = manifest.get("plugins")
    if not isinstance(plugins, list):
        return None
    for entry in plugins:
        if isinstance(entry, dict) and entry.get("name") == PLUGIN_NAME:
            version = entry.get("version")
            return version if isinstance(version, str) else None
    return None


def _entries_container(data):
    """Return the dict holding per-ref records, or None if unrecognized.

    Tolerates both ``{"plugins": {...}}`` and a bare top-level mapping, matching
    the registry shapes seen in the wild. Deliberately duplicated from
    repair_registry.py rather than imported: these scripts are loaded by path
    and their directory is kept off sys.path (see the pythonpath note in
    pyproject.toml), so a cross-script import would not resolve under test.
    """
    if not isinstance(data, dict):
        return None
    plugins = data.get("plugins")
    if isinstance(plugins, dict):
        return plugins
    return data if all(isinstance(k, str) for k in data) else None


def plan_update(registry, manifest, ref=TARGET_REF):
    """Decide whether to update and at which scope. Pure -- no I/O, no mutation.

    Returns ``(scope, installed, latest)``, or ``(None, None, None)`` when there
    is nothing to do. Callers must treat the None form as a healthy no-op.
    """
    container = _entries_container(registry)
    if container is None:
        return None, None, None

    records = container.get(ref)
    # Exactly one record: >1 is the duplicate-record defect (repair_registry.py),
    # and picking a record out of an ambiguous set risks deregistering bootstrap.
    if not isinstance(records, list) or len(records) != 1:
        return None, None, None

    record = records[0]
    if not isinstance(record, dict):
        return None, None, None

    scope = record.get("scope")
    installed = record.get("version")
    if not isinstance(scope, str) or not isinstance(installed, str):
        return None, None, None

    newest = latest_version(manifest)
    if not newest or _version_tuple(newest) <= _version_tuple(installed):
        return None, None, None

    return scope, installed, newest


def run_update(scope, claude=None, timeout=_UPDATE_TIMEOUT):
    """Run the scoped update. Returns (ok, detail). Never raises."""
    claude = claude or shutil.which("claude")
    if not claude:
        return False, "claude CLI not found"
    try:
        proc = subprocess.run(
            [claude, "plugin", "update", TARGET_REF, "--scope", scope],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, ""
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    return False, lines[-1].strip() if lines else "update failed"


def marker_path(home=None):
    """Where the repair records that it fired, so the campaign is measurable."""
    home = home or os.path.expanduser("~")
    return os.path.join(home, ".claude", "bootstrap-stuck-fix-update.json")


def main(argv=None):
    """Entry point. Always exits 0 -- a remediation must never break a session."""
    argv = argv or []
    dry_run = "--dry-run" in argv

    def _opt(name):
        if name in argv:
            idx = argv.index(name)
            if idx + 1 < len(argv):
                return argv[idx + 1]
        return None

    # --registry / --marketplace let tests and manual runs target copies. On
    # Windows os.path.expanduser reads USERPROFILE, so overriding $HOME in a
    # shell does NOT sandbox this (learned the hard way in repair_registry).
    reg_path = _opt("--registry") or os.path.join(
        os.path.expanduser("~"), ".claude", "plugins", "installed_plugins.json"
    )
    mkt_path = _opt("--marketplace") or marketplace_manifest_path()

    scope, installed, newest = plan_update(_load_json(reg_path), _load_json(mkt_path))
    if not scope:
        return 0

    if dry_run:
        print(
            f"bootstrap-stuck-fix: would update bootstrap {installed} -> {newest} "
            f"at {scope} scope"
        )
        return 0

    ok, detail = run_update(scope)
    if not ok:
        # Visible rather than silent: a persistent failure here means the
        # machine stays wedged, which is exactly what nobody noticed last time.
        print(
            f"bootstrap-stuck-fix: could not update bootstrap {installed} -> "
            f"{newest} at {scope} scope -- {detail}"
        )
        return 0

    print(
        f"bootstrap-stuck-fix: updated bootstrap {installed} -> {newest} at "
        f"{scope} scope -- restart to load it"
    )
    try:
        with open(marker_path(), "w", encoding="utf-8") as fh:
            json.dump(
                {"from": installed, "to": newest, "scope": scope}, fh, indent=2
            )
            fh.write("\n")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
