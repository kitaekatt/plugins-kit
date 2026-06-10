"""CLAUDE.md collection and submit-gate parsing.

VCS-neutral: works for any code-review skill that resolves a list of
local file paths and a workspace root. Used by both p4-kit and git-kit
review scripts.

The submit-gate format is project-author-facing prose embedded in any
CLAUDE.md (root, subdirectory, or both):

    **Submit gate:** <imperative -- what the author must do>.
    Applies to:
    - <path prefix or glob>
    - <path prefix or glob>

    <optional rationale paragraph, rendered verbatim with the gate>

A gate fires when at least one file in the CL falls within any of its
scope paths. The review skill surfaces fired gates as a checklist the
author confirms before the review renders.
"""

import fnmatch
import os
import re
import sys
from pathlib import Path
from typing import Optional


_SUBMIT_GATE_MARKER = re.compile(r"^\*\*Submit gate:\*\*\s*(.*)$", re.IGNORECASE)
_APPLIES_TO_LINE = re.compile(r"^Applies to\b.*?:\s*$", re.IGNORECASE)
_BULLET_LINE = re.compile(r"^\s*[-*]\s+(.+?)\s*$")
_GLOB_CHARS = re.compile(r"[*?\[]")
_HEADING_LINE = re.compile(r"^#{1,6}\s")


def collect_claude_mds(file_path: Path, workspace_root: Optional[Path]) -> list[str]:
    """Walk parents of file_path collecting CLAUDE.md ancestors.

    Stops at workspace_root (inclusive) if provided, otherwise at filesystem root.
    Returns absolute paths, ordered nearest-ancestor first.
    """
    try:
        file_path = file_path.resolve()
    except OSError:
        return []
    root = workspace_root.resolve() if workspace_root else None

    found: list[str] = []
    seen: set[str] = set()
    current = file_path.parent
    while True:
        candidate = current / "CLAUDE.md"
        if candidate.is_file():
            ap = str(candidate)
            if ap not in seen:
                found.append(ap)
                seen.add(ap)
        if root is not None and current == root:
            break
        parent = current.parent
        if parent == current:
            break
        if root is not None and parent != root and root not in parent.parents:
            break
        current = parent
    return found


def parse_submit_gates(text: str, source: str) -> tuple[list[dict], list[str]]:
    """Parse `**Submit gate:**` blocks out of a CLAUDE.md.

    Returns (gates, warnings).
    - gates: parsed entries with {source, summary, scope_paths, rationale, line_no}.
      `matched_files` is added later by match_gate_scope_to_files.
    - warnings: one-line messages for malformed blocks (missing Applies-to,
      empty scope list). Callers should surface these on stderr so maintainers
      notice silently-dropped gates.

    Forgiving structure: a block ends at the next marker, the next markdown
    heading, the first blank line after the rationale has begun (the
    rationale is a single paragraph), or EOF. Continuation lines between
    the marker and 'Applies to:' fold into the summary.
    """
    gates: list[dict] = []
    warnings: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _SUBMIT_GATE_MARKER.match(lines[i])
        if not m:
            i += 1
            continue
        marker_line = i
        summary = m.group(1).strip()
        i += 1

        # Fold continuation lines into the summary until we hit Applies-to,
        # a bullet, a blank line, another marker, or a heading.
        while i < len(lines):
            line = lines[i]
            if (
                not line.strip()
                or _APPLIES_TO_LINE.match(line)
                or _BULLET_LINE.match(line)
                or _SUBMIT_GATE_MARKER.match(line)
                or _HEADING_LINE.match(line)
            ):
                break
            summary = (summary + " " + line.strip()).strip()
            i += 1
        summary = summary.rstrip(".").strip()

        # Skip blank lines and locate `Applies to:`.
        applies_to_found = False
        while i < len(lines):
            line = lines[i]
            if _APPLIES_TO_LINE.match(line):
                applies_to_found = True
                i += 1
                break
            if not line.strip():
                i += 1
                continue
            break
        if not applies_to_found:
            warnings.append(
                f"{source}:{marker_line + 1}: submit-gate "
                f"'{summary[:60]}' has no 'Applies to:' section -- skipped"
            )
            continue

        # Collect bullet lines (the scope list).
        scope_paths: list[str] = []
        while i < len(lines):
            line = lines[i]
            bm = _BULLET_LINE.match(line)
            if bm:
                scope_paths.append(bm.group(1).strip().rstrip(",;"))
                i += 1
                continue
            if not line.strip():
                break
            break
        if not scope_paths:
            warnings.append(
                f"{source}:{marker_line + 1}: submit-gate "
                f"'{summary[:60]}' has empty scope list -- skipped"
            )
            continue

        # Collect optional rationale: text after one blank line, until the
        # next block boundary (marker, heading, first blank line once the
        # rationale has begun, EOF). One paragraph, per the format spec.
        while i < len(lines) and not lines[i].strip():
            i += 1
        rationale_lines: list[str] = []
        consecutive_blanks = 0
        while i < len(lines):
            line = lines[i]
            if _SUBMIT_GATE_MARKER.match(line) or _HEADING_LINE.match(line):
                break
            if not line.strip():
                consecutive_blanks += 1
                if consecutive_blanks >= 1 and rationale_lines:
                    break
                i += 1
                continue
            consecutive_blanks = 0
            rationale_lines.append(line.rstrip())
            i += 1

        gates.append(
            {
                "source": source,
                "summary": summary,
                "scope_paths": scope_paths,
                "rationale": "\n".join(rationale_lines).strip(),
                "line_no": marker_line + 1,
            }
        )
    return gates, warnings


def _normalize_path(p: str) -> str:
    """Return forward-slash absolute path, lowercased on Windows for matching."""
    norm = p.replace("\\", "/")
    if os.name == "nt":
        norm = norm.lower()
    return norm


def match_gate_scope_to_files(
    scope_paths: list[str],
    local_files: list[str],
    workspace_root: Optional[Path],
) -> list[str]:
    """Return the subset of local_files matched by any of scope_paths.

    Scope path semantics:
    - Contains glob chars (`*`, `?`, `[`): matched via fnmatch against the
      workspace-relative path (or the absolute path if no workspace root).
    - Otherwise: prefix match. `Foo/Bar/` and `Foo/Bar` both match `Foo/Bar/x.csv`
      and `Foo/Bar` itself, but not `Foo/BarBaz/x.csv`.

    Case-insensitive on Windows, case-sensitive elsewhere.

    Returns the original (non-normalized) local file strings, in input order.
    """
    if not local_files or not scope_paths:
        return []

    ws_norm: Optional[str] = None
    if workspace_root is not None:
        try:
            ws_norm = _normalize_path(str(workspace_root.resolve()))
        except OSError:
            ws_norm = None

    normalized_scopes: list[tuple[str, bool]] = []
    for scope in scope_paths:
        if not scope:
            continue
        s = scope.replace("\\", "/").lstrip("/")
        if os.name == "nt":
            s = s.lower()
        is_glob = bool(_GLOB_CHARS.search(s))
        normalized_scopes.append((s, is_glob))

    matched: list[str] = []
    for local in local_files:
        if not local:
            continue
        target_abs = _normalize_path(local)
        rel: Optional[str] = None
        if ws_norm and target_abs.startswith(ws_norm + "/"):
            rel = target_abs[len(ws_norm) + 1:]
        target = rel if rel is not None else target_abs

        for scope, is_glob in normalized_scopes:
            if is_glob:
                if fnmatch.fnmatchcase(target, scope):
                    matched.append(local)
                    break
            else:
                scope_clean = scope.rstrip("/")
                if target == scope_clean or target.startswith(scope_clean + "/"):
                    matched.append(local)
                    break
    return matched


def collect_submit_gates(
    claude_md_paths: list[str],
    local_files: list[str],
    workspace_root: Optional[Path],
) -> list[dict]:
    """Read every CLAUDE.md in `claude_md_paths`, parse submit gates, return
    only those that match at least one file in `local_files`.

    Each returned entry includes `matched_files` (the subset of local_files
    that triggered the gate). Parse warnings for malformed blocks are emitted
    to stderr so maintainers notice silently-dropped gates.
    """
    out: list[dict] = []
    for md_path in claude_md_paths:
        try:
            text = Path(md_path).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(
                f"prepare_review: could not read {md_path} for submit-gate scan: {e}",
                file=sys.stderr,
            )
            continue
        gates, warnings = parse_submit_gates(text, md_path)
        for w in warnings:
            print(f"prepare_review: {w}", file=sys.stderr)
        for gate in gates:
            matched = match_gate_scope_to_files(
                gate["scope_paths"], local_files, workspace_root
            )
            if not matched:
                continue
            gate["matched_files"] = matched
            out.append(gate)
    return out
