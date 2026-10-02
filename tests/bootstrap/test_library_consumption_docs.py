"""Doc guard for references/library-consumption.md.

The reference splits the project-consumer mode into 3a (a project venv that
bootstrap owns, so bootstrap wires the shared libraries in) and 3b (a foreign
interpreter, where bootstrap ships no machinery). The sentence "ships no
machinery" is true only for 3b. Left in a shared preamble above the split it
reads as a claim about every project consumer, which hid the 3a gap.

The anchor is the phrase "ships no machinery" and the `## Mode 3a` / `## Mode 3b`
headings, not the surrounding wording, so rewording a paragraph does not break
the guard; moving the claim out of 3b does.
"""

import re
from pathlib import Path

REF = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "bootstrap" / "skills" / "bootstrap" / "references"
    / "library-consumption.md"
)
CLAIM = "ships no machinery"


def _text():
    return REF.read_text(encoding="utf-8")


def _section(text, heading_prefix):
    """Body of the `## ` section whose heading starts with ``heading_prefix``."""
    m = re.search(rf"^## {re.escape(heading_prefix)}.*$", text, re.M)
    assert m, f"no '## {heading_prefix}' heading in {REF.name}"
    nxt = re.search(r"^## ", text[m.end():], re.M)
    end = m.end() + nxt.start() if nxt else len(text)
    return text[m.end():end]


def test_no_machinery_claim_appears_only_inside_mode_3b():
    text = _text()
    assert text.count(CLAIM) == 1, (
        f"'{CLAIM}' must appear exactly once, inside the Mode 3b section; "
        f"found {text.count(CLAIM)}")
    assert CLAIM in _section(text, "Mode 3b"), (
        f"'{CLAIM}' is true only of a foreign interpreter; it belongs in Mode 3b")
    assert CLAIM not in _section(text, "Mode 3a")


BARE_MODE_3 = re.compile(r"\bmode[ -]3(?![ab0-9])", re.I)


def test_no_bare_mode_3_reference_survives_in_bootstrap_references():
    """Mode 3 was split into 3a and 3b; a bare "mode 3" resolves to neither.

    Scans every reference under the bootstrap skill. The pattern requires the
    digit 3 not followed by `a`, `b` or another digit, so "mode 3a", "mode-3b"
    and "mode 30" never match.
    """
    refs = REF.parent
    hits = [
        f"{p.name}:{n}: {line.strip()[:100]}"
        for p in sorted(refs.glob("*.md"))
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if BARE_MODE_3.search(line)
    ]
    assert not hits, "bare 'mode 3' (use 3a or 3b):\n" + "\n".join(hits)


def test_bare_mode_3_pattern_separates_bare_from_split_forms():
    for bad in ("mode 3)", "Mode 3 (", "the mode-3 recipe", "mode 3."):
        assert BARE_MODE_3.search(bad), bad
    for ok in ("mode 3a", "Mode 3b --", "mode-3b shims", "modes 1-3a"):
        assert not BARE_MODE_3.search(ok), ok


def test_mode_table_has_3a_and_3b_rows():
    rows = [ln for ln in _text().splitlines() if ln.startswith("|")]
    firsts = [ln.split("|")[1].strip() for ln in rows]
    assert "3a" in firsts, "mode table has no 3a row"
    assert "3b" in firsts, "mode table has no 3b row"
    assert "3" not in firsts, "the unsplit mode 3 row must not remain"
