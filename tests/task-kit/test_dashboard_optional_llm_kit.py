"""llm-kit is an OPTIONAL edge: absent, too old, or fine.

the dashboard must work without llm-kit and refuse only the autonomous actions,
each time with a message that names the real state: absent and too-old read
differently, and each names the plugin, the minimum version and its own fix
command (install when absent, update when too old).
"""

from __future__ import annotations

import importlib
from pathlib import Path
import subprocess
import sys
import urllib.parse
from types import ModuleType

import pytest
import yaml
from dashboard_support import (
    REPO_ROOT,
    SCRIPTS_DIR,
    FakeEdge,
    Running,
    dashboard_world,
    runner_environment,
)

from task_system.dashboard import jobs_edge
from task_system.dashboard.inventory import project_id_for
from task_system.dashboard.jobs_edge import ABSENT, OK, TOO_OLD, EdgeError, JobsEdge
from task_system.dashboard.mutation import folder_revision

LLM_KIT_LIB = REPO_ROOT / "plugins" / "llm-kit" / "lib"


@pytest.fixture
def real_llm_kit(monkeypatch, tmp_path):
    """The working copy of llm_kit on sys.path for one test (with a scratch HOME); purged afterwards."""
    before = {name for name in sys.modules if name == "llm_kit" or name.startswith("llm_kit.")}
    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.delenv("MODEL_ENDPOINTS_REGISTRY", raising=False)
    monkeypatch.delenv("CLAUDE_BOOTSTRAP_DATA_ROOT", raising=False)
    monkeypatch.syspath_prepend(str(LLM_KIT_LIB))
    importlib.invalidate_caches()
    try:
        yield
    finally:
        for name in [n for n in sys.modules if (n == "llm_kit" or n.startswith("llm_kit.")) and n not in before]:
            del sys.modules[name]


# -- the three states ------------------------------------------------------------


def test_absent_reads_as_absent_with_the_install_command():
    status = JobsEdge(ttl_s=0).status()
    assert status.state == ABSENT and not status.available
    assert "llm-kit" in status.detail and jobs_edge.MIN_LLM_KIT_VERSION in status.detail
    assert "claude plugin install llm-kit@plugins-kit" in status.detail and "plugin update" not in status.detail


def test_a_stale_or_older_copy_reads_as_too_old_and_differently_from_absent(monkeypatch):
    fake = ModuleType("llm_kit")
    monkeypatch.setitem(sys.modules, "llm_kit", fake)

    class OldJob:  # a Job that does not carry options.disallowed_tools through
        def __init__(self, **kwargs):
            self.options = {}

    model = ModuleType("llm_kit.jobs.model")
    model.Job, model.JobFile, model.load_job_file = OldJob, object, lambda path: None
    model.Prompt, model.Contract = lambda **k: None, lambda **k: None
    registry = ModuleType("llm_kit.registry.models")
    registry.discover_model_entries = lambda: {}
    endpoints = ModuleType("llm_kit.registry.model_endpoints")
    endpoints.HARNESS_KIND = "harness"
    cli_jobs = ModuleType("llm_kit.cli.jobs")
    for name, module in (("llm_kit.jobs", ModuleType("llm_kit.jobs")), ("llm_kit.jobs.model", model), ("llm_kit.cli", ModuleType("llm_kit.cli")),
                         ("llm_kit.cli.jobs", cli_jobs), ("llm_kit.registry", ModuleType("llm_kit.registry")),
                         ("llm_kit.registry.models", registry), ("llm_kit.registry.model_endpoints", endpoints)):
        monkeypatch.setitem(sys.modules, name, module)
    status = JobsEdge(ttl_s=0).status()
    assert status.state == TOO_OLD and "too old or stale" in status.detail and "disallowed_tools" in status.detail
    assert status.detail != jobs_edge.ABSENT_MESSAGE and jobs_edge.MIN_LLM_KIT_VERSION in status.detail
    assert "claude plugin update llm-kit@plugins-kit" in status.detail and "plugin install" not in status.detail


def test_a_missing_submodule_is_too_old_not_absent(monkeypatch):
    monkeypatch.setitem(sys.modules, "llm_kit", ModuleType("llm_kit"))
    status = JobsEdge(ttl_s=0).status()  # llm_kit imports, but llm_kit.jobs.model does not exist
    assert status.state == TOO_OLD


def test_the_working_copy_of_llm_kit_probes_ok(real_llm_kit):
    status = JobsEdge(ttl_s=0).status()
    assert (status.state, status.available) == (OK, True)


def test_an_llm_kit_without_the_effort_menu_reads_as_too_old(real_llm_kit, monkeypatch):
    """The run-options controls rely on llm_kit.registry.effort.harness_effort_order; its absence is a stale copy, not a crash."""
    assert JobsEdge(ttl_s=0).status().state == OK  # (everything else imports first, so only this symbol is missing)
    effort = importlib.import_module("llm_kit.registry.effort")
    monkeypatch.delattr(effort, "harness_effort_order")
    status = JobsEdge(ttl_s=0).status()
    assert status.state == TOO_OLD and "harness_effort_order" in status.detail
    assert "claude plugin update llm-kit@plugins-kit" in status.detail


def test_the_effort_menu_resolves_through_the_real_registry_and_is_cached(real_llm_kit, monkeypatch):
    edge = JobsEdge(ttl_s=60)
    menu = edge.effort_menu("opus")
    assert "high" in menu and menu == edge.effort_menu("opus")
    effort = importlib.import_module("llm_kit.registry.effort")
    monkeypatch.setattr(effort, "harness_effort_order", lambda harness: (_ for _ in ()).throw(AssertionError("asked again")))
    assert edge.effort_menu("opus") == menu  # served from the cache within its TTL
    with pytest.raises(EdgeError, match="no-such-entry"):
        JobsEdge(ttl_s=0).effort_menu("no-such-entry")
    with pytest.raises(EdgeError, match="not a harness entry"):
        JobsEdge(ttl_s=0).effort_menu("or-qwen")  # an endpoint is never a task worker


def test_the_status_is_cached_for_its_ttl_and_not_forever(monkeypatch):
    calls = []
    edge = JobsEdge(ttl_s=60)
    monkeypatch.setattr(edge, "_probe", lambda: calls.append(1) or jobs_edge.EdgeStatus(ABSENT, "x"))
    edge.status()
    edge.status()
    assert len(calls) == 1
    edge._cached = (edge._cached[0] - 120, edge._cached[1])
    edge.status()
    assert len(calls) == 2


def test_entries_resolve_through_the_real_registry(real_llm_kit):
    edge = JobsEdge(ttl_s=0)
    opus = edge.entry("opus")
    assert (opus.kind, opus.harness, opus.adapter) == ("harness", "claude", "claude-cli")
    assert edge.entry("sol").adapter == "codex-cli"
    qwen = edge.entry("or-qwen")
    assert qwen.kind == "transport" and qwen.adapter is None  # an endpoint is never a task worker
    assert edge.entry("no-such-entry") is None


def test_the_real_loader_accepts_a_good_job_file_and_refuses_a_broken_one(real_llm_kit, tmp_path):
    edge = JobsEdge(ttl_s=0)
    good = tmp_path / "good.yaml"
    good.write_text(yaml.safe_dump({"jobs": [{"id": "j", "prompt": {"user": "x"}, "models": ["opus"],
                                              "contract": {"command": ["true"]}}]}), encoding="utf-8")
    edge.validate_job_file(good)
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"jobs": [{"id": "j", "prompt": {"user": "x"}, "contract": {"command": ["true"]}}]}), encoding="utf-8")
    with pytest.raises(EdgeError, match="refused the job file"):
        edge.validate_job_file(bad)


def test_the_runner_command_runs_one_job_file_to_one_ledger_under_the_runners_interpreter():
    argv = JobsEdge().runner_argv(SCRIPTS_DIR / "j.yaml", SCRIPTS_DIR / "l.sqlite3", 90.0, "run1")
    assert argv[0] == sys.executable and argv[1:6] == ["-m", "llm_kit.cli", "jobs", "run", str(SCRIPTS_DIR / "j.yaml")]
    assert argv[argv.index("--store") + 1].endswith("l.sqlite3") and argv[argv.index("--timeout") + 1] == "90"
    assert argv[argv.index("--run-id") + 1] == "run1"


def test_importing_the_dashboard_never_imports_llm_kit():
    code = ("import sys; sys.path[:0] = sys.argv[1:3];"
            "import task_system.dashboard.server, task_system.dashboard.scheduler, task_system.dashboard.translate, "
            "task_system.dashboard.acceptance, task_system.dashboard.handshake, task_system.dashboard.jobs_edge;"
            "edge = task_system.dashboard.jobs_edge.JobsEdge(); edge.status();"
            "print('llm_kit' in sys.modules)")
    import os

    proc = subprocess.run([sys.executable, "-c", code, str(REPO_ROOT / "plugins" / "skills-kit"), str(SCRIPTS_DIR)],
                          capture_output=True, text=True, env=dict(os.environ, _BOOTSTRAP_GUARD_SKIP_REEXEC="1", PYTHONPATH=""))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False"  # (llm_kit is not on that interpreter's path, and nothing tried to import it eagerly)


# -- what the server does about it ------------------------------------------------------


@pytest.fixture
def rig(tmp_path, monkeypatch):
    runner_environment(monkeypatch)
    devroot, project, folder = dashboard_world(tmp_path)
    edge = FakeEdge(state="absent", detail=jobs_edge.ABSENT_MESSAGE)
    srv = Running(tmp_path / "data", (devroot,), edge=edge, config_path=tmp_path / "none.yaml")
    pid = project_id_for(project)

    class Rig:
        pass

    r = Rig()
    r.srv, r.edge, r.project, r.folder, r.pid = srv, edge, project, folder, pid
    r.path = lambda action: f"/api/v1/items/{pid}/{urllib.parse.quote('dev/tasks/alpha', safe='')}/do-it/{action}"
    try:
        yield r
    finally:
        srv.stop()


def test_without_llm_kit_the_autonomous_actions_refuse_with_503_and_create_nothing(rig):
    revision = folder_revision(rig.folder)
    status, _h, body = rig.srv.post(rig.path("queue"), {"expected_revision": revision})
    assert status == 503 and body["error"]["code"] == "llm-kit-unavailable"
    assert "claude plugin install llm-kit@plugins-kit" in body["error"]["message"] and "plugin update" not in body["error"]["message"]
    assert rig.srv.scheduler.db.runs() == [] and not (rig.srv.server.root / "projects").exists()
    assert folder_revision(rig.folder) == revision  # the task is untouched: this is not a translation failure
    status, _h, body = rig.srv.post("/api/v1/runs/anything/retry", {"expected_revision": revision})
    assert status == 404  # unknown run; a real failed run is refused the same way by queue()


def test_the_rest_of_the_dashboard_still_works_and_says_why_runs_are_off(rig):
    status, _h, payload = rig.srv.json("GET", "/api/v1/dashboard")
    assert status == 200 and payload["tasks"] and payload["capabilities"]["read"] is True
    run = payload["capabilities"]["run"]
    assert run["available"] is False and run["state"] == "absent" and "claude plugin install llm-kit@plugins-kit" in run["reason"]
    assert payload["capabilities"]["mutations"]["actions"] == ["queue", "retry", "recheck", "cancel", "newtask", "additem", "defer", "undefer", "queueall", "remove", "runoptions"]
    # Re-check is a task-folder action, not an llm-kit one, so it works.
    status, _h, body = rig.srv.post(rig.path("recheck"), {"expected_revision": folder_revision(rig.folder)})
    assert status == 200 and body["changed"] is False


def test_too_old_and_absent_are_refused_with_their_own_messages_and_recovery_needs_no_restart(rig):
    absent = rig.srv.post(rig.path("queue"), {"expected_revision": folder_revision(rig.folder)})[2]["error"]["message"]
    rig.edge.state, rig.edge.detail = "too-old", jobs_edge.TOO_OLD_MESSAGE.format(why="Job drops options.disallowed_tools")
    stale = rig.srv.post(rig.path("queue"), {"expected_revision": folder_revision(rig.folder)})[2]["error"]["message"]
    assert absent != stale and "too old or stale" in stale and "disallowed_tools" in stale
    rig.edge.state, rig.edge.detail = "ok", "llm-kit is available"
    status, _h, body = rig.srv.post(rig.path("queue"), {"expected_revision": folder_revision(rig.folder)})
    assert status == 202 and body["run"]["state"] == "queued"
    assert rig.srv.settle(body["run"]["id"]).state == "accepted"  # and it really runs, so no process outlives the test


# -- the cancelled-state frontier ------------------------------------------------------------


def test_a_copy_without_the_cancel_store_method_reads_as_too_old(real_llm_kit, monkeypatch):
    from llm_kit.jobs import store

    monkeypatch.delattr(store.JobStore, "mark_cancelled")
    status = JobsEdge(ttl_s=0).status()
    assert status.state == TOO_OLD and "mark_cancelled" in status.detail
    assert jobs_edge.MIN_LLM_KIT_VERSION in status.detail and "claude plugin update llm-kit@plugins-kit" in status.detail


def test_a_copy_without_the_cancel_verb_reads_as_too_old(real_llm_kit, monkeypatch):
    import argparse

    from llm_kit.cli import jobs as cli_jobs

    def parser_without_cancel():
        parser = argparse.ArgumentParser(prog="llm-kit jobs")
        sub = parser.add_subparsers(dest="command", required=True)
        sub.add_parser("run")
        return parser

    monkeypatch.setattr(cli_jobs, "_parser", parser_without_cancel)
    status = JobsEdge(ttl_s=0).status()
    assert status.state == TOO_OLD and "jobs cancel" in status.detail


# -- the tool-less completion an endpoint helper makes --------------------------------------------


@pytest.mark.parametrize("module, symbol", [
    ("llm_kit.dispatch.declaration", "run"),
    ("llm_kit.dispatch.declaration", "RunRequest"),
    ("llm_kit.dispatch.declaration", "NoUsableRoutingTarget"),
    ("llm_kit.completion.types", "BackendOptions"),
])
def test_a_copy_without_the_tool_less_completion_symbols_reads_as_too_old(real_llm_kit, monkeypatch, module, symbol):
    """RED: drop the probe lines for these symbols and an endpoint helper call would fail with a bare AttributeError."""
    assert JobsEdge(ttl_s=0).status().state == OK
    monkeypatch.delattr(importlib.import_module(module), symbol)
    status = JobsEdge(ttl_s=0).status()
    assert status.state == TOO_OLD and symbol in status.detail
    assert "claude plugin update llm-kit@plugins-kit" in status.detail


def test_an_entry_resolves_for_the_checkout_that_asks(real_llm_kit, tmp_path):
    seen = []
    from llm_kit.registry import models

    original = models.discover_model_entries

    def spy(**kw):
        seen.append(kw)
        return original(**kw)

    models.discover_model_entries = spy
    try:
        edge = JobsEdge(ttl_s=0)
        assert edge.entry("or-qwen", project_root=tmp_path).kind == "transport"
        assert edge.entry("or-qwen").kind == "transport"
    finally:
        models.discover_model_entries = original
    assert seen == [{"project_root": str(tmp_path)}, {}]


def test_the_cancel_command_is_the_verb_the_ledger_owner_documents(real_llm_kit):
    argv = JobsEdge(ttl_s=0).cancel_argv(Path("/x/ledger.sqlite3"), "run-1", "why")
    assert argv[1:] == ["-m", "llm_kit.cli", "jobs", "cancel", "run-1", "--store", "/x/ledger.sqlite3", "--reason", "why"]
    from llm_kit.cli import jobs as cli_jobs

    parsed = cli_jobs._parser().parse_args(argv[4:])  # the verb, its positional and its flags all parse
    assert (parsed.command, parsed.run, parsed.reason) == ("cancel", "run-1", "why")
