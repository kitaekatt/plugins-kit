"""The task-kit launcher: decisions, browser opening, data-dir resolution, and
real start / reuse / restart / stop against real server subprocesses.

The decision tests inject every effect (probe, process identity, spawn, browser)
so each branch of "what is recorded, and is it really ours" is exercised without
a server. The integration tests run the real entrypoint in subprocesses over a
COPY of the plugin tree, so a code change can be made and the launcher's
restart-on-change observed.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest
from dashboard_support import ENTRYPOINT, PLUGIN_ROOT, REPO_ROOT, SCRIPTS_DIR, clean_env, make_task

from task_system.dashboard import launcher as L
from task_system.dashboard import state as dstate
from task_system.dashboard.codehash import code_hash

HASH = "a" * 64


# ---------------------------------------------------------------------------
# decisions with injected effects
# ---------------------------------------------------------------------------


def make_state(root: Path, **over) -> dstate.ServerState:
    values = dict(schema=1, pid=4242, pid_create_time=1000.0, nonce="nonce-1", port=40001,
                  token="tok-1", code_hash=HASH, started_at="2026-10-17T00:00:00+00:00",
                  log_path=str(root / "server.log"))
    values.update(over)
    state = dstate.ServerState(**values)
    root.mkdir(parents=True, exist_ok=True)
    dstate.write_state(root, state)
    return state


class Fakes:
    """Scriptable world: which pids are alive, what the port answers."""

    def __init__(self):
        self.create_times: dict[int, float] = {}
        self.health: dict | Exception | None = None
        self.spawned: list = []
        self.shutdowns: list = []
        self.killed: list = []
        self.opened: list = []
        self.browser_error: L.BrowserOpenError | None = None
        self.on_shutdown = None

    def probe(self, port):
        if isinstance(self.health, Exception):
            raise self.health
        if self.health is None:
            raise L.ProbeFailed("refused", "nothing listening")
        return self.health

    def create_time(self, pid):
        return self.create_times.get(pid)

    def shutdown(self, state, force):
        self.shutdowns.append(force)
        if self.on_shutdown:
            self.on_shutdown()

    def browser(self, url):
        self.opened.append(url)
        if self.browser_error:
            raise self.browser_error


def make_launcher(tmp_path, fakes: Fakes, **kw):
    out, err = io.StringIO(), io.StringIO()
    launcher = L.Launcher(
        data_dir=tmp_path / "data", scripts_dir=SCRIPTS_DIR, entrypoint=ENTRYPOINT,
        out=out, err=err, probe=fakes.probe, create_time=fakes.create_time,
        shutdown=fakes.shutdown, browser=fakes.browser,
        kill=lambda s: fakes.killed.append(s.pid), sleep=lambda _s: None,
        monotonic=iter(range(0, 10_000)).__next__, **kw)
    return launcher, out, err


def healthy(state, *, code=HASH, busy=0, nonce=None):
    return {"schema": 1, "instance": nonce or state.nonce, "code_hash": code, "port": state.port, "busy": busy}


def test_no_record_means_start(tmp_path):
    launcher, _o, _e = make_launcher(tmp_path, Fakes())
    assert launcher.decide(launcher.observe(), HASH) == L.START


def test_matching_process_nonce_and_code_is_reused(tmp_path):
    fakes = Fakes()
    launcher, _o, _e = make_launcher(tmp_path, fakes)
    state = make_state(launcher.root)
    fakes.create_times[state.pid] = state.pid_create_time + 0.4
    fakes.health = healthy(state)
    assert launcher.decide(launcher.observe(), HASH) == L.REUSE


def test_changed_code_restarts(tmp_path):
    fakes = Fakes()
    launcher, _o, _e = make_launcher(tmp_path, fakes)
    state = make_state(launcher.root)
    fakes.create_times[state.pid] = state.pid_create_time
    fakes.health = healthy(state, code="b" * 64)
    assert launcher.decide(launcher.observe(), HASH) == L.RESTART_CODE


def test_a_dead_process_with_a_silent_port_is_a_stale_record(tmp_path):
    fakes = Fakes()
    launcher, _o, _e = make_launcher(tmp_path, fakes)
    make_state(launcher.root)
    assert launcher.decide(launcher.observe(), HASH) == L.START


def test_a_reused_pid_is_not_the_recorded_server(tmp_path):
    fakes = Fakes()
    launcher, _o, _e = make_launcher(tmp_path, fakes)
    state = make_state(launcher.root)
    fakes.create_times[state.pid] = state.pid_create_time + 3600  # another process, same pid
    assert launcher.decide(launcher.observe(), HASH) == L.START


def test_a_live_process_that_does_not_answer_is_reported_not_replaced(tmp_path):
    fakes = Fakes()
    launcher, _o, _e = make_launcher(tmp_path, fakes)
    state = make_state(launcher.root)
    fakes.create_times[state.pid] = state.pid_create_time
    with pytest.raises(L.LauncherError, match=r"not answering.*stop --force"):
        launcher.decide(launcher.observe(), HASH)


def test_another_instance_on_the_port_is_never_touched(tmp_path):
    fakes = Fakes()
    launcher, _o, _e = make_launcher(tmp_path, fakes)
    state = make_state(launcher.root)
    fakes.create_times[state.pid] = state.pid_create_time
    fakes.health = healthy(state, nonce="someone-else")
    with pytest.raises(L.LauncherError, match="different dashboard instance"):
        launcher.decide(launcher.observe(), HASH)
    with pytest.raises(L.LauncherError):
        launcher.stop(force=True)
    assert not fakes.shutdowns and not fakes.killed


def test_a_stranger_answering_on_the_recorded_port_is_never_stopped(tmp_path):
    """The recorded process is gone but something answers: no port-owner guessing."""
    fakes = Fakes()
    launcher, _o, _e = make_launcher(tmp_path, fakes)
    state = make_state(launcher.root)
    fakes.health = healthy(state)
    with pytest.raises(L.LauncherError, match="not ours to stop"):
        launcher.decide(launcher.observe(), HASH)
    with pytest.raises(L.LauncherError, match="not ours to stop"):
        launcher.launch(open_url=False)
    assert not fakes.spawned and not fakes.killed and not fakes.shutdowns


@pytest.mark.parametrize("content", ["{not json", "[]", "", json.dumps({"schema": 1}),
                                     json.dumps({"schema": 2, "pid": 1, "pid_create_time": 1.0, "nonce": "n",
                                                 "port": 1, "token": "t", "code_hash": "h",
                                                 "started_at": "s", "log_path": "l"})])
def test_a_corrupt_record_is_an_error_naming_the_file_and_starts_nothing(tmp_path, content):
    """RED CHECK: corrupt state is never read as 'no server'."""
    fakes = Fakes()
    launcher, _o, _e = make_launcher(tmp_path, fakes)
    launcher.root.mkdir(parents=True)
    (launcher.root / "server.json").write_text(content, encoding="utf-8")
    for action in (lambda: launcher.launch(open_url=False), launcher.status,
                   lambda: launcher.stop(force=True)):
        with pytest.raises(L.LauncherError) as excinfo:
            action()
        assert str(launcher.root / "server.json") in str(excinfo.value)
    assert not fakes.spawned and not fakes.killed and not fakes.shutdowns


def test_the_corrupt_record_is_left_on_disk(tmp_path):
    fakes = Fakes()
    launcher, _o, _e = make_launcher(tmp_path, fakes)
    launcher.root.mkdir(parents=True)
    (launcher.root / "server.json").write_text("garbage", encoding="utf-8")
    with pytest.raises(L.LauncherError):
        launcher.launch(open_url=False)
    assert (launcher.root / "server.json").read_text(encoding="utf-8") == "garbage"


# -- launch / restart / stop with a fake server ---------------------------------


def test_launch_reuses_a_healthy_current_server_and_opens_the_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "code_hash", lambda _d: HASH)
    fakes = Fakes()
    launcher, out, _e = make_launcher(tmp_path, fakes)
    state = make_state(launcher.root)
    fakes.create_times[state.pid] = state.pid_create_time
    fakes.health = healthy(state)
    assert launcher.launch(open_url=True) == L.EXIT_OK
    assert "already running" in out.getvalue() and state.url() in out.getvalue()
    assert fakes.opened == [state.url()]
    assert not fakes.spawned and not fakes.shutdowns


def test_launch_without_opening_prints_the_url_only(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "code_hash", lambda _d: HASH)
    fakes = Fakes()
    launcher, out, _e = make_launcher(tmp_path, fakes)
    state = make_state(launcher.root)
    fakes.create_times[state.pid] = state.pid_create_time
    fakes.health = healthy(state)
    assert launcher.launch(open_url=False) == L.EXIT_OK
    assert state.url() in out.getvalue() and not fakes.opened


def test_code_change_restarts_an_idle_server(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "code_hash", lambda _d: HASH)
    fakes = Fakes()
    launcher, out, _e = make_launcher(tmp_path, fakes)
    old = make_state(launcher.root)
    fakes.create_times[old.pid] = old.pid_create_time
    fakes.health = healthy(old, code="b" * 64)
    new = dstate.ServerState(**{**old.__dict__, "pid": 5555, "nonce": "nonce-2", "port": 40002})

    def gone():
        fakes.create_times.pop(old.pid)
        fakes.health = None
        launcher.root.joinpath("server.json").unlink()

    class FakeProc:
        def poll(self):
            return None

    def spawn(entry, data, log):
        fakes.spawned.append(entry)
        dstate.write_state(launcher.root, new)
        fakes.create_times[new.pid] = new.pid_create_time
        fakes.health = healthy(new)
        return FakeProc()

    fakes.on_shutdown = gone
    launcher._spawn = spawn
    assert launcher.launch(open_url=False) == L.EXIT_OK
    assert fakes.shutdowns == [False] and len(fakes.spawned) == 1
    assert "restarting" in out.getvalue() and new.url() in out.getvalue()


def test_code_change_while_jobs_run_keeps_the_server_and_says_so(tmp_path, monkeypatch):
    """The refusal hook: no restart (and no spawn) while the server reports busy jobs."""
    monkeypatch.setattr(L, "code_hash", lambda _d: HASH)
    fakes = Fakes()
    launcher, out, err = make_launcher(tmp_path, fakes)
    old = make_state(launcher.root)
    fakes.create_times[old.pid] = old.pid_create_time
    fakes.health = healthy(old, code="b" * 64, busy=3)
    assert launcher.launch(open_url=False) == L.EXIT_OK
    assert not fakes.shutdowns and not fakes.spawned
    assert "3 job(s) are running" in err.getvalue() and "already running" in out.getvalue()


def test_stop_and_restart_are_refused_while_jobs_run_unless_forced(tmp_path):
    fakes = Fakes()
    launcher, _o, _e = make_launcher(tmp_path, fakes)
    state = make_state(launcher.root)
    fakes.create_times[state.pid] = state.pid_create_time
    fakes.health = healthy(state, busy=2)
    with pytest.raises(L.BusyError, match="2 job"):
        launcher.stop(force=False)
    with pytest.raises(L.BusyError):
        launcher.restart(force=False, open_url=False)
    assert not fakes.shutdowns
    fakes.on_shutdown = lambda: fakes.create_times.pop(state.pid)
    assert launcher.stop(force=True) == L.EXIT_OK
    assert fakes.shutdowns == [True]


def test_stop_reports_a_server_that_will_not_exit(tmp_path):
    fakes = Fakes()
    launcher, _o, _e = make_launcher(tmp_path, fakes)
    state = make_state(launcher.root)
    fakes.create_times[state.pid] = state.pid_create_time
    fakes.health = healthy(state)
    with pytest.raises(L.LauncherError, match="did not exit.*stop --force"):
        launcher.stop(force=False)
    assert launcher.stop(force=True) == L.EXIT_OK and fakes.killed == [state.pid]


def test_force_stop_kills_an_unresponsive_server_after_identity_check(tmp_path):
    fakes = Fakes()
    launcher, out, _e = make_launcher(tmp_path, fakes)
    state = make_state(launcher.root)
    fakes.create_times[state.pid] = state.pid_create_time
    with pytest.raises(L.LauncherError, match="not answering"):
        launcher.stop(force=False)
    assert launcher.stop(force=True) == L.EXIT_OK
    assert fakes.killed == [state.pid] and "forced" in out.getvalue()


def test_status_exit_codes(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "code_hash", lambda _d: HASH)
    fakes = Fakes()
    launcher, out, _e = make_launcher(tmp_path, fakes)
    assert launcher.status() == L.EXIT_NOT_RUNNING
    state = make_state(launcher.root)
    assert launcher.status() == L.EXIT_NOT_RUNNING and "stale record" in out.getvalue()
    fakes.create_times[state.pid] = state.pid_create_time
    fakes.health = healthy(state)
    assert launcher.status() == L.EXIT_OK
    assert "code: current" in out.getvalue()
    fakes.health = healthy(state, code="b" * 64)
    launcher.status()
    assert "OUT OF DATE" in out.getvalue()


def test_a_browser_failure_still_reports_the_url_and_a_distinct_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "code_hash", lambda _d: HASH)
    fakes = Fakes()
    fakes.browser_error = L.BrowserOpenError("exit", "xdg-open exited 3")
    launcher, out, err = make_launcher(tmp_path, fakes)
    state = make_state(launcher.root)
    fakes.create_times[state.pid] = state.pid_create_time
    fakes.health = healthy(state)
    assert launcher.launch(open_url=True) == L.EXIT_BROWSER
    assert state.url() in out.getvalue() and "could not open the browser (exit)" in err.getvalue()


def test_a_server_that_exits_while_starting_is_reported_with_its_log(tmp_path):
    fakes = Fakes()
    launcher, _o, _e = make_launcher(tmp_path, fakes)

    class Dead:
        def poll(self):
            return 1

    launcher._spawn = lambda *a: Dead()
    with pytest.raises(L.LauncherError, match=r"exited with code 1.*server\.log"):
        launcher.launch(open_url=False)


def test_a_server_that_never_becomes_ready_times_out_with_its_log(tmp_path):
    fakes = Fakes()
    launcher, _o, _e = make_launcher(tmp_path, fakes, ready_timeout=3)

    class Alive:
        def poll(self):
            return None

    launcher._spawn = lambda *a: Alive()
    with pytest.raises(L.LauncherError, match=r"did not become ready.*server\.log"):
        launcher.launch(open_url=False)


# ---------------------------------------------------------------------------
# browser opening
# ---------------------------------------------------------------------------


def _completed(code=0, err=""):
    return subprocess.CompletedProcess([], code, "", err)


def test_windows_opens_with_startfile_and_reports_a_spawn_error():
    opened = []
    L.open_browser("http://x/", platform="win32", startfile=opened.append)
    assert opened == ["http://x/"]

    def boom(_url):
        raise OSError("no handler")

    with pytest.raises(L.BrowserOpenError) as excinfo:
        L.open_browser("http://x/", platform="win32", startfile=boom)
    assert excinfo.value.kind == "spawn"


@pytest.mark.parametrize("platform,command", [("darwin", "open"), ("linux", "xdg-open")])
def test_posix_platforms_use_their_opener(platform, command):
    seen = []
    L.open_browser("http://x/", platform=platform,
                   run=lambda cmd, **kw: seen.append((cmd, kw["timeout"])) or _completed())
    assert seen[0][0] == [command, "http://x/"] and seen[0][1] > 0


def test_posix_open_failures_are_three_distinct_kinds():
    def missing(cmd, **kw):
        raise FileNotFoundError("xdg-open")

    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    kinds = []
    for run in (missing, hang, lambda cmd, **kw: _completed(3, "no browser")):
        with pytest.raises(L.BrowserOpenError) as excinfo:
            L.open_browser("http://x/", platform="linux", run=run)
        kinds.append(excinfo.value.kind)
    assert kinds == ["spawn", "timeout", "exit"]


def test_no_open_flag_and_environment_variable_suppress_the_browser(tmp_path, monkeypatch, outside_a_run):
    opened = []

    class StubLauncher:
        def __init__(self, **kw):
            pass

        def launch(self, *, open_url):
            opened.append(open_url)
            return 0

    monkeypatch.setattr(L, "Launcher", StubLauncher)
    kw = dict(scripts_dir=SCRIPTS_DIR, entrypoint=ENTRYPOINT)
    data = ["--data-dir", str(tmp_path)]
    monkeypatch.delenv(L.NO_OPEN_ENV, raising=False)
    L.main(data, **kw)
    L.main(data + ["--no-open"], **kw)
    monkeypatch.setenv(L.NO_OPEN_ENV, "1")
    L.main(data, **kw)
    assert opened == [True, False, False]


def test_spawn_uses_a_hidden_window_on_windows_and_a_new_session_elsewhere(tmp_path, monkeypatch):
    captured = {}

    class FakePopen:
        def __init__(self, command, **kwargs):
            captured.update(command=command, kwargs=kwargs)

    monkeypatch.setattr(L.subprocess, "Popen", FakePopen)
    L.spawn_server(ENTRYPOINT, tmp_path / "d", tmp_path / "d" / "dashboard" / "server.log")
    kwargs = captured["kwargs"]
    assert captured["command"][1:] == [str(ENTRYPOINT), "serve", "--data-dir", str(tmp_path / "d")]
    assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["stdout"] is kwargs["stderr"]
    if os.name == "nt":
        flags = kwargs["creationflags"]
        assert flags & subprocess.CREATE_NO_WINDOW
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
        # DETACHED_PROCESS would make CREATE_NO_WINDOW a no-op.
        assert not flags & 0x00000008
    else:
        assert kwargs["start_new_session"] is True


# ---------------------------------------------------------------------------
# data directory resolution (never the production state in a redirected session)
# ---------------------------------------------------------------------------


def _venv(root: Path, marketplace="plugins-kit") -> Path:
    venv = root / marketplace / "task-kit" / ".venv"
    venv.mkdir(parents=True)
    return venv


def test_data_dir_is_the_venvs_parent_with_the_marketplace_from_the_path(tmp_path):
    venv = _venv(tmp_path / "data", marketplace="some-marketplace")
    assert dstate.resolve_data_dir({}, str(venv)) == venv.parent.resolve()


def test_a_venv_outside_the_redirected_data_root_is_refused(tmp_path):
    """RED CHECK: a claudx session never resolves the production dashboard state."""
    production = _venv(tmp_path / "production")
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    with pytest.raises(dstate.DashboardStateError, match="refusing"):
        dstate.resolve_data_dir({dstate.DATA_ROOT_ENV: str(redirected)}, str(production))
    inside = _venv(redirected)
    assert dstate.resolve_data_dir({dstate.DATA_ROOT_ENV: str(redirected)}, str(inside)) == inside.parent.resolve()


def test_a_non_plugin_interpreter_cannot_resolve_a_data_dir(tmp_path):
    with pytest.raises(dstate.DashboardStateError, match="provisioned venv"):
        dstate.resolve_data_dir({}, str(tmp_path))


# ---------------------------------------------------------------------------
# the entrypoint: plugin layout and the thin data-dir bundle
# ---------------------------------------------------------------------------


def load_entrypoint():
    import importlib.util

    spec = importlib.util.spec_from_file_location("task_kit_entrypoint_under_test", ENTRYPOINT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_installed(cache_root: Path, version: str, marker: str) -> Path:
    root = cache_root / version
    (root / "bin").mkdir(parents=True)
    (root / "skills" / "task" / "scripts" / "task_system" / "dashboard").mkdir(parents=True)
    (root / "bin" / "task_kit_entrypoint.py").write_text(
        f"import sys\nprint('ran {marker}', sys.argv[1:])\n", encoding="utf-8")
    return root


def bundle_layout(tmp_path: Path) -> tuple[Path, Path]:
    """<plugins>/data/plugins-kit/task-kit/bin + <plugins>/cache/plugins-kit/task-kit/<versions>."""
    plugins = tmp_path / "home" / ".claude" / "plugins"
    bundle = plugins / "data" / "plugins-kit" / "task-kit"
    (bundle / "bin").mkdir(parents=True)
    shutil.copy2(ENTRYPOINT, bundle / "bin" / ENTRYPOINT.name)
    return bundle, plugins / "cache" / "plugins-kit" / "task-kit"


def test_bundle_locates_the_newest_numeric_version_in_the_cache(tmp_path):
    bundle, cache = bundle_layout(tmp_path)
    for version in ("0.2.0", "0.10.0", "0.9.0"):
        fake_installed(cache, version, version)
    (cache / "not-a-version").mkdir()
    module = load_entrypoint()
    assert module.installed_plugin_root(bundle) == cache / "0.10.0"
    result = subprocess.run([sys.executable, str(bundle / "bin" / ENTRYPOINT.name), "status"],
                            capture_output=True, text=True)
    assert result.returncode == 0 and "ran 0.10.0 ['status']" in result.stdout


def test_bundle_skips_cache_versions_without_the_dashboard(tmp_path):
    bundle, cache = bundle_layout(tmp_path)
    fake_installed(cache, "0.2.0", "has-dashboard")
    old = cache / "0.1.0"
    (old / "bin").mkdir(parents=True)
    (old / "bin" / "task_kit_entrypoint.py").write_text("print('old')\n", encoding="utf-8")
    fake_installed(cache, "0.0.9", "older")
    # 0.3.0 exists but has no dashboard package: it is not an installable launch target.
    (cache / "0.3.0" / "bin").mkdir(parents=True)
    (cache / "0.3.0" / "bin" / "task_kit_entrypoint.py").write_text("print('no dash')\n", encoding="utf-8")
    assert load_entrypoint().installed_plugin_root(bundle) == cache / "0.2.0"


def test_bundle_without_an_installed_version_fails_loudly(tmp_path):
    bundle, cache = bundle_layout(tmp_path)
    result = subprocess.run([sys.executable, str(bundle / "bin" / ENTRYPOINT.name)],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "no installed task-kit" in result.stderr and str(cache) in result.stderr


# ---------------------------------------------------------------------------
# real servers
# ---------------------------------------------------------------------------


def _env(extra=None):
    env = clean_env()  # a worker running these tests inherits the run markers; the launcher under test must not
    env["_BOOTSTRAP_GUARD_SKIP_REEXEC"] = "1"
    env["PYTHONPATH"] = str(REPO_ROOT / "plugins" / "skills-kit")
    for name in ("CLAUDE_BOOTSTRAP_DATA_ROOT", "CLAUDE_PLUGIN_TEST", "TASK_KIT_NO_OPEN"):
        env.pop(name, None)
    env.update(extra or {})
    return env


class Plugin:
    """A COPY of the plugin's launcher and code, so a test can change the code."""

    def __init__(self, tmp_path: Path, devroot: Path):
        self.root = tmp_path / "plugin"
        (self.root / "bin").mkdir(parents=True)
        shutil.copy2(ENTRYPOINT, self.root / "bin" / ENTRYPOINT.name)
        shutil.copytree(SCRIPTS_DIR, self.root / "skills" / "task" / "scripts",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.data = tmp_path / "data"
        self.devroot = devroot
        self.entry = self.root / "bin" / ENTRYPOINT.name
        self.runtime = self.data / "dashboard"

    def run(self, *args, timeout=90):
        return subprocess.run(
            [sys.executable, str(self.entry), "--data-dir", str(self.data), *args],
            capture_output=True, text=True, timeout=timeout, env=_env({"DEVROOT": str(self.devroot)}))

    def state(self) -> dstate.ServerState | None:
        return dstate.read_state(self.runtime)

    def cleanup(self):
        self.run("stop", "--force")


@pytest.fixture
def plugin(tmp_path):
    devroot = tmp_path / "devroot"
    (devroot / "alpha").mkdir(parents=True)
    make_task(devroot / "alpha", "dev/tasks", "one", title="One",
              items=[{"id": "an-item", "title": "An item", "state": "available"}])
    p = Plugin(tmp_path, devroot)
    try:
        yield p
    finally:
        p.cleanup()


def test_launch_reuse_status_and_stop_against_a_real_server(plugin):
    first = plugin.run("--no-open")
    assert first.returncode == 0, first.stderr
    assert "dashboard started" in first.stdout
    state = plugin.state()
    assert state and f":{state.port}/#{state.token}" in first.stdout
    assert psutil.Process(state.pid).is_running()
    assert (plugin.runtime / "server.log").exists()

    second = plugin.run("--no-open")
    assert second.returncode == 0 and "already running" in second.stdout
    assert plugin.state().pid == state.pid

    status = plugin.run("status")
    assert status.returncode == 0 and "code: current" in status.stdout

    stopped = plugin.run("stop")
    assert stopped.returncode == 0 and "stopped" in stopped.stdout
    assert plugin.state() is None
    assert plugin.run("status").returncode == L.EXIT_NOT_RUNNING
    assert plugin.run("stop").returncode == 0  # idempotent


def test_changing_the_server_code_restarts_it_on_the_next_launch(plugin):
    assert plugin.run("--no-open").returncode == 0
    old = plugin.state()
    assert old.code_hash == code_hash(plugin.root / "skills" / "task" / "scripts")
    target = plugin.root / "skills" / "task" / "scripts" / "task_system" / "dashboard" / "static" / "app.css"
    target.write_text(target.read_text(encoding="utf-8") + "\n.changed { color: red; }\n", encoding="utf-8")
    result = plugin.run("--no-open")
    assert result.returncode == 0, result.stderr
    assert "code changed; restarting" in result.stdout
    new = plugin.state()
    assert new.pid != old.pid and new.code_hash != old.code_hash
    deadline = time.time() + 10
    while psutil.pid_exists(old.pid) and time.time() < deadline:
        time.sleep(0.1)
    assert not psutil.pid_exists(old.pid) or not L.identity_matches(old, L.process_create_time(old.pid))


def test_a_killed_server_leaves_a_stale_record_that_the_next_launch_replaces(plugin):
    assert plugin.run("--no-open").returncode == 0
    dead = plugin.state()
    psutil.Process(dead.pid).kill()
    deadline = time.time() + 10
    while psutil.pid_exists(dead.pid) and time.time() < deadline:
        time.sleep(0.1)
    assert plugin.state() == dead  # kill left the record behind
    status = plugin.run("status")
    assert status.returncode == L.EXIT_NOT_RUNNING and "stale record" in status.stdout
    again = plugin.run("--no-open")
    assert again.returncode == 0 and "dashboard started" in again.stdout
    assert plugin.state().pid != dead.pid


def test_a_corrupt_record_stops_the_real_launcher_without_starting_a_server(plugin):
    plugin.runtime.mkdir(parents=True)
    (plugin.runtime / "server.json").write_text("{broken", encoding="utf-8")
    result = plugin.run("--no-open")
    assert result.returncode == L.EXIT_ERROR
    assert str(plugin.runtime / "server.json") in result.stderr
    assert not (plugin.runtime / "server.log").exists()
    assert (plugin.runtime / "server.json").read_text(encoding="utf-8") == "{broken"


def test_two_launches_at_once_end_with_one_server(plugin):
    procs = [subprocess.Popen(
        [sys.executable, str(plugin.entry), "--data-dir", str(plugin.data), "--no-open"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=_env({"DEVROOT": str(plugin.devroot)})) for _ in range(2)]
    results = [p.communicate(timeout=90) + (p.returncode,) for p in procs]
    assert [r[2] for r in results] == [0, 0], results
    state = plugin.state()
    servers = [p for p in psutil.process_iter(["pid", "cmdline"])
               if p.info["cmdline"] and "serve" in p.info["cmdline"] and str(plugin.data) in p.info["cmdline"]
               and "--data-dir" in p.info["cmdline"]]
    # A trampolined interpreter shows up twice (launcher stub and real process); count distinct recorded pids.
    assert state.pid in [p.info["pid"] for p in servers]
    for out, _err, _rc in results:
        assert f":{state.port}/#{state.token}" in out


def test_the_thin_bundle_runs_the_installed_copys_real_launcher(plugin, tmp_path):
    """End to end: data-dir bundle -> cache scan -> the cache copy's launcher -> a real server."""
    plugins = tmp_path / "home" / ".claude" / "plugins"
    cache_copy = plugins / "cache" / "plugins-kit" / "task-kit" / "0.2.0"
    shutil.copytree(plugin.root, cache_copy)
    bundle = plugins / "data" / "plugins-kit" / "task-kit"
    (bundle / "bin").mkdir(parents=True)
    shutil.copy2(ENTRYPOINT, bundle / "bin" / ENTRYPOINT.name)
    data = tmp_path / "bundle-data"
    run = lambda *a: subprocess.run(  # noqa: E731
        [sys.executable, str(bundle / "bin" / ENTRYPOINT.name), "--data-dir", str(data), *a],
        capture_output=True, text=True, timeout=90, env=_env({"DEVROOT": str(plugin.devroot)}))
    try:
        started = run("--no-open")
        assert started.returncode == 0, started.stderr
        state = dstate.read_state(data / "dashboard")
        assert state.code_hash == code_hash(cache_copy / "skills" / "task" / "scripts")
    finally:
        run("stop", "--force")


def test_the_launcher_refuses_inside_a_dashboard_run_with_exit_six_and_says_so_in_its_help(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TASK_KIT_AGENT_DEPTH", "0")
    assert L.EXIT_NESTED == 6
    for command in ([], ["status"], ["stop"], ["restart"], ["serve"]):
        assert L.main([*command, "--data-dir", str(tmp_path)], scripts_dir=SCRIPTS_DIR, entrypoint=ENTRYPOINT) == 6
    assert "refused inside a dashboard run" in capsys.readouterr().err
    assert not (tmp_path / "dashboard").exists()  # refused before anything was read or created
    assert "6 refused inside a dashboard run" in L.build_parser().format_help().replace("\n", " ").replace("  ", " ")
