"""Pure-mechanical triviality guard for the code-review pipeline (shared back-half).

A one-character typo fix in a Markdown file should not spin up a full opus
md-domain audit lane (whole-file detection). This module computes, from a claimed
file's diff hunks plus its pre-image, a per-file "triviality profile" using
ZERO inference -- only deterministic string/structure checks. A file is trivial
ONLY when EVERY one of these holds:

  1. total changed lines (added + removed) <= MAX_CHANGED_LINES;
  2. markdown structure preserved: the extracted skeleton (headings, list
     nesting, code fences, tables, blockquotes, thematic breaks) is IDENTICAL
     between the pre-image and the reconstructed post-image;
  3. no reference tokens changed: no link target, path-like token, or anchor
     differs between the removed-line text and the added-line text;
  4. no meaning-bearing keywords in the delta text (added OR removed):
     negation / modal / quantifier words (not, never, must, only, all, always,
     no, none, require(d/s), forbid(den), ban(ned)) -- case-insensitive,
     word-boundary;
  5. no YAML front-matter or embedded yaml/json/toml/config block is touched.

The profile is advisory: when it cannot parse the diff (malformed hunks, a
pre-image the hunks don't apply to) it fails CLOSED -- reports NOT trivial -- so
the fallback is always the full review, never a skipped one. The reasons list is
machine-readable so a caller can tell a disqualifier ("structure_changed") from
a simply-large change ("too_large").

`mechanical_checks` is the cheap script-side scan the skill reports for a file it
is going to SKIP: an ASCII scan and an absolute-path scan over the changed lines
only. It never gates -- it is the honest "here is what we checked mechanically"
line the skip section renders.

Home / ownership. This is VCS-neutral (it sees only diff text + pre-image text),
so it lives in the shared code_review package next to chunking / claude_mds /
ledger and is consumed by BOTH git-kit and p4-kit through prepare_review.py.
"""

from __future__ import annotations

import re
from typing import Optional

# A change touching more than this many lines is never trivial -- past a handful
# of lines a diff is large enough to warrant real review regardless of shape.
MAX_CHANGED_LINES = 5

# Meaning-bearing tokens (negation / modal / quantifier). Their mere PRESENCE in
# the delta -- added or removed -- disqualifies: a pure-mechanical guard cannot
# tell whether flipping / rewording around one of these words changed meaning, so
# it refuses to skip. Word-boundary, case-insensitive.
_KEYWORD_RE = re.compile(
    r"\b(?:not|never|must|only|all|always|no|none|"
    r"require[ds]?|forbid(?:den)?|ban(?:ned)?)\b",
    re.IGNORECASE,
)

# Fence info-strings whose bodies are structured config we refuse to treat as
# prose -- a content edit inside one of these is meaning-bearing.
_YAML_FENCE_LANGS = {"yaml", "yml", "json", "toml", "config", "cfg", "ini"}

_HUNK_HEADER_RE = re.compile(r"^@@+ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+")
_BLOCKQUOTE_RE = re.compile(r"^(\s*>)+")
_HR_RE = re.compile(r"^\s*(?:-{3,}|={3,}|\*{3,}|_{3,})\s*$")
_FENCE_OPEN_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*([A-Za-z0-9_+-]*)")
_FENCE_CLOSE_RE = re.compile(r"^\s*(?:`{3,}|~{3,})\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$")

# Reference tokens whose change would break a link / path / anchor.
_MD_LINK_RE = re.compile(r"\]\(([^)]+)\)")
_REF_DEF_RE = re.compile(r"^\s*\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
_BARE_URL_RE = re.compile(r"(?:https?|ftp)://[^\s)>\]]+", re.IGNORECASE)
_PATH_TOKEN_RE = re.compile(r"[\w.@~-]*/[\w./@~-]+")
_BACKTICK_PATH_RE = re.compile(r"`([^`]*/[^`]*)`")

# Absolute-path detectors for the mechanical scan.
_WIN_ABS_RE = re.compile(r"[A-Za-z]:[\\/]")
_POSIX_ABS_RE = re.compile(r"(?:^|[\s(\"'`])/[A-Za-z0-9._-]+/")


class _HunkParseError(Exception):
    """Raised when the diff hunks cannot be parsed / applied to the pre-image."""


class _DiffMismatchError(Exception):
    """Raised when a context or deletion line disagrees with the pre-image."""


def _parse_hunks(diff_section_text: str) -> list[dict]:
    """Extract unified-diff hunks from one file's diff section.

    Returns a list of {old_start, old_count, ops} where ops is a list of
    (op, content) with op in {' ', '+', '-'}. Only lines inside an `@@` hunk are
    considered, so file-header noise (`diff --git`, `index`, `--- a/`, `+++ b/`,
    p4 `==== ... ====`) never counts as a change. Raises _HunkParseError on a
    body line that is not a valid hunk-body line.
    """
    hunks: list[dict] = []
    current: Optional[dict] = None
    for line in diff_section_text.splitlines():
        m = _HUNK_HEADER_RE.match(line)
        if m:
            current = {
                "old_start": int(m.group(1)),
                "old_count": int(m.group(2)) if m.group(2) is not None else 1,
                "ops": [],
            }
            hunks.append(current)
            continue
        if current is None:
            continue
        if line == "":
            # A bare blank line inside a hunk is a context line with empty content
            # (git emits a single space for context, but tolerate a stray blank).
            current["ops"].append((" ", ""))
            continue
        op = line[0]
        if op == "\\":  # "\ No newline at end of file" marker -- ignore.
            continue
        if op in (" ", "+", "-"):
            current["ops"].append((op, line[1:]))
        else:
            raise _HunkParseError(f"unexpected hunk body line: {line!r}")
    return hunks


def _reconstruct(pre_lines: list[str], hunks: list[dict]) -> tuple[list[str], set[int], set[int]]:
    """Apply hunks to `pre_lines`, returning (post_lines, added_post, removed_pre).

    `added_post` is the set of 0-based indices in post_lines that were ADDED;
    `removed_pre` is the set of 0-based indices in pre_lines that were REMOVED.
    Raises _HunkParseError if a context/remove line does not match the pre-image
    (a diff that doesn't apply -- the caller then fails closed).
    """
    post: list[str] = []
    pre_idx = 0  # 0-based cursor into pre_lines
    added_post: set[int] = set()
    removed_pre: set[int] = set()
    for hunk in hunks:
        old_start = hunk["old_start"]
        old_count = hunk["old_count"]
        # Number of unchanged pre lines before this hunk's first change.
        context_end = old_start if old_count == 0 else old_start - 1
        if context_end < pre_idx or context_end > len(pre_lines):
            raise _HunkParseError("hunk start out of range for pre-image")
        while pre_idx < context_end:
            post.append(pre_lines[pre_idx])
            pre_idx += 1
        for op, content in hunk["ops"]:
            if op == " ":
                if pre_idx >= len(pre_lines):
                    raise _HunkParseError("context past end of pre-image")
                if pre_lines[pre_idx] != content:
                    raise _DiffMismatchError("context does not match pre-image")
                post.append(pre_lines[pre_idx])
                pre_idx += 1
            elif op == "-":
                if pre_idx >= len(pre_lines):
                    raise _HunkParseError("remove past end of pre-image")
                if pre_lines[pre_idx] != content:
                    raise _DiffMismatchError("deletion does not match pre-image")
                removed_pre.add(pre_idx)
                pre_idx += 1
            else:  # "+"
                added_post.add(len(post))
                post.append(content)
    while pre_idx < len(pre_lines):
        post.append(pre_lines[pre_idx])
        pre_idx += 1
    return post, added_post, removed_pre


def _skeleton(lines: list[str]) -> list[tuple]:
    """Structural skeleton of a Markdown document.

    Captures the SHAPE that a trivial edit must not disturb: heading level+text,
    list nesting (indent + ordered/unordered), fence markers with their info
    string, table separators, blockquote depth, and thematic breaks. Paragraph
    and list-item TEXT are deliberately excluded -- a prose typo is exactly the
    trivial case this guard exists to permit. Heading text IS included, so a
    heading rename (an anchor-affecting change) reads as a structure change.
    """
    skel: list[tuple] = []
    in_fence = False
    fence_char = ""
    for line in lines:
        if in_fence:
            if _FENCE_CLOSE_RE.match(line):
                skel.append(("fence-close",))
                in_fence = False
            # Content inside a fence contributes no skeleton (it is opaque body).
            continue
        fm = _FENCE_OPEN_RE.match(line)
        if fm:
            in_fence = True
            fence_char = fm.group(1)[0]
            skel.append(("fence-open", fm.group(2).lower()))
            continue
        hm = _HEADING_RE.match(line)
        if hm:
            skel.append(("h", len(hm.group(1)), hm.group(2)))
            continue
        if _TABLE_SEP_RE.match(line):
            skel.append(("tsep",))
            continue
        if _HR_RE.match(line):
            skel.append(("hr",))
            continue
        bq = _BLOCKQUOTE_RE.match(line)
        if bq:
            skel.append(("bq", bq.group(0).count(">")))
            continue
        lm = _LIST_RE.match(line)
        if lm:
            marker = "ol" if lm.group(2)[0].isdigit() else "ul"
            skel.append(("li", len(lm.group(1)), marker))
            continue
    return skel


def _reference_tokens(text: str) -> set[str]:
    """All link targets, bare URLs, and path-like / backticked-path tokens in text."""
    tokens: set[str] = set()
    tokens.update(m.strip() for m in _MD_LINK_RE.findall(text))
    tokens.update(m.strip() for m in _REF_DEF_RE.findall(text))
    tokens.update(m.strip() for m in _BARE_URL_RE.findall(text))
    tokens.update(m.strip() for m in _BACKTICK_PATH_RE.findall(text))
    tokens.update(m.strip() for m in _PATH_TOKEN_RE.findall(text))
    return {t for t in tokens if t}


def _yaml_region_indices(lines: list[str]) -> set[int]:
    """0-based line indices that lie inside YAML front-matter or a config fence.

    Front-matter is a `---` ... `---`/`...` block anchored at line 0. Config
    fences are ```lang blocks whose info string is in _YAML_FENCE_LANGS. Both the
    delimiter lines and the body count as "the region", so editing a fence marker
    or a front-matter key both register as a touch.
    """
    region: set[int] = set()
    n = len(lines)
    start = 0
    if n >= 1 and lines[0].strip() == "---":
        j = 1
        while j < n and lines[j].strip() not in ("---", "..."):
            j += 1
        if j < n:  # closed front-matter
            for k in range(0, j + 1):
                region.add(k)
            start = j + 1
    in_fence = False
    fence_is_yaml = False
    for idx in range(start, n):
        line = lines[idx]
        if not in_fence:
            fm = _FENCE_OPEN_RE.match(line)
            if fm:
                in_fence = True
                fence_is_yaml = fm.group(2).lower() in _YAML_FENCE_LANGS
                if fence_is_yaml:
                    region.add(idx)
        else:
            if fence_is_yaml:
                region.add(idx)
            if _FENCE_CLOSE_RE.match(line):
                in_fence = False
                fence_is_yaml = False
    return region


def _delta_lines(hunks: list[dict]) -> tuple[list[str], list[str]]:
    """Return (added_texts, removed_texts) -- the content of the +/- lines."""
    added = [c for h in hunks for op, c in h["ops"] if op == "+"]
    removed = [c for h in hunks for op, c in h["ops"] if op == "-"]
    return added, removed


def triviality_profile(
    diff_section_text: str, pre_image_text: Optional[str]
) -> dict:
    """Compute the pure-mechanical triviality profile for one claimed file.

    Returns {"trivial": bool, "reasons": [<code>, ...]}. `reasons` is empty when
    trivial; otherwise it lists every disqualifier that fired, drawn from:
    "too_large", "structure_changed", "reference_changed", "keyword_changed",
    "yaml_touched", "unparseable", "no_diff", "no_hunks", "diff_mismatch".
    Fails CLOSED when the hunks cannot be parsed or applied to the pre-image.
    """
    if not diff_section_text:
        return {"trivial": False, "reasons": ["no_diff"]}
    try:
        hunks = _parse_hunks(diff_section_text)
    except _HunkParseError:
        return {"trivial": False, "reasons": ["unparseable"]}
    if not hunks:
        return {"trivial": False, "reasons": ["no_hunks"]}

    added, removed = _delta_lines(hunks)
    reasons: list[str] = []

    if len(added) + len(removed) > MAX_CHANGED_LINES:
        reasons.append("too_large")

    # Keyword presence in the delta (added or removed).
    delta_text = "\n".join(added + removed)
    if _KEYWORD_RE.search(delta_text):
        reasons.append("keyword_changed")

    # Reference tokens must be identical between removed and added text.
    if _reference_tokens("\n".join(removed)) != _reference_tokens("\n".join(added)):
        reasons.append("reference_changed")

    # Structure + yaml-touch need the reconstructed post-image.
    pre_lines = pre_image_text.splitlines() if pre_image_text else []
    try:
        post_lines, added_post, removed_pre = _reconstruct(pre_lines, hunks)
    except _DiffMismatchError:
        return {"trivial": False, "reasons": ["diff_mismatch"]}
    except _HunkParseError:
        return {"trivial": False, "reasons": ["unparseable"]}

    if _skeleton(pre_lines) != _skeleton(post_lines):
        reasons.append("structure_changed")

    pre_yaml = _yaml_region_indices(pre_lines)
    post_yaml = _yaml_region_indices(post_lines)
    touched_yaml = any(i in post_yaml for i in added_post) or any(
        i in pre_yaml for i in removed_pre
    )
    if touched_yaml:
        reasons.append("yaml_touched")

    return {"trivial": not reasons, "reasons": reasons}


def mechanical_checks(diff_section_text: str) -> dict:
    """Cheap script-side scans over the CHANGED LINES only, for a skipped file.

    Returns {"ascii_clean": bool, "no_abs_paths": bool}. Never gates -- this is
    the honest "what we checked before skipping" line the skill renders. On an
    unparseable diff both default to True (nothing scannable found); the profile
    has already forced NOT-trivial in that case, so the skip never fires.
    """
    try:
        hunks = _parse_hunks(diff_section_text)
    except _HunkParseError:
        return {"ascii_clean": True, "no_abs_paths": True}
    added, removed = _delta_lines(hunks)
    changed = added + removed
    ascii_clean = all(ord(ch) < 128 for line in changed for ch in line)
    no_abs_paths = not any(
        _WIN_ABS_RE.search(line) or _POSIX_ABS_RE.search(line) for line in changed
    )
    return {"ascii_clean": ascii_clean, "no_abs_paths": no_abs_paths}
