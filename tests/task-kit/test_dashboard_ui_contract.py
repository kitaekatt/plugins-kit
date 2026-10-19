"""The dashboard UI contract: what the static page may contain, and what it draws.

Static checks pin the CSP-compatible shape of the shipped assets (no inline
script or style, nothing remote, only same-origin API reads). The rendering
checks run the real app.js under Node against a fake DOM fed by the real API
payload builders, and are skipped when Node is not installed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from dashboard_support import DASHBOARD_DIR, make_task

from task_system.dashboard import snapshot
from task_system.dashboard.inventory import Inventory

STATIC = DASHBOARD_DIR / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "app.css").read_text(encoding="utf-8")
HARNESS = Path(__file__).with_name("ui_harness.js")

# The actions that change shared state. Each is drawn in every place the design shows
# it; the three the server backs are live only when it says so, the rest stay disabled.
LATER_ACTIONS = {"queue", "cancel", "retry", "newtask", "additem", "pause", "recheck", "defer", "queueall"}
BACKED_ACTIONS = {"queue", "retry", "recheck"}


@pytest.mark.parametrize("name", ["index.html", "app.js", "app.css"])
def test_static_assets_are_ascii(name):
    (STATIC / name).read_text(encoding="utf-8").encode("ascii")


def test_index_has_no_inline_script_style_handlers_or_remote_references():
    assert re.findall(r"<script\b[^>]*>(.*?)</script>", HTML, re.S) == [""]
    assert re.findall(r"<script\b[^>]*\bsrc=\"([^\"]+)\"", HTML) == ["/static/app.js"]
    assert re.findall(r"<link\b[^>]*\bhref=\"([^\"]+)\"", HTML) == ["/static/app.css"]
    assert "<style" not in HTML.lower()
    assert not re.search(r"\sstyle\s*=", HTML)
    assert not re.search(r"\son[a-z]+\s*=", HTML)
    assert not re.search(r"(https?:)?//", re.sub(r"<!doctype[^>]*>", "", HTML, flags=re.I))


def test_css_pulls_in_nothing_remote():
    assert "@import" not in CSS
    assert not re.search(r"url\(\s*['\"]?(https?:)?//", CSS)


def test_app_js_makes_only_same_origin_calls_and_writes_only_the_three_backed_actions():
    assert not re.search(r"\b(eval|XMLHttpRequest|WebSocket|EventSource|sendBeacon|localStorage|sessionStorage)\b", JS)
    assert "new Function" not in JS and "document.write" not in JS
    assert not re.search(r"https?://", JS)
    fetches = re.findall(r"fetch\(([^,)]+)", JS)
    assert fetches == ["path", "path"]  # api() and send()
    assert re.findall(r"api\(\s*'(/api/v1/[^']*)'", JS)
    assert "X-Task-Kit-Token" in JS and "location.hash" in JS
    # The one POST lives in send(); the only things sent are queue, recheck and retry.
    assert re.findall(r"method\s*:\s*'(\w+)'", JS) == ["POST"]
    assert len(re.findall(r"send\('/api/v1/", JS)) == 3
    assert sorted(re.findall(r"'/(queue|recheck|retry)'", JS)) == ["queue", "recheck", "retry"]


def test_app_js_never_emits_markup_the_csp_would_block():
    assert not re.search(r"""style\s*=\s*\\?['"]""", JS)
    assert not re.search(r"\bon[a-z]+\s*=", JS)
    assert "setAttribute('style'" not in JS and "cssText" not in JS


def test_the_project_filter_is_a_dropdown_not_chips():
    assert '<select class="proj-select" id="projectSelect"' in HTML and "All projects" in HTML
    assert 'id="chips"' not in HTML and "fchip" in HTML  # the Needs-you toggle keeps the chip look


def test_the_prototype_scaffolding_is_gone():
    for text in (HTML, JS, CSS):
        assert "Prototype" not in text and "Mock data" not in text and "demo" not in text.lower()


def test_header_actions_that_change_state_are_present_and_disabled():
    for action in ("pause", "newtask"):
        button = re.search(rf"<button[^>]*data-later=\"{action}\"[^>]*>", HTML)
        assert button and " disabled" in button.group(0) and "aria-disabled=\"true\"" in button.group(0)


def test_every_later_action_the_script_can_draw_goes_through_the_disabled_helpers():
    ids = set(re.findall(r"later(?:Button|Pill)\(.*?'(\w+)'\)", JS))
    ids |= set(re.findall(r'data-later="(\w+)"', JS))
    ids |= set(re.findall(r"run(?:Button|Pill)\(.*?'(queue|retry|recheck)'", JS))
    assert LATER_ACTIONS - {"pause", "newtask"} <= ids
    for helper in ("laterButton", "laterPill", "runButton", "runPill"):
        body = re.search(rf"function {helper}\(.*?\n}}", JS, re.S).group(0)
        assert "disabled" in body and 'aria-disabled="true"' in body and "data-later" in body
    # The two helpers that can draw a live control both gate it on the server's say-so.
    for helper in ("runButton", "runPill"):
        body = re.search(rf"function {helper}\(.*?\n}}", JS, re.S).group(0)
        assert "canDo(action)" in body and "runCap().available" in body
    # Exactly the backed actions have handlers; a click on any other drawn control does nothing.
    table = JS.split("var ACTIONS = {")[1].split("\n};")[0]
    handlers = set(re.findall(r"^\s{2}(\w+):", table, re.M))
    assert handlers & LATER_ACTIONS == BACKED_ACTIONS


# ---------------------------------------------------------------------------
# rendering, under Node
# ---------------------------------------------------------------------------

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _world(tmp):
    devroot = tmp / "devroot"
    project = devroot / "alpha"
    project.mkdir(parents=True)
    make_task(project, "dev/tasks", "open-one", title="Open <one>", summary="Does the open thing.",
              items=[{"id": "go", "title": "Go & do", "state": "available", "priority": "P1"},
                     {"id": "go-too", "title": "Go too", "state": "available"},
                     {"id": "go-three", "title": "Go three", "state": "available"},
                     {"id": "wait", "title": "Wait", "state": "blocked-user", "note": "pick <one> or two"},
                     {"id": "mid", "title": "Mid", "state": "in-flight"},
                     {"id": "later", "title": "Later", "state": "deferred"}],
              sections={"go": "Do the thing."}, log="- 2026-10-01 09:00: began\n")
    make_task(project, "tmp", "stuck", title="Stuck", status="blocked", blocked_by=["dev/tasks/open-one"])
    make_task(project, "dev/tasks", "old", title="Old", status="closed")
    make_task(project, "tmp", "parked-task", title="Parked", status="deferred")
    return devroot


def _run_harness(tmp, payload, snap, extra=()):
    task = snap.find_task(payload["projects"][0]["id"], "dev/tasks/open-one")
    detail = snapshot.item_detail(task, "wait")
    (tmp / "payload.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp / "detail.json").write_text(json.dumps(detail), encoding="utf-8")
    pid = payload["projects"][0]["id"]
    result = subprocess.run(
        [NODE, str(HARNESS), str(STATIC / "app.js"), str(tmp / "payload.json"), str(tmp / "detail.json"),
         f"{pid}|dev/tasks/open-one|wait", f"{pid}|dev/tasks/open-one", *extra],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ui")
    devroot = _world(tmp)
    snap = Inventory(parents=(devroot,), min_interval=0).snapshot()
    return _run_harness(tmp, snapshot.dashboard_payload(snap), snap)


LIVE_CAPABILITIES = {
    "read": True,
    "run": {"available": True, "reason": None, "state": "ok"},
    "mutations": {"available": True, "actions": ["queue", "retry", "recheck"], "reason": "live"},
}


def _run_row(seq, run_id, project_id, state, item, **extra):
    from task_system.dashboard.runtime import Run

    return Run(seq=seq, id=run_id, project_id=project_id, project_root="x", task_ref="dev/tasks/open-one", item_id=item,
               item_title=f"Title of {item}", state=state, code=extra.get("code"), detail=extra.get("detail"),
               options={"worker": "claude", "entry": "opus"}, source_revision="r", queued_at="2026-10-01 09:00",
               started_at="2026-10-01 09:01" if state != "queued" else None,
               ended_at="2026-10-01 09:30" if state in ("failed", "accepted") else None,
               run_dir=None, runner_pid=None, runner_ctime=None, retry_of=None)


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """The same world with llm-kit available, one run in flight and one failed run."""
    tmp = tmp_path_factory.mktemp("ui-live")
    devroot = _world(tmp)
    snap = Inventory(parents=(devroot,), min_interval=0).snapshot()
    pid = snap.projects[0].id
    runs = [_run_row(2, "run-live", pid, "running", "go-too"),
            _run_row(1, "run-fail", pid, "failed", "go", code="timeout", detail="reached its limit")]
    payload = snapshot.dashboard_payload(snap, runtime={"capabilities": LIVE_CAPABILITIES, "runs": runs})
    report = _run_harness(tmp, payload, snap, (f"{pid}|dev/tasks/open-one|go-three", f"{pid}|dev/tasks/open-one|wait",
                                               f"run-fail|{pid}|dev/tasks/open-one"))
    report["revision"] = next(t["revision"] for t in payload["tasks"] if t["id"] == "dev/tasks/open-one")
    report["pid"] = pid
    report["payload"] = payload
    return report


def _buttons(html: str, attr: str) -> list[str]:
    return re.findall(rf"<button\b[^>]*\b{attr}=\"[^\"]*\"[^>]*>", html)


@needs_node
def test_app_js_runs_and_sends_the_token_on_every_request(rendered):
    assert rendered["requests"], "no API request was made"
    for request in rendered["requests"]:
        assert request["url"].startswith("/api/v1/")
        assert request["headers"] == {"X-Task-Kit-Token": "test-token"}
    assert rendered["pollDelays"] and rendered["pollDelays"][0] == 2000


@needs_node
def test_main_view_shows_pipelines_needs_you_in_flight_and_activity(rendered):
    main = rendered["main"]
    for text in ("In flight", "Needs you", "Pipelines", "Recent activity", "Open &lt;one&gt;", "Go &amp; do",
                 "Resolve in Claude Code", "needs you", "in flight", "Closed tasks", "Deferred tasks"):
        assert text in main, text
    assert "Open <one>" not in main and "pick <one>" not in main  # task text is escaped
    assert "alpha" in rendered["select"] and 'value=""' in rendered["select"]


@needs_node
def test_every_state_changing_action_is_drawn_and_disabled(rendered):
    """Queue, Cancel run, Retry, New task, Add item, ... are visible and cannot be clicked."""
    for surface in ("main", "itemPanel", "taskPanel"):
        for tag in _buttons(rendered[surface], "data-later"):
            assert " disabled" in tag and 'aria-disabled="true"' in tag, (surface, tag)
    drawn = {m for surface in ("main", "itemPanel") for m in re.findall(r'data-later="(\w+)"', rendered[surface])}
    assert {"queue", "cancel", "additem", "queueall", "recheck"} <= drawn
    assert "arrives in a later increment" in rendered["main"]
    # With llm-kit unavailable the autonomous controls say why and cannot be clicked.
    assert "Autonomous runs are unavailable" in rendered["banners"]
    assert 'data-action="queue"' not in rendered["main"] + rendered["itemPanel"]


@needs_node
def test_no_rendered_markup_carries_a_style_attribute_or_handler(rendered):
    for surface in ("main", "banners", "overview", "itemPanel", "itemResolve", "taskPanel", "taskResolve", "select"):
        html = rendered[surface]
        assert not re.search(r"\sstyle\s*=", html), surface
        assert not re.search(r"\son[a-z]+\s*=", html), surface


@needs_node
def test_resolve_in_claude_code_only_shows_commands_to_copy(rendered):
    assert "cmdbox" not in rendered["itemPanel"]
    box = rendered["itemResolve"]
    assert "cmdbox" in box and "/task work dev/tasks/open-one" in box and "claude" in box
    assert box.count('data-action="copy"') >= 3
    for surface in ("itemResolve", "taskResolve"):
        assert "data-action=\"queue\"" not in rendered[surface] and "POST" not in rendered[surface]
    assert "/task work dev/tasks/open-one" in rendered["taskResolve"]


@needs_node
def test_item_drawer_shows_state_note_plan_section_and_disabled_run_controls(rendered):
    panel = rendered["itemPanel"]
    assert "Waiting for you" in panel and "pick &lt;one&gt; or two" in panel
    assert "Run controls" in panel and "Model, effort and run options arrive in a later increment" in panel
    assert "plan.md section" in panel
    assert "No section for this item" in panel  # 'wait' has no section in the fixture
    assert "Open &lt;one&gt;" not in panel or "Open <one>" not in panel


@needs_node
def test_closing_the_drawer_returns_to_the_overview(rendered):
    assert "Projects" in rendered["afterClose"] and "How blocked work behaves" in rendered["afterClose"]


@needs_node
def test_with_runs_available_queue_retry_and_recheck_are_live_and_the_rest_stay_disabled(live):
    assert "Autonomous runs are on" in live["banners"]
    assert 'data-action="queue"' in live["main"] and 'data-action="recheck"' in live["main"]
    for surface in ("main", "itemPanel"):
        for tag in _buttons(live[surface], "data-later"):
            assert " disabled" in tag and 'aria-disabled="true"' in tag, (surface, tag)
    later = {m for surface in ("main", "itemPanel") for m in re.findall(r'data-later="(\w+)"', live[surface])}
    assert {"cancel", "additem", "queueall"} <= later and not later & {"queue", "recheck"}
    assert "Now running" in live["main"] and "Title of go-too" in live["main"]


@needs_node
def test_a_failed_run_offers_retry_and_a_running_one_shows_its_state(live):
    assert "running" in live["main"].lower()
    run = live["payload"]["runs"][0]
    assert run["id"] == "run-live" and run["state"] == "running" and not run["retryable"]
    failed = next(t for t in live["payload"]["tasks"] if t["id"] == "dev/tasks/open-one")["items"]
    go = next(i for i in failed if i["id"] == "go")
    assert go["run"]["state"] == "failed" and go["run"]["retryable"]


@needs_node
def test_the_three_actions_post_with_the_revision_the_page_saw(live):
    posts = live["posts"]
    assert [p["method"] for p in posts] == ["POST", "POST", "POST"]
    pid = live["pid"]
    prefix = f"/api/v1/items/{pid}/dev%2Ftasks%2Fopen-one"
    assert posts[0]["url"] == f"{prefix}/go-three/queue"
    assert posts[1]["url"] == f"{prefix}/wait/recheck"
    assert posts[2]["url"] == "/api/v1/runs/run-fail/retry"
    for post in posts:
        assert post["headers"] == {"X-Task-Kit-Token": "test-token", "Content-Type": "application/json"}
        assert json.loads(post["body"]) == {"expected_revision": live["revision"]}


@needs_node
def test_the_run_panel_shows_the_log_tail_and_its_own_controls(live):
    panel = live["runPanel"]
    assert "Run log" in panel and "log line" in panel and "run-fail" in panel
    assert 'data-action="retry"' in panel  # a failed run is retryable from its own panel
