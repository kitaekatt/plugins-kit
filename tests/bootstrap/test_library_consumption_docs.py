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


def test_mode_table_has_3a_and_3b_rows():
    rows = [ln for ln in _text().splitlines() if ln.startswith("|")]
    firsts = [ln.split("|")[1].strip() for ln in rows]
    assert "3a" in firsts, "mode table has no 3a row"
    assert "3b" in firsts, "mode table has no 3b row"
    assert "3" not in firsts, "the unsplit mode 3 row must not remain"
