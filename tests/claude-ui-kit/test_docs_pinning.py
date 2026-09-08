"""Pinning tests for claude-ui-kit's shipped documents.

Each test here pins a fact a document states about the code, by reading BOTH
the document and the code and asserting they agree. A prose contradiction has
no failing test until one is written -- this file exists so a future drift
between a document and statusline.sh / install_statusline.py fails a test
instead of shipping silently. See
dev/tasks/plugin-architecture-audit/briefs/claude-ui-kit-slice2.md.
"""

import re
from pathlib import Path

import install_statusline

_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "claude-ui-kit"
_README = _PLUGIN_ROOT / "README.md"
_SKILL = _PLUGIN_ROOT / "skills" / "statusline" / "SKILL.md"
_COMPONENTS = _PLUGIN_ROOT / "skills" / "statusline" / "references" / "components.md"
_STYLING = _PLUGIN_ROOT / "skills" / "statusline" / "references" / "styling.md"
_INSTALLER = _PLUGIN_ROOT / "scripts" / "install_statusline.py"
_STATUSLINE_SH = _PLUGIN_ROOT / "scripts" / "statusline.sh"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Item 1: SKILL.md's Windows statusLine.command guidance must prescribe the
# same portable command the installer emits -- not a machine-specific one.
# ---------------------------------------------------------------------------

class TestItem1PortableCommandGuidance:
    def test_no_absolute_interpreter_or_drive_letter_near_statusline_command(self):
        text = _read(_SKILL)
        lines_mentioning = [
            line for line in text.splitlines() if "statusLine.command" in line
        ]
        assert lines_mentioning, "expected at least one statusLine.command mention"
        drive_letter = re.compile(r"\b[A-Za-z]:[\\/]")
        for line in lines_mentioning:
            assert "bash.exe" not in line, f"absolute interpreter in: {line!r}"
            assert not drive_letter.search(line), f"drive letter in: {line!r}"

    def test_prescribed_command_satisfies_installer_is_portable(self):
        text = _read(_SKILL)
        m = re.search(r"`(bash ~/\.claude/[^`]+statusline\.sh)`", text)
        assert m, "SKILL.md must quote the exact portable command"
        prescribed = m.group(1)
        assert install_statusline._is_portable(prescribed)


# ---------------------------------------------------------------------------
# Item 2: README's install target must name the tracked user-global
# settings.json, and must not claim settings.local.json as the write target.
# ---------------------------------------------------------------------------

class TestItem2InstallTarget:
    def test_readme_names_tracked_user_global_settings(self):
        text = _read(_README)
        install_para = text.split("## Status line", 1)[1].split("##", 1)[0]
        assert "~/.claude/settings.json" in install_para
        assert "settings.local.json" not in install_para

    def test_readme_describes_conflict_as_a_failure_not_a_skip(self):
        text = _read(_README)
        conflict_section = text.split("## Conflict avoidance", 1)[1].split("##", 1)[0]
        assert "fix-all failure" in conflict_section
        assert "Skips entirely" not in conflict_section


# ---------------------------------------------------------------------------
# Item 3: components.md must not recommend @tsv, and must key its guidance
# off the \x1f delimiter statusline.sh actually uses.
# ---------------------------------------------------------------------------

class TestItem3DelimiterGuidance:
    def test_components_md_does_not_recommend_tsv(self):
        text = _read(_COMPONENTS)
        # The old broken guidance recommended @tsv as the fast pattern; it may
        # still be named for contrast ("not @tsv"), but never recommended.
        assert not re.search(r"with `@tsv`\) is fastest", text)
        assert "\\x1f" in text, "must key its guidance off the delimiter the script uses"

    def test_script_actually_uses_unit_separator(self):
        script = _read(_STATUSLINE_SH)
        assert "IFS=$'\\x1f'" in script
        assert re.search(r'join\("\\u001f"\)', script)
        # jq is never told to actually format as @tsv (the comment names it
        # only to explain why it was rejected).
        assert not re.search(r"@tsv['\")]", script)


# ---------------------------------------------------------------------------
# Item 4: SKILL.md must not prescribe `echo -e`; the script uses printf '%s'.
# ---------------------------------------------------------------------------

class TestItem4NoEchoDashE:
    def test_skill_md_does_not_prescribe_echo_dash_e(self):
        text = _read(_SKILL)
        # The old broken guidance prescribed echo -e as part of the output
        # discipline; it may still be named for contrast ("not echo -e"), but
        # never prescribed as the thing to preserve.
        assert not re.search(r"ANSI escapes, `echo -e`", text)
        assert "printf '%s'" in text, "must key its guidance off what the script uses"

    def test_script_uses_printf_percent_s(self):
        script = _read(_STATUSLINE_SH)
        assert "printf '%s'" in script


# ---------------------------------------------------------------------------
# Item 5: documented default thresholds must equal the defaults parsed out of
# statusline.sh, so a future default change fails this test instead of
# shipping a stale doc.
# ---------------------------------------------------------------------------

def _parse_default(script: str, var: str) -> str:
    m = re.search(rf'{var}="\$\{{STATUSLINE_{var}:-(\d+)\}}"', script)
    assert m, f"could not find default for {var} in statusline.sh"
    return m.group(1)


class TestItem5ThresholdDefaults:
    def test_shipped_defaults(self):
        script = _read(_STATUSLINE_SH)
        assert _parse_default(script, "CTX_ORANGE_AT") == "70"
        assert _parse_default(script, "CTX_RED_AT") == "30"
        assert _parse_default(script, "SESS_ORANGE_AT") == "30"
        assert _parse_default(script, "SESS_RED_AT") == "10"
        assert _parse_default(script, "WEEK_ORANGE_AT") == "30"
        assert _parse_default(script, "WEEK_RED_AT") == "10"

    def test_readme_documents_matching_defaults(self):
        script = _read(_STATUSLINE_SH)
        text = _read(_README)
        ctx_orange = _parse_default(script, "CTX_ORANGE_AT")
        ctx_red = _parse_default(script, "CTX_RED_AT")
        sess_orange = _parse_default(script, "SESS_ORANGE_AT")
        sess_red = _parse_default(script, "SESS_RED_AT")
        week_orange = _parse_default(script, "WEEK_ORANGE_AT")
        week_red = _parse_default(script, "WEEK_RED_AT")
        assert f"orange at or below {ctx_orange}%, red at or below {ctx_red}%" in text
        assert f"orange at or below {sess_orange}%, red at or below {sess_red}%" in text
        assert f"orange at or below {week_orange}%, red at or below {week_red}%" in text
        assert "STATUSLINE_WEEK_ORANGE_AT" in text
        assert "STATUSLINE_WEEK_RED_AT" in text
        # README must not still claim the 7-day cell is uncolored.
        assert "gray (no thresholds)" not in text

    def test_readme_week_cell_is_not_claimed_uncolored(self):
        text = _read(_README)
        assert "no thresholds" not in text


# ---------------------------------------------------------------------------
# Item 6: the copy-to-user-owned-location instruction and the segments-dir
# override must appear together, not in a separate section.
# ---------------------------------------------------------------------------

class TestItem6SegmentsDirAtCopyPoint:
    def test_copy_step_and_segments_override_are_the_same_bullet(self):
        text = _read(_SKILL)
        m = re.search(r"Copy it to `~/\.claude/statusline\.sh`.*", text)
        assert m, "expected the copy-to-user-owned-location step"
        copy_line = m.group(0)
        assert "STATUSLINE_SEGMENTS_DIR" in copy_line


# ---------------------------------------------------------------------------
# Item 7: ASCII in the shipped documents. Scoped to the two characters the
# repo's ASCII rule names by example (em-dash, right arrow) -- these files
# also legitimately quote statusline.sh's own render glyphs (emoji, block
# bars, box-drawing separators) as illustrative content, which this test
# does not touch. statusline.sh itself is excluded entirely: it is outside
# this slice's file-ownership list and its glyphs ARE the rendered product.
# ---------------------------------------------------------------------------

# Spelled as escapes, not literals: this file is tracked source and the repo's
# ASCII rule covers it too, detector or not.
_PROSE_PUNCTUATION_VIOLATIONS = ("\u2014", "\u2192")  # em-dash, rightwards arrow

_DOCS_UNDER_TEST = [_README, _SKILL, _COMPONENTS, _STYLING, _INSTALLER]


class TestItem7AsciiProsePunctuation:
    def test_no_em_dash_or_arrow_in_owned_documents(self):
        for path in _DOCS_UNDER_TEST:
            text = _read(path)
            for ch in _PROSE_PUNCTUATION_VIOLATIONS:
                assert ch not in text, f"{path.name} still contains {ch!r}"

    def test_statusline_sh_is_excluded_and_untouched(self):
        # Not in this slice's ownership; asserted present, not modified.
        assert _STATUSLINE_SH.is_file()


# ---------------------------------------------------------------------------
# Item 8: README must not use temporal deixis, and must correctly describe
# the segment normalization (120-char cap, missing-timeout case). The
# missing-timeout half of this pin also lives alongside
# test_overlong_sh_segment_is_capped in test_statusline.py.
# ---------------------------------------------------------------------------

_TEMPORAL_DEIXIS = ("Currently ships", "Future home", "no longer used")


class TestItem8NoTemporalDeixisAndAccurateSegmentApi:
    def test_readme_has_no_temporal_deixis(self):
        text = _read(_README)
        for phrase in _TEMPORAL_DEIXIS:
            assert phrase not in text, f"temporal deixis found: {phrase!r}"

    def test_readme_segment_api_names_the_cap_and_missing_timeout_case(self):
        text = _read(_README)
        segment_section = text.split("## Segment API", 1)[1].split("##", 1)[0]
        assert "120" in segment_section
        assert "timeout(1)" in segment_section
        # The old broken claim was that stdout is appended verbatim; it may
        # still be named for contrast ("NOT appended verbatim"), but the
        # unqualified claim must be gone. Whitespace-normalized because the
        # phrase wraps across a line break in prose.
        normalized = " ".join(segment_section.split())
        assert "is appended verbatim" not in normalized
