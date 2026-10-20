"""The endpoint helper broker: server-owned offers, consent by repository identity, a run-scoped token,
per-helper budgets reserved in one transaction, and a text-only call made by the server.

Most tests drive the REAL dashboard server, scheduler, runner process and helper client with the fake agent
(tests/task-kit/fake_llm.py behind a FakeEdge); the scheduler thread is frozen so a test ticks it itself. The
transaction tests drive ``RuntimeDB`` directly. The red checks named in each docstring were seen red against
the defect they prevent.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import psutil
import pytest
import yaml
from dashboard_support import (
    REPO_ROOT,
    FakeEdge,
    Running,
    git,
    make_checkouts,
    runner_environment,
    tree_digest,
)
from rig_support import processes_running, wait_for

from task_system.dashboard import endpoint_helper, repo_identity, runtime, scheduler
from task_system.dashboard.config import DashboardConfig, DashboardConfigError, EndpointHelper, load_config
from task_system.dashboard.inventory import project_id_for
from task_system.dashboard.jobs_edge import EntryInfo, JobsEdge
from task_system.dashboard.scheduler import QueueRefused
from task_system.dashboard.translate import RunOptions, translate

TASK = "dev/tasks/alpha"
HELPER_CLI = Path(endpoint_helper.cli_path())


def offer(name: str = "local-qwen", models=("or-qwen",), *, max_calls: int = 3, max_input_chars: int = 100,
          timeout_s: float = 5.0, purpose: str = "summarize logs") -> dict:
    return {"name": name, "models": list(models), "purpose": purpose, "max_calls": max_calls, "timeout_s": timeout_s,
            "max_input_chars": max_input_chars}


def helper_config(path: Path, helpers: dict, *, parallel: int = 2) -> None:
    path.write_text(yaml.safe_dump({"dashboard": {"max_parallel_projects": parallel, "endpoint_helpers": helpers}}), encoding="utf-8")


class Rig:
    """Checkouts under one dashboard with a frozen scheduler thread, plus a way to mint run grants by hand."""

    def __init__(self, tmp_path: Path, monkeypatch, *, names=("proj-a", "proj-b"), helpers: dict | None = None, **edge_kw) -> None:
        runner_environment(monkeypatch)
        monkeypatch.setattr(scheduler, "RUNNER_SLACK_S", 1.0)
        monkeypatch.setattr(scheduler, "RUNNER_GRACE_S", 2.0)
        self.tmp_path = tmp_path
        self.devroot, self.checkouts = make_checkouts(tmp_path, names)
        self.config = tmp_path / "config.yaml"
        self.set_helpers(helpers or {})
        self.edge = FakeEdge(**edge_kw)
        self.srv = Running(tmp_path / "data", (self.devroot,), edge=self.edge, config_path=self.config)
        sched = self.srv.scheduler
        assert wait_for(lambda: sched._thread is not None and sched._thread.is_alive(), 10)
        sched._stop.set()
        sched._wake.set()
        sched._thread.join(timeout=10)
        self._grants = 0

    @property
    def sched(self):
        return self.srv.scheduler

    @property
    def db(self) -> runtime.RuntimeDB:
        return self.srv.scheduler.db

    def set_helpers(self, helpers: dict) -> None:
        helper_config(self.config, helpers)

    def project(self, name: str) -> Path:
        return self.checkouts[name][0]

    def folder(self, name: str) -> Path:
        return self.checkouts[name][1]

    def queue(self, name: str = "proj-a", item: str = "do-it") -> runtime.Run:
        return self.sched.queue(project_root=str(self.project(name)), task_ref=TASK, item_id=item, body={"timeout_s": 60},
                                expected_revision=None)

    def tick_until(self, run_id: str, states, timeout_s: float = 60.0) -> runtime.Run:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.sched.tick()
            run = self.db.get(run_id)
            if run.state in tuple(states):
                return run
            time.sleep(0.1)
        raise AssertionError(f"run {run_id} never reached {states}: {self.db.get(run_id)}")

    def grant(self, offers: list[dict], *, state_after: str | None = None) -> tuple[str, str]:
        """A live run (leased, STARTING, with a directory) holding a grant of ``offers``: ``(run id, token)``."""
        self._grants += 1
        run_id = uuid.uuid4().hex[:16]
        root = self.project("proj-a")
        self.db.enqueue(run_id=run_id, project_id=f"fabricated-{self._grants}", project_root=str(root), task_ref=TASK, item_id="do-it",
                        item_title="t", options={}, source_revision="rev")
        assert self.db.try_lease(run_id, f"fabricated-{self._grants}", max_projects=1000)
        run_dir = self.tmp_path / "fabricated-runs" / run_id
        run_dir.mkdir(parents=True)
        self.db.set_state(run_id, runtime.STARTING, run_dir=str(run_dir))
        token = endpoint_helper.new_token()
        self.db.issue_helper_grant(run_id, endpoint_helper.hash_token(token), offers)
        if state_after:
            self.db.set_state(run_id, state_after)
        return run_id, token

    def call(self, token: str, helper: str, payload: dict | None = None, *, headers: dict | None = None, raw: bytes | None = None):
        body = raw if raw is not None else json.dumps(payload if payload is not None else {"input": "hello"}).encode("utf-8")
        sent = {"Content-Type": "application/json", endpoint_helper.RUN_TOKEN_HEADER: token}
        sent.update(headers or {})
        return self.srv.json("POST", f"/api/v1/helper-calls/{helper}", token=False, headers=sent, body=body)

    def close(self) -> None:
        try:
            self.srv.stop()
        except AssertionError:
            pass
        for proc in processes_running(str(self.tmp_path)):
            try:
                proc.kill()
            except psutil.NoSuchProcess:
                pass


@pytest.fixture
def rig(tmp_path, monkeypatch):
    made = []

    def build(**kw) -> Rig:
        r = Rig(tmp_path, monkeypatch, **kw)
        made.append(r)
        return r

    yield build
    for r in made:
        r.close()


def local_qwen(path_of: Path, **over) -> dict:
    body = {"models": ["or-qwen"], "repositories": [str(path_of)], "purpose": "summarize logs", "max_calls": 3,
            "timeout_s": 5, "max_input_chars": 100}
    body.update(over)
    return body


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def test_the_configuration_names_helpers_with_defaults_and_consent_keys(tmp_path):
    path = tmp_path / "task.local.yaml"
    path.write_text(yaml.safe_dump({"dashboard": {"endpoint_helpers": {
        "local-qwen": {"models": ["or-qwen", "or-gemini-lite"], "repositories": [
            "https://github.com/kitaekatt/plugins-kit", "git@github.com:kitaekatt/plugins-kit.git", str(tmp_path)],
            "purpose": "summarize long logs"}}}}), encoding="utf-8")
    (helper,) = load_config(path).endpoint_helpers
    assert helper == EndpointHelper(
        name="local-qwen", models=("or-qwen", "or-gemini-lite"),
        repositories=("github.com/kitaekatt/plugins-kit", repo_identity.path_key(tmp_path)),  # two spellings, one key
        purpose="summarize long logs", max_calls=20, timeout_s=300.0, max_input_chars=100000)
    assert helper.offered_to("github.com/kitaekatt/plugins-kit") and not helper.offered_to("github.com/someone/else")
    assert load_config(tmp_path / "missing.yaml").endpoint_helpers == ()


@pytest.mark.parametrize("helpers, fragment", [
    ({"claude": {"models": ["x"], "repositories": ["/a"]}}, "task worker"),
    ({"codex": {"models": ["x"], "repositories": ["/a"]}}, "task worker"),
    ({"Bad_Name": {"models": ["x"], "repositories": ["/a"]}}, "must match"),
    ({"h": {"models": [], "repositories": ["/a"]}}, "models must be a non-empty list"),
    ({"h": {"models": ["x", "x"], "repositories": ["/a"]}}, "distinct"),
    ({"h": {"models": ["x"], "repositories": []}}, "repositories must be a non-empty list"),
    ({"h": {"models": ["x"], "repositories": ["not a url"]}}, "repositories"),
    ({"h": {"models": ["x"], "repositories": ["/a"], "purpose": "two\nlines"}}, "one line"),
    ({"h": {"models": ["x"], "repositories": ["/a"], "purpose": "p" * 201}}, "one line"),
    ({"h": {"models": ["x"], "repositories": ["/a"], "max_calls": 0}}, "max_calls"),
    ({"h": {"models": ["x"], "repositories": ["/a"], "max_calls": True}}, "max_calls"),
    ({"h": {"models": ["x"], "repositories": ["/a"], "timeout_s": 0}}, "timeout_s"),
    ({"h": {"models": ["x"], "repositories": ["/a"], "max_input_chars": 1000001}}, "max_input_chars"),
    ({"h": {"models": ["x"], "repositories": ["/a"], "surprise": 1}}, "unknown keys"),
    ({"h": "not a mapping"}, "must be a mapping"),
])
def test_an_unusable_helper_section_is_an_error_naming_the_file(tmp_path, helpers, fragment):
    path = tmp_path / "task.local.yaml"
    path.write_text(yaml.safe_dump({"dashboard": {"endpoint_helpers": helpers}}), encoding="utf-8")
    with pytest.raises(DashboardConfigError) as caught:
        load_config(path)
    assert str(path) in str(caught.value) and fragment in str(caught.value)


# ---------------------------------------------------------------------------
# consent by repository identity
# ---------------------------------------------------------------------------


def _origin(root: Path, url: str) -> None:
    git(root, "remote", "add", "origin", url)


def test_consent_is_by_repository_identity_never_by_display_name(tmp_path):
    """R15. RED: key the consent by the display name and the second checkout, an unrelated repository with the
    same name, is offered the helper too."""
    _devroot, made = make_checkouts(tmp_path, ("one", "two", "bare"))
    _origin(made["one"][0], "git@github.com:kitaekatt/plugins-kit.git")
    _origin(made["two"][0], "https://github.com/someone-else/plugins-kit.git")  # same display name, other repository
    assert repo_identity.repo_name_for(made["one"][0]) == repo_identity.repo_name_for(made["two"][0]) == "plugins-kit"
    assert repo_identity.repo_key_for(made["one"][0]) == "github.com/kitaekatt/plugins-kit"
    assert repo_identity.repo_key_for(made["bare"][0]) == repo_identity.path_key(made["bare"][0])  # no origin: by path

    config = DashboardConfig(endpoint_helpers=(
        EndpointHelper(name="h", models=("or-qwen",), repositories=(
            repo_identity.config_key("https://github.com/kitaekatt/plugins-kit.git/"),  # another spelling of checkout one's origin
            repo_identity.config_key(str(made["bare"][0])))),))
    offered = {name: [o["name"] for o in endpoint_helper.offers_for(config, repo_identity.repo_key_for(root))]
               for name, (root, _f) in made.items()}
    assert offered == {"one": ["h"], "two": [], "bare": ["h"]}

    for spelling in ("https://github.com/kitaekatt/plugins-kit", "https://GitHub.com/kitaekatt/plugins-kit.git",
                     "ssh://git@github.com/kitaekatt/plugins-kit/", "git@github.com:kitaekatt/plugins-kit"):
        assert repo_identity.normalize_remote(spelling) == "github.com/kitaekatt/plugins-kit"
    assert repo_identity.normalize_remote("https://github.com/Kitaekatt/Plugins-Kit") != "github.com/kitaekatt/plugins-kit"  # path case stays


def test_a_translation_offers_exactly_the_helpers_the_checkout_consents_to(tmp_path):
    _devroot, made = make_checkouts(tmp_path, ("one", "two"))
    _origin(made["one"][0], "https://github.com/kitaekatt/plugins-kit.git")
    _origin(made["two"][0], "https://github.com/someone-else/plugins-kit.git")
    config = DashboardConfig(endpoint_helpers=(
        EndpointHelper(name="local-qwen", models=("or-qwen",), repositories=("github.com/kitaekatt/plugins-kit",), purpose="logs",
                       max_calls=2, max_input_chars=500),))
    specs = {}
    for name, (root, folder) in made.items():
        from task_system.dashboard.mutation import folder_revision
        result = translate(project_root=root, task_ref=TASK, item_id="do-it", options=RunOptions(), config=config, edge=FakeEdge(),
                           hold_for=lambda f: None, run_id="r", run_dir=tmp_path / "run" / name, accept_script=scheduler.ACCEPT_SCRIPT,
                           revision=folder_revision(folder))
        assert result.ready, result.reasons
        specs[name] = result.spec
    assert [o["name"] for o in specs["one"].helper_offers] == ["local-qwen"] and specs["two"].helper_offers == ()
    system = specs["one"].document["jobs"][0]["prompt"]["system"]
    assert "Endpoint helpers (text-only models: no tools, no file access)" in system
    assert "- local-qwen: logs (at most 2 calls in this run, input up to 500 characters)" in system
    assert f'Run: "{sys.executable}" "{HELPER_CLI}" <name> <<\'EOF\'' in system
    assert "Endpoint helpers" not in specs["two"].document["jobs"][0]["prompt"]["system"]


def test_a_harness_entry_among_a_helpers_models_refuses_the_run_and_leaves_the_task_alone(rig):
    """R12. RED: skip the entry-kind check and the run is queued (and would start) with an agent among its helper's models."""
    w = rig(helpers={})
    w.set_helpers({"agent-helper": local_qwen(w.project("proj-a"), models=["sonnet"])})
    before = tree_digest(w.folder("proj-a"))
    with pytest.raises(QueueRefused) as refused:
        w.queue("proj-a")
    assert refused.value.status == 422 and refused.value.code == "bad-options"
    assert [r["code"] for r in refused.value.reasons] == ["endpoint-helper"]
    assert "agent-helper" in str(refused.value) and "harness entry (claude)" in str(refused.value)
    assert not w.db.runs() and tree_digest(w.folder("proj-a")) == before  # no run row, folder byte-identical
    w.queue("proj-b")  # a checkout the helper is not offered to is unaffected

    # The configuration changed AFTER the entry was queued: the start ends the run `failed`, the task untouched.
    w.set_helpers({})
    queued = w.queue("proj-a")
    w.set_helpers({"agent-helper": local_qwen(w.project("proj-a"), models=["sonnet"])})
    before = tree_digest(w.folder("proj-a"))
    ended = w.tick_until(queued.id, (runtime.FAILED,))
    assert (ended.code, "endpoint-helper" in (ended.detail or "")) == ("bad-options", True)
    assert tree_digest(w.folder("proj-a")) == before
    assert w.db.helper_grant(queued.id) is None


# ---------------------------------------------------------------------------
# the route: authentication, forged and tampered state
# ---------------------------------------------------------------------------


def test_an_answer_whose_raw_copy_cannot_be_kept_ends_the_call_failed_and_is_not_returned(rig, monkeypatch):
    """RED: swallow the OSError of keeping the output (log and go on) and the call is answered 200 with no record."""
    w = rig()
    run_id, token = w.grant([offer("local-qwen", max_calls=1)])
    real = endpoint_helper.write_private

    def failing(path, text):
        if str(path).endswith(".out.txt"):
            raise PermissionError(13, "disk says no")
        real(path, text)

    monkeypatch.setattr(endpoint_helper, "write_private", failing)
    status, _h, body = w.call(token, "local-qwen")
    assert status == 500 and body["error"]["code"] == "not-kept" and "disk says no" in body["error"]["message"]
    (call,) = w.db.helper_calls(run_id)
    assert (call["state"], call["status"]) == ("failed", "not-kept") and call["ended_at"]  # finalised, not left reserved
    assert call["entry"] == "or-qwen" and "disk says no" in call["detail"]
    monkeypatch.setattr(endpoint_helper, "write_private", real)
    assert w.call(token, "local-qwen")[0] == 429  # the endpoint was reached, so the call still spent its budget


def test_a_registry_that_will_not_load_is_an_error_not_a_reason(rig):
    """RED: catch every exception from the edge's entry lookup and the failure becomes a quiet non-blocking reason."""
    from task_system.dashboard.jobs_edge import EdgeError

    w = rig()
    w.set_helpers({"local-qwen": local_qwen(w.project("proj-a"))})
    real = w.edge.entry

    def broken(name, project_root=None):
        if name == "or-qwen":
            raise EdgeError("llm-kit could not load its model registry: boom")
        return real(name, project_root)

    w.edge.entry = broken
    with pytest.raises(EdgeError, match="boom"):
        w.queue("proj-a")
    assert not w.db.runs()


def test_forged_and_tampered_state_changes_neither_offers_nor_counts(rig):
    """R13. RED: count a helper's calls from files in the run directory (the worker can delete them) and the
    budget comes back after the delete."""
    w = rig()
    run_id, token = w.grant([offer("local-qwen", max_calls=1)])

    status, _h, body = w.call("not-a-token", "local-qwen")
    assert status == 401 and body["error"]["code"] == "unknown-token"
    status, _h, body = w.call("", "local-qwen", headers={"X-Task-Kit-Token": w.srv.token, endpoint_helper.RUN_TOKEN_HEADER: ""})
    assert status == 401  # the dashboard token authorizes nothing on this route
    status, _h, _b = w.srv.json("GET", "/api/v1/dashboard", token=False, headers={endpoint_helper.RUN_TOKEN_HEADER: token})
    assert status == 401  # and the run token authorizes nothing else
    assert w.db.helper_calls(run_id) == []  # an unknown token names no run, so there is no row to write

    status, _h, body = w.call(token, "local-qwen")
    assert status == 200 and body["status"] == "completed" and body["text"] == "echo: hello"
    # The worker owns its run directory: it edits the spec, plants an offer, deletes what the server kept.
    run_dir = Path(w.db.get(run_id).run_dir)
    (run_dir / "spec.json").write_text(json.dumps({"helpers": [offer("rogue", max_calls=99)], "offers": [offer("rogue")]}), encoding="utf-8")
    for kept in (run_dir / "helpers").glob("*"):
        kept.unlink()
    (run_dir / "helpers").rmdir()
    status, _h, body = w.call(token, "rogue")
    assert status == 404 and body["error"]["offered"] == ["local-qwen"]
    status, _h, body = w.call(token, "local-qwen")
    assert status == 429 and "1 of 1 calls" in body["error"]["message"]  # the spent budget did not come back

    w.db.set_state(run_id, runtime.FAILED)  # a token of a run that reached a terminal state
    status, _h, body = w.call(token, "local-qwen")
    assert status == 410 and body["error"]["code"] == "revoked"
    states = [(c["helper"], c["state"]) for c in w.db.helper_calls(run_id)]
    assert states == [("local-qwen", "completed"), ("rogue", "refused"), ("local-qwen", "refused"), ("local-qwen", "refused")]
    assert len(w.edge.transport_calls) == 1  # only the first request ever reached an endpoint


def test_a_budget_is_per_helper_and_a_race_for_the_last_call_has_one_winner(rig):
    """R14. RED: count without one atomic step (the count and the insert in two transactions, with both threads
    counting before either inserts) and both racers are reserved; count every helper against one counter and
    the second helper loses its own budget."""
    w = rig()
    run_id, token = w.grant([offer("a", max_calls=1), offer("b", max_calls=1)])
    gate = threading.Barrier(2)
    original = w.db._helper_budget_used

    def counting(run, helper):
        used = original(run, helper)
        try:
            gate.wait(timeout=0.3)  # holds two racers at the count step if nothing serializes them
        except threading.BrokenBarrierError:
            pass
        return used

    w.db._helper_budget_used = counting
    start = threading.Barrier(2)
    outcomes: list[int] = []

    def racer() -> None:
        start.wait()
        outcomes.append(w.call(token, "a")[0])

    threads = [threading.Thread(target=racer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert sorted(outcomes) == [200, 429]
    assert sorted(c["state"] for c in w.db.helper_calls(run_id) if c["helper"] == "a") == ["completed", "refused"]  # two rows
    w.db._helper_budget_used = original
    assert w.call(token, "b")[0] == 200  # the other helper keeps its own budget
    assert w.call(token, "b")[0] == 429


def test_a_reservation_never_lands_on_a_revoked_grant(rig):
    """R14b. The count step inside ``reserve_helper_call`` holds the reserver while a second thread ends the run.
    RED: split authorization and insertion into two transactions (the count in the second) and the end of the run
    lands between them: a reservation is inserted on a revoked grant."""
    w = rig()
    outcomes = {"reserved": 0, "refused-410": 0}
    for _ in range(50):
        run_id, token = w.grant([offer("local-qwen", max_calls=5)])
        inside, release = threading.Event(), threading.Event()
        original = w.db._helper_budget_used

        def counting(run, helper, original=original, inside=inside, release=release):
            inside.set()
            release.wait(0.1)
            return original(run, helper)

        w.db._helper_budget_used = counting
        result: list = []
        reserver = threading.Thread(target=lambda: result.append(w.db.reserve_helper_call(endpoint_helper.hash_token(token), "local-qwen", 5)))
        revoker = threading.Thread(target=lambda: (inside.wait(10), w.db.set_state(run_id, runtime.FAILED), release.set()))
        reserver.start()
        revoker.start()
        reserver.join(timeout=20)
        revoker.join(timeout=20)
        w.db._helper_budget_used = original
        (got,) = result
        grant = w.db.helper_grant(run_id)
        assert grant["revoked_at"] is not None
        rows = [c for c in w.db.helper_calls(run_id) if c["state"] == "reserved"]
        assert all(c["reserved_at"] <= grant["revoked_at"] for c in rows), (rows, grant)  # no reservation after the revocation
        if isinstance(got, runtime.Reservation):
            assert len(rows) == 1
            outcomes["reserved"] += 1
        else:
            assert (got.status, rows) == (410, [])
            outcomes["refused-410"] += 1
    assert sum(outcomes.values()) == 50  # every attempt is a committed reservation or a 410


def test_input_plus_system_is_what_the_size_limit_counts(rig):
    """R17b. RED: count the input only and a tiny input with a 500-character system is reserved and sent."""
    w = rig()
    run_id, token = w.grant([offer("local-qwen", max_input_chars=100, max_calls=5)])
    status, _h, body = w.call(token, "local-qwen", {"input": "hi", "system": "s" * 500})
    assert status == 422 and body["error"]["code"] == "too-large"
    assert body["error"]["message"] == "input plus system is 502 characters; this helper accepts 100"
    assert [(c["state"], c["input_chars"]) for c in w.db.helper_calls(run_id)] == [("refused", 502)]  # a refused row, no reservation
    assert w.edge.transport_calls == []  # no endpoint call
    status, _h, body = w.call(token, "local-qwen", {"input": "x" * 50, "system": "s" * 50})
    assert status == 200
    (call,) = w.edge.transport_calls
    assert call["system"] == "s" * 50 and call["models"] == ["or-qwen"] and call["timeout_s"] == 5.0
    assert [c["input_chars"] for c in w.db.helper_calls(run_id)] == [502, 100]
    # A caller that sends no system text is answered with the server's own text, which the limit does not count.
    status, _h, _b = w.call(token, "local-qwen", {"input": "y" * 100})
    assert status == 200 and w.edge.transport_calls[-1]["system"] == endpoint_helper.DEFAULT_SYSTEM


def test_the_browser_cannot_cause_egress(rig):
    """R18. RED: drop the Origin gate and a page-originated request is answered (and spends the budget)."""
    w = rig()
    run_id, token = w.grant([offer("local-qwen")])
    status, _h, body = w.call(token, "local-qwen", headers={"Origin": f"http://127.0.0.1:{w.srv.port}"})
    assert status == 403 and body["error"]["code"] == "bad_origin"
    status, _h, _b = w.call(token, "local-qwen", headers={"Origin": "http://evil.example"})
    assert status == 403
    assert w.edge.transport_calls == [] and w.db.helper_calls(run_id) == []
    assert w.srv.json("OPTIONS", "/api/v1/helper-calls/local-qwen", token=False)[0] == 405  # no CORS preflight answer either
    assert w.srv.json("GET", "/api/v1/helper-calls/local-qwen", token=False)[0] == 405


def test_the_request_body_cap_is_four_mib_on_this_route_only(rig):
    w = rig()
    run_id, token = w.grant([offer("local-qwen", max_input_chars=1_000_000)])
    huge = json.dumps({"input": "x" * (1024 * 1024)}).encode("utf-8")  # over the 64 KiB of every other route, under 4 MiB
    status, _h, body = w.call(token, "local-qwen", raw=huge)
    assert status == 422 and body["error"]["code"] == "too-large"  # it was READ and refused by size, not by the body cap
    # Over the cap the server answers from the headers alone and never reads the body, so announce the length and send none.
    import socket

    with socket.create_connection(("127.0.0.1", w.srv.port), timeout=10) as conn:
        conn.sendall((f"POST /api/v1/helper-calls/local-qwen HTTP/1.1\r\nHost: 127.0.0.1:{w.srv.port}\r\nContent-Type: application/json\r\n"
                      f"{endpoint_helper.RUN_TOKEN_HEADER}: {token}\r\nContent-Length: {4 * 1024 * 1024 + 1}\r\n\r\n").encode("ascii"))
        chunks = []
        while chunk := conn.recv(65536):  # HTTP/1.0: the server closes after the answer
            chunks.append(chunk)
        reply = b"".join(chunks).decode("utf-8", "replace")
    assert reply.startswith("HTTP/1.0 413") and "body_too_large" in reply
    over_default = json.dumps({"expected_revision": "x" * (64 * 1024 + 1)}).encode("utf-8")
    status, _h, body = w.srv.json("POST", "/api/v1/shutdown", headers={"Origin": f"http://127.0.0.1:{w.srv.port}",
                                                                      "Content-Type": "application/json"}, body=over_default)
    assert status == 413  # the rest of the API keeps its 64 KiB
    assert [c["state"] for c in w.db.helper_calls(run_id)] == ["refused"]


def test_malformed_requests_are_refused_after_the_token_is_known(rig):
    w = rig()
    run_id, token = w.grant([offer("local-qwen")])
    assert w.call(token, "local-qwen", raw=b"not json")[0] == 400
    assert w.call(token, "local-qwen", {"input": 5})[0] == 422
    assert w.call(token, "local-qwen", {"input": "x", "extra": 1})[0] == 422
    assert w.call(token, "local-qwen", {"input": "   "})[2]["error"]["code"] == "empty-input"
    assert w.call(token, "local-qwen", {"input": "x", "system": 3})[0] == 422
    assert {c["state"] for c in w.db.helper_calls(run_id)} == {"refused"} and w.edge.transport_calls == []


def test_a_call_still_reserved_when_the_server_stops_is_interrupted_and_still_counts(rig):
    w = rig()
    run_id, token = w.grant([offer("local-qwen", max_calls=1)])
    reserved = w.db.reserve_helper_call(endpoint_helper.hash_token(token), "local-qwen", 3)
    assert isinstance(reserved, runtime.Reservation)
    assert w.db.interrupt_open_calls() == 1
    (call,) = w.db.helper_calls(run_id)
    assert call["state"] == "interrupted" and call["ended_at"] and "server stopped" in call["detail"]
    refusal = w.db.reserve_helper_call(endpoint_helper.hash_token(token), "local-qwen", 3)
    assert isinstance(refusal, runtime.Refusal) and refusal.status == 429  # fail closed: it counts


def test_a_server_start_interrupts_the_calls_the_previous_server_left_reserved(rig, tmp_path):
    """RED: drop the call in ``DashboardServer.start`` and the stranded reservation stays ``reserved`` for ever."""
    w = rig()
    run_id, token = w.grant([offer("local-qwen", max_calls=2)])
    assert isinstance(w.db.reserve_helper_call(endpoint_helper.hash_token(token), "local-qwen", 3), runtime.Reservation)
    w.srv.stop()  # the server goes away with a call in flight
    again = Running(tmp_path / "data", (w.devroot,), edge=w.edge, config_path=w.config)
    try:
        (call,) = again.scheduler.db.helper_calls(run_id)
        assert call["state"] == "interrupted" and "server stopped" in call["detail"]
        assert "1 endpoint helper call(s) were in flight" in again.log.getvalue()
    finally:
        again.stop()
        w.srv = again


def test_a_run_that_is_released_to_the_queue_loses_its_grant_and_a_new_dispatch_replaces_it(rig):
    w = rig()
    run_id, first = w.grant([offer("local-qwen")])
    w.db.release_to_queue(run_id)
    assert w.db.helper_grant(run_id)["revoked_at"] is not None
    assert w.call(first, "local-qwen")[0] == 410
    assert w.db.try_lease(run_id, "fabricated-1", max_projects=5)
    second = endpoint_helper.new_token()
    w.db.issue_helper_grant(run_id, endpoint_helper.hash_token(second), [offer("local-qwen")])
    assert w.call(first, "local-qwen")[0] == 401 and w.call(second, "local-qwen")[0] == 200


# ---------------------------------------------------------------------------
# end to end: a real run asks through the command its prompt prints
# ---------------------------------------------------------------------------


def test_a_run_asks_helpers_through_the_printed_command_and_the_server_keeps_the_record(rig):
    """R17. The fake agent runs the command printed in its prompt: an answer (exit 0), a helper it was not offered and
    an empty excerpt (exit 2), and a helper whose model the registry does not know (exit 3, with the dispositions)."""
    w = rig()
    path = w.project("proj-a")
    w.set_helpers({"local-qwen": local_qwen(path), "ghost": local_qwen(path, models=["not-in-registry"])})
    w.edge.behavior = "helper-call"
    w.edge.behavior_args = {"calls": [
        {"helper": "local-qwen", "input": "hello"},
        {"helper": "nope", "input": "x"},
        {"helper": "local-qwen", "input": "   "},
        {"helper": "ghost", "input": "x"},
        {"helper": "local-qwen", "input": "with system", "system": "be brief"},
    ]}
    run = w.queue("proj-a")
    ended = w.tick_until(run.id, runtime.TERMINAL_STATES)
    assert ended.state == runtime.ACCEPTED, (ended.code, ended.detail)
    run_dir = Path(ended.run_dir)

    results = json.loads((run_dir / "helper-calls.json").read_text(encoding="utf-8"))
    assert [r["exit"] for r in results] == [0, 2, 2, 3, 0]
    assert results[0]["stdout"] == "echo: hello" and results[0]["stderr"] == ""
    assert "not-offered" in results[1]["stderr"] and "offered: ghost, local-qwen" in results[1]["stderr"]
    assert "empty-input" in results[2]["stderr"]
    assert "unroutable" in results[3]["stderr"] and "no usable routing target" in results[3]["stderr"]
    assert results[4]["stdout"] == "echo: with system"

    calls = w.db.helper_calls(run.id)
    assert [(c["helper"], c["state"], c["status"]) for c in calls] == [
        ("local-qwen", "completed", "completed"), ("nope", "refused", None), ("local-qwen", "refused", None),
        ("ghost", "failed", "unroutable"), ("local-qwen", "completed", "completed")]
    assert calls[0]["entry"] == "or-qwen" and calls[0]["usage"] == {"input_tokens": 3, "output_tokens": 2} and calls[0]["reported_cost_usd"] == 0.0001
    assert "no usable routing target" in calls[3]["detail"]
    # The raw text stays in the run directory, privately, and is not served anywhere.
    first = calls[0]["id"]
    assert (run_dir / "helpers" / f"{first}.in.txt").read_text(encoding="utf-8") == "hello"
    assert (run_dir / "helpers" / f"{first}.out.txt").read_text(encoding="utf-8") == "echo: hello"
    assert (run_dir / "helpers" / f"{calls[4]['id']}.system.txt").read_text(encoding="utf-8") == "be brief"
    # The token went into the worker's environment only; the database kept its hash and the run's end revoked it.
    spec = json.loads((run_dir / "spec.json").read_text(encoding="utf-8"))
    token = spec["runner"]["env"][endpoint_helper.TOKEN_ENV]
    assert spec["runner"]["env"][endpoint_helper.ROOT_ENV] == str(w.srv.server.root)
    if os.name == "posix":  # the file that carries the token, and what the server keeps of the text, are owner-only
        owner_only = [run_dir / "spec.json", run_dir / "helpers", *(run_dir / "helpers").iterdir()]
        assert all(path.stat().st_mode & 0o077 == 0 for path in owner_only), owner_only
    assert w.db.helper_grant(run.id)["revoked_at"] is not None
    assert w.call(token, "local-qwen")[0] == 410
    assert token not in w.srv.log.getvalue() and endpoint_helper.hash_token(token) not in json.dumps(w.db.events(run.id))

    # The page's record of the run: what was offered and every call.
    status, _h, detail = w.srv.json("GET", f"/api/v1/runs/{run.id}")
    assert status == 200 and [o["name"] for o in detail["helpers_offered"]] == ["ghost", "local-qwen"]
    assert [c["helper"] for c in detail["helper_calls"]] == ["local-qwen", "nope", "local-qwen", "ghost", "local-qwen", "local-qwen"]  # the last: refused 410, above
    status, _h, snap = w.srv.json("GET", "/api/v1/dashboard")
    rows = {t["checkout"]: t for t in snap["tasks"]}
    assert rows[project_id_for(path)]["helpers"] == ["ghost", "local-qwen"] and rows[project_id_for(w.project("proj-b"))]["helpers"] == []


def test_a_checkout_that_did_not_consent_gets_no_token_and_no_section(rig):
    w = rig()
    w.set_helpers({"local-qwen": local_qwen(w.project("proj-a"))})
    run = w.queue("proj-b")
    ended = w.tick_until(run.id, runtime.TERMINAL_STATES)
    assert ended.state == runtime.ACCEPTED
    run_dir = Path(ended.run_dir)
    spec = json.loads((run_dir / "spec.json").read_text(encoding="utf-8"))
    assert endpoint_helper.TOKEN_ENV not in spec["runner"]["env"] and endpoint_helper.ROOT_ENV not in spec["runner"]["env"]
    assert "Endpoint helpers" not in (run_dir / "job.yaml").read_text(encoding="utf-8")
    assert w.db.helper_grant(run.id) is None


def test_the_helper_client_reports_what_happened_with_its_exit_status(rig, tmp_path):
    w = rig()
    run_id, token = w.grant([offer("local-qwen", max_calls=1)])
    env = {"PATH": "/usr/bin:/bin", endpoint_helper.TOKEN_ENV: token, endpoint_helper.ROOT_ENV: str(w.srv.server.root)}

    def client(*args, stdin="hello", environment=env):
        return subprocess.run([sys.executable, str(HELPER_CLI), *args], input=stdin, text=True, capture_output=True, env=environment, timeout=60)

    check = client("--check", environment={"PATH": "/usr/bin:/bin"})
    assert (check.returncode, check.stdout.strip()) == (0, "task-kit-helper-cli ok")  # no server, no token needed
    answered = client("local-qwen")
    assert (answered.returncode, answered.stdout) == (0, "echo: hello")
    spent = client("local-qwen")
    assert spent.returncode == 2 and spent.stderr.count("\n") == 1 and "budget-spent" in spent.stderr
    assert client("local-qwen", environment={"PATH": "/usr/bin:/bin"}).returncode == 2  # no token: not inside a run
    unreachable = client("local-qwen", environment={**env, endpoint_helper.ROOT_ENV: str(tmp_path / "nowhere")})
    assert unreachable.returncode == 1 and "server.json" in unreachable.stderr
    assert [c["state"] for c in w.db.helper_calls(run_id)] == ["completed", "refused"]


def test_the_client_imports_nothing_from_the_dashboard():
    source = HELPER_CLI.read_text(encoding="utf-8")
    assert "task_system" not in source and "llm_kit" not in source
    imported = {line.split()[1].split(".")[0] for line in source.splitlines() if line.startswith(("import ", "from "))}
    assert imported <= {"__future__", "argparse", "http", "json", "os", "sys", "pathlib"}


# ---------------------------------------------------------------------------
# the edge: a tool-less call through llm-kit's own declaration run
# ---------------------------------------------------------------------------


@pytest.fixture
def llm_kit_path(monkeypatch):
    import importlib

    before = {n for n in sys.modules if n == "llm_kit" or n.startswith("llm_kit.")}
    monkeypatch.syspath_prepend(str(REPO_ROOT / "plugins" / "llm-kit" / "lib"))
    importlib.invalidate_caches()
    try:
        importlib.import_module("llm_kit.dispatch.declaration")
    except ImportError as exc:  # pragma: no cover -- the working copy always carries it
        pytest.skip(f"llm_kit is not importable: {exc}")
    yield
    for name in [n for n in sys.modules if (n == "llm_kit" or n.startswith("llm_kit.")) and n not in before]:
        del sys.modules[name]


def test_the_completion_is_requested_text_only_through_the_real_declaration_run(llm_kit_path, tmp_path):
    """R16. A harness entry is armed text-only only because the call states the three guarantees. RED: drop
    ``requirements`` and the harness backend runs with its tools on."""
    from llm_kit.adapters import backends
    from llm_kit.completion.types import LLMResponse
    from llm_kit.dispatch.factory import BackendSelection
    from llm_kit.dispatch.reachability import Reachability
    from llm_kit.registry.model_endpoints import HARNESS_KIND, EndpointEntry

    class Agent(backends.ClaudeCliBackend):
        def complete(self, system, user, *, model, options=None):
            applied = ("text-only-mode",) if self.text_only else ()
            return LLMResponse(text="text-only" if self.text_only else "tools-on", model="m", input_tokens=3, output_tokens=2,
                               execution_controls_applied=applied)

    entries = {"opus": EndpointEntry(id="opus", base_url=None, model="opus", kind=HARNESS_KIND, harness="claude")}
    cache = {"opus": Reachability(status="reachable", checked="cli-version", detail="ok")}

    def factory(name, **_kw):
        return BackendSelection(endpoint=name, kind=HARNESS_KIND, backend=Agent(), model="opus", effort=None)

    outcome = JobsEdge().complete_transport(["opus"], system="s", prompt="p", timeout_s=5, project_root=tmp_path,
                                            backend_factory=factory, entries=entries, reachability_cache=cache)
    assert (outcome.status, outcome.text, outcome.entry) == ("completed", "text-only", "opus")
    assert outcome.usage == {"input_tokens": 3, "output_tokens": 2}


def test_a_transport_entry_answers_and_an_unknown_entry_is_the_floor_with_its_dispositions(llm_kit_path, tmp_path, monkeypatch):
    from llm_kit.completion.types import LLMResponse
    from llm_kit.dispatch.factory import BackendSelection
    from llm_kit.dispatch.reachability import Reachability
    from llm_kit.registry.model_endpoints import EndpointEntry

    calls = []

    class Wire:
        name = "openrouter"

        def complete(self, system, user, *, model, options=None):
            calls.append((system, user, model, options))
            return LLMResponse(text="answer", model=model, input_tokens=4, output_tokens=1, reported_cost_usd=None)

        def classify_halt(self, exc):
            return None

    entries = {"or-qwen": EndpointEntry(id="or-qwen", base_url="http://qwen.invalid/v1", model="qwen/qwen3-32b")}
    cache = {"or-qwen": Reachability(status="reachable", checked="cli-version", detail="ok")}

    def factory(name, **_kw):
        return BackendSelection(endpoint=name, kind="transport", backend=Wire(), model="qwen/qwen3-32b", effort=None)

    edge = JobsEdge()
    outcome = edge.complete_transport(["or-qwen"], system="s", prompt="p", timeout_s=7, project_root=tmp_path,
                                      backend_factory=factory, entries=entries, reachability_cache=cache)
    assert (outcome.status, outcome.text, outcome.entry) == ("completed", "answer", "or-qwen")
    assert calls[0][3].timeout_s == 7.0
    # A name the registry does not know is resolved against the real registry (an empty scratch HOME holds none of the user's).
    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.delenv("CLAUDE_BOOTSTRAP_DATA_ROOT", raising=False)
    floor = edge.complete_transport(["ghost-entry"], system="s", prompt="p", timeout_s=7, project_root=tmp_path)
    assert floor.status == "unroutable" and floor.entry is None
    assert floor.floor["kind"] == "no-usable-routing-target"
    assert [d["id"] for d in floor.floor["dispositions"]] == ["ghost-entry"] and "ghost-entry" in floor.detail
