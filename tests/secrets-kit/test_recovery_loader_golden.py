"""Golden characterization of the version-2 and version-3 recovery loaders.

`authoring._load_entry_operation` (entry add/update/remove records) and
`authoring._load_rotation_operation` (rotation records) are called DIRECTLY,
over a real pending-recovery tree (a publication whose push report was lost),
with one malformation applied per row. Each row records what the loader does:
the exception type and its message (temp paths normalized), or, for a record
it accepts, whether the returned AuthoringOperation carries exactly the
loader's inputs. The rows are compared to tests/secrets-kit/golden/
recovery_loaders.json, captured from the loaders before they were split into
shared validation helpers.

Each row targets one refusal branch: the base record is accepted, and the row
changes exactly the field (or the recovery file) that branch inspects. Rows
that change the phase also pin which checks a phase gates.

Regenerate ONLY for an intended behaviour change:
    SECRETS_KIT_UPDATE_GOLDEN=1 uv run --extra dev pytest tests/secrets-kit/test_recovery_loader_golden.py
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from typing import Any, Callable, Optional

import pytest

from secrets_kit import authoring
from secrets_kit import repo as repository
from sk_testlib import replace_git_tree
from test_dest_guard import _templates, adding, state_template  # noqa: F401
from test_entry_recovery import _pending_entry, entry_subject  # noqa: F401
from test_init import _seeding_template, seeding  # noqa: F401
from test_seed_recovery import pending_subject, rebound_record  # noqa: F401
from test_sync_view import _author_template, _configure_machine, _dummy_crypto

GOLDEN = Path(__file__).parent / "golden" / "recovery_loaders.json"
RECOVERY = authoring.RECOVERY_DIRECTORY
OTHER_DIGEST = hashlib.sha256(b"dummy other declaration").hexdigest()

ENTRY_PHASES = ["admitted", "syncing", "synchronized", "prepared", "applying", "publishing", "unsubmitted",
                "uncertain", "definitely_rejected", "confirmed", "finalizing", "restored", "unchanged",
                "cleaning", "dummy-unknown-phase"]


# --------------------------------------------------------------------------
# Context: the served pending tree and the loader's inputs
# --------------------------------------------------------------------------

def _physical(path: Path) -> list:
    info = path.lstat()
    return [info.st_dev, info.st_ino]


def _context(data_dir: Path) -> SimpleNamespace:
    directory = data_dir / RECOVERY
    record = json.loads((directory / "marker.json").read_bytes())
    clone = data_dir / "repo"
    return SimpleNamespace(data_dir=data_dir, clone=clone, directory=directory, record=record,
                           info=authoring._private(directory, directory=True),
                           marker=authoring._private(directory / "marker.json"))


def _outcome(loader: Callable, ctx: SimpleNamespace, record: dict) -> str:
    try:
        operation = loader(ctx.data_dir, ctx.info, ctx.marker, record)
    except Exception as error:  # the row records the exact type and message
        message = str(error).replace(str(ctx.data_dir), "<data>")
        return type(error).__name__ + ": " + message
    wrong = [name for name, ok in [
        ("type", type(operation) is authoring.AuthoringOperation),
        ("data_dir", operation.data_dir == ctx.data_dir),
        ("clone", operation.clone == ctx.clone),
        ("declared_repo", operation.declared_repo == repository._recorded_origin(ctx.clone)),
        ("record", operation.record is record),
        ("directory_identity", operation.directory_identity == (ctx.info.st_dev, ctx.info.st_ino)),
        ("marker_identity", operation.marker_identity == (ctx.marker.st_dev, ctx.marker.st_ino)),
        ("broken", operation.broken is False),
    ] if not ok]
    return "accepted" if not wrong else "accepted with wrong " + ",".join(wrong)


# --------------------------------------------------------------------------
# Row helpers: each returns an optional cleanup that restores the tree
# --------------------------------------------------------------------------

def _set(key: str, value: Any) -> Callable:
    def apply(rec: dict, ctx: SimpleNamespace) -> None:
        rec[key] = value
    return apply


def _drop(*keys: str) -> Callable:
    def apply(rec: dict, ctx: SimpleNamespace) -> None:
        for key in keys:
            rec.pop(key, None)
    return apply


def _both(*steps: Callable) -> Callable:
    def apply(rec: dict, ctx: SimpleNamespace) -> Optional[Callable]:
        cleanups = [step(rec, ctx) for step in steps]
        cleanups = [c for c in cleanups if callable(c)]
        return (lambda: [c() for c in reversed(cleanups)]) if cleanups else None
    return apply


def _bump(key: str) -> Callable:
    """A well-formed physical identity naming a different object."""
    def apply(rec: dict, ctx: SimpleNamespace) -> None:
        rec[key] = [rec[key][0], rec[key][1] + 1]
    return apply


def _cleaning(outcome: str, *, next_name: Any = None, remaining: Optional[Callable] = None) -> Callable:
    def apply(rec: dict, ctx: SimpleNamespace) -> None:
        names = sorted(rec["artifacts"])
        rec.update(phase="cleaning", cleanup_outcome=outcome,
                   cleanup_remaining=remaining(names) if remaining else names,
                   cleanup_next=next_name(names) if callable(next_name) else next_name)
    return apply


def _set_aside(name_of: Callable) -> Callable:
    """Move one recovery artifact out of the directory for the row."""
    def apply(rec: dict, ctx: SimpleNamespace) -> Callable:
        path = ctx.directory / name_of(rec)
        aside = ctx.data_dir.parent / ("set-aside-" + path.name)
        path.rename(aside)
        return lambda: aside.rename(path)
    return apply


def _foreign_file(rec: dict, ctx: SimpleNamespace) -> Callable:
    path = ctx.directory / "foreign-dummy-file"
    path.write_bytes(b"dummy foreign bytes")
    path.chmod(0o600)
    return path.unlink


def _artifact_mode(rec: dict, ctx: SimpleNamespace) -> Callable:
    path = ctx.directory / "entry-index"
    path.chmod(0o644)
    return lambda: path.chmod(0o600)


def _artifact_bytes(rec: dict, ctx: SimpleNamespace) -> Callable:
    path = ctx.directory / "entry-index"
    original = path.read_bytes()
    path.write_bytes(b"dummy substituted artifact")
    return lambda: path.write_bytes(original)


def _merge_head(rec: dict, ctx: SimpleNamespace) -> Callable:
    """An unfinished Git operation: only `_admit` refuses it."""
    path = ctx.clone / ".git" / "MERGE_HEAD"
    path.write_bytes(rec["entry_head"].encode("ascii") + b"\n")
    return path.unlink


def _unset_origin(rec: dict, ctx: SimpleNamespace) -> Callable:
    """No recorded origin: the binding query refuses."""
    url = repository._recorded_origin(ctx.clone)
    subprocess.run(["git", "config", "--local", "--unset", "remote.origin.url"], cwd=ctx.clone, check=True)
    return lambda: subprocess.run(["git", "config", "--local", "remote.origin.url", url], cwd=ctx.clone, check=True)


def _foreign_artifact_present(rec: dict, ctx: SimpleNamespace) -> Callable:
    """A private file outside the allowed names, recorded exactly as it is on disk."""
    path = ctx.directory / "foreign-dummy"
    path.write_bytes(b"dummy foreign artifact")
    path.chmod(0o600)
    rec["artifacts"]["foreign-dummy"] = authoring._file_state(path)
    return path.unlink


def _retire(name_of: Callable) -> Callable:
    """Drop one artifact from the record and from the directory, consistently."""
    def apply(rec: dict, ctx: SimpleNamespace) -> Callable:
        name = name_of(rec)
        rec["artifacts"].pop(name)
        return _set_aside(lambda rec: name)(rec, ctx)
    return apply


QUIET = (_set("outputs", {}), _set("phase", "admitted"), _drop("synced_files", "synced_index"))


def _late_mode(rec: dict, ctx: SimpleNamespace) -> Callable:
    """A fault only the artifact-file loop sees (late in both loaders, a distinct exception type)."""
    path = ctx.directory / "prepared-index"
    path.chmod(0o644)
    return lambda: path.chmod(0o600)


# Rows whose own fault is found by the artifact-file loop. Behind the late
# fault that loop meets two faulty files, and which it reports first follows
# set iteration order (PYTHONHASHSEED), so these get no late variants.
_FILE_LOOP_ROWS = {"artifact-digest", "artifact-proposed-blob", "file-bytes"}


def _with_late_faults(rows: list) -> list:
    """Every row again with a late fault, and again in the admitted phase with it.

    A refusal that a later, redundant check would also make is invisible on its
    own; behind a late fault of another type, disabling the early check changes
    the outcome. The admitted phase removes the phase-gated later checks.
    """
    kept = [(name, apply) for name, apply in rows if name not in _FILE_LOOP_ROWS]
    late = [(name + "+late", _both(apply, _late_mode)) for name, apply in kept]
    admitted = [(name + "+admitted+late", _both(apply, _set("phase", "admitted"), _late_mode)) for name, apply in kept]
    return rows + late + admitted


def _first_preimage(rec: dict) -> str:
    return next(name for name, state in sorted(rec["preimages"].items()) if state)


def _duplicate_stored(rec: dict, ctx: SimpleNamespace) -> None:
    """A second preimage receipt naming the stored file the first one names."""
    rec["preimages"]["blobs/dummy-duplicate.age"] = dict(rec["preimages"][_first_preimage(rec)])


def _preimage_field(field: str, value: Any) -> Callable:
    def apply(rec: dict, ctx: SimpleNamespace) -> None:
        rec["preimages"][_first_preimage(rec)][field] = value
    return apply


def _extra_preimage(name: str, state: Any) -> Callable:
    def apply(rec: dict, ctx: SimpleNamespace) -> None:
        rec["preimages"][name] = state
    return apply


def _output_field(name: str, field: str, value: Any) -> Callable:
    def apply(rec: dict, ctx: SimpleNamespace) -> None:
        rec["outputs"][name] = dict(rec["outputs"][name], **{field: value})
    return apply


def _publication(oid_of: Callable) -> Callable:
    """A well-formed commit and its own proof ref, naming another commit."""
    def apply(rec: dict, ctx: SimpleNamespace) -> None:
        rec["commit_oid"] = oid_of(rec)
        rec["proof_ref"] = repository._publication_ref(rec["commit_oid"])
    return apply


def _current_sync(rec: dict, ctx: SimpleNamespace) -> None:
    """Make the synchronized receipts describe the checkout as it now is."""
    rec["synced_head"] = authoring._head(ctx.clone)
    rec["synced_index"] = authoring._file_state(ctx.clone / ".git/index")
    rec["synced_files"] = {name: authoring._file_state(ctx.clone / name) for name in rec["preimages"]}


def _mutate_synced_file(rec: dict, ctx: SimpleNamespace) -> None:
    name = sorted(rec["synced_files"])[0]
    rec["synced_files"][name] = {"digest": OTHER_DIGEST, "mode": 0o644}


BAD_RECEIPT = {"digest": "dummy", "mode": 0o600}
COMMON_ROWS: list = [
    ("base", lambda rec, ctx: None),
    ("required-missing", _drop("branch")),
    ("unknown-key", _set("dummy_unknown_key", 1)),
    ("optional-reason-absent", _drop("publication_reason")),
    ("version-bool", _set("version", True)),
    ("version-float", _set("version", 2.0)),
    ("version-seed", _set("version", 1)),
    ("operation-seed", _set("operation", "seed")),
    *[("identity-%s-%s" % (key, shape), _set(key, value))
      for key in ("clone_identity", "git_identity", "directory_identity", "marker_identity")
      for shape, value in (("short", [1]), ("negative", [-1, 0]), ("bool", [True, 1]), ("text", "1,2"))],
    ("directory-moved", _bump("directory_identity")),
    ("marker-moved", _bump("marker_identity")),
    ("clone-moved", _bump("clone_identity")),
    ("git-moved", _bump("git_identity")),
    ("entry_head-malformed", _set("entry_head", "dummy")),
    ("entry_head-null", _set("entry_head", None)),
    ("synced_head-malformed", _set("synced_head", "dummy")),
    ("index-malformed", _set("index", BAD_RECEIPT)),
    ("index-extra-field", _set("index", {"digest": OTHER_DIGEST, "mode": 0o600, "stored": "x"})),
    ("index-mode-range", _set("index", {"digest": OTHER_DIGEST, "mode": 0o10000})),
    ("index-null", _set("index", None)),
    ("entry_cache-malformed", _set("entry_cache", BAD_RECEIPT)),
    ("entry_cache-null", _set("entry_cache", None)),
    ("synced_index-malformed", _set("synced_index", BAD_RECEIPT)),
    ("synced_index-absent", _drop("synced_index")),
    ("commit-malformed", _set("commit_oid", "dummy")),
    ("proof_ref-foreign", _set("proof_ref", "refs/heads/dummy-foreign-proof")),
    ("expected_tree-malformed", _set("expected_tree", "dummy")),
    ("expected_tree-absent", _drop("expected_tree")),
    ("proof_ref-without-commit", _drop("commit_oid")),
    ("publication-absent", _drop("commit_oid", "proof_ref")),
    ("declared_digest-malformed", _set("declared_repo_digest", "dummy")),
    ("declared_digest-other", _set("declared_repo_digest", OTHER_DIGEST)),
    ("preimages-list", _set("preimages", [])),
    ("preimages-no-manifest", lambda rec, ctx: rec["preimages"].pop("manifest.json")),
    ("preimage-ineligible-name", _extra_preimage("dummy/other.txt", None)),
    ("preimage-absent-slot", _extra_preimage("blobs/dummy-absent.age", None)),
    ("preimage-mode-range", _preimage_field("mode", -1)),
    ("preimage-stored-nonstring", _preimage_field("stored", 7)),
    ("preimage-stored-prefix", _preimage_field("stored", "dummy-0")),
    ("preimage-stored-nondigit", _preimage_field("stored", "preimage-x")),
    ("preimage-stored-duplicate", _duplicate_stored),
    ("preimage-receipt-unstored", _preimage_field("stored", None)),
    ("outputs-list", _set("outputs", [])),
    ("artifacts-list", _set("artifacts", [])),
    ("outputs-empty", _set("outputs", {})),
    ("outputs-empty-admitted", _both(_set("outputs", {}), _set("phase", "admitted"))),
    ("outputs-extra", lambda rec, ctx: rec["outputs"].update({".gitignore": rec["outputs"]["manifest.json"]})),
    ("outputs-manifest-stored", _output_field("manifest.json", "stored", "proposed-identity")),
    ("outputs-manifest-mode", _output_field("manifest.json", "mode", -1)),
    ("authored_paths-reversed", lambda rec, ctx: rec.update(authored_paths=rec["authored_paths"][::-1])),
    ("authored_paths-absent", _drop("authored_paths")),
    ("synced_files-list", _set("synced_files", [])),
    ("synced_files-keys", lambda rec, ctx: rec["synced_files"].pop("manifest.json")),
    ("synced_files-receipt", lambda rec, ctx: rec["synced_files"].update({"manifest.json": BAD_RECEIPT})),
    ("synced_files-absent", _drop("synced_files")),
    ("synced-absent-admitted", _both(_drop("synced_files", "synced_index"), _set("phase", "admitted"))),
    ("synced-absent-syncing", _both(_drop("synced_files", "synced_index"), _set("phase", "syncing"))),
    ("synced-absent-synchronized", _both(_drop("synced_files", "synced_index"), _set("phase", "synchronized"))),
    ("reason-unknown", _set("publication_reason", "dummy arbitrary sensitive marker text")),
    ("artifact-foreign", lambda rec, ctx: rec["artifacts"].update({"foreign-dummy": {"digest": OTHER_DIGEST, "mode": 0o600}})),
    ("artifact-null", lambda rec, ctx: rec["artifacts"].update({"entry-index": None})),
    ("artifact-receipt", lambda rec, ctx: rec["artifacts"].update({"entry-index": BAD_RECEIPT})),
    ("artifact-digest", lambda rec, ctx: rec["artifacts"].update({"entry-index": {"digest": OTHER_DIGEST, "mode": 0o600}})),
    ("artifact-unrecorded", lambda rec, ctx: rec["artifacts"].pop("entry-index")),
    ("file-foreign", _foreign_file),
    ("file-missing", _set_aside(lambda rec: "entry-index")),
    ("file-mode", _artifact_mode),
    ("file-bytes", _artifact_bytes),
    ("cleaning-no-outcome", _set("phase", "cleaning")),
    ("cleaning-confirmed", _cleaning("confirmed")),
    ("cleaning-restored", _cleaning("restored")),
    ("cleaning-unchanged", _cleaning("unchanged")),
    ("cleaning-unknown", _cleaning("dummy-unknown")),
    ("cleaning-remaining-text", _cleaning("restored", remaining=lambda names: "entry-index")),
    ("cleaning-remaining-duplicate", _cleaning("restored", remaining=lambda names: names + names[:1])),
    ("cleaning-remaining-short", _cleaning("restored", remaining=lambda names: names[1:])),
    ("cleaning-next-present", _cleaning("restored", next_name=lambda names: "entry-index")),
    ("cleaning-next-foreign", _cleaning("restored", next_name="foreign-dummy")),
    ("cleaning-next-absent", _both(_cleaning("restored", next_name=lambda names: "entry-index"),
                                   _set_aside(lambda rec: "entry-index"))),
    ("cleaning-confirmed-next-absent", _both(_cleaning("confirmed", next_name=lambda names: "entry-index"),
                                             _set_aside(lambda rec: "entry-index"))),
    ("branch-other", _set("branch", "refs/heads/dummy-other")),
    ("published-commit-other", _publication(lambda rec: rec["entry_head"])),
    ("published-tree-other", lambda rec, ctx: rec.update(expected_tree=rec["entry_head"])),
    ("published-output-other", _output_field("manifest.json", "digest", OTHER_DIGEST)),
    ("published-admit", _merge_head),
    ("unpublished-admit", _both(_merge_head, _set("phase", "definitely_rejected"))),
    ("unpublished-output-other", _both(_output_field("manifest.json", "digest", OTHER_DIGEST),
                                       _set("phase", "unsubmitted"))),
    ("unpublished-commit-other", _both(_publication(lambda rec: rec["entry_head"]), _set("phase", "applying"))),
    ("confirmed-cleaning-commit-other", _both(_cleaning("confirmed"), _publication(lambda rec: rec["entry_head"]))),
    ("restored-cleaning-commit-other", _both(_cleaning("restored"), _publication(lambda rec: rec["entry_head"]))),
    ("restored-cleaning-no-authored", _both(_cleaning("restored"), _drop("authored_paths", "expected_tree"))),
    ("confirmed-cleaning-no-authored", _both(_cleaning("confirmed"), _drop("authored_paths"))),
]
COMMON_ROWS += [("phase-" + phase, _set("phase", phase)) for phase in ENTRY_PHASES]
COMMON_ROWS += [
    ("preimages-text", _set("preimages", "manifest.json")),
    ("cleaning-outcome-list", _cleaning(["confirmed"])),
    ("cleaning-outcome-list-bad-synced", _both(_cleaning(["confirmed"]), lambda rec, ctx: rec["synced_files"].update(
        {"manifest.json": BAD_RECEIPT}))),
    ("clone_identity-text+head-null", _both(_set("clone_identity", "1,2"), _set("entry_head", None))),
    ("declared_digest-malformed+origin-unset", _both(_set("declared_repo_digest", "dummy"), _unset_origin)),
    ("origin-unset", _unset_origin),
    ("artifact-foreign-present", _foreign_artifact_present),
    ("preimages-names-list", _set("preimages", ["manifest.json", "identity.age"])),
    ("version-float-rotation", _set("version", 3.0)),
]


def _selected(rec: dict) -> str:
    return rec["selected_blob"]


ENTRY_ROWS: list = COMMON_ROWS + [
    ("version-rotation", _set("version", 3)),
    ("operation-rotate", _set("operation", "rotate")),
    ("operation-add", _set("operation", "add")),
    ("operation-update", _set("operation", "update")),
    ("operation-remove", _set("operation", "remove")),
    ("entry_blob_parent-malformed", _set("entry_blob_parent", [1])),
    ("entry_blob_parent-null", _set("entry_blob_parent", None)),
    ("created_blob_parent-malformed", _set("created_blob_parent", [1])),
    ("created_blob_parent-set", _set("created_blob_parent", [1, 2])),
    ("blob_parents-swapped", lambda rec, ctx: rec.update(created_blob_parent=rec["entry_blob_parent"], entry_blob_parent=None)),
    ("receiving-malformed", _set("blob_receiving_identity", [1])),
    ("receiving-moved", _set("blob_receiving_identity", [1, 2])),
    ("receiving-absent", _drop("blob_receiving_identity")),
    *[("selected-" + label, _set("selected_blob", value)) for label, value in (
        ("nonstring", 5), ("backslash", "blobs\\dummy.age"), ("nested", "blobs/a/dummy.age"),
        ("outside", "dummy.age"), ("not-age", "blobs/dummy.txt"), ("dotdot", "blobs/../dummy.age"),
        ("dot-segment", "blobs/./dummy.age"), ("identity", "identity.age"), ("null", None))],
    ("selected-unrecorded", lambda rec, ctx: rec["preimages"].pop(rec["selected_blob"])),
    ("outputs-selected-missing", lambda rec, ctx: rec["outputs"].pop(rec["selected_blob"])),
    ("outputs-selected-null", lambda rec, ctx: rec["outputs"].update({rec["selected_blob"]: None})),
    ("outputs-selected-stored", lambda rec, ctx: rec["outputs"].update(
        {rec["selected_blob"]: dict(rec["outputs"]["manifest.json"])})),
    ("artifact-proposed-blob", lambda rec, ctx: rec["artifacts"].update(
        {"proposed-blob": {"digest": OTHER_DIGEST, "mode": 0o600}})),
    ("artifact-proposed-identity", lambda rec, ctx: rec["artifacts"].update(
        {"proposed-identity": {"digest": OTHER_DIGEST, "mode": 0o600}})),
    ("unchanged-current", _both(_current_sync, _set("phase", "unchanged"))),
    ("unchanged-cleaning-current", _both(_current_sync, _cleaning("unchanged"))),
    ("unchanged-head", _both(_current_sync, _set("phase", "unchanged"),
                             lambda rec, ctx: rec.update(synced_head=rec["entry_head"]))),
    ("unchanged-index", _both(_current_sync, _set("phase", "unchanged"),
                              _set("synced_index", {"digest": OTHER_DIGEST, "mode": 0o644}))),
    ("unchanged-files", _both(_current_sync, _set("phase", "unchanged"), _mutate_synced_file)),
    ("unchanged-admit", _both(_current_sync, _set("phase", "unchanged"), _merge_head)),
    ("unchanged-cleaning-files", _both(_current_sync, _cleaning("unchanged"), _mutate_synced_file)),
    ("receiving-malformed+tree-absent", _both(_set("blob_receiving_identity", [1]), _drop("expected_tree"))),
    ("selected-backslash-recorded", _both(_set("selected_blob", "blobs\\dummy.age"), _set("outputs", {}),
                                          _set("phase", "admitted"), _drop("synced_files", "synced_index"),
                                          _extra_preimage("blobs\\dummy.age", None))),
    *[("unchanged-%s" % label, _both(_current_sync, _set("phase", "unchanged"), corrupt))
      for label, corrupt in (
          ("outputs-empty", _set("outputs", {})),
          ("authored_paths-absent", _drop("authored_paths")),
          ("authored_paths-reversed", lambda rec, ctx: rec.update(authored_paths=rec["authored_paths"][::-1])),
          ("tree-absent", _drop("commit_oid", "proof_ref", "expected_tree")),
          ("synced_files-absent", _drop("synced_files")),
          ("synced_index-absent", _drop("synced_index")),
          ("synced_index-malformed", _set("synced_index", BAD_RECEIPT)))],
    ("unchanged-cleaning-synced_files-absent", _both(_current_sync, _cleaning("unchanged"), _drop("synced_files"))),
    ("selected-unrecorded-unsynced", _both(lambda rec, ctx: rec["preimages"].pop(rec["selected_blob"]),
                                           _drop("synced_files", "synced_index"), _set("phase", "admitted"))),
]
ENTRY_ROWS = _with_late_faults(ENTRY_ROWS)

ROTATION_ROWS: list = COMMON_ROWS + [
    ("version-entry", _set("version", 2)),
    ("operation-add", _set("operation", "add")),
    ("cache_digest-malformed", _set("cache_digest", "dummy")),
    ("cache_digest-absent", _drop("cache_digest")),
    ("cache_digest-absent-admitted", _both(_drop("cache_digest"), _set("phase", "admitted"))),
    ("rotation_blobs-text", _set("rotation_blobs", "blobs/source.txt.age")),
    ("rotation_blobs-duplicate", lambda rec, ctx: rec.update(rotation_blobs=rec["rotation_blobs"] * 2)),
    ("rotation_blobs-unsorted", lambda rec, ctx: rec.update(rotation_blobs=["blobs/z.age"] + rec["rotation_blobs"])),
    ("rotation_blobs-nonstring", _set("rotation_blobs", [5])),
    ("rotation_blobs-nested", _set("rotation_blobs", ["blobs/a/b.age"])),
    ("rotation_blobs-backslash", _set("rotation_blobs", ["blobs\\b.age"])),
    ("rotation_blobs-empty", _set("rotation_blobs", [])),
    ("rotation_blobs-unrecorded", lambda rec, ctx: rec.update(rotation_blobs=sorted(rec["rotation_blobs"] + ["blobs/a.age"]))),
    ("preimages-no-identity", lambda rec, ctx: rec["preimages"].pop("identity.age")),
    ("receiving-list", _set("receiving_identities", [])),
    ("receiving-unknown-stored", lambda rec, ctx: rec["receiving_identities"].update({"proposed-blob-9": [1, 2]})),
    ("receiving-malformed", lambda rec, ctx: rec["receiving_identities"].update({"proposed-identity": [1]})),
    ("receiving-moved", lambda rec, ctx: rec["receiving_identities"].update({"proposed-identity": [1, 2]})),
    ("receiving-empty", _set("receiving_identities", {})),
    ("receiving-absent-file", _both(_cleaning("restored", next_name=lambda names: "proposed-identity"),
                                    _set_aside(lambda rec: "proposed-identity"),
                                    lambda rec, ctx: rec["receiving_identities"].update({"proposed-identity": [1, 2]}))),
    ("outputs-identity-missing", lambda rec, ctx: rec["outputs"].pop("identity.age")),
    ("outputs-blob-stored", _output_field("blobs/source.txt.age", "stored", "proposed-blob-1")),
    ("outputs-blob-null", lambda rec, ctx: rec["outputs"].update({"blobs/source.txt.age": None})),
    ("artifact-proposed-blob-1", lambda rec, ctx: rec["artifacts"].update(
        {"proposed-blob-1": {"digest": OTHER_DIGEST, "mode": 0o600}})),
    ("artifact-proposed-ignore", lambda rec, ctx: rec["artifacts"].update(
        {"proposed-ignore": {"digest": OTHER_DIGEST, "mode": 0o600}})),
    ("published-identity-output-other", _output_field("identity.age", "digest", OTHER_DIGEST)),
]
ROTATION_ROWS += [
    ("rotation_blobs-int", _set("rotation_blobs", 5)),
    ("rotation_blobs-duplicate-quiet", _both(lambda rec, ctx: rec.update(rotation_blobs=rec["rotation_blobs"] * 2),
                                             *QUIET)),
    ("rotation_blobs-unrecorded-quiet", _both(
        lambda rec, ctx: rec.update(rotation_blobs=sorted(rec["rotation_blobs"] + ["blobs/a.age"])), *QUIET)),
    ("preimages-no-identity-quiet", _both(_retire(lambda rec: rec["preimages"]["identity.age"]["stored"]),
                                          lambda rec, ctx: rec["preimages"].pop("identity.age"), *QUIET)),
    ("operation-seed-quiet", _both(_set("operation", "seed"), _retire(lambda rec: "proposed-blob-0"),
                                   _set("receiving_identities", {}), *QUIET)),
]
ROTATION_ROWS = _with_late_faults(ROTATION_ROWS)


def _table(loader: Callable, ctx: SimpleNamespace, rows: list, *, stop_at: Optional[dict] = None) -> dict:
    """Every row's outcome; with `stop_at`, return as soon as one differs from it."""
    names = [name for name, _ in rows]
    assert len(names) == len(set(names)), "row names must be unique"
    results = {}
    for name, apply in rows:
        record = json.loads(json.dumps(ctx.record))
        cleanup = apply(record, ctx)
        try:
            results[name] = _outcome(loader, ctx, record)
        finally:
            if callable(cleanup):
                cleanup()
        if stop_at is not None and results[name] != stop_at.get(name):
            return results
    return results


# --------------------------------------------------------------------------
# Served states
# --------------------------------------------------------------------------

def entry_context(subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, operation: str) -> SimpleNamespace:
    _pending_entry(subject, monkeypatch, operation)
    return _context(subject.data_dir)


def _lose_rotation_report(subject: SimpleNamespace, patch: pytest.MonkeyPatch) -> None:
    """Rotate with the push accepted but its report lost and no fresh proof."""
    real_run = subprocess.run

    def run(command, *args, **kwargs):
        own = command[0] == "git" and Path(kwargs.get("cwd") or ".") == subject.clone
        if own and command[1] == "fetch" and "--no-write-fetch-head" in command:
            return subprocess.CompletedProcess(command, 128, b"", b"dummy proof transport failure")
        result = real_run(command, *args, **kwargs)
        if own and command[1] == "push":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=result.stdout, stderr=result.stderr)
        return result

    with patch.context() as controlled:
        controlled.setattr(subprocess, "run", run)
        assert subject.cli.main(["rotate-identity"]) == 1


def _rotation_template() -> Path:
    def populate(subject: SimpleNamespace, patch: pytest.MonkeyPatch) -> None:
        _configure_machine(subject)
        _dummy_crypto(subject, patch)
        _lose_rotation_report(subject, patch)
        marker = json.loads((subject.data_dir / RECOVERY / "marker.json").read_bytes())
        assert marker["operation"] == "rotate" and marker["phase"] == "uncertain"
    return state_template("rotation-pending-uncertain", populate, after=_author_template())


def rotation_context(subject: SimpleNamespace) -> SimpleNamespace:
    """A copy of the rotation template, every physical binding re-derived for the copy."""
    replace_git_tree(_rotation_template(), subject.data_dir.parent)
    _configure_machine(subject)
    directory = subject.data_dir / RECOVERY
    marker = directory / "marker.json"
    record = rebound_record(subject.data_dir, json.loads(marker.read_bytes()))
    record["receiving_identities"] = {stored: _physical(directory / stored) for stored in record["receiving_identities"]}
    with marker.open("r+b") as stream:
        stream.write(json.dumps(record, sort_keys=True).encode())
        stream.truncate()
    ctx = _context(subject.data_dir)
    assert _outcome(authoring._load_rotation_operation, ctx, ctx.record) == "accepted"
    return ctx


# --------------------------------------------------------------------------
# The golden comparison
# --------------------------------------------------------------------------

def _compare(key: str, observed: dict) -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8")) if GOLDEN.exists() else {}
    if os.environ.get("SECRETS_KIT_UPDATE_GOLDEN") == "1":
        golden[key] = observed
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(golden, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return
    assert observed == golden[key]


@pytest.mark.parametrize("operation", ["add", "update", "remove"])
def test_entry_loader_matches_golden(entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
                                     operation: str) -> None:
    ctx = entry_context(entry_subject, monkeypatch, operation)
    _compare("entry-" + operation, _table(authoring._load_entry_operation, ctx, ENTRY_ROWS))


def test_rotation_loader_matches_golden(adding: SimpleNamespace) -> None:
    ctx = rotation_context(adding)
    _compare("rotation", _table(authoring._load_rotation_operation, ctx, ROTATION_ROWS))
