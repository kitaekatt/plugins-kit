"""The dashboard's job document and acceptance contract, run by the REAL llm-kit job runner.

Everything here is real except the model: llm-kit's own `run_job_file` loads the
job file the dashboard emits, selects the entry, runs the (injected, fake)
backend, then runs the job's contract command -- the dashboard's acceptance
script -- with the backend's answer on stdin, and records the verdict in a real
ledger. It is the closest check short of a live model that the two halves agree:
the answer reaches acceptance on stdin, acceptance's exit codes map to the
ledger states the design relies on, and a block ends `rejected`.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Optional

import pytest
import yaml
from dashboard_support import REPO_ROOT, FakeEdge, dashboard_world, runner_environment

from task_system.dashboard import floor
from task_system.dashboard.config import DashboardConfig
from task_system.dashboard.mutation import folder_revision, raw_items, stored_status
from task_system.dashboard.scheduler import ACCEPT_SCRIPT
from task_system.dashboard.translate import RunOptions, translate

TASK = "dev/tasks/alpha"

ANSWER_DONE = '```task_result\n{"schema": "task_result/v1", "status": "done", "summary": "Did it.", "needs": "", "reason": "", "resume_hint": "", "checks": [], "changed_paths": []}\n```\n'
ANSWER_BLOCKED = ('```task_result\n{"schema": "task_result/v1", "status": "blocked", "summary": "Stopped.", "needs": "decision", '
                  '"reason": "two designs", "resume_hint": "pick one", "checks": [], "changed_paths": []}\n```\n')


@pytest.fixture
def llm(monkeypatch, tmp_path):
    """llm_kit from the working copy, a scratch HOME, and a registry of one claude harness entry."""
    runner_environment(monkeypatch)
    before = {n for n in sys.modules if n == "llm_kit" or n.startswith("llm_kit.")}
    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.delenv("CLAUDE_BOOTSTRAP_DATA_ROOT", raising=False)
    monkeypatch.syspath_prepend(str(REPO_ROOT / "plugins" / "llm-kit" / "lib"))
    importlib.invalidate_caches()

    from llm_kit.completion.capabilities import Capabilities
    from llm_kit.completion.types import LLMResponse
    from llm_kit.dispatch import declaration, endpoint_resolver as resolver_module, factory as factory_module
    from llm_kit.dispatch import usage_budget
    from llm_kit.dispatch.pipeline_backends import ConfiguredBackendFactory
    from llm_kit.jobs import run as jobs_run
    from llm_kit.registry import models
    from llm_kit.registry.model_endpoints import EndpointEntry

    config = {"endpoints": {"opus": {"harness": "claude", "model": "opus"}}}
    entries = {"opus": EndpointEntry(id="opus", base_url=None, model="opus", kind="harness", harness="claude")}
    for module in (factory_module, resolver_module):
        monkeypatch.setattr(module, "load_model_config", lambda project_root=None: config, raising=False)
        monkeypatch.setattr(module, "discover_model_entries", lambda **_kw: dict(entries), raising=False)
    monkeypatch.setattr(resolver_module, "default_endpoint_name", lambda cfg: "opus")
    monkeypatch.setattr(models, "discover_model_entries", lambda **_kw: dict(entries))
    monkeypatch.setattr(declaration, "check_many", lambda *_a, **_k: {})
    monkeypatch.setattr(usage_budget, "record_observed_halt", lambda *a, **k: None)
    monkeypatch.setattr(jobs_run.BackpressurePolicy, "from_environment",
                        staticmethod(lambda: jobs_run.BackpressurePolicy(cap_s=0.0)))

    class Agent:
        """The fake claude-cli backend: optionally 'works' on the task folder, then answers."""

        name = "claude-cli"

        def __init__(self, answer: str, work=None) -> None:
            self.answer, self.work, self.calls = answer, work, []
            self.capabilities = ConfiguredBackendFactory().capability_records().get("claude-cli")

        def complete(self, system: str, user: str, *, model: str, options: object = None) -> LLMResponse:
            self.calls.append((system, user, model, options))
            if self.work:
                self.work()
            return LLMResponse(text=self.answer, model="fake", input_tokens=1, output_tokens=1, dropped_params=(),
                               execution_controls_applied=(), started_at="2026-09-01T00:00:00Z", ended_at="2026-09-01T00:00:01Z")

        def classify_halt(self, exc: BaseException) -> Optional[str]:
            return None

    class Factory:
        def __init__(self, agent) -> None:
            self.agent = agent

        def create(self, spec):
            return self.agent

        def capability_records(self):
            return dict(ConfiguredBackendFactory().capability_records())

        def capability_record(self, backend):
            return ConfiguredBackendFactory().capability_records().get(backend)

    try:
        yield SimpleNs(Agent=Agent, Factory=Factory, jobs_run=jobs_run)
    finally:
        for name in [n for n in sys.modules if (n == "llm_kit" or n.startswith("llm_kit.")) and n not in before]:
            del sys.modules[name]


class SimpleNs:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def materialize(tmp_path, project, folder):
    """The run directory exactly as the scheduler writes it, from the real translation."""
    run_dir = tmp_path / "run"
    result = translate(
        project_root=project, task_ref=TASK, item_id="do-it", options=RunOptions(), config=DashboardConfig(),
        edge=FakeEdge(), hold_for=lambda f: None, run_id="r1", run_dir=run_dir, accept_script=ACCEPT_SCRIPT,
        revision=folder_revision(folder))
    assert result.ready, result.reasons
    spec = result.spec
    run_dir.mkdir()
    (run_dir / "job.yaml").write_text(yaml.safe_dump(dict(spec.document), sort_keys=False), encoding="utf-8")
    start = spec.start
    (run_dir / "spec.json").write_text(json.dumps({
        "project_root": spec.project_root, "folder": spec.folder, "task_ref": TASK, "item_id": "do-it",
        "lock_path": str(tmp_path / "mutation.lock"), "accept": [],
        "start": {"revision": start.revision, "vcs": start.vcs, "head": start.head, "branch": start.branch,
                  "status": list(start.status), "log_lines": start.log_lines}}), encoding="utf-8")
    return run_dir


def complete_item(folder):
    plan = (folder / "plan.md").read_text(encoding="utf-8").replace(
        "  - id: do-it\n    title: Do it\n    state: available\n    priority: P1\n", "")
    (folder / "plan.md").write_text(plan, encoding="utf-8")
    with open(folder / "log.md", "a", encoding="utf-8") as handle:
        handle.write("- 2026-10-02 10:00: completed do-it\n")


def test_a_done_answer_reaches_acceptance_on_stdin_and_the_ledger_accepts(llm, tmp_path):
    _devroot, project, folder = dashboard_world(tmp_path)
    run_dir = materialize(tmp_path, project, folder)
    agent = llm.Agent(ANSWER_DONE, work=lambda: complete_item(folder))
    snapshot = llm.jobs_run.run_job_file(run_dir / "job.yaml", store_path=run_dir / "ledger.sqlite3", run_id="r1",
                                         timeout_s=120, backend_factory=llm.Factory(agent))
    assert [j.state.value for j in snapshot.jobs] == ["accepted"]
    (call,) = agent.calls
    assert call[2] == "opus" and "Make the thing." in call[1] and "task_result" in call[0]
    assert call[3].disallowed_tools == floor.CLAUDE_DISALLOWED_TOOLS  # the floor reached the backend options
    assert json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))["verdict"]["outcome"] == "accepted"
    assert "do-it" not in {i["id"] for i in raw_items((folder / "plan.md").read_text(encoding="utf-8"))}


def test_a_blocked_answer_ends_the_ledger_rejected_and_the_task_blocked(llm, tmp_path):
    _devroot, project, folder = dashboard_world(tmp_path)
    run_dir = materialize(tmp_path, project, folder)
    agent = llm.Agent(ANSWER_BLOCKED)
    snapshot = llm.jobs_run.run_job_file(run_dir / "job.yaml", store_path=run_dir / "ledger.sqlite3", run_id="r1",
                                         timeout_s=120, backend_factory=llm.Factory(agent))
    assert [j.state.value for j in snapshot.jobs] == ["rejected"]  # no interrupt, no waiting leg
    assert stored_status((folder / "task.yaml").read_text(encoding="utf-8")) == "blocked"
    item = {i["id"]: i for i in raw_items((folder / "plan.md").read_text(encoding="utf-8"))}["do-it"]
    assert item["state"] == "blocked-user" and "two designs" in item["note"]
    verdict = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))["verdict"]
    assert verdict["outcome"] == "agent-blocked"


def test_an_answer_without_a_result_is_rejected_by_the_real_runner(llm, tmp_path):
    _devroot, project, folder = dashboard_world(tmp_path)
    run_dir = materialize(tmp_path, project, folder)
    snapshot = llm.jobs_run.run_job_file(run_dir / "job.yaml", store_path=run_dir / "ledger.sqlite3", run_id="r1",
                                         timeout_s=120, backend_factory=llm.Factory(llm.Agent("it went fine")))
    assert [j.state.value for j in snapshot.jobs] == ["rejected"]
    assert json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))["verdict"]["code"] == "result-missing"


def test_a_call_that_hits_the_jobs_own_timeout_is_an_attempt_the_dashboard_reads_as_a_timeout(llm, tmp_path):
    """PIN against the real producer: llm-kit ends such a job `failed` with a `timeout` attempt.

    If llm-kit renames the attempt status or stops recording it, the dashboard would label a
    timed-out run "failed" again; this test fails first.
    """
    from llm_kit.completion.halt import AgentTimeoutError
    from llm_kit.jobs import model

    from task_system.dashboard import scheduler as dashboard_scheduler

    def slow() -> None:
        raise AgentTimeoutError("claude -p exceeded 30.0s timeout (elapsed 30s; likely rate-limit backoff at the CLI layer).",
                                cmd=["claude", "-p"], elapsed_s=30, stdout="", stderr="")

    _devroot, project, folder = dashboard_world(tmp_path)
    run_dir = materialize(tmp_path, project, folder)
    result = llm.jobs_run.run_job_file(run_dir / "job.yaml", store_path=run_dir / "ledger.sqlite3", run_id="r1",
                                       timeout_s=120, backend_factory=llm.Factory(llm.Agent(ANSWER_DONE, work=slow)))
    mapping = result.to_mapping()
    assert [j.state.value for j in result.jobs] == ["failed"]  # why it used to be shown as failed
    assert [a["status"] for a in mapping["attempts"]] == [model.TIMEOUT] == [dashboard_scheduler.ATTEMPT_TIMEOUT_STATUS]
    assert "exceeded 30.0s timeout" in dashboard_scheduler.job_timeout(mapping)


# ---------------------------------------------------------------------------
# cancellation against the REAL ledger
# ---------------------------------------------------------------------------

REAL_JOB = Path(__file__).with_name("real_ledger_job.py")


@pytest.fixture
def llm_path(monkeypatch, tmp_path):
    """llm_kit from the working copy importable here AND in every child process, with a scratch HOME."""
    import os

    before = {n for n in sys.modules if n == "llm_kit" or n.startswith("llm_kit.")}
    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.delenv("CLAUDE_BOOTSTRAP_DATA_ROOT", raising=False)
    lib = str(REPO_ROOT / "plugins" / "llm-kit" / "lib")
    monkeypatch.syspath_prepend(lib)
    importlib.invalidate_caches()
    try:
        yield lib
    finally:
        for name in [n for n in sys.modules if (n == "llm_kit" or n.startswith("llm_kit.")) and n not in before]:
            del sys.modules[name]


class HybridEdge(FakeEdge):
    """FakeEdge's job runner, but the REAL edge's cancel command, ledger reads and probe."""

    def __init__(self) -> None:
        super().__init__()
        from task_system.dashboard.jobs_edge import JobsEdge

        self.real = JobsEdge(ttl_s=0)

    def runner_argv(self, job_file, store, timeout_s, run_id):
        return [sys.executable, str(REAL_JOB), str(job_file), str(store), run_id]

    def cancel_argv(self, store, run_id, reason):
        return self.real.cancel_argv(store, run_id, reason)

    def ledger_snapshot(self, store, run_id):
        return self.real.ledger_snapshot(store, run_id)

    def ledger_events(self, store, run_id):
        return self.real.ledger_events(store, run_id)


def _real_rig(tmp_path, monkeypatch):
    import os

    from rig_support import Rig

    rig = Rig(tmp_path, monkeypatch, behavior="hang")
    # Child processes import llm-kit from the working copy. It records execution events with bootstrap_lib, which
    # bootstrap links into a real interpreter; here that is on the path too. (After Rig(): it sets PYTHONPATH.)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(
        [str(REPO_ROOT / "plugins" / "llm-kit" / "lib"), str(REPO_ROOT / "plugins" / "bootstrap"), os.environ["PYTHONPATH"]]))
    rig.edge = HybridEdge()
    rig.srv.scheduler.edge = rig.edge
    rig.srv.server._edge = rig.edge
    return rig


def _llm_env() -> dict:
    import os

    return dict(os.environ, PYTHONPATH=os.pathsep.join(
        [str(REPO_ROOT / "plugins" / "llm-kit" / "lib"), str(REPO_ROOT / "plugins" / "bootstrap"), os.environ.get("PYTHONPATH", "")]))


def _digest(path: Path) -> str | None:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def test_the_real_cancel_verb_closes_a_killed_runs_ledger_and_the_edge_reads_it(llm_path, tmp_path):
    from task_system.dashboard import runfiles
    from task_system.dashboard.jobs_edge import JobsEdge

    store = tmp_path / "ledger.sqlite3"
    from llm_kit.jobs.model import Contract, Job, Prompt
    from llm_kit.jobs.store import JobStore

    job = Job(id="item", prompt=Prompt(user="x"), models=("fake",), directory=tmp_path, max_attempts=1,
              contract=Contract(command=(sys.executable, "-c", "pass"), directory=tmp_path))
    ledger = JobStore(store)
    ledger.create_run("r1", [job])
    reservation = ledger.reserve_attempt("r1", "item", endpoint="e", backend="b", model="m", reserved_at="2026-09-01T00:00:00Z")
    ledger.arm_reservation("r1", "item", reservation.attempt_no, invoke_armed_at="2026-09-01T00:00:01Z")

    edge = JobsEdge(ttl_s=0)
    assert edge.status().available
    view = edge.ledger_snapshot(store, "r1")
    assert view.phase == "running" and not view.terminal
    assert edge.ledger_snapshot(store, "no-such-run") is None and edge.ledger_snapshot(tmp_path / "nope.sqlite3", "r1") is None

    record = runfiles.run_cancel_command(edge.cancel_argv(store, "r1", runfiles.REASON_PLACEHOLDER), "run-wide timeout",
                                         env=_llm_env())
    assert record["exit_code"] == 0 and runfiles.cancel_command_ok(record), record
    assert json.loads(record["stdout"])["jobs"] == [{"id": "item", "outcome": "cancelled", "state": "cancelled"}]
    view = edge.ledger_snapshot(store, "r1")
    assert view.phase == "cancelled" and view.terminal
    events = edge.ledger_events(store, "r1")
    assert any("run-wide timeout" in json.dumps(event) for event in events), events
    # Again: safe, and said so.
    again = runfiles.run_cancel_command(edge.cancel_argv(store, "r1", runfiles.REASON_PLACEHOLDER), "again", env=_llm_env())
    assert json.loads(again["stdout"])["jobs"][0]["outcome"] == "already-terminal"
    # An unknown run is exit 3, which the dashboard reads as "nothing durable exists".
    unknown = runfiles.run_cancel_command(edge.cancel_argv(store, "nope", runfiles.REASON_PLACEHOLDER), "x", env=_llm_env())
    assert unknown["exit_code"] == 3 and runfiles.cancel_command_ok(unknown)


def test_a_cancelled_run_through_the_dashboard_ends_with_a_cancelled_real_ledger(llm_path, tmp_path, monkeypatch):
    from rig_support import alive, wait_for

    from task_system.dashboard import runtime

    rig = _real_rig(tmp_path, monkeypatch)
    try:
        run_id = rig.queue("do-it", {"timeout_s": 120})
        run = rig.tick_until(run_id, (runtime.RUNNING,))
        gc = Path(run.run_dir) / "grandchild.pid"
        assert wait_for(gc.is_file)
        grandchild = int(gc.read_text(encoding="utf-8"))
        assert wait_for(lambda: rig.edge.ledger_snapshot(Path(run.run_dir) / "ledger.sqlite3", run_id) is not None)
        status, _h, body = rig.srv.json("GET", "/api/v1/dashboard")
        assert next(r for r in body["runs"] if r["id"] == run_id)["phase"] == "running"  # the live ledger phase, read-only
        assert rig.cancel(run_id)[0] == 202
        done = rig.tick_until(run_id, runtime.TERMINAL_STATES)
        assert (done.state, done.code) == (runtime.CANCELLED, "cancelled"), done
        assert wait_for(lambda: not alive(grandchild))
        view = rig.edge.ledger_snapshot(Path(run.run_dir) / "ledger.sqlite3", run_id)
        assert view.terminal and view.phase == "cancelled"
        status, _h, detail = rig.srv.json("GET", f"/api/v1/runs/{run_id}")
        assert status == 200 and any("cancelled from the dashboard" in json.dumps(e) for e in detail["ledger_events"]), detail
        assert rig.task_status() == "active"
    finally:
        rig.close()


def test_a_killed_runner_leaves_a_cancelled_real_ledger_and_a_blocked_task(llm_path, tmp_path, monkeypatch):
    import psutil
    from rig_support import alive, wait_for

    from task_system.dashboard import runtime

    rig = _real_rig(tmp_path, monkeypatch)
    try:
        run_id = rig.queue("do-it", {"timeout_s": 120})
        run = rig.tick_until(run_id, (runtime.RUNNING,))
        gc = Path(run.run_dir) / "grandchild.pid"
        assert wait_for(gc.is_file)
        grandchild = int(gc.read_text(encoding="utf-8"))
        psutil.Process(run.runner_pid).kill()
        done = rig.tick_until(run_id, runtime.TERMINAL_STATES)
        assert (done.state, done.code) == (runtime.CRASHED, "runner-crashed"), done
        assert wait_for(lambda: not alive(grandchild))
        assert rig.edge.ledger_snapshot(Path(run.run_dir) / "ledger.sqlite3", run_id).phase == "cancelled"
        assert rig.task_status() == "blocked" and "do-it" in rig.plan_item_ids()
    finally:
        rig.close()


def test_live_polls_leave_the_ledger_files_byte_unchanged(llm_path, tmp_path, monkeypatch):
    """The only ledger read while a run is live is LedgerReader.snapshot, which opens it read-only."""
    from rig_support import wait_for

    from task_system.dashboard import runtime

    rig = _real_rig(tmp_path, monkeypatch)
    try:
        run_id = rig.queue("do-it", {"timeout_s": 120})
        run = rig.tick_until(run_id, (runtime.RUNNING,))
        store = Path(run.run_dir) / "ledger.sqlite3"
        assert wait_for(lambda: rig.edge.ledger_snapshot(store, run_id) is not None)
        files = [store, store.with_name(store.name + "-wal")]
        before = [_digest(f) for f in files]
        sizes = [f.stat().st_size if f.exists() else None for f in files]
        for _ in range(15):
            assert rig.srv.json("GET", "/api/v1/dashboard")[0] == 200
            rig.srv.scheduler.tick()
            assert rig.srv.json("GET", f"/api/v1/runs/{run_id}")[0] == 200
        assert [_digest(f) for f in files] == before
        assert [f.stat().st_size if f.exists() else None for f in files] == sizes
        rig.cancel(run_id)
        rig.tick_until(run_id, runtime.TERMINAL_STATES)
    finally:
        rig.close()


def test_a_live_poll_reads_an_older_ledger_without_migrating_or_touching_it(llm_path, tmp_path, monkeypatch):
    """The sharpest form of "the only read while live is read-only": a poll of a schema-12 ledger must not upgrade it.

    A writing open (JobStore, whose constructor migrates) would turn the file into schema 13 and change its bytes
    under the process that owns it; LedgerReader opens it read-only and never migrates.
    """
    from llm_kit.jobs import store as store_module
    from llm_kit.jobs.model import Contract, Job, Prompt
    from llm_kit.jobs.store import JobStore, LedgerReader

    from task_system.dashboard.jobs_edge import JobsEdge

    full = list(store_module._MIGRATIONS)
    monkeypatch.setattr(store_module, "_MIGRATIONS", full[:12])
    path = tmp_path / "old.sqlite3"
    try:
        old = JobStore(path)
    finally:
        monkeypatch.setattr(store_module, "_MIGRATIONS", full)
    job = Job(id="item", prompt=Prompt(user="x"), models=("fake",), directory=tmp_path, max_attempts=1,
              contract=Contract(command=(sys.executable, "-c", "pass"), directory=tmp_path))
    old.create_run("r1", [job])
    reservation = old.reserve_attempt("r1", "item", endpoint="e", backend="b", model="m", reserved_at="2026-09-01T00:00:00Z")
    old.arm_reservation("r1", "item", reservation.attempt_no, invoke_armed_at="2026-09-01T00:00:01Z")
    wal = path.with_name(path.name + "-wal")
    before = _digest(path)
    edge = JobsEdge(ttl_s=0)
    for _ in range(20):
        assert edge.ledger_snapshot(path, "r1").phase == "running"
    assert _digest(path) == before  # the ledger file itself is byte for byte what the owner left
    assert not wal.exists() or wal.stat().st_size == 0  # SQLite's own empty sidecar is not a write to the ledger
    assert LedgerReader(path).schema_version() == 12
