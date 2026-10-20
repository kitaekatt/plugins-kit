"""The dashboard UI contract: what the static page may contain, and what it draws.

Static checks pin the CSP-compatible shape of the shipped assets (no inline
script or style, nothing remote, only same-origin API reads). The rendering
checks run the real app.js under Node against a fake DOM fed by the real API
payload builders, and are skipped when Node is not installed.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from dashboard_support import DASHBOARD_DIR, git, make_task

from task_system.dashboard import snapshot
from task_system.dashboard.inventory import Inventory, project_id_for

STATIC = DASHBOARD_DIR / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "app.css").read_text(encoding="utf-8")
HARNESS = Path(__file__).with_name("ui_harness.js")

# The actions that change shared state. Each is drawn in every place the design shows
# it; the ones the server backs are live only when it says so, the rest stay disabled.
LATER_ACTIONS = {"queue", "cancel", "retry", "newtask", "additem", "pause", "recheck", "defer", "undefer", "queueall", "remove"}
BACKED_ACTIONS = LATER_ACTIONS - {"pause"}  # Pause worker is the one action still not built


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


def test_app_js_makes_only_same_origin_calls_and_writes_only_the_backed_actions():
    assert not re.search(r"\b(eval|XMLHttpRequest|WebSocket|EventSource|sendBeacon|localStorage|sessionStorage)\b", JS)
    assert "new Function" not in JS and "document.write" not in JS
    assert not re.search(r"https?://", JS)
    fetches = re.findall(r"fetch\(([^,)]+)", JS)
    assert fetches == ["path", "path"]  # api() and send()
    assert re.findall(r"api\(\s*'(/api/v1/[^']*)'", JS)
    assert "X-Task-Kit-Token" in JS and "location.hash" in JS
    # The one fetch with a verb lives in send(method, ...); every call site names its verb as a literal.
    assert re.findall(r"method\s*:\s*(\w+)", JS) == ["method"]
    assert re.findall(r"send\((?!')(\w+)", JS) == ["method", "method"]  # its definition and the comment naming it: no call site hides its verb
    assert sorted(set(re.findall(r"send\('(\w+)'", JS))) == ["DELETE", "PATCH", "POST", "PUT"]
    assert len(re.findall(r"send\('\w+', '/api/v1/", JS)) == 10  # queue, recheck, retry, cancel, remove, queue all, defer, new task, add item, run options


def test_app_js_never_emits_markup_the_csp_would_block():
    assert not re.search(r"""style\s*=\s*\\?['"]""", JS)
    assert not re.search(r"\bon[a-z]+\s*=", JS)
    assert "setAttribute('style'" not in JS and "cssText" not in JS


def test_the_project_filter_is_a_dropdown_and_the_needs_filter_chip_is_replaced_by_the_sort():
    assert '<select class="proj-select" id="projectSelect"' in HTML and "All projects" in HTML
    assert 'id="chips"' not in HTML
    assert 'id="sortSeg"' in HTML and 'role="group"' in HTML  # one control, one active choice
    assert "needsBtn" not in HTML and "fchip" not in HTML and "needsfilter" not in JS and "needs=1" not in JS


def test_the_uncommitted_folder_warning_is_shown_as_information_not_as_a_finding():
    assert "Not a blocker" in JS and "f.info" in JS
    assert not re.search(r"concat\(f\.warnings, f\.item_errors, f\.info", JS)  # info is never counted as a finding


def test_there_is_no_side_panel_anywhere():
    for text in (HTML, JS, CSS):
        assert not re.search(r"\bid=\"(panel|scrim|layout)\"|<aside|has-panel|\.panel\b|\.scrim\b|slide-?over", text), text[:40]
    for name in ("renderPanel", "panelHtml", "panelItem", "panelTask", "panelRun", "panelOverview", "closepanel",
                 "selectrun", "panelOpen", "S.mode", "Escape", "'panel'", "'scrim'"):
        assert name not in JS, name
    assert "translateX(104%)" not in CSS  # the narrow-window slide-over is gone
    assert not re.search(r"\.layout\s*\{[^}]*grid-template-columns", CSS)  # one full-width column, always


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
    ids |= set(re.findall(r"run(?:Button|Pill)\(.*?'(queue|retry|recheck|queueall)'", JS))
    ids |= set(re.findall(r"authControl\(.*?'(defer|undefer)'", JS))
    assert LATER_ACTIONS - {"pause", "newtask", "remove"} <= ids
    assert "canDo('runoptions')" in JS  # the run option controls are drawn only when the server backs them
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


ORIGIN = "git@example.invalid:team/webapp.git"


def _world(tmp):
    """Two checkouts of one repository (webapp) with open, blocked, closed and deferred tasks."""
    devroot = tmp / "devroot"
    project = devroot / "alpha"
    other_checkout = devroot / "alpha2"
    for root in (project, other_checkout):
        root.mkdir(parents=True)
        git(root, "init", "-q", "-b", "main")
        git(root, "remote", "add", "origin", ORIGIN)
    make_task(project, "dev/tasks", "open-one", title="Open <one>", summary="Does the open thing.",
              items=[{"id": "go", "title": "Go & do", "state": "available", "priority": "P1"},
                     {"id": "go-too", "title": "Go too", "state": "available"},
                     {"id": "go-three", "title": "Go three", "state": "available"},
                     {"id": "wait", "title": "Wait", "state": "blocked-user", "note": "pick <one> or two"},
                     {"id": "mid", "title": "Mid", "state": "in-flight"},
                     {"id": "later", "title": "Later", "state": "deferred"}],
              sections={"go": "Do the thing."}, log="- 2026-10-01 09:00: began\n")
    make_task(project, "dev/tasks", "calm-p1", title="Calm", priority="P1",
              items=[{"id": "c1", "title": "Calm item", "state": "available"}], log="- 2026-09-01 09:00: calm\n")
    make_task(other_checkout, "dev/tasks", "busy-p3", title="Busy", priority="P3",
              items=[{"id": "b1", "title": "Busy item", "state": "in-flight"}], log="- 2026-10-05 09:00: busy\n")
    make_task(project, "dev/tasks", "stuck", title="Stuck", status="blocked", blocked_by=["dev/tasks/open-one"])
    make_task(project, "dev/tasks", "old", title="Old", status="closed")
    make_task(project, "dev/tasks", "parked-task", title="Parked", status="deferred")
    make_task(project, "tmp", "tmp-only", title="Scratch")  # the dashboard does not list tmp/ tasks
    return devroot


def _checkout(snap, stub):
    return next(t.checkout_id for p in snap.projects for t in p.tasks if t.id == f"dev/tasks/{stub}")


def _run_harness(tmp, payload, snap, extra=()):
    cid = _checkout(snap, "open-one")
    task = snap.find_task(cid, "dev/tasks/open-one")
    detail = snapshot.item_detail(task, "wait")
    (tmp / "payload.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp / "detail.json").write_text(json.dumps(detail), encoding="utf-8")
    result = subprocess.run(
        [NODE, str(HARNESS), str(STATIC / "app.js"), str(tmp / "payload.json"), str(tmp / "detail.json"),
         f"{cid}|dev/tasks/open-one|wait", f"{cid}|dev/tasks/open-one", *extra],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ui")
    devroot = _world(tmp)
    snap = Inventory(parents=(devroot,), min_interval=0).snapshot()
    report = _run_harness(tmp, snapshot.dashboard_payload(snap), snap)
    report["root"] = str(devroot / "alpha")
    return report


LIVE_CAPABILITIES = {
    "read": True,
    "run": {"available": True, "reason": None, "state": "ok"},
    "mutations": {"available": True, "actions": ["queue", "retry", "recheck", "cancel"], "reason": "live"},
}


def _run_row(seq, run_id, project_id, state, item, task="open-one", **extra):
    from task_system.dashboard.runtime import Run

    ended = ("failed", "accepted", "cancelled", "timeout")

    return Run(seq=seq, id=run_id, project_id=project_id, project_root="x", task_ref=f"dev/tasks/{task}", item_id=item,
               item_title=f"Title of {item}", state=state, code=extra.get("code"), detail=extra.get("detail"),
               options={"worker": "claude", "entry": "opus"}, source_revision="r", queued_at="2026-10-01 09:00",
               started_at="2026-10-01 09:01" if state != "queued" else None,
               ended_at="2026-10-01 09:30" if state in ended else None,
               run_dir=None, runner_pid=None, runner_ctime=None, retry_of=None)


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """The same world with llm-kit available, one run in flight and one failed run."""
    tmp = tmp_path_factory.mktemp("ui-live")
    devroot = _world(tmp)
    snap = Inventory(parents=(devroot,), min_interval=0).snapshot()
    cid = _checkout(snap, "open-one")  # runs belong to the checkout they execute in
    runs = [_run_row(5, "run-cancelled", cid, "cancelled", "wait", code="cancelled", detail="Partial work may be in the shared tree."),
            _run_row(4, "run-stopping", cid, "cancelling", "mid", code="cancelling", detail="stop requested"),
            _run_row(3, "run-queued", cid, "queued", "c1", task="calm-p1"),
            _run_row(2, "run-live", cid, "running", "go-too"),
            _run_row(1, "run-fail", cid, "failed", "go", code="timeout", detail="reached its limit")]
    payload = snapshot.dashboard_payload(snap, runtime={"capabilities": LIVE_CAPABILITIES, "runs": runs})
    report = _run_harness(tmp, payload, snap, (f"{cid}|dev/tasks/open-one|go-three", f"{cid}|dev/tasks/open-one|wait",
                                               f"run-fail|{cid}|dev/tasks/open-one", "run-live"))
    report["revision"] = next(t["revision"] for t in payload["tasks"] if t["id"] == "dev/tasks/open-one")
    report["pid"] = cid
    report["root"] = str(devroot / "alpha")
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


def _rows(html: str) -> dict[str, str]:
    """task stub -> that row's markup, for every row rendered in `html`."""
    out = {}
    for chunk in html.split('<div class="trow')[1:]:
        key = re.search(r'data-row="([^"]+)"', chunk).group(1)
        out[key.split("|", 1)[1].split("/")[-1]] = '<div class="trow' + chunk.split('<details class="sec"')[0]
    return out


def _text(html: str) -> str:
    """What a reader sees: the markup with every tag removed."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _open_list_order(html: str) -> list[str]:
    """Stubs of the open-task list, in display order (before the deferred/closed folds)."""
    start = html.index('data-open="open"')
    ends = [html.find(f'data-open="{k}"', start) for k in ("deferred", "closed", "other", "activity")]
    head = html[start:min(e for e in ends if e >= 0)]
    return [k.split("|", 1)[1].split("/")[-1] for k in re.findall(r'data-row="([^"]+)"', head)]


@needs_node
def test_one_flat_list_replaces_the_needs_you_and_in_flight_sections(rendered):
    main = rendered["main"]
    for text in ("Open tasks", "Recent activity", "Open &lt;one&gt;", "Closed tasks", "Deferred tasks"):
        assert text in main, text
    assert "Open <one>" not in main and "pick <one>" not in main  # task text is escaped
    for gone in ('sec-title">Needs you', 'sec-title">In flight', "Pipelines", "need-list", "project-card"):
        assert gone not in main, gone
    # One list holds the open tasks of every project and checkout, collapsed to one line each.
    assert _open_list_order(main).count("open-one") == 1 and len(_open_list_order(main)) == 4
    assert 'class="tbody"' not in main and 'aria-expanded="true"' not in main  # nothing is expanded until clicked
    # tmp/ tasks are not listed at all.
    assert "tmp-only" not in main and "Scratch" not in main
    # One project per repository: both checkouts are "webapp".
    assert "webapp" in rendered["select"] and "alpha2" in rendered["select"] and 'value=""' in rendered["select"]
    assert rendered["select"].count("<option") == 2  # All projects + webapp


@needs_node
def test_a_row_is_one_line_with_priority_title_identity_count_and_age(rendered):
    row = _rows(rendered["main"])["open-one"]
    assert 'class="pri P2"' in row and "Open &lt;one&gt;" in row
    assert '<span class="repo">webapp</span>:open-one' in row  # <repo>:<stub>, never the bare path
    assert 'title="dev/tasks/open-one"' in row  # the full path id stays in the data
    assert "6 items" in row and ("ago" in row or "just now" in row)
    assert 'data-action="toggletask"' in row and 'aria-expanded="false"' in row
    assert row.count("<button") == 1  # the whole line is one click target
    assert "dev/tasks/open-one" not in _text(row)  # only <repo>:<stub> is shown


@needs_node
def test_rows_carry_distinct_needs_you_and_in_flight_markers_with_counts(rendered):
    rows = _rows(rendered["main"])
    assert 'mk need' in rows["open-one"] and "1 needs you" in rows["open-one"]
    assert 'mk run' in rows["open-one"] and "1 in flight" in rows["open-one"]  # the in-flight item 'mid'
    assert 'mk need' in rows["stuck"] and 'mk run' not in rows["stuck"]  # a blocked task needs you too
    assert 'mk run' in rows["busy-p3"] and 'mk need' not in rows["busy-p3"]
    assert 'class="mk' not in rows["calm-p1"]  # nothing waiting, nothing running: no marker at all
    assert 'has-need' in rows["open-one"] and 'has-run' in rows["busy-p3"]


@needs_node
def test_clicking_a_row_opens_its_stage_map_and_only_one_task_is_open_at_a_time(rendered):
    opened = rendered["mainOpen"]
    assert opened.count('class="tbody"') == 1 and opened.count('aria-expanded="true"') == 1
    body = _rows(opened)["open-one"]
    for text in ("Go &amp; do", 'class="pipeline"', "Does the open thing.", "Working in", "webapp:open-one",
                 "dev/tasks/open-one"):
        assert text in body, text
    # Opening another task collapses the first (the existing stage map, unchanged).
    switched = rendered["mainSwitched"]
    assert switched.count('class="tbody"') == 1 and 'class="tbody"' not in _rows(switched)["open-one"]
    other = rendered["otherKey"].split("|", 1)[1].split("/")[-1]
    assert 'class="pipeline"' in _rows(switched)[other]
    assert 'class="tbody"' not in rendered["mainCollapsed"]  # a second click collapses it


@needs_node
def test_blocked_info_and_resolve_and_recheck_stay_reachable_in_the_expanded_task(rendered):
    body = _rows(rendered["mainOpen"])["open-one"]
    assert "need-list" in body and "Needs decision" in body and "pick &lt;one&gt; or two" in body
    assert "Resolve in Claude Code" in body and 'data-action="resolvesel"' in body
    assert 'data-later="recheck"' in body  # drawn, disabled while runs are unavailable
    assert 'class="tdetails"' in body and 'data-action="selecttask"' not in body  # task details are inline, not a button


@needs_node
def test_the_sort_is_one_single_choice_control_and_orders_the_list(rendered):
    bar = rendered["sortBar"]
    labels = re.findall(r">([^<]+)</button>", bar)
    assert labels == ["Priority", "Needs you first", "In progress first", "Latest activity"]
    assert bar.count('aria-pressed="true"') == 1 and re.search(r'aria-pressed="true">Priority<', bar)
    assert re.search(r'role="group"', HTML)
    # priority (default): priority, then latest activity; the dated task beats the undated one.
    assert _open_list_order(rendered["main"]) == ["calm-p1", "open-one", "stuck", "busy-p3"]
    expected = {
        "priority": ["calm-p1", "open-one", "stuck", "busy-p3"],
        "needs": ["open-one", "stuck", "calm-p1", "busy-p3"],
        "progress": ["open-one", "busy-p3", "calm-p1", "stuck"],
        "latest": ["busy-p3", "open-one", "calm-p1", "stuck"],
    }
    labels_by_id = {"priority": "Priority", "needs": "Needs you first", "progress": "In progress first",
                    "latest": "Latest activity"}
    for sort_id, order in expected.items():
        got = rendered["sorted"][sort_id]
        assert _open_list_order(got["main"]) == order, sort_id
        assert got["bar"].count('aria-pressed="true"') == 1, sort_id
        assert re.search(rf'aria-pressed="true">{labels_by_id[sort_id]}<', got["bar"]), sort_id


def _item_detail(html: str) -> str:
    """The inline item-detail block (it sits between the stage map and the task details)."""
    m = re.search(r'<div class="idetail".*?<div class="tdetails">', html, re.S)
    assert m, "no inline item detail rendered"
    return m.group(0)


def _task_details(html: str, stub: str = "open-one") -> str:
    row = _rows(html)[stub]
    return row[row.index('<div class="tdetails">'):]


@needs_node
def test_nothing_renders_beside_the_list_and_task_details_are_inline_under_the_stage_map(rendered):
    body = _rows(rendered["mainOpen"])["open-one"]
    assert 'class="idetail"' not in body  # no item selected yet
    assert body.index('class="pipeline"') < body.index('class="tdetails"')  # below the stage map
    details = _text(_task_details(rendered["mainOpen"]))
    for text in ("Task details", "Summary", "Does the open thing.", "Resolve in Claude Code", "Recent log entries", "began"):
        assert text in details, text
    assert "Items" not in details  # the stage map already lists the items
    assert 'class="tdetails"' in _rows(rendered["mainClosedOpened"])["old"]  # closed tasks get the same section


@needs_node
def test_a_blocked_task_states_its_reason_in_its_details(rendered):
    details = _text(_task_details(rendered["mainStuck"], "stuck"))
    assert "This task is blocked" in details and "Blocked by: dev/tasks/open-one" in details
    assert "No summary recorded." in details


@needs_node
def test_a_bubble_opens_its_item_inline_swaps_and_a_second_click_closes_it(rendered):
    detail = _item_detail(rendered["itemOpen"])
    assert 'data-item="' in detail and "Wait" in detail and "webapp:open-one" in detail
    assert 'class="node st-blocked sel"' in rendered["itemOpen"]  # the clicked bubble is highlighted
    assert rendered["itemOpen"].count(' sel"') == 1
    assert "Loading..." in _item_detail(rendered["itemLoading"])  # shown at once, filled in when the detail arrives
    # Another bubble swaps the detail and moves the highlight.
    swapped = rendered["itemSwapped"]
    assert 'class="node st-blocked sel"' not in swapped and swapped.count('class="idetail"') == 1 and swapped.count(' sel"') == 1
    # Clicking the selected bubble again closes it.
    assert 'class="idetail"' not in rendered["itemClosed"] and ' sel"' not in rendered["itemClosed"]
    # Opening another task drops the other task's selection.
    assert 'class="idetail"' not in rendered["itemAfterSwitch"]
    # The detail does not repeat what task details already show.
    assert "Task summary" not in detail and "Recent activity on this task" not in detail
    assert _rows(rendered["itemOpen"])["open-one"].count('aria-expanded="true"') == 1


@needs_node
def test_entry_points_open_the_task_in_the_list_and_scroll_to_it(rendered):
    opened = rendered["afterTaskClick"]
    assert 'data-open="closed" open' in opened  # its group was collapsed: revealed
    assert opened.count('aria-expanded="true"') == 1 and 'class="tbody"' in _rows(opened)["old"]
    assert any("dev/tasks/old" in s for s in rendered["taskClickScrolls"])
    assert "cmdbox" in _task_details(rendered["afterResolveTask"]) and rendered["resolveTaskScrolls"]
    after = rendered["afterResolveSel"]
    assert 'class="idetail"' in after and "cmdbox" in _item_detail(after)  # Resolve commands expanded as before


@needs_node
def test_recent_activity_and_the_other_lists_are_collapsed_by_default(rendered):
    main = rendered["main"]
    for key in ("activity", "closed", "deferred"):
        tag = re.search(rf'<details class="sec" data-open="{key}"[^>]*>', main).group(0)
        assert " open" not in tag, key
    # Open tasks is expanded by default; every group has the same collapse control.
    assert " open" in re.search(r'<details class="sec" data-open="open"[^>]*>', main).group(0)
    assert 'data-open="activity"' in main and "Recent activity" in main


@needs_node
def test_every_state_changing_action_is_drawn_and_disabled(rendered):
    """Queue, Cancel run, Retry, New task, Add item, ... are visible and cannot be clicked."""
    for surface in ("mainOpen", "itemOpen"):
        for tag in _buttons(rendered[surface], "data-later"):
            assert " disabled" in tag and 'aria-disabled="true"' in tag, (surface, tag)
    drawn = {m for surface in ("mainOpen", "itemOpen") for m in re.findall(r'data-later="(\w+)"', rendered[surface])}
    assert {"queue", "cancel", "additem", "queueall", "recheck"} <= drawn
    assert "arrives in a later increment" in rendered["mainOpen"]
    # With llm-kit unavailable the autonomous controls say why and cannot be clicked.
    assert "Autonomous runs are unavailable" in rendered["banners"]
    assert 'data-action="queue"' not in rendered["mainOpen"] + rendered["itemOpen"]


@needs_node
def test_no_rendered_markup_carries_a_style_attribute_or_handler(rendered):
    for surface in ("main", "mainOpen", "sortBar", "banners", "itemOpen", "itemResolve", "taskResolve", "afterResolveSel", "select"):
        html = rendered[surface]
        assert not re.search(r"\sstyle\s*=", html), surface
        assert not re.search(r"\son[a-z]+\s*=", html), surface


@needs_node
def test_resolve_in_claude_code_only_shows_two_commands_to_copy(rendered):
    assert "cmdbox" not in _item_detail(rendered["itemOpen"])
    for surface in (_item_detail(rendered["itemResolve"]), _task_details(rendered["taskResolve"])):
        box = surface[surface.index('class="cmdbox"'):]
        box = box[:box.index("</div></div></div>") + 18] if "</div></div></div>" in box else box
        assert box.count('class="cmdrow"') == 2 and box.count('data-action="copy"') == 2  # one copy button per box
        assert "Start Claude Code on this task" in box and "Or paste into a running session" in box
        assert f'cd &quot;{rendered["root"]}&quot; &amp;&amp; claude &quot;/task-kit:task work dev/tasks/open-one&quot;' in box
        assert "<code>/task-kit:task work dev/tasks/open-one</code>" in box
        assert "cmdcap" not in box and "/task work dev/tasks" not in box.replace("/task-kit:task work", "")  # the old form is gone
        assert 'data-action="queue"' not in surface and "POST" not in surface


@needs_node
def test_item_drawer_shows_state_note_plan_section_and_disabled_run_controls(rendered):
    panel = _item_detail(rendered["itemOpen"])
    assert "Waiting for you" in panel and "pick &lt;one&gt; or two" in panel
    assert "Run controls" in panel and "Worker, effort and time limit arrive in a later increment" in panel
    assert "webapp:open-one" in panel and "Runs in" in panel and rendered["root"] in panel
    assert "plan.md section" in panel
    assert "No section for this item" in panel  # 'wait' has no section in the fixture
    assert "Open &lt;one&gt;" not in panel or "Open <one>" not in panel


@needs_node
def test_with_runs_available_queue_retry_and_recheck_are_live_and_the_rest_stay_disabled(live):
    assert "Autonomous runs are on" in live["banners"]
    assert 'data-action="queue"' in live["mainOpen"] and 'data-action="recheck"' in live["mainOpen"]
    for surface in ("mainOpen", "itemWithRun"):
        for tag in _buttons(live[surface], "data-later"):
            assert " disabled" in tag and 'aria-disabled="true"' in tag, (surface, tag)
    later = {m for surface in ("mainOpen", "itemWithRun") for m in re.findall(r'data-later="(\w+)"', live[surface])}
    assert {"additem", "queueall"} <= later and not later & {"queue", "recheck", "cancel"}  # Cancel run is live now
    assert "Now running" in live["main"] and "Title of go-too" in live["main"]  # inside the collapsed activity section
    # A live run counts as in flight on its task's row; a queued one shows the queued chip.
    rows = _rows(live["main"])
    assert "2 in flight" in rows["open-one"]  # the in-flight item 'mid' plus the running run on 'go-too'
    assert "1 queued" in rows["calm-p1"] and 'mk q' in rows["calm-p1"] and 'mk run' not in rows["calm-p1"]


@needs_node
def test_a_failed_run_offers_retry_and_a_running_one_shows_its_state(live):
    assert "running" in live["mainOpen"].lower()
    run = next(r for r in live["payload"]["runs"] if r["id"] == "run-live")
    assert run["state"] == "running" and not run["retryable"] and run["checkout"] == live["pid"]
    failed = next(t for t in live["payload"]["tasks"] if t["id"] == "dev/tasks/open-one")["items"]
    go = next(i for i in failed if i["id"] == "go")
    assert go["run"]["state"] == "failed" and go["run"]["retryable"]


@needs_node
def test_the_actions_post_with_the_revision_the_page_saw_except_cancel_which_carries_none(live):
    posts = live["posts"]
    assert [p["method"] for p in posts] == ["POST", "POST", "POST", "POST"]
    pid = live["pid"]
    prefix = f"/api/v1/items/{pid}/dev%2Ftasks%2Fopen-one"
    assert posts[0]["url"] == f"{prefix}/go-three/queue"
    assert posts[1]["url"] == f"{prefix}/wait/recheck"
    assert posts[2]["url"] == "/api/v1/runs/run-fail/retry"
    assert posts[3]["url"] == "/api/v1/runs/run-live/cancel"
    for post in posts:
        assert post["headers"] == {"X-Task-Kit-Token": "test-token", "Content-Type": "application/json"}
    for post in posts[:3]:
        assert json.loads(post["body"]) == {"expected_revision": live["revision"]}
    assert json.loads(posts[3]["body"]) == {}  # process control on a run id: no task revision


@needs_node
def test_cancel_run_is_live_for_a_live_run_and_labels_say_cancelling_and_cancelled(live):
    main = live["main"]  # the Now running cards sit inside the collapsed activity section
    live_card = main[main.index("Title of go-too"):]
    live_card = live_card[:live_card.index("</div></div>")]
    assert re.search(r'<button[^>]*data-action="cancel"[^>]*data-key="run-live"', live_card)
    assert "disabled" not in re.search(r'<button[^>]*data-action="cancel"[^>]*>', live_card).group(0)
    stopping = main[main.index("Title of mid"):]
    stopping = stopping[:stopping.index("</div></div>")]
    assert "Cancelling" in main and 'data-action="cancel"' not in stopping  # no second cancel for a stopping run
    assert re.search(r'<button[^>]*disabled[^>]*data-idle="cancel"[^>]*>', stopping) and "Cancelling</button>" in stopping
    queued = main[main.index("Title of c1"):]
    assert re.search(r'data-action="cancel"[^>]*data-key="run-queued"', queued[:queued.index("</div></div>")])  # a queued run too
    recent = live["payload"]["recent_runs"]
    cancelled = next(r for r in recent if r["id"] == "run-cancelled")
    assert cancelled["retryable"] and not cancelled["cancellable"] and cancelled["state"] == "cancelled"
    stopping_row = next(r for r in live["payload"]["runs"] if r["id"] == "run-stopping")
    assert stopping_row["state"] == "cancelling" and not stopping_row["cancellable"] and not stopping_row["retryable"]
    assert "Cancelled" in live["mainOpen"] + live["main"] + live["itemWithRun"]
    assert 'data-later="cancel"' not in live["main"]  # nothing is drawn as "arrives later" any more


@needs_node
def test_a_run_row_expands_in_place_with_its_result_retry_and_log(live):
    closed = live["runClosed"]
    assert "log line" not in closed and 'class="run-detail"' not in closed
    opened = live["runInline"]
    row = opened[opened.index('class="run-detail"'):]
    row = row[:row.index("</tr>")]
    for text in ("timeout", "reached its limit", "Run log", "log line"):
        assert text in row, text
    assert 'data-action="retry"' in row  # a failed run is retryable from its own row


@needs_node
def test_the_item_detail_run_log_toggle_shows_the_same_log_inline(live):
    before = _item_detail(live["itemWithRun"])
    assert "Latest run" in before and "Run log" in before and "log line" not in before
    after = _item_detail(live["itemRunLog"])
    assert "Hide run log" in after and "log line" in after


@pytest.fixture(scope="module")
def no_llm(tmp_path_factory):
    """llm-kit went away while a run was live: Queue and Retry are off, Cancel run is still on."""
    tmp = tmp_path_factory.mktemp("ui-no-llm")
    devroot = _world(tmp)
    snap = Inventory(parents=(devroot,), min_interval=0).snapshot()
    cid = _checkout(snap, "open-one")
    capabilities = {
        "read": True,
        "run": {"available": False, "reason": "llm-kit is not importable", "state": "absent"},
        "mutations": {"available": True, "actions": ["queue", "retry", "recheck", "cancel"], "reason": "live"},
    }
    runs = [_run_row(2, "run-live", cid, "running", "go-too"), _run_row(1, "run-fail", cid, "failed", "go", code="x", detail="y")]
    payload = snapshot.dashboard_payload(snap, runtime={"capabilities": capabilities, "runs": runs})
    return _run_harness(tmp, payload, snap)


@needs_node
def test_cancel_run_does_not_depend_on_llm_kit_being_available(no_llm):
    assert 'data-action="cancel"' in no_llm["main"]  # the live card in Now running
    assert 'data-action="queue"' not in no_llm["mainOpen"] and 'data-action="retry"' not in no_llm["main"]


# ---------------------------------------------------------------------------
# authoring: the inline forms and controls
# ---------------------------------------------------------------------------

AUTHORING_HARNESS = Path(__file__).with_name("ui_authoring_harness.js")
ALL_ACTIONS = ["queue", "retry", "recheck", "cancel", "newtask", "additem", "defer", "undefer", "queueall", "remove", "runoptions"]
AUTHORING_CAPABILITIES = {
    "read": True,
    "run": {"available": True, "reason": None, "state": "ok"},
    "mutations": {"available": True, "actions": ALL_ACTIONS, "reason": "live"},
    "run_options": {
        "defaults": {"worker": "claude", "effort": None, "timeout_s": 7200.0},
        "workers": {
            "claude": {"available": True, "entry": "opus", "efforts": ["low", "medium", "high", "xhigh", "max"], "reason": None},
            "codex": {"available": False, "entry": "sol", "efforts": [], "reason": "Codex workers are refused until a live test shows the floor"},
        },
    },
    "subagents": {"claude": {"state": "off", "code": "no-record", "reason": "no characterization record; run `task-kit characterize`",
                             "command": "task-kit characterize", "limits": None, "residuals": [], "closed": [],
                             "cli_check": {"state": "unobserved", "detail": "no init event in the baseline run (timeout)"}}},
}
SUBAGENT_RESIDUALS = ["a resumed sub-agent can exceed the at-once limit", "there is no total limit on sub-agents",
                      "Claude Code can lift the at-once limit from its servers"]
AUTHORING_CAPABILITIES_ON = {
    **AUTHORING_CAPABILITIES,
    "subagents": {"claude": {
        "state": "on", "code": "on", "reason": "characterized on this Claude Code", "command": "task-kit characterize",
        "limits": {"depth": 1, "concurrent": 3},
        "binary": {"realpath": "/x/claude", "digest": "d" * 64, "version": "2.1.304 (Claude Code)"},
        "checked_at": "2026-10-20T10:00:00+00:00", "expires_at": "2026-10-27T10:00:00+00:00",
        "checks": {"agent_callable": "pass"}, "residuals": SUBAGENT_RESIDUALS,
        "closed": ["Workflows and ultracode are off in dashboard runs."],
        "cli_check": {"state": "observed", "detail": ""}}},
}


@pytest.fixture(scope="module")
def authoring(tmp_path_factory):
    """The live page: an available item, a deferred one, a queued entry (Queued #1), driven through every form."""
    return _drive_authoring(tmp_path_factory.mktemp("ui-authoring"), AUTHORING_CAPABILITIES)


@pytest.fixture(scope="module")
def authoring_on(tmp_path_factory):
    """The same page while native sub-agents are ON, and a started run that recorded its decision."""
    return _drive_authoring(tmp_path_factory.mktemp("ui-authoring-on"), AUTHORING_CAPABILITIES_ON)


def _drive_authoring(tmp, capabilities):
    devroot = _world(tmp)
    snap = Inventory(parents=(devroot,), min_interval=0).snapshot()
    cid = _checkout(snap, "open-one")
    queued = _run_row(7, "run-q", cid, "queued", "go-too")
    if capabilities["subagents"]["claude"]["state"] == "on":  # a run that recorded the decision it started with
        (tmp / "recorded").mkdir()
        (tmp / "recorded" / "spec.json").write_text(json.dumps({"subagents": {
            "state": "on", "reason": "characterized on this Claude Code", "limits": {"depth": 1, "concurrent": 3}}}), encoding="utf-8")
        queued = dataclasses.replace(queued, run_dir=str(tmp / "recorded"))
    waiting = {"code": "no-slot", "detail": "2 of 2 checkouts are running"}
    payload = snapshot.dashboard_payload(snap, runtime={"capabilities": capabilities, "runs": [queued], "held": set(), "run_active": set(),
                                                         "queue_view": {"run-q": (1, waiting)},
                                                         "helpers_by_checkout": {cid: ["local-qwen", "ghost"]}})
    task = snap.find_task(cid, "dev/tasks/open-one")
    scenario = {
        "taskKey": f"{cid}|dev/tasks/open-one", "availItem": f"{cid}|dev/tasks/open-one|go",
        "deferredItem": f"{cid}|dev/tasks/open-one|later", "queuedItem": f"{cid}|dev/tasks/open-one|go-too", "queuedRun": "run-q",
        "detail": snapshot.item_detail(task, "go"),
        "helperCalls": [
            {"id": 4, "helper": "local-qwen", "state": "completed", "status": "completed", "entry": "or-qwen",
             "reserved_at": "2026-10-20T10:00:00.000000+00:00", "ended_at": "2026-10-20T10:00:02.500000+00:00",
             "input_chars": 120, "output_chars": 300, "usage": {"input_tokens": 30, "output_tokens": 70}, "reported_cost_usd": 0.0021,
             "detail": None},
            {"id": 5, "helper": "ghost", "state": "refused", "status": None, "entry": None, "reserved_at": "2026-10-20T10:00:03.000000+00:00",
             "ended_at": None, "input_chars": 9, "output_chars": None, "usage": None, "reported_cost_usd": None,
             "detail": "budget-spent: 3 of 3 calls for ghost are spent in this run"},
        ],
    }
    (tmp / "payload.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")
    result = subprocess.run([NODE, str(AUTHORING_HARNESS), str(STATIC / "app.js"), str(tmp / "payload.json"), str(tmp / "scenario.json")],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    report["cid"], report["payload"], report["revision"] = cid, payload, next(t["revision"] for t in payload["tasks"] if t["id"] == "dev/tasks/open-one")
    return report


def _fields(html: str, scope: str) -> dict:
    return {m.group(1): m.group(0) for m in re.finditer(rf'<(?:input|textarea|select)\b[^>]*data-draft="{scope}"[^>]*data-field="(\w+)"[^>]*>', html)}


@needs_node
def test_new_task_is_enabled_by_the_page_once_the_server_backs_it_and_opens_an_inline_form(authoring):
    assert "disabled" not in authoring["newBtn"] and authoring["newBtn"]["data-action"] == "newtask"
    form = authoring["newOpen"]
    assert 'data-form="newtask"' in form and form.index('data-form="newtask"') < form.index('data-open="open"')  # above the task list
    for label in ("Project", "Title", "Priority", "First item (optional)", "First item details", "Create task", "Cancel"):
        assert label in form, label
    assert set(_fields(form, "nt")) == {"project", "title", "priority", "itemtitle", "details"}
    assert re.search(r'<option value="P2" selected>P2</option>', form)  # P2 is the default
    assert "webapp" in form  # the project dropdown lists the repository
    assert 'data-later="newtask"' not in form


@needs_node
def test_what_was_typed_survives_the_polls_re_render_with_focus_and_caret(authoring):
    after = authoring["newAfterPoll"]
    assert 'value="Fix the flaky build"' in after and "Run it twice.</textarea>" in after and 'value="Reproduce it"' in after
    assert re.search(r'<option value="P1" selected>P1</option>', after)
    assert '[data-fk="nt::title"]' in authoring["focusAfterPoll"]
    assert authoring["caretAfterPoll"] == [['[data-fk="nt::title"]', 4, 4]]


@needs_node
def test_create_task_posts_the_form_and_closes_it(authoring):
    (post,) = authoring["newSent"]
    assert post["method"] == "POST" and post["url"].startswith("/api/v1/projects/") and post["url"].endswith("/tasks")
    assert json.loads(post["body"]) == {"title": "Fix the flaky build", "priority": "P1",
                                        "first_item": {"title": "Reproduce it", "details": "Run it twice."}}
    assert 'data-form="newtask"' not in authoring["newAfterCreate"]
    assert "expected_revision" not in post["body"]  # judged on its input, not on a revision


@needs_node
def test_an_empty_title_is_refused_in_the_form_before_any_request(authoring):
    assert "Give the task a title." in authoring["newEmptyTitle"] and authoring["newEmptyTitleSent"] == []


@needs_node
def test_add_item_opens_an_inline_form_under_the_stage_map_with_three_buttons(authoring):
    body = _rows(authoring["addOpen"])["open-one"]
    form = body[body.index('data-form="additem"'):]
    assert body.index('class="pipeline"') < body.index('data-form="additem"') < body.index('class="tdetails"')
    assert set(_fields(form, "ai")) == {"title", "priority", "details"}
    for label in ("Add and queue", "Add only", "Cancel"):
        assert label in form, label
    assert 'data-action="addsubmit"' in form and 'data-queue="1"' in form and 'data-queue="0"' in form
    assert 'data-action="additem" data-key=' in body  # the bubble toggles it


@needs_node
def test_add_and_queue_without_details_is_refused_in_the_form_and_with_them_it_posts_the_revision_the_page_saw(authoring):
    assert "Add and queue needs details" in authoring["addNoDetails"] and authoring["addNoDetailsSent"] == []
    (post,) = authoring["addSent"]
    cid = authoring["cid"]
    assert post["method"] == "POST" and post["url"] == f"/api/v1/tasks/{cid}/dev%2Ftasks%2Fopen-one/items"
    assert json.loads(post["body"]) == {"expected_revision": authoring["revision"], "title": "Write the docs", "priority": "P2",
                                        "details": "Explain it.", "queue": True}
    assert 'data-form="additem"' not in authoring["addAfter"]


@needs_node
def test_a_refused_add_keeps_the_form_what_was_typed_and_says_why(authoring):
    refused = authoring["addRefused"]
    assert "a Claude Code session is working this task" in refused
    assert 'value="Kept title"' in refused and 'data-form="additem"' in refused


@needs_node
def test_defer_and_undefer_patch_the_item_with_the_revision_and_queue_all_posts_to_the_task(authoring):
    cid, prefix = authoring["cid"], authoring["cid"] + "/dev%2Ftasks%2Fopen-one"
    assert 'data-action="defer"' in authoring["availDetail"] and 'data-action="undefer"' in authoring["deferredDetail"]
    (defer,) = authoring["deferSent"]
    assert (defer["method"], defer["url"]) == ("PATCH", f"/api/v1/items/{prefix}/go")
    assert json.loads(defer["body"]) == {"expected_revision": authoring["revision"], "state": "deferred"}
    (undefer,) = authoring["undeferSent"]
    assert (undefer["method"], undefer["url"]) == ("PATCH", f"/api/v1/items/{prefix}/later")
    assert json.loads(undefer["body"]) == {"expected_revision": authoring["revision"], "state": "available"}
    (queue_all,) = authoring["queueAllSent"]
    assert (queue_all["method"], queue_all["url"]) == ("POST", f"/api/v1/tasks/{prefix}/queue")
    assert json.loads(queue_all["body"]) == {"expected_revision": authoring["revision"]}
    assert "Queue all available (2)" in _rows(authoring["openTask"])["open-one"]  # go and go-three; go-too is already queued


@needs_node
def test_run_options_of_an_available_item_ride_on_queue(authoring):
    controls = authoring["optionsAvailable"]
    assert "Worker" in controls and "Effort" in controls and "Time limit" in controls
    assert re.search(r'<button[^>]*data-action="optworker"[^>]*data-v="claude"[^>]*aria-pressed="true"', controls)
    codex = re.search(r"<button[^>]*>Codex</button>", controls).group(0)
    assert "disabled" in codex and "Codex workers are refused" in codex  # disabled with the reason
    assert '<option value="">Default</option>' in controls and all(f'<option value="{e}"' in controls for e in ("low", "high", "max"))
    assert 'data-optsave="1"' not in controls  # an available item saves nothing until Queue
    (queue,) = authoring["queueSent"]
    assert json.loads(queue["body"]) == {"expected_revision": authoring["revision"], "options": {"effort": "high", "timeout_s": 900}}
    assert "The time limit must be a positive number of minutes." in authoring["badMinutes"] and authoring["badMinutesSent"] == []


@needs_node
def test_a_queued_entry_says_why_it_waits_and_the_run_controls_say_sub_agents_are_off(authoring):
    queued = authoring["optionsQueued"]
    assert "Queued #1" in queued and "waiting: 2 of 2 checkouts are running" in queued
    assert authoring["payload"]["runs"][0]["waiting"] == {"code": "no-slot", "detail": "2 of 2 checkouts are running"}
    assert 'data-subagents="off"' in queued and "Native sub-agents: off -- no characterization record; run `task-kit characterize`." in queued
    assert "task-kit characterize</code>" in queued and 'data-action="copy"' in queued  # the command to copy
    assert "data-subagents-disclosure" not in queued  # the residuals are disclosed when sub-agents are on
    assert "Not observed: no init event in the baseline run (timeout)." in queued  # the check did not conclude: said so


@needs_node
def test_when_sub_agents_are_on_the_page_gives_the_limits_the_dates_and_the_residuals(authoring_on):
    """R24. RED: drop the residual list from ``subagentsHtml`` and the disclosure line loses them."""
    queued = authoring_on["optionsQueued"]
    assert 'data-subagents="on"' in queued
    assert ("Native sub-agents: on (at most 3 at once, no nesting; checked 2026-10-20 on 2.1.304 (Claude Code), recheck by 2026-10-27)."
            in queued)
    disclosure = re.search(r'<p class="hint" data-subagents-disclosure="1">(.*?)</p>', queued).group(1)
    assert disclosure.startswith("Claude Code enforces these per session; ")
    for residual in SUBAGENT_RESIDUALS:  # the resume, no-total-limit and server-flag residuals, each named
        assert residual in disclosure
    assert "Workflows and ultracode are off in dashboard runs." in disclosure
    assert "task-kit characterize" not in queued  # nothing to copy while it is on
    assert 'data-run-subagents="on"' in queued and "on (at most 3 at once, no nesting)" in queued  # the run's own record


@needs_node
def test_a_queued_entry_shows_its_place_its_options_and_saves_a_change_at_once(authoring):
    queued = authoring["optionsQueued"]
    assert "Queued #1" in queued and 'data-action="remove"' in queued and "Remove from queue" in queued
    assert 'data-action="cancel"' not in queued  # a queued entry is removed, not cancelled
    assert queued.count('data-optsave="1"') == 2 and "Saved to the queued entry" in queued
    (save,) = authoring["saveSent"]
    assert (save["method"], save["url"]) == ("PUT", f"/api/v1/items/{authoring['cid']}/dev%2Ftasks%2Fopen-one/go-too/run-options")
    assert json.loads(save["body"]) == {"options": {"worker": "claude", "effort": "low"}}
    (remove,) = authoring["removeSent"]
    assert (remove["method"], remove["url"]) == ("DELETE", "/api/v1/queue/run-q") and json.loads(remove["body"]) == {}


@needs_node
def test_authoring_is_drawn_off_with_the_reason_while_a_session_holds_the_task_or_a_run_works_it(authoring):
    held = _rows(authoring["held"])["open-one"]
    add = re.search(r'<button[^>]*data-idle="additem"[^>]*>', held).group(0)
    assert "disabled" in add and "A Claude Code session is working this task" in add and 'data-action="additem"' not in held
    working = _rows(authoring["runActive"])["open-one"]
    assert "A dashboard run is working this task" in re.search(r'<button[^>]*data-idle="additem"[^>]*>', working).group(0)
    assert 'data-action="queueall"' in held  # Queue is still allowed: the entry waits for the session


@needs_node
def test_removed_has_a_label_and_the_authoring_markup_has_no_style_or_handler(authoring):
    assert "removed: 'Removed'" in JS
    for surface in ("newOpen", "newAfterPoll", "addOpen", "addNoDetails", "optionsAvailable", "optionsQueued", "held"):
        html = authoring[surface]
        assert not re.search(r"\sstyle\s*=", html), surface
        assert not re.search(r"\son[a-z]+\s*=", html), surface
        assert "<form" not in html, surface  # CSP form-action 'none': nothing submits


@needs_node
def test_the_run_controls_name_the_helpers_offered_in_the_repository_and_a_run_lists_its_calls(authoring):
    queued = authoring["optionsQueued"]
    assert 'data-helpers="offered"' in queued and "Endpoint helpers for this repository: local-qwen, ghost." in queued
    rows = {t["id"]: t for t in authoring["payload"]["tasks"]}  # offered per checkout: every task there, no task elsewhere
    assert rows["dev/tasks/open-one"]["helpers"] == ["local-qwen", "ghost"]
    assert all(t["helpers"] == (["local-qwen", "ghost"] if t["checkout"] == authoring["cid"] else []) for t in rows.values())
    calls = authoring["runHelperCalls"]
    assert "Endpoint helper calls" in calls
    assert "local-qwen (or-qwen) - answered - 2.5 s - 120 characters in, 300 out - 100 tokens - $0.0021" in calls
    assert 'data-helper-call="5"' in calls and "ghost - refused - 9 characters in" in calls and "budget-spent: 3 of 3 calls" in calls


def test_a_task_with_no_helper_configured_says_so(tmp_path):
    snap = Inventory(parents=(_world(tmp_path),), min_interval=0).snapshot()
    payload = snapshot.dashboard_payload(snap, runtime={"capabilities": AUTHORING_CAPABILITIES, "runs": [], "held": set(), "run_active": set()})
    assert all(t["helpers"] == [] for t in payload["tasks"])
