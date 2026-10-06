"""The elevated fix queue never keeps or runs an env fix the manifest no longer
declares.

The 2026-10-06 incident: an `env_check` whose fix re-enabled a network filter
that had just crashed the machine was deleted from env.json, but its queued
fix survived under three origins. `--fix-all` launches the whole merged queue
and the hand-run `bootstrap-fix` shim reads queue.json as last written, so
either path would have staged the harmful fix.

One predicate owns correctness (bootstrap_lib.queue_records): an `env_check:`
or `symlink:` record is stale when its origin's layered env.json no longer
declares the entry, or declares it with a different content fingerprint
(`entry_sha256`), or when the record carries no fingerprint at all. The queue
rewrite prunes stale records from every other origin, and the runner refuses
to execute them.

Fingerprints are computed HERE with a test-local copy of the canonical form,
so these tests state the contract independently of the implementation.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import bootstrap_lib.fix_queue as fq
import bootstrap_lib.fix_runner as fr
from bootstrap_lib.fix_queue import FixTask


ROLLBACK = "system-instability-network-rollback"
ROLLBACK_ENTRY = {
    "name": ROLLBACK,
    "check": "bash ~/.claude/scripts/env/restore-network-baseline.ps1 check",
    "fix": "bash ~/.claude/scripts/env/restore-network-baseline.ps1 stage",
    "elevated": True,
}


def _fp(entry):
    canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _env_record(entry, origin, fingerprint=True, section="env_check"):
    record = {
        "id": f"{section}:{entry['name']}",
        "kind": "command",
        "label": entry["name"],
        "origin": origin,
        "elevated": True,
        "command": entry.get("fix") or entry.get("command") or "ln -s a b",
    }
    if fingerprint:
        record["entry_sha256"] = _fp(entry)
    return record


def _seed_queue(data_dir, records, current_os="ubuntu"):
    qpath = fq.queue_path(str(data_dir))
    os.makedirs(os.path.dirname(qpath), exist_ok=True)
    with open(qpath, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "os": current_os, "bash": "/usr/bin/bash",
                   "tasks": records}, fh)
    return qpath


def _write_json(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content))


@pytest.fixture(autouse=True)
def _stub_bash(monkeypatch):
    monkeypatch.setattr(fq, "resolve_bash", lambda: "/usr/bin/bash")


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture
def origins(tmp_path, home):
    """The incident's three origins: the ~/.claude dir and two projects."""
    a = tmp_path / "DJEwork"
    b = tmp_path / "env-config"
    a.mkdir()
    b.mkdir()
    return [str(home / ".claude"), str(a), str(b)]


def _user_manifest(home, env_checks):
    _write_json(home / ".claude" / "env.json",
                {"machines": {"h": {"os": "windows"}}, "env_checks": env_checks})


def _run_runner(monkeypatch, qpath):
    """Run fix_runner.main in-process; return (exit code, dispatched ids)."""
    dispatched = []
    monkeypatch.setattr(fr.Runner, "dispatch",
                        lambda self, task: dispatched.append(task.get("id")) or True)
    monkeypatch.setattr(fr, "wait_for_key", lambda prompt: None)
    code = fr.main([qpath])
    return code, dispatched


def _queue_ids(data_dir):
    qpath = fq.queue_path(str(data_dir))
    if not os.path.exists(qpath):
        return None
    with open(qpath, encoding="utf-8") as fh:
        return [(t["id"], t["origin"]) for t in json.load(fh)["tasks"]]


# --------------------------------------------------------------------------- #
# 1. The incident
# --------------------------------------------------------------------------- #

class TestIncident:
    @pytest.mark.parametrize("writer", [0, 1, 2, "other"])
    def test_deleted_check_is_gone_from_every_origin_after_one_rewrite(
            self, tmp_path, home, origins, writer):
        """The check was deleted from the user env.json. One rewrite from ANY
        origin -- one of the three, or a fourth project -- leaves none of the
        three records behind."""
        _user_manifest(home, [])   # the rollback check was deleted
        data = tmp_path / "data"
        _seed_queue(data, [_env_record(ROLLBACK_ENTRY, o) for o in origins])
        writer_origin = (str(tmp_path / "spiritcrossing") if writer == "other"
                         else origins[writer])

        fq.write_or_clear_queue([], str(data), "ubuntu", origin=writer_origin)

        assert _queue_ids(data) is None

    def test_runner_does_not_run_the_deleted_check(
            self, tmp_path, home, origins, monkeypatch, capsys):
        """The shim path: no rewrite happens, the runner reads queue.json as
        last written. It must refuse every record and say why."""
        _user_manifest(home, [])
        qpath = _seed_queue(tmp_path / "data",
                            [_env_record(ROLLBACK_ENTRY, o) for o in origins])

        code, dispatched = _run_runner(monkeypatch, qpath)

        assert dispatched == []
        assert code == getattr(fr, "EXIT_STALE_BLOCKED", "missing")
        assert code != fr.EXIT_TASK_FAILED
        out = capsys.readouterr().out
        assert f"env_check:{ROLLBACK}" in out


# --------------------------------------------------------------------------- #
# 2. Changed entries; 3. legacy records
# --------------------------------------------------------------------------- #

class TestChangedAndLegacy:
    def test_changed_check_with_unchanged_fix_is_pruned(self, tmp_path, home):
        queued = {"name": "x", "check": "old-check", "fix": "same-fix",
                  "elevated": True}
        _user_manifest(home, [dict(queued, check="new-check")])
        data = tmp_path / "data"
        _seed_queue(data, [_env_record(queued, "/project-a")])

        fq.write_or_clear_queue([], str(data), "ubuntu", origin="/project-b")

        assert _queue_ids(data) is None

    def test_changed_fix_is_pruned(self, tmp_path, home):
        queued = {"name": "x", "check": "c", "fix": "old-fix", "elevated": True}
        _user_manifest(home, [dict(queued, fix="new-fix")])
        data = tmp_path / "data"
        _seed_queue(data, [_env_record(queued, "/project-a")])

        fq.write_or_clear_queue([], str(data), "ubuntu", origin="/project-b")

        assert _queue_ids(data) is None

    def test_unchanged_entry_is_kept(self, tmp_path, home):
        """Control: the predicate does not prune a record that still matches."""
        entry = {"name": "x", "check": "c", "fix": "f", "elevated": True}
        _user_manifest(home, [entry])
        data = tmp_path / "data"
        _seed_queue(data, [_env_record(entry, "/project-a")])

        fq.write_or_clear_queue([], str(data), "ubuntu", origin="/project-b")

        assert _queue_ids(data) == [("env_check:x", "/project-a")]

    def test_legacy_record_without_fingerprint_is_stale(self, tmp_path, home):
        """A record from an engine that wrote no fingerprint cannot be shown to
        match the manifest, so it is pruned even though its id is declared."""
        entry = {"name": "x", "check": "c", "fix": "f", "elevated": True}
        _user_manifest(home, [entry])
        data = tmp_path / "data"
        _seed_queue(data, [_env_record(entry, "/project-a", fingerprint=False)])

        fq.write_or_clear_queue([], str(data), "ubuntu", origin="/project-b")

        assert _queue_ids(data) is None

    def test_deleted_symlink_is_pruned(self, tmp_path, home):
        link = {"name": "starship", "source": "a", "target": "b"}
        _write_json(home / ".claude" / "env.json",
                    {"machines": {"h": {"os": "windows"}}, "symlinks": []})
        data = tmp_path / "data"
        _seed_queue(data, [_env_record(link, "/project-a", section="symlink")])

        fq.write_or_clear_queue([], str(data), "ubuntu", origin="/project-b")

        assert _queue_ids(data) is None

    def test_project_only_check_is_judged_against_its_own_origin(
            self, tmp_path, home):
        """A check declared only in project A's layer stays queued for A, and
        the same record under an origin that does not declare it is pruned."""
        entry = {"name": "proj", "check": "c", "fix": "f", "elevated": True}
        _user_manifest(home, [])
        project_a = tmp_path / "a"
        _write_json(project_a / ".claude" / "env.json", {"env_checks": [entry]})
        data = tmp_path / "data"
        _seed_queue(data, [_env_record(entry, str(project_a)),
                           _env_record(entry, str(tmp_path / "b"))])

        fq.write_or_clear_queue([], str(data), "ubuntu",
                                origin=str(tmp_path / "c"))

        assert _queue_ids(data) == [("env_check:proj", str(project_a))]


# --------------------------------------------------------------------------- #
# 4. The shim path, run as a real script
# --------------------------------------------------------------------------- #

def _bash():
    """A POSIX bash (Git Bash on Windows, never the WSL/System32 launcher)."""
    import shutil
    candidates = []
    if os.name == "nt":
        candidates += [r"C:\Program Files\Git\usr\bin\bash.exe",
                       r"C:\Program Files\Git\bin\bash.exe"]
    candidates.append(shutil.which("bash"))
    for c in candidates:
        if c and Path(c).exists() and "WindowsApps" not in c \
                and "System32" not in c:
            return c
    pytest.skip("no POSIX bash available")


class TestShimPath:
    def test_runner_script_blocks_stale_entry_and_runs_the_rest(self, tmp_path):
        """`python fix_runner.py <queue>` -- the shim's exact invocation, with no
        package context -- must refuse the stale env record, name it, still run
        the unrelated tool task, and exit with the stale-block code."""
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        _write_json(home / ".claude" / "env.json",
                    {"machines": {"h": {"os": "windows"}}, "env_checks": []})
        stale_marker = tmp_path / "stale-ran"
        tool_marker = tmp_path / "tool-ran"
        stale = _env_record(ROLLBACK_ENTRY, str(tmp_path / "proj"))
        stale["command"] = f"touch '{stale_marker.as_posix()}'"
        stale["elevated"] = False
        tool = {"id": "tool:x", "kind": "command", "label": "Install x",
                "origin": str(tmp_path / "proj"),
                "command": f"touch '{tool_marker.as_posix()}'"}
        qpath = tmp_path / "queue.json"
        qpath.write_text(json.dumps({
            "version": 1, "os": "ubuntu", "bash": _bash(),
            "tasks": [stale, tool]}))
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))

        proc = subprocess.run(
            [sys.executable, fr.__file__, str(qpath)],
            input="\n\n", capture_output=True, text=True, timeout=120, env=env)

        assert "Traceback" not in proc.stderr, proc.stderr
        assert tool_marker.exists(), proc.stdout
        assert not stale_marker.exists()
        assert f"env_check:{ROLLBACK}" in proc.stdout
        assert proc.returncode == getattr(fr, "EXIT_STALE_BLOCKED", "missing")
        assert proc.returncode not in (fr.EXIT_OK, fr.EXIT_TASK_FAILED)


# --------------------------------------------------------------------------- #
# 5. Parse errors fail closed per entry, not per origin
# --------------------------------------------------------------------------- #

class TestParseErrors:
    def test_only_entries_declared_solely_in_the_broken_layer_are_blocked(
            self, tmp_path, home, monkeypatch, capsys):
        user_entry = {"name": "user-check", "check": "c", "fix": "f",
                      "elevated": True}
        project_entry = {"name": "project-check", "check": "c", "fix": "g",
                         "elevated": True}
        _user_manifest(home, [user_entry])
        project = tmp_path / "proj"
        broken = project / ".claude" / "env.json"
        broken.parent.mkdir(parents=True)
        broken.write_text("{not json")
        qpath = _seed_queue(tmp_path / "data", [
            _env_record(user_entry, str(project)),
            _env_record(project_entry, str(project)),
        ])

        code, dispatched = _run_runner(monkeypatch, qpath)

        assert dispatched == ["env_check:user-check"]
        out = capsys.readouterr().out
        assert "env_check:project-check" in out
        assert str(broken) in out
        assert code == getattr(fr, "EXIT_STALE_BLOCKED", "missing")

    def test_rewrite_keeps_the_declared_entry_and_prunes_the_other(
            self, tmp_path, home):
        user_entry = {"name": "user-check", "check": "c", "fix": "f",
                      "elevated": True}
        project_entry = {"name": "project-check", "check": "c", "fix": "g",
                         "elevated": True}
        _user_manifest(home, [user_entry])
        project = tmp_path / "proj"
        (project / ".claude").mkdir(parents=True)
        (project / ".claude" / "env.json").write_text("{not json")
        data = tmp_path / "data"
        _seed_queue(data, [_env_record(user_entry, str(project)),
                           _env_record(project_entry, str(project))])

        fq.write_or_clear_queue([], str(data), "ubuntu",
                                origin=str(tmp_path / "other"))

        assert _queue_ids(data) == [("env_check:user-check", str(project))]


# --------------------------------------------------------------------------- #
# 8. Cross-origin duplicates: separate on disk, run once
# --------------------------------------------------------------------------- #

class TestCrossOriginDuplicates:
    def test_identical_records_stay_separate_in_queue_json(self, tmp_path):
        """Each origin's copy is validated against that origin's own layers, so
        the file keeps both; deduplication happens only in what runs."""
        task = FixTask(id="tool:x", kind="command", label="Install x",
                       command="scoop install x", elevated=True)
        fq.write_or_clear_queue([task], str(tmp_path), "ubuntu",
                                origin="/project-a")
        fq.write_or_clear_queue([task], str(tmp_path), "ubuntu",
                                origin="/project-b")

        assert sorted(_queue_ids(tmp_path)) == [
            ("tool:x", "/project-a"), ("tool:x", "/project-b")]

    def test_identical_records_run_once(self, tmp_path, home, monkeypatch):
        entry = {"name": "x", "check": "c", "fix": "f", "elevated": True}
        _user_manifest(home, [entry])
        qpath = _seed_queue(tmp_path / "data", [
            _env_record(entry, "/project-a"), _env_record(entry, "/project-b")])

        code, dispatched = _run_runner(monkeypatch, qpath)

        assert dispatched == ["env_check:x"]
        assert code == fr.EXIT_OK

    def test_budget_and_disclosure_view_counts_the_operation_once(
            self, tmp_path):
        task = FixTask(id="tool:x", kind="command", label="Install x",
                       command="scoop install x", elevated=True, timeout=1000)
        fq.write_or_clear_queue([task], str(tmp_path), "ubuntu",
                                origin="/project-a")
        path = fq.write_or_clear_queue([task], str(tmp_path), "ubuntu",
                                       origin="/project-b")

        assert [t.id for t in fq.load_queue_tasks(path)] == ["tool:x"]


# --------------------------------------------------------------------------- #
# 9. Pruning to empty still clears the runnable artifacts
# --------------------------------------------------------------------------- #

class TestPrunedToEmpty:
    def test_queue_and_shim_are_deleted(self, tmp_path, home):
        _user_manifest(home, [])
        data = tmp_path / "data"
        qpath = _seed_queue(data, [_env_record(ROLLBACK_ENTRY, "/project-a")],
                            current_os="windows")
        spath = fq.shim_path(str(data), "windows")
        Path(spath).write_text("@echo off\r\n")

        result = fq.write_or_clear_queue([], str(data), "windows",
                                         origin="/project-b")

        assert result is None
        assert not os.path.exists(qpath)
        assert not os.path.exists(spath)
