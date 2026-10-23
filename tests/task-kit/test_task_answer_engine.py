"""The answer engine: one pass over a task repository (summary-on-commit-plan.md D4 steps 1-4, D7, the U4 checks).

Every test runs against scratch git repositories (a tasks repository cloned from a bare remote on local disk) and a
fake ``declaration.run``; the real ``llm_kit`` library supplies the result types only. No model is called.

Revert checks (each named test goes red when the named change is made to ``answer_engine.py``; recorded in the
unit's report):
- no modification order (``sorted`` by path) -> ``test_a_batch_answers_the_most_recently_modified_tasks_first``
- the repository lock held across ``_answer`` -> ``test_the_publisher_takes_the_lock_while_a_model_call_blocks``
- the fingerprint revalidation removed from the commit step -> ``test_a_task_edited_and_committed_during_answering_is_dropped_as_superseded``
- the dirty own files committed only when work is pending (freshness first) -> ``test_a_pass_after_a_failed_commit_commits_the_dirty_file_first``
- the whole index committed instead of ``--only`` paths -> ``test_a_separately_staged_path_stays_staged``
- ``REBASE_HEAD`` and ``sequencer`` dropped from ``GIT_OPERATION_PATHS`` -> ``test_a_git_operation_in_progress_skips_the_repository``
- a current record of any host counted as pending -> ``test_another_hosts_current_answer_is_not_recomputed``
- stop requests ignored (both stop checks) -> ``test_a_stop_request_ends_the_pass_between_phases``
- the deadline checks removed -> ``test_the_deadline_ends_the_answer_phase``
- the working-copy ownership test dropped from ``own_dirty`` -> ``test_a_file_named_for_this_host_with_another_fingerprint_is_not_committed``
"""

from __future__ import annotations

import datetime
import json
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from git_remote_support import clone, git, isolate_git_environment, make_remote, remote_files, write

from task_system import answer_engine, answer_store, questions, session_publish
from task_system.answer_engine import Budget, TaskTarget
from task_system.answer_store import AsOf, NewAnswer
from task_system.answers_config import AnswersConfig
from task_system.answers_types import Answerer
from task_system.discovery import read_task_block
from task_system.machine import Machine

LLM_KIT_LIB = (Path(__file__).resolve().parents[2] / "packages" / "katsoft-llm-kit" / "src").resolve()
SHIPPED_QUESTIONS = (
    Path(__file__).resolve().parents[2] / "plugins" / "task-kit" / "skills" / "task" / "questions"
)

ME = Machine(host="studio", fingerprint="3f9a0c1d2e4b5a6c", os_instance="macos")
OTHER = Machine(host="laptop", fingerprint="9999999999999999", os_instance="linux")
CONFIG = AnswersConfig(models=("qwen",), effort="medium", sources=())
NOW = datetime.datetime(2026, 10, 22, 12, 0)

ORIENTATION = {"headline": "A headline", "state": "Where it stands", "next": "The next action"}


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch, tmp_path):
    isolate_git_environment(monkeypatch, tmp_path)


@pytest.fixture
def llm(monkeypatch):
    """The real llm-kit library on sys.path for this test (the autouse conftest fixture hides it otherwise)."""
    monkeypatch.syspath_prepend(str(LLM_KIT_LIB))
    from llm_kit.dispatch import declaration  # noqa: PLC0415

    return SimpleNamespace(declaration=declaration)


class FakeRun:
    """A ``declaration.run`` stand-in: every call completes on entry ``qwen`` with a valid answer.

    ``on_call(request)`` runs inside the call (blocking, or editing the task, as a test needs). ``fail`` makes
    every call a task error. The answer depends on the question: the prompt of ``orientation`` names ``headline``.
    """

    def __init__(self, llm, on_call=None, fail: str | None = None):
        self.d = llm.declaration
        self.on_call = on_call
        self.fail = fail
        self.prompts: list[str] = []
        self.allows: list[set] = []
        self._lock = threading.Lock()

    @property
    def calls(self) -> int:
        return len(self.prompts)

    def __call__(self, names, request, *, project_root, requirements, exclude, max_attempts, reachability_cache, allow):
        with self._lock:
            self.prompts.append(request.prompt)
            self.allows.append(set(allow))
        if self.on_call is not None:
            self.on_call(request)
        d = self.d
        candidates = [name for name in names if name not in exclude]
        if not candidates:
            raise d.NoUsableRoutingTarget(names, [], "process")
        entry = candidates[0]
        if self.fail is not None:
            attempt = d.Attempt(entry, 1, None, error=self.fail, outcome="failed")
            return d.RunResult(d.RUN_FAILED, entry, None, (attempt,), f"task error: {self.fail}")
        body = ORIENTATION if "headline" in request.system else {"decisions": []}
        attempt = d.Attempt(entry, 1, None, outcome="completed")
        return d.RunResult(d.RUN_COMPLETED, entry, SimpleNamespace(text=json.dumps(body)), (attempt,))


class NeverRun:
    """A run that fails the test when a model is called."""

    calls = 0

    def __call__(self, *args, **kwargs):
        raise AssertionError("a model was called")


TASK_YAML = "task:\n  _schema_version: '1'\n  type: hand-off\n  title: {stub}\n  status: active\n"


@dataclass
class World:
    tmp_path: Path
    remote: Path
    top: Path
    data: Path
    catalog: questions.Catalog

    def folder(self, stub: str) -> Path:
        return self.top / "proj" / stub

    def targets(self, stubs=None, status="active") -> list[TaskTarget]:
        names = stubs if stubs is not None else sorted(p.name for p in (self.top / "proj").iterdir() if p.is_dir())
        return [TaskTarget(self.folder(stub), status) for stub in names]

    def pass_(self, run, *, allow=("local",), config=CONFIG, machine=ME, **kwargs):
        kwargs.setdefault("budget", Budget(parallel=1))
        return answer_engine.run_pass(
            self.top, machine, config, targets=kwargs.pop("targets", self.targets()), catalog=self.catalog,
            allow=list(allow), run=run, data_dir=self.data, now=lambda: NOW, **kwargs,
        )

    def edit(self, stub: str, text: str, *, commit: bool) -> None:
        write(self.top, f"proj/{stub}/plan.md", text)
        if commit:
            git(self.top, "add", "--", f"proj/{stub}/plan.md")
            git(self.top, "commit", "-q", "-m", f"edit {stub}", "--", f"proj/{stub}/plan.md")

    def snapshot(self) -> tuple[str, str]:
        return git(self.top, "status", "--porcelain=v1", "--untracked-files=all").stdout, git(self.top, "rev-parse", "HEAD").stdout


def add_task(top: Path, stub: str) -> None:
    write(top, f"proj/{stub}/task.yaml", TASK_YAML.format(stub=stub))
    write(top, f"proj/{stub}/plan.md", f"# Plan for task-{stub}-marker\n")
    write(top, f"proj/{stub}/CLAUDE.md", f"Notes of {stub}.\n")
    write(top, f"proj/{stub}/log.md", f"# Log\n\n- 2026-10-01: created {stub}\n")
    git(top, "add", "--", f"proj/{stub}")
    git(top, "commit", "-q", "-m", f"add {stub}")


def single_question_catalog(tmp_path: Path) -> questions.Catalog:
    directory = tmp_path / "questions"
    directory.mkdir()
    shutil.copy(SHIPPED_QUESTIONS / "orientation.yaml", directory / "orientation.yaml")
    (directory / "catalog.yaml").write_text(
        '_schema_version: "1"\nversion: 1\nquestions:\n  - orientation\n', encoding="utf-8"
    )
    return questions.load_catalog(directory)


@pytest.fixture
def world(tmp_path) -> World:
    remote = make_remote(tmp_path)
    top = clone(tmp_path, remote, "tasks")
    for stub in ("a", "b", "c"):  # committed in this order: c is the most recently modified
        add_task(top, stub)
    git(top, "push", "-q", "origin", "HEAD:main")
    return World(tmp_path, remote, top, tmp_path / "data", single_question_catalog(tmp_path))


def answered_tasks(report) -> set[str]:
    return {item.task for item in report.answered}


def new_answer(folder: Path, catalog, *, fingerprint: str | None = None) -> NewAnswer:
    question = catalog.get("orientation")
    return NewAnswer(
        question_version=question.version, answer=ORIENTATION,
        inputs_fingerprint=fingerprint or answer_store.question_fingerprint(folder, read_task_block(folder), question),
        as_of=AsOf(task_commit="a" * 40, code_commit=None),
        answerer=Answerer(kind="model", entry="qwen", name=None, tier="local", effort="medium",
                          attempts=(("qwen", "completed"),)),
        generated_at="2026-10-22T09:00:00-05:00",
    )


# --- the record format (U2) against the publisher (U5) ----------------------------------------------------------


@pytest.mark.parametrize(
    "fingerprint", ["3f9a0c1d2e4b5a6c", "1234567890123456", "12345e6789012345", "0000000000000000", "0e12345678901234"]
)
def test_a_record_written_by_the_store_is_published_as_the_machines_own(world, fingerprint):
    machine = Machine("studio", fingerprint, "macos")
    folder = world.folder("b")
    path = answer_store.write_answer(folder, "orientation", machine, new_answer(folder, world.catalog))
    text = path.read_text(encoding="utf-8")
    assert any(line.startswith("machine:") for line in text.splitlines())  # a column-0 machine line
    assert session_publish.answer_record_machine(text) == fingerprint
    assert session_publish.pending(world.top, machine) == ()  # an uncommitted answer is the engine's to commit
    relative = path.relative_to(world.top).as_posix()
    committed, error, _notes = answer_engine.commit_own_dirty(world.top, machine)
    assert (committed, error) == ([relative], None)
    assert session_publish.pending(world.top, machine) == (relative,)
    result = session_publish.publish_holding_lock(world.top, machine)
    assert result.status == session_publish.PUBLISHED, result.message
    assert relative in remote_files(world.remote)


def test_a_file_named_for_this_host_with_another_fingerprint_is_not_committed(world):
    foreign = Machine("studio", "aaaaaaaaaaaaaaaa", "linux")
    folder = world.folder("b")
    path = answer_store.write_answer(folder, "orientation", foreign, new_answer(folder, world.catalog))
    committed, error, notes = answer_engine.commit_own_dirty(world.top, ME)
    assert (committed, error) == ([], None)
    assert any("not this machine's" in note for note in notes)
    assert git(world.top, "status", "--porcelain").stdout.strip().endswith("proj/b/answers/")  # still untracked
    assert path.exists()


@pytest.mark.parametrize("who", [Machine("studio", "aaaaaaaaaaaaaaaa", "linux"), OTHER])
def test_the_publisher_refuses_a_committed_file_that_is_not_this_machines(world, who):
    folder = world.folder("b")
    path = answer_store.write_answer(folder, "orientation", who, new_answer(folder, world.catalog))
    relative = path.relative_to(world.top).as_posix()
    git(world.top, "add", "--", relative)
    git(world.top, "commit", "-q", "-m", "someone else's file", "--", relative)
    assert session_publish.pending(world.top, ME) == ()
    assert session_publish.publish_holding_lock(world.top, ME).status == session_publish.NOTHING_PENDING
    # with one of this machine's own files waiting, the foreign commit stops the push of both
    own = answer_store.write_answer(world.folder("c"), "orientation", ME, new_answer(world.folder("c"), world.catalog))
    answer_engine.commit_own_dirty(world.top, ME)
    result = session_publish.publish_holding_lock(world.top, ME)
    assert result.status == session_publish.FOREIGN_LOCAL
    assert not {relative, own.relative_to(world.top).as_posix()} & remote_files(world.remote)


# --- one pass -----------------------------------------------------------------------------------------------------


def test_a_pass_answers_commits_and_publishes_this_machines_files(world, llm):
    run = FakeRun(llm)
    report = world.pass_(run, budget=Budget(batch_size=4, parallel=1))
    assert report.outcome == "completed" and report.pending == 3
    assert answered_tasks(report) == {"proj/a", "proj/b", "proj/c"}
    assert report.commit and report.commit_error is None
    assert [item["status"] for item in report.publish] == ["nothing-pending", "published"]
    for stub in ("a", "b", "c"):
        relative = f"proj/{stub}/answers/orientation/studio.yaml"
        assert relative in remote_files(world.remote)
        assert git(world.top, "log", "-1", "--format=%s", "--", relative).stdout.strip() == "task-kit answers: studio"
        record = (world.top / relative).read_text(encoding="utf-8")
        assert session_publish.answer_record_machine(record) == ME.fingerprint
    assert git(world.top, "status", "--porcelain").stdout == ""
    assert report.unpublished == 0
    assert run.allows == [{"local"}] * 3  # a local-only caller dispatches in one phase
    # the next pass finds everything current and calls no model
    again = world.pass_(NeverRun())
    assert (again.outcome, again.pending, again.answered) == ("completed", 0, ())


def test_a_pass_answers_each_applicable_question_of_the_shipped_catalog(world, llm):
    catalog = questions.load_catalog(questions.default_shipped_dir())
    world.catalog = catalog
    run = FakeRun(llm)
    report = world.pass_(run, targets=world.targets(["a"]))
    assert {(item.task, item.question) for item in report.answered} == {
        ("proj/a", "orientation"), ("proj/a", "decisions"),
    }  # activity and task_commits are script questions: never a model call
    assert run.calls == 2


def test_a_batch_answers_the_most_recently_modified_tasks_first(world, llm):
    run = FakeRun(llm)
    report = world.pass_(run, budget=Budget(batch_size=2, parallel=1))
    assert answered_tasks(report) == {"proj/c", "proj/b"}
    assert report.kept == ("proj/c", "proj/b") and report.pending == 3
    assert not (world.folder("a") / "answers").exists()
    # the next pass takes the one that waited
    assert answered_tasks(world.pass_(FakeRun(llm), budget=Budget(batch_size=2, parallel=1))) == {"proj/a"}


def test_the_publisher_takes_the_lock_while_a_model_call_blocks(world, llm):
    entered, release = threading.Event(), threading.Event()

    def block(_request):
        entered.set()
        assert release.wait(60)

    holder: dict = {}
    worker = threading.Thread(
        target=lambda: holder.update(report=world.pass_(FakeRun(llm, on_call=block), targets=world.targets(["c"]))),
        daemon=True,
    )
    worker.start()
    try:
        assert entered.wait(60), "the pass never reached the model call"
        result = session_publish.publish(world.top, ME, data_dir=world.data, lock_wait_s=3)
        assert result.status != session_publish.BUSY, result.message
    finally:
        release.set()
        worker.join(60)
    assert answered_tasks(holder["report"]) == {"proj/c"}


def test_a_task_edited_and_committed_during_answering_is_dropped_as_superseded(world, llm):
    def edit_b(request):
        if "task-b-marker" in request.prompt:
            world.edit("b", "# Plan for task-b-marker\nchanged while the model was answering\n", commit=True)

    report = world.pass_(FakeRun(llm, on_call=edit_b), budget=Budget(batch_size=4, parallel=1))
    assert answered_tasks(report) == {"proj/a", "proj/c"}
    [dropped] = report.dropped
    assert (dropped.task, dropped.question) == ("proj/b", "orientation") and "superseded" in dropped.reason
    assert not (world.folder("b") / "answers").exists()
    # the edit is picked up by the next pass
    assert answered_tasks(world.pass_(FakeRun(llm))) == {"proj/b"}


def test_a_task_edited_but_not_committed_during_answering_is_dropped(world, llm):
    def edit_b(request):
        if "task-b-marker" in request.prompt:
            world.edit("b", "# Plan for task-b-marker\nuncommitted edit\n", commit=False)

    report = world.pass_(FakeRun(llm, on_call=edit_b), budget=Budget(batch_size=4, parallel=1))
    assert answered_tasks(report) == {"proj/a", "proj/c"}
    assert [(d.task, "uncommitted" in d.reason or "superseded" in d.reason) for d in report.dropped] == [("proj/b", True)]


def test_a_task_with_uncommitted_changes_is_skipped_and_named(world):
    world.edit("b", "# Plan for task-b-marker\nnot committed\n", commit=False)
    report = world.pass_(NeverRun(), targets=world.targets(["b"]))
    assert report.pending == 0 and report.skipped_tasks == (("proj/b", "the task folder has uncommitted changes outside answers/"),)


def test_a_pass_after_a_failed_commit_commits_the_dirty_file_first(world, llm):
    hook = world.top / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\necho refused >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    first = world.pass_(FakeRun(llm), targets=world.targets(["c"]))
    assert answered_tasks(first) == {"proj/c"} and first.commit is None and "refused" in first.commit_error
    relative = "proj/c/answers/orientation/studio.yaml"
    assert (world.top / relative).exists()
    assert git(world.top, "status", "--porcelain", "--", relative).stdout[:2] in ("??", "A ")  # written, not committed
    hook.unlink()
    second = world.pass_(NeverRun(), targets=world.targets(["c"]))
    assert second.committed_dirty == (relative,) and second.answered == () and second.pending == 0
    assert git(world.top, "log", "-1", "--format=%s", "--", relative).stdout.strip() == "task-kit answers: studio"
    assert git(world.top, "status", "--porcelain").stdout == ""
    assert relative in remote_files(world.remote)  # and published


def test_a_separately_staged_path_stays_staged(world, llm):
    write(world.top, "README.md", "tasks repo\nstaged by another session\n")
    git(world.top, "add", "--", "README.md")
    report = world.pass_(FakeRun(llm), targets=world.targets(["c"]))
    assert report.commit and answered_tasks(report) == {"proj/c"}
    assert git(world.top, "diff", "--cached", "--name-only").stdout.split() == ["README.md"]
    committed = git(world.top, "show", "--name-only", "--format=", report.commit).stdout.split()
    assert committed == ["proj/c/answers/orientation/studio.yaml"]


@pytest.mark.parametrize("marker", [("REBASE_HEAD", "file"), ("sequencer", "dir"), ("MERGE_HEAD", "file"),
                                    ("rebase-merge", "dir"), ("rebase-apply", "dir"),
                                    ("CHERRY_PICK_HEAD", "file"), ("REVERT_HEAD", "file")])
def test_a_git_operation_in_progress_skips_the_repository(world, marker):
    name, kind = marker
    path = world.top / ".git" / name
    path.mkdir() if kind == "dir" else path.write_text(git(world.top, "rev-parse", "HEAD").stdout, encoding="utf-8")
    before = world.snapshot()
    report = world.pass_(NeverRun())
    assert report.outcome == "skipped" and name in report.reason
    assert world.snapshot() == before and not world.data.exists()  # nothing touched, no lock taken


def test_a_detached_head_skips_the_repository(world):
    git(world.top, "checkout", "-q", "--detach")
    report = world.pass_(NeverRun())
    assert report.outcome == "skipped" and "detached" in report.reason


def test_another_hosts_current_answer_is_not_recomputed(world, llm):
    folder = world.folder("b")
    path = answer_store.write_answer(folder, "orientation", OTHER, new_answer(folder, world.catalog))
    relative = path.relative_to(world.top).as_posix()
    git(world.top, "add", "--", relative)
    git(world.top, "commit", "-q", "-m", "laptop answered b", "--", relative)
    git(world.top, "push", "-q", "origin", "HEAD:main")
    run = FakeRun(llm)
    report = world.pass_(run, budget=Budget(batch_size=4, parallel=1))
    assert answered_tasks(report) == {"proj/a", "proj/c"} and run.calls == 2
    assert not (folder / "answers" / "orientation" / "studio.yaml").exists()


def test_a_stale_answer_of_another_host_is_recomputed(world, llm):
    folder = world.folder("b")
    old = new_answer(folder, world.catalog, fingerprint="b" * 64)
    path = answer_store.write_answer(folder, "orientation", OTHER, old)
    relative = path.relative_to(world.top).as_posix()
    git(world.top, "add", "--", relative)
    git(world.top, "commit", "-q", "-m", "laptop answered b long ago", "--", relative)
    report = world.pass_(FakeRun(llm), targets=world.targets(["b"]))
    assert answered_tasks(report) == {"proj/b"}


def test_a_stop_request_ends_the_pass_between_phases(world):
    stop = {"on": False}
    report = world.pass_(
        NeverRun(), should_stop=lambda: stop["on"],
        on_phase=lambda name: stop.update(on=True) if name == "prepare" else None,
    )
    assert report.outcome == "stopped" and report.stop == "stop" and report.stopped_before == "answer"
    assert len(report.unattempted) == 3 and report.answered == ()
    assert not (world.folder("c") / "answers").exists()


def test_a_stop_request_before_the_pass_touches_nothing(world):
    before = world.snapshot()
    report = world.pass_(NeverRun(), should_stop=lambda: True)
    assert (report.outcome, report.stopped_before) == ("stopped", "prepare")
    assert world.snapshot() == before and not world.data.exists()


def test_a_stop_during_answering_keeps_the_finished_answers(world, llm):
    stop = {"on": False}
    run = FakeRun(llm, on_call=lambda _request: stop.update(on=True))
    report = world.pass_(run, should_stop=lambda: stop["on"], budget=Budget(batch_size=4, parallel=1))
    assert report.outcome == "stopped" and run.calls == 1
    assert answered_tasks(report) == {"proj/c"} and report.commit  # the finished answer is committed
    assert {task for task, _question in report.unattempted} == {"proj/b", "proj/a"}


def test_the_deadline_ends_the_answer_phase(world, llm):
    clock = {"now": 0.0}
    run = FakeRun(llm, on_call=lambda _request: clock.update(now=500.0))
    report = world.pass_(
        run, budget=Budget(batch_size=4, parallel=1, deadline=100.0), clock=lambda: clock["now"],
    )
    assert report.outcome == "stopped" and report.stop == "deadline" and run.calls == 1
    assert answered_tasks(report) == {"proj/c"}
    assert {task for task, _question in report.unattempted} == {"proj/b", "proj/a"}


def test_a_passed_deadline_starts_nothing(world):
    before = world.snapshot()
    report = world.pass_(NeverRun(), budget=Budget(deadline=1.0), clock=lambda: 2.0)
    assert (report.outcome, report.stop, report.stopped_before) == ("stopped", "deadline", "prepare")
    assert world.snapshot() == before


def test_a_failed_chain_is_reported_with_its_attempts_and_writes_nothing(world, llm):
    report = world.pass_(FakeRun(llm, fail="boom"), targets=world.targets(["c"]))
    [failed] = report.failed
    assert (failed.task, failed.question) == ("proj/c", "orientation") and failed.attempts == (("qwen", "failed"),)
    assert report.answered == () and report.commit is None
    assert not (world.folder("c") / "answers").exists()


def test_without_llm_kit_model_answering_refuses(world):
    report = world.pass_(NeverRun(), targets=world.targets(["c"]))
    assert report.outcome == "refused" and report.refusal["state"] == "absent"
    assert "bootstrap run" in report.refusal["message"]
    assert report.answered == () and not (world.folder("c") / "answers").exists()
    assert report.unattempted == (("proj/c", "orientation"),)


def test_a_busy_lock_at_prepare_skips_the_pass(world):
    with session_publish.repo_lock(world.top, world.data):
        report = world.pass_(NeverRun(), budget=Budget(lock_wait_s=0.2))
    assert report.outcome == "skipped" and "lock is busy" in report.reason


def test_a_busy_lock_at_commit_drops_the_finished_answers(world, llm):
    holder = {}

    def take_lock(_request):
        if "lock" not in holder:
            holder["lock"] = session_publish.repo_lock(world.top, world.data, 5)
            holder["lock"].__enter__()

    try:
        report = world.pass_(
            FakeRun(llm, on_call=take_lock), targets=world.targets(["c"]),
            budget=Budget(parallel=1, lock_wait_s=0.2, commit_attempts=2),
        )
    finally:
        holder["lock"].__exit__(None, None, None)
    assert report.answered == () and [d.reason for d in report.dropped] == ["unwritten: the repository lock stayed busy at commit"]
    assert not (world.folder("c") / "answers").exists()


# --- status and prune ---------------------------------------------------------------------------------------------


def test_status_reports_coverage_and_pending_without_writing(world):
    folder = world.folder("b")
    answer_store.write_answer(folder, "orientation", ME, new_answer(folder, world.catalog))  # written, uncommitted
    before = world.snapshot()
    files_before = sorted(p.relative_to(world.top).as_posix() for p in world.top.rglob("*") if ".git/" not in p.as_posix())
    report = answer_engine.status(world.top, ME, targets=world.targets(), catalog=world.catalog, now=lambda: NOW)
    assert world.snapshot() == before and not world.data.exists()
    assert sorted(p.relative_to(world.top).as_posix() for p in world.top.rglob("*") if ".git/" not in p.as_posix()) == files_before
    by_task = {task.rel: task for task in report.tasks}
    assert by_task["proj/b"].states == {"orientation": "current"} and by_task["proj/b"].pending == ()
    assert by_task["proj/b"].coverage.as_dict() == {"catalog_version": 1, "applicable": 1, "current": 1}
    assert by_task["proj/a"].pending == ("orientation",) and by_task["proj/a"].states == {"orientation": "missing"}
    assert report.own_uncommitted == ("proj/b/answers/orientation/studio.yaml",)
    assert report.skip_reason is None
    assert report.as_dict()["pending_tasks"] == 2


def test_prune_deletes_only_this_machines_files_of_retired_questions(world):
    folder = world.folder("b")
    own = answer_store.write_answer(folder, "retired_q", ME, new_answer(folder, world.catalog))
    theirs = answer_store.write_answer(folder, "retired_q", OTHER, new_answer(folder, world.catalog))
    live = answer_store.write_answer(folder, "orientation", ME, new_answer(folder, world.catalog))
    rels = [p.relative_to(world.top).as_posix() for p in (own, theirs, live)]
    git(world.top, "add", "--", *rels)
    git(world.top, "commit", "-q", "-m", "answers", "--", *rels)
    git(world.top, "push", "-q", "origin", "HEAD:main")
    report = answer_engine.prune(world.top, ME, world.catalog, targets=world.targets(), data_dir=world.data)
    assert report.pruned == (rels[0],) and report.refused == () and report.commit
    assert not own.exists() and theirs.exists() and live.exists()
    assert rels[0] not in remote_files(world.remote) and rels[1] in remote_files(world.remote)
    assert git(world.top, "status", "--porcelain").stdout == ""


def test_prune_refuses_a_file_whose_preimage_is_another_machines(world):
    folder = world.folder("b")
    foreign = Machine("studio", "aaaaaaaaaaaaaaaa", "linux")
    path = answer_store.write_answer(folder, "retired_q", foreign, new_answer(folder, world.catalog))
    relative = path.relative_to(world.top).as_posix()
    git(world.top, "add", "--", relative)
    git(world.top, "commit", "-q", "-m", "a file that is named for this host but is not ours", "--", relative)
    report = answer_engine.prune(world.top, ME, world.catalog, targets=world.targets(), data_dir=world.data)
    assert report.pruned == () and [item[0] for item in report.refused] == [relative] and report.commit is None
    assert path.exists()
