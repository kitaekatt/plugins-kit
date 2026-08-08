"""VCS-neutral back-half of the code-review preparation pipeline.

Both git-kit and p4-kit prepare-review scripts share the same shape:

    VCS front-half (kit-specific)          Shared back-half (this module)
    -----------------------------          ------------------------------
    fetch diff text                   ->   split_sections(diff_text, parse_header)
    run `git`/`p4` subprocesses       ->   run_vcs(executable, args)
    resolve per-file local paths      ->   assemble_bundle(...): chunk + write +
                                           CLAUDE.md walk + submit-gate scan
    add VCS-specific bundle fields    ->   emit_bundle(bundle, bundle_dir)

Each kit keeps a pure VCS front-half (range/CL resolution, diff fetching,
header parsing, hygiene checks) and delegates the format-neutral mechanics
here. Chunking policy lives in chunking.py; CLAUDE.md collection and
submit-gate parsing live in claude_mds.py -- this module composes them.
"""

import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from bootstrap_lib.code_review.chunking import (
    DiffSection,
    partition_sections_into_chunks,
    write_chunks,
)
from bootstrap_lib.code_review.claude_mds import (
    collect_claude_mds,
    collect_submit_gates,
)
from bootstrap_lib.code_review.generated import (
    detect_generated,
    local_size,
)
from bootstrap_lib.code_review.triviality import (
    mechanical_checks,
    triviality_profile,
)


# ---------------------------------------------------------------------------
# Claim matching (subject-lens review contributor support).
# ---------------------------------------------------------------------------
#
# A "claimed" changed file is one a subject-lens reviewer (skills-kit md-audit)
# owns: it is pulled OUT of the generic reviewer fan-out (its diff excluded from
# the chunks, its record moved to `claimed_files`) but REMAINS in the CLAUDE.md
# ruleset collection and submit-gate machinery. Matching is by claim glob, and
# lives here (shared) so a kit front-half and the back-half agree on exactly
# which files are claimed. Front-halves use it to decide which pre-images to
# materialize; assemble_bundle uses it to do the exclusion + routing.


def _matches_one_glob(norm: str, base: str, gnorm: str) -> bool:
    """Match one posix-normalized pattern against a normalized identifier.

    A `**/` prefix means "at ANY depth, including the root". For a single-segment
    tail (`**/CLAUDE.md`) that is a basename compare -- fnmatch's `*` alone would
    not match a bare-root `CLAUDE.md` against `*/CLAUDE.md`. For a multi-segment
    tail (`**/skills/*/references/*.md`) a basename compare is meaningless, so the
    tail is also tried ROOTED, which is what makes `**/` mean "including the root"
    for those too. Any pattern without the prefix is an ordinary fnmatch against
    the whole identifier.
    """
    if gnorm.startswith("**/"):
        tail = gnorm[3:]
        if "/" in tail:
            if fnmatch.fnmatch(norm, tail):
                return True
        elif fnmatch.fnmatch(base, tail):
            return True
    return fnmatch.fnmatch(norm, gnorm)


def matches_claim(identifier: str, claim_globs: list[str]) -> bool:
    """True if `identifier` is claimed by `claim_globs`.

    `identifier` is the kit's chunk-map key (git repo-relative path, p4 depot
    path). A pattern prefixed with `!` is an EXCLUSION; exclusions are evaluated
    FIRST and are absolute, so a caller can claim a broad shape while carving out
    a subset -- e.g. `["**/*.md", "!**/skills/*/references/*.md"]` claims every
    markdown file EXCEPT a skill's reference docs.

    The carve-out is not cosmetic. A claimed file is pulled out of the generic
    reviewer fan-out on the promise that a specialist reviews it instead; when no
    specialist actually reads that shape of file, claiming it removes the only
    review it had. Without negation the caller's only options are claim-everything
    (which strands those files) or drop the catch-all (which strands the files the
    specialist genuinely owns) -- neither expresses the real intent.

    A list of only exclusions claims nothing, which is the honest reading: no
    positive pattern was offered.
    """
    if not claim_globs:
        return False
    norm = identifier.replace("\\", "/")
    base = norm.rsplit("/", 1)[-1]

    positives: list[str] = []
    for g in claim_globs:
        gnorm = g.replace("\\", "/")
        if gnorm.startswith("!"):
            # An exclusion wins outright -- no positive pattern can re-claim the
            # file. Order-independent by design: a caller listing patterns in a
            # config should not have to reason about precedence.
            if _matches_one_glob(norm, base, gnorm[1:]):
                return False
        else:
            positives.append(gnorm)

    return any(_matches_one_glob(norm, base, g) for g in positives)


def canonical_local(local: Optional[str]) -> Optional[str]:
    """Canonicalize an emitted local path so it agrees with the CLAUDE.md chain.

    The CLAUDE.md ancestor walk (`collect_claude_mds`) resolve()s every path, so
    on a case-insensitive filesystem (Windows) its entries carry the canonical
    drive/component casing. A front-half's raw `local` may NOT: p4's
    `p4 where` emits a lowercase drive letter (`d:/dev/...`) while the resolved
    chain carries `D:/dev/...`, so a consumer comparing a claimed file's `local`
    against its own `claude_mds` self-entry (md-audit's
    "ancestorClaudeMdPaths = claude_mds minus the subject's own local") fails to
    remove the subject and treats every CLAUDE.md as its own parent.

    Routing the emitted `local` through the SAME resolve() the chain uses makes
    the two agree byte-for-byte. Idempotent for an already-resolved path (git
    resolves its locals at enumeration, so this is a no-op there). Falsy input
    (a file with no workspace mapping) passes through unchanged.
    """
    if not local:
        return local
    try:
        return str(Path(local).resolve())
    except OSError:
        return local


def preimage_relpath(identifier: str) -> str:
    """Deterministic bundle-relative path for a claimed file's pre-image.

    Basenames collide across directories (`a/CLAUDE.md`, `b/CLAUDE.md`), so a
    short content-independent hash of the full identifier disambiguates. The
    kit front-half writes the materialized pre-image to
    `<bundle_dir>/<preimage_relpath(identifier)>`.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", identifier).strip("-")
    digest = hashlib.sha1(identifier.encode("utf-8")).hexdigest()[:8]
    return f"pre-images/{safe}-{digest}" if safe else f"pre-images/{digest}"


def annotate_triviality(entry: dict, section_text: str) -> None:
    """Attach the pure-mechanical triviality profile to a claimed-file entry.

    Reads the entry's materialized `pre_image` (None for an add) and computes
    `trivial` + `trivial_reasons` from the file's diff hunks; for a trivial file
    it ALSO computes `trivial_checks` (an ASCII scan and an absolute-path scan
    over the changed lines) the skip section reports honestly. Mutates `entry`
    in place. Fails closed -- an unreadable pre-image or unparseable diff yields
    `trivial=False`, so the full review is always the fallback. VCS-neutral; see
    bootstrap_lib.code_review.triviality.
    """
    pre_path = entry.get("pre_image")
    pre_text: Optional[str] = None
    if pre_path:
        try:
            pre_text = Path(pre_path).read_text(encoding="utf-8")
        except OSError:
            pre_text = None
    profile = triviality_profile(section_text, pre_text)
    entry["trivial"] = profile["trivial"]
    entry["trivial_reasons"] = profile["reasons"]
    if profile["trivial"]:
        entry["trivial_checks"] = mechanical_checks(section_text)


def run_vcs(
    executable: str, args: list[str], cwd: Optional[Path] = None
) -> tuple[int, str, str]:
    """Run a VCS command, return (returncode, stdout, stderr).

    Forces UTF-8 decoding with errors='replace': non-Latin-1 file content
    (CJK, emoji) in diffs would abort the subprocess reader on Windows,
    whose default text decoder is the system ANSI codepage (cp1252 on
    en-US/en-GB). None stdout/stderr coalesce to ''.
    """
    proc = subprocess.run(
        [executable, *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd) if cwd else None,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def split_sections(
    diff_text: str,
    parse_header: Callable[[str], Optional[dict]],
) -> tuple[str, list[dict]]:
    """Split diff text into (preamble, sections) at file-header lines.

    `parse_header(line)` receives each line stripped of its trailing
    newline. It returns None for non-header lines, or a dict of
    kit-specific identifying fields (e.g. {"path": ...} for git,
    {"depot": ..., "rev": ..., "type": ...} for p4) for a header line.

    Each returned section is the parse_header dict plus:
    - "header": the raw header line (newline included)
    - "body":   every subsequent line up to the next header

    Lines before the first header accumulate into `preamble`.

    Note: the header matcher is a callable rather than a regex because
    git's header parsing is quote-aware (spaced and C-quoted paths) and
    cannot be expressed as a single pattern.
    """
    preamble_lines: list[str] = []
    sections: list[dict] = []
    current: Optional[dict] = None
    for line in diff_text.splitlines(keepends=True):
        fields = parse_header(line.rstrip("\n"))
        if fields is not None:
            if current is not None:
                sections.append(current)
            current = dict(fields)
            current["header"] = line
            current["body"] = ""
        elif current is None:
            preamble_lines.append(line)
        else:
            current["body"] += line
    if current is not None:
        sections.append(current)
    return "".join(preamble_lines), sections


def assemble_bundle(
    preamble: str,
    sections: list[DiffSection],
    files: list[dict],
    bundle_dir: Path,
    max_chunk_bytes: int,
    workspace_root: Optional[Path],
    claim_globs: Optional[list[str]] = None,
    review_generated: bool = False,
) -> dict:
    """Chunk the diff to disk and build the VCS-neutral bundle core.

    Args:
        preamble:   unattributed diff text before the first file section.
        sections:   vendor-neutral [{identifier, text}] diff sections
                    (the kit's `_*_diff_to_sections` adapter output).
        files:      one dict per changed file. Required keys:
                    - "identifier": the chunk-map key -- must equal the
                      matching section's identifier (git path, depot path).
                    - "local": absolute local path as str, or None/'' when
                      the file has no workspace mapping (skips the
                      CLAUDE.md walk for that file).
                    Every OTHER key (e.g. "path", "status", "depot") is
                    passed through verbatim to the output entry.
        bundle_dir: bundle directory (created if missing).
        max_chunk_bytes: per-chunk byte cap for the partitioner.
        workspace_root: stops the CLAUDE.md ancestor walk and anchors
                    submit-gate scope matching; None means filesystem root.
        claim_globs: OPTIONAL claim patterns (e.g. ["**/CLAUDE.md",
                    "**/SKILL.md"]); a `!`-prefixed pattern EXCLUDES, and wins
                    over every positive (see matches_claim). When non-empty,
                    files whose identifier matches are EXCLUDED from the diff
                    chunks and the
                    `changed_files` list (a subject-lens reviewer owns them),
                    and instead emitted under a new `claimed_files` key -- but
                    they STILL contribute to `unique_claude_mds` and the
                    submit-gate scan. When None/empty the returned dict is
                    byte-identical to the pre-claim contract (no `claimed_files`
                    key), so existing callers are unaffected.
        review_generated: OPTIONAL override. False (default) EXCLUDES every
                    machine-generated file from the diff chunks and the
                    `changed_files` list and surfaces it under
                    `generated_files` instead; True disables detection
                    entirely and reviews those files like any other. The
                    override exists for the author or user who explicitly asks
                    for the full review.

    Returns the shared bundle fields:
        {"bundle_dir", "diff_chunks", "changed_files",
         "unique_claude_mds", "submit_gates"}
    plus "claimed_files" when claim_globs is non-empty, plus "generated_files"
    when any changed file was detected as machine-generated. With neither
    present the dict is byte-identical to the pre-claim contract.

    Generated-file detection is CONTENT-first (a banner in the file's leading
    added lines, or in its leading lines on disk -- see
    bootstrap_lib.code_review.generated), never size-alone: a large
    hand-written file is still chunked and fully reviewed. A generated file
    contributes NO chunks and no reviewer lanes, because the review target is
    its GENERATOR, which is reviewed separately as ordinary source. It is never
    a pass: the skill renders it as an honest "not reviewed". Claimed files are
    evaluated first and are never re-routed here -- a subject-lens reviewer
    already owns them.

    Each generated_files entry is the input dict verbatim (identifier
    retained), plus "generated_signature" (the matched signature label) and
    "size_bytes" (the on-disk size, falling back to the diff section's byte
    length when the file has no readable local path).

    Each changed_files entry is the input dict minus "identifier", plus
    "chunk_index" (int or None when absent from the diff) and
    "claude_mds" (nearest-ancestor-first absolute paths). Each claimed_files
    entry is the input dict verbatim (identifier retained), carrying whatever
    the front-half attached (local, status/action, pre_image), PLUS the
    pure-mechanical triviality profile: "trivial" (bool), "trivial_reasons"
    (machine-readable disqualifier codes, [] when trivial) and -- only when
    trivial -- "trivial_checks" ({"ascii_clean", "no_abs_paths"} over the
    changed lines). See bootstrap_lib.code_review.triviality.
    """
    claim_globs = claim_globs or []
    bundle_dir.mkdir(parents=True, exist_ok=True)

    claimed_idents = {
        f["identifier"] for f in files if matches_claim(f["identifier"], claim_globs)
    }
    # Per-identifier diff section text, used to compute each claimed file's
    # triviality profile (the section is excluded from the reviewer chunks, but
    # its hunks are still what the pure-mechanical guard inspects).
    id_to_text = {s["identifier"]: s["text"] for s in sections}

    # Machine-generated artifacts: detected from content, excluded from
    # chunking, surfaced separately. Claimed files are skipped -- a subject-lens
    # reviewer already owns them, and re-routing would take away the review they
    # were claimed FOR.
    generated_sigs: dict[str, str] = {}
    if not review_generated:
        for f in files:
            ident = f["identifier"]
            if ident in claimed_idents:
                continue
            label = detect_generated(id_to_text.get(ident, ""), f.get("local"))
            if label:
                generated_sigs[ident] = label

    # Claimed and generated files' diff sections must not reach the generic
    # reviewers, so drop them before chunking. Their records still flow through
    # the CLAUDE.md / submit-gate walk below.
    review_sections = [
        s
        for s in sections
        if s["identifier"] not in claimed_idents
        and s["identifier"] not in generated_sigs
    ]

    chunks = partition_sections_into_chunks(
        review_sections, max_chunk_bytes, preamble=preamble
    )
    diff_chunks = write_chunks(chunks, bundle_dir)

    # identifier -> chunk index, for tagging changed_files entries.
    id_to_chunk: dict[str, int] = {}
    for entry in diff_chunks:
        for ident in entry["files"]:
            id_to_chunk[ident] = entry["index"]

    changed_files: list[dict] = []
    claimed_files: list[dict] = []
    generated_files: list[dict] = []
    unique: list[str] = []
    seen: set[str] = set()
    all_locals: list[str] = []
    for f in files:
        local = f.get("local")
        claude_mds: list[str] = []
        if local:
            all_locals.append(local)
            # Claimed files REMAIN in the ruleset collection: walk their
            # ancestors too, so a claimed CLAUDE.md's scope still surfaces.
            claude_mds = collect_claude_mds(Path(local), workspace_root)
            for cm in claude_mds:
                if cm not in seen:
                    unique.append(cm)
                    seen.add(cm)
        if f["identifier"] in claimed_idents:
            # Pass the entry through verbatim (identifier retained) so the
            # skill can resolve pre_image / status back to the subject file.
            # `claude_mds` is the nearest-ancestor-first CLAUDE.md chain and,
            # for a CLAUDE.md subject, INCLUDES the subject itself as its first
            # entry -- the skill drops the subject's own local to derive
            # md-audit's ancestorClaudeMdPaths / parentPath. Canonicalize the
            # emitted local so that self-removal compares equal (see
            # canonical_local) regardless of the front-half's path casing.
            entry = dict(f)
            if local:
                entry["local"] = canonical_local(local)
            entry["claude_mds"] = claude_mds
            # Pure-mechanical triviality profile (+ mechanical checks when
            # trivial), so the skill can skip the audit lane for a typo-sized
            # change and report an honest what-was-checked line instead.
            annotate_triviality(entry, id_to_text.get(f["identifier"], ""))
            claimed_files.append(entry)
            continue
        if f["identifier"] in generated_sigs:
            # Pass the entry through verbatim (identifier retained, matching the
            # claimed_files shape) so the skill can name the exact file it did
            # NOT review. Size is reported because "how much review was skipped"
            # is the question a reader asks next; it is never why the file was
            # skipped.
            entry = dict(f)
            if local:
                entry["local"] = canonical_local(local)
            entry["generated_signature"] = generated_sigs[f["identifier"]]
            size = local_size(local)
            if size is None:
                size = len(id_to_text.get(f["identifier"], "").encode("utf-8"))
            entry["size_bytes"] = size
            generated_files.append(entry)
            continue
        out = {k: v for k, v in f.items() if k != "identifier"}
        if local:
            out["local"] = canonical_local(local)
        out["chunk_index"] = id_to_chunk.get(f["identifier"])
        out["claude_mds"] = claude_mds
        changed_files.append(out)

    submit_gates = collect_submit_gates(unique, all_locals, workspace_root)

    result = {
        "bundle_dir": str(bundle_dir),
        "diff_chunks": diff_chunks,
        "changed_files": changed_files,
        "unique_claude_mds": unique,
        "submit_gates": submit_gates,
    }
    if claim_globs:
        result["claimed_files"] = claimed_files
    if generated_files:
        result["generated_files"] = generated_files
    return result


def emit_bundle(bundle: dict, bundle_dir: Path) -> int:
    """Persist `bundle.json` beside the chunks and mirror it to stdout.

    The on-disk copy lets the skill (or a human) re-read the bundle
    without re-running prepare_review. Returns 0 (the caller's exit code).
    """
    (bundle_dir / "bundle.json").write_text(
        json.dumps(bundle, indent=2) + "\n", encoding="utf-8"
    )
    json.dump(bundle, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0
