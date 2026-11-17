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

A self-registered entry fills a gap and must never outrank a declaration.
Merge-by-ref lets the user-local layer override a tracked layer, so an entry
written here before a tracked (or project) layer declared the same ref would
otherwise shadow that declaration for ever -- e.g. ``install: "auto"`` read as
``"manual"``. Two halves close it. Entries are written with
``"self_registered": true`` (:func:`is_self_registered` also accepts the exact
``{"ref", "install": "manual"}`` shape that earlier releases wrote unmarked,
so machines that already carry those entries converge). At load time the
engine drops such an entry from the user-local layer whenever another layer
declares the ref (:func:`drop_shadowed_self_registrations`), so the
declaration applies in the same pass; and when the user layer
(``~/.claude/bootstrap.json``) declares it, :func:`ensure_self_registration`
removes it from the file, logging an action entry. A project layer never
removes it: the file serves every project, so that removal was undone by the
next pass in another project. A hand-written entry (any
other shape) is never removed and still overrides as before.

A redirected test session (``CLAUDE_BOOTSTRAP_DATA_ROOT``) skips
self-registration entirely: the engine owns the decision, see Step 4b2.
"""

import json
import os

from .atomic_write import write_atomic
from .records import Entry


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


SELF_REGISTERED_KEY = "self_registered"


def is_self_registered(entry):
    """True when a ``plugins[]`` entry was written by self-registration.

    Marked entries carry ``"self_registered": true``. The exact unmarked shape
    ``{"ref": <str>, "install": "manual"}`` is what releases before the marker
    wrote, and is accepted too so those machines converge. Only meaningful for
    entries in the user-local file; callers must not apply it to other layers.
    """
    if not isinstance(entry, dict) or not isinstance(entry.get("ref"), str):
        return False
    if entry.get(SELF_REGISTERED_KEY) is True:
        return True
    return set(entry) == {"ref", "install"} and entry["install"] == "manual"


def _is_declared(ref, refs, names):
    name = ref.split(":", 1)[1] if ":" in ref else ref
    return ref in refs or name in names


def drop_shadowed_self_registrations(local_manifest, other_manifests):
    """Return ``local_manifest`` without self-registered entries another layer declares.

    ``other_manifests`` are the parsed contents of every other manifest layer
    (``None`` entries are ignored). The input is not mutated; when nothing is
    dropped it is returned unchanged.
    """
    if not isinstance(local_manifest, dict):
        return local_manifest
    plugins = local_manifest.get("plugins")
    if not isinstance(plugins, list):
        return local_manifest
    refs, names = set(), set()
    for other in other_manifests:
        if isinstance(other, dict):
            r, n = declared_plugin_ids(other)
            refs |= r
            names |= n
    kept = [e for e in plugins
            if not (is_self_registered(e) and _is_declared(e["ref"], refs, names))]
    if len(kept) == len(plugins):
        return local_manifest
    return {**local_manifest, "plugins": kept}


def _refusal(actions, oks, missing):
    """A malformed local file blocks only a pass that has something to add.

    With nothing missing the refusal is demoted to a verbose ok entry: the
    layered-manifest loader already reports the parse failure, and a stale
    self-registered entry cannot be pruned from a file nobody can read.
    """
    if not missing:
        oks.extend(actions)
        actions.clear()
    return actions, oks


def ensure_self_registration(local_path, candidate_refs, declared_refs,
                             declared_names, layer_declared_refs=frozenset(),
                             layer_declared_names=frozenset()):
    """Ensure every candidate plugin ref has an auto-update entry somewhere.

    Args:
        local_path: Path to ``~/.claude/bootstrap.local.json`` (the write
            target for missing entries).
        candidate_refs: Full ``mkt:name`` refs of installed plugins that ship
            a ``bootstrap.json`` (i.e. are bootstrap-dependent).
        declared_refs: Full refs already declared in any processed manifest.
        declared_names: Bare plugin names already declared.
        layer_declared_refs, layer_declared_names: Refs and bare names declared
            by the user layer (``~/.claude/bootstrap.json``; the engine
            passes no project layer). A self-registered
            entry in the file whose ref is declared there is removed (it would
            shadow that declaration under merge-by-ref).

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
    if not missing and not os.path.isfile(local_path):
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
            actions.append(Entry(
                f"self-register: {local_path} unreadable ({e}); "
                "left untouched, no entries added",
                short="self-register: local file unreadable"))
            return _refusal(actions, oks, missing)
        if not isinstance(data, dict):
            actions.append(Entry(
                f"self-register: {local_path} is not a JSON object; "
                "left untouched, no entries added",
                short="self-register: local file not JSON"))
            return _refusal(actions, oks, missing)

    plugins = data.get("plugins")
    if plugins is None:
        plugins = []
    if not isinstance(plugins, list):
        actions.append(Entry(
            f"self-register: {local_path} 'plugins' is not a list; "
            "left untouched, no entries added",
            short="self-register: bad 'plugins' list"))
        return _refusal(actions, oks, missing)

    # A self-registered entry that another layer now declares would shadow
    # that declaration under merge-by-ref: remove it.
    removed = [e["ref"] for e in plugins
               if is_self_registered(e)
               and _is_declared(e["ref"], layer_declared_refs,
                                layer_declared_names)]
    if removed:
        plugins = [e for e in plugins
                   if not (is_self_registered(e) and e["ref"] in removed)]

    # Belt over the merged-layer check: skip refs the file itself already
    # declares (in case the caller's declared set was built from stale layers).
    file_refs, file_names = declared_plugin_ids({"plugins": plugins})
    added = []
    for ref in missing:
        if ref in file_refs or ref.split(":", 1)[1] in file_names:
            continue
        if _is_declared(ref, layer_declared_refs, layer_declared_names):
            continue
        plugins.append({"ref": ref, "install": "manual",
                        SELF_REGISTERED_KEY: True})
        added.append(ref)

    if not added and not removed:
        if missing:
            oks.append("self-register: entries already present")
        else:
            oks.append(
                f"self-register: all {len(candidates)} bootstrap-dependent "
                "plugins have auto-update entries")
        return actions, oks

    data["plugins"] = plugins
    write_atomic(local_path, json.dumps(data, indent=2) + "\n")
    for ref in removed:
        actions.append(Entry(
            f"self-register: removed {ref} from {local_path} "
            "(declared in ~/.claude/bootstrap.json; the self-registered entry would "
            "shadow that declaration)",
            short=f"self-register: removed {ref}"))
    for ref in added:
        actions.append(Entry(
            f"self-register: added {ref} to {local_path} "
            "(install: manual -- auto-update only, never auto-install)",
            short=f"self-register: added {ref}"))
    return actions, oks
