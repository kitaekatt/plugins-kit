"""Tests for bootstrap_lib/manifest_lint.py -- bare-Python manifest lint (U3)."""

import pytest

from bootstrap_lib import manifest_lint

# ---------------------------------------------------------------------------
# T6 -- lint_command positives and negatives
# ---------------------------------------------------------------------------

POSITIVE_COMMANDS = [
    "python3 -c 1",
    "set -e; python -c 1",
    "x && python3 y",
    "$(python z)",
    "exec python3",
    "if python3 -c 1; then :; fi",
    "timeout 5 python3 x",
    "env A=1 python3 x",
    "pythonw x",
    "python3.12 x",
    "uv run python x",
    "uv run --extra dev python x",
    # A redirection target is not a location the command names: the most
    # common bare-python check stays linted.
    "python3 --version >/dev/null 2>&1",
    "python3 -c 1 > /dev/null",
]

NEGATIVE_COMMANDS = [
    '"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" -c 1',
    '"${BOOTSTRAP_PROJECT_PYTHON:-${BOOTSTRAP_PYTHON:?m}}" x',
    "/usr/bin/python3 x",
    "C:/tools/python.exe x",
    "mypython3",
    "pythonic",
    "uv python list",
    "ls ~/.local/share/python-standalone",
    'case "$ver" in ${want}.*) exit 0;; esac',
    "echo hi # python3 here",
    'uv run --project "$P" python x',
    # A command naming any absolute path or any $VAR chose its own locations
    # and is never linted.
    "python3 /opt/tool/setup.py",
    'python3 -c 1 && test -f "$HOME/x"',
    "python3 ~/bin/x.py",
    "python3 C:/tools/x.py",
]


@pytest.mark.parametrize("command", POSITIVE_COMMANDS)
def test_lint_command_flags_bare_python(command):
    assert manifest_lint.lint_command(command) is not None


@pytest.mark.parametrize("command", NEGATIVE_COMMANDS)
def test_lint_command_ignores_safe_forms(command):
    assert manifest_lint.lint_command(command) is None


# ---------------------------------------------------------------------------
# T6b -- message names both call-site forms, the fact, and the reference doc
# ---------------------------------------------------------------------------

def test_lint_manifest_message_names_call_site_and_fact():
    from bootstrap_lib import interpreter_env as ie

    manifest = {"tools": [{"name": "demo", "check": "python3 --version"}]}
    messages = manifest_lint.lint_manifest(manifest, source="bootstrap.json")
    assert len(messages) == 1
    msg = messages[0]
    assert msg.startswith("python: bootstrap.json demo.check calls bare python3; ")
    assert f"the multi-platform default is {ie.PLUGIN_CALL_SITE_EXPR}" in msg
    assert f"(project code: {ie.CALL_SITE_EXPR})" in msg
    assert f"see /bootstrap fact {ie.FACT_ID} and {ie.REFERENCE_DOC}" in msg
    assert ie.FACT_ID == "python_interpreter"
    assert ie.REFERENCE_DOC == "python-interpreter.md"
    assert "manifest_python" not in msg


def test_hint_names_both_forms_and_the_fact():
    from bootstrap_lib import interpreter_env as ie

    hint = manifest_lint.python_hint_for_output("bash: python3: command not found")
    assert hint.startswith("hint: ")
    assert ie.PLUGIN_CALL_SITE_EXPR in hint
    assert ie.CALL_SITE_EXPR in hint
    assert f"/bootstrap fact {ie.FACT_ID}" in hint
    assert ie.REFERENCE_DOC in hint


def test_manifest_lint_takes_its_names_from_interpreter_env():
    """The literals live in interpreter_env only (a consistency check)."""
    import inspect

    source = inspect.getsource(manifest_lint)
    for literal in ("python_interpreter", "python-interpreter.md", "0.120.0",
                    "manifest_python", "Python in manifest commands"):
        assert literal not in source, literal


def test_lint_hit_display_is_short_and_names_the_fact():
    manifest = {"tools": [{"name": "demo", "install": {"linux": "python3 x"}}]}
    hits = manifest_lint.lint_manifest_hits(manifest, source="bootstrap.json")
    assert [(h.entry, h.field, h.token) for h in hits] == [
        ("demo", "install.linux", "python3")]
    assert hits[0].display == "demo.install.linux: bare python3 (fact python_interpreter)"
    assert hits[0].message == manifest_lint.lint_manifest(
        manifest, source="bootstrap.json")[0]


def test_lint_manifest_uv_run_message_says_uv_run_python():
    manifest = {"tools": [{"name": "demo", "check": "uv run python x"}]}
    messages = manifest_lint.lint_manifest(manifest, source="bootstrap.json")
    assert len(messages) == 1
    assert "calls uv run python" in messages[0]
    assert "calls bare" not in messages[0]


# ---------------------------------------------------------------------------
# T6c -- python_hint_for_output positives and negative
# ---------------------------------------------------------------------------

HINT_POSITIVES = [
    "bash: python3: command not found",
    "'python' is not recognized as an internal or external command",
    "Python was not found; run without arguments to install from the Microsoft Store",
    "python3: not found in PATH",
]


@pytest.mark.parametrize("text", HINT_POSITIVES)
def test_python_hint_for_output_positives(text):
    assert manifest_lint.python_hint_for_output(text) is not None


def test_python_hint_for_output_negative():
    assert manifest_lint.python_hint_for_output("git: command not found") is None


# ---------------------------------------------------------------------------
# T6d -- lint_manifest walks every OS key regardless of host
# ---------------------------------------------------------------------------

def test_lint_manifest_walks_every_os_key_regardless_of_host():
    manifest = {
        "tools": [
            {
                "name": "demo",
                "install": {"macos": "python3 setup.py"},
            }
        ]
    }
    messages = manifest_lint.lint_manifest(manifest, source="bootstrap.json")
    assert len(messages) == 1
    assert "demo.install.macos" in messages[0]


# ---------------------------------------------------------------------------
# T6e -- per-OS object form (command + check), env_checks (check + fix),
# and tools[].check are all walked
# ---------------------------------------------------------------------------

def test_lint_manifest_per_os_object_form_and_env_checks_and_tool_check():
    manifest = {
        "tools": [
            {
                "name": "demo",
                "check": "python3 --version",
                "install": {
                    "windows": {
                        "command": "python3 install.py",
                        "check": "python3 verify.py",
                    },
                },
            }
        ],
        "env_checks": [
            {"name": "ssh-key", "check": "python3 check.py", "fix": "python3 fix.py"},
        ],
    }
    messages = manifest_lint.lint_manifest(manifest, source="bootstrap.json")
    joined = "\n".join(messages)
    assert len(messages) == 5
    assert "demo.check" in joined
    assert "demo.install.windows.command" in joined
    assert "demo.install.windows.check" in joined
    assert "ssh-key.check" in joined
    assert "ssh-key.fix" in joined
