"""Dashboard security: the private runtime root and owner-only ownership record,
the mutation-route table and its gates, and the idle-connection timeout.

The token in ``server.json`` authorizes queueing runs that start with
bypassPermissions, so on a shared machine no other local account may read it.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest
from dashboard_support import ENTRYPOINT, Running, make_task

from task_system.dashboard import launcher as L
from task_system.dashboard import state as dstate

posix_only = pytest.mark.skipif(
    os.name == "nt", reason="POSIX modes; Windows privacy comes from the profile ACL, which is not asserted")


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def world(tmp_path):
    devroot = tmp_path / "devroot"
    (devroot / "alpha").mkdir(parents=True)
    make_task(devroot / "alpha", "dev/tasks", "one", title="One",
              items=[{"id": "an-item", "title": "An item", "state": "available"}])
    return devroot


@pytest.fixture
def umask_022():
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


# ---------------------------------------------------------------------------
# H2: private root, owner-only server.json
# ---------------------------------------------------------------------------


@posix_only
def test_a_new_server_creates_an_owner_only_root_and_record(tmp_path, world, umask_022):
    """Red: restore ``root.mkdir`` and the plain ``open(temp, "wb")``; the modes read 0o755 and 0o644."""
    running = Running(tmp_path / "data", (world,))
    try:
        root = tmp_path / "data" / "dashboard"
        assert mode(root) == 0o700
        assert mode(root / "server.json") == 0o600
    finally:
        running.stop()


@posix_only
def test_an_existing_world_readable_root_and_record_are_tightened_and_logged(tmp_path, world, umask_022):
    root = tmp_path / "data" / "dashboard"
    root.mkdir(parents=True)
    root.chmod(0o755)
    stale = root / "server.json"
    stale.write_text("{}", encoding="utf-8")
    stale.chmod(0o644)
    running = Running(tmp_path / "data", (world,))
    try:
        assert mode(root) == 0o700
        assert mode(root / "server.json") == 0o600  # replaced by the new record, owner-only
        assert f"tightened {root} to owner-only" in running.log.getvalue()
    finally:
        running.stop()


@posix_only
def test_a_stale_temp_record_is_chmodded_before_it_is_replaced(tmp_path, umask_022):
    """O_TRUNC keeps the old mode of a leftover temp file; the write must still end 0600."""
    root = tmp_path / "dashboard"
    root.mkdir(mode=0o700)
    leftover = root / f"server.json.{os.getpid()}.tmp"
    leftover.write_text("old", encoding="utf-8")
    leftover.chmod(0o666)
    state = dstate.ServerState(schema=1, pid=4242, pid_create_time=1.0, nonce="n", port=40000, token="t",
                               code_hash="h", started_at="2026-10-17T00:00:00+00:00", log_path="x")
    dstate.write_state(root, state)
    assert mode(root / "server.json") == 0o600
    assert dstate.read_state(root) == state


@posix_only
def test_an_already_private_root_is_not_reported_as_tightened(tmp_path, umask_022):
    root = tmp_path / "dashboard"
    assert dstate.ensure_private_root(root) is False  # created 0700
    assert mode(root) == 0o700
    assert dstate.ensure_private_root(root) is False
    root.chmod(0o750)
    assert dstate.ensure_private_root(root) is True
    assert mode(root) == 0o700


@posix_only
def test_spawn_server_creates_the_root_owner_only(tmp_path, monkeypatch, umask_022):
    class FakePopen:
        def __init__(self, command, **kwargs):
            pass

    monkeypatch.setattr(L.subprocess, "Popen", FakePopen)
    root = tmp_path / "data" / "dashboard"
    L.spawn_server(ENTRYPOINT, tmp_path / "data", root / "server.log")
    assert mode(root) == 0o700


# ---------------------------------------------------------------------------
# H8: every mutation route refuses before acting; idle connections close
# ---------------------------------------------------------------------------

import re  # noqa: E402
import socket  # noqa: E402
import urllib.parse  # noqa: E402

from dashboard_support import SCRIPTS_DIR, tree_digest  # noqa: E402
from rig_support import TASK, Rig  # noqa: E402

from task_system.dashboard import endpoint_helper, runtime, server  # noqa: E402

DASHBOARD_DIR = SCRIPTS_DIR / "task_system" / "dashboard"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    built = Rig(tmp_path, monkeypatch, behavior="hang")
    try:
        yield built
    finally:
        built.close()


def concrete_paths(rig: Rig, run_id: str) -> dict:
    """One real path per MUTATION_ROUTES entry, built from ids the rig really holds."""
    task = urllib.parse.quote(TASK, safe="")
    base = f"/api/v1/items/{rig.pid}/{task}/do-it"
    return {
        ("POST", "items/{}/{}/{}/queue"): f"{base}/queue",
        ("POST", "items/{}/{}/{}/recheck"): f"{base}/recheck",
        ("POST", "tasks/{}/{}/items"): f"/api/v1/tasks/{rig.pid}/{task}/items",
        ("POST", "tasks/{}/{}/queue"): f"/api/v1/tasks/{rig.pid}/{task}/queue",
        ("PATCH", "items/{}/{}/{}"): base,
        ("PUT", "items/{}/{}/{}/run-options"): f"{base}/run-options",
        ("DELETE", "queue/{}"): f"/api/v1/queue/{run_id}",
        ("POST", "projects/{}/tasks"): f"/api/v1/projects/{rig.pid}/tasks",
        ("POST", "runs/{}/cancel"): f"/api/v1/runs/{run_id}/cancel",
        ("POST", "runs/{}/retry"): f"/api/v1/runs/{run_id}/retry",
        ("POST", "holds"): "/api/v1/holds",
        ("POST", "sessions/dismiss"): "/api/v1/sessions/dismiss",
        ("POST", "shutdown"): "/api/v1/shutdown",
        ("POST", f"{endpoint_helper.ROUTE}/{{}}"): f"/api/v1/{endpoint_helper.ROUTE}/some-helper",
    }


def test_the_sweep_covers_exactly_the_mutation_route_table(rig):
    """The builder map is the table: a route without a builder, or a builder without a route, fails.

    Red: drop an entry from MUTATION_ROUTES (the keys differ); an empty table fails the non-empty check.
    """
    run_id = rig.queue("do-it")
    assert server.MUTATION_ROUTES, "an empty table would make every sweep vacuous"
    assert set(concrete_paths(rig, run_id)) == set(server.MUTATION_ROUTES)


def test_every_mutation_route_refuses_each_gate_before_acting(rig):
    """Red: delete ``if self._read_json_body() is None: return`` from the cancel branch; a cancel without Origin
    is then accepted and this fails."""
    run_id = rig.queue("do-it")
    port = rig.srv.port
    origin = {"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"}
    folder_before = tree_digest(rig.folder)
    state_before = rig.db.get(run_id).state
    helper = ("POST", f"{endpoint_helper.ROUTE}/{{}}")
    for (method, shape), path in concrete_paths(rig, run_id).items():
        who = f"{method} {shape}"
        body = b"{}"
        if (method, shape) == helper:
            token = {endpoint_helper.RUN_TOKEN_HEADER: "run-token"}
            assert rig.srv.request(method, path, token=False, body=body, headers=origin | token)[0] == 403, who  # Origin
            assert rig.srv.request(method, path, token=False, body=body,
                                   headers={"Content-Type": "application/json"})[0] == 401, who  # no run token
            status = rig.srv.request(method, path, token=False, headers=token | {
                "Content-Type": "application/json", "Content-Length": str(endpoint_helper.MAX_BODY_BYTES + 1)})[0]
            assert status == 413, who
            continue
        assert rig.srv.request(method, path, token=False, body=body, headers=origin)[0] == 401, who
        status, _h, data = rig.srv.request(method, path, body=body, headers={"Content-Type": "application/json"})
        assert (status, json.loads(data)["error"]["code"]) == (403, "bad_origin"), (who, status, data)
        foreign = {"Origin": "http://evil.example", "Content-Type": "application/json"}
        assert rig.srv.request(method, path, body=body, headers=foreign)[0] == 403, who
        assert rig.srv.request(method, path, body=body, headers=origin | {"Content-Type": "text/plain"})[0] == 415, who
        status = rig.srv.request(method, path, headers=origin | {"Content-Length": str(server.MAX_BODY_BYTES + 1)})[0]
        assert status == 413, who
        assert rig.srv.request(method, path, body=body, headers=origin, host="evil.example")[0] == 403, who
    assert tree_digest(rig.folder) == folder_before
    assert rig.db.get(run_id).state == state_before == runtime.QUEUED


def test_a_mutation_the_table_does_not_list_is_refused_even_when_fully_valid(rig, monkeypatch):
    """The property the table carries: a handler whose route is unlisted cannot be served.

    The sweep alone moves with the table, so this pins the ENFORCEMENT. Red: delete the enforcement line in
    ``_dispatch``; the cancel then goes through.
    """
    run_id = rig.queue("do-it")
    monkeypatch.setattr(server, "MUTATION_ROUTES",
                        frozenset(r for r in server.MUTATION_ROUTES if r != ("POST", "runs/{}/cancel")))
    status, _h, body = rig.cancel(run_id)
    assert status == 405 and body["error"]["code"] == "method_not_allowed", (status, body)
    assert rig.db.get(run_id).state == runtime.QUEUED


def test_an_unknown_mutation_path_is_refused_with_405_after_the_token_check(rig):
    assert rig.srv.send("POST", "/api/v1/not-a-route")[0] == 405
    assert rig.srv.request("POST", "/api/v1/not-a-route", token=False)[0] == 401


def _enc_call(text: str, at: int) -> int:
    """The index just past a balanced ``enc( ... )`` starting at ``at``, skipping quoted strings."""
    depth, i = 0, at + len("enc")
    while i < len(text):
        char = text[i]
        if char == "'":
            i = text.index("'", i + 1)
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise AssertionError("unbalanced enc(")


def app_js_producers() -> tuple[set, int, int]:
    """(method, shape) of every ``send('METHOD', '/api/v1/...'`` call in app.js, the number of calls parsed, and the
    number of send( call sites (a call written in a form the parser misses makes the two differ)."""
    text = (DASHBOARD_DIR / "static" / "app.js").read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.strip().startswith(("*", "//", "/*")))
    sites = len(re.findall(r"\bsend\(", code.replace("function send(", "function SEND(")))
    found = set()
    parsed = 0
    for match in re.finditer(r"\bsend\('([A-Z]+)', ", code):
        i, shape = match.end(), ""
        while True:
            if code.startswith("'", i):
                end = code.index("'", i + 1)
                shape += code[i + 1:end]
                i = end + 1
            elif code.startswith("enc(", i):
                shape += "{}"
                i = _enc_call(code, i)
            else:
                raise AssertionError(f"cannot parse the path of a send( call near {code[match.start():match.start() + 80]!r}")
            rest = code[i:].lstrip()
            if not rest.startswith("+"):
                break
            i = len(code) - len(rest) + 1
            while code[i] == " ":
                i += 1
        assert shape.startswith("/api/v1/"), shape
        found.add((match.group(1), shape[len("/api/v1/"):]))
        parsed += 1
    return found, parsed, sites


def test_every_route_a_client_sends_to_is_in_the_mutation_table():
    """Producers are a subset of the table, and the extraction cannot miss a call written in another form.

    Red: add ``send('POST', '/api/v1/x')`` to app.js (not in the table); or rewrite one send( call with double
    quotes (the count check fails).
    """
    found, parsed, sites = app_js_producers()
    assert found and parsed == sites, (parsed, sites)  # every call site was parsed, none skipped
    literal = {
        "handshake.py": r'request\("POST", "/api/v1/([^"]*)"',
        "launcher.py": r'"POST",\s*"/api/v1/([^"]*)"',
        "helper_cli.py": r'request\("POST", f"/api/v1/([^"]*)"',
    }
    for name, pattern in literal.items():
        text = (DASHBOARD_DIR / name).read_text(encoding="utf-8")
        shapes = re.findall(pattern, text)
        assert shapes, f"no write call found in {name}"
        found |= {("POST", re.sub(r"\{[^}]*\}", "{}", shape)) for shape in shapes}
    missing = found - set(server.MUTATION_ROUTES)
    assert not missing, f"clients send to routes the server's table does not list: {sorted(missing)}"


def test_the_handler_ships_with_a_finite_idle_timeout():
    """The test below shortens the timeout to keep the run fast, so the shipped value is pinned on its own.

    Red: set ``_Handler.timeout`` to None.
    """
    assert server.IDLE_TIMEOUT_S == 30.0
    assert server._Handler.timeout == server.IDLE_TIMEOUT_S


def test_an_idle_connection_is_closed_by_the_server(tmp_path, world, monkeypatch):
    """A client that connects and sends nothing cannot hold a handler thread forever.

    Red: make the handler ignore its timeout (``setup`` without ``settimeout``); ``recv`` then hits the 10 s cap
    and this fails.
    """
    monkeypatch.setattr(server._Handler, "timeout", 1.0)
    running = Running(tmp_path / "data", (world,))
    try:
        with socket.create_connection(("127.0.0.1", running.port), timeout=10) as client:
            started = time.monotonic()
            assert client.recv(1) == b""
            assert time.monotonic() - started < 9
    finally:
        running.stop()
