#!/usr/bin/env python3
"""bootstrap install-hook -- write the ensure-bootstrap SessionStart hook into a project.

Run by a bootstrap administrator from a project root. It writes two files:

    <project>/.claude/hooks/ensure-bootstrap.sh   rendered from
                                                  templates/ensure-bootstrap.sh
    <project>/.claude/settings.json               one SessionStart entry that
                                                  runs that script

The minimum bootstrap version baked into the hook is the version of the
bootstrap plugin tree running this command -- the administrator's current
version. Re-running replaces the script and the settings entry in place, which
is how the floor is raised after a bootstrap release.

It never touches source control. A read-only target (a Perforce file that is
not opened for edit) is refused before anything is written, and committing or
submitting the result is the administrator's job.

Full procedure: skills/bootstrap/references/fleet-management.md.
Stdlib-only, like every bootstrap lever.
"""

from __future__ import annotations

import json
import os
import sys

HOOK_FILENAME = "ensure-bootstrap.sh"
VERSION_PLACEHOLDER = "@MIN_VERSION@"

# The settings entry is found again on a re-run by this marker in its command.
HOOK_COMMAND = (
    'if [ -f "$CLAUDE_PROJECT_DIR/.claude/bootstrap.json" ] && '
    '[ -f "$CLAUDE_PROJECT_DIR/.claude/hooks/' + HOOK_FILENAME + '" ]; then '
    'bash "$CLAUDE_PROJECT_DIR/.claude/hooks/' + HOOK_FILENAME + '"; fi'
)

# Installing a plugin and updating a marketplace clone are network operations.
HOOK_TIMEOUT_SECONDS = 300


class InstallError(Exception):
    """A refusal the administrator must act on; nothing has been written."""


def plugin_version(plugin_root: str) -> str:
    manifest = os.path.join(plugin_root, ".claude-plugin", "plugin.json")
    try:
        with open(manifest, encoding="utf-8") as fh:
            version = json.load(fh).get("version", "")
    except (OSError, ValueError) as exc:
        raise InstallError("cannot read the bootstrap version from %s: %s" % (manifest, exc))
    if not version:
        raise InstallError("%s has no version field" % manifest)
    return version


def render_hook(plugin_root: str, version: str) -> bytes:
    template = os.path.join(plugin_root, "templates", HOOK_FILENAME)
    try:
        with open(template, "rb") as fh:
            body = fh.read()
    except OSError as exc:
        raise InstallError("cannot read the hook template %s: %s" % (template, exc))
    placeholder = VERSION_PLACEHOLDER.encode("ascii")
    if placeholder not in body:
        raise InstallError("hook template %s has no %s placeholder" % (template, VERSION_PLACEHOLDER))
    return body.replace(placeholder, version.encode("ascii"))


def _is_our_hook(hook) -> bool:
    return isinstance(hook, dict) and HOOK_FILENAME in str(hook.get("command", ""))


def merge_settings(settings: dict) -> dict:
    """Return settings with exactly one ensure-bootstrap SessionStart entry.

    Existing ensure-bootstrap entries are removed wherever they sit, a group
    left empty by that removal is dropped, and one fresh group is appended.
    Every other hook and key is left as it was.
    """
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError("settings.json 'hooks' is not an object")
    groups = hooks.get("SessionStart", [])
    if not isinstance(groups, list):
        raise InstallError("settings.json 'hooks.SessionStart' is not a list")
    kept = []
    for group in groups:
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            remaining = [h for h in group["hooks"] if not _is_our_hook(h)]
            if not remaining and len(remaining) != len(group["hooks"]):
                continue
            group = dict(group, hooks=remaining)
        kept.append(group)
    kept.append({
        "hooks": [{
            "type": "command",
            "command": HOOK_COMMAND,
            "timeout": HOOK_TIMEOUT_SECONDS,
        }]
    })
    hooks["SessionStart"] = kept
    return settings


def _require_writable(path: str) -> None:
    if os.path.exists(path) and not os.access(path, os.W_OK):
        raise InstallError(
            "%s is read-only. Open it for edit first (for Perforce: p4 edit), then re-run." % path)


def install(project_dir: str, plugin_root: str) -> dict:
    """Write the hook and settings entry. Returns a summary for printing."""
    project_dir = os.path.abspath(project_dir)
    claude_dir = os.path.join(project_dir, ".claude")
    if not os.path.isfile(os.path.join(claude_dir, "bootstrap.json")):
        raise InstallError(
            "%s has no .claude/bootstrap.json. The hook only runs in projects that "
            "declare one, so run this from the root of a bootstrap project." % project_dir)

    version = plugin_version(plugin_root)
    hook_body = render_hook(plugin_root, version)
    hook_path = os.path.join(claude_dir, "hooks", HOOK_FILENAME)
    settings_path = os.path.join(claude_dir, "settings.json")

    settings = {}
    settings_text = ""
    if os.path.exists(settings_path):
        try:
            with open(settings_path, encoding="utf-8") as fh:
                settings_text = fh.read()
            settings = json.loads(settings_text)
        except (OSError, ValueError) as exc:
            raise InstallError("cannot parse %s: %s" % (settings_path, exc))
        if not isinstance(settings, dict):
            raise InstallError("%s is not a JSON object" % settings_path)
    new_text = json.dumps(merge_settings(settings), indent=2, ensure_ascii=False) + "\n"

    old_hook = None
    if os.path.exists(hook_path):
        with open(hook_path, "rb") as fh:
            old_hook = fh.read()
    hook_changed = old_hook != hook_body
    settings_changed = new_text != settings_text

    # Check both targets before writing either, so a refusal leaves no half-install.
    if hook_changed:
        _require_writable(hook_path)
    if settings_changed:
        _require_writable(settings_path)

    if hook_changed:
        os.makedirs(os.path.dirname(hook_path), exist_ok=True)
        with open(hook_path, "wb") as fh:
            fh.write(hook_body)
    if settings_changed:
        with open(settings_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(new_text)

    return {
        "min_version": version,
        "hook": hook_path,
        "hook_changed": hook_changed,
        "settings": settings_path,
        "settings_changed": settings_changed,
    }


def main(plugin_root: str, project_dir: str = "") -> int:
    try:
        result = install(project_dir or os.getcwd(), plugin_root)
    except InstallError as exc:
        print("bootstrap install-hook: %s" % exc, file=sys.stderr)
        return 2
    print("Minimum bootstrap version: %s" % result["min_version"])
    for key in ("hook", "settings"):
        state = "written" if result[key + "_changed"] else "unchanged"
        print("  %s: %s" % (state, result[key]))
    if result["hook_changed"] or result["settings_changed"]:
        print("Commit or submit these files so the project's users receive the hook.")
    return 0
