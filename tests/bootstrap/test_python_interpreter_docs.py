"""Docs/discoverability tests for the Python interpreter variables contract (U6r).

Covers the U6r slice of the discoverability channels table (plan v2 section 7,
channel 1: the SKILL.md fact + its reference doc). The engine-side wiring
(the `python: BOOTSTRAP_PYTHON=` ok entry) is U1r's test; the lint wiring is
U7's; the shell/CLI surfaces are U9/U10/U2r's.

T8a and T8c reuse the logic proven in the earlier u6 draft
(D:/Dev/plugins-kit-bp/u6, commit 9b8b159d,
tests/bootstrap/test_manifest_python_docs.py), adapted to the v3 fact id
(`python_interpreter`, not `manifest_python`) and the v3 reference doc
(`python-interpreter.md`, not a section of manifest-reference.md).

T8b, T8d, T8e assert only against constants and doc text this worktree's F0
base already provides (`bootstrap_lib.interpreter_env` and its constants
landed in F0), so all five tests are collectible and runnable standalone in
this worktree -- they do not depend on another parallel unit's module
additions.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = REPO_ROOT / "plugins" / "bootstrap" / "skills" / "bootstrap" / "SKILL.md"
PYTHON_INTERPRETER_REF = (
    REPO_ROOT
    / "plugins"
    / "bootstrap"
    / "skills"
    / "bootstrap"
    / "references"
    / "python-interpreter.md"
)
ROOT_CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
GUARD_PATH = "tests/repo-scripts/test_python_invocation_standard.py"

# The channel-1 discoverability keywords (plan v2 section 7, row 1) the
# python_interpreter fact must carry so an agent searching any of these terms
# finds the fact.
CHANNEL_1_KEYWORDS = [
    "python",
    "python3",
    "interpreter",
    "which python",
    "BOOTSTRAP_PYTHON",
    "BOOTSTRAP_PROJECT_PYTHON",
    "project venv",
    "python not found",
    "command not found",
    "not recognized",
    "Store stub",
    "uv run python",
    "terminal",
    "PowerShell",
    "cmd",
    "shell hook",
    "project_python",
]


def _skill_yaml():
    """Parse the single fenced ```yaml block in bootstrap's SKILL.md."""
    text = SKILL_MD.read_text(encoding="utf-8")
    match = re.search(r"```yaml\n(.*?)\n```", text, re.DOTALL)
    assert match, "SKILL.md has no fenced yaml block"
    return yaml.safe_load(match.group(1))


def _fact(doc, fact_id):
    facts = doc["reference_skill"]["facts"]
    for fact in facts:
        if fact["id"] == fact_id:
            return fact
    return None


def test_skill_fact_python_interpreter_exists_with_channel1_keywords():
    """T8a: the python_interpreter fact exists with the channel-1 keywords.

    Revert: change the fact id back to any other spelling (or delete the
    fact) -- this goes RED because `_fact` returns None.
    """
    doc = _skill_yaml()
    fact = _fact(doc, "python_interpreter")
    assert fact is not None, "SKILL.md is missing the python_interpreter fact"
    keywords = fact.get("keywords", [])
    missing = [kw for kw in CHANNEL_1_KEYWORDS if kw not in keywords]
    assert not missing, f"python_interpreter keywords missing {missing!r}"


def test_python_interpreter_fact_is_grouped_under_config_files():
    """T8a (grouping half): python_interpreter is registered in config_files.fact_ids.

    Revert: remove "python_interpreter" from config_files.fact_ids -- RED,
    since the grouping then no longer routes the config_files keyword surface
    to this fact.
    """
    doc = _skill_yaml()
    groupings = doc["reference_skill"]["groupings"]
    config_files = next((g for g in groupings if g["name"] == "config_files"), None)
    assert config_files is not None, "SKILL.md is missing the config_files grouping"
    assert "python_interpreter" in config_files["fact_ids"]


def test_reference_doc_heading_and_call_site_expressions_are_verbatim():
    """T8b: the reference doc's H1 is REFERENCE_HEADING; both expressions are
    quoted verbatim from interpreter_env.py -- not retyped.

    Revert: hand-edit either quoted expression in python-interpreter.md by one
    character (e.g. drop the `:?`) -- RED, since the doc text then no longer
    contains interpreter_env.CALL_SITE_EXPR / PLUGIN_CALL_SITE_EXPR verbatim.
    """
    from bootstrap_lib import interpreter_env

    text = PYTHON_INTERPRETER_REF.read_text(encoding="utf-8")
    heading_pattern = r"^# " + re.escape(interpreter_env.REFERENCE_HEADING) + r"$"
    assert re.search(heading_pattern, text, re.MULTILINE), (
        f"python-interpreter.md is missing the H1 {interpreter_env.REFERENCE_HEADING!r}"
    )
    assert interpreter_env.CALL_SITE_EXPR in text
    assert interpreter_env.PLUGIN_CALL_SITE_EXPR in text


def test_call_site_expr_fails_loudly_when_unset(monkeypatch):
    """T8c: on real bash, the plugin call-site expression fails loudly when
    BOOTSTRAP_PYTHON is unset.

    Revert: change PLUGIN_CALL_SITE_EXPR's `:?` to `:-` (a default instead of
    a hard failure) -- RED, since the command then exits 0 with an empty
    value instead of failing with MIN_VERSION_HINT.
    """
    from bootstrap_lib import env_features, interpreter_env, tool_check

    monkeypatch.delenv("BOOTSTRAP_PYTHON", raising=False)
    monkeypatch.delenv("BOOTSTRAP_PROJECT_PYTHON", raising=False)
    if tool_check.resolve_bash() is None:
        pytest.skip("no bash resolvable on this host")

    returncode, detail = env_features.run_env_command(
        "echo " + interpreter_env.PLUGIN_CALL_SITE_EXPR, 10
    )
    assert returncode not in (0, None), f"expected a shell failure, got {(returncode, detail)!r}"
    assert interpreter_env.MIN_VERSION_HINT in detail


def test_root_claude_md_names_the_guard():
    """T8d: the root CLAUDE.md text names the repo guard test path.

    Revert: delete GUARD_PATH from CLAUDE.md (or misspell it) -- RED.
    """
    text = ROOT_CLAUDE_MD.read_text(encoding="utf-8")
    assert GUARD_PATH in text


def test_reference_doc_has_per_shell_examples():
    """T8e: bash, PowerShell, and cmd snippets are present in the reference
    doc, each naming at least one of the two interpreter variables.

    Revert: delete the "**cmd.exe**" subsection (or either of the other two)
    from python-interpreter.md -- RED, since the corresponding marker is then
    absent from the text.
    """
    text = PYTHON_INTERPRETER_REF.read_text(encoding="utf-8")

    bash_match = re.search(r"\*\*bash / zsh\*\*\n\n(.+?)\n\n", text, re.DOTALL)
    assert bash_match, "python-interpreter.md is missing a bash/zsh example"
    assert "BOOTSTRAP_PYTHON" in bash_match.group(1)

    powershell_match = re.search(r"\*\*PowerShell[^\n]*\*\*\n\n(.+?)\n\n", text, re.DOTALL)
    assert powershell_match, "python-interpreter.md is missing a PowerShell example"
    assert "BOOTSTRAP_PYTHON" in powershell_match.group(1)

    cmd_match = re.search(r"\*\*cmd\.exe\*\*.*?\n\n(.+?)\n\n", text, re.DOTALL)
    assert cmd_match, "python-interpreter.md is missing a cmd.exe example"
    assert "BOOTSTRAP_PYTHON" in cmd_match.group(1)
