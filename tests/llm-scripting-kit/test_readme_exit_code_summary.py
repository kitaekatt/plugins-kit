"""Pins README.md's top-level exit-code summary against cli.py's EXIT_* constants.

The summary paragraph used to enumerate only 0..3 ("Exit codes are `0` for
success, `1` for a runtime failure, `2` for invalid input/configuration, and
`3` for a classified persistent halt..."), while `complete --request-file`
can return EXIT_PROTOCOL (4) and `probe` can return EXIT_INDETERMINATE (5) --
5 is documented later in the probe table, but 4 appears nowhere in README at
all.
"""

import re
from pathlib import Path

from llm_scripting_kit import cli


README_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "llm-scripting-kit"
    / "README.md"
)


def _exit_code_summary_paragraph() -> str:
    text = README_PATH.read_text(encoding="utf-8")
    marker = "Exit codes are"
    start = text.index(marker)
    # The paragraph runs to the next blank line.
    end = text.index("\n\n", start)
    return text[start:end]


def test_every_exit_constant_is_named_in_the_readme_summary():
    paragraph = _exit_code_summary_paragraph()
    exit_constants = {
        name: value
        for name, value in vars(cli).items()
        if name.startswith("EXIT_") and isinstance(value, int)
    }
    assert exit_constants, "expected at least one EXIT_* constant on cli"
    missing = [
        f"{name}={value}"
        for name, value in exit_constants.items()
        if f"`{value}`" not in paragraph
    ]
    assert not missing, (
        f"README's exit-code summary paragraph is missing: {missing}\n\n{paragraph}"
    )
