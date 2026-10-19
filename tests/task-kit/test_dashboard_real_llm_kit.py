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
from dashboard_support import REPO_ROOT, FakeEdge, agent_world, runner_environment

from task_system.dashboard import floor
from task_system.dashboard.config import DashboardConfig
from task_system.dashboard.mutation import folder_revision, raw_items, stored_status
from task_system.dashboard.scheduler import ACCEPT_SCRIPT
from task_system.dashboard.translate import RunOptions, translate

TASK = "tmp/alpha"

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
    _devroot, project, folder = agent_world(tmp_path)
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
    _devroot, project, folder = agent_world(tmp_path)
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
    _devroot, project, folder = agent_world(tmp_path)
    run_dir = materialize(tmp_path, project, folder)
    snapshot = llm.jobs_run.run_job_file(run_dir / "job.yaml", store_path=run_dir / "ledger.sqlite3", run_id="r1",
                                         timeout_s=120, backend_factory=llm.Factory(llm.Agent("it went fine")))
    assert [j.state.value for j in snapshot.jobs] == ["rejected"]
    assert json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))["verdict"]["code"] == "result-missing"
