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


def matches_claim(identifier: str, claim_globs: list[str]) -> bool:
    """True if `identifier` matches any pattern in `claim_globs`.

    `identifier` is the kit's chunk-map key (git repo-relative path, p4 depot
    path). A `**/NAME` pattern matches NAME at ANY depth, including the root
    (fnmatch's `*` alone would not match a bare-root `NAME` against `*/NAME`),
    so it is special-cased to a basename compare. Any other pattern is an
    ordinary fnmatch against the whole (posix-normalized) identifier.
    """
    if not claim_globs:
        return False
    norm = identifier.replace("\\", "/")
    base = norm.rsplit("/", 1)[-1]
    for g in claim_globs:
        gnorm = g.replace("\\", "/")
        if gnorm.startswith("**/") and fnmatch.fnmatch(base, gnorm[3:]):
            return True
        if fnmatch.fnmatch(norm, gnorm):
            return True
    return False


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
                    "**/SKILL.md"]). When non-empty, files whose identifier
                    matches are EXCLUDED from the diff chunks and the
                    `changed_files` list (a subject-lens reviewer owns them),
                    and instead emitted under a new `claimed_files` key -- but
                    they STILL contribute to `unique_claude_mds` and the
                    submit-gate scan. When None/empty the returned dict is
                    byte-identical to the pre-claim contract (no `claimed_files`
                    key), so existing callers are unaffected.

    Returns the shared bundle fields:
        {"bundle_dir", "diff_chunks", "changed_files",
         "unique_claude_mds", "submit_gates"}
    plus "claimed_files" when claim_globs is non-empty.

    Each changed_files entry is the input dict minus "identifier", plus
    "chunk_index" (int or None when absent from the diff) and
    "claude_mds" (nearest-ancestor-first absolute paths). Each claimed_files
    entry is the input dict verbatim (identifier retained), carrying whatever
    the front-half attached (local, status/action, pre_image).
    """
    claim_globs = claim_globs or []
    bundle_dir.mkdir(parents=True, exist_ok=True)

    claimed_idents = {
        f["identifier"] for f in files if matches_claim(f["identifier"], claim_globs)
    }
    # Claimed files' diff sections must not reach the generic reviewers, so
    # drop them before chunking. Their records still flow through the CLAUDE.md
    # / submit-gate walk below.
    review_sections = [s for s in sections if s["identifier"] not in claimed_idents]

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
            claimed_files.append(entry)
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
