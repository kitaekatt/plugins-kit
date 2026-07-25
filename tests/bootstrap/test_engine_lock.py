"""Tests for the engine's single-instance lock (proc_lock).

Rapid session start/exit/restart can fire several independent bootstrap
launchers within the same few seconds -- session-bootstrap.sh, the harvest,
the SessionStart-missed rescue -- each of which only guards against
RE-launching itself, not a genuinely concurrent OTHER process. main() must
refuse to run a second pass while one is already active, and must never wedge
the lock permanently when a prior holder crashed or was killed.
"""

import hashlib
import os
import sys
import threading
import time

import pytest

from bootstrap_lib import engine, proc_lock


def _argv(data_dir, **extra):
    argv = [
        "bootstrap_engine.py",
        "--plugin-root", "unused-root",
        "--data-dir", str(data_dir),
    ]
    for k, v in extra.items():
        argv.append(f"--{k.replace('_', '-')}")
        if v is not True:
            argv.append(str(v))
    return argv


class TestProcLock:
    def test_acquires_when_no_lock_present(self, tmp_path):
        with proc_lock.engine_lock(str(tmp_path)) as acquired:
            assert acquired
        assert not (tmp_path / proc_lock.LOCK_FILENAME).exists(), (
            "lock file must be removed on clean release"
        )

    def test_second_acquire_fails_while_first_held(self, tmp_path):
        lock_path = tmp_path / proc_lock.LOCK_FILENAME
        # Simulate a currently-running holder: write OUR OWN pid (guaranteed
        # alive) so the liveness check treats it as active.
        lock_path.write_text(f"{os.getpid()}\n123.0\n")
        assert proc_lock._try_acquire(str(lock_path)) is False

    def test_stale_lock_from_dead_pid_is_stolen(self, tmp_path):
        lock_path = tmp_path / proc_lock.LOCK_FILENAME
        # PID unlikely to be alive; pick a large arbitrary number and confirm
        # not alive first (best-effort — extremely unlikely to collide).
        dead_pid = 999999
        assert not proc_lock._pid_alive(dead_pid)
        lock_path.write_text(f"{dead_pid}\n123.0\n")
        assert proc_lock._try_acquire(str(lock_path)) is True
        assert lock_path.read_text().splitlines()[0] == str(os.getpid())

    def test_fresh_empty_lock_is_not_stolen(self, tmp_path, monkeypatch):
        """A lock file that exists but hasn't had its PID payload written yet
        (the window inside _create_exclusive between os.open() and the write
        landing) must be treated as in-flight, not stale -- stealing it would
        let a racer rip the true winner's lock out from under it."""
        lock_path = tmp_path / proc_lock.LOCK_FILENAME
        lock_path.write_text("")  # simulates the momentary empty-file window
        monkeypatch.setattr(proc_lock, "_MAX_ACQUIRE_ATTEMPTS", 2)
        assert proc_lock._try_acquire(str(lock_path)) is False
        # Must NOT have unlinked/stolen it -- still empty, not our pid.
        assert lock_path.read_text() == ""

    def test_old_unparseable_lock_is_stolen(self, tmp_path, monkeypatch):
        """Unlike a FRESH empty lock, one that's sat unparseable past the
        grace window is presumed abandoned (a holder that crashed between
        open() and its payload write) and must be recoverable."""
        import os as _os
        lock_path = tmp_path / proc_lock.LOCK_FILENAME
        lock_path.write_text("")
        old_time = time.time() - 10
        _os.utime(str(lock_path), (old_time, old_time))
        monkeypatch.setattr(proc_lock, "_EMPTY_LOCK_GRACE_SECONDS", 0.01)
        assert proc_lock._try_acquire(str(lock_path)) is True
        assert lock_path.read_text().splitlines()[0] == str(os.getpid())

    def test_live_pid_but_aged_past_ceiling_is_stolen(self, tmp_path):
        """Guards against PID reuse: a lock whose recorded PID reads as alive
        (an unrelated live process now holds that recycled PID number) but
        whose file has aged past the stale-age ceiling must still be
        recoverable -- liveness alone can never expire, so without an age
        ceiling this would wedge the lock forever."""
        lock_path = tmp_path / proc_lock.LOCK_FILENAME
        lock_path.write_text(f"{os.getpid()}\n123.0\n")  # our own pid: alive
        old_time = time.time() - (proc_lock._STALE_AGE_SECONDS + 5)
        os.utime(str(lock_path), (old_time, old_time))
        assert proc_lock._try_acquire(str(lock_path)) is True
        assert lock_path.read_text().splitlines()[0] == str(os.getpid())

    def test_live_pid_within_ceiling_is_not_stolen(self, tmp_path):
        lock_path = tmp_path / proc_lock.LOCK_FILENAME
        lock_path.write_text(f"{os.getpid()}\n123.0\n")
        assert proc_lock._try_acquire(str(lock_path)) is False

    def test_stale_removal_is_ownership_conditional(self, tmp_path):
        """The steal path must not blindly unlink whatever is at lock_path --
        only the PID it just judged stale. If a real winner's fresh lock has
        since landed there (a race between reading the stale PID and
        removing it), that winner's lock must survive."""
        lock_path = tmp_path / proc_lock.LOCK_FILENAME
        dead_pid = 999997
        assert not proc_lock._pid_alive(dead_pid)
        lock_path.write_text(f"{dead_pid}\n123.0\n")

        # _remove_if_owned must refuse to delete a lock now owned by a
        # DIFFERENT pid than the one it was asked to remove.
        proc_lock._remove_if_owned(str(lock_path), dead_pid + 1)
        assert lock_path.read_text().splitlines()[0] == str(dead_pid), (
            "removal must be a no-op when the current owner doesn't match"
        )
        proc_lock._remove_if_owned(str(lock_path), dead_pid)
        assert not lock_path.exists(), "removal must succeed when the owner matches"

    def test_lock_released_even_on_exception(self, tmp_path):
        with pytest.raises(RuntimeError):
            with proc_lock.engine_lock(str(tmp_path)) as acquired:
                assert acquired
                raise RuntimeError("boom")
        assert not (tmp_path / proc_lock.LOCK_FILENAME).exists()

    def test_concurrent_stale_lock_steal_has_exactly_one_winner(self, tmp_path):
        """Two racers both observing the same stale lock must not both
        believe they hold it -- the steal path must be exclusive, not a
        non-exclusive overwrite (which is exactly the bug this lock exists
        to prevent: two engine passes running at once)."""
        lock_path = tmp_path / proc_lock.LOCK_FILENAME
        dead_pid = 999998
        assert not proc_lock._pid_alive(dead_pid)
        lock_path.write_text(f"{dead_pid}\n123.0\n")

        results = []
        barrier = threading.Barrier(8)

        def racer():
            barrier.wait()
            results.append(proc_lock._try_acquire(str(lock_path)))

        threads = [threading.Thread(target=racer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(True) == 1, (
            f"exactly one racer must win the stale-lock steal, got {results}"
        )

    def test_release_lock_removes_own_lock(self, tmp_path):
        with proc_lock.engine_lock(str(tmp_path)):
            proc_lock.release_lock(str(tmp_path))
            assert not (tmp_path / proc_lock.LOCK_FILENAME).exists()
        # engine_lock's own exit must be a no-op afterward (idempotent).
        assert not (tmp_path / proc_lock.LOCK_FILENAME).exists()

    def test_release_lock_never_touches_a_different_owner(self, tmp_path):
        lock_path = tmp_path / proc_lock.LOCK_FILENAME
        other_pid = os.getpid() + 1  # guaranteed different from our own pid
        lock_path.write_text(f"{other_pid}\n123.0\n")
        proc_lock.release_lock(str(tmp_path))
        assert lock_path.exists(), "release_lock must never remove a lock it doesn't own"
        assert lock_path.read_text().splitlines()[0] == str(other_pid)


class TestEngineMainLock:
    def test_main_stands_down_when_another_instance_holds_lock(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        lock_path = data_dir / proc_lock.LOCK_FILENAME
        lock_path.write_text(f"{os.getpid()}\n123.0\n")  # simulate live holder

        called = []
        monkeypatch.setattr(engine, "_main", lambda: called.append(True))
        monkeypatch.setattr(sys, "argv", _argv(data_dir, background=True))

        engine.main()  # must return quietly, not run _main, not raise

        assert called == [], "a second instance must never run _main while the lock is held"
        # Standing down must not disturb the existing holder's lock file.
        assert lock_path.read_text().splitlines()[0] == str(os.getpid())

    def test_stand_down_rolls_back_project_cooldown(self, tmp_path, monkeypatch):
        """A lock-contended launch must not silently strand the project it
        was for behind an already-consumed cooldown stamp -- otherwise that
        project gets no bootstrap pass until the throttle window expires on
        its own (the cross-project starvation the per-project cooldown was
        built to prevent)."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / proc_lock.LOCK_FILENAME).write_text(f"{os.getpid()}\n123.0\n")

        project_dir = str(tmp_path / "proj")
        key = hashlib.sha1(project_dir.encode("utf-8")).hexdigest()
        cooldowns = data_dir / "cooldowns"
        cooldowns.mkdir()
        stamp = cooldowns / f"last_run_epoch.{key}"
        stamp.write_text("123")

        monkeypatch.setattr(engine, "_main", lambda: (_ for _ in ()).throw(
            AssertionError("_main must not run while lock is held")
        ))
        monkeypatch.setattr(sys, "argv", _argv(
            data_dir, background=True, project_dir=project_dir,
        ))

        engine.main()

        assert not stamp.exists(), (
            "stand-down must clear the project's cooldown stamp so it gets a retry"
        )

    def test_stand_down_leaves_harvest_launch_markers_alone(self, tmp_path, monkeypatch):
        """These markers are GLOBAL, not per-launch: a plain SessionStart
        losing the lock race must not wipe a DIFFERENT, still-running
        harvest/import-retry/relaunch pass's in-flight guard (that pass may
        be the very one holding the lock right now). Clearing a marker this
        stand-down doesn't own would falsely invite a duplicate spawn."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / proc_lock.LOCK_FILENAME).write_text(f"{os.getpid()}\n123.0\n")
        harvest_stamp = data_dir / "harvest_launched_version"
        harvest_stamp.write_text("1.2.3")
        retry_stamp = data_dir / "import_retry_launched"
        retry_stamp.write_text("1")
        relaunch_stamp = data_dir / "plugins_relaunch_hash"
        relaunch_stamp.write_text("deadbeef")

        monkeypatch.setattr(engine, "_main", lambda: None)
        monkeypatch.setattr(sys, "argv", _argv(data_dir, background=True))

        engine.main()

        assert harvest_stamp.read_text() == "1.2.3"
        assert retry_stamp.read_text() == "1"
        assert relaunch_stamp.read_text() == "deadbeef"

    def test_stand_down_logs_to_bootstrap_log(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / proc_lock.LOCK_FILENAME).write_text(f"{os.getpid()}\n123.0\n")

        monkeypatch.setattr(engine, "_main", lambda: None)
        monkeypatch.setattr(sys, "argv", _argv(data_dir, background=True))

        engine.main()

        log_file = data_dir / "bootstrap.log"
        assert log_file.is_file(), "a stand-down must be visible in bootstrap.log"
        assert "stand-down" in log_file.read_text()

    def test_main_runs_and_releases_lock_when_uncontended(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        called = []
        monkeypatch.setattr(engine, "_main", lambda: called.append(True))
        monkeypatch.setattr(sys, "argv", _argv(data_dir, background=True))

        engine.main()

        assert called == [True]
        assert not (data_dir / proc_lock.LOCK_FILENAME).exists()

    def test_main_releases_lock_after_crash(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        def _boom():
            raise RuntimeError("boom")

        monkeypatch.setattr(engine, "_main", _boom)
        monkeypatch.setattr(sys, "argv", _argv(data_dir, background=True))

        with pytest.raises(SystemExit):
            engine.main()

        assert not (data_dir / proc_lock.LOCK_FILENAME).exists(), (
            "a crashed pass must not wedge the lock for the next session"
        )


class TestSpawnRecheckPassReleasesLock:
    def test_releases_lock_before_spawning_child(self, tmp_path, monkeypatch):
        """_spawn_recheck_pass launches a SECOND full engine process with the
        SAME --data-dir while this (parent) process is still inside its own
        engine_lock(). Without releasing first, the child would see the
        parent's still-alive PID as the lock holder and stand down without
        running its post-elevation re-check."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        with proc_lock.engine_lock(str(data_dir)) as acquired:
            assert acquired

            captured_cmd = []
            data_dir_str = str(data_dir)

            class _FakeArgs:
                data_dir = data_dir_str
                project_dir = None
                verbose = False
                console = False
                background = False

            def _fake_run(cmd):
                # The lock must already be released by the time the child
                # would actually run, so a real child process (which also
                # goes through engine.main() -> engine_lock) could acquire it.
                assert not (data_dir / proc_lock.LOCK_FILENAME).exists(), (
                    "lock must be released before the child engine spawns"
                )
                captured_cmd.append(cmd)

            import subprocess as _subprocess
            monkeypatch.setattr(_subprocess, "run", _fake_run)

            engine._spawn_recheck_pass(_FakeArgs(), "unused-plugin-root")

            assert captured_cmd, "expected subprocess.run to be invoked"
