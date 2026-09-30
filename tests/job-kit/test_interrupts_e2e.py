"""End-to-end durable interrupts through ``cli.main``.

A real contract script (a subprocess) requests an interrupt through the request
file; the model transport is a hermetic fake. Each test drives the verbs an
operator would run -- ``run``, ``status``, ``resolve``, ``resume``, ``events`` --
and checks exit codes, recorded state and the mixed v1/v2 event stream.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from llm_scripting_kit.completion import BackendSelection, Capabilities, LLMResponse

from bootstrap_lib.execution_event import SCHEMA_V1, SCHEMA_V2, read_jsonl, validate_stream

from job_kit import cli
import job_kit.run as run_module

# Requests an interrupt on its first run; a continuation (told apart by
# JOB_KIT_INTERRUPT_RESOLUTION) records the resolution it received and succeeds.
CONTRACT_SCRIPT = r"""
import json, os, sys

log_path = sys.argv[1]
resolution_path = os.environ.get("JOB_KIT_INTERRUPT_RESOLUTION")
if resolution_path is None:
    request = {
        "schema": "job-kit.interrupt-request/v1",
        "kind": "approval",
        "request_schema": {"type": "object", "required": ["approved"],
                           "properties": {"approved": {"const": True}}},
        "payload": {"action": "push release tag"},
    }
    if len(sys.argv) > 2:
        request["expires_in_s"] = int(sys.argv[2])
    with open(os.environ["JOB_KIT_INTERRUPT_REQUEST"], "w", encoding="utf-8") as handle:
        json.dump(request, handle)
else:
    with open(resolution_path, "r", encoding="utf-8") as handle:
        document = json.load(handle)
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(document) + "\n")
"""


class _Backend:
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system: str, user: str, *, model: str, options: Any = None) -> LLMResponse:
        self.calls += 1
        return LLMResponse(text="answer", model="fake-model")

    def classify_halt(self, exc: BaseException) -> None:
        return None


class Flow:
    """One jobs file, one ledger and one fake transport."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *extra: str) -> None:
        self.tmp_path = tmp_path
        self.store = tmp_path / "ledger" / "runs.sqlite3"
        self.script = tmp_path / "contract.py"
        self.script.write_text(CONTRACT_SCRIPT, encoding="utf-8")
        self.log = tmp_path / "continuations.jsonl"
        self.backend = _Backend()
        self.streams = 0
        monkeypatch.setattr(
            run_module, "adapter_capabilities", lambda: {"fake": Capabilities(adapter="fake")}
        )
        monkeypatch.setattr(
            run_module,
            "create_backend",
            lambda endpoint, **_: BackendSelection(endpoint, "fake", self.backend, "fake-model"),
        )
        work = tmp_path / "work"
        work.mkdir()
        document = {
            "max_parallel": 1,
            "workspace_root": str(tmp_path / "ws"),
            "jobs": [
                {
                    "id": "release",
                    "prompt": {"user": "release"},
                    "models": ["fake-endpoint"],
                    "directory": str(work),
                    "contract": {
                        "command": [sys.executable, str(self.script), str(self.log), *extra],
                        "directory": str(work),
                    },
                }
            ],
        }
        self.jobs = tmp_path / "jobs.json"
        self.jobs.write_text(json.dumps(document), encoding="utf-8")

    def verb(self, capsys: Any, *args: str) -> tuple[int, dict]:
        capsys.readouterr()
        code = cli.main([*args])
        out = capsys.readouterr().out
        return code, json.loads(out.splitlines()[-1]) if out.strip() else {}

    def run(self, capsys: Any) -> tuple[int, dict]:
        return self.verb(
            capsys, "run", str(self.jobs), "--store", str(self.store), "--run-id", "r"
        )

    def resume(self, capsys: Any) -> tuple[int, dict]:
        return self.verb(capsys, "resume", "r", "--store", str(self.store))

    def status(self, capsys: Any) -> dict:
        code, body = self.verb(capsys, "status", "r", "--store", str(self.store))
        assert code == cli.EXIT_OK
        return body

    def resolve(self, capsys: Any, *args: str) -> tuple[int, dict]:
        return self.verb(capsys, "resolve", "r", "1", *args, "--store", str(self.store))

    def stream(self, capsys: Any) -> list[dict]:
        self.streams += 1
        out = self.tmp_path / f"stream-{self.streams}.jsonl"
        code, _ = self.verb(capsys, "events", "r", "--store", str(self.store), "--out", str(out))
        assert code == cli.EXIT_OK
        events = list(read_jsonl(out))
        assert validate_stream(events)
        return events

    def continuations(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


def _phases(stream: list[dict]) -> list[str]:
    return [e["payload"]["phase"] for e in stream if e["event"] == "interrupt"]


def _lapse(monkeypatch: pytest.MonkeyPatch) -> None:
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 3600)


def test_request_resolve_resume_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    flow = Flow(tmp_path, monkeypatch)

    code, _ = flow.run(capsys)
    assert code == cli.EXIT_WAITING == 4
    assert flow.backend.calls == 1
    status = flow.status(capsys)
    assert status["run"]["status"] == "waiting"
    assert [job["state"] for job in status["jobs"]] == ["waiting"]
    assert status["interrupts"][0]["payload"] == {"action": "push release tag"}
    assert flow.continuations() == []

    code, resolved = flow.resolve(capsys, "--input", '{"approved": true}')
    assert code == cli.EXIT_OK
    assert resolved["outcome"] == "recorded" and resolved["state"] == "waiting"

    code, body = flow.resume(capsys)
    assert code == cli.EXIT_OK
    assert body["jobs"][0]["state"] == "accepted"
    assert flow.backend.calls == 1  # the continuation makes no model call
    [document] = flow.continuations()
    assert document["outcome"] == "answered" and document["input"] == {"approved": True}

    stream = flow.stream(capsys)
    assert _phases(stream) == ["requested", "resolved"]
    by_name = {e["event"]: e["schema"] for e in stream}
    assert by_name["interrupt"] == SCHEMA_V2
    assert by_name["result"] == by_name["terminal"] == SCHEMA_V1
    assert stream[-1]["event"] == "terminal"
    assert stream[-1]["payload"]["state"] == "accepted"


def test_replayed_resolution_is_idempotent_and_conflict_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    flow = Flow(tmp_path, monkeypatch)
    flow.run(capsys)
    flow.resolve(capsys, "--input", '{"approved": true}')
    code, body = flow.resolve(capsys, "--input", '{"approved":true}')
    assert (code, body["outcome"]) == (cli.EXIT_OK, "replayed")
    code, body = flow.resolve(capsys, "--reject")
    assert code == cli.EXIT_FAILURE and body["refused"] == "conflict"


def test_invalid_answer_is_refused_and_the_job_keeps_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    flow = Flow(tmp_path, monkeypatch)
    flow.run(capsys)
    code, body = flow.resolve(capsys, "--input", '{"approved": false}')
    assert code == cli.EXIT_FAILURE and body["refused"] == "schema"
    code, _ = flow.resume(capsys)
    assert code == cli.EXIT_WAITING
    assert flow.continuations() == []


def test_reject_ends_the_job_without_a_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    flow = Flow(tmp_path, monkeypatch)
    assert flow.run(capsys)[0] == cli.EXIT_WAITING

    code, resolved = flow.resolve(capsys, "--reject", "--reason", "not today")
    assert code == cli.EXIT_OK and resolved["state"] == "operator_rejected"

    code, body = flow.resume(capsys)
    assert code == cli.EXIT_FAILURE  # a rejection is terminal and not accepted
    assert body["jobs"][0]["state"] == "operator_rejected"
    assert flow.continuations() == []
    assert flow.backend.calls == 1

    stream = flow.stream(capsys)
    assert _phases(stream) == ["requested", "rejected"]
    assert stream[-1]["event"] == "terminal"
    assert stream[-1]["payload"]["state"] == "operator_rejected"
    assert stream[-1]["payload"]["reason"] == "not today"


def test_lapsed_expiry_is_recorded_by_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    flow = Flow(tmp_path, monkeypatch, "1")
    assert flow.run(capsys)[0] == cli.EXIT_WAITING
    assert flow.status(capsys)["jobs"][0]["effective_state"] == "waiting"

    _lapse(monkeypatch)
    lapsed = flow.status(capsys)
    assert lapsed["jobs"][0]["state"] == "waiting"  # status does not write
    assert lapsed["jobs"][0]["effective_state"] == "expired"

    code, body = flow.resume(capsys)
    assert code == cli.EXIT_FAILURE
    assert body["jobs"][0]["state"] == "expired"
    assert flow.continuations() == []

    stream = flow.stream(capsys)
    assert _phases(stream) == ["requested", "expired"]
    assert stream[-1]["payload"]["state"] == "expired"


def test_resolve_after_lapse_records_expiry_and_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    flow = Flow(tmp_path, monkeypatch, "1")
    flow.run(capsys)
    _lapse(monkeypatch)

    code, body = flow.resolve(capsys, "--input", '{"approved": true}')
    assert code == cli.EXIT_FAILURE and body["refused"] == "expired"
    assert flow.status(capsys)["jobs"][0]["state"] == "expired"
    assert _phases(flow.stream(capsys)) == ["requested", "expired"]
    code, _ = flow.resume(capsys)
    assert code == cli.EXIT_FAILURE
    assert flow.continuations() == []
