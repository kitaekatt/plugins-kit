"""/direct write and operator-question verbs end to end under grants (unit U11).

Nothing on the path is a double. The relay is the real ``llm_kit.relay.server.RelayServer`` (uvicorn on
loopback, a tmp HOME and state directory, a real job ledger holding open interrupts); the grant hook
(``userpromptsubmit/direct_grant.py``) and the gate (``pretooluse/direct_gate.py``) run as subprocesses fed
hook JSON on stdin, as Claude Code runs them; and the relay call is the real ``bin/llm-kit relay client``
shim in a subprocess, run by ``bash -c`` on the exact command string the skill prescribes.

For each verb the test types a ``/direct`` line (mint), asks the gate about the prescribed command (allowed
once, denied for a different target, denied after the use is spent), runs the call, and then bypasses the
gate to show the relay itself refuses a spent or foreign grant (the relay enforces the same grant).

The command shapes come from the skill's verb table: ``TEMPLATES`` below must appear verbatim in
SKILL.md (test_templates_are_the_skill_table), so a skill edit that changes a prescribed shape turns that
test red instead of silently diverging from what is exercised here.

Edits that turn the guards red (each shown red when this file was written):

* the gate's answer target -- in direct_gate.parse_client_args drop ``"answer"`` from
  ``verb in ("read", "send", "answer")``: every answer test goes red (the gate denies the covered call).
* the grant hook's answer target -- in direct_grant._SHAPES change ``"answer": (ONE_USE, "one")`` to
  ``(ONE_USE, "none")``: the answer tests go red (the grant covers no target).
* one-use -- in direct_grant change ``"send": (ONE_USE, "one")`` to ``(READ_USES, "one")``: the send
  second-use tests go red.
* the gate's refusal -- in direct_gate.run change ``if result.get("covered") is not True:`` to
  ``if False:``: every "denied" assertion goes red.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from llm_kit.completion.types import LLMResponse
from llm_kit.jobs.interrupts import REQUEST_ENVELOPE_V1
from llm_kit.jobs.model import (
    Acceptance,
    Attempt,
    Contract,
    InterruptRequest,
    Job,
    JobState,
    Prompt,
    Usage,
)
from llm_kit.jobs.store import JobStore
from llm_kit.registry import consent
from llm_kit.relay import config as C
from llm_kit.relay import discovery as D
from llm_kit.relay import server as S
from llm_kit.relay.client import HttpRelayClient

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "llm-kit"
SKILL = PLUGIN / "skills" / "direct" / "SKILL.md"
GRANT_HOOK = PLUGIN / "hooks" / "userpromptsubmit" / "direct_grant.py"
GATE_HOOK = PLUGIN / "hooks" / "pretooluse" / "direct_gate.py"
SHIM = PLUGIN / "bin" / "llm-kit"

LABEL = "e2ebox"
NONCE = "nonce-" + "x" * 20
APPROVAL = {
    "type": "object",
    "required": ["approved"],
    "properties": {"approved": {"const": True}},
    "additionalProperties": False,
}

#: The prescribed call per verb, as the skill's verb table writes it (``llm-kit`` is the shim path).
TEMPLATES = {
    "send": "relay client send ADDRESS --session S --kind K --body 'TEXT'",
    "replies_id": "relay client replies --session S --message-id ID",
    "replies_address": "relay client replies --session S --address A",
    "questions": "relay client questions --session S [--machine M] [--state open|all]",
    "answer": "relay client answer ID --session S --decision D [--input 'JSON'] [--reason 'TEXT']",
}


def q(value: str) -> str:
    """One typed value in single quotes, an embedded ``'`` written ``'\\''`` (the skill's quoting rule)."""
    return "'" + value.replace("'", "'\\''") + "'"


def call_prefix() -> str:
    return f"{SHIM} relay client"


# --------------------------------------------------------------------------
# job ledger (the setup tests/llm-kit/test_relay_questions.py in katsoft-libs uses)
# --------------------------------------------------------------------------

def _job(directory: Path, job_id: str) -> Job:
    return Job(
        id=job_id, prompt=Prompt(user=f"run {job_id}"), models=("fake",), directory=directory, max_attempts=2,
        contract=Contract(command=(sys.executable, "-c", "pass"), directory=directory),
    )


def add_waiting_job(path: Path, run_id: str, job_id: str = "job") -> None:
    """A real ledger run whose job is waiting on one unresolved approval interrupt."""
    store = JobStore(path)
    store.create_run(run_id, [_job(path.parent, job_id)])
    reservation = store.reserve_attempt(
        run_id, job_id, endpoint="fake-endpoint", backend="fake-backend", model="fake-model",
        reserved_at="2026-09-01T00:00:00Z",
    )
    store.arm_reservation(run_id, job_id, reservation.attempt_no, invoke_armed_at="2026-09-01T00:00:01Z")
    attempt = Attempt(
        run_id=run_id, job_id=job_id, attempt_no=reservation.attempt_no, endpoint="fake-endpoint",
        backend="fake-backend", model="fake-model", status="completed", started_at="2026-09-01T00:00:01Z",
        ended_at="2026-09-01T00:00:02Z", usage=Usage(input_tokens=3, output_tokens=5), response_text="answer",
        acceptance=Acceptance(command=("contract",), directory=Path.cwd(), exit_code=0, stdout="", stderr="",
                              wall_ms=1, accepted=False, outcome="interrupt_requested"),
    )
    request = InterruptRequest(envelope=REQUEST_ENVELOPE_V1, kind="approval", request_schema=dict(APPROVAL),
                               payload={"action": "push tag"}, expires_in_s=None)
    store.append_attempt(attempt, interrupt=request, at=time.time())
    assert store.get_job(run_id, job_id).state is JobState.WAITING


# --------------------------------------------------------------------------
# the stack
# --------------------------------------------------------------------------

class _Backend:
    name = "fake"

    def complete(self, system, user, *, model, options=None):  # never called: no ask verb here
        return LLMResponse(text="x", model=model, finish_reason="stop", input_tokens=1, output_tokens=1)

    def classify_halt(self, exc):  # pragma: no cover
        return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Stack:
    """A real relay and the environment the hooks and the client shim read, all under tmp."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".claude" / "env.json").write_text(json.dumps(
            {"machines": {socket.gethostname(): {"os": "macos", "tailscale_hostname": LABEL}}}))
        self.state = self.home / "state"
        self.cfg = self.home / "cfg"
        (self.cfg / "llm-kit").mkdir(parents=True)
        self.ledger = tmp_path / "jobs-main.sqlite3"
        add_waiting_job(self.ledger, "run-a")
        add_waiting_job(self.ledger, "run-b")
        self.port = _free_port()
        (self.cfg / "llm-kit" / "relay.yaml").write_text(
            f"relay:\n  host: 127.0.0.1\n  port: {self.port}\nledgers:\n  - {self.ledger}\n")
        claude_home, codex_home = tmp_path / "claude-home", tmp_path / "codex-home"
        (claude_home / "sessions").mkdir(parents=True)
        (claude_home / "projects" / "-tmp-proj").mkdir(parents=True)
        (codex_home / "sessions").mkdir(parents=True)
        config = C.load_config(
            env_json=self.home / ".claude" / "env.json", environ={}, hostname=socket.gethostname,
            config_path=self.cfg / "llm-kit" / "relay.yaml", state_root=self.state / "llm-kit" / "relay")
        assert config.relay_port == self.port and config.ledgers == (self.ledger,)
        self.server = S.RelayServer(
            config, roots=D.DiscoveryRoots(claude_home, codex_home), probe=D.PsutilProbe(),
            backend_factory=lambda entry: SimpleNamespace(backend=_Backend(), model=entry.model),
            policy_loader=lambda scope: consent.policy(scope, path=tmp_path / "consent.json"),
            poll_interval_s=0.05, maintain_interval_s=3600.0,
        ).start()
        assert self.server.url.endswith(f":{self.port}"), self.server.url
        self.client = HttpRelayClient(self.server.url, machine=LABEL, relay_token=self.server.prepared.relay_token,
                                      hub_bearer=self.server.prepared.hub_bearer)
        # The shim looks for its venv under $CLAUDE_BOOTSTRAP_DATA_ROOT; this wrapper is that venv's interpreter.
        wrapper = tmp_path / "bootstrap-data" / "plugins-kit" / "llm-kit" / ".venv" / "bin" / "python"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        wrapper.chmod(0o755)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("LLM_KIT_", "XDG_", "CLAUDE_PLUGIN", "CLAUDE_BOOTSTRAP"))}
        env.update({
            "HOME": str(self.home), "XDG_STATE_HOME": str(self.state), "XDG_CONFIG_HOME": str(self.cfg),
            "CLAUDE_BOOTSTRAP_DATA_ROOT": str(tmp_path / "bootstrap-data"), "_BOOTSTRAP_GUARD_SKIP_REEXEC": "1",
        })
        self.env = env
        self.session = str(uuid.uuid4())
        self.nonce = NONCE

    def close(self) -> None:
        self.client.close()
        self.server.stop()

    # -- sessions the relay can deliver to -------------------------------------------------

    def address(self, session: str) -> str:
        return f"{LABEL}/claude/{session}"

    def register_channel(self) -> str:
        """A registered, accepting session (what the llm-kit channel server does); returns its session id."""
        session = str(uuid.uuid4())
        self.channel(session, "register", {
            "session_id": session, "harness": "claude", "pid": os.getpid(), "cwd": "/Users/u/Dev/app",
            "version": "2.1.316", "accepts": ["note", "instruction", "question"], "channel_nonce": NONCE})
        return session

    def channel(self, session: str, verb: str, params: dict) -> dict:
        return self.client.call(verb, params, origin=f"channel:{self.address(session)}")

    def deliver(self, session: str, registration: dict) -> list[dict]:
        creds = {"registration_id": registration["registration_id"], "session_id": session, "channel_nonce": NONCE}
        polled = self.channel(session, "poll", dict(creds, wait_s=0))
        for message in polled["messages"]:
            self.channel(session, "ack", dict(creds, message_id=message["id"]))
        return polled["messages"]

    # -- the hooks and the client, run the way Claude Code runs them --------------------------

    def _hook(self, script: Path, payload: dict) -> tuple[int, Optional[dict], str]:
        done = subprocess.run([sys.executable, str(script)], input=json.dumps(payload), capture_output=True,
                              text=True, env=self.env, timeout=30)
        return done.returncode, (json.loads(done.stdout) if done.stdout.strip() else None), done.stderr

    def type_direct(self, line: str, session: Optional[str] = None) -> str:
        """The user types ``line``: runs the grant hook; returns the context line the model would see."""
        rc, out, err = self._hook(GRANT_HOOK, {"session_id": session or self.session,
                                               "hook_event_name": "UserPromptSubmit", "prompt": line, "cwd": "/tmp"})
        assert (rc, err) == (0, ""), (rc, err)
        assert out is not None and "decision" not in out, out
        context = out["hookSpecificOutput"]["additionalContext"]
        assert "a relay grant was minted" in context, context
        return context

    def gate(self, command: str, session: Optional[str] = None) -> Optional[str]:
        """The PreToolUse hook on a Bash call: the denial reason, or None when it lets the call through."""
        rc, out, err = self._hook(GATE_HOOK, {
            "session_id": session or self.session, "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": command}, "cwd": "/tmp"})
        assert (rc, err) == (0, ""), (rc, err)
        if out is None:
            return None
        decision = out["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny", out
        return decision["permissionDecisionReason"]

    def run(self, command: str) -> tuple[int, Optional[dict], Optional[dict]]:
        """The Bash tool running ``command``: exit code, stdout JSON, stderr JSON (the client's contract)."""
        done = subprocess.run(["bash", "-c", command], capture_output=True, text=True, env=self.env, timeout=60)
        out = json.loads(done.stdout) if done.stdout.strip() else None
        err = json.loads(done.stderr) if done.stderr.strip() else None
        return done.returncode, out, err

    def refused_by_gate(self, command: str) -> str:
        reason = self.gate(command)
        assert reason is not None and reason.startswith("Refused"), reason
        return reason

    def operator(self, verb: str, params: dict) -> dict:
        return self.client.call(verb, params, origin=f"operator:{LABEL}")

    def resolutions(self) -> list[tuple]:
        conn = sqlite3.connect(self.ledger)
        try:
            return conn.execute(
                "SELECT interrupt_id, outcome, input_json, reason FROM interrupt_resolutions ORDER BY interrupt_id"
            ).fetchall()
        finally:
            conn.close()

    # -- the commands the skill prescribes ---------------------------------------------------

    def send_cmd(self, to: str, body: str, kind: str = "note", session: Optional[str] = None) -> str:
        return f"{call_prefix()} send {q(to)} --session {session or self.session} --kind {q(kind)} --body {q(body)}"

    def replies_cmd(self, *, message_id: Optional[str] = None, address: Optional[str] = None, wait: Optional[int] = None) -> str:
        which = f"--message-id {q(message_id)}" if message_id else f"--address {q(address)}"
        tail = f" --wait {wait}" if wait is not None else ""
        return f"{call_prefix()} replies --session {self.session} {which}{tail}"

    def questions_cmd(self, machine: Optional[str] = None, state: Optional[str] = None) -> str:
        cmd = f"{call_prefix()} questions --session {self.session}"
        if machine is not None:
            cmd += f" --machine {q(machine)}"
        if state is not None:
            cmd += f" --state {q(state)}"
        return cmd

    def answer_cmd(self, qid: str, decision: str, input_json: Optional[str] = None, reason: Optional[str] = None) -> str:
        cmd = f"{call_prefix()} answer {q(qid)} --session {self.session} --decision {q(decision)}"
        if input_json is not None:
            cmd += f" --input {q(input_json)}"
        if reason is not None:
            cmd += f" --reason {q(reason)}"
        return cmd


@pytest.fixture()
def stack(tmp_path):
    built = Stack(tmp_path)
    try:
        yield built
    finally:
        built.close()


def error_code(err: Optional[dict]) -> str:
    assert err is not None, "the client printed no error object"
    body = err.get("error", err)
    return body["code"]


# --------------------------------------------------------------------------
# the prescribed shapes are the skill's
# --------------------------------------------------------------------------

def test_templates_are_the_skill_table():
    text = SKILL.read_text(encoding="utf-8").replace("`llm-kit relay client", "`relay client").replace("\\|", "|")
    for name, template in TEMPLATES.items():
        if name == "replies_address":
            continue
        assert template in text, (name, template)
    # replies --address is documented as the in-place alternative to --message-id
    assert "`--address A` in place of `--message-id`" in text


# --------------------------------------------------------------------------
# send
# --------------------------------------------------------------------------

AWKWARD_BODY = "it's $(echo hi) \"q\" ; `x` | a && b"


def test_send_is_granted_once_for_the_typed_address_and_delivers_the_body_verbatim(stack):
    target = stack.register_channel()
    to, other_to = stack.address(target), stack.address(str(uuid.uuid4()))
    mine = stack.send_cmd(to, AWKWARD_BODY, kind="question")

    # No /direct typed yet: the gate refuses and the relay refuses.
    assert "grant_required" in stack.refused_by_gate(mine)
    rc, _, err = stack.run(mine)
    assert (rc, error_code(err)) == (1, "grant_required")

    context = stack.type_direct(f"/direct send {to} --kind question {AWKWARD_BODY}")
    assert f"verb send_message, targets {to}, 300 s, 1 use(s)" in context

    # A different target is not covered, and asking does not spend the grant.
    assert "grant_required" in stack.refused_by_gate(stack.send_cmd(other_to, "x"))
    assert stack.gate(mine) is None

    rc, out, err = stack.run(mine)
    assert (rc, err) == (0, None), (rc, err)
    message = out["message"]
    assert (message["to"], message["kind"], message["body"]) == (to, "question", AWKWARD_BODY)

    # One use: the gate refuses the repeat, and so does the relay if the gate is bypassed.
    assert stack.refused_by_gate(mine)
    rc, out, err = stack.run(mine)
    assert (rc, out, error_code(err)) == (1, None, "grant_required")


def test_send_to_an_unknown_session_passes_the_gate_and_the_relay_reports_it(stack):
    ghost = stack.address(str(uuid.uuid4()))
    command = stack.send_cmd(ghost, "hello")
    stack.type_direct(f"/direct send {ghost} hello")
    assert stack.gate(command) is None
    rc, out, err = stack.run(command)
    assert rc == 1 and out is None
    assert error_code(err) == "target_not_found"


def test_send_note_is_the_default_kind_the_skill_names(stack):
    target = stack.register_channel()
    to = stack.address(target)
    stack.type_direct(f"/direct send {to} plain words")
    rc, out, err = stack.run(stack.send_cmd(to, "plain words"))
    assert (rc, err) == (0, None) and out["message"]["kind"] == "note"


# --------------------------------------------------------------------------
# replies
# --------------------------------------------------------------------------

def _sent_and_answered(stack):
    """A sent question and the receiving channel's reply to it; returns (address, message id, session)."""
    session = str(uuid.uuid4())
    registration = stack.channel(session, "register", {
        "session_id": session, "harness": "claude", "pid": os.getpid(), "cwd": "/Users/u/Dev/app",
        "version": "2.1.316", "accepts": ["note", "instruction", "question"], "channel_nonce": NONCE})
    to = stack.address(session)
    stack.type_direct(f"/direct send {to} --kind question ready?")
    rc, out, err = stack.run(stack.send_cmd(to, "ready?", kind="question"))
    assert (rc, err) == (0, None), err
    message_id = out["message"]["id"]
    assert [m["id"] for m in stack.deliver(session, registration)] == [message_id]
    creds = {"registration_id": registration["registration_id"], "session_id": session, "channel_nonce": NONCE}
    stack.channel(session, "reply", dict(creds, in_reply_to=message_id, state="answered", body="ready"))
    return to, message_id, session


def test_replies_by_message_id_has_eight_uses_and_covers_only_that_message(stack):
    to, message_id, _ = _sent_and_answered(stack)
    context = stack.type_direct(f"/direct replies --message-id {message_id}")
    assert f"verb get_replies, targets {message_id}, 300 s, 8 use(s)" in context
    command = stack.replies_cmd(message_id=message_id)

    assert "grant_required" in stack.refused_by_gate(stack.replies_cmd(message_id="msg_" + "0" * 20))
    assert "grant_required" in stack.refused_by_gate(stack.replies_cmd(address=to))
    for _ in range(2):  # a read grant lets the same call page or poll again
        assert stack.gate(command) is None
        rc, out, err = stack.run(command)
        assert (rc, err) == (0, None), err
        assert [(m["state"], m["reply"]["body"]) for m in out["messages"]] == [("answered", "ready")]
    # The long-poll form is the same verb and target.
    assert stack.gate(stack.replies_cmd(message_id=message_id, wait=1)) is None


def test_replies_by_address_is_covered_by_the_address_grant(stack):
    to, message_id, _ = _sent_and_answered(stack)
    stack.type_direct(f"/direct replies --address {to}")
    command = stack.replies_cmd(address=to)
    assert stack.gate(command) is None
    rc, out, err = stack.run(command)
    assert (rc, err) == (0, None), err
    assert [m["id"] for m in out["messages"]] == [message_id]
    assert "grant_required" in stack.refused_by_gate(stack.replies_cmd(message_id=message_id))


def test_replies_grant_is_spent_after_eight_calls(stack):
    to, message_id, _ = _sent_and_answered(stack)
    stack.type_direct(f"/direct replies --message-id {message_id}")
    command = stack.replies_cmd(message_id=message_id)
    for n in range(8):
        rc, _, err = stack.run(command)
        assert (rc, err) == (0, None), (n, err)
    assert stack.refused_by_gate(command)
    rc, _, err = stack.run(command)
    assert (rc, error_code(err)) == (1, "grant_required")


# --------------------------------------------------------------------------
# questions
# --------------------------------------------------------------------------

def test_questions_lists_the_open_interrupts_of_the_watched_ledger(stack):
    context = stack.type_direct("/direct questions")
    assert "verb list_questions, targets no target, 300 s, 8 use(s)" in context
    command = stack.questions_cmd()
    assert stack.gate(command) is None
    rc, out, err = stack.run(command)
    assert (rc, err) == (0, None), err
    assert [(x["source"]["run_id"], x["state"], x["kind"]) for x in out["questions"]] == [
        ("run-a", "open", "approval"), ("run-b", "open", "approval")]
    assert all(x["id"].startswith(f"q:{LABEL}/jobs-main:run-") for x in out["questions"])
    assert out["ledgers"] == [{"ledger": "jobs-main", "status": "ok"}]
    # The typed grant has no target, so a call that names a machine is a different target.
    assert "grant_required" in stack.refused_by_gate(stack.questions_cmd(machine="elsewhere"))
    # --state is not a target.
    assert stack.gate(stack.questions_cmd(state="all")) is None


def test_questions_with_a_typed_machine_covers_that_machine_only(stack):
    stack.type_direct(f"/direct questions {LABEL} --state all")
    assert stack.gate(stack.questions_cmd(machine=LABEL, state="all")) is None
    rc, out, err = stack.run(stack.questions_cmd(machine=LABEL, state="all"))
    assert (rc, err) == (0, None) and len(out["questions"]) == 2
    assert "grant_required" in stack.refused_by_gate(stack.questions_cmd(machine="elsewhere"))
    assert "grant_required" in stack.refused_by_gate(stack.questions_cmd())


def test_questions_for_another_machine_reports_it_unreachable_not_empty(stack):
    stack.type_direct("/direct questions elsewhere")
    command = stack.questions_cmd(machine="elsewhere")
    assert stack.gate(command) is None
    rc, out, err = stack.run(command)
    assert (rc, err) == (0, None)
    assert out["questions"] == [] and out["machines"][0]["status"] == "unreachable"


# --------------------------------------------------------------------------
# answer
# --------------------------------------------------------------------------

def _question_ids(stack) -> list[str]:
    """The ids as the user would read them from /direct questions (never computed by the test)."""
    stack.type_direct("/direct questions")
    rc, out, err = stack.run(stack.questions_cmd())
    assert (rc, err) == (0, None), err
    return [x["id"] for x in out["questions"]]


def test_answer_resolves_one_interrupt_under_a_one_use_grant_for_that_question_id(stack):
    qid_a, qid_b = _question_ids(stack)
    command = stack.answer_cmd(qid_a, "answer", '{"approved": true}')

    assert "grant_required" in stack.refused_by_gate(command)
    rc, _, err = stack.run(command)
    assert (rc, error_code(err)) == (1, "grant_required")
    assert stack.resolutions() == []

    context = stack.type_direct(f'/direct answer {qid_a} --decision answer --input {{"approved": true}}')
    assert f"verb answer_question, targets {qid_a}, 300 s, 1 use(s)" in context

    # The other question is a different target; asking about it spends nothing.
    assert "grant_required" in stack.refused_by_gate(stack.answer_cmd(qid_b, "answer", '{"approved": true}'))
    assert stack.gate(command) is None

    rc, out, err = stack.run(command)
    assert (rc, err) == (0, None), err
    rows = stack.resolutions()
    assert [(r[1], json.loads(r[2])) for r in rows] == [("answered", {"approved": True})]
    assert (out["id"], out["state"]) == (qid_a, "answered")

    # Spent: the gate refuses the repeat, and the relay does too when the gate is bypassed.
    assert stack.refused_by_gate(command)
    rc, _, err = stack.run(command)
    assert (rc, error_code(err)) == (1, "grant_required")
    assert len(stack.resolutions()) == 1
    # The grant never reached the other question.
    rc, _, err = stack.run(stack.answer_cmd(qid_b, "answer", '{"approved": true}'))
    assert (rc, error_code(err)) == (1, "grant_required")
    assert len(stack.resolutions()) == 1


def test_answer_reject_records_the_reason(stack):
    qid_a, _ = _question_ids(stack)
    stack.type_direct(f"/direct answer {qid_a} --decision reject --reason no thanks")
    command = stack.answer_cmd(qid_a, "reject", reason="it's not time")
    assert stack.gate(command) is None
    rc, _, err = stack.run(command)
    assert (rc, err) == (0, None), err
    assert [(r[1], r[3]) for r in stack.resolutions()] == [("rejected", "it's not time")]


def test_a_refused_answer_refunds_its_use_so_the_same_grant_can_answer_correctly(stack):
    qid_a, _ = _question_ids(stack)
    stack.type_direct(f'/direct answer {qid_a} --decision answer --input {{"approved": true}}')
    wrong = stack.answer_cmd(qid_a, "answer", '{"approved": false}')
    assert stack.gate(wrong) is None
    rc, out, err = stack.run(wrong)
    assert (rc, out, error_code(err)) == (1, None, "answer_invalid")
    assert stack.resolutions() == []
    right = stack.answer_cmd(qid_a, "answer", '{"approved": true}')
    assert stack.gate(right) is None  # the refused call gave its use back
    rc, _, err = stack.run(right)
    assert (rc, err) == (0, None), err
    assert [r[1] for r in stack.resolutions()] == ["answered"]


def test_answer_needs_input_and_reject_refuses_it_and_neither_spends_the_grant(stack):
    """The skill's answer row states this; it is the relay's schema, not the hook's."""
    qid_a, _ = _question_ids(stack)
    stack.type_direct(f"/direct answer {qid_a} --decision answer")
    for command in (stack.answer_cmd(qid_a, "answer"), stack.answer_cmd(qid_a, "reject", '{"approved": true}')):
        assert stack.gate(command) is None
        rc, out, err = stack.run(command)
        assert (rc, out, error_code(err)) == (1, None, "bad_request")
    assert stack.resolutions() == []
    assert stack.gate(stack.answer_cmd(qid_a, "answer", '{"approved": true}')) is None  # still live


def test_answer_records_the_resolution_and_leaves_the_job_waiting(stack):
    qid_a, _ = _question_ids(stack)
    stack.type_direct(f"/direct answer {qid_a} --decision answer --input x")
    rc, out, err = stack.run(stack.answer_cmd(qid_a, "answer", '{"approved": true}'))
    assert (rc, err) == (0, None), err
    assert (out["decision"], out["state"], out["job_state"], out["replayed"]) == ("answer", "answered", "waiting", False)


def test_answered_question_leaves_the_open_list_and_shows_in_all(stack):
    qid_a, qid_b = _question_ids(stack)
    stack.type_direct(f'/direct answer {qid_a} --decision answer --input {{"approved": true}}')
    rc, _, err = stack.run(stack.answer_cmd(qid_a, "answer", '{"approved": true}'))
    assert (rc, err) == (0, None), err
    stack.type_direct("/direct questions --state all")
    rc, out, err = stack.run(stack.questions_cmd(state="all"))
    assert (rc, err) == (0, None)
    assert {x["id"]: x["state"] for x in out["questions"]} == {qid_a: "answered", qid_b: "open"}


# --------------------------------------------------------------------------
# a selector typed as the send target is resolved the same way by mint, gate and call
# --------------------------------------------------------------------------

def test_send_to_a_selector_is_granted_for_the_address_it_resolves_to(stack):
    session = str(uuid.uuid4())
    stack.channel(session, "register", {
        "session_id": session, "harness": "claude", "pid": os.getpid(), "cwd": "/Users/u/Dev/app",
        "version": "2.1.316", "accepts": ["note"], "channel_nonce": NONCE, "task": "plugins-kit/harness-messaging"})
    selector = "task:plugins-kit/harness-messaging"
    context = stack.type_direct(f"/direct send {selector} hello there")
    assert f"targets {stack.address(session)}," in context  # the grant names the resolved address
    command = stack.send_cmd(selector, "hello there")
    assert stack.gate(command) is None
    assert stack.gate(stack.send_cmd(stack.address(session), "hello there")) is None  # same session by address
    rc, out, err = stack.run(command)
    assert (rc, err) == (0, None), err
    assert out["message"]["to"] == stack.address(session)
    assert stack.refused_by_gate(command)
