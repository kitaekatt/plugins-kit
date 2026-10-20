"""The dashboard HTTP server: security gates, API shape, lifecycle.

Each server runs in-process on a thread, bound to 127.0.0.1 port 0, over a
fixture DEVROOT under tmp_path. Raw ``http.client`` requests let a test set an
arbitrary Host or Origin header.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest
from dashboard_support import Running, make_task

from task_system.dashboard import snapshot
from task_system.dashboard import state as dstate
from task_system.dashboard.server import (
    EXIT_LOCK_HELD,
    MAX_BODY_BYTES,
    CSP,
    DashboardServer,
)
from task_system.dashboard.inventory import Inventory
from dashboard_support import SCRIPTS_DIR

SECTION = "Do the thing.\n\nDone when the file exists."


@pytest.fixture
def world(tmp_path):
    devroot = tmp_path / "devroot"
    (devroot / "alpha").mkdir(parents=True)
    (devroot / "beta").mkdir()
    make_task(
        devroot / "alpha", "dev/tasks", "open-one", title="Open one", summary="Does the open thing.",
        items=[
            {"id": "has-section", "title": "Has section", "state": "available", "priority": "P1"},
            {"id": "no-section", "title": "No section", "state": "available"},
            {"id": "needs-me", "title": "Needs me", "state": "blocked-user", "note": "pick one"},
        ],
        sections={"has-section": SECTION},
        log="- 2026-10-01 09:00: first entry\n- 2026-10-02 10:30: second entry\n",
    )
    make_task(devroot / "beta", "dev/tasks", "other", title="Other", items=[
        {"id": "solo", "title": "Solo", "state": "in-flight", "note": "in the harness"}])
    return devroot


@pytest.fixture
def srv(tmp_path, world):
    running = Running(tmp_path / "data", (world,))
    try:
        yield running
    finally:
        running.stop()


def project_id(srv, name):
    status, _h, body = srv.json("GET", "/api/v1/dashboard")
    assert status == 200
    return next(p["id"] for p in body["projects"] if p["name"] == name)


def checkout_id(srv, name, task_id):
    """The id item routes use: the checkout the task is read from, not the project."""
    _s, _h, body = srv.json("GET", "/api/v1/dashboard")
    mine = [t for t in body["tasks"] if t["repo"] == name]
    # An id no task has still needs a real checkout, so the 404 under test is the task's.
    return next((t["checkout"] for t in mine if t["id"] == task_id), mine[0]["checkout"])


# -- health, token, host ----------------------------------------------------


def test_health_is_unauthenticated_and_reveals_only_four_facts(srv):
    status, _headers, body = srv.json("GET", "/api/v1/health", token=False)
    assert status == 200
    assert set(body) == {"schema", "instance", "code_hash", "port", "busy"}
    assert body["instance"] == srv.state.nonce and body["port"] == srv.port and body["busy"] == 0
    assert srv.token not in json.dumps(body)


@pytest.mark.parametrize("token", [False, "wrong-token", ""])
def test_api_needs_the_token(srv, token):
    status, _h, body = srv.json("GET", "/api/v1/dashboard", token=token)
    assert status == 401 and body["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("host", ["evil.example", "evil.example:1", "127.0.0.1", "127.0.0.1:1",
                                  "0.0.0.0:{port}", "[::1]:{port}", ""])
def test_non_loopback_host_is_refused_on_every_route(srv, host):
    """RED CHECK: a DNS-rebinding style Host never reaches a route."""
    host = host.format(port=srv.port)
    for path in ("/", "/static/app.js", "/api/v1/health", "/api/v1/dashboard"):
        status, _h, data = srv.request("GET", path, host=host)
        assert status == 403, (host, path)
        assert json.loads(data)["error"]["code"] == "forbidden_host"


def test_localhost_host_is_accepted(srv):
    status, _h, _b = srv.request("GET", "/api/v1/health", host=f"localhost:{srv.port}")
    assert status == 200


def test_no_cors_header_is_ever_emitted_and_options_is_refused(srv):
    for method, path, kw in (
        ("GET", "/api/v1/health", {}),
        ("GET", "/", {}),
        ("GET", "/api/v1/dashboard", {"headers": {"Origin": "https://evil.example"}}),
        ("OPTIONS", "/api/v1/dashboard", {"headers": {"Origin": "https://evil.example",
                                                      "Access-Control-Request-Method": "POST"}}),
    ):
        status, headers, _b = srv.request(method, path, **kw)
        assert not [h for h in headers if h.startswith("access-control-")], (method, path)
    status, _h, _b = srv.request("OPTIONS", "/api/v1/dashboard")
    assert status >= 400


@pytest.mark.parametrize("path,kw,status", [
    ("/", {}, 200), ("/static/app.js", {}, 200), ("/api/v1/health", {"token": False}, 200),
    ("/api/v1/dashboard", {}, 200), ("/api/v1/dashboard", {"token": False}, 401),
    ("/nope", {}, 404), ("/api/v1/nope", {}, 404),
])
def test_every_response_is_no_store_with_a_restrictive_csp(srv, path, kw, status):
    got, headers, _b = srv.request("GET", path, **kw)
    assert got == status
    assert headers["cache-control"] == "no-store"
    assert headers["content-security-policy"] == CSP
    assert "script-src 'self'" in CSP and "style-src 'self'" in CSP and "object-src 'none'" in CSP
    assert "frame-ancestors 'none'" in CSP and "unsafe-inline" not in CSP and "http" not in CSP
    assert headers["x-content-type-options"] == "nosniff"


# -- mutation gates (shutdown is the one mutation in this increment) ---------


def _post(srv, **overrides):
    headers = {"Origin": f"http://127.0.0.1:{srv.port}", "Content-Type": "application/json"}
    body = overrides.pop("body", b"{}")
    headers.update(overrides.pop("headers", {}))
    headers = {k: v for k, v in headers.items() if v is not None}
    return srv.json("POST", "/api/v1/shutdown", headers=headers, body=body, **overrides)


@pytest.mark.parametrize("headers,status,code", [
    ({"Origin": None}, 403, "bad_origin"),
    ({"Origin": "https://evil.example"}, 403, "bad_origin"),
    ({"Origin": "http://127.0.0.1:1"}, 403, "bad_origin"),
    ({"Content-Type": "text/plain"}, 415, "bad_content_type"),
    ({"Content-Type": None}, 415, "bad_content_type"),
])
def test_mutation_gates_refuse_before_acting(srv, headers, status, code):
    got, _h, body = _post(srv, headers=headers)
    assert (got, body["error"]["code"]) == (status, code)
    assert srv.thread.is_alive()


def test_mutation_body_gates(srv):
    got, _h, body = _post(srv, body=b"x" * (MAX_BODY_BYTES + 1))
    assert (got, body["error"]["code"]) == (413, "body_too_large")
    got, _h, body = _post(srv, body=b"not json")
    assert (got, body["error"]["code"]) == (400, "bad_json")
    got, _h, body = _post(srv, body=b"[1]")
    assert (got, body["error"]["code"]) == (400, "bad_json")
    got, _h, body = _post(srv, token=False)
    assert got == 401
    assert srv.thread.is_alive()


def test_shutdown_needs_the_token_and_then_stops_the_server(srv):
    got, _h, body = _post(srv)
    assert (got, body) == (200, {"ok": True})
    srv.thread.join(timeout=10)
    assert not srv.thread.is_alive()
    assert dstate.read_state(srv.server.root) is None  # ownership record removed on exit


def test_shutdown_is_refused_while_jobs_run_unless_forced(tmp_path, world):
    running = Running(tmp_path / "data", (world,), busy=lambda: 2)
    try:
        got, _h, body = _post(running)
        assert (got, body["error"]["code"]) == (409, "busy")
        assert running.thread.is_alive()
        got, _h, _b = _post(running, body=b'{"force": true}')
        assert got == 200
        running.thread.join(timeout=10)
        assert not running.thread.is_alive()
    finally:
        running.stop()


def test_reads_are_not_mutations_and_wrong_verbs_are_refused(srv):
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        status, _h, _b = srv.request(method, "/api/v1/dashboard", headers={"Content-Length": "0"})
        assert status == 405, method


# -- static ------------------------------------------------------------------


def test_static_assets_are_served_from_memory_with_types(srv):
    status, headers, body = srv.request("GET", "/", token=False)
    assert status == 200 and headers["content-type"].startswith("text/html") and b"/static/app.js" in body
    status, headers, _b = srv.request("GET", "/static/app.js", token=False)
    assert status == 200 and headers["content-type"].startswith("text/javascript")
    status, headers, _b = srv.request("GET", "/static/app.css", token=False)
    assert status == 200 and headers["content-type"].startswith("text/css")
    status, _h, _b = srv.request("GET", "/static/../codehash.py", token=False)
    assert status == 404
    status, _h, _b = srv.request("GET", "/static/missing.js", token=False)
    assert status == 404


# -- API ----------------------------------------------------------------------


def test_dashboard_payload_shape_and_filters(srv):
    status, _h, body = srv.json("GET", "/api/v1/dashboard")
    assert status == 200 and body["schema"] == 1
    assert body["capabilities"]["run"]["available"] is False
    assert body["capabilities"]["run"]["state"] == "absent"  # llm-kit is not on the task-kit test path
    assert body["capabilities"]["mutations"]["actions"] == ["queue", "retry", "recheck", "cancel"]
    assert {p["name"] for p in body["projects"]} == {"alpha", "beta"}
    assert {t["id"] for t in body["tasks"]} == {"dev/tasks/open-one", "dev/tasks/other"}
    assert [n["kind"] for n in body["needs"]] == ["item-blocked"]
    assert body["needs"][0]["detail"] == "pick one"
    assert [x["item"] for x in body["in_flight"]] == ["solo"]
    assert [a["detail"] for a in body["activity"]][:2] == ["second entry", "first entry"]
    assert body["totals"] == {"projects": 2, "tasks": 2, "needs": 1}

    alpha = project_id(srv, "alpha")
    _s, _h, only_alpha = srv.json("GET", f"/api/v1/dashboard?project={alpha}")
    assert [t["id"] for t in only_alpha["tasks"]] == ["dev/tasks/open-one"]
    assert only_alpha["totals"]["projects"] == 2  # the project list itself is unfiltered
    _s, _h, only_needs = srv.json("GET", "/api/v1/dashboard?needs=1")
    assert [t["id"] for t in only_needs["tasks"]] == ["dev/tasks/open-one"]
    _s, _h, found = srv.json("GET", "/api/v1/dashboard?q=" + urllib.parse.quote("solo"))
    assert [t["id"] for t in found["tasks"]] == ["dev/tasks/other"]
    _s, _h, none = srv.json("GET", "/api/v1/dashboard?q=zzz-nothing")
    assert none["tasks"] == []


def test_payload_carries_repo_identity_working_root_and_per_task_needs(srv, world):
    _s, _h, body = srv.json("GET", "/api/v1/dashboard")
    task = next(t for t in body["tasks"] if t["id"] == "dev/tasks/open-one")
    assert task["repo"] == "alpha" and task["stub"] == "open-one" and task["id"] == "dev/tasks/open-one"
    assert task["project"] == project_id(srv, "alpha")
    assert task["checkout"] == checkout_id(srv, "alpha", "dev/tasks/open-one") != task["project"]
    assert task["key"] == f"{task['checkout']}|dev/tasks/open-one"
    assert task["working_root"] == str(world / "alpha")  # outside the home directory: shown as is
    assert [n["item"] for n in task["needs"]] == ["needs-me"] and task["needs"][0]["checkout"] == task["checkout"]
    project = next(p for p in body["projects"] if p["name"] == "alpha")
    assert project["checkouts"] == [str(world / "alpha")] and "root" not in project


def test_display_path_is_home_relative_only_under_the_home_directory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert snapshot.display_path(str(home / "Dev" / "plugins-kit3")) == "~/Dev/plugins-kit3"
    assert snapshot.display_path(str(home)) == "~"
    assert snapshot.display_path(str(tmp_path / "home2" / "x")) == str(tmp_path / "home2" / "x")
    assert snapshot.display_path(str(tmp_path / "elsewhere")) == str(tmp_path / "elsewhere")


def test_revision_is_stable_until_a_task_changes(srv, world):
    _s, _h, first = srv.json("GET", "/api/v1/dashboard")
    _s, _h, second = srv.json("GET", "/api/v1/dashboard")
    assert first["revision"] == second["revision"]
    log = world / "alpha" / "dev" / "tasks" / "open-one" / "log.md"
    log.write_text(log.read_text(encoding="utf-8") + "- 2026-10-03 08:00: third entry\n", encoding="utf-8")
    _s, _h, third = srv.json("GET", "/api/v1/dashboard")
    assert third["revision"] != first["revision"]
    assert third["activity"][0]["detail"] == "third entry"


def test_refresh_param_revalidates_every_folder(srv):
    srv.json("GET", "/api/v1/dashboard")
    loads = srv.inventory.loads
    srv.json("GET", "/api/v1/dashboard")
    assert srv.inventory.loads == loads
    srv.json("GET", "/api/v1/dashboard?refresh=1")
    assert srv.inventory.loads == loads + 2


def _item_path(srv, project, task, item, suffix=""):
    cid = checkout_id(srv, project, task)
    return f"/api/v1/items/{cid}/{urllib.parse.quote(task, safe='')}/{item}{suffix}"


def test_item_detail_returns_the_plan_section_and_handoff(srv, world):
    status, _h, body = srv.json("GET", _item_path(srv, "alpha", "dev/tasks/open-one", "has-section"))
    assert status == 200
    assert body["item"]["id"] == "has-section" and body["task"]["summary"] == "Does the open thing."
    assert body["plan_section"]["status"] == "found"
    assert body["plan_section"]["body"] == SECTION
    root = str(world / "alpha")
    assert [c["command"] for c in body["handoff"]["commands"]] == [
        f'cd "{root}" && claude "/task-kit:task work dev/tasks/open-one"', "/task-kit:task work dev/tasks/open-one"]
    _s, _h, missing = srv.json("GET", _item_path(srv, "alpha", "dev/tasks/open-one", "no-section"))
    assert missing["plan_section"]["status"] == "missing"


def test_handoff_endpoint_and_unknown_items(srv):
    status, _h, body = srv.json("GET", _item_path(srv, "alpha", "dev/tasks/open-one", "needs-me", "/handoff"))
    assert status == 200 and len(body["commands"]) == 2 and body["commands"][1]["command"].startswith("/task-kit:task work ")
    for path in (_item_path(srv, "alpha", "dev/tasks/open-one", "nope"),
                 _item_path(srv, "alpha", "dev/tasks/missing", "needs-me"),
                 "/api/v1/items/0000/x/y"):
        status, _h, body = srv.json("GET", path)
        assert status == 404, path


# -- logging --------------------------------------------------------------------


def test_logs_carry_no_token_no_query_string_and_no_headers(srv):
    srv.request("GET", "/api/v1/dashboard?q=secret-search-text", token="not-the-token-but-secret-ish")
    srv.request("GET", "/api/v1/dashboard?q=secret-search-text")
    log = srv.log.getvalue()
    assert srv.token not in log
    assert "secret-search-text" not in log and "secret-ish" not in log
    assert "GET /api/v1/dashboard -> 401" in log and "GET /api/v1/dashboard -> 200" in log


def test_an_internal_error_is_a_500_with_a_traceback_in_the_log(srv, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("inventory exploded")

    monkeypatch.setattr(srv.inventory, "snapshot", boom)
    status, _h, body = srv.json("GET", "/api/v1/dashboard")
    assert status == 500 and "inventory exploded" in body["error"]["message"]
    assert "Traceback" in srv.log.getvalue()
    status, _h, _b = srv.request("GET", "/api/v1/health", token=False)
    assert status == 200  # the server survived


# -- ownership record and lock ----------------------------------------------------


def test_server_publishes_an_ownership_record_and_owns_the_lock(srv, tmp_path, world):
    state = dstate.read_state(srv.server.root)
    assert state == srv.state
    assert state.port == srv.port and state.nonce and state.token and state.code_hash
    assert state.url() == f"http://127.0.0.1:{srv.port}/#{state.token}"
    second = DashboardServer(data_dir=tmp_path / "data", scripts_dir=SCRIPTS_DIR,
                             inventory=Inventory(parents=(world,)))
    with pytest.raises(dstate.LockHeldError):
        second.start()
    assert dstate.read_state(srv.server.root) == state  # the loser did not touch the record


def test_run_exits_75_when_another_server_holds_the_lock(srv, tmp_path, monkeypatch):
    from task_system.dashboard import server as server_module

    monkeypatch.setattr(server_module, "Inventory", lambda: Inventory(parents=()))
    assert server_module.run(tmp_path / "data", SCRIPTS_DIR, log=srv.log) == EXIT_LOCK_HELD


def test_static_assets_survive_the_source_directory_disappearing(tmp_path, world):
    """Assets are read into memory at startup; a plugin update may delete the dir."""
    import shutil

    scripts = tmp_path / "scripts-copy"
    shutil.copytree(SCRIPTS_DIR, scripts, ignore=shutil.ignore_patterns("__pycache__"))
    from task_system.dashboard import server as server_module

    assets = server_module.load_static(scripts)
    shutil.rmtree(scripts)
    assert b"<!doctype html>" in assets["index.html"][0]
