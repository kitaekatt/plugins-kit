"""claudx containment escapes, reproduced at pytest level without launching claude.

Each test asserts the CONTAINED behavior of one escape recorded in
dev/tasks/claudx-shared-lib-escape; the containment mechanics are in
docs/reference/testing.md ("claudx containment mechanics"). An escape that is
reproduced but not yet fixed is marked ``xfail(strict=True)`` so a later fix
XPASSes and strict turns that into a failure telling the fixer to delete the
marker; none is open in this file today.

Isolation: every subprocess gets HOME and USERPROFILE pointed into tmp_path, so
nothing here reads or writes the real ``~/.claude``. The "cached bootstrap" is a
fake whose only effect is a marker file; no real bootstrap pass ever runs (the
real ``session-bootstrap.sh`` performs persistent PATH writes and installs, which
a test must never do to its host -- see test_plugin_test_session.py).

Escape 1 -- harvest launched the CACHED engine from the PRODUCTION registry.
    With no ``engine_ran_version`` stamp in the redirected data dir, harvest.py
    resolved bootstrap's installPath from
    ``$HOME/.claude/plugins/installed_plugins.json`` and detached
    ``<installPath>/hooks/sessionstart/session-bootstrap.sh``. Fixed: harvest.py
    stands down when CLAUDE_PLUGIN_TEST is set.
Escape 2 -- the cached script's stand-down was spelling-fragile.
    The only thing that stopped that launched engine was a glob on its own
    PLUGIN_ROOT (``*/.claude/plugins/cache/*``); an installPath spelled through
    the target of a ``~/.claude`` link missed it. Fixed by the same harvest
    stand-down: the cached engine is never launched, so its glob is not relied on.
Escape 3 -- an inherited re-exec loop flag kept a child on the wrong interpreter.
    ``reexec_under_plugin_venv`` returned on ``_BOOTSTRAP_GUARD_VENV_REEXEC``
    before it looked at ``sys.prefix``. Fixed: the flag names the interpreter it
    re-exec'd into and is trusted only for that interpreter; the legacy value
    "1" is untrusted too, and the test opt-out is the separate variable
    ``_BOOTSTRAP_GUARD_SKIP_REEXEC``.
Escape 5 -- a ``<PLUGIN>_ROOT`` pointing into the plugin cache reached a claudx
    session. Fixed: the engine's export and record, and the SessionStart
    prelude's re-emit, refuse such a value in a claudx session. Only the cache of
    the marketplace the launcher names in CLAUDE_PLUGIN_TEST_MARKETPLACE is
    refused; a plugin installed from another marketplace keeps its root.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import venv
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "plugins" / "bootstrap"
HARVEST_PY = BOOTSTRAP / "bootstrap_lib" / "harvest.py"
SESSION_HOOK = BOOTSTRAP / "hooks" / "sessionstart" / "session-bootstrap.sh"
GUARD_PY = BOOTSTRAP / "bootstrap_lib" / "bootstrap_guard.py"


# --------------------------------------------------------------------------
# Escapes 1 and 2: harvest -> cached session-bootstrap.sh
# --------------------------------------------------------------------------

def _stand_down_prelude() -> str:
    """The REAL session-bootstrap.sh text from ``SCRIPT_DIR=`` up to (not
    including) the trap that precedes any side effect, which holds its
    claude-plugin-test stand-down. Reusing the real text means the glob under
    test is the shipped one. Fails loudly if the script is reshaped."""
    text = SESSION_HOOK.read_text(encoding="utf-8")
    start = text.index("SCRIPT_DIR=")
    end = text.index("# Set trap after BOOTSTRAP_LABEL")
    prelude = text[start:end]
    assert '*/.claude/plugins/cache/*) true ;; *) false ;; esac; then' in prelude, (
        "session-bootstrap.sh no longer carries the CLAUDE_PLUGIN_TEST stand-down "
        "before its first side effect; update this fixture"
    )
    return prelude


def _fake_cached_script(with_stand_down: bool) -> str:
    """A stand-in for the cached bootstrap's session-bootstrap.sh. It records that
    it ran (RAN), that it finished (DONE, via an EXIT trap so an early
    ``exit 0`` in the stand-down still reports), and that it got past the
    stand-down and would have run a pass (ESCAPED)."""
    body = [
        "#!/usr/bin/env bash",
        'trap \': > "$CLAUDX_ESCAPE_DONE"\' EXIT',
        ': > "$CLAUDX_ESCAPE_RAN"',
    ]
    if with_stand_down:
        body.append(_stand_down_prelude())
    body.append(': > "$CLAUDX_ESCAPE_ESCAPED"')
    return "\n".join(body) + "\n"


class _Harvest:
    """One harvest run under a claudx-shaped environment, with a fake HOME."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.data_root = tmp_path / "data-dev"
        self.data_dir = self.data_root / "plugins-kit" / "bootstrap"
        self.project = tmp_path / "project"
        self.project.mkdir()
        self.ran = tmp_path / "marker-ran"
        self.done = tmp_path / "marker-done"
        self.escaped = tmp_path / "marker-escaped"

    def install_fake_cached_bootstrap(
        self, install_path: Path, registry: Path, *, with_stand_down: bool
    ) -> None:
        sb = install_path / "hooks" / "sessionstart" / "session-bootstrap.sh"
        sb.parent.mkdir(parents=True)
        # newline="\n": bash must not see CRLF on a Windows host.
        with open(sb, "w", encoding="utf-8", newline="\n") as f:
            f.write(_fake_cached_script(with_stand_down))
        # The real prelude sources hooks/marketplace-name.sh from PLUGIN_ROOT.
        shutil.copyfile(BOOTSTRAP / "hooks" / "marketplace-name.sh",
                        install_path / "hooks" / "marketplace-name.sh")
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(
            json.dumps({"version": 2, "plugins": {"bootstrap@plugins-kit": [
                {"version": "9.9.9", "installPath": str(install_path)}
            ]}}),
            encoding="utf-8",
        )

    def run(self, *, claudx: bool = True) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.pop("CLAUDE_PLUGIN_TEST", None)
        env.pop("CLAUDE_BOOTSTRAP_DATA_ROOT", None)
        env.update({
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "CLAUDX_ESCAPE_RAN": self.ran.as_posix(),
            "CLAUDX_ESCAPE_DONE": self.done.as_posix(),
            "CLAUDX_ESCAPE_ESCAPED": self.escaped.as_posix(),
        })
        if claudx:
            env["CLAUDE_PLUGIN_TEST"] = "1"
            env["CLAUDE_BOOTSTRAP_DATA_ROOT"] = str(self.data_root)
        # The argument list the UserPromptSubmit hook passes: no --registry, no
        # --settings, and no engine_ran_version stamp in the redirected data dir.
        return subprocess.run(
            [sys.executable, str(HARVEST_PY),
             "--data-dir", str(self.data_dir),
             "--project-dir", str(self.project),
             "--marketplace", "plugins-kit"],
            capture_output=True, text=True, timeout=60, env=env,
            stdin=subprocess.DEVNULL,
        )

    @property
    def launched(self) -> bool:
        """harvest.py recorded a launch (it writes the stamp before spawning)."""
        return (self.data_dir / "harvest_launched_version").exists()

    def wait_for_launched_child(self, timeout: float = 30.0) -> None:
        """Poll for the detached child's completion marker. Only called when a
        launch happened, so the contained case never waits."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.done.exists():
                return
            time.sleep(0.1)
        stderr = self.data_dir / "harvest.stderr"
        raise AssertionError(
            "harvest launched a cached session-bootstrap.sh but it never "
            f"finished within {timeout}s; harvest.stderr="
            f"{stderr.read_text(errors='replace') if stderr.exists() else '<absent>'}"
        )

    def assert_contained(self) -> None:
        proc = self.run()
        assert proc.returncode == 0, proc.stderr
        if not self.launched:
            return  # contained: nothing was launched, nothing to wait for
        self.wait_for_launched_child()
        assert not self.escaped.exists(), (
            "claudx escape: harvest.py launched the CACHED session-bootstrap.sh "
            "from the production registry inside a CLAUDE_PLUGIN_TEST / "
            "CLAUDE_BOOTSTRAP_DATA_ROOT session, and that engine got past its "
            f"stand-down (ran={self.ran.exists()}, escaped=True, "
            f"harvest_launched_version="
            f"{(self.data_dir / 'harvest_launched_version').read_text().strip()!r})"
        )


def test_harvest_does_not_launch_the_cached_engine_in_a_claudx_session(tmp_path):
    """Escape 1. A cached engine that does not stand down (one older than the
    stand-down, or one the stand-down does not recognise) runs a pass against the
    redirected data root, because harvest.py resolves it from the production
    registry. The install path matches the cache glob here on purpose: this is
    the harvest's launch decision, not the stand-down's spelling."""
    h = _Harvest(tmp_path)
    install = h.home / ".claude" / "plugins" / "cache" / "plugins-kit" / "bootstrap" / "9.9.9"
    h.install_fake_cached_bootstrap(
        install, h.home / ".claude" / "plugins" / "installed_plugins.json",
        with_stand_down=False,
    )
    h.assert_contained()


def test_harvest_still_launches_the_cached_engine_outside_claudx(tmp_path):
    """Control for escapes 1 and 2, NOT a containment test: outside a claudx
    session the same fixture DOES launch the cached script. Passing here is what
    makes the two containment tests mean something -- they go green because
    harvest.py stands down under CLAUDE_PLUGIN_TEST, not because the fixture
    never reaches the cached script."""
    h = _Harvest(tmp_path)
    install = h.home / ".claude" / "plugins" / "cache" / "plugins-kit" / "bootstrap" / "9.9.9"
    h.install_fake_cached_bootstrap(
        install, h.home / ".claude" / "plugins" / "installed_plugins.json",
        with_stand_down=False,
    )
    proc = h.run(claudx=False)
    assert proc.returncode == 0, proc.stderr
    assert h.launched, "control needs harvest to have launched the cached script"
    h.wait_for_launched_child()
    assert h.ran.exists(), "the fake cached script never ran"


def _make_dir_link(link: Path, target: Path) -> bool:
    """Directory link usable without elevation: a junction on Windows, a symlink
    elsewhere. False when the host refuses."""
    try:
        if os.name == "nt":
            res = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True, text=True, timeout=30,
            )
            return res.returncode == 0 and link.exists()
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def test_stand_down_does_not_depend_on_how_the_cache_path_is_spelled(tmp_path):
    """Escape 2. ``~/.claude`` is a link to a differently named directory (the
    recorded case: ``~/.claude`` -> a settings checkout), and the registry
    records installPath spelled through the TARGET. PLUGIN_ROOT then lacks
    ``/.claude/plugins/cache/``, the stand-down glob misses, and the cached
    engine runs a pass in a claudx session."""
    h = _Harvest(tmp_path)
    target = tmp_path / "claude-settings"
    target.mkdir()
    link = h.home / ".claude"
    if not _make_dir_link(link, target):
        pytest.skip("cannot create a directory junction/symlink on this host")
    try:
        install = target / "plugins" / "cache" / "plugins-kit" / "bootstrap" / "9.9.9"
        h.install_fake_cached_bootstrap(
            install, link / "plugins" / "installed_plugins.json", with_stand_down=True,
        )
        h.assert_contained()
    finally:
        # rmdir removes the junction/symlink itself, never the target's contents.
        if link.is_symlink() or link.exists():
            try:
                os.rmdir(link)
            except OSError:
                link.unlink()


# --------------------------------------------------------------------------
# Escape 3: inherited re-exec loop flag
# --------------------------------------------------------------------------

_LOOP_FLAG = "_BOOTSTRAP_GUARD_VENV_REEXEC"
_SKIP_FLAG = "_BOOTSTRAP_GUARD_SKIP_REEXEC"


def _run_guarded_consumer(tmp_path: Path, *, inherited_flag: "str | None"):
    """Run a consumer under an interpreter that is NOT the redirected plugin venv
    and report the sys.prefix it ends up on after reexec_under_plugin_venv.

    ``inherited_flag`` is the value of the loop flag in the child's environment,
    or None for an absent flag."""
    redirected = tmp_path / "data-dev"
    plugin_venv = redirected / "plugins-kit" / "git-kit" / ".venv"
    # A real, empty venv: the re-exec target must be an executable that starts.
    # Symlinked on POSIX, as uv and bootstrap build it: EnvBuilder's default copies
    # the interpreter binary, and a copied standalone python (macOS, the
    # @executable_path/../lib libpython rpath) cannot find its library from bin/.
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(plugin_venv)
    script = tmp_path / "consumer.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(GUARD_PY.parent)!r})\n"
        "from bootstrap_guard import reexec_under_plugin_venv\n"
        "reexec_under_plugin_venv('git-kit')\n"
        "print('PREFIX=' + sys.prefix)\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["CLAUDE_BOOTSTRAP_DATA_ROOT"] = str(redirected)
    env.pop(_LOOP_FLAG, None)
    # A full pytest run carries the skip variable from another directory's
    # conftest; the child must make the real re-exec decision.
    env.pop(_SKIP_FLAG, None)
    if inherited_flag is not None:
        env[_LOOP_FLAG] = inherited_flag
    res = subprocess.run(
        [sys.executable, str(script)], env=env,
        capture_output=True, text=True, timeout=60,
    )
    prefix = ""
    for line in res.stdout.splitlines():
        if line.startswith("PREFIX="):
            prefix = line[len("PREFIX="):]
    return res, prefix, plugin_venv


def _on_plugin_venv(prefix: str, plugin_venv: Path) -> bool:
    return bool(prefix) and Path(prefix).resolve() == plugin_venv.resolve()


def test_guard_reexecs_into_the_redirected_venv_without_an_inherited_flag(tmp_path):
    """Control for escape 3, NOT a containment test: with no inherited loop flag the guard
    moves the process into the existing redirected venv. Proves the fixture's
    venv is a valid re-exec target, so the test below fails on the flag alone."""
    res, prefix, plugin_venv = _run_guarded_consumer(tmp_path, inherited_flag=None)
    assert res.returncode == 0, res.stderr
    assert _on_plugin_venv(prefix, plugin_venv), (
        f"expected re-exec into {plugin_venv}, ended on prefix {prefix!r}; "
        f"stderr={res.stderr!r}"
    )


def test_inherited_loop_flag_does_not_leave_a_child_on_the_wrong_interpreter(tmp_path):
    """Escape 3. The flag is process-tree scoped, so a child of another plugin's
    already re-exec'd process inherits it. The guard exports the flag with the
    interpreter it moved into as the value, so the inherited value here is the
    other plugin's venv python. Under redirection with THIS plugin's venv present
    that flag must not stand in for a re-exec: the process re-execs into the
    redirected venv, or it refuses."""
    res, prefix, plugin_venv = _run_guarded_consumer(
        tmp_path,
        inherited_flag=str(
            tmp_path / "data-dev" / "plugins-kit" / "p4-kit" / ".venv" / "bin" / "python"
        ),
    )
    # The guard's only refusal in this state is SystemExit(2) with its
    # "redirected plugin interpreter is missing" line. The fixture venv exists, so
    # that refusal is not expected to fire here; it is accepted only by its exact
    # exit code and message. A crash or a signal exit (negative returncode) never
    # satisfies it, and neither does any other nonzero exit.
    refused = (
        res.returncode == 2
        and not prefix
        and "redirected plugin interpreter is missing" in res.stderr
    )
    assert _on_plugin_venv(prefix, plugin_venv) or refused, (
        f"{_LOOP_FLAG} was inherited, the redirected venv {plugin_venv} exists, and "
        f"the process stayed on sys.prefix={prefix!r} (exit {res.returncode}, "
        f"stderr={res.stderr!r}); neither a re-exec into the venv nor the guard's "
        "refusal (exit 2 with its missing-interpreter message) happened"
    )


def test_an_inherited_legacy_1_flag_lands_on_the_redirected_venv(tmp_path):
    """Escape 3, legacy value. An old-guard parent exports the loop flag as "1",
    which the old guard treated as an opt-out and returned on, keeping the child
    on the wrong interpreter. The loop flag now carries only a target path, so
    the child re-execs into the redirected venv once and cannot loop."""
    res, prefix, plugin_venv = _run_guarded_consumer(tmp_path, inherited_flag="1")
    assert res.returncode == 0, res.stderr
    assert _on_plugin_venv(prefix, plugin_venv), (
        f"{_LOOP_FLAG}=1 was inherited, the redirected venv {plugin_venv} exists, and "
        f"the process stayed on sys.prefix={prefix!r}; the legacy value still opts out"
    )
    assert "legacy" in res.stderr, res.stderr


# --------------------------------------------------------------------------
# Escape 5: <PLUGIN>_ROOT pointing into the plugin cache
# --------------------------------------------------------------------------

_CLAUDX = "CLAUDE_PLUGIN_TEST"
_OWNED = "CLAUDE_PLUGIN_TEST_MARKETPLACE"


@pytest.fixture(autouse=True)
def _no_ambient_owned_marketplace(monkeypatch):
    """A developer's own claudx session exports the owned marketplace; a test
    that does not name one must not inherit it."""
    monkeypatch.delenv(_OWNED, raising=False)


def _cache_plugin(tmp_path: Path, name: str = "alpha-kit", marketplace: str = "kit") -> Path:
    """A plugin install directory inside a ``plugins/cache`` tree, as Claude Code
    lays one out (``cache/<marketplace>/<plugin>/<version>``), holding a
    bootstrap.json so the engine treats it as a plugin."""
    install = tmp_path / "plugins" / "cache" / marketplace / name / "1.0.0"
    install.mkdir(parents=True)
    (install / "bootstrap.json").write_text("{}", encoding="utf-8")
    return install


def _record(data_dir: Path) -> dict:
    from bootstrap_lib.env_var_check import plugin_roots_record_path, read_env_record
    return dict(read_env_record(plugin_roots_record_path(str(data_dir))))


def _maintain(data_dir: Path, *plugins):
    from types import SimpleNamespace

    from bootstrap_lib.engine import _maintain_env_records
    data_dir.mkdir(parents=True, exist_ok=True)
    _maintain_env_records(
        str(data_dir),
        [SimpleNamespace(name=n, install_path=str(p)) for n, p in plugins],
        [], [],
    )


class TestRecordRefusesCacheRoots:
    """The engine's pre-gate record is what the SessionStart prelude re-emits."""

    def test_a_cache_root_is_not_recorded_and_the_refusal_is_loud(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv(_CLAUDX, "1")
        cache = _cache_plugin(tmp_path)
        _maintain(tmp_path / "data", ("alpha-kit", cache))
        assert _record(tmp_path / "data") == {}
        err = capsys.readouterr().err
        assert "ALPHA_KIT_ROOT" in err and str(cache) in err, err

    def test_a_root_outside_the_cache_is_still_recorded(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv(_CLAUDX, "1")
        dev = tmp_path / "checkout" / "plugins" / "alpha-kit"
        dev.mkdir(parents=True)
        (dev / "bootstrap.json").write_text("{}", encoding="utf-8")
        _maintain(tmp_path / "data", ("alpha-kit", dev))
        assert _record(tmp_path / "data") == {"ALPHA_KIT_ROOT": str(dev)}
        assert capsys.readouterr().err == ""

    def test_outside_claudx_a_cache_root_is_recorded_as_before(
        self, tmp_path, monkeypatch, capsys
    ):
        """The refusal is claudx-only: in an ordinary session the cache IS the
        plugin in use."""
        monkeypatch.delenv(_CLAUDX, raising=False)
        cache = _cache_plugin(tmp_path)
        _maintain(tmp_path / "data", ("alpha-kit", cache))
        assert _record(tmp_path / "data") == {"ALPHA_KIT_ROOT": str(cache)}
        assert capsys.readouterr().err == ""

    def test_another_marketplaces_cache_root_is_kept(self, tmp_path, monkeypatch, capsys):
        """A plugin from a marketplace claudx does not replace is legitimately
        installed in the cache; its root is recorded and nothing is printed."""
        monkeypatch.setenv(_CLAUDX, "1")
        monkeypatch.setenv(_OWNED, "kit")
        other = _cache_plugin(tmp_path, "beta-kit", marketplace="other-mkt")
        _maintain(tmp_path / "data", ("beta-kit", other))
        assert _record(tmp_path / "data") == {"BETA_KIT_ROOT": str(other)}
        assert capsys.readouterr().err == ""

    def test_the_owned_marketplaces_cache_root_is_refused(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv(_CLAUDX, "1")
        monkeypatch.setenv(_OWNED, "kit")
        cache = _cache_plugin(tmp_path, "alpha-kit", marketplace="kit")
        _maintain(tmp_path / "data", ("alpha-kit", cache))
        assert _record(tmp_path / "data") == {}
        err = capsys.readouterr().err
        assert "ALPHA_KIT_ROOT" in err and str(cache) in err and "kit" in err, err

    def test_the_owned_marketplace_match_ignores_case(self, tmp_path, monkeypatch, capsys):
        """The cache sits on a case-insensitive filesystem on Windows and macOS."""
        monkeypatch.setenv(_CLAUDX, "1")
        monkeypatch.setenv(_OWNED, "KIT")
        cache = _cache_plugin(tmp_path, "alpha-kit", marketplace="kit")
        _maintain(tmp_path / "data", ("alpha-kit", cache))
        assert _record(tmp_path / "data") == {}
        assert "ALPHA_KIT_ROOT" in capsys.readouterr().err

    def test_with_the_owned_marketplace_unnamed_every_cache_root_is_refused(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv(_CLAUDX, "1")
        monkeypatch.delenv(_OWNED, raising=False)
        other = _cache_plugin(tmp_path, "beta-kit", marketplace="other-mkt")
        _maintain(tmp_path / "data", ("beta-kit", other))
        assert _record(tmp_path / "data") == {}
        err = capsys.readouterr().err
        assert "BETA_KIT_ROOT" in err and _OWNED in err, err

    def test_a_cache_root_reached_through_a_link_is_refused(
        self, tmp_path, monkeypatch, capsys
    ):
        """Judged by where the directory lives, not by how it is spelled: the
        link's own path carries no ``plugins/cache`` component."""
        monkeypatch.setenv(_CLAUDX, "1")
        cache = _cache_plugin(tmp_path)
        link = tmp_path / "settings-checkout"
        if not _make_dir_link(link, cache):
            pytest.skip("cannot create a directory junction/symlink on this host")
        try:
            _maintain(tmp_path / "data", ("alpha-kit", link))
        finally:
            try:
                os.rmdir(link)
            except OSError:
                link.unlink()
        assert _record(tmp_path / "data") == {}
        assert "ALPHA_KIT_ROOT" in capsys.readouterr().err


def _run_engine_pass(tmp_path: Path, install: Path, *, claudx: bool, owned: str = ""):
    """One real engine pass over a registry that records ``install`` for
    alpha-kit, with HOME isolated; returns (result, env_file, data_dir)."""
    from bootstrap.link_compat import link_tree
    from bootstrap.test_engine_multiplugin import (
        BOOTSTRAP_ROOT, _write_minimal_defaults, run_engine,
    )
    plugins_dir = tmp_path / "plugins"
    fake_root = plugins_dir / "bootstrap"
    fake_root.mkdir(parents=True, exist_ok=True)
    link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
    link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
    _write_minimal_defaults(fake_root)
    (fake_root / "bootstrap.json").write_text("{}", encoding="utf-8")
    (plugins_dir / "installed_plugins.json").write_text(json.dumps({"plugins": {
        "kit:alpha-kit": [{"installPath": str(install), "version": "1.0.0"}],
    }}), encoding="utf-8")
    data_dir = tmp_path / "data" / "kit" / "bootstrap"
    data_dir.mkdir(parents=True)
    (data_dir / "config.json").write_text(json.dumps({
        "schema_version": 3, "enabled_plugins": ["kit:alpha-kit"],
        "log_level": "info", "log_success_shell": False, "log_success_checks": True,
    }), encoding="utf-8")
    home = tmp_path / "_home"
    home.mkdir()
    env_file = tmp_path / "env-file.sh"
    env = {k: v for k, v in os.environ.items() if k not in (_CLAUDX, _OWNED, "CLAUDE_ENV_FILE")}
    env.update({"HOME": str(home), "USERPROFILE": str(home), "CLAUDE_ENV_FILE": str(env_file)})
    if claudx:
        env[_CLAUDX] = "1"
    if owned:
        env[_OWNED] = owned
    result = run_engine(str(data_dir), plugin_root=str(fake_root), env=env)
    return result, env_file, data_dir


class TestEnginePassRefusesCacheRoots:
    """The per-plugin export, driven through a real pass."""

    def test_a_pass_exports_a_cache_root_outside_claudx(self, tmp_path):
        """Control: the same pass exports and records the cache path, so the
        refusal below is the claudx check and not an engine that never exports."""
        install = _cache_plugin(tmp_path)
        result, env_file, data_dir = _run_engine_pass(tmp_path, install, claudx=False)
        assert result.returncode == 0, result.stderr
        assert "ALPHA_KIT_ROOT" in env_file.read_text(encoding="utf-8")
        assert _record(data_dir) == {"ALPHA_KIT_ROOT": str(install)}

    def test_a_claudx_pass_refuses_to_export_or_record_a_cache_root(self, tmp_path):
        install = _cache_plugin(tmp_path)
        result, env_file, data_dir = _run_engine_pass(tmp_path, install, claudx=True)
        assert result.returncode == 0, result.stderr
        text = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
        assert "ALPHA_KIT_ROOT" not in text
        assert _record(data_dir) == {}
        assert "ALPHA_KIT_ROOT" in result.stderr and str(install) in result.stderr, result.stderr

    def test_a_claudx_pass_keeps_another_marketplaces_cache_root(self, tmp_path):
        """The install sits under ``cache/kit``; the session owns
        ``launcher-mkt``, so the root is a legitimately installed plugin's and is
        exported and recorded."""
        install = _cache_plugin(tmp_path)
        result, env_file, data_dir = _run_engine_pass(
            tmp_path, install, claudx=True, owned="launcher-mkt")
        assert result.returncode == 0, result.stderr
        assert "ALPHA_KIT_ROOT" in env_file.read_text(encoding="utf-8")
        assert _record(data_dir) == {"ALPHA_KIT_ROOT": str(install)}
        assert "refusing to export" not in result.stderr, result.stderr

    def test_a_claudx_pass_refuses_the_owned_marketplaces_cache_root(self, tmp_path):
        install = _cache_plugin(tmp_path)
        result, env_file, data_dir = _run_engine_pass(
            tmp_path, install, claudx=True, owned="kit")
        assert result.returncode == 0, result.stderr
        text = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
        assert "ALPHA_KIT_ROOT" not in text
        assert _record(data_dir) == {}
        assert "ALPHA_KIT_ROOT" in result.stderr and str(install) in result.stderr, result.stderr


try:  # the prelude is bash; the harness lives beside the other SessionStart tests
    from bootstrap.test_sessionstart_interpreter_env import (
        _env as _hook_env, _run as _hook_run, _scaffold as _hook_scaffold, needs_bash,
    )
except ImportError:  # pragma: no cover - collected from an unexpected rootdir
    needs_bash = pytest.mark.skip(reason="SessionStart harness unavailable")


@needs_bash
class TestPreludeRefusesCacheRoots:
    """A gate-skipped claudx session: the hook re-emits the recorded roots."""

    def _scaffold(self, tmp_path: Path, root: Path):
        from bootstrap_lib.env_var_check import plugin_roots_record_path, write_env_record
        scaffold = _hook_scaffold(tmp_path)
        scaffold.seed_guard()
        write_env_record(
            plugin_roots_record_path(str(scaffold.plugin_data)),
            {"HUE_KIT_ROOT": root.as_posix()},
        )
        return scaffold

    def test_outside_claudx_a_cache_root_is_re_emitted(self, tmp_path):
        """Control: the fixture reaches the re-emit for a ``plugins/cache`` path."""
        root = _cache_plugin(tmp_path, "hue-kit")
        scaffold = self._scaffold(tmp_path, root)
        result = _hook_run(scaffold)
        assert result.returncode == 0, result.stderr
        assert scaffold.exports("HUE_KIT_ROOT") == [f"'{root.as_posix()}'"]

    def test_a_claudx_session_refuses_a_cache_root_loudly(self, tmp_path):
        root = _cache_plugin(tmp_path, "hue-kit")
        scaffold = self._scaffold(tmp_path, root)
        result = _hook_run(scaffold, env=_hook_env(scaffold, **{_CLAUDX: "1"}))
        assert result.returncode == 0, result.stderr
        assert scaffold.exports("HUE_KIT_ROOT") == []
        assert "HUE_KIT_ROOT" in result.stderr and root.as_posix() in result.stderr, \
            result.stderr

    def test_a_claudx_session_still_re_emits_a_dev_root(self, tmp_path):
        root = tmp_path / "checkout" / "plugins" / "hue-kit"
        root.mkdir(parents=True)
        scaffold = self._scaffold(tmp_path, root)
        result = _hook_run(scaffold, env=_hook_env(scaffold, **{_CLAUDX: "1"}))
        assert result.returncode == 0, result.stderr
        assert scaffold.exports("HUE_KIT_ROOT") == [f"'{root.as_posix()}'"]

    def test_a_claudx_session_keeps_another_marketplaces_cache_root(self, tmp_path):
        root = _cache_plugin(tmp_path, "hue-kit", marketplace="other-mkt")
        scaffold = self._scaffold(tmp_path, root)
        result = _hook_run(scaffold, env=_hook_env(scaffold, **{_CLAUDX: "1", _OWNED: "kit"}))
        assert result.returncode == 0, result.stderr
        assert scaffold.exports("HUE_KIT_ROOT") == [f"'{root.as_posix()}'"]
        assert "refusing to export" not in result.stderr, result.stderr

    def test_a_claudx_session_refuses_the_owned_marketplaces_cache_root(self, tmp_path):
        root = _cache_plugin(tmp_path, "hue-kit", marketplace="kit")
        scaffold = self._scaffold(tmp_path, root)
        result = _hook_run(scaffold, env=_hook_env(scaffold, **{_CLAUDX: "1", _OWNED: "kit"}))
        assert result.returncode == 0, result.stderr
        assert scaffold.exports("HUE_KIT_ROOT") == []
        assert "HUE_KIT_ROOT" in result.stderr and root.as_posix() in result.stderr, \
            result.stderr

    def test_the_owned_marketplace_match_ignores_case(self, tmp_path):
        root = _cache_plugin(tmp_path, "hue-kit", marketplace="kit")
        scaffold = self._scaffold(tmp_path, root)
        result = _hook_run(scaffold, env=_hook_env(scaffold, **{_CLAUDX: "1", _OWNED: "KIT"}))
        assert result.returncode == 0, result.stderr
        assert scaffold.exports("HUE_KIT_ROOT") == []
        assert "HUE_KIT_ROOT" in result.stderr, result.stderr

    def test_with_the_owned_marketplace_unnamed_every_cache_root_is_refused(self, tmp_path):
        root = _cache_plugin(tmp_path, "hue-kit", marketplace="other-mkt")
        scaffold = self._scaffold(tmp_path, root)
        result = _hook_run(scaffold, env=_hook_env(scaffold, **{_CLAUDX: "1"}))
        assert result.returncode == 0, result.stderr
        assert scaffold.exports("HUE_KIT_ROOT") == []
        assert "HUE_KIT_ROOT" in result.stderr and _OWNED in result.stderr, result.stderr


class TestLauncherNamesTheOwnedMarketplace:
    """scripts/claude_plugin_test.py is the only writer of CLAUDE_PLUGIN_TEST."""

    def test_the_session_env_carries_the_marketplace_of_the_working_copy(self, tmp_path):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "claude_plugin_test_under_test", REPO_ROOT / "scripts" / "claude_plugin_test.py")
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        env = launcher._session_env({}, tmp_path, launcher._marketplace_name())
        listing = json.loads(
            (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        assert env[_CLAUDX] == "1"
        assert env[_OWNED] == listing["name"]
