"""The `bootstrap` CLI lever (plugins/bootstrap/scripts/bootstrap_cli.py).

Two contracts, and they are the reason this lever exists at all:

1. Asking "is a pass running?" must be READ-ONLY. A status probe that
   acquired the lock -- even briefly -- would clear a stale one and could make
   a genuine launcher stand down, so the probe reads the lock and never
   touches it.
2. `run` applies only user/project layers. It must refuse a running pass,
   whose manifest scope may include plugins or another project.
"""

import hashlib
import importlib.util
import json
import os
import sys
import threading
import time

import pytest

from bootstrap_lib import proc_lock
from bootstrap_lib.records import EVENTS_FILENAME, WATCH_FILENAME, PassRecorder

SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "plugins", "bootstrap", "scripts",
)


def _load_cli():
    """Load the lever by path -- its dir is deliberately not on pythonpath."""
    spec = importlib.util.spec_from_file_location(
        "bootstrap_cli", os.path.join(SCRIPTS, "bootstrap_cli.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


# --------------------------------------------------------------------------
# lock_holder: the read-only probe
# --------------------------------------------------------------------------

class TestLockHolder:

    def test_no_lock_file_reads_as_not_running(self, tmp_path):
        assert proc_lock.lock_holder(str(tmp_path)) is None

    def test_live_holder_is_reported_without_disturbing_the_lock(self, tmp_path):
        data_dir = str(tmp_path)
        with proc_lock.engine_lock(data_dir) as acquired:
            assert acquired
            info = proc_lock.lock_holder(data_dir)
            assert info is not None
            assert info["pid"] == os.getpid()
            # The probe must leave the lock exactly as it found it: a second
            # launcher still has to stand down afterwards.
            with proc_lock.engine_lock(data_dir) as second:
                assert second is False
            assert proc_lock.lock_holder(data_dir)["pid"] == os.getpid()

    def test_dead_holder_reads_as_not_running(self, tmp_path):
        lock = tmp_path / proc_lock.LOCK_FILENAME
        lock.write_text("%d\n%f\n" % (0x7FFFFFFF, time.time()))
        # A PID that cannot be alive must not wedge the CLI into reporting a
        # phantom pass -- the same staleness rule _try_acquire applies.
        assert proc_lock.lock_holder(str(tmp_path)) is None

    def test_live_pid_with_aged_lock_reads_as_not_running(self, tmp_path):
        """PID reuse: a recycled number must not wedge the report forever."""
        lock = tmp_path / proc_lock.LOCK_FILENAME
        stamp = time.time() - (proc_lock._STALE_AGE_SECONDS + 60)
        lock.write_text("%d\n%f\n" % (os.getpid(), stamp))
        os.utime(str(lock), (stamp, stamp))
        assert proc_lock.lock_holder(str(tmp_path)) is None

    def test_polling_the_lock_does_not_wedge_its_release(self, tmp_path):
        """A reader must never prevent the holder from releasing.

        On Windows an open handle makes the holder's release rename fail with
        a sharing violation. Giving up there leaves an ownerless lock file on
        disk, which is honored until the six-hour stale ceiling -- every
        bootstrap pass on the machine stands down until then. Observed
        directly: a tail polling lock_holder several times a second wedged the
        very pass it was watching.
        """
        data_dir = str(tmp_path)
        stop = threading.Event()

        def poll():
            while not stop.is_set():
                proc_lock.lock_holder(data_dir)

        reader = threading.Thread(target=poll)
        reader.start()
        try:
            for _ in range(40):
                with proc_lock.engine_lock(data_dir) as acquired:
                    assert acquired
                assert proc_lock.lock_holder(data_dir) is None, (
                    "the lock survived its holder while a reader was polling"
                )
        finally:
            stop.set()
            reader.join()

    def test_unparseable_lock_is_in_flight_then_stale(self, tmp_path):
        lock = tmp_path / proc_lock.LOCK_FILENAME
        lock.write_text("")
        info = proc_lock.lock_holder(str(tmp_path))
        assert info is not None and info["pid"] is None  # mid-claim
        aged = time.time() - (proc_lock._EMPTY_LOCK_GRACE_SECONDS + 5)
        os.utime(str(lock), (aged, aged))
        assert proc_lock.lock_holder(str(tmp_path)) is None


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

class TestStatus:

    @pytest.fixture
    def data_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(tmp_path))
        monkeypatch.delenv("BOOTSTRAP_MARKETPLACE", raising=False)
        (tmp_path / "mkt-a" / "bootstrap").mkdir(parents=True)
        return tmp_path

    def test_reports_idle(self, data_root, capsys):
        assert cli.cmd_status(_args(json=False)) == 0
        assert "no bootstrap pass is running" in capsys.readouterr().out

    def test_reports_running_and_then_waits_for_it(
            self, data_root, monkeypatch, capsys):
        """The bare command BLOCKS on a running pass -- the whole point.

        Answering "yes, one is running" and exiting leaves the asker to poll
        by hand, and strands them a second before the pass they care about
        finishes.
        """
        followed = []
        monkeypatch.setattr(cli, "follow", lambda d: followed.append(d) or 0)
        with proc_lock.engine_lock(str(data_root / "mkt-a" / "bootstrap")):
            assert cli.cmd_status(_args(json=False)) == 0
        out = capsys.readouterr().out
        assert "RUNNING" in out and str(os.getpid()) in out
        assert followed == [str(data_root / "mkt-a" / "bootstrap")]

    def test_idle_does_not_block(self, data_root, monkeypatch, capsys):
        monkeypatch.setattr(cli, "follow", lambda d: pytest.fail(
            "nothing is running; there is nothing to wait for"))
        assert cli.cmd_status(_args(json=False)) == 0
        capsys.readouterr()

    def test_json_never_blocks(self, data_root, monkeypatch, capsys):
        """The scripting form must return even while a pass is in flight."""
        monkeypatch.setattr(cli, "follow", lambda d: pytest.fail(
            "--json is the non-blocking probe"))
        with proc_lock.engine_lock(str(data_root / "mkt-a" / "bootstrap")):
            assert cli.cmd_status(_args(json=True)) == 0
        assert json.loads(capsys.readouterr().out)[0]["running"] is True

    def test_several_running_marketplaces_are_not_interleaved(
            self, data_root, monkeypatch, capsys):
        (data_root / "mkt-b" / "bootstrap").mkdir(parents=True)
        monkeypatch.setattr(cli, "follow", lambda d: pytest.fail(
            "two engines' lines would be attributed to the wrong one"))
        with proc_lock.engine_lock(str(data_root / "mkt-a" / "bootstrap")):
            with proc_lock.engine_lock(str(data_root / "mkt-b" / "bootstrap")):
                assert cli.cmd_status(_args(json=False)) == 0
        assert "BOOTSTRAP_MARKETPLACE" in capsys.readouterr().out

    def test_json_is_machine_readable(self, data_root, capsys):
        assert cli.cmd_status(_args(json=True)) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload == [{"marketplace": "mkt-a", "running": False}]

    def test_idle_still_exits_zero(self, data_root, capsys):
        """Both answers are correct answers; neither is an error."""
        assert cli.cmd_status(_args(json=True)) == 0
        capsys.readouterr()

    def test_every_marketplace_is_reported(self, data_root, capsys):
        (data_root / "mkt-b" / "bootstrap").mkdir(parents=True)
        cli.cmd_status(_args(json=True))
        payload = json.loads(capsys.readouterr().out)
        assert {r["marketplace"] for r in payload} == {"mkt-a", "mkt-b"}


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

class TestRun:

    @pytest.fixture(autouse=True)
    def no_real_data_root(self, tmp_path_factory, monkeypatch):
        """Belt and braces for the leak the fixture above describes.

        Every test in this class gets a redirected data root by default, so
        forgetting one cannot write into the developer's real
        ~/.claude/plugins/data. A test that wants its own still sets it.
        """
        monkeypatch.setenv(
            "CLAUDE_BOOTSTRAP_DATA_ROOT",
            str(tmp_path_factory.mktemp("default-data-root")))

    def test_refuses_instead_of_attaching_to_a_different_scope(
            self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(tmp_path))
        monkeypatch.setenv("BOOTSTRAP_MARKETPLACE", "mkt-a")
        data_dir = tmp_path / "mkt-a" / "bootstrap"
        data_dir.mkdir(parents=True)

        launched = []
        monkeypatch.setattr(cli.subprocess, "Popen",
                            lambda *a, **k: launched.append(a))
        monkeypatch.setattr(cli, "follow", lambda d: pytest.fail("attached"))

        with proc_lock.engine_lock(str(data_dir)):
            rc = cli.cmd_run(_args(plugin_root="", forward=[]))

        assert rc == 2
        assert launched == [], "a second engine must never be spawned"
        assert "already running" in capsys.readouterr().err

    def test_engine_flags_pass_through(self, tmp_path, monkeypatch, capsys):
        """`bootstrap run --verbose` is the spelling the help advertises.

        Neither nargs="*" nor argparse.REMAINDER carries a LEADING dash-token
        into a subparser's first positional, so this exact invocation once
        died with "unrecognized arguments: --verbose".
        """
        # REDIRECT THE DATA ROOT, always. `run` now creates the watch marker
        # (and its parent directory) rather than only reading, so a test that
        # names a marketplace without redirecting the root writes into the
        # developer's real ~/.claude/plugins/data.
        monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(tmp_path))
        monkeypatch.setattr(cli, "marketplaces", lambda: ["mkt-a"])
        monkeypatch.setattr(cli, "holder", lambda d: None)
        monkeypatch.setattr(cli, "find_plugin_root", lambda m, f="": "/plug")
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return _ExitedProcess()

        monkeypatch.setattr(cli.subprocess, "Popen", fake_run)
        monkeypatch.setattr(cli, "FINAL_GRACE_SECONDS", 0.0)
        monkeypatch.setattr(cli, "POLL_INTERVAL", 0.0)
        assert cli.main(["run", "--verbose"]) == 0
        capsys.readouterr()
        assert seen["cmd"][-2:] == ["--console", "--verbose"]
        assert seen["cmd"][0] == sys.executable
        assert os.path.normpath(seen["cmd"][1]) == os.path.normpath(
            "/plug/scripts/bootstrap_run.py"
        )
        assert seen["cmd"][seen["cmd"].index("--project-dir") + 1] == os.getcwd()

    def test_unknown_flag_without_run_is_still_an_error(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["--nonsense"])
        assert "unrecognized" in capsys.readouterr().err

    def test_losing_the_lock_race_returns_runner_refusal(
            self, tmp_path, monkeypatch, capsys):
        """The up-front lock check is not the last word.

        Another launcher can take the lock after the up-front check. The
        runner's refusal must be returned; no wider pass may be attached.
        """
        monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(tmp_path))
        monkeypatch.setenv("BOOTSTRAP_MARKETPLACE", "mkt-a")
        data_dir = tmp_path / "mkt-a" / "bootstrap"
        data_dir.mkdir(parents=True)
        monkeypatch.setattr(cli, "find_plugin_root", lambda m, f="": "/plug")
        monkeypatch.setattr(cli, "follow", lambda d: pytest.fail("attached"))

        monkeypatch.setattr(cli, "FINAL_GRACE_SECONDS", 0.0)
        monkeypatch.setattr(cli, "POLL_INTERVAL", 0.0)

        def racing_engine(cmd, **kw):
            # Stand in for the launcher that won: it holds the lock by the
            # time the engine we launched has given up.
            racing_engine.lock = proc_lock.engine_lock(str(data_dir))
            assert racing_engine.lock.__enter__() is True
            process = _ExitedProcess()
            process.returncode = 2
            return process

        monkeypatch.setattr(cli.subprocess, "Popen", racing_engine)
        try:
            assert cli.cmd_run(_args(plugin_root="", forward=[])) == 2
        finally:
            racing_engine.lock.__exit__(None, None, None)
        capsys.readouterr()

    def test_streams_the_pass_it_launched(
            self, tmp_path, monkeypatch, capsys):
        """A launched pass must stream too, not only an attached one.

        The console engine prints its verdict and its failures and nothing
        else, so a clean three-minute pass showed five lines of shell preamble
        and exited -- indistinguishable, from the terminal, from bootstrap
        having done nothing.
        """
        monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(tmp_path))
        monkeypatch.setenv("BOOTSTRAP_MARKETPLACE", "mkt-a")
        data_dir = tmp_path / "mkt-a" / "bootstrap"
        data_dir.mkdir(parents=True)
        events = data_dir / EVENTS_FILENAME
        events.write_text(_event(seq=0, text="from an earlier pass") + "\n")
        monkeypatch.setattr(cli, "find_plugin_root", lambda m, f="": "/plug")
        monkeypatch.setattr(cli, "FINAL_GRACE_SECONDS", 0.0)
        monkeypatch.setattr(cli, "POLL_INTERVAL", 0.0)

        def engine_that_records(cmd, **kw):
            with open(str(events), "a") as f:
                f.write(_event(seq=1, text="uv: ok") + "\n")
                f.write(json.dumps({"kind": "emit", "pass": "p1",
                                    "system_message": "the verdict"}) + "\n")
            return _ExitedProcess()

        monkeypatch.setattr(cli.subprocess, "Popen", engine_that_records)
        assert cli.cmd_run(_args(plugin_root="", forward=[])) == 0
        out = capsys.readouterr().out
        assert "uv: ok" in out
        assert "from an earlier pass" not in out
        # The child prints the verdict to this same terminal; printing it
        # again from the event stream would double it.
        assert "the verdict" not in out

    def test_refuses_to_guess_between_marketplaces(
            self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(tmp_path))
        monkeypatch.delenv("BOOTSTRAP_MARKETPLACE", raising=False)
        for name in ("mkt-a", "mkt-b"):
            (tmp_path / name / "bootstrap").mkdir(parents=True)
        assert cli.cmd_run(_args(plugin_root="", forward=[])) == 2
        assert "BOOTSTRAP_MARKETPLACE" in capsys.readouterr().err


# --------------------------------------------------------------------------
# follow / rendering
# --------------------------------------------------------------------------

class TestPluginRootResolution:

    def test_explicit_override_outranks_the_installed_cache(
            self, tmp_path, monkeypatch):
        """Otherwise `run` silently launches the installed engine.

        Observed: with BOOTSTRAP_PLUGIN_ROOT pointing at a dev checkout, the
        pass ran the cached version's wrapper while reporting the dev tree's
        name -- so a fix under test never executed and the run looked like it
        had.
        """
        root = tmp_path / "devtree"
        (root / "hooks" / "sessionstart").mkdir(parents=True)
        (root / "hooks" / "sessionstart" / "session-bootstrap.sh").write_text("")
        monkeypatch.setenv("BOOTSTRAP_PLUGIN_ROOT", str(root))
        assert cli.find_plugin_root("plugins-kit") == str(root)

    def test_a_bogus_override_falls_back_to_discovery(
            self, tmp_path, monkeypatch):
        """An override naming no plugin tree must not disable resolution."""
        bogus = tmp_path / "nope"
        monkeypatch.setenv("BOOTSTRAP_PLUGIN_ROOT", str(bogus))
        fallback = tmp_path / "fallback"
        (fallback / "hooks" / "sessionstart").mkdir(parents=True)
        (fallback / "hooks" / "sessionstart" / "session-bootstrap.sh").write_text("")
        # Resolution continues: whatever comes back is a real plugin tree
        # (this machine's installed cache, or the supplied fallback), never
        # the path that does not exist.
        resolved = cli.find_plugin_root("plugins-kit", str(fallback))
        assert resolved != str(bogus)
        assert cli._is_plugin_root(resolved)

    def test_versions_sort_numerically(self):
        """0.98.1 must not outrank 0.104.0 -- that runs a superseded engine."""
        assert cli._version_key("0.104.0") > cli._version_key("0.98.1")


class TestFollow:

    def test_streams_a_running_pass_until_its_lock_clears(
            self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(cli, "FINAL_GRACE_SECONDS", 0.2)
        monkeypatch.setattr(cli, "POLL_INTERVAL", 0.02)
        data_dir = str(tmp_path)
        events = tmp_path / EVENTS_FILENAME
        # Output that predates the attach belongs to an earlier pass and must
        # not be replayed.
        events.write_text(_event(seq=0, text="from a previous pass") + "\n")

        released = threading.Event()

        holding = threading.Event()
        watch = tmp_path / WATCH_FILENAME

        def hold():
            with proc_lock.engine_lock(data_dir):
                holding.set()
                # The watch marker is follow()'s attach, observably: it writes
                # it before recording its start offset. Waiting on it (rather
                # than on a sleep sized for an idle machine) is what makes the
                # assertion below about STREAMING and not about a replay of
                # the file's existing tail.
                _poll_until(watch.exists)
                with open(str(events), "a") as f:
                    f.write(_event(seq=1, text="uv: ok") + "\n")
                    f.flush()
            released.set()

        worker = threading.Thread(target=hold)
        worker.start()
        assert holding.wait(10.0), "worker never acquired the lock"
        assert cli.follow(data_dir) == 0
        worker.join()
        assert released.is_set()

        out = capsys.readouterr().out
        assert "uv: ok" in out
        assert "from a previous pass" not in out
        assert "finished" in out

    def test_watch_marker_is_removed_afterwards(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "FINAL_GRACE_SECONDS", 0.0)
        monkeypatch.setattr(cli, "POLL_INTERVAL", 0.01)
        cli.follow(str(tmp_path))
        # Left behind, it would make every future pass flush per second for a
        # reader who has gone.
        assert not (tmp_path / WATCH_FILENAME).exists()

    def test_partial_final_line_is_not_printed_twice(self, tmp_path, capsys):
        events = tmp_path / EVENTS_FILENAME
        complete = _event(seq=1, text="first")
        events.write_text(complete + "\n" + '{"seq": 2, "te')
        offset = cli._drain(str(events), 0)
        assert "first" in capsys.readouterr().out
        with open(str(events), "a") as f:
            f.write('xt": "second", "kind": "check"}\n')
        cli._drain(str(events), offset)
        out = capsys.readouterr().out
        assert "second" in out
        assert "first" not in out

    def test_rotation_mid_tail_restarts_rather_than_going_silent(
            self, tmp_path, capsys):
        events = tmp_path / EVENTS_FILENAME
        events.write_text(_event(seq=1, text="before rotation") + "\n")
        offset = cli._drain(str(events), 0)
        capsys.readouterr()
        events.write_text(_event(seq=1, text="after rotation") + "\n")
        cli._drain(str(events), offset)
        assert "after rotation" in capsys.readouterr().out

    def test_emit_records_render_as_the_verdict(self):
        rendered = cli._render(json.dumps(
            {"kind": "emit", "system_message": "bootstrap complete"}))
        assert "bootstrap complete" in rendered

    def test_unparseable_line_is_skipped(self):
        assert cli._render("not json at all") is None


# --------------------------------------------------------------------------
# the recorder's watched-flush, which is what makes a tail live at all
# --------------------------------------------------------------------------

class TestWatchedFlush:

    def test_unwatched_pass_still_writes_only_at_exit(self, tmp_path):
        recorder = PassRecorder(str(tmp_path), autoflush=False)
        for i in range(50):
            recorder.record_entry("ok", "entry %d" % i)
        assert not (tmp_path / EVENTS_FILENAME).exists()

    def test_watched_pass_flushes_as_it_goes(self, tmp_path):
        recorder = PassRecorder(str(tmp_path), autoflush=False)
        (tmp_path / WATCH_FILENAME).write_text("1")
        recorder.record_entry("ok", "visible mid-pass")
        assert (tmp_path / EVENTS_FILENAME).exists()

    def test_flushes_are_throttled(self, tmp_path):
        recorder = PassRecorder(str(tmp_path), autoflush=False)
        (tmp_path / WATCH_FILENAME).write_text("1")
        recorder.record_entry("ok", "first")
        size = (tmp_path / EVENTS_FILENAME).stat().st_size
        for i in range(20):
            recorder.record_entry("ok", "burst %d" % i)
        assert (tmp_path / EVENTS_FILENAME).stat().st_size == size


# --------------------------------------------------------------------------

class _ExitedProcess:
    """A Popen stand-in that has already exited cleanly."""

    returncode = 0

    def poll(self):
        return 0


def _poll_until(predicate, timeout=10.0):
    """Wait on a causal observable, never on a duration."""
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition never became true"
        time.sleep(0.005)


def _args(**kw):
    kw.setdefault("json", False)
    kw.setdefault("plugin_root", "")
    kw.setdefault("forward", [])
    return type("Args", (), kw)()


def _event(seq, text):
    return json.dumps({
        "pass": "p1", "seq": seq, "ts": "2026-09-11T17:00:0%dZ" % (seq % 10),
        "kind": "check", "sev": "ok", "section": "tools", "text": text,
    })


# --------------------------------------------------------------------------
# reset
# --------------------------------------------------------------------------

class TestReset:
    """`bootstrap reset` -- the cooldown lever, reachable from the CLI.

    The verb OWNS no reset logic: it delegates to bootstrap-reset-cooldown.sh,
    which is the single place that knows how a cooldown stamp is keyed and
    which files go with it. So what is worth pinning is that the delegation
    happens, that flags survive it, and that the end-to-end effect is a stamp
    that is gone.
    """

    @pytest.fixture
    def plugin_root(self):
        # The repo's own bootstrap tree -- find_plugin_root accepts it via the
        # fallback the shim normally supplies.
        return os.path.dirname(SCRIPTS)

    def test_removes_the_cooldown_stamp(self, tmp_path, monkeypatch, plugin_root):
        """End to end, through the real shell lever, on a redirected data root."""
        project = tmp_path / "proj"
        project.mkdir()
        cooldowns = tmp_path / "data" / "mkt-a" / "bootstrap" / "cooldowns"
        cooldowns.mkdir(parents=True)
        key = hashlib.sha1(str(project).encode()).hexdigest()
        stamp = cooldowns / ("last_run_epoch.%s" % key)
        stamp.write_text("123\n")

        monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(tmp_path / "data"))
        monkeypatch.delenv("BOOTSTRAP_MARKETPLACE", raising=False)
        rc = cli.main(["--plugin-root", plugin_root,
                       "reset", "--project", str(project)])

        assert rc == 0
        assert not stamp.exists(), "the cooldown stamp must be gone"

    def test_flags_pass_through(self, tmp_path, monkeypatch):
        """`--all`, `--status` and friends belong to the lever, not to argparse."""
        monkeypatch.setattr(cli, "find_reset_script", lambda f="": "/plug/reset.sh")
        seen = {}

        def fake_call(cmd, **kw):
            seen["cmd"] = cmd
            return 0

        monkeypatch.setattr(cli.subprocess, "call", fake_call)
        assert cli.main(["reset", "--all", "--clear-alerts"]) == 0
        assert seen["cmd"] == ["bash", "/plug/reset.sh", "--all", "--clear-alerts"]

    def test_help_reaches_the_lever_rather_than_argparse(self, monkeypatch):
        """The advertised flags live in the delegate's help, so -h must reach it."""
        monkeypatch.setattr(cli, "find_reset_script", lambda f="": "/plug/reset.sh")
        seen = {}

        def fake_call(cmd, **kw):
            seen["cmd"] = cmd
            return 0

        monkeypatch.setattr(cli.subprocess, "call", fake_call)
        assert cli.main(["reset", "--help"]) == 0
        assert seen["cmd"][-1] == "--help"

    def test_no_plugin_tree_is_an_error_not_a_silent_success(
            self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(cli, "find_reset_script", lambda f="": "")
        assert cli.main(["reset"]) == 2
        assert "no bootstrap plugin tree" in capsys.readouterr().err

    def test_exit_code_is_the_levers_own(self, monkeypatch):
        monkeypatch.setattr(cli, "find_reset_script", lambda f="": "/plug/reset.sh")
        monkeypatch.setattr(cli.subprocess, "call", lambda cmd, **kw: 2)
        assert cli.main(["reset", "--project", "/nope"]) == 2
