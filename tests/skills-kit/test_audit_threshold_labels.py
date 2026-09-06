"""I4: the row LABEL for name-length / desc-160-char must carry the
RESOLVED threshold, not the hardcoded shipped default. A tuned
desc_max_chars (or name_max_chars) via thresholds: made the row lie -- the
verdict used th["desc_max_chars"] but the label text said "160 chars"
unconditionally.
"""

from pathlib import Path

from skills_kit_lib.audit import THRESHOLDS, audit


def _write(tmp_path: Path) -> Path:
    d = tmp_path / "s"
    d.mkdir()
    p = d / "SKILL.md"
    p.write_text(
        "---\nname: x\ndescription: Use when doing X. Do NOT use for Y.\nskill-type: reference-skill\n---\n"
        "# X\n\n## Example\n\nfoo\n\n## Gotcha\n\nbar\n",
        encoding="utf-8",
    )
    return p


def _row(rows, rule):
    for r in rows:
        if r["rule"] == rule:
            return r
    raise AssertionError(f"rule '{rule}' not found")


class FakeResolved:
    def __init__(self, thresholds):
        self.thresholds = thresholds
        self.disabled_rules = set()


def test_desc_row_label_carries_resolved_limit(tmp_path):
    p = _write(tmp_path)
    resolved = FakeResolved({"desc_max_chars": 200})
    report = audit(p, resolved)
    row = _row(report["universal"], "desc-160-char")
    assert "200" in row["row"], row

    default_report = audit(p)
    default_row = _row(default_report["universal"], "desc-160-char")
    assert str(THRESHOLDS["desc_max_chars"]) in default_row["row"], default_row


def test_name_row_label_carries_resolved_limit(tmp_path):
    p = _write(tmp_path)
    resolved = FakeResolved({"name_max_chars": 32})
    report = audit(p, resolved)
    row = _row(report["universal"], "name-length")
    assert "32" in row["row"], row
