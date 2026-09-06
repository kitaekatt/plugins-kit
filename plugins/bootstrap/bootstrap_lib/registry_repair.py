"""Self-repair for malformed records in Claude Code's plugin registry.

Two distinct malformations are covered, each by its own narrowly-scoped rule:
the "chimera" record (rule 1) and the "orphan project" record (rule 2). They
share the write path, the healthy-survivor discipline, and the reason they
matter -- Claude Code's loader picks ``entries[0]``, so a malformed record at
index 0 decides what loads.

DEFECT 1 -- the chimera record
------------------------------
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

DEFECT 2 -- the orphan project record
-------------------------------------
A record can carry ``scope: "project"`` with NO ``projectPath``:

    {"scope": "project", "version": "0.3.4", "installPath": ...}          <- orphan
    {"scope": "project", "version": "0.3.4", "projectPath": "C:\\dev\\a"}
    {"scope": "project", "version": "0.3.4", "installPath": ...}          <- orphan
    {"scope": "project", "version": "0.3.4", "projectPath": "C:\\dev\\b"}

That shape is malformed by definition: a project-scope install is *defined* by
the project it belongs to, so a project record without a project names no
install. Observed live 2026-07-27 (``engineer@private-plugins``: 4 records, 2
orphans, one of them at index 0; ``prototyping@private-plugins``: 3 records, 1
orphan at index 0). Both refs appeared under "Needs attention" in ``/plugin``
with a "not cached" error while every ref holding a single clean record was
healthy -- a strong correlation, not a proven cause: the orphans' installPaths
existed on disk and named the same version as their well-formed siblings.

Note how this differs from defect 1 in consequence. A chimera pins an OLD
version, so the symptom is stale code running silently. The orphans observed
here agreed with their siblings on both ``version`` and ``installPath``, so
nothing stale can load -- the damage is confined to whatever Claude Code does
when it cannot resolve a project record to a project.

WHAT IT DOES -- deliberately narrow
-----------------------------------
Per ref, rule 1 then rule 2:

  1. >1 records, at least one scope=="user" WITHOUT projectPath, and one or
     more scope=="user" WITH projectPath
         -> drop the projectPath-bearing user record(s)

  2. one or more scope=="project" records WITHOUT a projectPath, and at least
     one record that is not itself such a record
         -> drop the projectPath-less project record(s)

Explicitly NOT touched:
  * Well-formed ``scope: "project"`` records -- a genuine per-project install is
    legitimate, and rule 2 is scoped to the projectPath-LESS ones only. (Before
    rule 2 existed this entry read "``scope: "project"`` records" without
    qualification; that blanket exclusion is what rule 2 narrows.)
  * ``version`` / ``installPath`` -- both rules remove malformed duplicates,
    neither chooses which version wins. Surviving records are well-formed
    Claude-Code-authored data.
  * Any ref where no record would survive: better to leave a machine wedged
    than to deregister its plugin entirely. Such refs are reported by
    ``find_unrepairable`` so the skip is logged rather than silent.
  * Duplicate records that are *well-formed*. Deduping identical
    ``(scope, projectPath, version, installPath)`` tuples was considered and
    declined: equality on those four fields does not imply equality of the
    whole record, so a dedupe would silently pick a winner among records that
    may differ in fields this module does not model -- exactly the
    version-choosing behavior both rules refuse. It is also unnecessary for the
    observed defect, whose duplicates are all orphans that rule 2 already
    removes. Duplicates are left in place by design.

Ordering note: Claude Code reads the registry and loads plugins at startup,
BEFORE SessionStart hooks fire. A repair therefore takes effect on the NEXT
session, not the one that performs it.

Historically shipped as the standalone ``bootstrap-stuck-fix`` plugin, which a
wedged machine needed because it had no prior version to be stuck on. Bootstrap
carries the repair natively from 0.62.0.

WHY RULE 2 SHIPS IN BOOTSTRAP, NOT bootstrap-stuck-fix
------------------------------------------------------
The escape-hatch test in CLAUDE.md asks: would this change have to be installed
by the thing it repairs? For the observed defect, no. The affected refs are
``engineer`` / ``prototyping``; bootstrap's own record is well-formed, so
bootstrap installs, updates, and runs normally and can carry the fix. Rule 1's
history is the contrast -- it repaired *bootstrap's own* wedge, which is why it
needed a standalone plugin first.

If this shape ever lands on the bootstrap ref itself, that reasoning inverts:
a ref Claude Code will not load is a ref whose SessionStart hook never fires,
so bootstrap could not repair itself and the remediation would have to move to
``bootstrap-stuck-fix``. That is not hypothetical -- bootstrap's live record on
the machine this was diagnosed from is itself ``scope: "project"``, one
mis-timed ``claude plugin install`` away from the same shape. It is
deliberately NOT pre-emptively mirrored into bootstrap-stuck-fix: that plugin's
repair is hard-scoped to a single ref and its own narrowness discipline argues
against widening it for a defect nobody has observed there. The trigger to
revisit is a bootstrap ref found carrying an orphan record.
"""

import json
import os
import shutil

BACKUP_SUFFIX = ".registry-repair.bak"


class RepairResult(dict):
    """Mapping of repaired refs with an optional write failure."""

    def __init__(self, *args, error: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.error = error


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


def _is_orphan_project(record):
    """True for a ``scope: "project"`` record naming no project (defect 2).

    An absent, null, or empty ``projectPath`` all qualify -- each leaves the
    record unable to name the install it claims to scope.
    """
    return (
        isinstance(record, dict)
        and record.get("scope") == "project"
        and not record.get("projectPath")
    )


def _plan_ref_orphan_project(records):
    """Rule 2. Returns ``(keep, dropped)``; ``([], [])`` = no-op.

    Kept separate from ``_plan_ref`` rather than folded into it: the two rules
    match different shapes AND apply different survivor guards (rule 1 requires
    a healthy *user* record to survive, rule 2 accepts any non-orphan record),
    so a merged predicate would blur two independent narrowness contracts.
    """
    if not isinstance(records, list):
        return [], []

    dropped = [r for r in records if _is_orphan_project(r)]
    if not dropped:
        return [], []

    keep = [r for r in records if not _is_orphan_project(r)]
    # Healthy-survivor guard: never deregister a plugin outright.
    if not keep:
        return [], []

    return keep, dropped


def _plan_ref(records):
    """Rule 1. Returns ``(keep, dropped)``; ``([], [])`` = no-op."""
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
        current, dropped = records, []
        for rule in (_plan_ref, _plan_ref_orphan_project):
            keep, removed = rule(current)
            if removed:
                current, dropped = keep, dropped + removed
        if dropped:
            plan[ref] = (current, dropped)
    return plan


def find_unrepairable(data):
    """Refs holding ONLY orphan-project records. Pure -- no I/O, no mutation.

    Returns ``{ref: orphan_count}``. These are refs rule 2 matched but refused
    to act on, because dropping the orphans would leave the ref with zero
    records and deregister the plugin. Reported so the healthy-survivor guard
    logs a skip instead of failing silently -- a silent bootstrap operation is
    a bug (engine-internals.md, "Every check must log its outcome").
    """
    container = _entries_container(data)
    if container is None:
        return {}
    skipped = {}
    for ref, records in container.items():
        if not isinstance(records, list) or not records:
            continue
        orphans = [r for r in records if _is_orphan_project(r)]
        if orphans and len(orphans) == len(records):
            skipped[ref] = len(orphans)
    return skipped


def load_registry(path):
    """Parse the registry at *path*, or return ``None`` if unreadable.

    Mirrors ``apply_repair``'s tolerance: this runs on every session start and
    an unreadable registry is not ours to repair.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def apply_repair(path, backup=True):
    """Repair the registry at *path* in place. Returns ``{ref: [dropped records]}``.

    Writes atomically so a crash or a concurrent Claude Code write can never
    leave a truncated registry. Only writes when records are actually dropped:
    the registry's mtime arms the SessionStart cooldown's registry-change
    bypass, so a no-op rewrite every pass would re-arm a full bootstrap pass
    every session. An unreadable or unparseable registry is not ours to repair
    -- this runs on every session start and must never break one.
    """
    data = load_registry(path)
    if data is None:
        return RepairResult()

    plan = plan_repair(data)
    if not plan:
        return RepairResult()

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
    except OSError as exc:
        return RepairResult(error=f"could not write registry: {exc}")

    return RepairResult({ref: dropped for ref, (_keep, dropped) in plan.items()})


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


def describe_unrepairable(skipped_by_ref):
    """One-line summary of a ``find_unrepairable`` result, or "" when empty."""
    if not skipped_by_ref:
        return ""
    parts = [f"{ref} ({skipped_by_ref[ref]})" for ref in sorted(skipped_by_ref)]
    return (
        f"registry: left {len(parts)} ref(s) alone - only malformed records "
        f"present, dropping them would deregister the plugin - {'; '.join(parts)}"
    )
