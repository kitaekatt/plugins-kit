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

import json
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

    Returns the shared bundle fields:
        {"bundle_dir", "diff_chunks", "changed_files",
         "unique_claude_mds", "submit_gates"}

    Each changed_files entry is the input dict minus "identifier", plus
    "chunk_index" (int or None when absent from the diff) and
    "claude_mds" (nearest-ancestor-first absolute paths).
    """
    bundle_dir.mkdir(parents=True, exist_ok=True)
    chunks = partition_sections_into_chunks(
        sections, max_chunk_bytes, preamble=preamble
    )
    diff_chunks = write_chunks(chunks, bundle_dir)

    # identifier -> chunk index, for tagging changed_files entries.
    id_to_chunk: dict[str, int] = {}
    for entry in diff_chunks:
        for ident in entry["files"]:
            id_to_chunk[ident] = entry["index"]

    changed_files: list[dict] = []
    unique: list[str] = []
    seen: set[str] = set()
    for f in files:
        local = f.get("local")
        claude_mds: list[str] = []
        if local:
            claude_mds = collect_claude_mds(Path(local), workspace_root)
            for cm in claude_mds:
                if cm not in seen:
                    unique.append(cm)
                    seen.add(cm)
        out = {k: v for k, v in f.items() if k != "identifier"}
        out["chunk_index"] = id_to_chunk.get(f["identifier"])
        out["claude_mds"] = claude_mds
        changed_files.append(out)

    submit_gates = collect_submit_gates(
        unique,
        [cf["local"] for cf in changed_files if cf.get("local")],
        workspace_root,
    )

    return {
        "bundle_dir": str(bundle_dir),
        "diff_chunks": diff_chunks,
        "changed_files": changed_files,
        "unique_claude_mds": unique,
        "submit_gates": submit_gates,
    }


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
