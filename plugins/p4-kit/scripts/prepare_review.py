#!/usr/bin/env python3
"""Gather review context for a Perforce changelist.

Usage: prepare_review.py <CL>

Runs `p4 describe -du <CL>` (with `-S` fallback for shelved CLs), parses the
changed depot files, resolves them to local workspace paths via `p4 where`,
walks each file's parent directories up to the workspace root collecting any
ancestor CLAUDE.md files, and emits a JSON bundle on stdout.

`p4 describe -du` emits no `@@` hunks for `add` or `delete` actions. To ensure
reviewers see the full introduced/removed code, this script synthesizes
new-file / deleted-file hunks for those actions by fetching content via
`p4 print`. Supports both shelved (`@=<CL>`) and submitted (`#<rev>`) forms.
Non-text filetypes (binary, apple, resource, ...) are never content-inlined;
they get a one-line `(binary file added: N bytes)` placeholder instead. A
`utf16`-typed add/delete file stays text-like -- its content is fetched via a
temp-file `p4 print -q -o` write and sniffed for a UTF-16 BOM or the
NUL-alternation byte pattern before being decoded, so real UTF-16 bytes decode
correctly instead of mojibaking through a forced UTF-8 read.

Also runs `p4 reconcile -n` recursively over the minimal covering set of
directories containing CL files, and reports any unreconciled files
(untracked adds, unopened edits, missing deletes) that the user may have
forgotten to include in the CL. `.p4ignore` is honored by p4 itself; files
already opened in any pending CL are skipped by reconcile.

The same directory set is queried with `p4 -ztag opened -c default` to report
files open in the default changelist. These use the `default_open` bundle key
because folding them into the numbered CL requires `p4 reopen -c`, while
unreconciled files require `p4 reconcile -c`.

Reconcile can also report a depot path the CL already has open, under two
action-transition sequences: opened for edit then deleted from the
workspace, or opened for delete then recreated in the workspace. Neither is
a forgotten file -- the CL already owns the path -- so neither is offered
via `unreconciled`. Each instead surfaces via the `stale_open` bundle key:
the CL will fail `p4 submit` outright (open for edit, file missing) or the
submit would delete content that is on disk (open for delete, file
present).

Also runs `p4 resolve -n -c <CL>` to report any files in the CL with
pending merge/integrate resolves. These are informational: the diff still
goes to reviewers (conflict markers in the file content are themselves
a legitimate review observation), but the user is warned that the CL is
not submittable until each unresolved file is run through `p4 resolve`.

When the CL header names a different client than `p4 -ztag info`, the review
continues against the shelf but skips client-local hygiene scans. Each skipped
scan is recorded in `hygiene_incomplete`, claim pre-images are refused, and a
`foreign_change` entry identifies the CL owner. CLAUDE.md discovery uses the
invoking workspace so its review rules govern the review.

Any hygiene scan can itself fail to run -- a bad
workspace, an unreachable server -- for a reason other than "nothing to
report". That case is never folded into a clean empty `unreconciled` or
`unresolved` list, which would read identically to "ran and found nothing";
it is recorded in the `hygiene_incomplete` bundle key instead, naming which
scan did not complete and why. A failed hygiene scan does not abort the
prepare -- the diff review is still worth having.

The workspace root is intentionally excluded from recursive scans -- if a
CL touches a root-level file, the root is scanned non-recursively (`/*`)
and deeper CL directories keep their recursive (`/...`) scans separately.
Recursing from the workspace root would crawl every untracked directory
in the tree (Binaries/, Intermediate/, build outputs, IDE files, etc.)
even when `.p4ignore` doesn't list them all -- a blast radius the review
prep doesn't need.

Scans every ancestor CLAUDE.md collected above for `**Submit gate:**` blocks.
Each gate names a list of scope paths (prefixes if no glob chars, fnmatch globs
otherwise); a gate fires when at least one file in the CL falls within any of
its scope paths. Gates are deterministic reminders the author must act on
locally before submit (e.g. build a binary, regenerate a derived file, run a
validator) -- not in-diff issues. Authoring format:

    **Submit gate:** <imperative>.
    Applies to:
    - <path prefix or glob>
    - <path prefix or glob>

    <optional rationale paragraph>

Diff text is NOT inlined in the bundle. It is partitioned into chunks of
<= MAX_CHUNK_BYTES each, written to <bundle_dir>/chunks/chunk-NNN.diff,
and indexed by `diff_chunks`. Each `changed_files` entry carries the
`chunk_index` that contains its diff. Reviewer subagents Read one chunk
per agent -- one large CL fans out across multiple agents in parallel
instead of forcing every reviewer to ingest the full diff. The bundle
itself (also written to <bundle_dir>/bundle.json) is small enough to
inline through stdout.

Output schema:
    {
      "cl": "<CL>",
      "description": "<change description>",
      "project_root": "<absolute path to the p4 workspace root, or null if unresolvable>",
      "bundle_dir": "<absolute path to bundle directory>",
      "diff_chunks": [
        {"index": 0, "path": "chunks/chunk-000.diff",
         "files": ["<depot path>", ...], "bytes": <int>}
      ],
      "changed_files": [
        {"depot": "<depot path>", "local": "<local path>",
         "chunk_index": <int or null if absent from diff>,
         "claude_mds": ["<absolute path>", ...]}
      ],
      "unique_claude_mds": ["<absolute path>", ...],
      "unreconciled": [
        {"local": "<local path>", "depot": "<depot path>", "action": "add"|"edit"|"delete"}
      ],
      "default_open": [
        {"local": "<local path>", "depot": "<depot path>", "action": "<open action>"}
      ],
      "stale_open": [
        {"depot": "<depot path>", "local": "<local path or null>",
         "open_action": "<the action the CL has this file open for>",
         "workspace_state": "missing"|"present"}
      ],
      "shelf_drift": [
        {"depot": "<depot path>", "local": "<local workspace path>"}
      ],
      "foreign_change": {                    # present only when the CL header
        "user": "<CL owner>",                 # names a different client
        "client": "<CL client>"               # than p4 info
      },
      "unresolved": [
        {"local": "<local path>", "depot": "<depot path>",
         "resolve_type": "<p4 resolveType, e.g. content/branch/delete>",
         "from_file": "<source depot path, may be empty>"}
      ],
      "hygiene_incomplete": [                 # always present; empty means all
                                               # applicable scans completed --
                                               # a non-empty entry means a scan
                                               # below could NOT run, so its own
                                               # empty list must not be read as
                                               # "clean"
        {"scan": "unreconciled"|"default_open"|"unresolved"|"shelf_fingerprint"|"shelf_opened"|"shelf_drift"|"machine_emitted",
         "reason": "<p4 error detail>"}
      ],
      "claimed_files": [                      # present only when --claim was passed
        {"identifier": "<depot path>", "depot": "<depot path>",
         "local": "<local path>", "action": "add"|"edit"|"delete"|...,
         "pre_image": "<local path to materialized #have pre-image, or null for an add>",
         "trivial": <bool>,                    # pure-mechanical: typo-sized + no meaning-bearing change
         "trivial_reasons": ["<code>", ...],   # disqualifiers when trivial=false (e.g. too_large, structure_changed)
         "trivial_checks": {"ascii_clean": <bool>, "no_abs_paths": <bool>},  # only when trivial=true
         "claude_mds": ["<absolute path>", ...]}   # nearest-ancestor-first; includes self for a CLAUDE.md subject
      ],
      "submit_gates": [
        {"source": "<absolute path to CLAUDE.md>",
         "summary": "<one-line imperative>",
         "scope_paths": ["<prefix or glob>", ...],
         "matched_files": ["<local path>", ...],
         "rationale": "<optional prose, may be empty>",
         "line_no": <int>}
      ],
      "auto_shelved": <bool>,
      "shelf_fingerprint": {"<depot path>": "<md5 digest>", ...},
      "change_id": "<CL>",                       # ledger key: the change identity
      "ledger_baseline": "<token>",              # invalidates stale ledger entries
      "ledger_hits": [                           # findings previously declined for this CL, still valid
        {"key": "<hash>", "kind": "md_audit"|"code_review", "file": "<path>",
         "verdict": "declined", "baseline": "<token>", "timestamp": <float>,
         "label": "<short human label>", "severity": "<md_audit only>"}
      ]
    }

`auto_shelved` is true when this run executed `p4 shelve -c <CL>` to make the
diff fetchable. `shelf_fingerprint` is the {depot: digest} map of the resulting
shelf -- captured so a subsequent `--cleanup <bundle_dir>` invocation can verify
the shelf still matches what we created before deleting it. Empty when
`auto_shelved` is false (we did not create the shelf and must not touch it).

`shelf_drift` contains one entry for each opened shelved file whose local
workspace digest differs. It warns rather than refuses because the shelf
digest is server-normalized; an entry contains the depot path and local path.

A `<CL>` invocation also accepts `--claim <glob>` (repeatable). A changed file
whose depot path matches a claim glob is held back from the generic reviewer
fan-out (its diff is excluded from the chunks and it is dropped from
`changed_files`) and surfaced under `claimed_files` instead, with its `#have`
pre-image materialized to `<bundle_dir>/pre-images/<name>`. Claimed files still
contribute to `unique_claude_mds` and the submit-gate scan. With no `--claim`
the bundle is byte-identical to a bundle built without claims (no
`claimed_files` key).

A glob prefixed with `!` is an EXCLUSION and beats every positive pattern, so a
caller can claim a broad shape while carving out a subset that no specialist
actually reviews -- e.g. `--claim '**/*.md' --claim '!vendor/**/*.md'`.
Exclusions match depot paths the same way positives do.

Modes:
- `prepare_review.py <CL>` -- gather context, emit bundle JSON on stdout.
- `prepare_review.py --cleanup <bundle_dir>` -- read bundle.json, and if
  `auto_shelved` is true and the live shelf fingerprint still matches, run
  `p4 shelve -d -c <CL>`. Any mismatch (file added/removed/changed, shelf gone)
  is a silent no-op -- the user's work is never overwritten.
- `prepare_review.py --ledger-record <declined.json>` -- record findings the
  author declined into the ledger so they render collapsed (not re-litigated)
  on the next review of the same CL. Payload: {change_id, baseline, declined:[...]}.
  See bootstrap_lib.code_review.ledger.

`--claim` requires a PENDING CL: pre-images come from the workspace `#have`
revision, which is POST-change for a submitted CL once the workspace has synced
past it, so `--claim` on a submitted CL exits with an error (re-run without
`--claim` for a plain informational review).

Stderr-only diagnostics. Non-zero exit on hard failure.
"""

import hashlib
import importlib
import inspect
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn, Optional

# Plugins define their own bootstrap-provisioned venv and must run under it
# preferentially. A bare `python` or `uv run` invocation lands in a different
# environment with no shared-libs .pth, so re-exec under the provisioned venv
# before importing bootstrap_lib below -- a no-op when already there. The guard
# is the vendored, stdlib-only bootstrap_guard next to this script; importing it
# can never itself trip the missing-bootstrap_lib failure.
from bootstrap_guard import data_dir, reexec_under_plugin_venv  # noqa: E402

reexec_under_plugin_venv("p4-kit")


# bootstrap_lib is linked onto p4-kit's venv by the bootstrap shared-libs .pth
# (p4-kit declares "shared_lib_imports": ["bootstrap_lib"]). When this script runs
# under that venv the import below just works -- no path discovery. The try/except
# below remains as a safety net for the installed-but-not-yet-provisioned window.

_MIN_BOOTSTRAP_VERSION = "0.102.0"
_BOOTSTRAP_FRONTIER = (
    "bootstrap_lib.code_review.pipeline.run_vcs(timeout=...), "
    "bootstrap_lib.code_review.mechanical"
)


def _exit_bootstrap_too_old() -> NoReturn:
    """Refuse a review when bootstrap lacks the required shared API."""
    from bootstrap_guard import EXIT_BOOTSTRAP_MISSING

    print(
        "[p4-kit] the installed 'plugins-kit:bootstrap' plugin is too old or "
        "stale for p4-kit's code review "
        f"(requires bootstrap >= {_MIN_BOOTSTRAP_VERSION}; "
        f"missing: {_BOOTSTRAP_FRONTIER}). Run "
        "`claude plugin update bootstrap@plugins-kit`. Then start a new "
        "session and retry.",
        file=sys.stderr,
    )
    sys.exit(EXIT_BOOTSTRAP_MISSING)

try:
    review_pipeline = importlib.import_module("bootstrap_lib.code_review.pipeline")
    ledger = importlib.import_module("bootstrap_lib.code_review.ledger")
except ModuleNotFoundError as exc:
    from bootstrap_guard import require_bootstrap

    if exc.name == "bootstrap_lib":
        require_bootstrap(
            "p4-kit", feature="code review", missing="bootstrap_lib", force=True
        )
    _exit_bootstrap_too_old()
except ImportError:
    _exit_bootstrap_too_old()

# `run_vcs(timeout=...)` is the frontier API. Importing its module cannot prove
# that the linked bootstrap copy accepts the keyword, so inspect the signature
# before any review path can call it.
try:
    run_vcs_parameters = inspect.signature(review_pipeline.run_vcs).parameters
except (AttributeError, TypeError, ValueError):
    _exit_bootstrap_too_old()
if "timeout" not in run_vcs_parameters:
    _exit_bootstrap_too_old()

# Repair PATH before any subprocess fan-out. On Windows, a bloated
# launching-shell PATH can overrun cmd.exe's variable size limit during
# venv activation and leave this Python with a stripped PATH that
# breaks `subprocess.run(["p4", ...])` with FileNotFoundError. Pulling
# the registry-canonical PATH back in restores p4 visibility.
from bootstrap_lib.path_repair import repair_path  # noqa: E402

# Shared VCS-neutral review pipeline -- subprocess wrapper, section
# splitting, chunking + CLAUDE.md walk + submit-gate scan, bundle
# emission. See bootstrap_lib/code_review/pipeline.py.
from bootstrap_lib.code_review.mechanical import requires_pre_image  # noqa: E402
from bootstrap_lib.code_review.pipeline import (  # noqa: E402
    assemble_bundle,
    emit_bundle,
    matches_claim,
    preimage_relpath,
    run_vcs,
    split_sections,
)

repair_path()


# This module is the PERFORCE VcsAdapter (bootstrap_lib.code_review.vcs_adapter)
# by shape, not by inheritance: get_workspace_root -> workspace_root, CL-number
# resolution -> resolve_target, extract_diff (over fetch_describe) -> fetch_diff,
# _parse_p4_header -> parse_header, _p4_diff_to_sections -> diff_to_sections,
# parse_file_actions + resolve_local_paths -> enumerate_changed_files,
# find_unreconciled -> hygiene_unincluded, find_unresolved -> hygiene_unresolved,
# materialize_preimage -> materialize_preimage. p4 DOES implement the auto-shelve
# optional capability: auto_shelve_cl + fetch_shelf_fingerprint -> snapshot_change,
# cleanup_auto_shelve -> cleanup, AND materialize_preimage (used by the claim
# mechanism). See the protocol docstring for the full contract before changing
# any of these shapes.


# Captures (depot, rev, filetype). The filetype (e.g. `text`, `binary`,
# `binary+l`) drives the binary guard in extract_diff's hunk synthesis.
_FILE_HEADER = re.compile(r"^==== (//[^#]+)#(\d+) \(([^)]*)\) ====\s*$")
_AFFECTED_LINE = re.compile(r"^\.\.\. (//[^#]+)#(\d+) ([\w/]+)\s*$")
_CHANGE_OWNER_HEADER = re.compile(
    r"^Change\s+\d+\s+by\s+([^@\s]+)@([^\s]+)\s+on(?:\s|$)"
)
_RECONCILE_ACTIONS = {"add", "edit", "delete"}

_ADD_ACTIONS = {"add", "branch", "move/add", "import"}
_DELETE_ACTIONS = {"delete", "move/delete", "purge"}

# Shared with p4kit_vcs's own adapter (lib/p4kit_vcs/p4_vcs.py, whose
# _runner_timeout_s does the equivalent read) so a p4 subprocess spawned by
# either path honors the same knob. Read directly here rather than imported:
# this script does not put plugins/p4-kit/lib on sys.path (only its own
# scripts/ dir and the plugin root), so importing p4kit_vcs.p4_vcs would add a
# path-manipulation step this small a read does not justify.
_P4_TIMEOUT_ENV_VAR = "P4KIT_VCS_TIMEOUT_S"
_P4_DEFAULT_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class ShelfScanResult:
    """Shelf digests, actions, and scan status used during bundle assembly."""

    digests: dict[str, str] = field(default_factory=dict)
    actions: dict[str, str] = field(default_factory=dict)
    scan_ok: bool = True
    scan_reason: str = ""


def _read_ztag_records(
    output: str, *, record_start_field: Optional[str] = None
) -> list[dict[str, str]]:
    """Parse `p4 -ztag` output into field-value records.

    A blank line ends a record. The final record does not require a trailing
    blank line. Some commands omit separators, so callers can name a field
    that also starts the next record.
    """
    records: list[dict[str, str]] = []
    record: dict[str, str] = {}

    def flush() -> None:
        nonlocal record
        if record:
            records.append(record)
            record = {}

    for line in output.splitlines():
        if line.strip() == "":
            flush()
            continue
        if not line.startswith("... "):
            continue
        tagged = line[len("... "):]
        field, separator, value = tagged.partition(" ")
        if record_start_field == field and record:
            flush()
        record[field] = value.strip() if separator else ""
    flush()
    return records


def _p4_timeout_s() -> float:
    """Read the p4 subprocess timeout (seconds) from `P4KIT_VCS_TIMEOUT_S`.

    Falls back to `_P4_DEFAULT_TIMEOUT_S` when the variable is unset or holds
    a value `float()` rejects.
    """
    raw = os.environ.get(_P4_TIMEOUT_ENV_VAR)
    if not raw:
        return _P4_DEFAULT_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return _P4_DEFAULT_TIMEOUT_S

# Cap each `p4 ... <paths>` invocation. Windows' CreateProcess limits the
# combined command line to ~32 KB; bulk CLs (asset reconciles, regen passes)
# can easily push 500+ depot paths totalling 70+ KB into one call and trip
# `FileNotFoundError: [WinError 206] The filename or extension is too long`.
# 100 keeps each batch well under any platform's limit with room to spare.
_P4_PATH_BATCH = 100

# Max bytes per per-chunk diff file. Sized for the Read tool: large CLs
# fail with "file too large" above some unpublished threshold (a 1.4 MB
# diff hit it on CL 148623, 119 files). 1 MB leaves ~40% headroom under
# the only known failure point and keeps chunk counts close to 1 for
# typical CLs. Tune downward if a Read failure surfaces.
MAX_CHUNK_BYTES = 1024 * 1024

# Where bundles land on disk. <CL> directory holds bundle.json and
# chunks/. Overwritten on each prepare_review run for the same CL.
# Derived from bootstrap_guard.data_dir("p4-kit") -- not hand-built from
# Path.home() -- so CLAUDE_BOOTSTRAP_DATA_ROOT redirects this too. Without
# that, a scripts/claude_plugin_test.py session correctly re-execs into the
# REDIRECTED venv but this process would still write bundles, pre-images and
# the durable ledger.json into the PRODUCTION tree: a test-session review that
# declines a finding would mutate the real declined-findings memory.
DEFAULT_BUNDLE_ROOT = data_dir("p4-kit") / "reviews"


def _ledger_path() -> Path:
    """The declined-findings ledger path: a single JSON file in the plugin's
    version-independent data dir, a sibling of the per-CL bundle dirs.
    Keyed per change-id (CL). See bootstrap_lib.code_review.ledger.

    Deliberately LAZY (re-derives from bootstrap_guard.data_dir on every
    call) rather than a module-level constant -- a constant computed once at
    import time would not honour CLAUDE_BOOTSTRAP_DATA_ROOT if it changes
    within the same process after this module has already been imported.
    """
    return data_dir("p4-kit") / "reviews" / "ledger.json"


def run_p4(args: list[str]) -> tuple[int, str, str]:
    """Run a p4 command, return (returncode, stdout, stderr).

    Thin wrapper over the shared run_vcs, which forces UTF-8 decoding so
    non-Latin-1 content (CJK, emoji) in diffs doesn't abort the subprocess
    reader thread on Windows, whose default text decoder is the system
    ANSI codepage (cp1252 on en-US/en-GB). Bounded by `_p4_timeout_s`
    (`P4KIT_VCS_TIMEOUT_S`) so an unreachable server cannot hang the review.
    """
    return run_vcs("p4", args, timeout=_p4_timeout_s())


def has_describe_content(output: str) -> bool:
    """True if p4 describe output has reviewable content.

    Reviewable means EITHER:
    - the Differences section has at least one ==== file header (which
      extract_diff parses directly), OR
    - the Affected/Shelved files section lists at least one synthesizable
      action (add/delete), since pure-add and pure-delete CLs have empty
      Differences sections but extract_diff can fill them in via p4 print.

    A describe with only `edit` actions and no Differences headers is not
    reviewable here -- edits need real diff bodies, not synthesis.
    """
    if "Differences ..." in output:
        after = output.split("Differences ...", 1)[1]
        if any(_FILE_HEADER.match(line) for line in after.splitlines()):
            return True
    actions = parse_file_actions(output)
    return any(
        action in _ADD_ACTIONS or action in _DELETE_ACTIONS
        for _, action in actions.values()
    )


def _is_pending(output: str) -> bool:
    """True if the first `Change ...` header line marks the CL as `*pending*`."""
    for line in output.splitlines():
        if line.startswith("Change "):
            return "*pending*" in line
    return False


def _parse_change_owner(output: str) -> Optional[dict[str, str]]:
    """Return the user and client from the first parseable Change header."""
    for line in output.splitlines():
        if not line.startswith("Change "):
            continue
        match = _CHANGE_OWNER_HEADER.match(line)
        if match:
            return {"user": match.group(1), "client": match.group(2)}
        return None
    return None


class PendingUnshelvedError(ValueError):
    """Raised when a pending CL has no shelved content to diff.

    Distinct from a generic `no describe content` failure so callers (notably
    `build_bundle`) can react with auto-shelve + retry instead of propagating.
    """


def fetch_describe(cl: str) -> tuple[str, bool]:
    """Return (`p4 describe -du` output, is_shelved) for CL.

    Routing:
    - Submitted CLs come back from the regular describe with `is_shelved=False`
      so synthesis fetches via `#<rev>`.
    - Pending CLs are routed to the shelved (`-S`) describe with `is_shelved=True`
      so synthesis fetches via `@=<CL>`. Going through `#<rev>` would fail for
      pending adds because no submitted revision exists yet.
    - Pending CLs that have not been shelved raise PendingUnshelvedError so the
      caller can auto-shelve and retry.
    """
    rc, out, _ = run_p4(["describe", "-du", cl])
    if rc == 0 and not _is_pending(out) and has_describe_content(out):
        return out, False

    rc_s, out_s, _ = run_p4(["describe", "-du", "-S", cl])
    if rc_s == 0 and has_describe_content(out_s):
        return out_s, True

    if rc == 0 and _is_pending(out):
        raise PendingUnshelvedError(
            f"pending CL {cl} has no shelved content to review"
        )
    raise ValueError(f"no describe content found for CL {cl} (tried committed and shelved)")


def fetch_shelf_fingerprint(cl: str) -> ShelfScanResult:
    """Return shelf digests, actions, and scan status for a CL.

    Empty digests if no shelf exists. Uses `p4 -ztag fstat -Ol //...@=<CL>`;
    `-Ol` forces the per-revision `digest` field so the fingerprint is a
    content-hash of the shelved file (cheap -- no content download).

    Files shelved as deletes have no digest; recorded as empty string so the
    file's presence in the shelf is still part of the fingerprint.
    """
    rc, out, err = run_p4(["-ztag", "fstat", "-Ol", f"//...@={cl}"])
    if rc != 0:
        detail = err.strip() or out.strip()
        if "no such file" in detail.lower() or "no file(s)" in detail.lower():
            return ShelfScanResult()
        return ShelfScanResult(
            scan_ok=False,
            scan_reason=detail or f"exit {rc}",
        )
    digests: dict[str, str] = {}
    actions: dict[str, str] = {}
    for record in _read_ztag_records(out):
        depot = record.get("depotFile")
        if not depot:
            continue
        digests[depot] = record.get("digest", "")
        action = ""
        for field, value in record.items():
            if field in {"headAction", "action"}:
                action = value
        if action:
            actions[depot] = action
    return ShelfScanResult(digests=digests, actions=actions)


def _parse_opened_files(output: str) -> dict[str, str]:
    """Parse depot path and action pairs from `p4 -ztag opened`."""
    opened: dict[str, str] = {}
    for record in _read_ztag_records(output):
        depot = record.get("depotFile")
        action = record.get("action")
        if depot and action:
            opened[depot] = action
    return opened


def fetch_opened_files(cl: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Return the CL's open depot paths and actions."""
    rc, out, err = run_p4(["-ztag", "opened", "-c", cl])
    if rc != 0:
        reason = err.strip() or out.strip() or f"exit {rc}"
        return {}, [{"scan": "shelf_opened", "reason": reason}]
    return _parse_opened_files(out), []


def shelf_divergence(
    cl: str,
    shelf_scan: ShelfScanResult,
    opened: Optional[dict[str, str]] = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Return deterministic membership and action differences for a pending CL."""
    incomplete: list[dict[str, str]] = []
    if not shelf_scan.scan_ok:
        incomplete.append(
            {"scan": "shelf_fingerprint", "reason": shelf_scan.scan_reason}
        )
        return [], incomplete
    if opened is None:
        opened, incomplete = fetch_opened_files(cl)
        if incomplete:
            return [], incomplete
    shelf_fingerprint = shelf_scan.digests
    shelf_actions = shelf_scan.actions
    divergence: list[dict[str, str]] = []
    for depot in sorted(set(opened) - set(shelf_fingerprint)):
        divergence.append({"depot": depot, "kind": "opened after shelving"})
    for depot in sorted(set(shelf_fingerprint) - set(opened)):
        divergence.append({"depot": depot, "kind": "not open in CL"})
    for depot in sorted(set(opened) & set(shelf_fingerprint)):
        if shelf_actions.get(depot) and opened[depot] != shelf_actions[depot]:
            divergence.append({"depot": depot, "kind": "open action differs"})
    return divergence, incomplete


def _shelf_content_drift(
    shelf_fingerprint: dict[str, str],
    opened: dict[str, str],
    local_map: dict[str, Optional[str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Compare readable local files against the shelf and report skipped paths."""
    drift: list[dict[str, str]] = []
    incomplete: list[dict[str, str]] = []
    for depot in sorted(set(shelf_fingerprint) & set(opened)):
        digest = shelf_fingerprint[depot]
        local = local_map.get(depot)
        if not digest or not local:
            if digest and not local:
                incomplete.append(
                    {"scan": "shelf_drift", "reason": f"no local mapping for {depot}"}
                )
            continue
        try:
            local_digest = hashlib.md5(Path(local).read_bytes()).hexdigest().upper()
        except (OSError, ValueError) as exc:
            incomplete.append({"scan": "shelf_drift", "reason": f"could not hash {depot}: {exc}"})
            continue
        # The shelf digest is server-normalized; local line-ending differences
        # can false-positive, so this path warns rather than refuses.
        if local_digest != digest.upper():
            drift.append({"depot": depot, "local": local})
    return drift, incomplete


def auto_shelve_cl(cl: str) -> ShelfScanResult:
    """Run `p4 shelve -c <cl>` and return the resulting shelf fingerprint.

    Raises ValueError on shelve failure or if no shelved files appear afterward
    (pathological -- impossible because the caller only invokes this when
    the CL has open files but no shelf).
    """
    rc, out, err = run_p4(["shelve", "-c", cl])
    if rc != 0:
        raise ValueError(
            f"p4 shelve -c {cl} failed: {(err or out).strip() or '(no output)'}"
        )
    shelf_scan = fetch_shelf_fingerprint(cl)
    if not shelf_scan.digests:
        raise ValueError(
            f"p4 shelve -c {cl} reported success but no shelved files were found afterward"
        )
    return shelf_scan


def parse_description(describe_output: str) -> str:
    """Extract the indented description block following the `Change ...` header."""
    lines = describe_output.splitlines()
    desc_lines: list[str] = []
    in_desc = False
    for line in lines:
        if not in_desc:
            if line.startswith("Change "):
                in_desc = True
            continue
        if line.startswith("\t"):
            desc_lines.append(line[1:])
        elif desc_lines:
            break
    return "\n".join(desc_lines).strip()


def parse_depot_files(describe_output: str) -> list[str]:
    """Extract depot paths from `==== //depot/path#rev (type) ====` headers."""
    files: list[str] = []
    for line in describe_output.splitlines():
        m = _FILE_HEADER.match(line)
        if m:
            files.append(m.group(1))
    return files


def parse_file_actions(describe_output: str) -> dict[str, tuple[str, str]]:
    """Map depot path -> (rev, action) from 'Affected files ...' / 'Shelved files ...' sections.

    Lines look like: `... //depot/path#rev action` (action e.g. `add`, `edit`, `delete`, `move/add`).
    Stops parsing when `Differences ...` is reached.
    """
    actions: dict[str, tuple[str, str]] = {}
    in_section = False
    for line in describe_output.splitlines():
        if line.startswith("Affected files ...") or line.startswith("Shelved files ..."):
            in_section = True
            continue
        if not in_section:
            continue
        if line.startswith("Differences ..."):
            break
        m = _AFFECTED_LINE.match(line)
        if m:
            actions[m.group(1)] = (m.group(2), m.group(3))
    return actions


def _parse_p4_header(line: str) -> Optional[dict]:
    """Header matcher for the shared splitter."""
    m = _FILE_HEADER.match(line)
    if not m:
        return None
    return {"depot": m.group(1), "rev": m.group(2), "type": m.group(3)}


def split_diff_sections(diff_text: str) -> tuple[str, list[dict]]:
    """Split diff text into (preamble, [{depot, rev, type, header, body}, ...]) by file header."""
    return split_sections(diff_text, _parse_p4_header)


def _content_spec(
    depot_path: str, rev: str, cl: str, is_shelved: bool, is_delete: bool
) -> Optional[str]:
    """File spec addressing the content a synthesized hunk should show.

    - Shelved add/edit: `//depot/path@=<CL>` (shelved content at this CL)
    - Shelved delete:   `//depot/path#head` (head rev is the content about to be deleted)
    - Submitted add:    `//depot/path#<rev>` (content at the submitted rev)
    - Submitted delete: `//depot/path#<rev-1>` (content prior to deletion)

    Returns None when no addressable content exists (delete at rev 1).
    """
    if is_shelved:
        return f"{depot_path}#head" if is_delete else f"{depot_path}@={cl}"
    if is_delete:
        try:
            rev_num = int(rev)
        except ValueError:
            return None
        if rev_num <= 1:
            return None
        return f"{depot_path}#{rev_num - 1}"
    return f"{depot_path}#{rev}"


_UTF16_BOM_LE = b"\xff\xfe"
_UTF16_BOM_BE = b"\xfe\xff"


def _is_utf16_filetype(filetype: Optional[str]) -> bool:
    """True when a p4 filetype's base (before `+modifiers`) is exactly `utf16`."""
    if not filetype:
        return False
    return filetype.split("+", 1)[0].strip().lower() == "utf16"


def _decode_utf16_aware(data: bytes) -> str:
    """Decode raw file bytes as UTF-16 when a BOM or the classic every-other-
    byte-NUL pattern is present, else as UTF-8.

    Whether `p4 print` emits a `utf16`-typed file's bytes verbatim (as UTF-16)
    or translates them to the client charset (UTF-8) is `hypothesis:` here --
    untestable without a live p4 server in unicode mode. This function is
    correct under either reading: real UTF-16 bytes are recovered via the
    sniff; already-UTF-8 bytes carry neither a UTF-16 BOM nor the
    NUL-alternation pattern, so they use the UTF-8 branch.
    """
    if data[:2] in (_UTF16_BOM_LE, _UTF16_BOM_BE):
        return data.decode("utf-16", errors="replace")
    sample = data[:512]
    if len(sample) >= 4:
        even_nul = sum(1 for b in sample[0::2] if b == 0)
        odd_nul = sum(1 for b in sample[1::2] if b == 0)
        threshold = len(sample) // 4
        if even_nul > threshold or odd_nul > threshold:
            return data.decode("utf-16", errors="replace")
    return data.decode("utf-8", errors="replace")


def _fetch_content_bytes_via_outfile(spec: str) -> Optional[bytes]:
    """Fetch `spec`'s content as raw bytes via `p4 print -q -o <tmp>`.

    Writing to a file sidesteps `run_vcs`'s forced UTF-8 stdout decode (see
    its docstring in bootstrap_lib.code_review.pipeline), so the bytes read
    back are exactly what p4 wrote -- needed to sniff for UTF-16 before
    committing to a text decoding. Used only for utf16-typed files; every
    other filetype keeps the plain captured-stdout path.
    """
    fd, tmp_name = tempfile.mkstemp(prefix="prepare_review_print_")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        rc, _, _ = run_p4(["print", "-q", "-o", str(tmp_path), spec])
        if rc != 0:
            return None
        return tmp_path.read_bytes()
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def fetch_file_content(
    depot_path: str,
    rev: str,
    cl: str,
    is_shelved: bool,
    is_delete: bool,
    filetype: Optional[str] = None,
) -> Optional[str]:
    """Fetch file content via `p4 print -q` (see _content_spec for addressing).

    A `utf16`-typed file is fetched via a temp-file `-o` write and sniffed for
    a UTF-16 BOM / NUL-alternation pattern before deciding how to decode it --
    see `_fetch_content_bytes_via_outfile` and `_decode_utf16_aware`. This is
    a decoding concern only: `utf16` stays classified as text-like by
    `_is_text_filetype` (unchanged), so it keeps going through the normal
    add/delete hunk synthesis rather than the binary-placeholder path.
    `filetype` defaults to `None`, which is never `utf16`, so every existing
    caller that does not pass it keeps the unchanged captured-stdout path.
    """
    spec = _content_spec(depot_path, rev, cl, is_shelved, is_delete)
    if spec is None:
        return None
    if _is_utf16_filetype(filetype):
        data = _fetch_content_bytes_via_outfile(spec)
        if data is None:
            return None
        return _decode_utf16_aware(data)
    rc, out, _ = run_p4(["print", "-q", spec])
    if rc != 0:
        return None
    return out


def fetch_filetype(
    depot_path: str, rev: str, cl: str, is_shelved: bool, is_delete: bool
) -> Optional[str]:
    """Look up the p4 filetype for a file omitted from the Differences section.

    Files with a ==== header carry their type inline; omitted files (e.g.
    pure adds in mixed shelved CLs) need an fstat. `type` is the open/shelved
    filetype; `headType` covers submitted revisions -- prefer `type`, fall
    back to `headType`. Returns None on any failure (caller defaults to text,
    the default behavior).
    """
    spec = _content_spec(depot_path, rev, cl, is_shelved, is_delete)
    if spec is None:
        return None
    rc, out, _ = run_p4(["fstat", "-T", "type,headType", spec])
    if rc != 0:
        return None
    found: dict[str, str] = {}
    for line in out.splitlines():
        for field in ("type", "headType"):
            prefix = f"... {field} "
            if line.startswith(prefix):
                found[field] = line[len(prefix):].strip()
    return found.get("type") or found.get("headType")


def fetch_file_size(
    depot_path: str, rev: str, cl: str, is_shelved: bool, is_delete: bool
) -> Optional[int]:
    """Byte size of the content a synthesized hunk would have shown.

    Uses `p4 fstat -Ol -T fileSize` (per-revision field; no content
    download). Returns None when the size is unavailable.
    """
    spec = _content_spec(depot_path, rev, cl, is_shelved, is_delete)
    if spec is None:
        return None
    rc, out, _ = run_p4(["fstat", "-Ol", "-T", "fileSize", spec])
    if rc != 0:
        return None
    for line in out.splitlines():
        if line.startswith("... fileSize "):
            try:
                return int(line[len("... fileSize "):].strip())
            except ValueError:
                return None
    return None


# p4 base filetypes whose content must not be inlined into a text diff.
# Substring "binary" covers binary/xbinary/ubinary/...; the named set covers
# the remaining non-text bases. Unknown or empty types default to text
# (the default behavior without a recognized non-text type).
_NON_TEXT_BASE_TYPES = {"apple", "resource", "tempobj", "ctempobj", "uresource"}


def _is_text_filetype(filetype: Optional[str]) -> bool:
    """True when a p4 filetype's base (before `+modifiers`) is text-like."""
    if not filetype:
        return True
    base = filetype.split("+", 1)[0].strip().lower()
    if "binary" in base:
        return False
    return base not in _NON_TEXT_BASE_TYPES


def _binary_placeholder(action_word: str, size: Optional[int]) -> str:
    """One-line stand-in for binary content we refuse to inline."""
    detail = f"{size} bytes" if size is not None else "size unknown"
    return f"(binary file {action_word}: {detail})\n"


def synthesize_add_hunk(content: str) -> str:
    """Produce a synthetic `@@ -0,0 +1,N @@` new-file hunk with `+` prefix on each line."""
    lines = content.splitlines()
    if not lines:
        return ""
    body = "\n".join(f"+{line}" for line in lines)
    return f"@@ -0,0 +1,{len(lines)} @@\n{body}\n"


def synthesize_delete_hunk(content: str) -> str:
    """Produce a synthetic `@@ -1,N +0,0 @@` deleted-file hunk with `-` prefix on each line."""
    lines = content.splitlines()
    if not lines:
        return ""
    body = "\n".join(f"-{line}" for line in lines)
    return f"@@ -1,{len(lines)} +0,0 @@\n{body}\n"


def extract_diff(
    describe_output: str,
    actions: Optional[dict[str, tuple[str, str]]] = None,
    cl: str = "",
    is_shelved: bool = False,
) -> str:
    """Return diff content after `Differences ...`, synthesizing hunks for add/delete files.

    If `actions` is provided, file sections whose body has no `@@` hunk are filled with a
    synthesized new-file (for add-style actions) or deleted-file (for delete-style actions)
    hunk, by fetching content via `p4 print`. A warning is emitted to stderr naming any
    files we could not synthesize.

    Binary guard: non-text filetypes (binary, apple, resource, ...) are never
    content-inlined -- a shelved .uasset would mojibake-bloat the chunks. The
    type comes from the ==== header's `(type)` field, or `p4 fstat` for files
    omitted from Differences. Binary add/delete sections get a one-line
    `(binary file added: N bytes)` placeholder instead.
    """
    if "Differences ..." not in describe_output:
        return ""
    raw = describe_output.split("Differences ...", 1)[1].lstrip("\n")
    if not actions:
        return raw

    preamble, sections = split_diff_sections(raw)
    result_parts: list[str] = [preamble] if preamble else []
    synthesized_adds: list[str] = []
    synthesized_deletes: list[str] = []
    skipped_binaries: list[str] = []
    unhandled: list[tuple[str, str]] = []
    seen_depots: set[str] = set()

    for sec in sections:
        depot = sec["depot"]
        rev = sec["rev"]
        header = sec["header"]
        body = sec["body"]
        seen_depots.add(depot)

        if "@@" in body:
            result_parts.append(header + body)
            continue

        action_info = actions.get(depot)
        if action_info is None:
            result_parts.append(header + body)
            continue

        _, action = action_info
        is_add = action in _ADD_ACTIONS
        is_delete = action in _DELETE_ACTIONS
        if (is_add or is_delete) and not _is_text_filetype(sec.get("type")):
            size = fetch_file_size(depot, rev, cl, is_shelved, is_delete)
            placeholder = _binary_placeholder(
                "deleted" if is_delete else "added", size
            )
            result_parts.append(header + body + placeholder)
            skipped_binaries.append(depot)
            continue
        if is_add:
            content = fetch_file_content(
                depot, rev, cl, is_shelved, is_delete=False, filetype=sec.get("type")
            )
            if content is not None:
                hunk = synthesize_add_hunk(content)
                result_parts.append(header + body + hunk)
                synthesized_adds.append(depot)
                continue
            unhandled.append((depot, action))
        elif is_delete:
            content = fetch_file_content(
                depot, rev, cl, is_shelved, is_delete=True, filetype=sec.get("type")
            )
            if content is not None:
                hunk = synthesize_delete_hunk(content)
                result_parts.append(header + body + hunk)
                synthesized_deletes.append(depot)
                continue
            unhandled.append((depot, action))

        result_parts.append(header + body)

    # Synthesize complete sections for files in `actions` that were omitted from
    # the Differences section entirely (e.g. mixed shelved CLs where pure-adds
    # only appear in "Shelved files ..." and never get a ==== header).
    for depot, (rev, action) in actions.items():
        if depot in seen_depots:
            continue
        is_add = action in _ADD_ACTIONS
        is_delete = action in _DELETE_ACTIONS
        if not (is_add or is_delete):
            continue
        # No ==== header to read the type from; ask fstat. Unknown -> text
        # (the historical hardcoded default).
        filetype = fetch_filetype(depot, rev, cl, is_shelved, is_delete) or "text"
        synthesized_header = f"==== {depot}#{rev} ({filetype}) ====\n"
        if not _is_text_filetype(filetype):
            size = fetch_file_size(depot, rev, cl, is_shelved, is_delete)
            placeholder = _binary_placeholder(
                "deleted" if is_delete else "added", size
            )
            result_parts.append(synthesized_header + placeholder)
            skipped_binaries.append(depot)
            continue
        if is_add:
            content = fetch_file_content(
                depot, rev, cl, is_shelved, is_delete=False, filetype=filetype
            )
            if content is not None:
                result_parts.append(synthesized_header + synthesize_add_hunk(content))
                synthesized_adds.append(depot)
                continue
            unhandled.append((depot, action))
        else:
            content = fetch_file_content(
                depot, rev, cl, is_shelved, is_delete=True, filetype=filetype
            )
            if content is not None:
                result_parts.append(synthesized_header + synthesize_delete_hunk(content))
                synthesized_deletes.append(depot)
                continue
            unhandled.append((depot, action))

    if skipped_binaries:
        print(
            f"prepare_review: skipped binary content for {len(skipped_binaries)} file(s) "
            "(placeholder emitted): " + ", ".join(skipped_binaries),
            file=sys.stderr,
        )
    if synthesized_adds:
        print(
            f"prepare_review: synthesized add hunks for {len(synthesized_adds)} file(s): "
            + ", ".join(synthesized_adds),
            file=sys.stderr,
        )
    if synthesized_deletes:
        print(
            f"prepare_review: synthesized delete hunks for {len(synthesized_deletes)} file(s): "
            + ", ".join(synthesized_deletes),
            file=sys.stderr,
        )
    if unhandled:
        items = ", ".join(f"{p} ({a})" for p, a in unhandled)
        print(
            f"prepare_review: WARNING: could not synthesize hunks for: {items}",
            file=sys.stderr,
        )

    return "".join(result_parts)


def _p4_diff_to_sections(diff_text: str) -> tuple[str, list[dict]]:
    """Adapter: p4-format diff string -> generic (preamble, [DiffSection]).

    The shared chunker in bootstrap_lib.code_review takes a list of vendor-
    neutral `{identifier, text}` dicts. p4's `==== //depot/path#rev ====`
    headers carry the depot path as the identifier; the full section text
    (header + hunks) becomes `text`.
    """
    preamble, sections = split_diff_sections(diff_text)
    return preamble, [
        {"identifier": s["depot"], "text": s["header"] + s["body"]}
        for s in sections
    ]


def resolve_local_paths(depot_paths: list[str]) -> dict[str, Optional[str]]:
    """Map each depot path to a local workspace path via `p4 -ztag where`.

    Returns {depot_path: local_path_or_None}. Files not in the workspace map to None.

    Batched in chunks of `_P4_PATH_BATCH` so bulk CLs don't trip the Windows
    CreateProcess command-line length limit (~32 KB).

    A batch's overall return code is non-zero when ANY argument in it is
    unmapped (e.g. `<path> - file(s) not in client view.`), but `p4 where`
    still emits ztag rows for every argument it COULD map. Stdout is parsed
    unconditionally so one unmapped file doesn't cost the rest of the batch
    its local paths; a batch is treated as empty only when stdout itself is
    empty.
    """
    result: dict[str, Optional[str]] = {p: None for p in depot_paths}
    if not depot_paths:
        return result

    for i in range(0, len(depot_paths), _P4_PATH_BATCH):
        chunk = depot_paths[i:i + _P4_PATH_BATCH]
        _, out, _ = run_p4(["-ztag", "where", *chunk])
        for record in _read_ztag_records(out, record_start_field="depotFile"):
            depot = record.get("depotFile")
            if depot and "path" in record:
                result[depot] = record["path"]
    return result


def compute_minimal_dirs(
    local_paths: list[Optional[str]],
    workspace_root: Optional[Path] = None,
) -> list[tuple[Path, bool]]:
    """Collapse parent directories of `local_paths` to the minimal covering set.

    Returns a list of (directory, recursive) pairs. `recursive=True` means scan
    `<dir>/...`; `recursive=False` means scan `<dir>/*` (immediate children only).

    Given file paths, returns the set of containing directories with descendants
    removed: e.g. {/a, /a/b, /c} collapses to [(/a, True), (/c, True)]. A single
    recursive `p4 reconcile -n <dir>/...` over each then covers everything.

    The workspace root is treated specially: it never absorbs descendants, and
    if it appears in the parent set it is returned with `recursive=False`. This
    prevents `p4 reconcile -n <root>/...` from crawling every untracked tree in
    the workspace when a CL happens to touch a root-level file.

    Non-existent paths and `None` entries are skipped (e.g. files outside the
    workspace, or whose parent directory was deleted as part of the CL).
    """
    ws_root = workspace_root.resolve() if workspace_root else None

    dirs: set[Path] = set()
    for p in local_paths:
        if not p:
            continue
        try:
            d = Path(p).parent.resolve()
        except OSError:
            continue
        if d.is_dir():
            dirs.add(d)
    if not dirs:
        return []

    root_present = ws_root in dirs
    # Exclude the workspace root from the descendant-collapse pass. Letting it
    # absorb deeper dirs would expand a single root-level file into a recursive
    # scan of the whole workspace.
    collapsable = [d for d in dirs if d != ws_root]

    # Sort shallowest-first so a kept ancestor is checked before its descendants.
    sorted_dirs = sorted(collapsable, key=lambda d: len(d.parts))
    minimal: list[tuple[Path, bool]] = []
    kept_paths: list[Path] = []
    for d in sorted_dirs:
        if any(kept == d or kept in d.parents for kept in kept_paths):
            continue
        kept_paths.append(d)
        minimal.append((d, True))

    if root_present:
        minimal.append((ws_root, False))
    return minimal


def _p4_paths_for_dir_specs(dir_specs: list[tuple[Path, bool]]) -> list[str]:
    """Convert scan directories to recursive or immediate-child file specs."""
    return [
        f"{directory}/..." if recursive else f"{directory}/*"
        for directory, recursive in dir_specs
    ]


def find_unreconciled(
    dir_specs: list[tuple[Path, bool]]
) -> tuple[list[dict], list[dict]]:
    """Run `p4 -ztag reconcile -n` over `dir_specs` and return unreconciled files.

    `dir_specs` is a list of (directory, recursive) pairs. Recursive entries are
    scanned as `<dir>/...`; non-recursive entries as `<dir>/*` (immediate
    children only -- used for the workspace root to avoid crawling the whole
    tree).

    Returns `(items, incomplete)`. Each `items` entry:
    {"local": <path>, "depot": <path>, "action": "add"|"edit"|"delete"}.
    `.p4ignore` is honored by p4. Files already opened in any pending CL are skipped.
    A single p4 invocation handles all specs at once (batched -- see
    `_P4_PATH_BATCH`).

    A batch failing for a reason other than "no file(s) to reconcile" (a p4
    error, no workspace, an unreachable server, ...) is NOT folded into an
    empty `items` list -- that would read identically to "ran and found
    nothing" to a caller, the exact ambiguity this scan exists to avoid (see
    the module docstring's `hygiene_incomplete` key). Instead each such batch
    contributes one `{"scan": "unreconciled", "reason": <detail>}` entry to
    `incomplete`, and the scan continues with the remaining batches -- a
    partial hygiene failure must not abort the whole prepare.
    """
    if not dir_specs:
        return [], []
    specs = _p4_paths_for_dir_specs(dir_specs)

    items: list[dict] = []
    incomplete: list[dict] = []
    for i in range(0, len(specs), _P4_PATH_BATCH):
        chunk = specs[i:i + _P4_PATH_BATCH]
        rc, out, err = run_p4(["-ztag", "reconcile", "-n", *chunk])
        # rc != 0 with "no file(s) to reconcile" means nothing to report -- not an error.
        if rc != 0 and "no file(s) to reconcile" not in (err + out):
            reason = err.strip() or out.strip() or f"exit {rc}"
            print(
                f"prepare_review: reconcile check failed (rc={rc}): {reason}",
                file=sys.stderr,
            )
            incomplete.append({"scan": "unreconciled", "reason": reason})
            continue
        items.extend(_parse_reconcile_output(out))
    return items, incomplete


def _partition_own_depot_files(
    items: list[dict],
    own_depot_files: set[str],
    actions: dict[str, tuple[str, str]],
) -> tuple[list[dict], list[dict]]:
    """Split reconcile hits into (unreconciled, stale_open) by CL ownership.

    `p4 reconcile -n` can report a file already open in the CL under some
    action-transition sequences (opened for edit then deleted locally, or
    opened for delete then recreated) even though nothing was forgotten --
    the file is already part of the CL. A hit whose depot path is not owned
    by the CL passes through to `unreconciled` unchanged.

    A hit that IS owned by the CL carries real submit-blocking signal when
    reconcile's proposed action is `delete` (the CL has the file open for
    edit, but it is missing from the workspace) or `add` (the CL has the
    file open for delete, but it is present on disk). Those become
    `stale_open` entries, with `open_action` taken from the CL's own parsed
    actions (not the reconcile row). Any other proposed action on an owned
    depot path (e.g. a spurious `edit` re-detection) carries neither signal
    and is dropped entirely, matching how ownership filtering worked before
    `stale_open` existed.
    """
    unreconciled: list[dict] = []
    stale_open: list[dict] = []
    for item in items:
        depot = item.get("depot")
        if depot not in own_depot_files:
            unreconciled.append(item)
            continue
        proposed = item.get("action")
        if proposed == "delete":
            workspace_state = "missing"
        elif proposed == "add":
            workspace_state = "present"
        else:
            continue
        open_action = actions.get(depot, ("", ""))[1]
        stale_open.append(
            {
                "depot": depot,
                "local": item.get("local") or None,
                "open_action": open_action,
                "workspace_state": workspace_state,
            }
        )
    return unreconciled, stale_open


def _parse_reconcile_output(out: str) -> list[dict]:
    items: list[dict] = []
    for record in _read_ztag_records(out):
        action = record.get("action")
        local = record.get("clientFile")
        if action in _RECONCILE_ACTIONS and local:
            items.append(
                {
                    "local": local,
                    "depot": record.get("depotFile", ""),
                    "action": action,
                }
            )
    return items


def _parse_default_open_output(out: str) -> list[dict[str, str]]:
    """Parse depot path, client path, and action from ztag opened records."""
    items: list[dict[str, str]] = []
    for record in _read_ztag_records(out, record_start_field="depotFile"):
        depot = record.get("depotFile")
        local = record.get("clientFile")
        action = record.get("action")
        if depot and local and action:
            items.append({"local": local, "depot": depot, "action": action})
    return items


def find_default_open(
    dir_specs: list[tuple[Path, bool]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Return files open in the default CL under the reconcile scan paths."""
    if not dir_specs:
        return [], []
    specs = _p4_paths_for_dir_specs(dir_specs)
    items: list[dict[str, str]] = []
    incomplete: list[dict[str, str]] = []
    for i in range(0, len(specs), _P4_PATH_BATCH):
        chunk = specs[i:i + _P4_PATH_BATCH]
        rc, out, err = run_p4(["-ztag", "opened", "-c", "default", *chunk])
        detail = err.strip() or out.strip()
        no_open_files = "not opened on this client" in (out + err).lower()
        if rc != 0 and not no_open_files:
            reason = detail or f"exit {rc}"
            print(
                f"prepare_review: default changelist check failed (rc={rc}): {reason}",
                file=sys.stderr,
            )
            incomplete.append({"scan": "default_open", "reason": reason})
            continue
        items.extend(_parse_default_open_output(out))
    return items, incomplete


def find_unresolved(cl: str) -> tuple[list[dict], list[dict]]:
    """Run `p4 -ztag resolve -n -c <CL>` and return unresolved files in this CL.

    Returns `(items, incomplete)`. Each `items` entry: {"local": <path>,
    "depot": <path>, "resolve_type": <p4 resolveType>, "from_file": <source>}.

    p4 exits non-zero with "no file(s) to resolve" when the CL is clean -- that
    isn't an error. On any other failure, `items` is `[]` (no invented
    findings) but `incomplete` carries one `{"scan": "unresolved", "reason":
    <detail>}` entry, so a caller can tell "ran, found nothing" from "did not
    run" -- an empty `unresolved` alone would look identical to a submittable
    CL. The review still proceeds; this scan failing is not fatal to the
    prepare.
    """
    rc, out, err = run_p4(["-ztag", "resolve", "-n", "-c", cl])
    if rc != 0 and "no file(s) to resolve" not in (err + out):
        reason = err.strip() or out.strip() or f"exit {rc}"
        print(
            f"prepare_review: resolve check failed (rc={rc}): {reason}",
            file=sys.stderr,
        )
        return [], [{"scan": "unresolved", "reason": reason}]

    items: list[dict] = []
    for record in _read_ztag_records(out):
        local = record.get("clientFile", "")
        depot = record.get("toFile", "")
        if local or depot:
            items.append(
                {
                    "local": local,
                    "depot": depot,
                    "resolve_type": record.get("resolveType", ""),
                    "from_file": record.get("fromFile", ""),
                }
            )
    return items, []


def get_workspace_root() -> tuple[Optional[Path], Optional[str]]:
    """Get clientRoot and clientName from one `p4 -ztag info` call."""
    rc, out, _ = run_p4(["-ztag", "info"])
    if rc != 0:
        return None, None
    workspace_root: Optional[Path] = None
    client_name: Optional[str] = None
    for record in _read_ztag_records(out):
        if "clientRoot" in record:
            workspace_root = Path(record["clientRoot"])
        if "clientName" in record:
            value = record["clientName"]
            if value and value.lower() not in {"unknown", "*unknown*"}:
                client_name = value
    return workspace_root, client_name


def materialize_preimage(depot: str, action: str, bundle_dir: Path) -> Optional[str]:
    """Write `depot`'s #have (pre-edit) content into the bundle; return its path.

    An add-style action (add / branch / move/add / import) has no prior
    content, so returns None -- the subject-lens reviewer treats every finding
    as attributable. For an edit/delete/integrate the `#have` revision is the
    workspace's synced copy, i.e. the file as it was before this CL touched it.
    """
    if action in _ADD_ACTIONS:
        return None
    dest = bundle_dir / preimage_relpath(depot)
    dest.parent.mkdir(parents=True, exist_ok=True)
    rc, _, _ = run_p4(["print", "-q", "-o", str(dest), f"{depot}#have"])
    if rc != 0:
        return None
    return str(dest)


def build_bundle(
    cl: str,
    bundle_dir: Path,
    claim_globs: Optional[list[str]] = None,
    ledger_path: Optional[Path] = None,
    review_machine_emitted: bool = False,
) -> dict:
    """Gather CL context, partition diff into chunks on disk, return the index bundle.

    Writes per-file diff fragments under `<bundle_dir>/chunks/chunk-NNN.diff`
    (created if missing; stale chunks removed). The returned dict carries
    `bundle_dir`, `diff_chunks` index, and per-`changed_files` `chunk_index`
    -- the diff text itself is NOT inlined, so the bundle stays small enough
    for downstream stdout / consumer ingestion.

    If the CL is pending with no shelved content, runs `p4 shelve -c <cl>` so
    the diff is fetchable, records `auto_shelved=True` plus the resulting
    shelf fingerprint, and expects a later `--cleanup <bundle_dir>` invocation
    to delete the shelf iff its fingerprint still matches.

    When `claim_globs` is non-empty, changed files whose depot path matches a
    claim pattern are held back from the generic reviewers (see
    assemble_bundle): their pre-image is materialized into the bundle and they
    are surfaced under a top-level `claimed_files` list instead of
    `changed_files`. When empty the bundle is byte-identical to a bundle built
    without claims.

    Machine-emitted files -- detected from a content signature OR from living
    under a path a plugin declares that it writes (bootstrap_lib.code_review
    .machine_emitted and .machine_emitted_paths) -- are likewise held back from
    the reviewers and surfaced under a top-level `machine_emitted_files` list,
    because the review target is the GENERATOR, not its output.
    `review_machine_emitted=True` disables that exclusion for an author who
    explicitly wants the full review.
    """
    claim_globs = claim_globs or []
    auto_shelved = False
    shelf_fingerprint: dict[str, str] = {}
    shelf_observed: Optional[ShelfScanResult] = None
    shelf_scan_incomplete: list[dict[str, str]] = []
    opened: Optional[dict[str, str]] = None
    opened_incomplete: list[dict[str, str]] = []
    try:
        describe, is_shelved = fetch_describe(cl)
    except PendingUnshelvedError:
        # A shelf can appear after fetch_describe reports an unshelved CL.
        # Preserve a shelf created by another process and reuse this scan.
        shelf_observed = fetch_shelf_fingerprint(cl)
        if not shelf_observed.scan_ok:
            raise ValueError(
                f"could not check CL {cl} for a shelf before auto-shelve: "
                f"{shelf_observed.scan_reason}"
            )
        if shelf_observed.digests:
            describe, is_shelved = fetch_describe(cl)
        else:
            # auto_shelve_cl fetches and validates the post-shelve fingerprint;
            # capture that result instead of making another post-shelve query.
            shelf_observed = auto_shelve_cl(cl)
            shelf_fingerprint = shelf_observed.digests
            describe, is_shelved = fetch_describe(cl)
            auto_shelved = True

    workspace_root, client_name = get_workspace_root()
    change_owner = _parse_change_owner(describe)
    foreign_change: Optional[dict[str, str]] = None
    if (
        change_owner is not None
        and client_name is not None
        and change_owner["client"] != client_name
    ):
        foreign_change = change_owner

    if claim_globs and foreign_change is not None:
        raise ValueError(
            f"CL {cl} belongs to foreign client {foreign_change['client']}; "
            f"claim pre-images require its client workspace -- "
            f"re-run without --claim for a plain informational review"
        )

    if _is_pending(describe) and is_shelved and not auto_shelved:
        if shelf_observed is None:
            shelf_observed = fetch_shelf_fingerprint(cl)
        divergence = []
        if foreign_change is not None:
            opened_incomplete = [
                {
                    "scan": "shelf_opened",
                    "reason": f"skipped for foreign client {foreign_change['client']}",
                }
            ]
        if shelf_observed.scan_ok and foreign_change is None:
            opened, opened_incomplete = fetch_opened_files(cl)
            if not opened_incomplete:
                divergence, divergence_incomplete = shelf_divergence(
                    cl, shelf_observed, opened
                )
                opened_incomplete += divergence_incomplete
        elif not shelf_observed.scan_ok:
            shelf_scan_incomplete = [
                {
                    "scan": "shelf_fingerprint",
                    "reason": shelf_observed.scan_reason,
                }
            ]
        if divergence:
            details = "; ".join(
                f"{item['depot']} ({item['kind']})" for item in divergence
            )
            raise ValueError(
                f"CL {cl} shelf does not match its open files: {details}; "
                f"repair with p4 shelve -f -c {cl}"
            )

    # Claim pre-images are materialized from the workspace's #have revision,
    # which for a SUBMITTED CL is POST-change once the workspace has synced past
    # it -- so attribution silently inverts. The skill's scope already excludes
    # submitted CLs; guard the --claim path explicitly with an actionable error.
    # Without --claim, submitted CLs remain reviewable as plain informational
    # reviews (behavior untouched).
    if claim_globs and not _is_pending(describe):
        raise ValueError(
            f"CL {cl} is submitted; claim pre-images require a pending CL -- "
            f"re-run without --claim for a plain informational review"
        )

    description = parse_description(describe)
    actions = parse_file_actions(describe)
    diff = extract_diff(describe, actions, cl, is_shelved)
    # `actions` is the canonical per-file list (Affected/Shelved files section).
    # Pure-add files in mixed shelved CLs may be absent from the Differences
    # section entirely, so deriving the file list from ==== headers undercounts.
    depot_files = list(actions.keys())
    local_map = resolve_local_paths(depot_files)

    shelf_drift: list[dict[str, str]] = []
    shelf_drift_incomplete: list[dict[str, str]] = []
    if foreign_change is not None:
        shelf_drift_incomplete = [
            {
                "scan": "shelf_drift",
                "reason": f"skipped for foreign client {foreign_change['client']}",
            }
        ]
    elif _is_pending(describe) and is_shelved:
        if shelf_observed is not None and shelf_observed.scan_ok:
            if opened is None:
                opened, opened_incomplete = fetch_opened_files(cl)
            if not opened_incomplete:
                shelf_drift, shelf_drift_incomplete = _shelf_content_drift(
                    shelf_observed.digests, opened, local_map
                )
        if shelf_drift:
            paths = ", ".join(item["depot"] for item in shelf_drift)
            print(
                f"prepare_review: shelf content differs from the workspace for {paths}",
                file=sys.stderr,
            )

    preamble, sections = _p4_diff_to_sections(diff)
    files = [
        {"identifier": depot, "depot": depot, "local": local_map.get(depot)}
        for depot in depot_files
    ]
    # Materialize immutable pre-images for locally-owned pending files. A
    # foreign workspace's #have and a submitted CL's #have are not the reviewed
    # pre-image, so those cases deliberately leave the post-image precondition
    # unmet. Within that set, a claimed file always needs one (the triviality
    # guard reads it) and every other file needs one only when a registered
    # mechanical check reads the post-image -- each costs a `p4 print`, so on a
    # large CL the difference is one round-trip per file. Asking the registry
    # rather than assuming means this widens by itself when the first
    # structured-parse check lands. `action` remains an output field only for
    # claims; assemble_bundle strips it from generic changed-file records.
    if foreign_change is None and _is_pending(describe):
        scan_needs_pre_image = requires_pre_image()
        for f in files:
            if not (
                scan_needs_pre_image
                or matches_claim(f["identifier"], claim_globs)
            ):
                continue
            action = actions.get(f["identifier"], ("", ""))[1]
            f["action"] = action
            f["pre_image"] = materialize_preimage(f["depot"], action, bundle_dir)
            f["pre_image_is_empty"] = action in _ADD_ACTIONS
    skip_machine_emitted_scan = (
        foreign_change is not None and not review_machine_emitted
    )
    machine_emitted_incomplete: list[dict[str, str]] = []
    if skip_machine_emitted_scan:
        machine_emitted_incomplete = [
            {
                "scan": "machine_emitted",
                "reason": f"skipped for foreign client {foreign_change['client']}",
            }
        ]
    core = assemble_bundle(
        preamble=preamble,
        sections=sections,
        files=files,
        bundle_dir=bundle_dir,
        max_chunk_bytes=MAX_CHUNK_BYTES,
        workspace_root=workspace_root,
        claim_globs=claim_globs,
        # Deliberately the OLD kwarg spelling. p4-kit and bootstrap are
        # versioned and cached independently, so a new p4-kit routinely runs
        # against a published bootstrap that predates the machine_emitted
        # rename and accepts only `review_generated` -- passing the new name
        # there raises TypeError on every review. The post-rename
        # assemble_bundle still accepts this spelling as a deprecated alias,
        # so the old name works against both. Retire per rename-spec H.2.
        # Machine-emitted detection prefers local file bytes when available.
        # A foreign CL maps to reviewer bytes, so disable that classification
        # rather than excluding shelf content based on another workspace.
        review_generated=review_machine_emitted or skip_machine_emitted_scan,
    )
    changed_files = core["changed_files"]

    # Seed the hygiene scan from files BEFORE routing, not from changed_files
    # alone. assemble_bundle has already stripped claimed and machine-emitted
    # identifiers out of changed_files (their own contract routes them to
    # claimed_files / machine_emitted_files instead), so a fully-claimed CL
    # would otherwise collapse to an empty changed_files, compute_minimal_dirs
    # would return [], find_unreconciled would short-circuit, and p4 would
    # never be invoked -- the forgotten-files gate would report clean without
    # running. Mirrors git-kit's hygiene_sources.
    hygiene_sources = (
        changed_files
        + core.get("claimed_files", [])
        + (core.get("machine_emitted_files") or core.get("generated_files") or [])
    )
    # Foreign ownership is determined before this block. Keep every client-local
    # scan inside the non-foreign branch so reviewer workspace state cannot be
    # attributed to the CL author.
    if foreign_change is not None:
        skip_reason = f"skipped for foreign client {foreign_change['client']}"
        unreconciled: list[dict] = []
        default_open: list[dict] = []
        stale_open: list[dict] = []
        unresolved: list[dict] = []
        unreconciled_incomplete = [
            {"scan": "unreconciled", "reason": skip_reason}
        ]
        default_open_incomplete = [
            {"scan": "default_open", "reason": skip_reason}
        ]
        unresolved_incomplete = [
            {"scan": "unresolved", "reason": skip_reason}
        ]
    else:
        minimal_dirs = compute_minimal_dirs(
            [f["local"] for f in hygiene_sources], workspace_root
        )
        unreconciled_raw, unreconciled_incomplete = find_unreconciled(minimal_dirs)
        unreconciled, stale_open = _partition_own_depot_files(
            unreconciled_raw, set(depot_files), actions
        )
        default_open, default_open_incomplete = find_default_open(minimal_dirs)
        unresolved, unresolved_incomplete = find_unresolved(cl)
    # A failed scan must never serialize as an empty list indistinguishable
    # from "ran and found nothing" -- hygiene_incomplete is always present
    # (empty when all applicable scans ran cleanly) and names which scan(s)
    # could not complete and why. See find_unreconciled / find_unresolved.
    hygiene_incomplete = (
        unreconciled_incomplete
        + default_open_incomplete
        + unresolved_incomplete
        + shelf_scan_incomplete
        + opened_incomplete
        + shelf_drift_incomplete
        + machine_emitted_incomplete
    )

    # Declined-findings ledger. The baseline folds the CL's shelf fingerprint
    # (content) and per-file (rev, action) map (identity) into one token: when
    # the author reshelves / edits / the CL's revisions move, the baseline
    # changes and previously-declined findings re-surface. `shelf_now` reuses
    # the just-captured fingerprint when we auto-shelved; otherwise it is a
    # cheap `fstat -Ol` (no content download), tolerant of an absent shelf.
    if shelf_observed is None:
        shelf_observed = fetch_shelf_fingerprint(cl)
    shelf_now = shelf_observed.digests
    ledger_baseline = ledger.baseline_token({"actions": actions, "shelf": shelf_now})
    change_id = cl
    ledger_hits = ledger.ledger_hits(ledger_path or _ledger_path(), change_id, ledger_baseline)

    bundle: dict = {
        "cl": cl,
        "description": description,
        "project_root": str(workspace_root) if workspace_root else None,
        "bundle_dir": core["bundle_dir"],
        "diff_chunks": core["diff_chunks"],
        "changed_files": changed_files,
        "unique_claude_mds": core["unique_claude_mds"],
        "unreconciled": unreconciled,
        "default_open": default_open,
        "stale_open": stale_open,
        "unresolved": unresolved,
        "hygiene_incomplete": hygiene_incomplete,
        "shelf_drift": shelf_drift,
        "submit_gates": core["submit_gates"],
        "auto_shelved": auto_shelved,
        "shelf_fingerprint": shelf_fingerprint,
        "change_id": change_id,
        "ledger_baseline": ledger_baseline,
        "ledger_hits": ledger_hits,
    }
    if claim_globs:
        bundle["claimed_files"] = core.get("claimed_files", [])
    if foreign_change is not None:
        bundle["foreign_change"] = foreign_change
    # Read NEW-key-or-OLD-key: a bootstrap predating the machine_emitted rename
    # still writes `generated_files`. Tolerating both spellings keeps this kit
    # order-free against the bootstrap half of the rename -- reading only the new
    # key against an older bootstrap would silently drop the "not reviewed"
    # section for files that are ALREADY excluded from the diff chunks.
    emitted = core.get("machine_emitted_files") or core.get("generated_files")
    if emitted:
        bundle["machine_emitted_files"] = emitted
    return bundle


def cleanup_auto_shelve(bundle_dir: Path) -> int:
    """Delete the auto-created shelf for `bundle_dir`'s CL iff it still matches.

    Reads bundle.json. If `auto_shelved` is false we did not create the shelf
    and exit silently. Otherwise re-fingerprints the live shelf and compares
    to the recorded fingerprint:
      - exact match  -> `p4 shelve -d -c <cl>`, brief stderr confirmation.
      - any mismatch -> leave the shelf alone, brief stderr explanation.
      - shelf gone   -> nothing to delete, brief stderr note.

    Deterministic: no inference, no force, no inferring author intent. The
    user's shelved work is never overwritten.
    """
    bundle_path = bundle_dir / "bundle.json"
    if not bundle_path.is_file():
        print(
            f"prepare_review --cleanup: no bundle.json in {bundle_dir}",
            file=sys.stderr,
        )
        return 1
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    if not bundle.get("auto_shelved"):
        return 0
    cl = bundle["cl"]
    recorded = bundle.get("shelf_fingerprint", {})
    current = fetch_shelf_fingerprint(cl).digests
    if not current:
        print(
            f"prepare_review: CL {cl} shelf already gone; nothing to clean up.",
            file=sys.stderr,
        )
        return 0
    if current != recorded:
        print(
            f"prepare_review: CL {cl} shelf changed since review prep; "
            f"leaving in place (your work is preserved).",
            file=sys.stderr,
        )
        return 0
    rc, _, err = run_p4(["shelve", "-d", "-c", cl])
    if rc != 0:
        print(
            f"prepare_review: p4 shelve -d -c {cl} failed: {err.strip() or '(no output)'}",
            file=sys.stderr,
        )
        return 1
    print(
        f"prepare_review: deleted auto-created shelf for CL {cl}.",
        file=sys.stderr,
    )
    return 0


def _parse_args(args: list[str]) -> tuple[list[str], list[str], bool]:
    """Split argv[1:] into (positionals, claim_globs, review_machine_emitted).

    `--claim <glob>` (repeatable) and `--claim=<glob>` collect claim patterns;
    `--review-machine-emitted` turns OFF machine-emitted-artifact exclusion so
    those files are chunked and reviewed like any other; everything else (the CL
    number) is a positional. Raises ValueError on a `--claim` with no value.
    """
    positionals: list[str] = []
    claim_globs: list[str] = []
    review_machine_emitted = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--review-machine-emitted":
            review_machine_emitted = True
            i += 1
        elif a == "--claim":
            if i + 1 >= len(args):
                raise ValueError("--claim requires a glob argument")
            claim_globs.append(args[i + 1])
            i += 2
        elif a.startswith("--claim="):
            claim_globs.append(a[len("--claim="):])
            i += 1
        else:
            positionals.append(a)
            i += 1
    return positionals, claim_globs, review_machine_emitted


def _usage() -> int:
    print(
        "Usage: prepare_review.py <CL> [--claim <glob> ...] [--review-machine-emitted]\n"
        "       prepare_review.py --cleanup <bundle_dir>\n"
        "       prepare_review.py --ledger-record <declined.json>",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str]) -> int:
    # `p4` missing from PATH surfaces as subprocess.run's FileNotFoundError from
    # deep inside whichever p4 call happens to run first (describe, reconcile,
    # shelve fingerprinting, ...) -- catch it here, once, rather than in every
    # call site, and exit with the same convention a build_bundle ValueError
    # uses (1) instead of letting it escape as an unhandled traceback.
    try:
        if len(argv) == 3 and argv[1] == "--cleanup":
            return cleanup_auto_shelve(Path(argv[2]))
        if len(argv) == 3 and argv[1] == "--ledger-record":
            try:
                n = ledger.record_from_file(_ledger_path(), Path(argv[2]))
            except (OSError, ValueError) as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1
            print(f"prepare_review: recorded {n} declined finding(s) into the ledger.", file=sys.stderr)
            return 0
        try:
            positionals, claim_globs, review_machine_emitted = _parse_args(argv[1:])
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 2
        if len(positionals) != 1 or positionals[0].startswith("-"):
            return _usage()
        cl = positionals[0]
        # Validate before building bundle_dir from the unvalidated positional --
        # "../../foo" would otherwise write outside the reviews root and "."
        # would write into the root itself, beside ledger.json, where the
        # stale-chunk sweep operates.
        if not re.fullmatch(r"[0-9]+", cl):
            print(f"Error: CL must be a positive integer, got {cl!r}", file=sys.stderr)
            return 2
        bundle_dir = DEFAULT_BUNDLE_ROOT / cl
        bundle_dir.mkdir(parents=True, exist_ok=True)
        try:
            bundle = build_bundle(
                cl,
                bundle_dir,
                claim_globs=claim_globs,
                review_machine_emitted=review_machine_emitted,
            )
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        return emit_bundle(bundle, bundle_dir)
    except FileNotFoundError as e:
        missing = e.filename or "p4"
        print(
            f"Error: '{missing}' executable not found. p4-kit requires the "
            "Helix Core Command-Line Client (p4) to be installed and on PATH.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
