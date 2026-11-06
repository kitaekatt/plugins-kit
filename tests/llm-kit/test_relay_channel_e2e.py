"""Messaging end to end (unit M9): real relay, real channel server, scripted Claude Code.

Nothing here is a double on the messaging path. The relay is the real
``llm_kit.relay.server.RelayServer`` (uvicorn on loopback, a tmp state
directory, tmp harness homes); the sender is the real ``HttpRelayClient`` acting
as a ``/direct`` session (grants minted and used); the channel is the real
``plugins/llm-kit/channel/relay_channel.py`` process. The one stand-in is Claude
Code: ``FAKE_CLAUDE_HOST`` is a small process that plays the claude process (it
is the channel's parent, owns the channel's stdio pipes and writes
``<claude home>/sessions/<pid>.json``), and the test drives the MCP side of it
by hand: initialize, notifications/initialized, receive
``notifications/claude/channel``, call the ``reply`` tool. Killing the host is
Claude Code exiting, which is what the session-ended checks need.

Nothing reads or writes the real ~/.claude or ~/.local/state.
"""
from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from llm_kit.completion.types import LLMResponse
from llm_kit.registry import consent
from llm_kit.relay import config as C
from llm_kit.relay import discovery as D
from llm_kit.relay import protocol as P
from llm_kit.relay import server as S
from llm_kit.relay.client import HttpRelayClient

SCRIPT = Path(__file__).resolve().parents[2] / "plugins" / "llm-kit" / "channel" / "relay_channel.py"
MACHINE = "m5pro"
ENV_JSON = {"machines": {"m5pro.local": {"os": "macos", "tailscale_hostname": "m5pro"}}}

FAKE_CLAUDE_HOST = r'''
import json, os, subprocess, sys, threading, time
import psutil

home, session, channel = sys.argv[1:4]
pid = os.getpid()
sessions = os.path.join(home, "sessions")
os.makedirs(sessions, exist_ok=True)
path = os.path.join(sessions, "%d.json" % pid)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as handle:
    json.dump({"pid": pid, "sessionId": session, "cwd": "/work/proj", "kind": "interactive",
               "startedAt": int(psutil.Process(pid).create_time() * 1000)}, handle)
os.replace(tmp, path)
child = subprocess.Popen([sys.executable, channel], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=sys.stderr, text=True)

def feed():
    for line in sys.stdin:
        child.stdin.write(line)
        child.stdin.flush()
    child.stdin.close()

threading.Thread(target=feed, daemon=True).start()
for line in child.stdout:
    sys.stdout.write(line)
    sys.stdout.flush()
sys.exit(child.wait())
'''


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Backend:
    name = "fake"

    def complete(self, system, user, *, model, options=None):  # never called by messaging
        return LLMResponse(text="x", model=model, finish_reason="stop", input_tokens=1, output_tokens=1)

    def classify_halt(self, exc):  # pragma: no cover
        return None


class Stack:
    """The relay (restartable on the same port and state) plus the sender's client."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.claude_home = tmp_path / "claude-home"
        (self.claude_home / "sessions").mkdir(parents=True)
        (self.claude_home / "projects" / "-tmp-proj").mkdir(parents=True)
        codex = tmp_path / "codex-home"
        (codex / "sessions").mkdir(parents=True)
        self.roots = D.DiscoveryRoots(self.claude_home, codex)
        self.xdg_state = tmp_path / "xdg-state"
        env_json = tmp_path / "env.json"
        env_json.write_text(json.dumps(ENV_JSON), encoding="utf-8")
        self.config = C.load_config(env_json=env_json, environ={}, hostname=lambda: "m5pro.local",
                                    config_path=tmp_path / "config" / "relay.yaml",
                                    state_root=self.xdg_state / "llm-kit" / "relay")
        self.port = _free_port()
        self.server = None
        self.client = None
        self.sender = str(uuid.uuid4())
        self.start_relay()

    def start_relay(self):
        self.server = S.RelayServer(
            self.config, port=self.port, roots=self.roots, probe=D.PsutilProbe(),
            backend_factory=lambda entry: SimpleNamespace(backend=_Backend(), model=entry.model),
            policy_loader=lambda scope: consent.policy(scope, path=self.tmp_path / "consent.json"),
            poll_interval_s=0.05, maintain_interval_s=0.2,
        ).start()
        if self.client is not None:
            self.client.close()
        self.client = HttpRelayClient(self.server.url, machine=MACHINE, relay_token=self.server.prepared.relay_token,
                                      hub_bearer=self.server.prepared.hub_bearer)

    def stop_relay(self):
        self.server.stop()

    @property
    def mailbox(self):
        return self.server.mailbox

    # -- the sender: a /direct session ----------------------------------------------

    @property
    def sender_origin(self):
        return "direct:%s/claude/%s" % (MACHINE, self.sender)

    def direct(self, verb, params, grant=None):
        return self.client.call(verb, params, origin=self.sender_origin, grant=grant)

    def mint(self, verb, targets=(), uses=1):
        return self.direct("grant_mint", {
            "session": "%s/claude/%s" % (MACHINE, self.sender), "verb": verb, "targets": list(targets),
            "expires_in_s": 120, "uses": uses, "origin_proof": {"type": "none"}, "command": "/direct " + verb,
        })["grant"]

    def send(self, to, body="run the tests", kind="instruction", **extra):
        self.mint("send_message", [to])
        return self.direct("send_message", dict({"to": to, "kind": kind, "body": body}, **extra))

    def replies(self, **params):
        target = params.get("message_id") or params["address"]
        self.mint("get_replies", [target])
        return self.direct("get_replies", params)["messages"]

    def operator(self, verb, params):
        return self.client.call(verb, params, origin="operator:" + MACHINE)

    def registrations(self):
        return self.operator("status", {})["registrations"]

    def wait_for(self, predicate, what, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        raise AssertionError("timed out waiting for " + what)

    def refused(self, code, call, *args, **kwargs):
        with pytest.raises(P.RelayError) as caught:
            call(*args, **kwargs)
        assert caught.value.code == code, caught.value
        return caught.value


class Peer:
    """Scripted Claude Code: one fake claude process holding one channel server."""

    def __init__(self, stack, session=None, accepts=None):
        self.stack = stack
        self.session = session or str(uuid.uuid4())
        self.address = "%s/claude/%s" % (MACHINE, self.session)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("LLM_KIT_", "XDG_", "CLAUDE"))}
        env.update({
            "LLM_KIT_RELAY_URL": stack.server.url,
            "LLM_KIT_RELAY_MACHINE": MACHINE,
            "LLM_KIT_CHANNEL_BACKOFF": "0.1,0.4",
            "LLM_KIT_CHANNEL_SESSION_POLL_S": "0.1",
            "XDG_STATE_HOME": str(stack.xdg_state),
            "CLAUDE_CONFIG_DIR": str(stack.claude_home),
        })
        if accepts is not None:
            env["LLM_KIT_RELAY_ACCEPTS"] = accepts
        self.host = subprocess.Popen(
            [sys.executable, "-c", FAKE_CLAUDE_HOST, str(stack.claude_home), self.session, str(SCRIPT)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        self.lines = queue.Queue()
        self.stderr_lines = []
        threading.Thread(target=self._pump, args=(self.host.stdout, lambda l: self.lines.put(json.loads(l))),
                         daemon=True).start()
        threading.Thread(target=self._pump, args=(self.host.stderr, self.stderr_lines.append), daemon=True).start()
        self._next = 0

    @staticmethod
    def _pump(stream, sink):
        for line in stream:
            sink(line.rstrip("\n"))

    def send(self, obj):
        self.host.stdin.write(json.dumps(obj) + "\n")
        self.host.stdin.flush()

    def request(self, method, params=None, timeout=10.0):
        self._next += 1
        rid = self._next
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        while True:
            message = self.next_message(timeout)
            if message.get("id") == rid:
                return message

    def next_message(self, timeout=10.0):
        try:
            return self.lines.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError("no message from the channel within %.0fs; stderr: %s" % (timeout, self.stderr_lines))

    def no_message(self, wait):
        try:
            extra = self.lines.get(timeout=wait)
        except queue.Empty:
            return
        raise AssertionError("unexpected message from the channel: %r" % (extra,))

    def handshake(self):
        """What Claude Code does on start; returns once the channel is registered with the relay."""
        discover = self.request("server/discover")
        assert discover["error"]["code"] == -32601
        init = self.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                           "clientInfo": {"name": "claude-code", "version": "9.9.9"}})
        assert init["result"]["capabilities"]["experimental"] == {"claude/channel": {}}
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.stack.wait_for(lambda: any(self.address in line for line in self.stderr_lines if "registered as" in line),
                            "the channel to register")
        return self

    def channel_message(self, timeout=10.0):
        message = self.next_message(timeout)
        assert message.get("method") == "notifications/claude/channel", message
        return message["params"]

    def reply(self, message_id, body, state="answered"):
        response = self.request("tools/call", {"name": "reply", "arguments": {
            "in_reply_to": message_id, "body": body, "state": state}})
        return response["result"]

    def set_session(self, session):
        path = self.stack.claude_home / "sessions" / ("%d.json" % self.host.pid)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["sessionId"] = session
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)

    def channel_pid(self):
        children = psutil.Process(self.host.pid).children()
        assert len(children) == 1, children
        return children[0].pid

    def close(self):
        if self.host.poll() is None:
            try:
                self.host.stdin.close()
            except OSError:
                pass
            try:
                self.host.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.host.kill()
                self.host.wait()


@pytest.fixture
def stack(tmp_path):
    made = Stack(tmp_path)
    yield made
    try:
        made.stop_relay()
    except Exception:  # a test that stopped the relay itself
        pass
    if made.client is not None:
        made.client.close()


@pytest.fixture
def peer(stack):
    made = []

    def make(**kwargs):
        p = Peer(stack, **kwargs)
        made.append(p)
        return p.handshake()

    yield make
    for p in made:
        p.close()


# --------------------------------------------------------------------------
# send_message -> channel -> reply -> get_replies
# --------------------------------------------------------------------------

def test_a_sent_message_reaches_the_session_and_its_reply_comes_back(stack, peer):
    session = peer()
    sent = stack.send(session.address, body="Are the tests green?", kind="question")
    message = sent["message"]
    assert message["state"] == "queued" and message["sender"] == stack.sender_origin

    pushed = session.channel_message()
    assert pushed["content"] == "Are the tests green?"
    assert pushed["meta"] == {"message_id": message["id"], "sender": stack.sender_origin, "kind": "question", "hop": "0"}
    stack.wait_for(lambda: stack.mailbox.get_message(message["id"])["state"] == "acked", "the channel's ack")

    waiter = {}

    def long_poll():
        started = time.monotonic()
        waiter["messages"] = stack.replies(message_id=message["id"], wait_s=15)
        waiter["after"] = time.monotonic() - started

    thread = threading.Thread(target=long_poll)
    thread.start()
    time.sleep(0.5)
    result = session.reply(message["id"], "Yes, all green.")
    assert result["isError"] is False and "reply recorded (answered)" in result["content"][0]["text"]
    thread.join(timeout=10)
    assert not thread.is_alive() and waiter["after"] < 10, "get_replies did not wake on the reply"
    record = waiter["messages"][0]
    assert record["state"] == "answered" and record["reply"]["body"] == "Yes, all green."
    assert record["reply"]["sender"] == "channel:" + session.address

    # the same answer through the by-address form
    by_address = stack.replies(address=session.address)
    assert [m["id"] for m in by_address] == [message["id"]]

    # audit identities: the sender is direct:m5pro/claude/<id>, the session is channel:m5pro/claude/<id>
    rows = stack.mailbox.audit_rows(limit=1000)
    sends = [r for r in rows if r["event"] == "call" and r["verb"] == "send_message"]
    assert [r["origin"] for r in sends] == [stack.sender_origin] and sends[0]["outcome"] == "ok"
    channel_origin = "channel:" + session.address
    assert {r["verb"] for r in rows if r["origin"] == channel_origin} >= {"register", "poll", "ack", "reply"}
    moves = [(r["from_state"], r["to_state"], r["verb"], r["origin"]) for r in rows
             if r["event"] == "transition" and r["message_id"] == message["id"]]
    assert moves == [
        (None, "queued", "send_message", stack.sender_origin),
        ("queued", "delivered", "poll", channel_origin),
        ("delivered", "acked", "ack", channel_origin),
        ("acked", "answered", "reply", channel_origin),
    ]


def test_a_resent_message_id_is_pushed_once_and_a_decline_comes_back_declined(stack, peer):
    session = peer()
    first = stack.send(session.address, body="deploy it", id="msg_resend_once")
    again = stack.send(session.address, body="deploy it", id="msg_resend_once")
    assert first["replayed"] is False and again["replayed"] is True
    assert session.channel_message()["meta"]["message_id"] == "msg_resend_once"
    session.no_message(1.5)  # not pushed a second time

    result = session.reply("msg_resend_once", "will not deploy without the owner", state="declined")
    assert result["isError"] is False
    record = stack.replies(message_id="msg_resend_once")[0]
    assert record["state"] == "declined" and record["reply"]["body"] == "will not deploy without the owner"
    # a second reply to the same message is a tool error, not a second record
    second = session.reply("msg_resend_once", "changed my mind")
    assert second["isError"] is True and "invalid_transition" in second["content"][0]["text"]


def test_a_session_that_did_not_opt_in_is_refused_with_a_reason_and_nothing_is_pushed(stack, peer):
    read_only = peer(accepts="")
    err = stack.refused("not_opted_in", stack.send, read_only.address, kind="note", body="hello?")
    assert stack.mailbox.get_message(err.details["message_id"])["state"] == "refused"
    notes_only = peer(accepts="note")
    stack.refused("not_opted_in", stack.send, notes_only.address, kind="instruction", body="do it")
    stack.send(notes_only.address, kind="note", body="fyi")
    assert notes_only.channel_message()["content"] == "fyi"
    read_only.no_message(1.0)
    notes_only.no_message(0.5)


# --------------------------------------------------------------------------
# pause, restart, expiry
# --------------------------------------------------------------------------

def test_pause_stops_delivery_survives_a_restart_and_queued_messages_arrive_after_resume(stack, peer):
    session = peer()
    stack.send(session.address, kind="note", body="before the pause")
    assert session.channel_message()["content"] == "before the pause"

    paused_at = time.monotonic()
    stack.operator("pause", {"scope": "relay", "reason": "owner away"})
    err = stack.refused("paused", stack.send, session.address, kind="note", body="while paused")
    assert err.retryable is True and err.details["scope"] == "relay"
    # delivery stops within one heartbeat (30 s): the channel sees `paused` from its next poll
    stack.wait_for(lambda: any("relay is paused" in line for line in session.stderr_lines), "the channel to see the pause",
                   timeout=10.0)
    assert time.monotonic() - paused_at < P.HEARTBEAT_INTERVAL_S
    seen_seq = stack.mailbox.audit_rows(limit=100000)[-1]["seq"]

    # messages queued before the pause (written to the mailbox as an earlier send would have been)
    origin = P.parse_identity(stack.sender_origin)
    keep = stack.mailbox.send_message(origin=origin, to=session.address, kind="instruction", body="queued earlier")["message"]
    lapse = stack.mailbox.send_message(origin=origin, to=session.address, kind="note", body="stale soon",
                                       expires_in_s=1)["message"]
    session.no_message(2.5)  # nothing is delivered while paused
    assert stack.mailbox.get_message(keep["id"])["state"] == "queued"
    # a paused relay is polled slowly: every poll is an audit row that is never deleted
    polls = [r for r in stack.mailbox.audit_rows(since_seq=seen_seq, limit=100000)
             if r["verb"] == "poll" and r["origin"] == "channel:" + session.address]
    assert len(polls) <= 1, "the channel polled a paused relay %d times in 2.5 s" % len(polls)
    # expiry keeps running during a pause
    stack.wait_for(lambda: stack.mailbox.get_message(lapse["id"])["state"] == "lapsed", "the short-lived message to lapse")

    # the flag survives a relay restart; the channel registers again; still nothing is delivered
    stack.stop_relay()
    stack.start_relay()
    status = stack.operator("status", {})
    assert status["pause"]["relay"]["paused"] is True and status["pause"]["relay"]["reason"] == "owner away"
    stack.wait_for(lambda: stack.registrations() == 1, "the channel to register with the restarted relay", timeout=15.0)
    session.no_message(2.0)
    stack.refused("paused", stack.send, session.address, kind="note", body="still paused")

    stack.operator("resume", {"scope": "relay"})
    pushed = session.channel_message(timeout=15.0)
    assert pushed["content"] == "queued earlier" and pushed["meta"]["message_id"] == keep["id"]
    session.no_message(1.0)  # the lapsed message is never delivered
    session.reply(keep["id"], "done")
    assert stack.replies(message_id=keep["id"])[0]["state"] == "answered"
    assert stack.replies(message_id=lapse["id"])[0]["state"] == "lapsed"


# --------------------------------------------------------------------------
# session ended
# --------------------------------------------------------------------------

def test_a_session_that_exits_ends_its_open_messages_and_refuses_new_ones(stack, peer):
    session = peer()
    sent = stack.send(session.address, body="long job", kind="instruction")["message"]
    assert session.channel_message()["meta"]["message_id"] == sent["id"]
    stack.wait_for(lambda: stack.mailbox.get_message(sent["id"])["state"] == "acked", "the ack")
    channel_pid = session.channel_pid()

    session.host.kill()  # Claude Code exits; the channel sees stdin close
    session.host.wait(timeout=10)
    record = stack.wait_for(lambda: (r := stack.replies(message_id=sent["id"])[0])["state"] == "session-ended" and r,
                            "the open message to end", timeout=15.0)
    assert record["state_reason"] == "deregistered"
    stack.wait_for(lambda: not psutil.pid_exists(channel_pid), "the channel process to exit", timeout=15.0)
    assert stack.registrations() == 0
    stack.refused("session_ended", stack.send, session.address, kind="note", body="anyone there?")


def test_a_session_id_change_ends_messages_to_the_old_address_and_the_new_one_takes_replies(stack, peer):
    session = peer()
    old_address = session.address
    sent = stack.send(old_address, body="before /clear", kind="instruction")["message"]
    assert session.channel_message()["meta"]["message_id"] == sent["id"]
    stack.wait_for(lambda: stack.mailbox.get_message(sent["id"])["state"] == "acked", "the ack")

    new_session = str(uuid.uuid4())
    session.set_session(new_session)  # /clear: a new session id in the same process
    new_address = "%s/claude/%s" % (MACHINE, new_session)
    record = stack.wait_for(lambda: (r := stack.mailbox.get_message(sent["id"]))["state"] == "session-ended" and r,
                            "the old message to end", timeout=10.0)
    assert record["state_reason"] == "session_changed"
    stack.refused("session_ended", stack.send, old_address, kind="note", body="to the old conversation")

    # a late reply to the old conversation's message is refused, never filed under the new session
    late = session.reply(sent["id"], "too late")
    assert late["isError"] is True
    assert stack.mailbox.get_message(sent["id"])["state"] == "session-ended"

    # the move was a heartbeat carrying previous_session_id, not a second registration
    rows = stack.mailbox.audit_rows(limit=1000)
    assert [r["origin"] for r in rows if r["verb"] == "register"] == ["channel:" + old_address]
    renamed = [r for r in rows if r["verb"] == "heartbeat" and r["origin"] == "channel:" + new_address]
    assert renamed and renamed[0]["outcome"] == "ok"

    fresh = stack.send(new_address, body="after /clear", kind="question")["message"]
    assert session.channel_message()["meta"]["message_id"] == fresh["id"]
    assert session.reply(fresh["id"], "hello again")["isError"] is False
    assert stack.replies(message_id=fresh["id"])[0]["state"] == "answered"
