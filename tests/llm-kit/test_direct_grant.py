"""The /direct grant hooks (alternative A) against a fake relay.

Real hook scripts run as subprocesses, fed hook-shaped JSON on stdin, against a
stdlib HTTP server that speaks enough relay-protocol/1 to mint and check grants
(token check, origin identity, one session mints only for itself, coverage by
verb, session and target, check never consumes).

What the hooks are: a GUARDRAIL, not a security boundary (relay alternative A).
The last tests pin the known bypasses as documented limitations so nobody reads
the gate as stronger than it is.

Each guard test names the edit that turns it red:

* typed-only minting -- in userpromptsubmit/direct_grant.py change
  ``_PREFIX_RE.match(text)`` to ``_PREFIX_RE.search(text)`` and delete the
  ``<channel `` check: test_channel_wrapped_direct_does_not_mint goes red.
* the gate's refusal -- in pretooluse/direct_gate.py change
  ``if result.get("covered") is not True:`` to ``if False:``:
  test_gate_denies_without_a_grant goes red.
* the gate never consumes -- the check goes through ``grant_check``; change the
  gate's verb to ``grant_mint`` and test_gate_allows_with_a_grant_and_never_consumes
  goes red.
* relay down never blocks -- raise instead of returning the notice in
  direct_grant.run's RelayUnavailable branch: test_relay_down_never_blocks_the_prompt
  goes red.
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
GRANT_HOOK = HOOKS / "userpromptsubmit" / "direct_grant.py"
GATE_HOOK = HOOKS / "pretooluse" / "direct_gate.py"
WRAPPER = HOOKS / "run-direct-hook.sh"

MACHINE = "testbox"
SESSION = "b91e0c44-2f8a-4d3b-8c6e-71a5d0e3f2a9"
OTHER = "7f3c2a10-5b7e-4c1d-9a8e-0d6f4b2c9e11"
TOKEN = "tok-" + "a" * 40
READ_TARGET = f"{MACHINE}/claude/{OTHER}"


# -- fake relay -------------------------------------------------------------------------------

class FakeRelay:
    """Enough of relay-protocol/1 to mint and check grants, per protocol section 14."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.grants: list[dict[str, Any]] = []
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
        origin = envelope["origin"]
        params = envelope["params"]
        if verb in ("grant_mint", "grant_check"):
            if not origin.startswith("direct:") or origin[len("direct:"):] != params["session"]:
                return self._error(rid, "origin_not_allowed", "a /direct session acts only for itself")
        if verb == "grant_mint":
            if self.mint_error:
                return self._error(rid, *self.mint_error)
            grant = {
                "grant_id": f"grt_{len(self.grants) + 1}", "session": params["session"], "verb": params["verb"],
                "targets": list(params["targets"]), "uses": params["uses"], "uses_remaining": params["uses"],
                "state": "live", "origin_proof": params["origin_proof"],
            }
            self.grants.append(grant)
            return {"protocol": "relay-protocol/1", "id": rid, "ok": True, "result": {"grant": dict(grant)}}
        if verb == "grant_check":
            for grant in self.grants:
                if params.get("grant_id") not in (None, grant["grant_id"]):
                    continue
                target = params.get("target")
                covers = (grant["session"] == params["session"] and grant["verb"] == params["verb"]
                          and grant["uses_remaining"] > 0
                          and ((target is None and not grant["targets"]) or (target in grant["targets"])))
                if covers:
                    return {"protocol": "relay-protocol/1", "id": rid, "ok": True,
                            "result": {"covered": True, "grant_id": grant["grant_id"], "code": None}}
            return {"protocol": "relay-protocol/1", "id": rid, "ok": True,
                    "result": {"covered": False, "grant_id": None, "code": "grant_required"}}
        return self._error(rid, "unknown_verb", verb)

    def verbs(self) -> list[str]:
        return [r["verb"] for r in self.requests]


@pytest.fixture()
def relay():
    fake = FakeRelay()
    yield fake
    fake.close()


@pytest.fixture()
def home(tmp_path, relay):
    """A scratch HOME with env.json, a relay token (0600) and relay.yaml pointing at the fake relay."""
    return make_home(tmp_path, relay.port)


def make_home(tmp_path: Path, port: int, *, token: Optional[str] = TOKEN) -> dict[str, str]:
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
    (cfg / "relay.yaml").write_text(f"relay:\n  host: 127.0.0.1\n  port: {port}\n")
    env = {k: v for k, v in os.environ.items() if k not in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "HOME")}
    env.update({"HOME": str(root), "XDG_STATE_HOME": str(root / "state"), "XDG_CONFIG_HOME": str(root / "cfg")})
    return env


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


def context(out: Optional[dict]) -> str:
    assert out is not None
    return out["hookSpecificOutput"]["additionalContext"]


# -- UserPromptSubmit: minting --------------------------------------------------------------------

def test_typed_direct_mints_a_grant(relay, home):
    rc, out, err = run_hook(GRANT_HOOK, prompt_payload(f"/direct read {READ_TARGET}"), home)
    assert (rc, err) == (0, "")
    assert relay.verbs() == ["grant_mint"]
    request = relay.requests[0]
    assert request["token"] == TOKEN
    assert request["origin"] == f"direct:{MACHINE}/claude/{SESSION}"
    assert request["params"] == {
        "session": f"{MACHINE}/claude/{SESSION}", "verb": "read_session", "targets": [READ_TARGET],
        "expires_in_s": 300, "uses": 8, "origin_proof": {"type": "none"},
        "command": f"/direct read {READ_TARGET}",
    }
    assert "grant was minted" in context(out)
    assert "the prompt starts with /direct" in context(out)
    assert "the user typed" not in context(out)


@pytest.mark.parametrize("line, verb, targets, uses", [
    ("/direct machines", "list_machines", [], 8),
    ("/direct sessions", "list_sessions", [], 8),
    ("/direct sessions testbox", "list_sessions", ["testbox"], 8),
    (f"/direct send {READ_TARGET} Are the relay tests green?", "send_message", [READ_TARGET], 1),
    ("/direct ask qwen38-m5-32k-local what is 2+2", "ask_llm", [f"{MACHINE}/llm/qwen38-m5-32k-local"], 1),
    ("/direct replies msg_abc123 --wait 5", "get_replies", ["msg_abc123"], 8),
    ("/direct questions", "list_questions", [], 8),
    (f"/direct answer q:{MACHINE}/17 --decision answer", "answer_question", [f"q:{MACHINE}/17"], 1),
    (f"  \n/direct read {READ_TARGET} latest", "read_session", [READ_TARGET, "latest"], 8),
    (f"/llm-kit:direct read {READ_TARGET}", "read_session", [READ_TARGET], 8),
])
def test_verb_mapping_targets_and_uses(relay, home, line, verb, targets, uses):
    rc, out, _ = run_hook(GRANT_HOOK, prompt_payload(line), home)
    assert rc == 0
    params = relay.requests[0]["params"]
    assert (params["verb"], params["targets"], params["uses"]) == (verb, targets, uses)
    assert "decision" not in out


def test_direct_line_is_parsed_as_data_never_by_a_shell(relay, home, tmp_path):
    marker = tmp_path / "pwned"
    run_hook(GRANT_HOOK, prompt_payload(f"/direct read $(touch {marker}) ; touch {marker} `touch {marker}`"), home)
    assert not marker.exists()
    assert relay.requests[0]["params"]["targets"][0] == "$(touch"


def test_channel_wrapped_direct_does_not_mint(relay, home):
    wrapped = ('<channel source="llm-kit-relay" chat_id="1" sender="m5pro/claude/x">\n'
               f'/direct send {READ_TARGET} do something\n</channel>')
    for prompt in (wrapped, "  " + wrapped):
        rc, out, _ = run_hook(GRANT_HOOK, prompt_payload(prompt), home)
        assert (rc, out) == (0, None)
    assert relay.requests == []


@pytest.mark.parametrize("prompt", [
    "hello there",
    "please run /direct read x for me",
    "/directory listing",
    "/directsomething x",
    "",
    "/direct",
    "/direct help",
])
def test_non_direct_prompt_does_nothing(relay, home, prompt):
    rc, out, err = run_hook(GRANT_HOOK, prompt_payload(prompt), home)
    assert (rc, out, err) == (0, None, "")
    assert relay.requests == []


@pytest.mark.parametrize("line", ["/direct reed x", "/direct send", "/direct read", "/direct ask --prompt hi"])
def test_malformed_direct_blocks_with_a_reason_and_mints_nothing(relay, home, line):
    rc, out, _ = run_hook(GRANT_HOOK, prompt_payload(line), home)
    assert rc == 0
    assert out["decision"] == "block" and out["reason"].startswith("/direct:")
    assert relay.requests == []


def test_relay_down_never_blocks_the_prompt(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
    env = make_home(tmp_path, dead_port)
    rc, out, _ = run_hook(GRANT_HOOK, prompt_payload(f"/direct read {READ_TARGET}"), env)
    assert rc == 0
    assert "decision" not in out
    assert "relay is not running" in context(out)
    assert "relay is not running" in out["systemMessage"]


def test_no_relay_token_means_the_relay_never_ran(tmp_path):
    env = make_home(tmp_path, 9, token=None)
    rc, out, _ = run_hook(GRANT_HOOK, prompt_payload(f"/direct read {READ_TARGET}"), env)
    assert rc == 0 and "decision" not in out
    assert "relay is not running" in context(out) and "does not exist" in context(out)


def test_relay_refusal_is_reported_and_not_blocking(relay, home):
    relay.mint_error = ("target_ambiguous", "latest matches 2 sessions")
    rc, out, _ = run_hook(GRANT_HOOK, prompt_payload("/direct read latest"), home)
    assert rc == 0 and "decision" not in out
    assert "target_ambiguous" in context(out) and "No grant was minted" in context(out)


def test_a_loose_token_mode_is_refused_not_used(relay, home):
    token = Path(home["XDG_STATE_HOME"]) / "llm-kit" / "relay" / "relay.token"
    token.chmod(0o644)
    assert stat.S_IMODE(token.stat().st_mode) == 0o644
    rc, out, _ = run_hook(GRANT_HOOK, prompt_payload(f"/direct read {READ_TARGET}"), home)
    assert rc == 0 and "loose mode" in context(out)
    assert relay.requests == []


def test_unresolvable_machine_is_reported(relay, home):
    (Path(home["HOME"]) / ".claude" / "env.json").write_text(json.dumps({"machines": {"other-host": {}}}))
    rc, out, _ = run_hook(GRANT_HOOK, prompt_payload(f"/direct read {READ_TARGET}"), home)
    assert rc == 0 and "machine label is unresolved" in context(out)
    assert relay.requests == []


@pytest.mark.parametrize("stdin", ["not json", "[]", json.dumps({"prompt": "/direct machines"}),
                                   json.dumps({"session_id": "latest", "prompt": "/direct machines"})])
def test_bad_hook_input_fails_loudly(relay, home, stdin):
    rc, out, err = run_hook(GRANT_HOOK, stdin, home)
    assert rc == 1 and out is None and "direct_grant.py:" in err
    assert relay.requests == []


# -- PreToolUse: the gate ------------------------------------------------------------------------------

CLIENT = f"llm-kit relay client read {READ_TARGET} --session {SESSION}"


def mint(relay, home, line: str) -> None:
    rc, out, _ = run_hook(GRANT_HOOK, prompt_payload(line), home)
    assert rc == 0 and "decision" not in out
    assert relay.verbs()[-1] == "grant_mint"


def test_gate_denies_without_a_grant(relay, home):
    rc, out, _ = run_hook(GATE_HOOK, bash_payload(CLIENT), home)
    assert rc == 0
    decision = out["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "grant_required" in decision["permissionDecisionReason"]
    assert f"/direct read {READ_TARGET}" in decision["permissionDecisionReason"]
    assert "Only the owner should grant it" in decision["permissionDecisionReason"]
    assert "can grant it" not in decision["permissionDecisionReason"]
    assert relay.verbs() == ["grant_check"]


def test_gate_allows_with_a_grant_and_never_consumes(relay, home):
    mint(relay, home, f"/direct send {READ_TARGET} hello")
    send = f"llm-kit relay client send {READ_TARGET} --kind note --body hi --session {SESSION}"
    for _ in range(3):  # a one-use write grant: repeated checks must all still pass
        rc, out, _ = run_hook(GATE_HOOK, bash_payload(send), home)
        assert (rc, out) == (0, None)
    assert relay.grants[0]["uses_remaining"] == 1
    assert relay.verbs() == ["grant_mint"] + ["grant_check"] * 3


def test_gate_covers_by_verb_session_and_target(relay, home):
    mint(relay, home, f"/direct read {READ_TARGET}")
    denied = [
        f"llm-kit relay client read {MACHINE}/claude/other-session --session {SESSION}",        # wrong target
        f"llm-kit relay client send {READ_TARGET} --kind note --body x --session {SESSION}",     # wrong verb
        f"llm-kit relay client sessions --session {SESSION}",                                     # wrong verb
    ]
    for command in denied:
        _, out, _ = run_hook(GATE_HOOK, bash_payload(command), home)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", command
    # the same call from another session is not covered by this session's grant
    _, out, _ = run_hook(GATE_HOOK, bash_payload(CLIENT.replace(SESSION, OTHER), OTHER), home)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    _, out, _ = run_hook(GATE_HOOK, bash_payload(CLIENT), home)
    assert out is None


def test_gate_refuses_a_session_flag_for_another_session_without_asking_the_relay(relay, home):
    _, out, _ = run_hook(GATE_HOOK, bash_payload(CLIENT.replace(SESSION, OTHER)), home)
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny" and "not this session" in reason
    assert relay.requests == []


@pytest.mark.parametrize("command", [
    "ls -la",
    "echo relay client",
    "git log --oneline",
    "grep -r 'relay client' docs",
    "llm-kit relay status",
    "llm-kit models list",
])
def test_gate_ignores_unrelated_bash(relay, home, command):
    rc, out, err = run_hook(GATE_HOOK, bash_payload(command), home)
    assert (rc, out, err) == (0, None, "")
    assert relay.requests == []


def test_gate_ignores_other_tools(relay, home):
    rc, out, _ = run_hook(GATE_HOOK, bash_payload(CLIENT, tool="Read"), home)
    assert (rc, out) == (0, None) and relay.requests == []


@pytest.mark.parametrize("command", [
    f"cd /tmp && {CLIENT}",
    f"FOO=1 {CLIENT} | head",
    f"bash -c '{CLIENT}'",
    f'echo "$({CLIENT})"',
    f"/Users/x/.claude/plugins/data/plugins-kit/llm-kit/.venv/bin/llm-kit relay client read {READ_TARGET} --session={SESSION}",
    f"python -m llm_kit relay client read {READ_TARGET} --session {SESSION}",
])
def test_gate_sees_the_plain_wrapped_forms(relay, home, command):
    _, out, _ = run_hook(GATE_HOOK, bash_payload(command), home)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", command
    assert relay.verbs() == ["grant_check"]


def test_gate_does_not_guess_at_unknown_or_abbreviated_options(relay, home):
    for command in (f"llm-kit relay client read {READ_TARGET} --sess {SESSION}",
                    f"llm-kit relay client read {READ_TARGET} --session {SESSION} -x",
                    "llm-kit relay client read 'unterminated"):
        _, out, _ = run_hook(GATE_HOOK, bash_payload(command), home)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", command
    assert relay.requests == []


def test_gate_relay_down_allows_with_a_note_not_a_denial(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
    env = make_home(tmp_path, dead_port)
    rc, out, _ = run_hook(GATE_HOOK, bash_payload(CLIENT), env)
    assert rc == 0
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert "relay is not running" in out["hookSpecificOutput"]["additionalContext"]


def test_gate_bad_input_fails_closed_loudly(relay, home):
    for stdin in ("not json", json.dumps({"tool_name": "Bash", "tool_input": {}, "session_id": SESSION}),
                  json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})):
        rc, out, err = run_hook(GATE_HOOK, stdin, home)
        assert rc == 2 and out is None and "direct_gate.py:" in err
    assert relay.requests == []


def test_gate_asks_the_relay_for_a_check_only(relay, home):
    """Even for a write verb with a live grant, the gate's only relay verb is grant_check."""
    mint(relay, home, f"/direct send {READ_TARGET} hi")
    relay.requests.clear()
    run_hook(GATE_HOOK, bash_payload(f"llm-kit relay client send {READ_TARGET} --kind note --body hi --session {SESSION}"), home)
    assert relay.verbs() == ["grant_check"]
    assert relay.grants[0]["uses_remaining"] == 1


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
    done = subprocess.run(["sh", str(WRAPPER), "userpromptsubmit/direct_grant.py"],
                          input=json.dumps(prompt_payload(f"/direct read {READ_TARGET}")),
                          capture_output=True, text=True, env=env, timeout=30)
    assert done.returncode == 0, done.stderr
    assert relay.verbs() == ["grant_mint"]


@pytest.mark.skipif(os.name == "nt", reason="the wrapper is POSIX sh")
def test_wrapper_without_an_approved_interpreter_says_so_and_does_not_block(relay, home):
    env = {k: v for k, v in home.items() if k != "BOOTSTRAP_PYTHON"}
    env["BOOTSTRAP_PYTHON"] = sys.executable  # outside the standalone directory: must not be accepted
    done = subprocess.run(["sh", str(WRAPPER), "pretooluse/direct_gate.py"], input=json.dumps(bash_payload(CLIENT)),
                          capture_output=True, text=True, env=env, timeout=30)
    assert done.returncode == 0
    assert "no approved interpreter" in json.loads(done.stdout)["systemMessage"]
    assert relay.requests == []


def test_hooks_json_registers_both_hooks_on_existing_scripts():
    config = json.loads((HOOKS / "hooks.json").read_text())["hooks"]
    (prompt_entry,) = config["UserPromptSubmit"]
    (gate_entry,) = config["PreToolUse"]
    assert gate_entry["matcher"] == "Bash"
    for entry in (prompt_entry, gate_entry):
        (hook,) = entry["hooks"]
        assert hook["type"] == "command"
        command = hook["command"]
        assert command.startswith('bash "${CLAUDE_PLUGIN_ROOT}/hooks/run-direct-hook.sh" ')
        script = command.rsplit(" ", 1)[1]
        assert (HOOKS / script).is_file()
    assert prompt_entry["hooks"][0]["command"].endswith("userpromptsubmit/direct_grant.py")
    assert gate_entry["hooks"][0]["command"].endswith("pretooluse/direct_gate.py")


def test_hook_sources_are_ascii_and_state_they_are_a_guardrail():
    for path in (GRANT_HOOK, GATE_HOOK, HOOKS / "direct_relay.py", WRAPPER, HOOKS / "hooks.json"):
        text = path.read_text(encoding="utf-8")
        text.encode("ascii")
    for path in (GRANT_HOOK, GATE_HOOK, HOOKS / "direct_relay.py"):
        assert "NOT A SECURITY BOUNDARY" in path.read_text()


# -- known limitations, pinned (alternative A is a guardrail) ----------------------------------------------

def test_known_limitation_hook_input_is_a_claim_anything_that_can_run_the_hook_can_mint(relay, home):
    """The hook cannot tell a typed prompt from a forged payload: a same-user model can pipe one in."""
    rc, out, _ = run_hook(GRANT_HOOK, prompt_payload(f"/direct send {READ_TARGET} forged"), home)
    assert rc == 0 and relay.verbs() == ["grant_mint"]


def test_known_limitation_the_gate_reads_command_text_not_behaviour(relay, home, tmp_path):
    """A script that makes the call, or a call the gate cannot see in the command text, has no opinion."""
    script = tmp_path / "go.sh"
    script.write_text(CLIENT + "\n")
    for command in (f"bash {script}", 'X="llm-kit relay"; $X client machines --session s'):
        rc, out, _ = run_hook(GATE_HOOK, bash_payload(command), home)
        assert (rc, out) == (0, None), command
    assert relay.requests == []
