"""``task-kit export``: the stdout contract, error codes, no dashboard import, and no writes.

Revert checks named per test: printing a warning to stdout, importing the
dashboard inventory from the export, and writing a file each turn one of these
red.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from dashboard_support import ENTRYPOINT, REPO_ROOT, tree_digest
from task_export_support import MACHINE, NOW, inject, make_world

from task_system import export, export_cli
from task_system.machine import MachineError


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    return home


def _env(home: Path, **extra: str) -> dict:
    env = {key: value for key, value in os.environ.items() if key not in ("DEVROOT", "CLAUDE_BOOTSTRAP_DATA_ROOT")}
    env.update({"HOME": str(home), "USERPROFILE": str(home), "_BOOTSTRAP_GUARD_SKIP_REEXEC": "1",
                "PYTHONPATH": str(REPO_ROOT / "plugins" / "skills-kit")})  # skills_kit_lib, as the plugin venv links it
    env.update(extra)
    return env


def _cli(argv: list[str], home: Path, *, python_flags: tuple[str, ...] = (), **extra: str) -> subprocess.CompletedProcess:
    """The real entrypoint, as ``bin/task-kit export ...`` runs it."""
    return subprocess.run(
        [sys.executable, *python_flags, str(ENTRYPOINT), "export", *argv],
        capture_output=True, text=True, env=_env(home, **extra), timeout=120,
    )


def _one_document(stdout: str) -> dict:
    """stdout must be exactly one serialized document and its newline -- nothing else."""
    assert stdout.endswith("\n") and stdout.count("\n") == 1, stdout
    doc = json.loads(stdout)
    assert stdout == export_cli.serialize(doc)
    return doc


def _item_argv(parent: Path, checkout: str, task: str, item: str) -> list[str]:
    return ["item", "--format", "json", "--protocol", "1", "--parent", str(parent), "--checkout", checkout,
            "--task", task, "--item", item]


def test_stdout_holds_exactly_one_document_for_every_outcome(world, home):
    # Revert check: a print() to stdout anywhere in the export makes _one_document fail.
    alpha = export.checkout_id(world / "alpha")
    cases = {
        "ok": (["tasks", "--format", "json", "--protocol", "1", "--parent", str(world)], 0),
        "usage": (["tasks", "--format", "json", "--protocol", "1", "--bogus"], 2),
        "unknown_checkout": (_item_argv(world, "f" * 16, "dev/tasks/build-export", "write-schema"), 3),
        "unknown_task": (_item_argv(world, alpha, "dev/tasks/../alpha", "write-schema"), 3),
        "unknown_item": (_item_argv(world, alpha, "dev/tasks/build-export", "nope"), 3),
        "protocol_unsupported": (["item", "--format", "json", "--protocol", "9"], 4),
        "discovery_failed": (["tasks", "--format", "json", "--protocol", "1"], 1),
    }
    for name, (argv, expected) in cases.items():
        done = _cli(argv, home)
        doc = _one_document(done.stdout)
        assert done.returncode == expected, (name, done.stderr)
        if name == "ok":
            assert doc["kind"] == "tasks" and "error" not in doc
        else:
            assert doc["error"]["code"] == name, (name, doc)
            assert done.stderr.strip(), f"{name}: a handled error also says so on stderr"


def test_an_unexpected_exception_is_an_internal_document(world, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(export, "tasks_payload", boom)
    out, err = io.StringIO(), io.StringIO()
    code = export_cli.run(["tasks", "--format", "json", "--protocol", "1", "--parent", str(world)], stdout=out, stderr=err)
    doc = _one_document(out.getvalue())
    assert code == 1 and doc["error"] == {"code": "internal", "message": "RuntimeError: disk on fire"}


def test_a_task_system_that_cannot_load_still_answers_one_document(home):
    # The plugin venv missing (no PyYAML) must not read as "too old" (exit 2, empty stdout).
    script = (
        "import runpy, sys\n"
        "sys.modules['yaml'] = None\n"
        f"sys.argv = [{str(ENTRYPOINT)!r}, 'export', 'protocols', '--format', 'json']\n"
        f"runpy.run_path({str(ENTRYPOINT)!r}, run_name='__main__')\n"
    )
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=_env(home), timeout=60)
    doc = _one_document(done.stdout)
    assert done.returncode == 1 and doc["error"]["code"] == "internal" and doc["protocol"] == 1


def test_protocols_needs_no_discovery(home):
    done = _cli(["protocols", "--format", "json"], home)
    assert done.returncode == 0
    assert _one_document(done.stdout)["payload"] == {"protocols": [1]}


def test_unknown_checkout_task_and_item_are_distinct_codes(world):
    alpha = export.checkout_id(world / "alpha")
    codes = []
    for argv in (
        _item_argv(world, "0" * 16, "dev/tasks/build-export", "write-schema"),
        _item_argv(world, alpha, "dev/tasks/later-idea", "write-schema"),  # alpha-two's task, asked of alpha
        _item_argv(world, alpha, "dev/tasks/build-export", "missing"),
    ):
        code, doc = export_cli.document(argv)
        codes.append((code, doc["error"]["code"]))
    assert codes == [(3, "unknown_checkout"), (3, "unknown_task"), (3, "unknown_item")]


def test_the_export_imports_no_dashboard_module(world, home):
    # Revert check: importing task_system.dashboard.inventory in export.py puts it in the import trace.
    done = _cli(["tasks", "--format", "json", "--protocol", "1", "--parent", str(world)], home,
                python_flags=("-X", "importtime"))
    assert done.returncode == 0, done.stderr
    imported = [line.rsplit("|", 1)[-1].strip() for line in done.stderr.splitlines() if line.startswith("import time:")]
    assert "task_system.export" in imported
    assert [name for name in imported if name.startswith("task_system.dashboard")] == []


def test_the_export_writes_nothing(world, home):
    # Revert check: a write of any file under the task roots or the home directory (a session reconcile,
    # a cache file) changes a digest. The export also runs git with GIT_OPTIONAL_LOCKS=0 so that no
    # index refresh can write; git 2.54 refreshed no index for this fixture either way, so that line is
    # defensive and this test does not prove it.
    alpha = world / "alpha"
    (alpha / ".gitignore").write_text("", encoding="utf-8")  # alpha's tasks are tracked, the usual layout
    for args in (("add", "-A"), ("commit", "-q", "-m", "track the tasks")):
        subprocess.run(["git", "-C", str(alpha), *args], check=True, capture_output=True)
    before = tree_digest(world, home)
    for argv in (
        ["tasks", "--format", "json", "--protocol", "1", "--parent", str(world)],
        _item_argv(world, export.checkout_id(alpha), "dev/tasks/build-export", "write-schema"),
    ):
        done = _cli(argv, home)
        assert done.returncode == 0, done.stderr
    assert tree_digest(world, home) == before


def test_the_revision_moves_exactly_when_a_fact_moves(world):
    argv = ["tasks", "--format", "json", "--protocol", "1", "--parent", str(world)]
    first = export_cli.document(argv, **inject())[1]["payload"]
    again = export_cli.document(argv, **inject())[1]["payload"]
    assert first["revision"] == again["revision"]
    log = world / "alpha" / "dev" / "tasks" / "build-export" / "log.md"
    log.write_text(log.read_text(encoding="utf-8") + "- 2026-10-22 16:00: Another entry.\n", encoding="utf-8")
    moved = export_cli.document(argv, **inject())[1]["payload"]
    assert moved["revision"] != first["revision"]
    task = lambda payload: next(t for t in payload["tasks"] if t["stub"] == "build-export")  # noqa: E731
    assert task(moved)["revision"] != task(first)["revision"]
    assert task(moved)["updates_total"] == 4 and task(moved)["last_activity"] == "2026-10-22 16:00"


def test_updates_keep_the_newest_twenty(world):
    log = world / "alpha" / "dev" / "tasks" / "wait-on-review" / "log.md"
    log.write_text("# Log\n\n" + "".join(f"- 2026-09-{day:02d} 10:00: Entry {day}.\n" for day in range(1, 26)),
                   encoding="utf-8")
    payload = export.tasks_payload((world,), resolve_machine=lambda: MACHINE, now=lambda: NOW)
    task = next(t for t in payload["tasks"] if t["stub"] == "wait-on-review")
    assert task["updates_total"] == 25 and len(task["updates"]) == export.MAX_UPDATES
    assert task["updates"][0]["detail"] == "Entry 25." and task["updates"][-1]["detail"] == "Entry 6."


def test_an_unidentifiable_machine_is_an_export_error_not_a_failure(world):
    def no_machine():
        raise MachineError("no machine id")

    payload = export.tasks_payload((world,), resolve_machine=no_machine, now=lambda: NOW)
    assert [error["code"] for error in payload["errors"]] == ["sessions"]
    assert all(task["sessions"] == [] for task in payload["tasks"])


def test_a_long_plan_section_is_truncated(world):
    plan = world / "alpha" / "dev" / "tasks" / "build-export" / "plan.md"
    plan.write_text(plan.read_text(encoding="utf-8") + "\n### write-cli -- Write the CLI\n\n" + "x" * 9000 + "\n",
                    encoding="utf-8")
    payload = export.item_payload(export.checkout_id(world / "alpha"), "dev/tasks/build-export", "write-cli", (world,))
    assert len(payload["plan_section"]) == export.PLAN_SECTION_LIMIT and payload["plan_section_truncated"] is True

