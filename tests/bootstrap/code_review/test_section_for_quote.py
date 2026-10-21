"""section_for_quote returns the CLAUDE.md section a cited rule lives in.

The validator for a claude_md issue gets that section instead of the whole file.
The fixture is a frozen copy of a real project CLAUDE.md (CRLF line endings,
bash code fences containing `#` comment lines).
"""

from __future__ import annotations

from pathlib import Path

from bootstrap_lib.code_review.sections import section_for_quote

FIXTURE = Path(__file__).parent / "fixtures" / "spiritcrossing_CLAUDE.md"


def _fixture_text() -> str:
    return FIXTURE.read_bytes().decode("utf-8")


def test_real_file_submit_safety_stops_before_next_same_level_heading():
    section = section_for_quote(_fixture_text(), "Never `p4 obliterate` -- destroys history.")
    assert section is not None
    assert section.startswith("### Submit safety")
    assert "Never `p4 obliterate`" in section
    assert "Reference integrity" in section  # the section's last bullet is kept
    assert "### Auto Mode pre-authorization" not in section


def test_whitespace_normalized_match_like_the_citation_check():
    section = section_for_quote(
        _fixture_text(), "Never   `p4 obliterate`\n--  destroys history."
    )
    assert section is not None and section.startswith("### Submit safety")


def test_higher_level_heading_ends_the_section():
    text = "# T\n\nintro\n\n## A\n\n### A1\nrule one\n\n## B\nrule two\n"
    assert section_for_quote(text, "rule one") == "### A1\nrule one\n\n"
    assert section_for_quote(text, "rule two") == "## B\nrule two\n"


def test_subsections_stay_inside_their_parent():
    text = "## A\nlead rule\n### A1\nsub rule\n## B\nother\n"
    assert section_for_quote(text, "lead rule") == "## A\nlead rule\n### A1\nsub rule\n"


def test_quote_above_every_heading_returns_whole_file():
    text = "preamble rule\n\n# Title\nbody\n"
    assert section_for_quote(text, "preamble rule") == text


def test_not_found_is_none():
    assert section_for_quote(_fixture_text(), "this rule text exists nowhere") is None


def test_blank_quote_is_none():
    assert section_for_quote(_fixture_text(), "   ") is None


def test_hash_lines_inside_code_fences_are_not_headings():
    text = "## Build\n```bash\n# Full build\n./build.sh\n```\nafter fence rule\n## Next\nx\n"
    assert section_for_quote(text, "after fence rule") == (
        "## Build\n```bash\n# Full build\n./build.sh\n```\nafter fence rule\n"
    )


def test_quote_inside_a_code_fence_returns_its_section():
    text = "## Build\n```bash\n# Full build\n./build.sh\n```\n## Next\nx\n"
    assert section_for_quote(text, "./build.sh") == "## Build\n```bash\n# Full build\n./build.sh\n```\n"


def test_crlf_file_keeps_its_line_endings():
    text = "## A\r\nrule\r\n## B\r\nx\r\n"
    assert section_for_quote(text, "rule") == "## A\r\nrule\r\n"


def test_cli_prints_section_and_reports_not_found(capsys):
    from bootstrap_lib.code_review.sections import main

    assert main(["--claude-md", str(FIXTURE), "--quote", "Never `p4 obliterate` -- destroys history."]) == 0
    out = capsys.readouterr().out
    assert out.startswith("### Submit safety") and "### Auto Mode pre-authorization" not in out

    assert main(["--claude-md", str(FIXTURE), "--quote", "no such rule anywhere"]) == 3
    assert capsys.readouterr().out == ""

    assert main(["--claude-md", str(FIXTURE) + ".missing", "--quote", "x"]) == 2


def test_lane_prompts_still_exports_the_lookup():
    from bootstrap_lib.code_review import lane_prompts, sections

    assert lane_prompts.section_for_quote is sections.section_for_quote


def test_cli_runs_on_an_interpreter_without_pyyaml():
    """Step 7 runs the CLI under bootstrap's own interpreter, which has no pyyaml.

    lane_prompts imports the mechanical checks and through them yaml, so this
    module must not import it, directly or through a package import.
    """
    import subprocess
    import sys

    import bootstrap_lib

    lib_root = Path(bootstrap_lib.__file__).resolve().parents[1]
    code = (
        "import runpy, sys\n"
        "sys.modules['yaml'] = None\n"
        f"sys.path.insert(0, {str(lib_root)!r})\n"
        f"sys.argv = ['sections', '--claude-md', {str(FIXTURE)!r},"
        " '--quote', 'Never `p4 obliterate` -- destroys history.']\n"
        "runpy.run_module('bootstrap_lib.code_review.sections', run_name='__main__')\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, encoding="utf-8"
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("### Submit safety")
