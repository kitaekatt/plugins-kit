"""Run: uv run --extra dev pytest tests/git-kit/test_worktree_holder_session.py -q.

Triggers: Bash-tool ancestry, missing session parents, holder death during
maintenance, and release from inside the holder's own session. Every repository and fake Claude process is test-owned.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator

import pytest

import worktree_safety as safety
import worktrees as wt
from test_worktree_lifecycle import holder, lifecycle


@contextmanager
def fake_claude(tmp_path: Path, *command: str, script: str | None = None) -> Iterator[subprocess.Popen]:
    """Keep a native process named claude alive after its Bash-tool child exits."""
    if os.name == "nt":
        pytest.skip("POSIX shell ancestry integration")
    executable = tmp_path / "claude"
    executable.symlink_to(shutil.which("sh"))
    if script is None:
        script = '"$@"; read waiting' if command else 'read waiting'
    process = subprocess.Popen([str(executable), "-c", script, "fake-claude", *command],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        yield process
    finally:
        process.communicate("done\n", timeout=30)


def cli_entry() -> str:
    scripts = Path(__file__).parents[2] / "plugins/git-kit/scripts"
    return (f"import sys; sys.path.insert(0, {str(scripts)!r}); "
            "from worktree_lifecycle import Lifecycle; "
            "Lifecycle.request_maintenance = lambda self: {}; "
            "from worktree_cli import main; sys.exit(main(sys.argv[1:]))")


@pytest.mark.parametrize("operation", ["take", "adopt"])
def test_cli_records_nearest_claude_ancestor(lifecycle, tmp_path: Path, monkeypatch, operation: str) -> None:
    obj = lifecycle
    obj.maintain()
    extra = []
    if operation == "adopt":
        record = obj.read_records()[0]
        record.update(state="parked")
        obj.save(record)
        extra = ["--id", record["id"]]
    output = tmp_path / "result.json"
    finished = tmp_path / "finished"
    # claude -> shell -> python; the intermediate shell stays until Python exits.
    shell = shutil.which("sh")
    if not shell:
        pytest.skip("POSIX shell ancestry integration")
    command = [shell, "-c", '"$@" > "$RESULT_FILE"; echo $? > "$EXIT_FILE"', "tool-shell",
               sys.executable, "-c", cli_entry(), operation, "--repo", str(obj.repo),
               "--agent", "session-holder", "--session", "test-session", *extra]
    monkeypatch.setenv("RESULT_FILE", str(output))
    monkeypatch.setenv("EXIT_FILE", str(finished))
    with fake_claude(tmp_path, *command) as parent:
        deadline = time.monotonic() + 30
        while not finished.exists() and time.monotonic() < deadline:
            assert parent.poll() is None
            time.sleep(0.01)
        assert finished.exists(), "take/adopt subprocess did not finish"
        assert finished.read_text().strip() == "0"
        result = json.loads(output.read_text())
        recorded = obj.get(result["id"])["holder"]
        assert recorded["pid"] == parent.pid
        assert recorded["start"] == safety.process_identity(parent.pid)["start"]
        obj.reconcile()
        assert obj.get(result["id"])["holder"] == recorded


def pid_named(pid: int, text: str) -> bool:
    """A blocker names the PID as a whole number, never inside the hex claim ID."""
    import re
    return re.search(rf"(?<![0-9a-f]){pid}(?![0-9a-f])", text) is not None


@pytest.mark.parametrize("case", ["alone", "inside", "pipeline", "subshell", "other_shell"])
def test_release_ignores_its_caller_chain_but_not_other_processes(lifecycle, tmp_path: Path, monkeypatch, case: str) -> None:
    """claude -> Bash-tool shell (cwd and argv in the worktree) -> python release.

    The shell waits for release, so it must not block. Another process of the
    same shell command ("pipeline": a sibling with its cwd in the worktree) does
    not block release either, but maintenance keeps the worktree while it runs.
    A separate shell under the holder ("other_shell": another Bash call) blocks.
    "subshell" is a grandchild of the same shell, as a pipeline element that
    runs through a shell function or a ( ... ) subshell; it does not block.
    "inside" runs release with its own cwd in the worktree, with no pipeline:
    neither release nor the scans it starts may block it.
    """
    obj = lifecycle
    obj.maintain()
    shell = shutil.which("sh")
    if not shell:
        pytest.skip("POSIX shell ancestry integration")
    agent = "self-release"
    worktree = obj.namespace / agent
    # The holder reads the claim, optionally starts another shell in the worktree
    # (its own child), then runs the Bash-tool shell with the claim on stdin.
    holder_script = ('read claim; if [ "$CASE" = other_shell ]; then (cd "$WORKTREE" && exec sleep 60) & '
                     'echo $! > "$OTHER_PID_FILE"; fi; echo "$claim" | "$@"; read waiting')
    script = ('read claim; cd "$1" || exit 90; shift; echo $$ > "$SHELL_PID_FILE"; '
              'if [ "$CASE" = pipeline ]; then sleep 60 & echo $! > "$SIBLING_PID_FILE"; fi; '
              'if [ "$CASE" = subshell ]; then (sleep 60 & echo $! > "$GRANDCHILD_PID_FILE"; wait) & '
              'echo $! > "$SIBLING_PID_FILE"; while [ ! -s "$GRANDCHILD_PID_FILE" ]; do sleep 0.05; done; fi; '
              'run_dir=$REPO_DIR; if [ "$CASE" = inside ]; then run_dir=$PWD; fi; '
              '(cd "$run_dir" && exec "$@" --claim "$claim") > "$RESULT_FILE" 2> "$ERROR_FILE"; echo $? > "$EXIT_FILE"')
    files = {name: tmp_path / name.lower() for name in
             ("SHELL_PID_FILE", "SIBLING_PID_FILE", "GRANDCHILD_PID_FILE", "OTHER_PID_FILE",
              "RESULT_FILE", "ERROR_FILE", "EXIT_FILE")}
    for name, path in files.items():
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv("REPO_DIR", str(obj.repo))
    monkeypatch.setenv("WORKTREE", str(worktree))
    monkeypatch.setenv("CASE", case)
    command = [shell, "-c", script, "tool-shell", str(worktree),
               sys.executable, "-c", cli_entry(), "release", "--repo", str(obj.repo)]
    with fake_claude(tmp_path, *command, script=holder_script) as parent:
        try:
            result = obj.take(agent, "self-session", process=safety.process_identity(parent.pid))
            assert Path(result["path"]) == worktree
            parent.stdin.write(result["claim"] + "\n")
            parent.stdin.flush()
            finished = files["EXIT_FILE"]
            deadline = time.monotonic() + 60
            while not finished.exists() and time.monotonic() < deadline:
                assert parent.poll() is None
                time.sleep(0.01)
            assert finished.exists(), "release subprocess did not finish"
            code = finished.read_text().strip()
            error = files["ERROR_FILE"].read_text()
            shell_pid = int(files["SHELL_PID_FILE"].read_text())
            assert not pid_named(shell_pid, error), error
            assert not pid_named(parent.pid, error), error
            record = obj.get(result["id"])
            if case == "other_shell":
                assert code != "0" and "Release safety failed" in error
                assert pid_named(int(files["OTHER_PID_FILE"].read_text()), error), error
                assert record["holder"]["claim"] == result["claim"]
                return
            assert code == "0", error
            assert json.loads(files["RESULT_FILE"].read_text())["state"] == "released"
            assert record["holder"] is None and record["state"] == "released"
            if case in {"pipeline", "subshell"}:
                # Maintenance has no callers: the sibling still in the worktree keeps it.
                obj.maintain()
                kept = obj.get(result["id"])
                assert kept["state"] == "released" and kept["path"] == str(worktree) and worktree.is_dir()
        finally:
            # Background sleeps hold the fake holder's pipes; end them before it exits.
            for name in ("GRANDCHILD_PID_FILE", "SIBLING_PID_FILE", "OTHER_PID_FILE"):
                if files[name].exists():
                    try:
                        os.kill(int(files[name].read_text()), 15)
                    except ProcessLookupError:
                        pass


@pytest.mark.parametrize("operation", ["take", "adopt"])
def test_no_claude_ancestor_refuses_without_explicit_holder(lifecycle, monkeypatch, capsys, operation: str) -> None:
    from worktree_cli import main

    obj = lifecycle
    obj.maintain()
    # Model a process chain with no Claude regardless of the test harness parent.
    monkeypatch.setattr(os, "getppid", os.getpid)
    monkeypatch.setattr(safety, "process_info", lambda pid: {"ppid": 0, "executable": "python", "command": "python"}, raising=False)
    extra = []
    if operation == "adopt":
        record = obj.read_records()[0]
        record.update(state="parked")
        obj.save(record)
        extra = ["--id", record["id"]]
    assert main([operation, "--repo", str(obj.repo), "--agent", "orphan", "--session", "session", *extra]) == 2
    assert "Claude" in capsys.readouterr().err
    assert all(not row["holder"] for row in obj.read_records())


@pytest.mark.parametrize("executable", [None, "/bin/sh"])
def test_legacy_exited_shell_holder_is_preserved(lifecycle, executable: str | None) -> None:
    obj = lifecycle
    obj.maintain()
    if os.name == "nt":
        pytest.skip("POSIX shell process")
    with subprocess.Popen([shutil.which("sh"), "-c", "read waiting"], stdin=subprocess.PIPE, text=True) as shell:
        identity = safety.process_identity(shell.pid)
        result = obj.take("legacy", "legacy-session", process=identity)
        shell.communicate("done\n", timeout=30)
    record = obj.get(result["id"])
    # The published version did not record executable evidence at all.
    record["holder"].pop("executable", None)
    record["holder"].pop("command", None)
    if executable is not None:
        record["holder"]["executable"] = executable
    obj.save(record)
    obj.maintain()
    current = obj.get(result["id"])
    assert current["holder"] == record["holder"]
    assert current["state"] == "taken" and Path(current["path"]).exists()


@pytest.mark.parametrize("operation", ["take", "adopt"])
def test_explicit_holder_fallback(lifecycle, operation: str) -> None:
    from worktree_cli import main

    obj = lifecycle
    obj.maintain()
    extra = []
    if operation == "adopt":
        record = obj.read_records()[0]
        record.update(state="parked")
        obj.save(record)
        extra = ["--id", record["id"]]
    assert main([operation, "--repo", str(obj.repo), "--agent", "explicit", "--session", "session",
                 "--holder-pid", str(os.getpid()), *extra]) == 0
    record = next(row for row in obj.read_records() if row["holder"])
    assert record["holder"]["pid"] == os.getpid()


def test_dead_claude_holder_is_parked(lifecycle, tmp_path: Path, monkeypatch) -> None:
    import worktree_migration as migration

    monkeypatch.setattr(migration, "claude_processes", lambda: [])
    obj = lifecycle
    obj.maintain()
    with fake_claude(tmp_path) as parent:
        result = obj.take("ended", "ended-session", process=safety.process_identity(parent.pid))
    obj.reconcile()
    record = obj.get(result["id"])
    assert record["state"] == "parked" and record["holder"] is None
    assert record["last_holder"]["pid"] == parent.pid


@pytest.mark.parametrize("case, parked", [("idle", True), ("recent-git", False), ("recent-edit", False),
                                           ("process-inside", False)])
def test_live_session_claim_parks_when_nothing_uses_the_worktree(lifecycle, tmp_path: Path, monkeypatch,
                                                                 case: str, parked: bool) -> None:
    """A live session keeps a claim only while the worktree shows use: a process in it or recent changes."""
    obj = lifecycle
    obj.maintain()
    result = obj.take("long-done", "orchestrator", process=holder())
    path = Path(obj.get(result["id"])["path"])
    old = time.time() - safety.IDLE_CLAIM_SECONDS - 60
    for target in [path, *path.rglob("*")]:
        os.utime(target, (old, old), follow_symlinks=False)
    gitdir = Path(wt.git(path, "rev-parse", "--absolute-git-dir"))
    for target in [gitdir, *gitdir.rglob("*")]:
        os.utime(target, (old, old), follow_symlinks=False)
    if case == "recent-git":
        os.utime(gitdir / "HEAD")
    elif case == "recent-edit":
        (path / "draft.txt").write_text("in progress")
    process = None
    if case == "process-inside":
        process = subprocess.Popen(["sleep", "60"], cwd=path)
    try:
        obj.reconcile()
    finally:
        if process:
            process.kill()
            process.wait()
    record = obj.get(result["id"])
    assert (record["state"] == "parked" and record["holder"] is None) is parked
    if parked:
        assert record["last_holder"]["session"] == "orchestrator"


@pytest.mark.parametrize("evidence", ["live", "unknown-session", "failed-census"])
def test_dead_holder_keeps_claim_when_session_may_be_alive(lifecycle, monkeypatch, evidence: str) -> None:
    import worktree_migration as migration

    obj = lifecycle
    obj.maintain()
    result = obj.take("resumed", "resumed-session", process=holder())
    record = obj.get(result["id"])
    record["holder"].update(pid=99999999, start="gone", executable="claude")
    obj.save(record)
    if evidence == "failed-census":
        def census() -> list[dict]:
            raise ValueError("process census unavailable")
    else:
        def census() -> list[dict]:
            return [{"pid": os.getpid(), "executable": "claude",
                     "command": "claude --resume resumed-session" if evidence == "live" else "claude"}]
    monkeypatch.setattr(migration, "claude_processes", census)
    obj.reconcile()
    assert obj.get(result["id"])["holder"] == record["holder"]
    assert obj.get(result["id"])["state"] == "taken"


@pytest.mark.parametrize("file_session, expected", [("other-session", "dead"), ("resumed-session-full-id", "live"),
                                                     (None, "uncertain"), ("stale-start", "uncertain")])
def test_session_file_resolves_claude_without_session_argv(tmp_path: Path, monkeypatch,
                                                           file_session: str | None, expected: str) -> None:
    """Claude Code's sessions/<pid>.json names the session of a process whose argv omits it."""
    import worktree_migration as migration

    if os.name == "nt":
        pytest.skip("POSIX session files")
    # Claude Code writes procStart as ps lstart in UTC, not local time.
    utc_start = subprocess.run(["ps", "-p", str(os.getpid()), "-o", "lstart="], capture_output=True, text=True,
                               check=True, env={**os.environ, "TZ": "UTC"}).stdout.strip()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    if file_session:
        (tmp_path / "sessions").mkdir()
        start = "Thu Jan  1 00:00:00 1970" if file_session == "stale-start" else utc_start
        (tmp_path / "sessions" / f"{os.getpid()}.json").write_text(json.dumps(
            {"pid": os.getpid(), "sessionId": file_session, "procStart": start}))
    monkeypatch.setattr(migration, "claude_processes",
                        lambda: [{"pid": os.getpid(), "executable": "claude", "command": "claude"}])
    assert safety.holder_state({"pid": 99999999, "start": "gone", "executable": "claude",
                                "session": "resumed-session"}) == expected


def test_incomplete_claude_census_keeps_claim(monkeypatch) -> None:
    """A visible Claude executable with unavailable argv is uncertain evidence."""
    if os.name == "nt":
        pytest.skip("POSIX process census")
    def census(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        assert argv[:2] == ["ps", "-axo"]
        output = "4242 /home/test/.local/bin/claude\n" if argv[-1] == "pid=,comm=" else ""
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")
    monkeypatch.setattr(subprocess, "run", census)
    assert safety.holder_state({"pid": 99999999, "start": "gone", "executable": "claude",
                                "session": "still-working"}) != "dead"
