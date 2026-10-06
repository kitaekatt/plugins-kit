"""What makes a fix-queue record runnable: one predicate, two call sites.

The elevated fix queue (``<data_dir>/elevate/queue.json``) holds deferred
operations from every project ("origin") a pass ran in. A stored record does
harm only when :mod:`bootstrap_lib.fix_runner` EXECUTES it, and two paths reach
the runner: ``--fix-all`` (the engine rewrites the queue, then launches it) and
the hand-run ``bootstrap-fix`` shim (reads queue.json as last written, no
rewrite). So correctness is owned by ONE predicate evaluated at BOTH:

  * :func:`bootstrap_lib.fix_queue.write_or_clear_queue` prunes stale records
    from every other origin before writing (this pass's own records are
    freshly collected from the current manifest and are not re-judged);
  * the runner refuses stale records before executing anything.

Staleness applies to records produced from env.json entries -- ``env_check:``
and ``symlink:`` ids. Such a record carries ``entry_sha256``, the fingerprint
of the declared manifest entry it came from. It is stale when its origin's
layered env.json (``load_layered_env_manifests(origin)``) no longer declares
the entry, declares it with different content (a changed ``check`` with an
unchanged ``fix`` included), or when the record has no fingerprint (written by
an older engine, so it cannot be shown to match). A layer that fails to parse
is skipped and reported; an entry declared only in that layer then reads as
undeclared, so the failure is closed per ENTRY, not per origin.

Also here: the identity of a queued operation and the deduplication of
identical records. queue.json keeps one record per origin, because each copy is
validated against its own origin's layers; the runner's workload and the
engine's budget/disclosure view collapse identical records, AFTER staleness.

Stdlib-only. The runner runs as a standalone script and reaches this module
through a guarded package import (fix_runner._queue_records_module).
"""

from .env_manifest import canonical_manifest_hash, load_layered_env_manifests

# Record-id prefix -> the env.json section whose entry (identified by `name`)
# produced the record.
ENV_RECORD_SECTIONS = {
    "env_check:": "env_checks",
    "symlink:": "symlinks",
}


def entry_sha256(entry):
    """Fingerprint of one declared env.json entry (canonical JSON sha256)."""
    return canonical_manifest_hash(entry)


def env_record_target(record_id):
    """``(section, name)`` for a record produced from env.json, else None."""
    if not isinstance(record_id, str):
        return None
    for prefix, section in ENV_RECORD_SECTIONS.items():
        if record_id.startswith(prefix):
            return section, record_id[len(prefix):]
    return None


def task_identity(record):
    """What makes two queued records the same operation, origin aside."""
    return (record.get("id"), record.get("kind"), record.get("command"),
            tuple(record.get("packages") or ()),
            tuple(record.get("entries") or ()), record.get("target"))


def dedupe_records(records):
    """Identical operations collapsed to their first record, order kept."""
    seen = set()
    out = []
    for record in records:
        identity = task_identity(record)
        if identity in seen:
            continue
        seen.add(identity)
        out.append(record)
    return out


def _declared_fingerprints(merged, section, name):
    entries = merged.get(section)
    if not isinstance(entries, list):
        return set()
    return {entry_sha256(entry) for entry in entries
            if isinstance(entry, dict) and entry.get("name") == name}


def _origin_label(origin):
    return origin if origin else "user scope"


def split_stale(records, home=None):
    """Partition records into ``(fresh, stale, parse_errors)``.

    ``stale`` is a list of ``(record, reason)``; ``parse_errors`` is a list of
    ``{"path", "error"}`` dicts, one per unreadable layer met while judging
    (deduplicated by path -- the user layers are shared by every origin).
    Records not produced from env.json are always fresh. ``home`` is passed
    to the manifest loader (see env_manifest.env_manifest_paths).
    """
    loaded = {}
    parse_errors = []
    seen_error_paths = set()
    fresh = []
    stale = []
    for record in records:
        target = env_record_target(record.get("id"))
        if target is None:
            fresh.append(record)
            continue
        section, name = target
        if "origin" not in record:
            stale.append((record, "no origin recorded (written by an older "
                                  "bootstrap)"))
            continue
        fingerprint = record.get("entry_sha256")
        if not fingerprint:
            stale.append((record, "no entry fingerprint (written by an older "
                                  "bootstrap), so it cannot be matched to "
                                  "env.json"))
            continue
        origin = record.get("origin") or ""
        if origin not in loaded:
            merged, errors = load_layered_env_manifests(origin, home=home)
            loaded[origin] = merged
            for error in errors:
                if error["path"] not in seen_error_paths:
                    seen_error_paths.add(error["path"])
                    parse_errors.append(error)
        declared = _declared_fingerprints(loaded[origin], section, name)
        if not declared:
            stale.append((record, f"'{name}' is no longer declared in "
                                  f"env.json for {_origin_label(origin)}"))
        elif fingerprint not in declared:
            stale.append((record, f"'{name}' changed in env.json for "
                                  f"{_origin_label(origin)} since it was "
                                  f"queued"))
        else:
            fresh.append(record)
    return fresh, stale, parse_errors


def describe_stale(record, reason):
    """One human line naming a stale record and why it will not run."""
    origin = _origin_label(record.get("origin") or "")
    return f"{record.get('id')} [{origin}]: {reason}"


def describe_parse_error(error):
    """One human line for an unreadable env.json layer met while judging."""
    return (f"env.json layer {error['path']} could not be read "
            f"({error['error']}); fixes declared only there were not kept")
