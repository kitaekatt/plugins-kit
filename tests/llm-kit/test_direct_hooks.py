"""The /direct hooks (alternative A) against a fake relay: enable once, gate, revoke.

Owner ruling 2026-11-11: typing /direct once enables every /direct verb for the rest of the session, /clear
turns it off, a send shows no permission prompt, and a sent message is visible in the session. Real hook
scripts run as subprocesses, fed hook-shaped JSON on stdin, against a stdlib HTTP server that speaks enough
relay-protocol/1 to mint grants (token check, a /direct session mints only for itself).

What the hooks are: a GUARDRAIL, not a security boundary (relay alternative A). The last tests pin the known
bypasses as documented limitations so nobody reads the gate as stronger than it is.

Each guard test names the edit that turns it red (each shown red when this file was written):

* typed-only enabling -- in userpromptsubmit/direct_enable.typed_direct change ``_PREFIX_RE.match(text)`` to
  ``_PREFIX_RE.search(text)`` and delete the ``<channel `` check: test_channel_wrapped_direct_does_not_enable
  and test_non_direct_prompt_does_nothing go red.
* the gate refuses a session that is not enabled -- in pretooluse/direct_gate.run change
  ``if record is None:`` to ``if False:``: test_gate_denies_a_session_that_is_not_enabled goes red.
* the gate allows an enabled call -- in direct_gate.run return ``None`` instead of the final ``_allow(...)``:
  test_gate_allows_an_enabled_call_and_mints_its_one_use_grant goes red.
* plain form only -- in direct_gate.plain_words drop the final ``else: return None`` branch (accept any
  character): test_gate_refuses_every_form_it_cannot_read_exactly goes red.
* own launcher only -- in direct_gate.plain_client_args drop the ``realpath`` comparison:
  test_gate_refuses_an_imposter_launcher goes red.
* visible sends -- in direct_gate.shown_to_owner return ``None`` for ``send``:
  test_a_send_shows_the_owner_its_exact_target_and_text goes red.
* /clear and session end revoke -- in sessionend/direct_revoke.run return ``False`` without calling
  ``R.revoke``: test_session_end_revokes_only_that_session goes red.
* off by default, enable hook -- in userpromptsubmit/direct_enable.run change ``if not opted_in:`` to
  ``if False:``: test_a_typed_direct_enables_nothing_until_the_owner_turns_the_relay_on goes red.
* off by default, gate -- in pretooluse/direct_gate.run change ``if not opted_in:`` to ``if False:``:
  test_the_gate_refuses_every_call_while_the_relay_is_off goes red.
* a malformed opt-in is not read as on -- in direct_relay.relay_opt_in replace the final ``raise HookError``
  of the ``enabled`` branch with ``value = True``: test_an_unreadable_opt_in_enables_nothing_and_the_gate_refuses
  goes red.
* continue (remote resume, relay protocol amendment 3) -- delete ``"continue"`` from direct_relay.CLIENT_VERBS:
  test_an_enabled_continue_is_allowed_shown_and_granted_for_its_full_address goes red; in direct_gate.shown_to_owner
  return ``None`` for ``continue``: the same test goes red; in direct_gate.run delete the ``continue_target_problem``
  check: test_a_continue_must_name_a_full_address_on_this_machine goes red.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "llm-kit"
HOOKS = PLUGIN / "hooks"
ENABLE_HOOK = HOOKS / "userpromptsubmit" / "direct_enable.py"
GATE_HOOK = HOOKS / "pretooluse" / "direct_gate.py"
REVOKE_HOOK = HOOKS / "sessionend" / "direct_revoke.py"
WRAPPER = HOOKS / "run-direct-hook.sh"
LAUNCHER = PLUGIN / "bin" / "llm-kit"

MACHINE = "testbox"
SESSION = "b91e0c44-2f8a-4d3b-8c6e-71a5d0e3f2a9"
OTHER = "7f3c2a10-5b7e-4c1d-9a8e-0d6f4b2c9e11"
TOKEN = "tok-" + "a" * 40
TARGET = f"{MACHINE}/claude/{OTHER}"
IDLE = f"{MACHINE}/codex/019a6f2e-7c41-7d20-9b3e-5f8a1c2d4e60"


# -- fake relay -------------------------------------------------------------------------------

class FakeRelay:
    """Enough of relay-protocol/1 to mint grants, per protocol section 14."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.mint_error: Optional[tuple[str, str]] = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                envelope = json.loads(self.rfile.read(length))
                reply = outer.handle(self.path, envelope, self.headers.get("X-Relay-Token"))
                body = json.dumps(reply).encode()
                self.send_response(200 if reply["ok"] else 403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @staticmethod
    def _error(request_id: str, code: str, message: str) -> dict[str, Any]:
        return {"protocol": "relay-protocol/1", "id": request_id, "ok": False,
                "error": {"code": code, "message": message, "retryable": False}}

    def handle(self, path: str, envelope: dict[str, Any], token: Optional[str]) -> dict[str, Any]:
        rid = envelope.get("id")
        verb = path.rsplit("/", 1)[-1]
        self.requests.append({"verb": verb, "origin": envelope.get("origin"), "params": envelope.get("params"),
                              "token": token})
        if token != TOKEN:
            return self._error(rid, "unauthenticated", "bad token")
        params = envelope["params"]
        if verb != "grant_mint":
            return self._error(rid, "unknown_verb", verb)
        if envelope["origin"] != f"direct:{params['session']}":
            return self._error(rid, "origin_not_allowed", "a /direct session acts only for itself")
        if self.mint_error:
            return self._error(rid, *self.mint_error)
        grant = {"grant_id": f"grt_{len(self.requests)}", "session": params["session"], "verb": params["verb"],
                 "targets": list(params["targets"]), "uses": params["uses"], "uses_remaining": params["uses"],
                 "state": "live", "origin_proof": params["origin_proof"]}
        return {"protocol": "relay-protocol/1", "id": rid, "ok": True, "result": {"grant": grant}}

    def verbs(self) -> list[str]:
        return [r["verb"] for r in self.requests]


@pytest.fixture()
def relay():
    fake = FakeRelay()
    yield fake
    fake.close()


@pytest.fixture()
def home(tmp_path, relay):
    """A scratch HOME with env.json, a relay token (0600) and relay.yaml turning the relay on at the fake relay."""
    return make_home(tmp_path, relay.port)


def make_home(tmp_path: Path, port: int, *, token: Optional[str] = TOKEN, opt_in: Optional[str] = "enabled: true\n",
              ) -> dict[str, str]:
    root = tmp_path / "home"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "env.json").write_text(json.dumps(
        {"machines": {socket.gethostname(): {"tailscale_hostname": MACHINE.upper()}}}))
    state = root / "state" / "llm-kit" / "relay"
    state.mkdir(parents=True)
    if token is not None:
        (state / "relay.token").write_text(token + "\n")
        (state / "relay.token").chmod(0o600)
    cfg = root / "cfg" / "llm-kit"
    cfg.mkdir(parents=True)
    (cfg / "relay.yaml").write_text(f"{opt_in or ''}relay:\n  host: 127.0.0.1\n  port: {port}\n")
    env = {k: v for k, v in os.environ.items()
           if k not in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "HOME", "CLAUDE_BOOTSTRAP_DATA_ROOT")}
    env.update({"HOME": str(root), "XDG_STATE_HOME": str(root / "state"), "XDG_CONFIG_HOME": str(root / "cfg"),
                "CLAUDE_BOOTSTRAP_DATA_ROOT": str(root / "bootstrap-data")})
    return env


def record_path(env: dict[str, str], session: str = SESSION) -> Path:
    """The plugin's user-scoped data directory, under the session's bootstrap data root."""
    data = Path(env["CLAUDE_BOOTSTRAP_DATA_ROOT"]) / "plugins-kit" / "llm-kit"
    return data / "direct" / "enabled" / f"{session}.json"


def run_hook(script: Path, payload: Any, env: dict[str, str]) -> tuple[int, Optional[dict], str]:
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    done = subprocess.run([sys.executable, str(script)], input=stdin, capture_output=True, text=True,
                          env=env, timeout=30)
    out = json.loads(done.stdout) if done.stdout.strip() else None
    return done.returncode, out, done.stderr


def prompt_payload(prompt: str, session: str = SESSION) -> dict[str, Any]:
    return {"session_id": session, "hook_event_name": "UserPromptSubmit", "prompt": prompt, "cwd": "/tmp"}


def bash_payload(command: str, session: str = SESSION, tool: str = "Bash") -> dict[str, Any]:
    return {"session_id": session, "hook_event_name": "PreToolUse", "tool_name": tool,
            "tool_input": {"command": command}, "cwd": "/tmp"}


def end_payload(session: str = SESSION, reason: str = "clear") -> dict[str, Any]:
    return {"session_id": session, "hook_event_name": "SessionEnd", "reason": reason, "cwd": "/tmp"}


def context(out: Optional[dict]) -> str:
    assert out is not None
    return out["hookSpecificOutput"]["additionalContext"]


def decision(out: Optional[dict]) -> tuple[str, str]:
    assert out is not None
    specific = out["hookSpecificOutput"]
    return specific["permissionDecision"], specific["permissionDecisionReason"]


def client(args: str, session: str = SESSION) -> str:
    return f"{LAUNCHER} relay client {args} --session {session}"


def enable(home: dict[str, str], line: str = "/direct", session: str = SESSION) -> None:
    rc, out, err = run_hook(ENABLE_HOOK, prompt_payload(line, session), home)
    assert (rc, err) == (0, "") and "is enabled" in context(out)


# -- UserPromptSubmit: enabling ------------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "/direct", "/direct help", f"/direct read {TARGET}", f"  \n/direct send {TARGET} hi", "/llm-kit:direct machines",
    "/direct reed x",
])
def test_a_typed_direct_enables_the_session_without_asking_the_relay(relay, home, line):
    rc, out, err = run_hook(ENABLE_HOOK, prompt_payload(line), home)
    assert (rc, err) == (0, "")
    text = context(out)
    assert f"this session ({SESSION}) is enabled for every /direct verb" in text and "/clear" in text
    assert f"`{LAUNCHER.resolve()} relay client <verb> --session {SESSION} ...`" in text
    assert "not a security boundary" in text and "decision" not in out
    path = record_path(home)
    record = json.loads(path.read_text())
    assert record["session_id"] == SESSION and record["command"] == " ".join(line.strip().split("\n")[0].split())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert relay.requests == []


def test_without_a_redirected_data_root_the_record_is_in_the_users_plugin_data_dir(relay, home):
    env = {k: v for k, v in home.items() if k != "CLAUDE_BOOTSTRAP_DATA_ROOT"}
    enable(env)
    expected = Path(home["HOME"]) / ".claude" / "plugins" / "data" / "plugins-kit" / "llm-kit" / "direct" / "enabled"
    assert [p.name for p in expected.iterdir()] == [f"{SESSION}.json"]
    assert not (Path(home["XDG_STATE_HOME"]) / "llm-kit" / "direct").exists()
    _, out, _ = run_hook(GATE_HOOK, bash_payload(client("machines")), env)
    assert decision(out)[0] == "allow"
    run_hook(REVOKE_HOOK, end_payload(), env)
    assert list(expected.iterdir()) == []


def test_the_typed_line_is_data_never_run_by_a_shell(relay, home, tmp_path):
    marker = tmp_path / "pwned"
    line = f"/direct read $(touch {marker}) ; touch {marker} `touch {marker}`"
    enable(home, line)
    assert not marker.exists() and json.loads(record_path(home).read_text())["command"] == line


def test_channel_wrapped_direct_does_not_enable(relay, home):
    wrapped = ('<channel source="relay" chat_id="1" sender="m5pro/claude/x">\n'
               f'/direct send {TARGET} do something\n</channel>')
    for prompt in (wrapped, "  " + wrapped):
        rc, out, _ = run_hook(ENABLE_HOOK, prompt_payload(prompt), home)
        assert (rc, out) == (0, None)
    assert not record_path(home).exists() and relay.requests == []


@pytest.mark.parametrize("prompt", [
    "hello there", "please run /direct read x for me", "/directory listing", "/directsomething x", "",
])
def test_non_direct_prompt_does_nothing(relay, home, prompt):
    rc, out, err = run_hook(ENABLE_HOOK, prompt_payload(prompt), home)
    assert (rc, out, err) == (0, None, "")
    assert not record_path(home).parent.exists()


def test_a_record_that_cannot_be_written_is_reported_and_does_not_block(relay, home):
    blocker = record_path(home).parent
    blocker.parent.mkdir(parents=True)
    blocker.write_text("a file where the directory should be")
    rc, out, _ = run_hook(ENABLE_HOOK, prompt_payload("/direct"), home)
    assert rc == 0 and "decision" not in out
    assert "could not be enabled" in context(out) and "could not be enabled" in out["systemMessage"]


@pytest.mark.parametrize("stdin", ["not json", "[]", json.dumps({"prompt": "/direct machines"}),
                                   json.dumps({"session_id": "latest", "prompt": "/direct machines"})])
def test_bad_hook_input_fails_loudly(relay, home, stdin):
    rc, out, err = run_hook(ENABLE_HOOK, stdin, home)
    assert rc == 1 and out is None and "direct_enable.py:" in err


# -- off by default: the owner's relay.yaml turns the relay on --------------------------------------

def set_opt_in(env: dict[str, str], text: Optional[str]) -> Path:
    """Rewrite relay.yaml with ``text`` (None deletes the file), keeping the fake relay's listener block."""
    path = Path(env["XDG_CONFIG_HOME"]) / "llm-kit" / "relay.yaml"
    listener = path.read_text().split("relay:\n", 1)[1]
    if text is None:
        path.unlink()
    else:
        path.write_text(f"{text}relay:\n{listener}")
    return path


OFF_STATES = {
    "no file": None,
    "no key": "",
    "enabled false": "enabled: false\n",
    "enabled off": "enabled: off  # not yet\n",
}


@pytest.mark.parametrize("state", sorted(OFF_STATES))
def test_a_typed_direct_enables_nothing_until_the_owner_turns_the_relay_on(relay, home, state):
    path = set_opt_in(home, OFF_STATES[state])
    rc, out, err = run_hook(ENABLE_HOOK, prompt_payload("/direct machines"), home)
    assert (rc, err) == (0, "")
    text = context(out)
    assert text.startswith("/direct is off on this machine") and "systemMessage" not in out
    assert f"put `enabled: true` in {path}" in text and "is enabled" not in text
    assert not record_path(home).exists() and relay.requests == []


@pytest.mark.parametrize("state", sorted(OFF_STATES))
def test_the_gate_refuses_every_call_while_the_relay_is_off(relay, home, state):
    enable(home)  # a record from when the relay was on does not outlive turning it off
    path = set_opt_in(home, OFF_STATES[state])
    for args in ("machines", f"read {TARGET}", f"send {TARGET} --kind note --body 'hi'"):
        rc, out, _ = run_hook(GATE_HOOK, bash_payload(client(args)), home)
        verdict, reason = decision(out)
        assert rc == 0 and verdict == "deny" and "/direct is off on this machine" in reason
        assert f"put `enabled: true` in {path}" in reason and "Do not change that file yourself" in reason
    assert relay.requests == []


@pytest.mark.parametrize("text", ['enabled: "true"\n', "enabled: maybe\n", "enabled:\n", "enabled: 1\n",
                                  "enable: true\n", "{enabled: true}\n", "---\nenabled: true\n",
                                  "enabled: true\nenabled: true\n"])
def test_an_unreadable_opt_in_enables_nothing_and_the_gate_refuses(relay, home, text):
    enable(home)
    record_path(home).unlink()
    set_opt_in(home, text)
    rc, out, err = run_hook(ENABLE_HOOK, prompt_payload("/direct"), home)
    assert (rc, err) == (0, "")
    assert "relay settings could not be read" in context(out) and "could not be read" in out["systemMessage"]
    assert not record_path(home).exists()
    verdict, reason = decision(run_hook(GATE_HOOK, bash_payload(client("machines")), home)[1])
    assert verdict == "deny" and "cannot read the relay settings" in reason
    assert relay.requests == []


def test_the_opt_in_is_read_from_the_default_config_path_without_xdg(relay, home):
    env = {k: v for k, v in home.items() if k != "XDG_CONFIG_HOME"}
    rc, out, _ = run_hook(ENABLE_HOOK, prompt_payload("/direct"), env)
    default = Path(home["HOME"]) / ".config" / "llm-kit" / "relay.yaml"
    assert rc == 0 and f"put `enabled: true` in {default}" in context(out)
    assert not record_path(env).exists()


# -- PreToolUse: the gate ---------------------------------------------------------------------------

def test_gate_denies_a_session_that_is_not_enabled(relay, home):
    rc, out, _ = run_hook(GATE_HOOK, bash_payload(client(f"read {TARGET}")), home)
    assert rc == 0
    verdict, reason = decision(out)
    assert verdict == "deny" and "/direct is not enabled in this session" in reason
    assert "Only the owner enables it, by typing /direct in this session" in reason
    assert relay.requests == []


def test_gate_allows_an_enabled_call_and_mints_its_one_use_grant(relay, home):
    enable(home, f"/direct read {TARGET}")
    rc, out, err = run_hook(GATE_HOOK, bash_payload(client(f"read {TARGET} --limit 5")), home)
    assert (rc, err) == (0, "")
    verdict, reason = decision(out)
    assert verdict == "allow" and reason.startswith(f"/direct read {TARGET}")
    assert "systemMessage" not in out
    (request,) = relay.requests
    assert request["token"] == TOKEN and request["origin"] == f"direct:{MACHINE}/claude/{SESSION}"
    assert request["params"] == {
        "session": f"{MACHINE}/claude/{SESSION}", "verb": "read_session", "targets": [TARGET],
        "expires_in_s": 120, "uses": 1, "origin_proof": {"type": "none"}, "command": f"/direct read {TARGET}",
    }


@pytest.mark.parametrize("args, verb, targets", [
    ("machines", "list_machines", []),
    ("sessions", "list_sessions", []),
    ("sessions --state all", "list_sessions", []),
    (f"sessions --machine {MACHINE}", "list_sessions", [MACHINE]),
    (f"read {TARGET} --since c:10", "read_session", [TARGET]),
    ("read latest", "read_session", ["latest"]),
    ("ask qwen38-m5-32k-local --prompt 'what is 2+2'", "ask_llm", [f"{MACHINE}/llm/qwen38-m5-32k-local"]),
    (f"send {TARGET} --kind note --body hi", "send_message", [TARGET]),
    ("replies --message-id msg_abc123 --wait 5", "get_replies", ["msg_abc123"]),
    (f"replies --address {TARGET}", "get_replies", [TARGET]),
    ("questions --state all", "list_questions", []),
    (f"answer q:{MACHINE}/jobs:run:7 --decision reject --reason 'not now'", "answer_question",
     [f"q:{MACHINE}/jobs:run:7"]),
    (f"continue {IDLE} --kind instruction --body hi", "continue_session", [IDLE]),
])
def test_one_enablement_covers_every_verb_each_with_its_own_grant(relay, home, args, verb, targets):
    enable(home)
    for _ in range(2):  # no use count: the same call is allowed again
        _, out, _ = run_hook(GATE_HOOK, bash_payload(client(args)), home)
        assert decision(out)[0] == "allow", args
    assert [(r["params"]["verb"], r["params"]["targets"], r["params"]["uses"]) for r in relay.requests] == [
        (verb, targets, 1)] * 2


def test_a_send_shows_the_owner_its_exact_target_and_text(relay, home):
    enable(home)
    body = "Are the relay tests green? It's $HOME and `this`; keep it."
    quoted = "'" + body.replace("'", "'\\''") + "'"
    _, out, _ = run_hook(GATE_HOOK, bash_payload(client(f"send {TARGET} --kind instruction --body {quoted}")), home)
    assert decision(out)[0] == "allow"
    assert out["systemMessage"] == f"/direct send to {TARGET} (instruction):\n{body}"


def test_an_answer_shows_the_owner_the_decision_and_input(relay, home):
    enable(home)
    qid = f"q:{MACHINE}/jobs:run:7"
    _, out, _ = run_hook(GATE_HOOK, bash_payload(client(f"answer {qid} --decision answer --input '{{\"ok\": true}}'")),
                         home)
    assert decision(out)[0] == "allow"
    assert out["systemMessage"] == f'/direct answer {qid}: answer\n{{"ok": true}}'


def test_an_enabled_continue_is_allowed_shown_and_granted_for_its_full_address(relay, home):
    enable(home)
    body = "Rerun the failing test; it's in `tests/` and $HOME is fine."
    quoted = "'" + body.replace("'", "'\\''") + "'"
    rc, out, err = run_hook(GATE_HOOK, bash_payload(client(f"continue {IDLE} --kind instruction --body {quoted}")),
                            home)
    assert (rc, err) == (0, "")
    verdict, reason = decision(out)
    assert verdict == "allow" and reason.startswith(f"/direct continue {IDLE}")
    assert out["systemMessage"] == f"/direct continue {IDLE} (instruction), one resumed turn:\n{body}"
    (request,) = relay.requests
    assert request["origin"] == f"direct:{MACHINE}/claude/{SESSION}"
    assert request["params"] == {
        "session": f"{MACHINE}/claude/{SESSION}", "verb": "continue_session", "targets": [IDLE],
        "expires_in_s": 120, "uses": 1, "origin_proof": {"type": "none"}, "command": "/direct",
    }


def test_a_continue_is_refused_in_a_session_that_is_not_enabled(relay, home):
    verdict, reason = decision(run_hook(GATE_HOOK, bash_payload(client(f"continue {IDLE} --kind note --body hi")),
                                        home)[1])
    assert verdict == "deny" and "/direct is not enabled in this session" in reason
    assert relay.requests == []


@pytest.mark.parametrize("args", [
    "continue latest --kind note --body hi",
    "continue task:relay --kind note --body hi",
    "continue cwd:/tmp --kind note --body hi",
    f"continue {OTHER} --kind note --body hi",
    f"continue {MACHINE}/llm/qwen38 --kind note --body hi",
    f"continue {MACHINE}/claude/latest --kind note --body hi",
    f"continue otherbox/claude/{OTHER} --kind note --body hi",
    "continue --kind note --body hi",
])
def test_a_continue_must_name_a_full_address_on_this_machine(relay, home, args):
    enable(home)
    verdict, reason = decision(run_hook(GATE_HOOK, bash_payload(client(args)), home)[1])
    assert verdict == "deny" and "full session address on this machine" in reason, args
    assert relay.requests == []


def test_a_continue_must_carry_its_text_in_body(relay, home):
    enable(home)
    verdict, reason = decision(run_hook(GATE_HOOK, bash_payload(client(f"continue {IDLE} --kind note --body -")),
                                        home)[1])
    assert verdict == "deny" and "so the owner sees what is sent" in reason
    assert relay.requests == []


_CONT = f"continue {IDLE} --kind note --body hi"


@pytest.mark.parametrize("command", [
    f"bash -c '{client(_CONT)}'",
    f"sh -c \"cd /tmp && {client(_CONT)}\"",
    f"nohup bash -c '{client(_CONT)}'",
    f"env FOO=1 nohup bash -c '{client(_CONT)}'",
    f"timeout 30 sh -c '{client(_CONT)}'",
    f"sudo bash -c '{client(_CONT)}'",
    f"eval '{client(_CONT)}'",
    f"env FOO=1 {client(_CONT)}",
    f"cd /tmp && {client(_CONT)}",
    f"echo $({client(_CONT)})",
    f"{client(_CONT)} | head",
    client(f'continue {IDLE} --kind note --body "$(id)"'),
])
@pytest.mark.parametrize("enabled", [True, False])
def test_a_wrapped_or_shell_c_continue_is_refused(relay, home, command, enabled):
    if enabled:
        enable(home)
    verdict, _ = decision(run_hook(GATE_HOOK, bash_payload(command), home)[1])
    assert verdict == "deny", command
    assert relay.requests == []


def test_gate_refuses_another_sessions_id_or_a_missing_one_without_asking_the_relay(relay, home):
    enable(home)
    enable(home, session=OTHER)
    for command in (client(f"read {TARGET}", session=OTHER), f"{LAUNCHER} relay client read {TARGET}"):
        verdict, reason = decision(run_hook(GATE_HOOK, bash_payload(command), home)[1])
        assert verdict == "deny" and "--session must be this session's id" in reason, command
    assert relay.requests == []


@pytest.mark.parametrize("args, why", [
    (f"read {TARGET} --grant grt_1", "omit --grant"),
    (f"read {TARGET} --config /tmp/relay.yaml", "omit --config"),
    (f"read {TARGET} --state-dir /tmp/state", "omit --state-dir"),
    (f"read {TARGET} --env-json /tmp/env.json", "omit --env-json"),
    (f"send {TARGET} --kind note --body -", "so the owner sees what is sent"),
])
def test_gate_refuses_options_it_does_not_allow(relay, home, args, why):
    enable(home)
    verdict, reason = decision(run_hook(GATE_HOOK, bash_payload(client(args)), home)[1])
    assert verdict == "deny" and why in reason
    assert relay.requests == []


@pytest.mark.parametrize("command", [
    f"cd /tmp && {client('machines')}",
    f"{client('machines')} | head",
    f"{client('machines')}; rm -rf /tmp/x",
    f"{client('machines')} > /tmp/out",
    f"FOO=1 {client('machines')}",
    f"bash -c '{client('machines')}'",
    f'echo "$({client("machines")})"',
    client(f'send {TARGET} --kind note --body "$(id)"'),
    client(f"send {TARGET} --kind note --body $HOME"),
    client(f"send {TARGET} --kind note --body `id`"),
    client(f"send {TARGET} --kind note --body hi*"),
    client(f"send {TARGET} --kind note --body ~/x"),
    client(f"send {TARGET} --kind note --body =ls"),
    client(f"send {TARGET} --kind note --body hi\nrm -rf /tmp/x"),
    f"python -m llm_kit relay client machines --session {SESSION}",
    f"llm-kit relay client machines --session {SESSION}",
])
def test_gate_refuses_every_form_it_cannot_read_exactly(relay, home, command):
    enable(home)
    verdict, reason = decision(run_hook(GATE_HOOK, bash_payload(command), home)[1])
    assert verdict == "deny", command
    assert relay.requests == []


def test_gate_refuses_an_imposter_launcher(relay, home, tmp_path):
    enable(home)
    fake = tmp_path / "bin" / "llm-kit"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\necho owned\n")
    fake.chmod(0o755)
    verdict, reason = decision(run_hook(GATE_HOOK, bash_payload(f"{fake} relay client machines --session {SESSION}"),
                                        home)[1])
    assert verdict == "deny" and str(LAUNCHER.resolve()) in reason
    assert relay.requests == []


def test_gate_allows_the_launcher_through_a_symlinked_path(relay, home, tmp_path):
    enable(home)
    link = tmp_path / "plugin-link"
    link.symlink_to(PLUGIN)
    _, out, _ = run_hook(GATE_HOOK, bash_payload(f"{link}/bin/llm-kit relay client machines --session {SESSION}"), home)
    assert decision(out)[0] == "allow"


def test_gate_does_not_guess_at_unknown_or_abbreviated_options(relay, home):
    enable(home)
    for command in (f"{LAUNCHER} relay client read {TARGET} --sess {SESSION}",
                    client(f"read {TARGET} -x"),
                    f"{LAUNCHER} relay client read 'unterminated"):
        verdict, _ = decision(run_hook(GATE_HOOK, bash_payload(command), home)[1])
        assert verdict == "deny", command
    assert relay.requests == []


def test_help_is_left_to_the_normal_permission_flow(relay, home):
    enable(home)
    rc, out, _ = run_hook(GATE_HOOK, bash_payload(f"{LAUNCHER} relay client read --help"), home)
    assert (rc, out) == (0, None) and relay.requests == []


@pytest.mark.parametrize("command", [
    "ls -la", "echo relay client", "git log --oneline", "grep -r 'relay client' docs",
    f"{LAUNCHER} relay status", f"{LAUNCHER} models list",
])
def test_gate_ignores_unrelated_bash(relay, home, command):
    rc, out, err = run_hook(GATE_HOOK, bash_payload(command), home)
    assert (rc, out, err) == (0, None, "")
    assert relay.requests == []


def test_gate_ignores_other_tools(relay, home):
    rc, out, _ = run_hook(GATE_HOOK, bash_payload(client("machines"), tool="Read"), home)
    assert (rc, out) == (0, None) and relay.requests == []


def test_gate_relay_down_allows_an_enabled_call_with_a_note(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
    env = make_home(tmp_path, dead_port)
    enable(env)
    rc, out, _ = run_hook(GATE_HOOK, bash_payload(client("machines")), env)
    assert rc == 0 and decision(out)[0] == "allow"
    assert "relay is not running" in out["hookSpecificOutput"]["additionalContext"]


def test_gate_relay_down_still_refuses_a_session_that_is_not_enabled(tmp_path):
    env = make_home(tmp_path, 9)
    assert decision(run_hook(GATE_HOOK, bash_payload(client("machines")), env)[1])[0] == "deny"


def test_a_mint_refusal_denies_with_the_relays_code(relay, home):
    enable(home)
    relay.mint_error = ("target_ambiguous", "latest matches 2 sessions")
    verdict, reason = decision(run_hook(GATE_HOOK, bash_payload(client("read latest")), home)[1])
    assert verdict == "deny" and "target_ambiguous" in reason


def test_a_loose_record_mode_is_refused_not_trusted(relay, home):
    enable(home)
    record_path(home).chmod(0o644)
    verdict, reason = decision(run_hook(GATE_HOOK, bash_payload(client("machines")), home)[1])
    assert verdict == "deny" and "loose mode" in reason
    assert relay.requests == []


def test_gate_bad_input_fails_closed_loudly(relay, home):
    for stdin in ("not json", json.dumps({"tool_name": "Bash", "tool_input": {}, "session_id": SESSION}),
                  json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})):
        rc, out, err = run_hook(GATE_HOOK, stdin, home)
        assert rc == 2 and out is None and "direct_gate.py:" in err
    assert relay.requests == []


# -- SessionEnd: /clear and the end of the session -----------------------------------------------------

@pytest.mark.parametrize("reason", ["clear", "prompt_input_exit", "logout", "other"])
def test_session_end_revokes_only_that_session(relay, home, reason):
    enable(home)
    enable(home, session=OTHER)
    rc, out, err = run_hook(REVOKE_HOOK, end_payload(reason=reason), home)
    assert (rc, out, err) == (0, None, "")
    assert not record_path(home).exists() and record_path(home, OTHER).exists()
    verdict, _ = decision(run_hook(GATE_HOOK, bash_payload(client("machines")), home)[1])
    assert verdict == "deny"
    _, out, _ = run_hook(GATE_HOOK, bash_payload(client("machines", session=OTHER), session=OTHER), home)
    assert decision(out)[0] == "allow"


def test_session_end_without_a_record_is_quiet(relay, home):
    assert run_hook(REVOKE_HOOK, end_payload(), home) == (0, None, "")


def test_session_end_bad_input_fails_loudly(relay, home):
    rc, out, err = run_hook(REVOKE_HOOK, "not json", home)
    assert rc == 1 and "direct_revoke.py:" in err


# -- plain_words, the gate's reader ---------------------------------------------------------------------

def _gate_module():
    import importlib.util

    sys.path.insert(0, str(HOOKS))
    try:
        spec = importlib.util.spec_from_file_location("direct_gate_words", GATE_HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(HOOKS))
    return module


@pytest.mark.parametrize("command, words", [
    ("a b\tc", ["a", "b", "c"]),
    ("--body 'two words'", ["--body", "two words"]),
    ("'it'\\''s'", ["it's"]),
    ("'line one\nline two'", ["line one\nline two"]),
    ("''", [""]),
    ("--kind=note", ["--kind=note"]),
    ("q:m5pro/jobs:run:1 a@b%c+d,e", ["q:m5pro/jobs:run:1", "a@b%c+d,e"]),
])
def test_plain_words_reads_bare_and_single_quoted_text(command, words):
    assert _gate_module().plain_words(command) == words


@pytest.mark.parametrize("command", [
    'a "b"', "a $b", "a `b`", "a b*", "a ~/b", "a =b", "a 'b", "a\nb", "a;b", "a|b", "a&b", "a>b", "a(b)",
    "a {b,c}", "a [b]", "a b?", "a\\\nb", "a #b", "a !b",
])
def test_plain_words_refuses_anything_a_shell_would_expand(command):
    assert _gate_module().plain_words(command) is None


# -- wrapper and registration ----------------------------------------------------------------------------

def _fake_standalone(home: dict[str, str]) -> Path:
    """A stand-in standalone interpreter at the path the wrapper looks for: a stub that execs this test's Python."""
    stub = Path(home["HOME"]) / ".local" / "share" / "python-standalone" / "python" / "bin" / "python3"
    stub.parent.mkdir(parents=True)
    stub.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    stub.chmod(0o755)
    return stub


@pytest.mark.skipif(os.name == "nt", reason="the wrapper is POSIX sh")
def test_wrapper_runs_a_hook_under_the_standalone_interpreter(relay, home):
    _fake_standalone(home)
    env = {k: v for k, v in home.items() if k != "BOOTSTRAP_PYTHON"}
    done = subprocess.run(["sh", str(WRAPPER), "userpromptsubmit/direct_enable.py"],
                          input=json.dumps(prompt_payload("/direct")),
                          capture_output=True, text=True, env=env, timeout=30)
    assert done.returncode == 0, done.stderr
    assert record_path(home).exists()


@pytest.mark.skipif(os.name == "nt", reason="the wrapper is POSIX sh")
def test_wrapper_without_an_approved_interpreter_says_so_and_does_not_block(relay, home):
    env = {k: v for k, v in home.items() if k != "BOOTSTRAP_PYTHON"}
    env["BOOTSTRAP_PYTHON"] = sys.executable  # outside the standalone directory: must not be accepted
    done = subprocess.run(["sh", str(WRAPPER), "pretooluse/direct_gate.py"],
                          input=json.dumps(bash_payload(client("machines"))),
                          capture_output=True, text=True, env=env, timeout=30)
    assert done.returncode == 0
    assert "no approved interpreter" in json.loads(done.stdout)["systemMessage"]
    assert relay.requests == []


def test_hooks_json_registers_the_three_hooks_on_existing_scripts():
    config = json.loads((HOOKS / "hooks.json").read_text())["hooks"]
    assert set(config) == {"UserPromptSubmit", "PreToolUse", "SessionEnd"}
    (prompt_entry,) = config["UserPromptSubmit"]
    (gate_entry,) = config["PreToolUse"]
    (end_entry,) = config["SessionEnd"]
    assert gate_entry["matcher"] == "Bash" and "matcher" not in end_entry
    for entry, script in ((prompt_entry, "userpromptsubmit/direct_enable.py"),
                          (gate_entry, "pretooluse/direct_gate.py"), (end_entry, "sessionend/direct_revoke.py")):
        (hook,) = entry["hooks"]
        assert hook["type"] == "command"
        assert hook["command"] == f'bash "${{CLAUDE_PLUGIN_ROOT}}/hooks/run-direct-hook.sh" {script}'
        assert (HOOKS / script).is_file()


def test_hook_sources_are_ascii_and_state_they_are_a_guardrail():
    for path in (ENABLE_HOOK, GATE_HOOK, REVOKE_HOOK, HOOKS / "direct_relay.py", WRAPPER, HOOKS / "hooks.json"):
        path.read_text(encoding="utf-8").encode("ascii")
    for path in (ENABLE_HOOK, GATE_HOOK, HOOKS / "direct_relay.py"):
        assert "NOT A SECURITY BOUNDARY" in path.read_text()


def test_the_launcher_the_gate_allows_is_this_plugins_shim():
    assert _gate_module().R.launcher() == LAUNCHER.resolve() and LAUNCHER.is_file()


# -- known limitations, pinned (alternative A is a guardrail) ----------------------------------------------

def test_known_limitation_hook_input_is_a_claim_anything_that_can_run_the_hook_can_enable(relay, home):
    """The hook cannot tell a typed prompt from a forged payload: a same-user model can pipe one in."""
    rc, out, _ = run_hook(ENABLE_HOOK, prompt_payload("/direct"), home)
    assert rc == 0 and record_path(home).exists()


def test_known_limitation_the_gate_reads_command_text_not_behaviour(relay, home, tmp_path):
    """A script that makes the call is invisible to the gate (no opinion); the relay then finds no grant."""
    script = tmp_path / "go.sh"
    script.write_text(client("machines") + "\n")
    for command in (f"bash {script}", 'X="llm-kit relay"; $X client machines --session s'):
        rc, out, _ = run_hook(GATE_HOOK, bash_payload(command), home)
        assert (rc, out) == (0, None), command
    assert relay.requests == []


# -- the gate engages on an invocation, not on a mention ------------------------------------------------

PHRASE = "llm-kit relay client"

MENTIONS = [
    f'git commit -m "gate: narrow {PHRASE} matching"',
    f"git commit -m 'it is the {PHRASE} gate'",
    f'git commit -m "$(cat <<\'MSG\'\nfix: the {PHRASE} gate\n\nit\'s a "quoted" mention\nMSG\n)"',
    f"python3 - <<'EOF'\nprint(\"it's the {PHRASE} gate\")\nEOF",
    f"cat <<EOF > /tmp/notes.txt\nthe {PHRASE} gate's body\nEOF",
    f"echo '{PHRASE} send'",
    f'echo "{PHRASE} send" >> /tmp/log',
    f"printf '%s\\n' \"{PHRASE}\"",
    f"grep -rn '{PHRASE}' docs",
    f"grep -rn \"{PHRASE}\" docs | head",
    f"# {PHRASE} is mentioned in a comment\nls",
    f"ls # {PHRASE}",
    f"echo hi 2> /tmp/{PHRASE.replace(' ', '_')}.err",
    "tail -f /tmp/x.log  # relay client note",
    f'gh pr create --title "t" --body "uses {PHRASE} send"',
]


@pytest.mark.parametrize("command", MENTIONS)
def test_a_mention_of_the_relay_client_does_not_engage_the_gate(relay, home, command):
    rc, out, err = run_hook(GATE_HOOK, bash_payload(command), home)
    assert (rc, out, err) == (0, None, ""), command
    assert relay.requests == []


REAL_FORMS = [
    client(f"read {TARGET} --limit 5"),
    client("machines"),
    f"{LAUNCHER} relay client sessions --session {SESSION} --state live",
    f"{LAUNCHER} relay client send {TARGET} --session {SESSION} --kind note --body 'it'\\''s text'",
    f"{LAUNCHER} relay client ask {TARGET} --session {SESSION} --prompt 'hi'",
    f"{LAUNCHER} relay client replies --session {SESSION} --message-id m1",
    f"{LAUNCHER} relay client questions --session {SESSION}",
    f"{LAUNCHER} relay client answer q1 --session {SESSION} --decision reject --reason 'no'",
    f"{LAUNCHER} relay client continue {IDLE} --session {SESSION} --kind note --body 'go on'",
]


@pytest.mark.parametrize("command", REAL_FORMS)
def test_every_real_invocation_form_engages_the_gate_and_is_refused_when_not_enabled(relay, home, command):
    verdict, reason = decision(run_hook(GATE_HOOK, bash_payload(command), home)[1])
    assert verdict == "deny" and "/direct is not enabled in this session" in reason, command
    assert relay.requests == []


@pytest.mark.parametrize("command", [
    f"cd /tmp && {client('machines')}",
    f"cd /tmp; {client('machines')}",
    f"false || {client('machines')}",
    f"echo hi\n{client('machines')}",
    f"echo $({client('machines')})",
    f'echo "$({client("machines")})"',
    f"echo `{client('machines')}`",
    f"cat <<EOF\n$({client('machines')})\nEOF",
    f"bash -c '{client('machines')}'",
    f"sh -c \"cd /tmp && {client('machines')}\"",
    f"eval '{client('machines')}'",
    f"FOO=1 {client('machines')}",
    f"env FOO=1 {client('machines')}",
    f"( {client('machines')} )",
    f"{client('machines')} | head",
    f"{client('machines')} > /tmp/out",
    f"echo hi | xargs {client('machines')}",
    f"python -m llm_kit relay client machines --session {SESSION}",
    f"uv run llm-kit relay client machines --session {SESSION}",
    f"llm-kit relay client machines --session {SESSION}",
    f"git commit -m 'x' && {client('machines')}",
    f"nohup bash -c '{client('machines')}'",
    f"env bash -c '{client('machines')}'",
    f"timeout 30 sh -c '{client('machines')}'",
    f"sudo bash -c '{client('machines')}'",
    f"env FOO=1 nohup bash -c '{client('machines')}'",
])
def test_a_chained_or_wrapped_real_invocation_still_engages_the_gate(relay, home, command):
    verdict, reason = decision(run_hook(GATE_HOOK, bash_payload(command), home)[1])
    assert verdict == "deny", command
    assert relay.requests == []


@pytest.mark.parametrize("command", [
    f'$CLI relay client machines --session {SESSION}',
    f'"$LAUNCHER" relay client machines --session {SESSION}',
    f"$(which llm-kit) relay client machines --session {SESSION}",
    f"{LAUNCHER} relay client read 'unterminated",
    f"git commit -m \"it's\" && {LAUNCHER} relay client read 'unterminated",
])
def test_an_unreadable_or_ambiguous_real_invocation_fails_closed(relay, home, command):
    enable(home)
    verdict, _ = decision(run_hook(GATE_HOOK, bash_payload(command), home)[1])
    assert verdict == "deny", command
    assert relay.requests == []


def test_the_enabled_plain_call_is_still_allowed_after_the_matcher_change(relay, home):
    enable(home)
    assert decision(run_hook(GATE_HOOK, bash_payload(client("machines")), home)[1])[0] == "allow"
    # a mention in an enabled session stays ignored, not allowed
    assert run_hook(GATE_HOOK, bash_payload(f'git commit -m "{PHRASE}"'), home)[1] is None
