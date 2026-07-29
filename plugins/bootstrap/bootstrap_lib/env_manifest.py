"""env.json core: layered loading, machine identity, and the env gate.

env.json is the identity-bearing sibling of bootstrap.json
(bootstrap-env-refactor spec, sections 4.1/4.2/4.4): same engine, same
SessionStart pass, same layered-manifest model -- but its entries may be
keyed by hostname as well as OS, and processing it on a machine whose
hostname is not declared in its ``machines`` registry is a hard error.

This module owns the mechanics the engine's env pass (engine.py Step 3e)
builds on:

- **Four layers** (spec 4.1), lowest priority first: ``~/.claude/env.json``
  (the primary tracked home), ``~/.claude/env.local.json``,
  ``<project>/.claude/env.json``, ``<project>/.claude/env.local.json``.
  Merged with :func:`manifest_merge.merge_env_manifests` (identity-keyed
  array union, dict deep-merge, null-is-absent).
- **Machine identity** (spec 4.2): the ``machines`` registry keys are
  hostnames; the current hostname resolves exact-match-first, then by the
  domain-stripped short form (terminalcolor-init's precedent -- one rule).
  Unknown machine, a declared-vs-detected os mismatch, a non-list
  ``os``/``hosts`` filter value, and a ``hosts`` filter naming an
  unregistered hostname are all hard errors surfaced by the engine; there
  are no fallbacks.
- **The env gate** (spec 4.4): a dedicated stamp, ``env_state.json`` in
  bootstrap's data dir, records the sha256 of the canonical merged
  manifest, the engine version, and the last result. The env phase runs
  only on first run / merged-hash change / non-clean last result / engine
  version change / explicit reset (scripts/env-reset-cooldown.sh deletes
  the stamp); a clean, unchanged pass is skipped. The stamp is independent
  of the per-project bootstrap cooldown and is the ONLY gate for the phase.

Backwards-readable evolution (spec 4.5): v1 is the canonical form; unknown
keys are ignored with a verbose log line (the engine's job), user files are
never rewritten on disk.
"""

import hashlib
import json
import os
import socket
import time

from .manifest_merge import merge_env_manifests
from .stamps import global_stamp

# The env gate stamp filename, in bootstrap's data dir. Also known to
# scripts/env-reset-cooldown.sh (shared name convention, like the cooldown's
# bash/Python path convention in stamps.py).
ENV_STATE_STAMP = "env_state.json"

# Periodic re-check TTL: a clean, unchanged pass reopens the gate once the
# stamp is older than this. Bounds the out-of-band-drift window (remote repo
# drift seen by env_checks like repo-sync, hand-edited rc lines) that the
# hash/result/version triggers can never observe.
ENV_STATE_MAX_AGE_SECONDS = 24 * 60 * 60


# ---------------------------------------------------------------------------
# Layered loading
# ---------------------------------------------------------------------------

def env_manifest_paths(project_dir):
    """The four env.json layer paths, lowest priority first (spec 4.1)."""
    home = os.environ.get("HOME") or os.path.expanduser("~")
    claude_home = os.path.join(home, ".claude")
    paths = [
        os.path.join(claude_home, "env.json"),
        os.path.join(claude_home, "env.local.json"),
    ]
    if project_dir:
        project_claude = os.path.join(project_dir, ".claude")
        paths.append(os.path.join(project_claude, "env.json"))
        paths.append(os.path.join(project_claude, "env.local.json"))
    return paths


def load_layered_env_manifests(project_dir):
    """Load and merge the four env.json layers.

    Returns ``(merged_manifest, parse_errors)`` where parse_errors is a list
    of ``{"path": <path>, "error": <message>}`` dicts. Layers that fail to
    parse are skipped (the merge continues with the rest); the engine
    surfaces each parse error as a persistent failure and keeps the gate
    open until it is fixed. Mirrors engine._load_layered_manifests.
    """
    merged = {}
    parse_errors = []
    for path in env_manifest_paths(project_dir):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r") as f:
                layer = json.load(f)
        except json.JSONDecodeError as e:
            parse_errors.append({"path": path, "error": f"JSON parse error: {e}"})
            continue
        except OSError as e:
            parse_errors.append({"path": path, "error": f"read error: {e}"})
            continue
        merged = merge_env_manifests(merged, layer)
    return merged, parse_errors


def canonical_manifest_hash(merged):
    """sha256 hex of the canonical merged manifest.

    Canonical form: JSON with sorted keys and compact separators, over the
    FULL merged dict (unknown keys included) -- so "modified" means the
    merged content changed in any way, covering edits, additions, and
    removals of any layer file (spec 4.4).
    """
    canonical = json.dumps(merged, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Machine identity (spec 4.2)
# ---------------------------------------------------------------------------

def current_hostname():
    """The hostname used for machines-registry resolution."""
    return socket.gethostname()


def resolve_machine(machines, hostname):
    """Resolve ``hostname`` against the machines registry.

    Exact key match first, then the domain-stripped short form
    (``hostname.split(".", 1)[0]``). Returns the matching registry KEY, or
    ``None`` when the machine is unknown (the caller hard-errors -- no
    fallback, no guessing).
    """
    if hostname in machines:
        return hostname
    short = hostname.split(".", 1)[0]
    if short in machines:
        return short
    return None


def requires_satisfied(requires, machine_entry):
    """Evaluate a tools[] ``requires`` mapping against a machine entry.

    ``requires`` is a bootstrap.json tools[] targeting object mapping
    machine-attribute name -> expected value; ``machine_entry`` is the
    resolved machine's dict from the env.json ``machines`` registry. The
    tool applies iff EVERY pair holds (conjunction; an empty mapping is
    trivially satisfied). Per pair:

    - ``true``  -> the machine entry must carry the key AND it is truthy.
    - ``false`` -> the machine entry must omit the key, or it is falsy.
    - any other value -> ``machine_entry.get(key) == expected``.

    The boolean forms use ``is True`` / ``is False`` deliberately: JSON
    ``true`` must not be satisfied by an attribute that merely equals ``1``
    by Python's bool/int coercion -- boolean requires mean presence+truth,
    equality requires mean equality.
    """
    for key, expected in requires.items():
        actual = machine_entry.get(key)
        if expected is True:
            if not actual:
                return False
        elif expected is False:
            if actual:
                return False
        elif actual != expected:
            return False
    return True


class MachineRequiresResolver:
    """Lazy, memoized machine identity for the tools[] ``requires`` gate.

    The bootstrap tools phase (engine Step 3c) runs BEFORE the env pass
    (Step 3e), and the ``env_state.json`` stamp gates that PASS, not
    identity -- so a tool entry carrying ``requires`` needs its own identity
    lookup. Construction is free of I/O: the env.json layers are loaded and
    the hostname resolved only on the first ``resolve()`` call, so a
    manifest with no ``requires`` key anywhere never touches env.json at
    all (fresh/standalone machines and projects without an env.json keep
    working exactly as before). The outcome is memoized: one lookup per
    pass, and every requires-bearing tool sees the same answer.

    ``resolve()`` returns ``(machine_key, machine_entry, error)``: on
    success ``error`` is None; on failure the first two are None and
    ``error`` says why identity could not be established (no machines
    registry, unresolvable hostname, malformed entry). Turning that into a
    hard failure is the CALLER's job -- this class only reports; no
    fallbacks, no guessing.
    """

    def __init__(self, project_dir):
        self.project_dir = project_dir
        self._resolved = False
        self._outcome = (None, None, None)

    def resolve(self):
        if self._resolved:
            return self._outcome
        self._resolved = True
        merged, parse_errors = load_layered_env_manifests(self.project_dir)
        machines = merged.get("machines")
        if not isinstance(machines, dict) or not machines:
            # Missing env.json is only an error because a `requires` needs
            # it; mention any unparseable layer, since a broken file and a
            # missing one look identical from here.
            detail = ""
            if parse_errors:
                broken = ", ".join(pe["path"] for pe in parse_errors)
                detail = f" (unparseable layer(s): {broken})"
            self._outcome = (
                None, None,
                f"no 'machines' registry found in the env.json layers{detail}",
            )
            return self._outcome
        hostname = current_hostname()
        key = resolve_machine(machines, hostname)
        if key is None:
            known = ", ".join(sorted(machines))
            self._outcome = (
                None, None,
                f"machine '{hostname}' is not in the env.json 'machines' "
                f"registry (known: {known})",
            )
            return self._outcome
        entry = machines[key]
        if not isinstance(entry, dict):
            self._outcome = (
                None, None,
                f"machines entry '{key}' must be an object, got "
                f"{type(entry).__name__}",
            )
            return self._outcome
        self._outcome = (key, entry, None)
        return self._outcome


def validate_entry_filters(merged, machines):
    """Validate every entry-level ``os``/``hosts`` filter in the manifest.

    Two rules, both section-agnostic (walks every list-valued section of the
    merged manifest, so filters in sections this engine version does not yet
    handle are still validated -- filters are manifest facts, not section
    semantics):

    - **Shape**: a present ``os`` or ``hosts`` filter must be a list. A
      scalar (e.g. ``"os": "macos"``) would silently fall into Python
      substring semantics under ``in`` -- a validation failure, never a
      guess.
    - **Registry** (typo protection, spec 4.2): every hostname referenced by
      any entry's ``hosts`` filter must exist as a ``machines`` key.

    Returns a list of descriptive error strings (empty when valid).
    """
    errors = []
    known = ", ".join(sorted(machines))
    for section in sorted(merged):
        value = merged[section]
        if section == "machines" or not isinstance(value, list):
            continue
        for entry in value:
            if not isinstance(entry, dict):
                continue
            label = entry.get("name", entry.get("id", "(unnamed)"))
            bad_shape = False
            for filt in ("os", "hosts"):
                filter_value = entry.get(filt)
                if filter_value is not None and not isinstance(filter_value, list):
                    bad_shape = True
                    errors.append(
                        f"{section} entry '{label}': '{filt}' filter must be "
                        f"a list, got {type(filter_value).__name__} "
                        f"{filter_value!r}. Write \"{filt}\": [...]."
                    )
            if bad_shape:
                continue
            hosts = entry.get("hosts")
            if not hosts:
                continue
            unregistered = [str(h) for h in hosts if h not in machines]
            if unregistered:
                errors.append(
                    f"{section} entry '{label}': hosts filter names "
                    f"unregistered machine(s) {', '.join(unregistered)}. "
                    f"Known machines: {known}. Fix the hostname or add the "
                    f"machine to the env.json 'machines' registry."
                )
    return errors


def entry_applies(entry, current_os, machine_key):
    """Apply the generic entry-level ``os``/``hosts`` filters (spec 4.2).

    Omitted = applies everywhere env.json applies; both present =
    intersection. ``machine_key`` is the machines-registry key the current
    hostname resolved to (hosts filters name registry keys).
    """
    os_filter = entry.get("os")
    if os_filter is not None and current_os not in os_filter:
        return False
    hosts_filter = entry.get("hosts")
    if hosts_filter is not None and machine_key not in hosts_filter:
        return False
    return True


# ---------------------------------------------------------------------------
# The env gate (spec 4.4)
# ---------------------------------------------------------------------------

def read_env_state(data_dir):
    """Read the env stamp. Returns the state dict, or ``None`` when absent.

    An unreadable/corrupt stamp is treated as absent: the stamp is
    engine-owned state, and "absent" simply reopens the gate (the reset
    semantics), converging back to a valid stamp on the next pass.
    """
    raw = global_stamp(data_dir, ENV_STATE_STAMP).read()
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(state, dict):
        return None
    return state


def env_gate_reason(state, manifest_hash, engine_version, stamp_age=None):
    """Why the env phase must run this pass, or ``None`` to skip.

    The phase RUNS iff any of (spec 4.4): no stamp (first run, which an
    explicit reset recreates by deleting the stamp); the merged-manifest
    hash changed; the last result was not clean (any failure -- including
    needs_elevation -- re-runs the phase every session until green); the
    engine version changed (new features may reinterpret entries); or the
    stamp is older than ENV_STATE_MAX_AGE_SECONDS (``stamp_age`` in seconds,
    from :func:`env_state_age`; ``None`` disables the age trigger).
    """
    if state is None:
        return "first run (no env stamp)"
    if state.get("manifest_sha256") != manifest_hash:
        return "env.json modified (merged-manifest hash changed)"
    if state.get("last_result") != "clean":
        return f"last pass result was '{state.get('last_result')}'"
    if state.get("engine_version") != engine_version:
        return (
            f"engine updated "
            f"({state.get('engine_version')} -> {engine_version})"
        )
    if stamp_age is not None and stamp_age >= ENV_STATE_MAX_AGE_SECONDS:
        return f"periodic re-check (last pass {stamp_age / 3600:.0f}h ago)"
    return None


def env_state_age(data_dir):
    """Seconds since the env stamp was last written, or ``None`` when the
    stamp is missing/unreadable (those states already reopen the gate)."""
    mtime = global_stamp(data_dir, ENV_STATE_STAMP).mtime()
    if mtime is None:
        return None
    return max(0.0, time.time() - mtime)


def write_env_state(data_dir, manifest_hash, engine_version, result):
    """Stamp the pass outcome. ``result`` is ``"clean"`` or ``"failed"``."""
    state = {
        "manifest_sha256": manifest_hash,
        "engine_version": engine_version,
        "last_result": result,
    }
    global_stamp(data_dir, ENV_STATE_STAMP).write(json.dumps(state, sort_keys=True))
