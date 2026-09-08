"""Enforcement for engine-internals.md's collated-message rule 5.

The rule: "Never put absolute paths or shell command strings in display text.
Keep the complete entry in `bootstrap.log`."

The rule was applied by hand at three call sites and, in that state, held only
for as long as everyone remembered it -- a documented rule with no test is a
rule that drifts. These tests render each reclassified path the way `_main`
does (`messages.numbered` over the action entries, which is what produces the
`--- <header>: ... ---` display line) and assert the result carries no absolute
path and no backticked command, while the underlying entry still carries both
for the log.

Scope note: this pins the paths the reclassification touched. It is not a
whole-engine sweep -- an entry emitted somewhere else can still violate rule 5,
and adding it here is the way to close that hole.
"""

import re

import pytest

from bootstrap_lib import engine
from bootstrap_lib.messages import numbered

# A POSIX absolute path (/Users/..., /home/...) or a Windows one (C:\..., D:/...).
# Deliberately NOT anchored to a separator class that would also match a bare
# "/" in prose: the target is a rooted path of at least two segments.
_ABS_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|(?<![\w.])/)[\w.$-]+[\\/][\w.$-]+")
_BACKTICKED_COMMAND = re.compile(r"`[^`]*\b(?:uv|npm|pip|git|claude|scoop|brew|apt)\b[^`]*`")


def assert_display_clean(rendered):
    """The rendered display line must carry no absolute path and no command."""
    path = _ABS_PATH.search(rendered)
    assert path is None, (
        f"rule 5: absolute path {path.group(0)!r} reached display text: {rendered!r}"
    )
    cmd = _BACKTICKED_COMMAND.search(rendered)
    assert cmd is None, (
        f"rule 5: shell command {cmd.group(0)!r} reached display text: {rendered!r}"
    )


class _Result:
    def __init__(self, passed, message, remediation_cmd=None):
        self.passed = passed
        self.message = message
        self.remediation_cmd = remediation_cmd


class TestRegexSelfCheck:
    """The assertions are only worth as much as the patterns behind them."""

    @pytest.mark.parametrize("bad", [
        "venv: not ready, running `uv sync --project /Users/x/.claude/p/0.4.1`",
        r"venv: stale editable install: points at C:\Users\truff\.claude\cache",
        "project_npm: not ready, running `npm ci` in /Users/x/proj",
        "project config: migrated /tmp/old/config.yaml -> /tmp/new/config.yaml",
    ])
    def test_violations_are_detected(self, bad):
        with pytest.raises(AssertionError):
            assert_display_clean(bad)

    @pytest.mark.parametrize("good", [
        "venv: re-synced",
        "project_npm: installed",
        "project config: created unreal-kit.yaml",
        "env_check repo-sync-tasks: fixed",
        "updated: 0.4.0 -> 0.4.1",
    ])
    def test_compliant_lines_pass(self, good):
        assert_display_clean(good)


class TestVenvDisplay:
    def test_resync_detail_stays_out_of_display(self, tmp_path, monkeypatch):
        """The uv argv and the stale-.pth diagnostic are log-only."""
        cache = r"C:\Users\truff\.claude\plugins\cache\plugins-kit\job-kit\0.4.1"
        monkeypatch.setattr(engine, "_process_venv_def", engine._process_venv_def)
        from bootstrap_lib import venv_check

        monkeypatch.setattr(
            venv_check, "ensure_venv",
            lambda *a, **k: (
                _Result(True, "ok"),
                [
                    f"not ready, running `uv sync --project {cache}` - stale "
                    f"editable install: __editable__.job_kit-0.4.0.pth points "
                    f"at /Users/x/Dev/claude-settings/plugins/cache/job-kit/0.4.0/lib",
                    "re-synced",
                ],
            ),
        )
        monkeypatch.setattr(venv_check, "export_venv_env_var", lambda *a, **k: None)

        actions, oks, failures, quiet = [], [], [], []
        engine._process_venv_def(
            {"check_imports": []}, str(tmp_path), str(tmp_path), "", "venv",
            actions, oks, failures, plugin_name="job-kit", quiet_entries=quiet,
        )

        assert_display_clean(numbered(actions))
        assert "venv: re-synced" in numbered(actions)
        # The detail is not discarded -- it is retained for the log.
        assert any("uv sync --project" in q for q in quiet), quiet

    def test_failure_display_is_short_but_logged_in_full(self, tmp_path, monkeypatch):
        from bootstrap_lib import venv_check

        monkeypatch.setattr(
            venv_check, "ensure_venv",
            lambda *a, **k: (
                _Result(False, f"uv sync failed in /Users/x/.claude/plugins/data/p"),
                [],
            ),
        )
        actions, oks, failures, quiet = [], [], [], []
        engine._process_venv_def(
            {"check_imports": []}, str(tmp_path), str(tmp_path), "", "venv",
            actions, oks, failures, plugin_name="job-kit", quiet_entries=quiet,
        )

        assert_display_clean(numbered(actions))
        assert failures, "a failed venv must still register a fix-all failure"
        assert "/Users/x" in str(actions[0]), "the log text keeps the full message"


class TestProjectNpmDisplay:
    def test_npm_argv_and_project_dir_stay_out_of_display(self, tmp_path, monkeypatch):
        """The sibling of the venv case -- the defect this test was added for."""
        from bootstrap_lib import npm_check

        monkeypatch.setattr(
            npm_check, "ensure_node_modules",
            lambda *a, **k: (
                _Result(True, "node_modules is current"),
                [
                    "not ready, running `npm ci` in /Users/x/Dev/proj",
                    "created",
                ],
            ),
        )
        monkeypatch.setattr(
            engine, "_resolve_project_subdir",
            lambda project_dir, subdir, label: (str(tmp_path), None),
        )

        quiet = []
        actions, oks, failures = engine._process_project_npm(
            {}, str(tmp_path), quiet_entries=quiet)

        assert_display_clean(numbered(actions))
        assert any("npm ci" in q for q in quiet), quiet

    def test_npm_failure_display_is_short(self, tmp_path, monkeypatch):
        from bootstrap_lib import npm_check

        monkeypatch.setattr(
            npm_check, "ensure_node_modules",
            lambda *a, **k: (
                _Result(False, "npm ci failed (exit 1) in /Users/x/Dev/proj",
                        remediation_cmd="npm ci"),
                ["npm ci failed (exit 1) in /Users/x/Dev/proj: ELIFECYCLE"],
            ),
        )
        monkeypatch.setattr(
            engine, "_resolve_project_subdir",
            lambda project_dir, subdir, label: (str(tmp_path), None),
        )

        quiet = []
        actions, oks, failures = engine._process_project_npm(
            {}, str(tmp_path), quiet_entries=quiet)

        assert_display_clean(numbered(actions))
        assert failures, "a failed project_npm must still register a fix-all failure"


class TestAuthoredLabelWinsRegardlessOfWidth:
    """`numbered()` must honour an authored display label even when the full
    text fits, or rule 5 holds only for entries that happen to be long."""

    def test_short_entry_with_authored_label_uses_the_label(self):
        from bootstrap_lib.records import Entry

        # 35 chars -- comfortably under ITEM_MAX, and carrying a path.
        entry = Entry("project config: updated /tmp/x.yaml",
                      short="project config: updated x.yaml")
        rendered = numbered([entry])
        assert rendered == "project config: updated x.yaml"
        assert_display_clean(rendered)

    def test_long_entry_without_a_label_still_derives_at_a_separator(self):
        long_text = "some-tool: FAILED - install attempted but not found in PATH"
        assert numbered([long_text]) == "some-tool: FAILED"


class TestLinkToolDirToPath:
    """Drives the real function -- reverting it to a bare append fails here."""

    def test_added_to_path_keeps_the_dir_out_of_display(self, tmp_path, monkeypatch):
        from bootstrap_lib import path_check

        tool_dir = tmp_path / "Users" / "x" / ".local" / "bin"
        tool_dir.mkdir(parents=True)
        monkeypatch.setattr(
            path_check, "add_path_to_shell_config",
            lambda *a, **k: (True, "rc file updated"),
        )
        monkeypatch.setenv("PATH", "/usr/bin")

        result = type("R", (), {
            "on_path": False, "path": str(tool_dir / "uv"), "subject": "uv",
        })()
        actions = []
        engine._link_tool_dir_to_path(result, "", actions)

        assert actions, "the function must still emit an entry"
        assert_display_clean(numbered(actions))
        assert str(tool_dir) in str(actions[0]), "the log text keeps the dir"

    def test_path_persist_failure_keeps_the_dir_out_of_display(self, tmp_path, monkeypatch):
        from bootstrap_lib import path_check

        tool_dir = tmp_path / "Users" / "x" / ".local" / "bin"
        tool_dir.mkdir(parents=True)
        monkeypatch.setattr(
            path_check, "add_path_to_shell_config",
            lambda *a, **k: (False, "rc file is read-only"),
        )
        monkeypatch.setenv("PATH", "/usr/bin")

        result = type("R", (), {
            "on_path": False, "path": str(tool_dir / "uv"), "subject": "uv",
        })()
        actions = []
        engine._link_tool_dir_to_path(result, "", actions)

        assert actions
        assert_display_clean(numbered(actions))
        assert "FAILED" in numbered(actions), "a failure must still read as one"


class TestNoUnlabelledPathInterpolation:
    """Source-level guard over the functions this work touched.

    The runtime tests above cover behavior; this covers the SHAPE, so a new
    branch added to one of these functions cannot reintroduce the defect by
    appending a raw f-string. Scoped to named functions deliberately -- a
    whole-module sweep needs an allowlist of legitimate exemptions (an entry
    whose payload IS a command the user must retype), and that triage is not
    yet done.
    """

    GUARDED = ("_link_tool_dir_to_path", "_process_venv_def",
               "_process_project_npm", "_process_project_config")

    def test_guarded_functions_label_every_path_bearing_entry(self):
        import ast
        import pathlib as _pl
        import re as _re

        src = _pl.Path(engine.__file__).read_text()
        tree = ast.parse(src)
        pathy = _re.compile(r"(cmd|command|argv|path|dir|root|target|dest|src|file|pth)$", _re.I)

        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name not in self.GUARDED:
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                fn = call.func
                # a bare `<something>action_entries.append(...)`
                if not (isinstance(fn, ast.Attribute) and fn.attr == "append"):
                    continue
                owner = getattr(fn.value, "id", "") or getattr(fn.value, "attr", "")
                if "action" not in owner:
                    continue
                for a in call.args:
                    if not isinstance(a, ast.JoinedStr):
                        continue
                    for v in a.values:
                        if isinstance(v, ast.FormattedValue):
                            leaf = ast.unparse(v.value).split(".")[-1].split("[")[0]
                            if pathy.search(leaf):
                                offenders.append(
                                    f"{node.name}:{call.lineno} interpolates {leaf!r} "
                                    f"into an unlabelled action entry"
                                )
        assert not offenders, (
            "rule 5: use _append_detail(..., display=...) instead of a bare "
            "append:\n  " + "\n  ".join(offenders)
        )
