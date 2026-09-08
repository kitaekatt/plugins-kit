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
    """Source-level guard over EVERY display-bound entry in ``bootstrap_lib``.

    The runtime tests above cover behavior; this covers the SHAPE, so a branch
    added anywhere in the package cannot reintroduce the defect by appending a
    raw f-string. The scope is the whole package deliberately: a guard scoped to
    the functions one change happened to touch only watches sites already fixed.

    Exemptions are declared AT THE SITE with a reason:

        ctx.action(f"... {manual_cmd}")  # rule5-exempt: the user retypes it

    A marker on the call's own lines, or on the line directly above it, clears
    that call. The reason text is required and is the point: rule 5 has a real
    exception (a command whose whole purpose is to be retyped), and an
    exemption nobody had to justify is indistinguishable from an oversight. A
    marker travels with the code, which a line-number allowlist does not -- and
    this tree is edited by several sessions at once.
    """

    #: Interpolated names whose values are paths or command strings.
    PATHY = r"(cmd|command|argv|path|dir|root|target|dest|src|file|url|pth)$"

    #: `# rule5-exempt: <reason>` -- the reason is not optional.
    MARKER = r"#\s*rule5-exempt:\s*\S"

    @staticmethod
    def _lib_sources():
        import pathlib as _pl
        lib = _pl.Path(engine.__file__).parent
        return sorted(lib.rglob("*.py"))

    def test_no_unlabelled_path_interpolation_in_bootstrap_lib(self):
        import ast
        import re as _re

        pathy = _re.compile(self.PATHY, _re.I)
        marker = _re.compile(self.MARKER)
        offenders = []

        for src_path in self._lib_sources():
            src = src_path.read_text()
            lines = src.splitlines()
            tree = ast.parse(src)

            for call in ast.walk(tree):
                if not isinstance(call, ast.Call):
                    continue

                fn = call.func
                takes_display = False
                if isinstance(fn, ast.Attribute) and fn.attr == "append":
                    owner = getattr(fn.value, "id", "") or getattr(fn.value, "attr", "")
                    # Only lists that reach the collated display. `ok_entries`
                    # is verbose-only and `quiet_entries` never displays, so
                    # neither is in rule 5's scope.
                    if "action" not in owner:
                        continue
                elif (isinstance(fn, ast.Attribute)
                      and fn.attr in ("action", "fail")
                      and getattr(fn.value, "id", "") == "ctx"):
                    takes_display = True
                else:
                    continue

                # `display=` clears a call only where it is a REAL parameter.
                # `list.append` takes no keyword arguments, so honouring it on
                # the append shape would accept a call that raises TypeError
                # the moment it runs -- silencing the guard with code that
                # cannot work. The append shape's fix is `_append_detail`,
                # which this matcher does not match at all.
                if takes_display and any(k.arg == "display" for k in call.keywords):
                    continue

                leaves = []
                for a in call.args:
                    if not isinstance(a, ast.JoinedStr):
                        continue
                    for v in a.values:
                        if not isinstance(v, ast.FormattedValue):
                            continue
                        leaf = ast.unparse(v.value).split(".")[-1].split("[")[0]
                        if pathy.search(leaf):
                            leaves.append(leaf)
                if not leaves:
                    continue

                # A marker anywhere in the call's own span, or immediately above it.
                first = max(call.lineno - 2, 0)
                last = min(call.end_lineno or call.lineno, len(lines))
                if any(marker.search(ln) for ln in lines[first:last]):
                    continue

                offenders.append(
                    f"{src_path.name}:{call.lineno} interpolates "
                    f"{', '.join(sorted(set(leaves)))} into an unlabelled "
                    f"display entry"
                )

        assert not offenders, (
            "rule 5 (engine-internals.md, 'Collated message text'): pass "
            "display=... / _append_detail(..., display=...) so the path or "
            "command stays in the log, or declare an exemption at the site "
            "with `# rule5-exempt: <reason>`:\n  " + "\n  ".join(offenders)
        )

    def test_guard_would_catch_a_regression(self):
        """The guard has to be able to FAIL -- verified against a real site.

        A guard nobody has seen go red is not a guard. This drives the same
        matcher over a synthetic module shaped like the production call it is
        meant to catch.
        """
        import ast
        import re as _re

        pathy = _re.compile(self.PATHY, _re.I)
        marker = _re.compile(self.MARKER)

        def offenders_in(source):
            found = []
            lines = source.splitlines()
            for call in ast.walk(ast.parse(source)):
                if not isinstance(call, ast.Call):
                    continue
                fn = call.func
                if not (isinstance(fn, ast.Attribute) and fn.attr == "append"):
                    continue
                owner = getattr(fn.value, "id", "") or getattr(fn.value, "attr", "")
                if "action" not in owner:
                    continue
                for a in call.args:
                    if not isinstance(a, ast.JoinedStr):
                        continue
                    for v in a.values:
                        if not isinstance(v, ast.FormattedValue):
                            continue
                        leaf = ast.unparse(v.value).split(".")[-1].split("[")[0]
                        if not pathy.search(leaf):
                            continue
                        first = max(call.lineno - 2, 0)
                        last = min(call.end_lineno or call.lineno, len(lines))
                        if any(marker.search(ln) for ln in lines[first:last]):
                            continue
                        found.append(leaf)
            return found

        bare = 'action_entries.append(f"config: FAILED to load {config_path}")'
        assert offenders_in(bare) == ["config_path"]

        # The real fix for the append shape is _append_detail, which the
        # matcher does not match at all.
        fixed = (
            '_append_detail(action_entries,\n'
            '               f"config: FAILED to load {config_path}",\n'
            '               display="config: FAILED to load")'
        )
        assert offenders_in(fixed) == []

        # And a `display=` passed to list.append does NOT clear the call: that
        # code raises TypeError, so accepting it would let a broken call
        # silence the guard.
        bogus = (
            'action_entries.append(f"config: FAILED to load {config_path}",\n'
            '                      display="config: FAILED to load")'
        )
        assert offenders_in(bogus) == ["config_path"]

        exempted = (
            'action_entries.append(f"run: {manual_cmd}")'
            '  # rule5-exempt: the user retypes it'
        )
        assert offenders_in(exempted) == []

        unreasoned = 'action_entries.append(f"run: {manual_cmd}")  # rule5-exempt:'
        assert offenders_in(unreasoned) == ["manual_cmd"]
