"""Inline HTML renderer for the task review artifact."""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any
from urllib.parse import quote


_SECTION_TITLES = {
    "open": "Open tasks",
    "deferred": "Deferred tasks",
    "closed": "Closed tasks",
    "other": "Needs attention",
}

# Task rows dim with age: full brightness up to FRESH_HOURS old, fading
# linearly to MIN_BRIGHTNESS at STALE_HOURS and beyond.
FRESH_HOURS = 4.0
STALE_HOURS = 120.0
MIN_BRIGHTNESS = 0.33


def _parse_activity(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def age_brightness(last_activity: Any, now: datetime) -> float:
    """Brightness for a task last active at ``last_activity`` (undated = dimmest)."""
    activity = _parse_activity(last_activity)
    if activity is None:
        return MIN_BRIGHTNESS
    if len(last_activity) == 10:  # date only: read as noon, like the log parser
        activity = activity.replace(hour=12)
    hours = (now - activity).total_seconds() / 3600
    if hours <= FRESH_HOURS:
        return 1.0
    if hours >= STALE_HOURS:
        return MIN_BRIGHTNESS
    fraction = (hours - FRESH_HOURS) / (STALE_HOURS - FRESH_HOURS)
    return 1.0 - fraction * (1.0 - MIN_BRIGHTNESS)


def _text(value: Any, fallback: str = "-") -> str:
    if value is None or value == "":
        return fallback
    return escape(str(value))


def _task_key(task: dict[str, Any]) -> str:
    return str(task.get("key") or task.get("id") or "")


def _render_task(task: dict[str, Any], now: datetime) -> str:
    missing = bool(task.get("summary_missing"))
    summary_status = task.get("summary_status") or "unavailable"
    if missing:
        summary_class = "missing"
        summary_text = (
            "Summary is stale and needs regeneration."
            if summary_status == "stale"
            else "Summary is missing."
        )
    elif summary_status == "unavailable" and not task.get("summary"):
        summary_class = "unavailable"
        summary_text = "Summary is unavailable for this task locally."
    else:
        summary_class = "present"
        summary_text = _text(task.get("summary"))
    task_id = _text(task.get("id"))
    title = _text(task.get("title"), "Untitled task")
    status = _text(task.get("status"))
    last_update = _text(task.get("last_update"))
    summary_id = "task-summary-" + quote(_task_key(task), safe="")
    return (
        '<div class="task-card '
        + summary_class
        + '" style="--age-brightness:'
        + f"{age_brightness(task.get('last_activity'), now):.2f}"
        + '"><div class="task-row">'
        '<span class="task-name-wrap"><span class="task-title" tabindex="0" aria-describedby="'
        + escape(summary_id, quote=True)
        + '">'
        + title
        + '</span><span class="summary-card" id="'
        + escape(summary_id, quote=True)
        + '" role="tooltip"><strong>Summary:</strong> '
        + summary_text
        + '</span></span><span class="task-id-group"><button class="task-id-copy" type="button" data-task-id="'
        + escape(str(task.get("id") or ""), quote=True)
        + '" aria-label="Copy task work command for '
        + escape(str(task.get("id") or ""), quote=True)
        + '">'
        + task_id
        + '</button><span class="copy-feedback" aria-live="polite" aria-atomic="true"></span></span><span class="task-status status-'
        + escape(str(task.get("status") or "unknown"))
        + '">'
        + status
        + '</span><span class="task-date" title="'
        + _text(task.get("last_activity"), "")
        + '">'
        + last_update
        + "</span></div></div>"
    )


def _render_project(
    project: dict[str, Any],
    tasks_by_key: dict[str, dict[str, Any]],
    now: datetime,
) -> str:
    project_tasks = [
        tasks_by_key[key]
        for key in project.get("tasks", [])
        if key in tasks_by_key
    ]
    sections = project.get("sections") or {}
    rendered_sections = []
    for name in ("open", "deferred", "closed", "other"):
        task_list = [
            tasks_by_key[key]
            for key in sections.get(name, [])
            if key in tasks_by_key
        ]
        cards = "".join(_render_task(task, now) for task in task_list)
        if not task_list:
            continue
        rendered_sections.append(
            '<section class="task-section"><h3>'
            + _text(_SECTION_TITLES[name])
            + '<span class="count">'
            + str(len(task_list))
            + "</span></h3>"
            + cards
            + "</section>"
        )
    return (
        '<details class="project-card">'
        '<summary class="project-row"><span class="project-title">'
        + _text(project.get("name"), "Unnamed project")
        + '</span><span class="project-root">'
        + _text(project.get("root"))
        + '</span><span class="count">'
        + str(len(project_tasks))
        + " task(s)</span><span class=\"project-date\">"
        + _text(project.get("last_update"))
        + "</span></summary>"
        '<div class="project-body">'
        + "".join(rendered_sections)
        + "</div></details>"
    )


def render_review_html(data: dict[str, Any], now: datetime | None = None) -> str:
    """Render a complete self-contained review page from grouped listing data."""
    now = now or datetime.now()
    tasks = {_task_key(task): task for task in data.get("tasks", [])}
    projects = list(
        data.get("projects")
        or [
            {
                "name": data.get("scope", "project"),
                "root": "",
                "tasks": list(tasks),
                "sections": data.get("sections", {}),
            }
        ]
    )
    rendered_projects = "".join(_render_project(project, tasks, now) for project in projects)
    if not rendered_projects:
        rendered_projects = '<p class="empty">No projects with non-archived tasks.</p>'

    diagnostics = data.get("diagnostics", [])
    diagnostic_html = ""
    if diagnostics:
        rows = "".join(
            '<li><span class="diagnostic-code">'
            + _text(item.get("code"))
            + "</span>"
            + _text(item.get("message"))
            + "</li>"
            for item in diagnostics
        )
        diagnostic_html = (
            '<aside class="diagnostics"><strong>Review notes</strong><ul>'
            + rows
            + "</ul></aside>"
        )
    task_count = len(tasks)
    project_count = len(projects)
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            "<title>Task review</title>",
            "<style>",
            ":root { color-scheme: light dark; --bg:#10151d; --panel:#18212d; --text:#e9eef5; --muted:#9eabba; --line:#334255; --accent:#71b7ff; --warn:#f2b84b; --bad:#ff7b7b; --good:#83d6a3; }",
            "@media (prefers-color-scheme: light) { :root { --bg:#f5f7fa; --panel:#fff; --text:#17202c; --muted:#617083; --line:#d9e0e8; --accent:#1769aa; --warn:#9a6400; --bad:#b42318; --good:#147a43; } }",
            "* { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--text); font:14px/1.25 system-ui,-apple-system,Segoe UI,sans-serif; }",
            "main { max-width:1180px; margin:0 auto; padding:8px 12px 16px; } h1 { font-size:1.2em; line-height:1.25; margin:0 0 2px; } h2,h3 { display:flex; gap:6px; align-items:center; border-bottom:1px solid var(--line); padding-bottom:3px; margin:8px 0 4px; } h4 { margin:8px 0 3px; }",
            ".subtitle,.muted,.empty { color:var(--muted); } .count { font-size:.8em; color:var(--muted); font-weight:normal; }",
            ".diagnostics { border:1px solid var(--warn); background:color-mix(in srgb,var(--warn) 12%,transparent); padding:4px 8px; margin:4px 0; border-radius:4px; } .diagnostics ul { margin:2px 0 0; padding-left:16px; } .diagnostic-code { color:var(--warn); font-family:ui-monospace,monospace; margin-right:6px; }",
            ".project-card { background:var(--panel); border:1px solid var(--line); border-radius:4px; margin:2px 0; } .project-card[open] { border-color:var(--accent); } .project-row { cursor:pointer; list-style:none; display:grid; grid-template-columns:10px minmax(0,auto) auto minmax(0,1fr) auto; gap:6px; align-items:center; padding:1px 5px; min-height:1.5em; } .project-row::-webkit-details-marker { display:none; } .project-row::before { content:'>'; color:var(--muted); font-size:.75em; } .project-card[open] > .project-row::before { content:'v'; } .project-title { font-weight:700; } .project-root { color:var(--muted); font:12px ui-monospace,monospace; overflow-wrap:anywhere; } .project-row .count { justify-self:start; white-space:nowrap; } .project-date { color:var(--muted); font-size:.85em; white-space:nowrap; } .project-body { border-top:1px solid var(--line); padding:0 6px 6px; }",
            ".task-section h3 { font-size:1em; }",
            ".task-title,.task-id-group,.task-status,.task-date { opacity:var(--age-brightness,1); } .task-card { position:relative; background:color-mix(in srgb,var(--panel) 82%,var(--bg)); border:1px solid var(--line); border-radius:4px; margin:2px 0; } .task-row { display:grid; grid-template-columns:minmax(0,1fr) minmax(120px,auto) auto auto; gap:6px; align-items:center; padding:3px 6px; } .task-name-wrap { position:relative; min-width:0; } .task-title { font-weight:650; cursor:help; } .task-title:focus-visible { outline:1px solid var(--accent); outline-offset:2px; } .summary-card { display:none; position:absolute; z-index:5; top:calc(100% + 5px); left:0; width:min(420px,calc(100vw - 32px)); padding:9px 11px; border:1px solid var(--accent); border-radius:4px; background:var(--panel); box-shadow:0 4px 14px #0006; white-space:normal; overflow-wrap:anywhere; } .task-name-wrap:hover .summary-card, .task-title:focus + .summary-card { display:block; } .task-card.missing .summary-card { border-color:var(--bad); } .task-card.unavailable .summary-card { border-color:var(--warn); } .task-id-group { display:flex; align-items:center; gap:6px; min-width:0; } .task-id-copy { appearance:none; border:0; background:transparent; color:var(--muted); font:12px ui-monospace,monospace; padding:0; text-align:left; overflow-wrap:anywhere; cursor:pointer; } .task-id-copy:hover { color:var(--accent); text-decoration:underline; } .task-id-copy:focus-visible { outline:1px solid var(--accent); outline-offset:2px; } .copy-feedback { color:var(--good); font-size:11px; white-space:nowrap; } .copy-feedback[data-state=error] { color:var(--bad); } .task-status { font-size:12px; padding:2px 7px; border-radius:99px; border:1px solid var(--line); } .status-active,.status-blocked { color:var(--accent); } .status-deferred { color:var(--warn); } .status-closed { color:var(--good); } .task-date { color:var(--muted); white-space:nowrap; font-size:12px; } .empty { padding:4px 6px; margin:2px 0; }",
            "@media (max-width:700px) { .project-row { grid-template-columns:10px minmax(0,1fr) auto; } .project-root { grid-column:2 / -1; grid-row:2; } .project-row .count { grid-column:3; grid-row:1; justify-self:end; } .task-row { grid-template-columns:minmax(0,1fr) auto; } .task-name-wrap { grid-column:1 / -1; } .task-id-group { grid-column:1; grid-row:2; } .task-status { grid-column:1; grid-row:3; width:max-content; } .task-date { grid-column:2; grid-row:3; } .copy-feedback { color:var(--good); } }",
            "</style>",
            "</head>",
            "<body><main>",
            "<h1>Task review</h1>",
            '<p class="subtitle">'
            + _text(data.get("scope"), "all")
            + " scope - "
            + str(project_count)
            + " project(s) - "
            + str(task_count)
            + " task(s) - generated from the structured task listing</p>",
            diagnostic_html,
            rendered_projects,
            "</main>",
            "<script>",
            "// Keep one project open at a time.",
            "document.querySelectorAll('details.project-card').forEach(function (current) {",
            "  current.addEventListener('toggle', function () {",
            "    if (!current.open) return;",
            "    document.querySelectorAll('details.project-card[open]').forEach(function (other) {",
            "      if (other !== current) other.removeAttribute('open');",
            "    });",
            "  });",
            "});",
            "document.querySelectorAll('.task-id-copy').forEach(function (button) {",
            "  button.addEventListener('click', async function (event) {",
            "    event.preventDefault();",
            "    event.stopPropagation();",
            "    var command = 'task work ' + button.dataset.taskId;",
            "    var feedback = button.nextElementSibling;",
            "    async function legacyCopy() {",
            "      var field = document.createElement('textarea');",
            "      field.value = command;",
            "      field.setAttribute('readonly', '');",
            "      field.style.position = 'fixed';",
            "      field.style.opacity = '0';",
            "      document.body.appendChild(field);",
            "      field.select();",
            "      var copied = false;",
            "      try { copied = document.execCommand('copy'); } finally { field.remove(); }",
            "      if (!copied) throw new Error('copy failed');",
            "    }",
            "    try {",
            "      if (navigator.clipboard && navigator.clipboard.writeText) {",
            "        try { await navigator.clipboard.writeText(command); } catch (_) { await legacyCopy(); }",
            "      } else { await legacyCopy(); }",
            "      feedback.dataset.state = 'success';",
            "      feedback.textContent = 'Copied';",
            "    } catch (_) { feedback.dataset.state = 'error'; feedback.textContent = 'Copy failed'; }",
            "    window.setTimeout(function () { feedback.textContent = ''; feedback.removeAttribute('data-state'); }, 1400);",
            "  });",
            "});",
            "</script>",
            "</body></html>",
            "",
        ]
    )
