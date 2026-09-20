"""Unit tests for bootstrap_lib.interpreter_env.

The engine exports BOOTSTRAP_PYTHON (and, after project_venv, the project
interpreter) into its own process so manifest commands never guess a Python
on PATH. These tests pin the helper; the engine wiring is pinned separately in
test_engine_python_export.py, because a helper test cannot see whether the
engine calls it.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from bootstrap_lib import env_var_check, interpreter_env, path_check, session_env
from bootstrap_lib.interpreter_env import (
    ENGINE_VAR,
    PROJECT_VAR,
    begin_pass,
    export_project_python,
    shell_path,
)
from bootstrap_lib.tool_check import resolve_bash


def git_bash_or_skip():
    """A bash that understands the engine's Windows paths, or skip.

    On Windows only Git Bash (MSYS) qualifies: a WSL bash found first on PATH
    reports Linux and cannot open a C:/ path. On POSIX any bash does.
    """
    bash = resolve_bash()
    if not bash:
        pytest.skip("no bash on PATH")
    if os.name == "nt":
        try:
            uname = subprocess.run(
                [bash, "-c", "uname -s"], capture_output=True, text=True,
                timeout=30,
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            pytest.skip(f"bash at {bash} did not run: {exc}")
        if not uname.upper().startswith(("MINGW", "MSYS")):
            pytest.skip(f"bash at {bash} is not Git Bash (uname -s: {uname!r})")
    return bash


def fake_venv_python(venv_dir: Path) -> Path:
    """Create the file venv_check._find_python looks for on this platform."""
    rel = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    python = venv_dir.joinpath(*rel)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("")
    return python


class TestConstants:
    def test_call_site_expressions_share_the_hint(self):
        hint = interpreter_env.MIN_VERSION_HINT
        assert hint == f"requires bootstrap >= {interpreter_env.MIN_VERSION}"
        assert interpreter_env.PLUGIN_CALL_SITE_EXPR == (
            '"${BOOTSTRAP_PYTHON:?' + hint + '}"')
        assert interpreter_env.CALL_SITE_EXPR == (
            '"${BOOTSTRAP_PROJECT_PYTHON:-${BOOTSTRAP_PYTHON:?' + hint + '}}"')
        assert (ENGINE_VAR, PROJECT_VAR) == (
            "BOOTSTRAP_PYTHON", "BOOTSTRAP_PROJECT_PYTHON")


class TestShellPath:
    def test_shell_path_forward_slashes_on_windows(self):
        assert shell_path(r"C:\Users\x\python.exe", windows=True) == (
            "C:/Users/x/python.exe")

    def test_shell_path_is_identity_off_windows(self):
        raw = r"/opt/odd\name/python"
        assert shell_path(raw, windows=False) == raw

    def test_shell_path_default_follows_os_name(self):
        raw = r"C:\a\b"
        assert shell_path(raw) == shell_path(raw, windows=(os.name == "nt"))


class TestBeginPass:
    def test_begin_pass_sets_engine_var_and_overrides_inherited(self, monkeypatch):
        monkeypatch.setenv(ENGINE_VAR, "/junk/inherited/python")
        value = begin_pass()
        assert value == shell_path(sys.executable)
        assert os.environ[ENGINE_VAR] == value

    def test_begin_pass_clears_inherited_project_var(self, monkeypatch):
        monkeypatch.setenv(PROJECT_VAR, "/some/other/project/.venv/bin/python")
        begin_pass()
        assert PROJECT_VAR not in os.environ

    def test_begin_pass_never_persists(self, monkeypatch):
        def refuse(*_a, **_k):
            raise AssertionError("begin_pass must not persist anything")

        for module, name in (
            (env_var_check, "set_env_var"),
            (env_var_check, "export_env_var"),
            (env_var_check, "_set_windows_env_var"),
            (path_check, "add_path_to_shell_config"),
            (path_check, "_add_path_to_windows_registry"),
            (session_env, "record"),
            (session_env, "flush"),
        ):
            monkeypatch.setattr(module, name, refuse)
        path_before = os.environ.get("PATH")
        begin_pass()
        assert os.environ.get("PATH") == path_before


class TestExportProjectPython:
    def test_exports_existing_venv_interpreter(self, tmp_path):
        python = fake_venv_python(tmp_path / ".venv")
        value = export_project_python(str(tmp_path / ".venv"))
        assert value == shell_path(str(python))
        assert os.environ[PROJECT_VAR] == value

    def test_missing_interpreter_exports_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv(PROJECT_VAR, "/left/alone")
        assert export_project_python(str(tmp_path / ".venv")) is None
        assert os.environ[PROJECT_VAR] == "/left/alone"


def test_engine_var_executes_under_real_bash():
    """D4: the exported value, forward slashes on Windows, runs under bash."""
    bash = git_bash_or_skip()
    begin_pass()
    completed = subprocess.run(
        [bash, "-c", '"$BOOTSTRAP_PYTHON" -c "import sys; print(sys.version_info[0])"'],
        env=os.environ.copy(), capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "3"


# ---------------------------------------------------------------------------
# interface-v3 (U1r): normative resolution rule, record file, layered settings
# ---------------------------------------------------------------------------

from bootstrap_lib.interpreter_env import (  # noqa: E402
    GLOBAL_KEY,
    LAYERED_KEY,
    OPT_OUT,
    RECORD_OPT_OUT,
    RECORD_SUBDIR,
    STANDALONE_DIR_REL,
    default_project_python,
    export_project_default,
    interpreter_env_settings,
    is_bootstrap_owned,
    normalize_path,
    project_key,
    project_python_note,
    project_python_opted_out,
    read_record,
    record_opted_out,
    remove_record,
    write_record,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_VALUE = "C:/engine/python.exe" if os.name == "nt" else "/engine/bin/python3"


@pytest.fixture(autouse=True)
def _no_ambient_interpreter_env(monkeypatch):
    """`uv run` exports VIRTUAL_ENV; the rule under test reads it (step 2)."""
    for name in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "CLAUDE_ENV_FILE"):
        monkeypatch.delenv(name, raising=False)
    session_env.reset()
    yield
    session_env.reset()


def make_exe(path: Path) -> Path:
    """An empty file every platform's executable test accepts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    path.chmod(0o755)
    return path


def make_venv(venv_dir: Path, *, cfg: bool = True) -> Path:
    """A venv the walk-up qualifies: pyvenv.cfg plus an executable interpreter."""
    rel = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    python = make_exe(venv_dir.joinpath(*rel))
    if cfg:
        (venv_dir / "pyvenv.cfg").write_text("home = /x\n")
    return python


def norm(path) -> str:
    return normalize_path(os.path.abspath(str(path)))


@pytest.fixture
def tree(tmp_path):
    """home/proj with every source the rule knows, each individually removable."""
    home = tmp_path / "home"
    project = home / "proj"
    project.mkdir(parents=True)
    data = tmp_path / "data"
    return {
        "home": home,
        "project": project,
        "data": data,
        "record_dir": data / RECORD_SUBDIR,
        "virtual_env": tmp_path / "active",
        "virtual_env_python": make_venv(tmp_path / "active", cfg=False),
        "recorded": make_exe(tmp_path / "recorded" / "python.exe"),
        "venv": make_venv(project / ".venv"),
    }


def _write_record_line(tree, line):
    tree["record_dir"].mkdir(parents=True, exist_ok=True)
    (tree["record_dir"] / "k").write_text(line + "\n")


def _resolve(tree, *, sources, **kw):
    """default_project_python with only ``sources`` supplied."""
    env = {ENGINE_VAR: ENGINE_VALUE}
    if "virtual_env" in sources:
        env["VIRTUAL_ENV"] = str(tree["virtual_env"])
    if "record" in sources:
        _write_record_line(tree, norm(tree["recorded"]))
    if "venv" not in sources:
        (tree["project"] / ".venv" / "pyvenv.cfg").unlink(missing_ok=True)
    kw.setdefault("opted_out", OPT_OUT in sources)
    return default_project_python(
        str(tree["project"]), record_dir=str(tree["record_dir"]), key="k",
        env=env, home=str(tree["home"]), **kw)


class TestNormativeRule:
    ORDER = (OPT_OUT, "virtual_env", "record", "venv", "engine")

    @pytest.mark.parametrize("source", ORDER)
    def test_default_project_python_source_order(self, tree, source):
        """T10a: each source wins exactly when every higher one is absent."""
        present = self.ORDER[self.ORDER.index(source):]
        expected = {
            OPT_OUT: None,
            "virtual_env": norm(tree["virtual_env_python"]),
            "record": norm(tree["recorded"]),
            "venv": norm(tree["venv"]),
            "engine": normalize_path(ENGINE_VALUE),
        }[source]
        assert _resolve(tree, sources=present) == (expected, source)

    def test_value_form_is_absolute_forward_slash(self, tree):
        value, _ = _resolve(tree, sources=("venv",))
        assert "\\" not in value
        assert os.path.isabs(value)
        if os.name == "nt":
            assert value[0].isupper() and value[1:3] == ":/"

    def test_default_project_python_honours_subdir(self, tree):
        """T10b: the walk starts at <project>/<subdir>, then climbs."""
        (tree["project"] / ".venv" / "pyvenv.cfg").unlink()
        sub_python = make_venv(tree["project"] / "python" / ".venv")
        kw = dict(env={ENGINE_VAR: ENGINE_VALUE}, home=str(tree["home"]))
        assert default_project_python(str(tree["project"]), subdir="python", **kw) == (
            norm(sub_python), "venv")
        assert default_project_python(str(tree["project"]), **kw) == (
            normalize_path(ENGINE_VALUE), "engine")
        # A subdir without its own venv climbs to the project root's venv.
        (tree["project"] / "python" / ".venv" / "pyvenv.cfg").unlink()
        (tree["project"] / ".venv" / "pyvenv.cfg").write_text("")
        assert default_project_python(str(tree["project"]), subdir="python", **kw) == (
            norm(tree["venv"]), "venv")

    def test_opt_out_disables_every_source(self, tree):
        """T10c: an opted-out project has NO project value -- not even an
        activated VIRTUAL_ENV, a recorded interpreter, or a qualifying venv."""
        assert _resolve(tree, sources=("virtual_env", "record", "venv", "engine"),
                        opted_out=True) == (None, OPT_OUT)

    def test_opt_out_record_marker_is_honoured(self, tree):
        """The always lane has no manifest: the record marker carries the opt-out."""
        _write_record_line(tree, RECORD_OPT_OUT)
        assert _resolve(tree, sources=("virtual_env", "venv", "engine")) == (None, OPT_OUT)
        # Without a record_dir (the Step 3c fresh resolution) the marker is unseen.
        assert default_project_python(
            str(tree["project"]), env={ENGINE_VAR: ENGINE_VALUE},
            home=str(tree["home"])) == (norm(tree["venv"]), "venv")

    def test_opt_out_marker_under_global_key_is_ignored(self, tree):
        tree["record_dir"].mkdir(parents=True)
        (tree["record_dir"] / GLOBAL_KEY).write_text(RECORD_OPT_OUT + "\n")
        assert default_project_python(
            str(tree["project"]), record_dir=str(tree["record_dir"]), key=GLOBAL_KEY,
            env={ENGINE_VAR: ENGINE_VALUE}, home=str(tree["home"]))[1] == "venv"

    def test_walk_up_stops_after_home(self, tree, tmp_path):
        """A venv above $HOME is never used; $HOME's own venv is."""
        (tree["project"] / ".venv" / "pyvenv.cfg").unlink()
        make_venv(tmp_path / ".venv")  # tmp_path is home's parent
        kw = dict(env={ENGINE_VAR: ENGINE_VALUE}, home=str(tree["home"]))
        assert default_project_python(str(tree["project"]), **kw)[1] == "engine"
        home_python = make_venv(tree["home"] / ".venv")
        assert default_project_python(str(tree["project"]), **kw) == (
            norm(home_python), "venv")

    def test_venv_without_pyvenv_cfg_does_not_qualify(self, tree):
        (tree["project"] / ".venv" / "pyvenv.cfg").unlink()
        nested = tree["project"] / "pkg"
        nested.mkdir()
        make_venv(nested / ".venv", cfg=False)
        kw = dict(env={ENGINE_VAR: ENGINE_VALUE}, home=str(tree["home"]))
        assert default_project_python(str(nested), **kw)[1] == "engine"

    def test_uv_project_environment_relative_and_absolute(self, tree, tmp_path):
        named = make_venv(tree["project"] / "envs" / "dev")
        kw = dict(home=str(tree["home"]))
        env = {ENGINE_VAR: ENGINE_VALUE, "UV_PROJECT_ENVIRONMENT": "envs/dev"}
        assert default_project_python(str(tree["project"]), env=env, **kw) == (
            norm(named), "venv")
        absolute = make_venv(tmp_path / "elsewhere")
        env["UV_PROJECT_ENVIRONMENT"] = str(tmp_path / "elsewhere")
        assert default_project_python(str(tree["project"]), env=env, **kw) == (
            norm(absolute), "venv")
        # An absolute value that does not qualify falls back to the .venv walk.
        env["UV_PROJECT_ENVIRONMENT"] = str(tmp_path / "missing")
        assert default_project_python(str(tree["project"]), env=env, **kw) == (
            norm(tree["venv"]), "venv")

    def test_engine_fallback_without_engine_var(self, tree):
        (tree["project"] / ".venv" / "pyvenv.cfg").unlink()
        value, source = default_project_python(
            str(tree["project"]), env={}, home=str(tree["home"]))
        assert source == "engine"
        assert value == normalize_path(
            interpreter_env.standalone_python(str(tree["home"])))

    def test_record_ignored_for_global_key(self, tree):
        tree["record_dir"].mkdir(parents=True)
        (tree["record_dir"] / GLOBAL_KEY).write_text(norm(tree["recorded"]) + "\n")
        (tree["project"] / ".venv" / "pyvenv.cfg").unlink()
        assert default_project_python(
            str(tree["project"]), record_dir=str(tree["record_dir"]), key=GLOBAL_KEY,
            env={ENGINE_VAR: ENGINE_VALUE}, home=str(tree["home"]))[1] == "engine"


class TestNormalizePath:
    @pytest.mark.parametrize("raw, expected", [
        ("C:\\Users\\x\\", "C:/Users/x"),
        ("/c/Users/x", "C:/Users/x"),
        ("/d", "D:/"),
        ("c:", "C:/"),
        ("C:/", "C:/"),
        ("C:/a/../b", "C:/b"),
        ("//server/share/x", "//server/share/x"),
    ])
    def test_windows_forms(self, raw, expected):
        assert normalize_path(raw, windows=True) == expected

    def test_posix_keeps_msys_lookalike(self):
        assert normalize_path("/c/Users/x/", windows=False) == "/c/Users/x"


class TestIsBootstrapOwned:
    @staticmethod
    def _owned_exe(home):
        return make_exe(home.joinpath(*STANDALONE_DIR_REL.split("/"), "python", "python.exe"))

    def test_inside_standalone_dir_and_not_a_venv(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        exe = self._owned_exe(home)
        monkeypatch.setattr(sys, "base_prefix", sys.prefix)
        assert is_bootstrap_owned(str(exe), str(home)) is True
        other = make_exe(tmp_path / "other" / "python.exe")
        assert is_bootstrap_owned(str(other), str(home)) is False

    def test_a_venv_is_never_owned(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        exe = self._owned_exe(home)
        monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "base"))
        monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
        assert is_bootstrap_owned(str(exe), str(home)) is False

    def test_sibling_prefix_is_not_inside(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        exe = make_exe(home / ".local" / "share" / "python-standalone-old" / "python.exe")
        monkeypatch.setattr(sys, "base_prefix", sys.prefix)
        assert is_bootstrap_owned(str(exe), str(home)) is False


class TestRecordFile:
    def test_write_policy_and_round_trip(self, tmp_path):
        data = tmp_path / "data"
        exe = norm(make_exe(tmp_path / "py.exe"))
        for source in ("virtual_env", "engine", "record", "override"):
            assert write_record(str(data), "k", exe, source) is False
        assert write_record(str(data), GLOBAL_KEY, exe, "venv") is False
        assert write_record(str(data), GLOBAL_KEY, None, OPT_OUT) is False
        assert not (data / RECORD_SUBDIR).exists()
        assert write_record(str(data), "k", exe, "venv") is True
        path = data / RECORD_SUBDIR / "k"
        # One line WITH a trailing newline: the hook's `read -r` needs it.
        assert path.read_bytes() == (exe + "\n").encode("utf-8")
        assert read_record(str(data), "k") == exe
        assert record_opted_out(str(data), "k") is False
        assert remove_record(str(data), "k") is True
        assert remove_record(str(data), "k") is False
        assert read_record(str(data), "k") is None

    def test_opt_out_marker_format(self, tmp_path):
        """The marker is the literal line `opt_out`; the value is ignored."""
        data = tmp_path / "data"
        assert RECORD_OPT_OUT == "opt_out"
        assert write_record(str(data), "k", "/ignored/python", OPT_OUT) is True
        path = data / RECORD_SUBDIR / "k"
        assert path.read_bytes() == b"opt_out\n"
        assert record_opted_out(str(data), "k") is True
        assert read_record(str(data), "k") is None
        exe = norm(make_exe(tmp_path / "py.exe"))
        assert write_record(str(data), "k", exe, "venv") is True
        assert record_opted_out(str(data), "k") is False

    @pytest.mark.parametrize("content", ["", "\n", "{exe}\n{exe}\n", "{missing}\n"])
    def test_read_record_rejects_bad_content(self, tmp_path, content):
        data = tmp_path / "data"
        exe = norm(make_exe(tmp_path / "py.exe"))
        (data / RECORD_SUBDIR).mkdir(parents=True)
        (data / RECORD_SUBDIR / "k").write_text(
            content.format(exe=exe, missing=norm(tmp_path / "absent.exe")))
        assert read_record(str(data), "k") is None

    def test_read_record_rejects_several_lines_whatever_the_filesystem_says(
            self, tmp_path, monkeypatch):
        """The one-line rule is its own guard: on POSIX a path CAN contain a
        newline, so the executability check alone would not reject it."""
        data = tmp_path / "data"
        (data / RECORD_SUBDIR).mkdir(parents=True)
        (data / RECORD_SUBDIR / "k").write_text("/a/python\n/b/python\n")
        monkeypatch.setattr(interpreter_env, "_is_executable", lambda _p: True)
        assert read_record(str(data), "k") is None
        (data / RECORD_SUBDIR / "k").write_text("/a/python\n")
        assert read_record(str(data), "k") == "/a/python"

    def test_read_record_never_for_global_key(self, tmp_path):
        data = tmp_path / "data"
        exe = norm(make_exe(tmp_path / "py.exe"))
        (data / RECORD_SUBDIR).mkdir(parents=True)
        (data / RECORD_SUBDIR / GLOBAL_KEY).write_text(exe + "\n")
        assert read_record(str(data), GLOBAL_KEY) is None


class TestExports:
    def test_export_project_default_sets_env_and_session_block(self, tree, monkeypatch):
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(tree["data"] / "sessionstart-hook-0.sh"))
        value, source = export_project_default(
            str(tree["project"]), home=str(tree["home"]))
        assert (value, source) == (norm(tree["venv"]), "venv")
        assert os.environ[PROJECT_VAR] == value
        assert session_env._pending[PROJECT_VAR] == value

    def test_export_project_default_opt_out_removes_the_name(self, tree, monkeypatch):
        env_file = tree["data"] / "sessionstart-hook-0.sh"
        env_file.parent.mkdir(parents=True)
        # An earlier writer (the hook prelude) already exported a value.
        env_file.write_text(
            f"export {ENGINE_VAR}=/e\nexport {PROJECT_VAR}=/stale\n", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
        monkeypatch.setenv(PROJECT_VAR, "/inherited")
        session_env.record(PROJECT_VAR, "/buffered")
        assert export_project_default(
            str(tree["project"]), opted_out=True, home=str(tree["home"])) == (None, OPT_OUT)
        assert PROJECT_VAR not in os.environ
        assert PROJECT_VAR not in session_env._pending
        session_env.flush()
        assert env_file.read_text(encoding="utf-8") == f"export {ENGINE_VAR}=/e\n"

    def test_export_project_python_records_session_block(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(tmp_path / "sessionstart-hook-0.sh"))
        python = fake_venv_python(tmp_path / ".venv")
        value = export_project_python(str(tmp_path / ".venv"))
        assert value == shell_path(str(python))
        assert session_env._pending[PROJECT_VAR] == value


class TestLayeredSettings:
    def test_defaults(self):
        assert interpreter_env_settings([], parse_errors=False) == (True, True, [])
        assert interpreter_env_settings(None, parse_errors=False) == (True, True, [])

    def test_user_layers_later_wins(self):
        layers = [{LAYERED_KEY: {"persist": False, "shell_hook": False}},
                  {LAYERED_KEY: {"shell_hook": True}}]
        assert interpreter_env_settings(layers, parse_errors=False) == (False, True, [])

    def test_project_layer_is_ignored_with_a_note(self):
        persist, shell_hook, notes = interpreter_env_settings(
            [{}], parse_errors=False,
            project_layers=[{LAYERED_KEY: {"persist": False}}])
        assert (persist, shell_hook) == (True, True)
        assert len(notes) == 1 and "project manifest is ignored" in notes[0]

    def test_parse_errors_disable_both_with_a_note(self):
        persist, shell_hook, notes = interpreter_env_settings(
            [{LAYERED_KEY: {"persist": True}}], parse_errors=True)
        assert (persist, shell_hook) == (False, False)
        assert len(notes) == 1 and "failed to parse" in notes[0]

    @pytest.mark.parametrize("block", ["off", {"persist": "no"}, {"persits": False}])
    def test_malformed_values_are_noted_and_ignored(self, block):
        persist, shell_hook, notes = interpreter_env_settings(
            [{LAYERED_KEY: block}], parse_errors=False)
        assert (persist, shell_hook) == (True, True)
        assert len(notes) == 1

    @pytest.mark.parametrize("layers, opted_out, noted", [
        ({}, False, False),
        ({"project_python": False}, True, False),
        ({"project_python": ".venv/bin/python"}, False, True),
        ({"project_python": ""}, False, True),
        ({"project_python": True}, False, True),
        ({"project_python": 0}, False, True),
        ({"project_python": None}, False, True),
        ([{"project_python": "a"}, {"project_python": False}], True, False),
        ([{"project_python": False}, {"project_python": "b"}], False, True),
    ])
    def test_project_python_accepts_only_false(self, layers, opted_out, noted):
        """T10d (setting level): false opts out; anything else is one note and
        is treated as absent."""
        assert project_python_opted_out(layers) is opted_out
        note = project_python_note(layers)
        assert (note is not None) is noted
        if noted:
            assert "accepts only false" in note


def test_project_key_matches_hook_sha1():
    """T10h: the engine's fallback key is the hook's `printf | sha1sum` key."""
    bash = git_bash_or_skip()
    script = "printf '%s' \"$PK_DIR\" | sha1sum | awk '{print $1}'"
    for raw in ("/c/Users/you/My Project", "D:/dev/plugins-kit", "/home/u/p"):
        completed = subprocess.run(
            [bash, "-c", script], env={**os.environ, "PK_DIR": raw},
            capture_output=True, text=True, timeout=60,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == project_key(raw)


def test_standalone_dir_literal_mirrors_agree():
    """T11f -- a CONSISTENCY check, not a behaviour test: every mirrored
    spelling of the standalone interpreter directory equals STANDALONE_DIR_REL.
    A rename that moves all five together stays green by design."""
    import json

    boot = REPO_ROOT / "plugins" / "bootstrap"
    rel = STANDALONE_DIR_REL
    needles = {
        boot / "hooks" / "sessionstart" / "session-bootstrap.sh":
            'STANDALONE_DIR="${HOME}/' + rel + '"',
        boot / "hooks" / "userpromptsubmit" / "bootstrap-display.sh":
            '"${HOME}/' + rel + '/python/python.exe"',
        boot / "scripts" / "bootstrap.sh":
            '"${HOME}/' + rel + '/python/python.exe"',
        boot / "shell" / "project-python.sh":
            rel + "/python/python.exe",
    }
    for path, needle in needles.items():
        assert needle in path.read_text(encoding="utf-8"), path.name
    config = json.loads((boot / "defaults" / "config.json").read_text(encoding="utf-8"))
    assert config["self_setup"]["python_stub_check"]["good_python_dir"] == (
        "~/" + rel + "/python")
    assert interpreter_env.standalone_python("/h", windows=True) == (
        "/h/" + rel + "/python/python.exe")
