"""/direct end to end: one typed /direct enables every verb, the gate grants each call, the relay enforces.

Owner ruling 2026-11-11: typing /direct once enables every /direct verb for the rest of the session, /clear
turns it off, sending shows no permission prompt, and what is sent is visible in the session.

Nothing on the path is a double. The relay is the real ``llm_kit.relay.server.RelayServer`` (uvicorn on
loopback, a tmp HOME and state directory, a real job ledger holding open interrupts); the hooks
(``userpromptsubmit/direct_enable.py``, ``pretooluse/direct_gate.py``, ``sessionend/direct_revoke.py``) run as
subprocesses fed hook JSON on stdin, as Claude Code runs them; and the relay call is the real
``bin/llm-kit relay client`` shim in a subprocess, run by ``bash -c`` on the exact command string the skill
prescribes.

The gate mints a one-use grant for each call it allows, so the relay's own grant check is what makes a call
that skipped the gate fail: ``run_ungated`` runs the same command without the gate and must be refused
``grant_required``.

The command shapes come from the skill's verb table: ``TEMPLATES`` below must appear verbatim in
SKILL.md (test_templates_are_the_skill_table), so a skill edit that changes a prescribed shape turns that
test red instead of silently diverging from what is exercised here.

Edits that turn the guards red (each shown red when this file was written):

* the gate's answer target -- in direct_gate.parse_client_args drop ``"answer"`` from
  ``verb in ("read", "send", "answer")``: every answer test goes red (the minted grant names no target).
* one use per gated call -- in direct_gate set ``GRANT_USES = 2``: test_each_gated_send_spends_its_own_grant
  goes red (an ungated repeat passes the relay).
* the gate's refusal of a session that is not enabled -- in direct_gate.run change ``if record is None:`` to
  ``if False:``: test_nothing_runs_before_the_owner_types_direct goes red.
* revocation -- in sessionend/direct_revoke.run return ``False`` without calling ``R.revoke``:
  test_session_end_turns_it_off goes red.
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
ENABLE_HOOK = PLUGIN / "hooks" / "userpromptsubmit" / "direct_enable.py"
GATE_HOOK = PLUGIN / "hooks" / "pretooluse" / "direct_gate.py"
REVOKE_HOOK = PLUGIN / "hooks" / "sessionend" / "direct_revoke.py"
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
    "sessions": "relay client sessions --session S [--machine M] [--harness claude|codex] "
                "[--state live|all|live-accepting|live-read-only|idle|ended]",
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
        self.claude_home = claude_home
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

    def type_direct(self, line: str = "/direct", session: Optional[str] = None) -> str:
        """The user types ``line``: runs the enable hook; returns the context line the model would see."""
        rc, out, err = self._hook(ENABLE_HOOK, {"session_id": session or self.session,
                                                "hook_event_name": "UserPromptSubmit", "prompt": line, "cwd": "/tmp"})
        assert (rc, err) == (0, ""), (rc, err)
        assert out is not None and "decision" not in out, out
        context = out["hookSpecificOutput"]["additionalContext"]
        assert "is enabled for every /direct verb" in context, context
        return context

    def end_session(self, reason: str = "clear", session: Optional[str] = None) -> None:
        rc, out, err = self._hook(REVOKE_HOOK, {"session_id": session or self.session,
                                                "hook_event_name": "SessionEnd", "reason": reason, "cwd": "/tmp"})
        assert (rc, out, err) == (0, None, ""), (rc, out, err)

    def gate_out(self, command: str, session: Optional[str] = None) -> Optional[dict]:
        rc, out, err = self._hook(GATE_HOOK, {
            "session_id": session or self.session, "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": command}, "cwd": "/tmp"})
        assert (rc, err) == (0, ""), (rc, err)
        return out

    def gate(self, command: str, session: Optional[str] = None) -> Optional[str]:
        """The PreToolUse hook on a Bash call: the denial reason, or None when it allows the call."""
        out = self.gate_out(command, session)
        assert out is not None, "the gate gave no opinion on a relay client call"
        decision = out["hookSpecificOutput"]
        if decision["permissionDecision"] == "allow":
            return None
        assert decision["permissionDecision"] == "deny", out
        return decision["permissionDecisionReason"]

    def run_ungated(self, command: str) -> tuple[int, Optional[dict], Optional[dict]]:
        """The Bash tool running ``command`` without asking the gate: exit code, stdout JSON, stderr JSON."""
        done = subprocess.run(["bash", "-c", command], capture_output=True, text=True, env=self.env, timeout=60)
        out = json.loads(done.stdout) if done.stdout.strip() else None
        err = json.loads(done.stderr) if done.stderr.strip() else None
        return done.returncode, out, err

    def run(self, command: str) -> tuple[int, Optional[dict], Optional[dict]]:
        """What Claude Code does: the gate first (it must allow), then the Bash call."""
        reason = self.gate(command)
        assert reason is None, reason
        return self.run_ungated(command)

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

    def sessions_cmd(self, state: Optional[str] = None) -> str:
        cmd = f"{call_prefix()} sessions --session {self.session}"
        return cmd + (f" --state {q(state)}" if state is not None else "")

    def write_transcript(self, session: str, age_s: float) -> None:
        path = self.claude_home / "projects" / "-tmp-proj" / f"{session}.jsonl"
        path.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "old work"},
                                    "timestamp": "2026-11-03T10:00:00.000Z", "cwd": "/tmp/proj"}) + "\n")
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))

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
# enabling once
# --------------------------------------------------------------------------

AWKWARD_BODY = "it's $(echo hi) \"q\" ; `x` | a && b"


def test_nothing_runs_before_the_owner_types_direct(stack):
    target = stack.address(stack.register_channel())
    for command in (stack.send_cmd(target, "hi"), stack.questions_cmd(), stack.replies_cmd(address=target)):
        assert "/direct is not enabled in this session" in stack.refused_by_gate(command)
        rc, out, err = stack.run_ungated(command)
        assert (rc, out, error_code(err)) == (1, None, "grant_required"), command
    assert stack.server.mailbox.messages_to(target) == []


def test_one_typed_direct_enables_every_verb_on_every_target(stack):
    # one registration per pid (a second register from this test process would end the first)
    first = stack.address(stack.register_channel())
    ghost = stack.address(str(uuid.uuid4()))
    stack.type_direct("/direct")
    for n in range(3):
        rc, out, err = stack.run(stack.send_cmd(first, f"status {n}?", kind="question"))
        assert (rc, err) == (0, None), err
        assert out["message"]["to"] == first
    rc, out, err = stack.run(stack.send_cmd(ghost, "anyone?"))  # another target, same enablement
    assert (rc, error_code(err)) == (1, "target_not_found")
    rc, out, err = stack.run(stack.replies_cmd(address=first))
    assert (rc, err) == (0, None) and len(out["messages"]) == 3
    rc, out, err = stack.run(stack.questions_cmd(state="all"))
    assert (rc, err) == (0, None) and len(out["questions"]) == 2


def test_a_send_is_delivered_verbatim_and_shown_to_the_owner(stack):
    target = stack.register_channel()
    to = stack.address(target)
    command = stack.send_cmd(to, AWKWARD_BODY, kind="question")
    stack.type_direct(f"/direct send {to} --kind question {AWKWARD_BODY}")
    shown = stack.gate_out(command)
    assert shown["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert shown["systemMessage"] == f"/direct send to {to} (question):\n{AWKWARD_BODY}"
    rc, out, err = stack.run_ungated(command)
    assert (rc, err) == (0, None), (rc, err)
    message = out["message"]
    assert (message["to"], message["kind"], message["body"]) == (to, "question", AWKWARD_BODY)
    grants = [r for r in stack.server.mailbox.audit_rows() if r["verb"] == "grant_mint" and r["outcome"] == "ok"]
    assert len(grants) == 1


def test_each_gated_send_spends_its_own_grant(stack):
    to = stack.address(stack.register_channel())
    command = stack.send_cmd(to, "once")
    stack.type_direct()
    assert stack.run(command)[0] == 0
    # The gate's grant was spent by that call: the same command without the gate is refused by the relay.
    rc, out, err = stack.run_ungated(command)
    assert (rc, out, error_code(err)) == (1, None, "grant_required")
    assert stack.run(command)[0] == 0  # the gate grants the next one
    assert len(stack.server.mailbox.messages_to(to)) == 2


def test_session_end_turns_it_off(stack):
    to = stack.address(stack.register_channel())
    stack.type_direct()
    assert stack.run(stack.send_cmd(to, "before"))[0] == 0
    stack.end_session("clear")
    assert "/direct is not enabled in this session" in stack.refused_by_gate(stack.send_cmd(to, "after"))
    rc, _, err = stack.run_ungated(stack.send_cmd(to, "after"))
    assert (rc, error_code(err)) == (1, "grant_required")
    stack.type_direct()
    assert stack.run(stack.send_cmd(to, "again"))[0] == 0
    assert [m["body"] for m in stack.server.mailbox.messages_to(to)] == ["before", "again"]


def test_another_sessions_enablement_does_not_cover_this_one(stack):
    to = stack.address(stack.register_channel())
    other = str(uuid.uuid4())
    stack.type_direct(session=other)
    assert "/direct is not enabled in this session" in stack.refused_by_gate(stack.send_cmd(to, "x"))
    assert "--session must be this session's id" in stack.refused_by_gate(stack.send_cmd(to, "x", session=other))


# --------------------------------------------------------------------------
# send
# --------------------------------------------------------------------------

def test_send_to_an_unknown_session_passes_the_gate_and_the_relay_reports_it(stack):
    ghost = stack.address(str(uuid.uuid4()))
    stack.type_direct(f"/direct send {ghost} hello")
    rc, out, err = stack.run(stack.send_cmd(ghost, "hello"))
    assert rc == 1 and out is None
    assert error_code(err) == "target_not_found"


def test_send_note_is_the_default_kind_the_skill_names(stack):
    target = stack.register_channel()
    to = stack.address(target)
    stack.type_direct(f"/direct send {to} plain words")
    rc, out, err = stack.run(stack.send_cmd(to, "plain words"))
    assert (rc, err) == (0, None) and out["message"]["kind"] == "note"


def test_send_to_a_selector_is_granted_for_the_address_it_resolves_to(stack):
    session = str(uuid.uuid4())
    stack.channel(session, "register", {
        "session_id": session, "harness": "claude", "pid": os.getpid(), "cwd": "/Users/u/Dev/app",
        "version": "2.1.316", "accepts": ["note"], "channel_nonce": NONCE, "task": "plugins-kit/harness-messaging"})
    selector = "task:plugins-kit/harness-messaging"
    stack.type_direct()
    rc, out, err = stack.run(stack.send_cmd(selector, "hello there"))
    assert (rc, err) == (0, None), err
    assert out["message"]["to"] == stack.address(session)
    minted = [r for r in stack.server.mailbox.audit_rows() if r["verb"] == "grant_mint" and r["outcome"] == "ok"]
    assert minted[-1]["origin"] == f"direct:{stack.address(stack.session)}"


def test_an_unresolvable_selector_is_refused_by_the_gate_with_the_relays_code(stack):
    stack.type_direct()
    assert "target_not_found" in stack.refused_by_gate(stack.send_cmd("task:no/such-task", "x"))


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------

def test_sessions_lists_running_sessions_by_default_and_counts_the_rest(stack):
    live = stack.register_channel()
    idle, ended = str(uuid.uuid4()), str(uuid.uuid4())
    stack.write_transcript(idle, 3600)
    stack.write_transcript(ended, 8 * 86400)
    stack.type_direct("/direct sessions")
    rc, out, err = stack.run(stack.sessions_cmd())
    assert (rc, err) == (0, None), err
    assert [s["session_id"] for s in out["sessions"]] == [live]
    assert out["omitted"] == {"idle": 1, "ended": 1}
    rc, out, err = stack.run(stack.sessions_cmd(state="all"))
    assert (rc, err) == (0, None), err
    assert {s["session_id"] for s in out["sessions"]} == {live, idle, ended} and "omitted" not in out


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
    stack.type_direct()
    rc, out, err = stack.run(stack.send_cmd(to, "ready?", kind="question"))
    assert (rc, err) == (0, None), err
    message_id = out["message"]["id"]
    assert [m["id"] for m in stack.deliver(session, registration)] == [message_id]
    creds = {"registration_id": registration["registration_id"], "session_id": session, "channel_nonce": NONCE}
    stack.channel(session, "reply", dict(creds, in_reply_to=message_id, state="answered", body="ready"))
    return to, message_id, session


def test_replies_by_message_id_and_by_address_and_the_long_poll(stack):
    to, message_id, _ = _sent_and_answered(stack)
    for command in (stack.replies_cmd(message_id=message_id), stack.replies_cmd(message_id=message_id, wait=1),
                    stack.replies_cmd(address=to)):
        rc, out, err = stack.run(command)
        assert (rc, err) == (0, None), err
        assert [(m["state"], m["reply"]["body"]) for m in out["messages"]] == [("answered", "ready")]


# --------------------------------------------------------------------------
# questions
# --------------------------------------------------------------------------

def test_questions_lists_the_open_interrupts_of_the_watched_ledger(stack):
    stack.type_direct("/direct questions")
    rc, out, err = stack.run(stack.questions_cmd())
    assert (rc, err) == (0, None), err
    assert [(x["source"]["run_id"], x["state"], x["kind"]) for x in out["questions"]] == [
        ("run-a", "open", "approval"), ("run-b", "open", "approval")]
    assert all(x["id"].startswith(f"q:{LABEL}/jobs-main:run-") for x in out["questions"])
    assert out["ledgers"] == [{"ledger": "jobs-main", "status": "ok"}]


def test_questions_for_another_machine_reports_it_unreachable_not_empty(stack):
    stack.type_direct()
    rc, out, err = stack.run(stack.questions_cmd(machine="elsewhere"))
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


def test_answer_resolves_one_interrupt_and_shows_the_owner_what_was_sent(stack):
    qid_a, qid_b = _question_ids(stack)
    command = stack.answer_cmd(qid_a, "answer", '{"approved": true}')
    shown = stack.gate_out(command)
    assert shown["systemMessage"] == f'/direct answer {qid_a}: answer\n{{"approved": true}}'
    rc, out, err = stack.run_ungated(command)
    assert (rc, err) == (0, None), err
    assert [(r[1], json.loads(r[2])) for r in stack.resolutions()] == [("answered", {"approved": True})]
    assert (out["id"], out["state"]) == (qid_a, "answered")
    # The grant the gate minted named qid_a only, and it is spent: ungated, qid_b is refused by the relay.
    rc, _, err = stack.run_ungated(stack.answer_cmd(qid_b, "answer", '{"approved": true}'))
    assert (rc, error_code(err)) == (1, "grant_required")
    assert len(stack.resolutions()) == 1


def test_answer_reject_records_the_reason(stack):
    qid_a, _ = _question_ids(stack)
    rc, _, err = stack.run(stack.answer_cmd(qid_a, "reject", reason="it's not time"))
    assert (rc, err) == (0, None), err
    assert [(r[1], r[3]) for r in stack.resolutions()] == [("rejected", "it's not time")]


def test_a_refused_answer_writes_nothing_and_the_right_answer_then_lands(stack):
    qid_a, _ = _question_ids(stack)
    rc, out, err = stack.run(stack.answer_cmd(qid_a, "answer", '{"approved": false}'))
    assert (rc, out, error_code(err)) == (1, None, "answer_invalid")
    for command in (stack.answer_cmd(qid_a, "answer"), stack.answer_cmd(qid_a, "reject", '{"approved": true}')):
        rc, out, err = stack.run(command)
        assert (rc, out, error_code(err)) == (1, None, "bad_request")
    assert stack.resolutions() == []
    rc, _, err = stack.run(stack.answer_cmd(qid_a, "answer", '{"approved": true}'))
    assert (rc, err) == (0, None), err
    assert [r[1] for r in stack.resolutions()] == ["answered"]


def test_answer_records_the_resolution_and_leaves_the_job_waiting(stack):
    qid_a, _ = _question_ids(stack)
    rc, out, err = stack.run(stack.answer_cmd(qid_a, "answer", '{"approved": true}'))
    assert (rc, err) == (0, None), err
    assert (out["decision"], out["state"], out["job_state"], out["replayed"]) == ("answer", "answered", "waiting", False)


def test_answered_question_leaves_the_open_list_and_shows_in_all(stack):
    qid_a, qid_b = _question_ids(stack)
    rc, _, err = stack.run(stack.answer_cmd(qid_a, "answer", '{"approved": true}'))
    assert (rc, err) == (0, None), err
    rc, out, err = stack.run(stack.questions_cmd(state="all"))
    assert (rc, err) == (0, None)
    assert {x["id"]: x["state"] for x in out["questions"]} == {qid_a: "answered", qid_b: "open"}
