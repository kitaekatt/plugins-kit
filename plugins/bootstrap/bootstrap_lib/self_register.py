"""Self-registration of bootstrap-dependent plugins for auto-update.

Problem this closes: the engine's plugin phase (``_phase_plugins``) only
manages plugins DECLARED in a manifest's ``plugins[]`` list. On a typical
user machine the only declarations are bootstrap's own self-entry and
explicit dependency edges (e.g. awesome-kit -> skills-kit). Every other
installed plugin was never bootstrap-updated: it fell back to Claude Code's
own autoUpdate, which reads the marketplace clone at session start BEFORE
bootstrap's SessionStart pass refreshes it -- so a publish always converged
one restart late (observed live 2026-07-20: skills-kit, declared, updated
in-session two seconds after the publish; hue-kit, undeclared, stayed on the
prior version).

Fix (opinionated stance): every installed plugin that ships a
``bootstrap.json`` self-registers for auto-update. After the per-plugin pass,
the engine collects the processed plugins; any that have no ``plugins[]``
entry anywhere (merged user/project layers or another plugin's manifest) get
``{"ref": "<mkt>:<name>", "install": "manual"}`` appended to
``~/.claude/bootstrap.local.json``.

Why ``install: "manual"``: that mode updates a plugin only while it is
installed and NEVER installs it -- so an uninstalled plugin stays
uninstalled. Uninstalling is the one supported opt-out; a deliberately
deleted entry is re-added on the next pass (user decision 2026-07-20: no
tombstones, no ignore-list).

Why ``bootstrap.local.json`` (gitignored, per-machine) and not the tracked
``~/.claude/bootstrap.json``: re-registration is unconditional, so writing
the tracked file would let every machine rewrite a git-synced file on every
pass with no way to stop it short of uninstalling -- the permanently-dirty
shared-file failure mode. The local file gives identical behavior with each
machine self-healing independently; nothing enters git.

The file is real user config, not ours: unrelated keys and existing
``plugins[]`` entries are preserved; a file that fails to parse is left
untouched (the layered-manifest loader already surfaces the parse failure).
Entries written here take effect from the NEXT pass -- the plugin was just
provisioned this pass, so nothing is stale in the gap.
"""

import json
import os

from .atomic_write import write_atomic


def declared_plugin_ids(manifest):
    """Collect plugin identities declared in a manifest's ``plugins[]`` list.

    Returns ``(full_refs, bare_names)`` -- e.g. an entry
    ``{"ref": "plugins-kit:skills-kit"}`` contributes
    ``"plugins-kit:skills-kit"`` to *full_refs* and ``"skills-kit"`` to
    *bare_names*; a bare ``{"ref": "bootstrap"}`` contributes only to
    *bare_names*.
    """
    refs, names = set(), set()
    plugins = (manifest or {}).get("plugins") or []
    if not isinstance(plugins, list):
        return refs, names
    for entry in plugins:
        if not isinstance(entry, dict):
            continue
        ref = entry.get("ref") or ""
        if not ref:
            continue
        if ":" in ref:
            refs.add(ref)
            names.add(ref.split(":", 1)[1])
        else:
            names.add(ref)
    return refs, names


def ensure_self_registration(local_path, candidate_refs, declared_refs,
                             declared_names):
    """Ensure every candidate plugin ref has an auto-update entry somewhere.

    Args:
        local_path: Path to ``~/.claude/bootstrap.local.json`` (the write
            target for missing entries).
        candidate_refs: Full ``mkt:name`` refs of installed plugins that ship
            a ``bootstrap.json`` (i.e. are bootstrap-dependent).
        declared_refs: Full refs already declared in any processed manifest.
        declared_names: Bare plugin names already declared.

    Returns:
        ``(action_entries, ok_entries)`` -- actions only when the file was
        written (or refused); ok entries are verbose-only steady-state noise.
        Never raises on a malformed local file; it is left untouched.
    """
    actions, oks = [], []
    candidates = sorted(set(candidate_refs))
    missing = [
        ref for ref in candidates
        if ref not in declared_refs
        and ref.split(":", 1)[1] not in declared_names
    ]
    if not missing:
        oks.append(
            f"self-register: all {len(candidates)} bootstrap-dependent "
            "plugins have auto-update entries")
        return actions, oks

    data = {}
    if os.path.isfile(local_path):
        try:
            with open(local_path, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            actions.append(
                f"self-register: {local_path} unreadable ({e}); "
                "left untouched, no entries added")
            return actions, oks
        if not isinstance(data, dict):
            actions.append(
                f"self-register: {local_path} is not a JSON object; "
                "left untouched, no entries added")
            return actions, oks

    plugins = data.get("plugins")
    if plugins is None:
        plugins = []
    if not isinstance(plugins, list):
        actions.append(
            f"self-register: {local_path} 'plugins' is not a list; "
            "left untouched, no entries added")
        return actions, oks

    # Belt over the merged-layer check: skip refs the file itself already
    # declares (in case the caller's declared set was built from stale layers).
    file_refs, file_names = declared_plugin_ids({"plugins": plugins})
    added = []
    for ref in missing:
        if ref in file_refs or ref.split(":", 1)[1] in file_names:
            continue
        plugins.append({"ref": ref, "install": "manual"})
        added.append(ref)

    if not added:
        oks.append("self-register: entries already present")
        return actions, oks

    data["plugins"] = plugins
    write_atomic(local_path, json.dumps(data, indent=2) + "\n")
    for ref in added:
        actions.append(
            f"self-register: added {ref} to {local_path} "
            "(install: manual -- auto-update only, never auto-install)")
    return actions, oks
