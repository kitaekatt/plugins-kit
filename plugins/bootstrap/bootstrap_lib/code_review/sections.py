"""Find the CLAUDE.md section that holds a quoted rule.

Stdlib only, on purpose. Step 7 of p4-code-review and git-code-review runs this
module's CLI under bootstrap's own interpreter, which has no third-party
packages; `lane_prompts` imports the mechanical checks and through them pyyaml,
so the lookup cannot live there. `lane_prompts` re-exports `section_for_quote`.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+\S")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def section_for_quote(text: str, quote: str) -> str | None:
    """Return the Markdown section of `text` that contains `quote`.

    The section runs from the nearest heading above the quote through just
    before the next heading of the same or a higher level; deeper headings stay
    inside it. A quote above every heading returns the whole text. Returns None
    when the quote is not found. The match uses the citation check's whitespace
    normalization, so a quote that `_citation_verification` accepts is found
    here too. Lines inside a fenced code block are never headings.
    """
    normalized_quote = re.sub(r"\s+", " ", quote).strip()
    if not normalized_quote:
        return None
    lines = text.splitlines(keepends=True)
    # Whitespace-normalize the text while remembering which line each
    # normalized character came from.
    chars: list[str] = []
    owner: list[int] = []
    pending_space = False
    for number, line in enumerate(lines):
        for ch in line:
            if ch.isspace():
                pending_space = True
                continue
            if pending_space and chars:
                chars.append(" ")
                owner.append(number)
            pending_space = False
            chars.append(ch)
            owner.append(number)
    start = "".join(chars).find(normalized_quote)
    if start < 0:
        return None
    quote_line = owner[start]

    headings: list[tuple[int, int]] = []  # (line number, level)
    fence: str | None = None
    for number, line in enumerate(lines):
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker[0] * len(marker)
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if fence is None:
            heading = _HEADING_RE.match(line)
            if heading:
                headings.append((number, len(heading.group(1))))

    above = [h for h in headings if h[0] <= quote_line]
    if not above:
        return text
    first, level = above[-1]
    end = len(lines)
    for number, other_level in headings:
        if number > first and other_level <= level:
            end = number
            break
    return "".join(lines[first:end])


def main(argv: list[str] | None = None) -> int:
    """CLI: print the CLAUDE.md section that contains a quoted rule.

    ``python -m bootstrap_lib.code_review.sections --claude-md PATH --quote TEXT``
    prints the section on stdout and exits 0. Exit 3 means the quote was not
    found (stdout stays empty; the caller falls back to the whole file); exit 2
    means the file could not be read.
    """
    parser = argparse.ArgumentParser(prog="sections", description=main.__doc__)
    parser.add_argument("--claude-md", required=True, type=Path)
    parser.add_argument("--quote", required=True)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        text = args.claude_md.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        print(f"cannot read {args.claude_md}: {exc}", file=sys.stderr)
        return 2
    section = section_for_quote(text, args.quote)
    if section is None:
        print(f"quote not found in {args.claude_md}", file=sys.stderr)
        return 3
    sys.stdout.write(section)
    return 0


if __name__ == "__main__":
    sys.exit(main())
