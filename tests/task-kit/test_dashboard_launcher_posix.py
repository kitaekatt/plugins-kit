"""The POSIX launch chain, run live: the shim's interpreter chain under every
distinct bash, browser opening end to end, and the detached server's survival.

POSIX only. Windows has its own shim (``task-kit.cmd``) and its own spawn flags,
covered by ``test_dashboard_launcher_windows.py`` with fakes.

The interpreter chain runs the real shim, copied into a throwaway plugin tree
whose entrypoint is a marker, under ``/bin/bash`` (GNU bash 3.2 on a stock Mac)
and under the first ``bash`` on PATH, de-duplicated by realpath. The
BOOTSTRAP_PYTHON validation cases live in the repo guard
(``tests/repo-scripts/test_launcher_shim_data_root_redirect.py``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from dashboard_support import PLUGIN_ROOT, clean_env

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX shim and POSIX process semantics")

_SCRUBBED = (
    "CLAUDE_BOOTSTRAP_DATA_ROOT", "CLAUDE_PLUGIN_TEST", "CLAUDE_PLUGIN_TEST_MARKETPLACE",
    "CLAUDE_PLUGIN_TEST_DIRS", "BOOTSTRAP_PYTHON", "BOOTSTRAP_PROJECT_PYTHON",
    "TASK_KIT_AGENT_DEPTH", "TASK_KIT_RUN_TOKEN",
)
_TOOLS = ("dirname", "uname", "tr", "readlink")


def distinct_bashes() -> list[str]:
    """``/bin/bash`` and the first bash on PATH, one entry per real file."""
    found: dict[str, str] = {}
    for candidate in ("/bin/bash", shutil.which("bash")):
        if candidate and Path(candidate).exists():
            found.setdefault(os.path.realpath(candidate), candidate)
    return list(found.values())


def _bash_id(path: str) -> str:
    out = subprocess.run([path, "-c", 'printf %s "${BASH_VERSION}"'],
                         capture_output=True, text=True, timeout=30).stdout
    return f"bash-{out.split('(')[0]}"


BASHES = distinct_bashes()


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def tools_dir(root: Path) -> Path:
    """A directory linking only the utilities the shim calls, so a test controls PATH exactly."""
    tools = root / "tools"
    tools.mkdir(exist_ok=True)
    for name in _TOOLS:
        found = shutil.which(name)
        assert found, f"{name} not found on PATH"
        if not (tools / name).exists():
            (tools / name).symlink_to(found)
    return tools


def shim_env(home: Path, path: str) -> dict[str, str]:
    env = clean_env()
    for name in _SCRUBBED:
        env.pop(name, None)
    env["HOME"] = str(home)
    env["PATH"] = path
    return env


class ShimTree:
    """The real shim in a throwaway plugin tree; its entrypoint prints a marker."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "plugin"
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.shim = self.root / "bin" / "task-kit"
        self.shim.parent.mkdir(parents=True)
        shutil.copy2(PLUGIN_ROOT / "bin" / "task-kit", self.shim)
        (self.root / "bin" / "task_kit_entrypoint.py").write_text(
            "import sys\nprint('ENTRYPOINT', sys.prefix != sys.base_prefix)\n", encoding="utf-8")
        self.tools = tools_dir(tmp_path)
        self.venv_python = (self.home / ".claude" / "plugins" / "data" / "plugins-kit"
                            / "task-kit" / ".venv" / "bin" / "python")

    def run(self, bash: str, path: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([bash, str(self.shim), *args], capture_output=True, text=True, timeout=60,
                              env=shim_env(self.home, path))


@pytest.mark.parametrize("bash", BASHES, ids=[_bash_id(b) for b in BASHES])
def test_the_production_venv_is_chosen_first(tmp_path, bash):
    """(a) The plugin venv outranks everything on PATH."""
    tree = ShimTree(tmp_path)
    _script(tree.venv_python, "echo VENV \"$@\"")
    _script(tmp_path / "fakebin" / "python3", "echo PATHPY")
    result = tree.run(bash, f"{tmp_path / 'fakebin'}:{tree.tools}", "status")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith("VENV "), result.stdout
    assert result.stdout.strip().endswith("task_kit_entrypoint.py status")


@pytest.mark.parametrize("bash", BASHES, ids=[_bash_id(b) for b in BASHES])
def test_with_no_interpreter_anywhere_the_shim_exits_one_and_says_so(tmp_path, bash):
    """(e) No venv, no BOOTSTRAP_PYTHON, no python on PATH: a loud exit 1.

    Shown to fail: change that branch's ``exit 1`` to ``exit 0``.
    """
    tree = ShimTree(tmp_path)
    result = tree.run(bash, str(tree.tools))
    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    assert "no Python interpreter found" in result.stderr


@pytest.mark.parametrize("bash", BASHES, ids=[_bash_id(b) for b in BASHES])
def test_a_python_on_path_runs_the_entrypoint_when_there_is_no_venv(tmp_path, bash):
    """The last link of the chain: ``python3`` on PATH."""
    tree = ShimTree(tmp_path)
    _script(tmp_path / "fakebin" / "python3", "echo PATHPY")
    result = tree.run(bash, f"{tmp_path / 'fakebin'}:{tree.tools}")
    assert result.returncode == 0 and result.stdout.strip() == "PATHPY", (result.stdout, result.stderr)


# ---------------------------------------------------------------------------
# the real launcher: browser opening end to end, and the detached server
# ---------------------------------------------------------------------------

import signal  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402

import psutil  # noqa: E402
from test_task_kit_launcher import Plugin, _env  # noqa: E402

from task_system.dashboard import launcher as L  # noqa: E402
from task_system.dashboard import proctree  # noqa: E402
from task_system.dashboard import state as dstate  # noqa: E402

OPENER = "open" if sys.platform == "darwin" else "xdg-open"


class LiveLauncher(Plugin):
    """The real entrypoint over a copy of the plugin, with a PATH the test controls."""

    def __init__(self, tmp_path: Path) -> None:
        devroot = tmp_path / "devroot"
        (devroot / "alpha").mkdir(parents=True)
        super().__init__(tmp_path, devroot)
        self.fakebin = tmp_path / "fakebin"
        self.fakebin.mkdir()
        self.tools = tmp_path / "launcher-tools"
        self.tools.mkdir()
        for name in ("git", "sleep"):
            found = shutil.which(name)
            assert found, f"{name} not found on PATH"
            (self.tools / name).symlink_to(found)

    def opener(self, body: str) -> Path:
        return _script(self.fakebin / OPENER, body)

    def launch(self, *args: str, timeout: int = 90) -> subprocess.CompletedProcess:
        env = _env({"DEVROOT": str(self.devroot)})
        env["PATH"] = f"{self.fakebin}:{self.tools}"
        return subprocess.run([sys.executable, str(self.entry), "--data-dir", str(self.data), *args],
                              capture_output=True, text=True, timeout=timeout, env=env)

    def cleanup(self) -> None:  # noqa: D401
        self.launch("stop", "--force")


@pytest.fixture
def live(tmp_path):
    launcher = LiveLauncher(tmp_path)
    try:
        yield launcher
    finally:
        launcher.cleanup()


def test_with_no_opener_the_launcher_exits_four_and_still_prints_the_url(live):
    """Case A. Red: make ``launch()`` swallow BrowserOpenError; the exit is 0 and this fails."""
    result = live.launch()
    assert result.returncode == L.EXIT_BROWSER, (result.stdout, result.stderr)
    assert "(spawn)" in result.stderr
    state = live.state()
    assert state is not None and f":{state.port}/#{state.token}" in result.stdout
    assert live.launch("status").returncode == 0


def test_an_opener_that_fails_has_its_reason_in_the_launchers_stderr(live):
    """Case B."""
    live.opener('echo "no display" >&2\nexit 3')
    result = live.launch()
    assert result.returncode == L.EXIT_BROWSER, (result.stdout, result.stderr)
    assert "no display" in result.stderr and "(exit)" in result.stderr


def test_an_opener_that_never_returns_follows_the_platforms_rule(live, tmp_path):
    """Case C. macOS judges ``open`` by its exit status, so a hang is a timeout (exit 4). Linux's
    xdg-open may be the browser itself: still running at the deadline is accepted (exit 0), left alive."""
    pidfile = tmp_path / "opener.pid"
    live.opener(f'echo $$ > "{pidfile}"\nexec sleep 60')
    started = time.monotonic()
    try:
        result = live.launch()
        elapsed = time.monotonic() - started
        if sys.platform == "darwin":
            assert result.returncode == L.EXIT_BROWSER and "(timeout)" in result.stderr, (result.stdout, result.stderr)
        else:
            assert result.returncode == 0, (result.stdout, result.stderr)
            assert "may be the browser itself" in result.stdout
            assert psutil.pid_exists(int(pidfile.read_text()))  # not killed
        assert elapsed < 40
    finally:
        if pidfile.exists():
            try:
                psutil.Process(int(pidfile.read_text())).kill()
            except psutil.NoSuchProcess:
                pass


def test_the_detached_server_survives_a_kill_of_the_launchers_process_group(live):
    """H6. The launcher leads group G (as the shell that ran ``/task dashboard`` does); the server must not be in it.

    Red: drop ``start_new_session`` from the POSIX spawn. The server is then in G, killpg kills it, and
    ``status`` no longer answers 0.
    """
    env = _env({"DEVROOT": str(live.devroot)})
    env["PATH"] = f"{live.fakebin}:{live.tools}"
    leader = subprocess.Popen([sys.executable, str(live.entry), "--data-dir", str(live.data), "--no-open"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                              start_new_session=True)
    assert leader.wait(timeout=90) == 0, leader.stderr.read()
    recorded = live.state()
    assert recorded is not None
    try:
        os.killpg(leader.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # nothing of the group remains: the expected outcome
    ident = proctree.ident_of(recorded.pid)
    assert ident is not None
    deadline = time.monotonic() + 3  # let a (wrongly) signalled server finish dying before judging
    while time.monotonic() < deadline and proctree.live(ident) is not None:
        time.sleep(0.1)
    status = live.launch("status")
    assert status.returncode == 0, (status.stdout, status.stderr)
    assert "code: current" in status.stdout
