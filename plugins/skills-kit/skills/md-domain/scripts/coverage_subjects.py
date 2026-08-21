#!/usr/bin/env python3
"""coverage_subjects.py -- produce and verify the JSONL input for the analyze
lane's `subjectsFile` mode (workflow/coverage-detect.js).

Two verbs:

    coverage_subjects.py build <dir> [<dir> ...] --out <file.jsonl>
                               [--tree] [--overrides <file>]
    coverage_subjects.py verify <report.json> <subjects.jsonl>

WHY THIS SHIPS WITH THE PLUGIN RATHER THAN BEING THE CALLER'S PROBLEM.

`subjectsFile` mode exists because a Workflow script has no filesystem: subject
payloads cannot travel through the orchestrator's context at four figures of
directories, so the agents read their own slice of a file instead. That buys the
cost saving and costs two things, and BOTH of them land on the caller:

  * The file has invariants a slice depends on. A slice is a LINE RANGE, so one
    wrapped record, one blank line, or a count that disagrees with the file
    shifts every later slice onto the wrong directories. A consuming project
    should not have to know that. `build` is where those invariants are enforced
    and where the count is produced from the same list that was serialized.

  * The lane cannot verify its own provenance in that mode. It has no
    filesystem, so `root` and `codeFiles` come back ATTESTED BY THE AGENT and
    every record says so (`provenance: agent-attested`). coverage-lane.md makes
    a caller-side re-check MANDATORY before those candidates are promoted, and
    `verify` IS that re-check. A mandatory check with no implementation is how a
    gate quietly becomes an unrun gate -- the same shape as the failure this
    whole capability exists to correct, where criteria that were present and
    correct were simply never reached.

Stdlib-only. Discovery is md-domain's OWN `discover_coverage.build_subject`,
imported rather than reimplemented, so this cannot drift from the subject shape
the lane's arg contract describes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from discover_coverage import (  # noqa: E402
    NOISE_DIR_NAMES,
    build_subject,
    root_exclusion,
)
from vcs_ignore import detect_vcs, ignored_paths  # noqa: E402

META_SUFFIX = ".meta.json"


# ---------------------------------------------------------------------------
# Path canonicalization.
#
# DELIBERATE DUPLICATION, and it is part of why `verify` is worth having. These
# helpers mirror `canonicalSegments` / `canonicalPath` / `splitAnchor` /
# `anchorRejectionReason` in workflow/coverage-detect.js. The rule cannot be
# shared across the JS/Python boundary, so it is implemented twice and pinned
# once: tests/skills-kit/test_coverage_subjects.py runs the same case table
# through this implementation that tests/skills-kit/test_coverage_batching.py
# runs through the lane's. If the two ever disagree, that table fails.
# ---------------------------------------------------------------------------

_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_TRAILING_NUMBER = re.compile(r"^(.*):(\d+)$")


def windows_shaped(path: str) -> bool:
    """True when a path carries a drive letter, a UNC prefix, or a backslash.

    Case-folding is applied ONLY to such paths, and only when either side of a
    comparison is one: folding everywhere would merge two files on a
    case-sensitive filesystem that differ only in case, which is a real shape in
    POSIX trees.
    """
    text = str(path)
    return bool(_DRIVE.match(text)) or text.startswith("\\\\") or "\\" in text


def canonical_segments(path: str) -> list[str]:
    out: list[str] = []
    parts = unicodedata.normalize("NFC", str(path)).replace("\\", "/").split("/")
    for index, seg in enumerate(parts):
        if seg == "":
            if index == 0:
                out.append("")
            continue
        if seg == ".":
            continue
        if seg == "..":
            last = out[-1] if out else None
            if last is not None and last not in ("", ".."):
                out.pop()
                continue
            out.append("..")
            continue
        out.append(seg)
    return out


def canonical_path(path: str, fold: bool) -> str:
    joined = "/".join(canonical_segments(path))
    return joined.lower() if fold else joined


def split_anchor(raw: str) -> tuple[str, int] | None:
    """Split "file:line" or "file:line:col". A line number is REQUIRED.

    Peeled from the END one trailing number group at a time. A single pattern
    with an optional column group binds the LAST number to the line and swallows
    the real line into the filename, so "f.cpp:12:4" parses as the file
    "f.cpp:12" at line 4. Peeling also keeps a drive letter safe, because "C:"
    is never a trailing digit group.
    """
    first = _TRAILING_NUMBER.match(str(raw).strip())
    if not first:
        return None
    second = _TRAILING_NUMBER.match(first.group(1))
    name = (second.group(1) if second else first.group(1)).strip()
    line = int(second.group(2) if second else first.group(2))
    if not name or line < 1:
        return None
    return name, line


def anchor_rejection_reason(raw: str, code_files: list[str]) -> str | None:
    """None when the anchor names a file in `code_files` with a line number.

    MEMBERSHIP, not containment. "Does the anchor sit under the root" admitted an
    empty string, a file that does not exist, a foreign file sharing a directory
    name, and any bare filename at all. A relative spelling is accepted only when
    it is a trailing path-SEGMENT suffix of EXACTLY ONE entry.
    """
    parsed = split_anchor(raw)
    if parsed is None:
        return "no line number"
    name = parsed[0]
    fold = windows_shaped(name) or any(windows_shaped(f) for f in code_files)
    anchor = canonical_path(name, fold)
    if not anchor:
        return "empty path"
    listed = [canonical_path(f, fold) for f in code_files]
    if anchor in listed:
        return None
    hits = [f for f in listed if f.endswith("/" + anchor)]
    if len(hits) == 1:
        return None
    if len(hits) > 1:
        return "ambiguous relative path"
    return "names no file in this subject own code-file list"


def same_path(left: str, right: str) -> bool:
    fold = windows_shaped(left) or windows_shaped(right)
    return canonical_path(left, fold) == canonical_path(right, fold)


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def _is_noise(name: str) -> bool:
    """discover_coverage.walk_directory's own noise predicate, applied verbatim.

    Not a restatement and not a second list: `NOISE_DIR_NAMES` is IMPORTED, so a
    name added there reaches this walk with no change here. That is the whole
    point -- that module's docstring says its exclusions live in one home "so the
    verbs that use it cannot disagree about what it means", and `--tree` is a new
    verb that was using half of that home.

    Two details are inherited rather than improved on, deliberately:

      * Membership is CASE-SENSITIVE, unlike the vendored/generated rules in
        `_skip_reason`. Matching case-insensitively here would prune names that
        module keeps, which is the same disagreement in the opposite direction.
      * `.claude` is the one dot-directory that is NOT noise. It holds
        hand-authored team configuration, which is exactly the content this verb
        exists to find. A blanket dot-directory prune -- what this walk did
        before -- silently disagreed with the module on that one name.
    """
    return name in NOISE_DIR_NAMES or (name.startswith(".") and name != ".claude")


def _enumerate_tree(root: Path) -> list[Path]:
    """Every directory in `root`'s tree that could be a subject, root included.

    Three prunes, and every one of them is the discovery module's own rule rather
    than a local restatement:

      * NOISE (`_is_noise` over the imported `NOISE_DIR_NAMES`) -- build output
        and tooling state: `Intermediate`, `Saved`, `Binaries`,
        `DerivedDataCache`, `__pycache__`, `.venv`, and the rest, plus every
        dot-directory except `.claude`.
      * STRUCTURAL (`root_exclusion`, which is `_skip_reason`) -- vendored,
        generated, and content-detected vendored bundles.
      * VCS-IGNORED (`vcs_ignore.ignored_paths`) -- see below.

    Getting only two of the three is not a smaller version of the rule, it is a
    DIFFERENT rule. An Unreal tree carries `Intermediate/`, `Saved/` and
    `Binaries/` under most module and plugin directories, so the half that was
    missing turned hundreds of build-output directories into coverage subjects,
    each one costing a full agent run over compiler leavings.

    VCS-IGNORED DESCENDANTS ARE PRUNED TOO, and this is the half that is easy to
    get wrong. `build_subject` honours an explicitly named ignored root WHOLESALE
    -- the user asked for it, so it is assessed and the exclusion reported. That
    is right for `build <dir>`, where the user named exactly that directory, and
    wrong for every DESCENDANT under `--tree`, where the user named the tree and
    not the thousands of directories in it. Without this, a repo whose ignore
    rules cover Binaries/, Saved/ or DerivedDataCache/ turns every one of them
    into a coverage subject, which contradicts the lane's own rule that a
    directory the VCS is configured to ignore is NOT a subject.

    The ignore question is asked through `vcs_ignore.ignored_paths` -- the same
    helper `build_subject` uses -- one batched subprocess per level, with the VCS
    detected once for the whole tree. Asking during the walk rather than
    afterwards is what keeps an ignored subtree from being descended at all.

    Naming an ignored directory OPTS ITS WHOLE TREE IN. If the root the user
    named is itself ignored, the pruning is switched off for that walk rather
    than applied to its descendants -- otherwise `build --tree ./Binaries` would
    return `Binaries` alone and nothing under it, which is not what anyone who
    typed that meant. This is the same honour-wholesale semantics
    `build_subject` already implements for an explicitly named ignored root,
    extended to the tree the user explicitly pointed at.

    A tree is enumerated only on request (`--tree`). This verb has no implicit
    whole-repo default for the same reason the lane has none: an unbounded
    default is how the capability becomes expensive.
    """
    found: list[Path] = []
    vcs = detect_vcs(root)
    if vcs and ignored_paths([root], root=root, vcs=vcs):
        vcs = None
    for current, dirnames, _files in os.walk(root):
        here = Path(current)
        kept = sorted(
            d for d in dirnames
            if not _is_noise(d) and root_exclusion(here / d) is None
        )
        if kept and vcs:
            ignored = ignored_paths(
                [here / d for d in kept], root=root, vcs=vcs
            )
            kept = [d for d in kept if (here / d) not in ignored]
        dirnames[:] = kept
        found.append(here)
    return found


def _read_overrides(path: Path) -> list[tuple[int, str]]:
    """(line number, directory) for each override entry.

    THE SHAPE A REAL CORPUS HAS is "these trees, PLUS these specific
    directories, NOT recursively". `--tree` is a whole-invocation flag, so
    without this the caller has to choose between two wrong runs: leave the
    exceptions out, or name them alongside the roots and have `--tree` walk them
    too. The second is the dangerous one -- reinstating 9 first-party
    directories parked under vendored parents dragged in 116 vendored
    descendants, and the corpus got BIGGER, which reads as more coverage.

    An override entry is a CLAIM AGAINST A PRUNE. The plugin cannot know that
    `ThirdParty/SFDate` is first-party build glue; that is the consuming
    project's local knowledge, and this file is how it arrives as input rather
    than as a patch to the prune rules.

    Blank lines are ignored and `#` starts a comment, so the evidence for a
    claim can live beside it. Paths are resolved against the working directory,
    the same as the positional root arguments.
    """
    entries: list[tuple[int, str]] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if line:
            entries.append((number, line))
    return entries


def _is_worth_assessing(subject: dict) -> bool:
    """A directory with no direct code and nothing unrecognized is not a subject.

    It is not an error -- a directory holding only subdirectories is a normal and
    correct empty result -- but it has no candidates to find, so dispatching an
    agent at it spends a batch slot on a guaranteed empty answer. A directory
    with zero code files and a NON-empty unknownExtensions is KEPT: that is the
    discovery-failure case, and dropping it here would hide exactly the
    never-read directory the lane refuses to call clean.
    """
    return bool(subject["codeFiles"]) or bool(subject["unknownExtensions"])


def _write_atomic(out_path: Path, lines: list[str]) -> None:
    """Write the file so a partial or wrapped one can never be observed.

    LF endings and ASCII regardless of platform: the agents slice this file by
    LINE NUMBER, so a CRLF-translating text write on Windows is not a cosmetic
    difference. Written to a temp file in the destination directory, flushed and
    fsynced, then os.replace'd into place -- so the destination name either does
    not exist or names a complete file, never a half-written one.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="ascii", newline="\n", delete=False,
        dir=str(out_path.parent), prefix=out_path.name + ".", suffix=".tmp",
    )
    try:
        with handle as stream:
            for line in lines:
                stream.write(line)
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(handle.name, out_path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cmd_build(args: argparse.Namespace) -> int:
    roots: list[Path] = []
    for name in args.directory:
        target = Path(name).resolve()
        if not target.is_dir():
            print(f"error: not a directory: {target}", file=sys.stderr)
            return 2
        if args.tree:
            roots.extend(_enumerate_tree(target))
        else:
            roots.append(target)

    seen: set[str] = set()
    ordered: list[Path] = []
    for root in roots:
        key = canonical_path(str(root), windows_shaped(str(root)))
        if key in seen:
            continue
        seen.add(key)
        ordered.append(root)

    subjects = []
    dropped = 0
    for root in ordered:
        subject = build_subject(root)
        if args.tree and not _is_worth_assessing(subject):
            dropped += 1
            continue
        subjects.append(subject)

    from_roots = len(subjects)
    override_added = 0
    override_redundant = 0

    if args.overrides:
        overrides_path = Path(args.overrides).resolve()
        if not overrides_path.is_file():
            print(f"error: no such overrides file: {overrides_path}", file=sys.stderr)
            return 2
        subject_keys = {
            canonical_path(str(s["root"]), windows_shaped(str(s["root"])))
            for s in subjects
        }
        problems: list[str] = []
        # Every entry is checked before anything is written. A rotted override
        # file must fail LOUDLY and completely rather than quietly shrinking the
        # corpus by the entries that no longer resolve, and reporting only the
        # first would make fixing a stale file an iterative guessing game.
        for number, raw in _read_overrides(overrides_path):
            target = Path(raw).resolve()
            if not target.is_dir():
                problems.append(
                    f"{overrides_path.name}:{number}: not a directory: {target}"
                )
                continue
            subject = build_subject(target)
            if not _is_worth_assessing(subject):
                problems.append(
                    f"{overrides_path.name}:{number}: nothing to assess in {target} "
                    "-- no direct code files and no unrecognized extensions"
                )
                continue
            key = canonical_path(str(subject["root"]), windows_shaped(str(subject["root"])))
            if key in subject_keys:
                # Already a subject from the walk. Counted rather than silently
                # dropped, so a stale override file is VISIBLE instead of inert.
                override_redundant += 1
                continue
            subject_keys.add(key)
            subjects.append(subject)
            override_added += 1
        if problems:
            print(
                f"error: {len(problems)} unusable override entry/ies. An override "
                "file that has rotted must be fixed, not silently honoured in "
                "part:", file=sys.stderr,
            )
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            return 2

    if not subjects:
        print(
            "error: no subjects. Nothing under the named directory/ies has "
            "direct code files or unrecognized extensions.",
            file=sys.stderr,
        )
        return 2

    # ATOMICITY OF THE COUNT. `subject_count` is the len() of the very list that
    # is serialized below -- never an independent observation of the file. The
    # file is then re-read, its lines re-counted and re-parsed, and the whole
    # publish is abandoned on any disagreement, so a subjects file and a count
    # that contradict each other cannot both survive this function.
    lines = [json.dumps(s, ensure_ascii=True, sort_keys=True) for s in subjects]
    for index, line in enumerate(lines):
        if "\n" in line or "\r" in line:
            print(
                f"error: record {index + 1} serialized with an embedded newline; "
                "refusing to write a file whose line numbers would not address "
                "its records.",
                file=sys.stderr,
            )
            return 1

    out_path = Path(args.out).resolve()
    _write_atomic(out_path, lines)

    subject_count = len(subjects)
    readback = out_path.read_text(encoding="ascii").split("\n")
    if readback and readback[-1] == "":
        readback.pop()
    if len(readback) != subject_count:
        out_path.unlink(missing_ok=True)
        print(
            f"error: serialized {subject_count} record(s) but the file reads back "
            f"as {len(readback)} line(s); refusing to publish it.",
            file=sys.stderr,
        )
        return 1
    for index, line in enumerate(readback):
        if not line.strip():
            out_path.unlink(missing_ok=True)
            print(f"error: line {index + 1} is blank.", file=sys.stderr)
            return 1
        try:
            record = json.loads(line)
        except ValueError as exc:
            out_path.unlink(missing_ok=True)
            print(f"error: line {index + 1} does not parse: {exc}", file=sys.stderr)
            return 1
        if not record.get("root"):
            out_path.unlink(missing_ok=True)
            print(f"error: line {index + 1} has no root.", file=sys.stderr)
            return 1

    meta_path = Path(str(out_path) + META_SUFFIX)
    _write_atomic(meta_path, [json.dumps({
        "subjectCount": subject_count,
        "sha256": _sha256(out_path),
        "subjectsFile": str(out_path),
    }, ensure_ascii=True, sort_keys=True)])

    if dropped:
        print(
            f"note: {dropped} directory/ies had no direct code and no unrecognized "
            "extensions, so they were not made subjects (each child directory is "
            "its own subject).",
            file=sys.stderr,
        )
    # Three counts, not one total. The override population is the caller's own
    # claim against the plugin's prunes, so it has to be legible as its own
    # number -- folded into a total, a corpus that grew because an override file
    # over-reached is indistinguishable from one that grew because the tree did.
    if args.overrides:
        print(
            f"subjects: {from_roots} from the named roots, {override_added} added "
            f"by override ({override_redundant} redundant) = {subject_count}",
            file=sys.stderr,
        )
    print(f"wrote {subject_count} subject(s) to {out_path}", file=sys.stderr)
    print("", file=sys.stderr)
    print("Workflow args (these two must travel together):", file=sys.stderr)
    print(json.dumps({
        "subjectsFile": str(out_path),
        "subjectCount": subject_count,
    }, ensure_ascii=True, indent=2))
    print("", file=sys.stderr)
    print(
        f"After the run, verify it:\n"
        f"  coverage_subjects.py verify <report.json> {out_path}",
        file=sys.stderr,
    )
    return 0


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def _records_here(payload) -> list[dict] | None:
    """The per-subject list carried directly by `payload`, or None."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("perSubject", "subjects"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return None


def _load_records(path: Path) -> list[dict]:
    """The per-subject records, from whichever shape the caller actually holds.

    THE WRAPPER IS THE COMMON CASE, not an edge case. The Workflow tool does not
    hand back the lane's return object -- it wraps it, alongside `summary`,
    `logs`, `totalTokens` and friends, with the lane's object under `result`. So
    the shape a caller has in a file is almost never the shape the lane returned,
    and refusing it meant the person holding the artifact had to write an
    extraction step before the mandatory check would look at their report. A gate
    that needs glue before it will run is a gate that gets skipped -- which is
    the same failure this verb exists to prevent, one level up.

    Accepted, in order: the lane's own object; a bare list of records; the
    Workflow wrapper (unwrapped through `result`); and any single nested object
    carrying a per-subject list. Unwrapping only LOCATES the records -- every
    check downstream runs on them unchanged, so no shape is a path that skips
    checks.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))

    direct = _records_here(payload)
    if direct is not None:
        return direct

    if isinstance(payload, dict):
        # The Workflow wrapper, named explicitly so the common case is not left
        # to the general search below.
        nested = _records_here(payload.get("result"))
        if nested is not None:
            return nested

        # Any single nested object carrying one. More than one is AMBIGUOUS and
        # is refused rather than guessed at: picking one would silently verify a
        # report the caller did not mean.
        found = [
            (key, records) for key, value in payload.items()
            if isinstance(value, dict)
            for records in [_records_here(value)] if records is not None
        ]
        if len(found) == 1:
            return found[0][1]
        if len(found) > 1:
            raise ValueError(
                "several nested objects carry a perSubject list ("
                + ", ".join(sorted(key for key, _ in found))
                + ") -- pass the one you mean rather than the object holding both"
            )

    raise ValueError(
        "no perSubject found (looked at the top level and under `result`). A "
        "verifiable report is one of: the object the analyze lane returned "
        "({perSubject: [...], totals: {...}}); the Workflow tool's wrapper "
        "around it ({summary, logs, result: {perSubject: [...]}, ...}), which is "
        "what a saved run normally looks like; or a bare JSON list of the "
        "per-subject records"
    )


def _load_subjects(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8")
    lines = raw.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    subjects = []
    for index, line in enumerate(lines):
        if not line.strip():
            raise ValueError(
                f"{path}:{index + 1} is blank. A slice is a line range, so a "
                "blank line shifts every later slice onto the wrong directory. "
                "Rebuild the file with the build verb."
            )
        subjects.append(json.loads(line))
    return subjects


def cmd_verify(args: argparse.Namespace) -> int:
    report_path = Path(args.report).resolve()
    subjects_path = Path(args.subjects).resolve()
    try:
        records = _load_records(report_path)
        subjects = _load_subjects(subjects_path)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    failures: list[str] = []
    count = len(subjects)

    # CHECK 0 -- the file is the one that was built. Only when the sidecar the
    # build verb writes is present; a hand-made file simply skips it.
    meta_path = Path(str(subjects_path) + META_SUFFIX)
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError:
            meta = None
        if isinstance(meta, dict):
            if meta.get("subjectCount") != count:
                failures.append(
                    f"[file] {meta_path.name} records subjectCount "
                    f"{meta.get('subjectCount')} but the file holds {count} "
                    "line(s) -- it was edited after it was built"
                )
            if meta.get("sha256") and meta["sha256"] != _sha256(subjects_path):
                failures.append(
                    f"[file] {subjects_path.name} does not match the sha256 "
                    "recorded at build time -- it was edited after it was built"
                )

    # CHECK 1 -- every requested key accounted for, exactly once.
    by_key: dict[str, dict] = {}
    for index, record in enumerate(records):
        key = record.get("subjectKey")
        if not isinstance(key, str) or not key:
            failures.append(f"[key] record at position {index} carries no subjectKey")
            continue
        if not re.fullmatch(r"L\d+", key):
            failures.append(
                f"[key] {key}: not a subjectsFile key (expected L<line>). A report "
                "produced from INLINE subjects needs no verification -- its "
                "identity was never agent-supplied -- so this is the wrong report "
                "or the wrong subjects file"
            )
            continue
        line_no = int(key[1:])
        if not 1 <= line_no <= count:
            failures.append(
                f"[key] {key}: names line {line_no}, outside the file's 1..{count}"
            )
            continue
        if key in by_key:
            failures.append(f"[key] {key}: returned more than once")
            continue
        by_key[key] = record

    for line_no in range(1, count + 1):
        key = f"L{line_no}"
        if key not in by_key:
            failures.append(
                f"[key] {key} ({subjects[line_no - 1].get('root')}): requested but "
                "absent from the report -- neither assessed nor accounted for"
            )

    # CHECKS 2 and 3 -- roots and anchors re-checked against the subjects file.
    for key, record in sorted(by_key.items(), key=lambda kv: int(kv[0][1:])):
        line_no = int(key[1:])
        truth = subjects[line_no - 1]
        true_root = str(truth.get("root", ""))
        code_files = [str(f) for f in truth.get("codeFiles", [])]
        status = record.get("status")
        reported_root = str(record.get("root", ""))

        if status not in ("ASSESSED", "NOT-ASSESSED"):
            failures.append(
                f"[status] {key} ({true_root}): status is {status!r}, expected "
                "ASSESSED or NOT-ASSESSED"
            )

        if status == "NOT-ASSESSED":
            # The lane stamps an unreturned key with a synthetic placeholder root
            # because it has no record to read a real one from. Either that or
            # the true root is honest; anything else is a fabrication.
            placeholder = reported_root.endswith("#" + key)
            if not placeholder and not same_path(reported_root, true_root):
                failures.append(
                    f'[root] {key}: NOT-ASSESSED record names "{reported_root}", '
                    f'which is neither "{true_root}" nor the lane placeholder'
                )
            if record.get("candidates"):
                failures.append(
                    f"[status] {key} ({true_root}): NOT-ASSESSED but carries "
                    f"{len(record['candidates'])} candidate(s)"
                )
            continue

        if not same_path(reported_root, true_root):
            failures.append(
                f'[root] {key}: report says "{reported_root}", subjects file line '
                f'{line_no} says "{true_root}" -- the assessment is filed under a '
                "directory it does not describe"
            )

        for position, candidate in enumerate(record.get("candidates") or []):
            label = f"{key} ({true_root}) candidate {position}"
            destination = str(candidate.get("destination", ""))
            if not same_path(destination, true_root):
                failures.append(
                    f'[destination] {label}: destination "{destination}" is not '
                    "the assessed directory; generation groups by this field"
                )
            anchors = candidate.get("anchors") or []
            if not anchors:
                failures.append(f"[anchor] {label}: no anchors (CV-7 evidence floor)")
            for anchor in anchors:
                reason = anchor_rejection_reason(str(anchor), code_files)
                if reason:
                    failures.append(f'[anchor] {label}: "{anchor}" -- {reason}')

    assessed = sum(1 for r in by_key.values() if r.get("status") == "ASSESSED")
    if failures:
        print(
            f"FAIL: {len(failures)} problem(s) verifying {report_path.name} "
            f"against {subjects_path.name}"
        )
        for failure in failures:
            print(f"  {failure}")
        print("")
        print(
            "This report is NOT verified. Its records are agent-attested: the "
            "analyze lane has no filesystem and could not check any of the above. "
            "Do not promote candidates from it until these are resolved."
        )
        return 1

    print(
        f"OK: {count} requested subject(s), {assessed} ASSESSED, "
        f"{count - assessed} NOT-ASSESSED. Every key accounted for; every root "
        f"and every candidate anchor re-checked against {subjects_path.name}."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="coverage_subjects.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="verb")

    build = sub.add_parser(
        "build", help="emit the JSONL subjects file and its exact subjectCount")
    build.add_argument(
        "directory", nargs="+", help="directory/ies to make subjects of")
    build.add_argument("--out", required=True, help="path to write the .jsonl to")
    build.add_argument(
        "--tree", action="store_true",
        help=(
            "make every directory under each named directory its own subject "
            "(structurally excluded and dot-directories pruned). Without it, "
            "each named directory is exactly one subject -- the lane's own unit."
        ),
    )
    build.add_argument(
        "--overrides", default=None, metavar="FILE",
        help=(
            "a file of directories to add as SINGLE subjects, one per line, "
            "non-recursively and regardless of any prune that would otherwise "
            "exclude them. Blank lines ignored, # starts a comment. This is "
            "where a consuming project's local knowledge goes -- an entry is a "
            "claim against a prune, so record its evidence in a comment."
        ),
    )
    build.set_defaults(func=cmd_build)

    verify = sub.add_parser(
        "verify", help="re-check an agent-attested report against the subjects file")
    verify.add_argument("report", help="the JSON the analyze lane returned")
    verify.add_argument("subjects", help="the .jsonl the run was given")
    verify.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    if not getattr(args, "verb", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
