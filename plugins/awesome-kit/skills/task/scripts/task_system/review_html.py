"""Inline HTML renderer for the task review artifact."""

from __future__ import annotations

from html import escape
from typing import Any


_SECTION_TITLES = {
    "open": "Open tasks",
    "closed": "Closed tasks",
    "other": "Needs attention",
}


def _text(value: Any, fallback: str = "-") -> str:
    if value is None or value == "":
        return fallback
    return escape(str(value))


def _task_sort_key(task: dict[str, Any]) -> tuple[bool, str, str]:
    last_update = task.get("last_update")
    return (last_update is not None, last_update or "", task.get("id", ""))


def _render_updates(task: dict[str, Any]) -> str:
    updates = task.get("updates") or []
    if not updates:
        return '<p class="muted">No dated activity entries.</p>'
    items = []
    for update in updates:
        items.append(
            "<li><time>"
            + _text(update.get("date"))
            + "</time><span>"
            + _text(update.get("detail"))
            + "</span></li>"
        )
    return "<ul class=\"updates\">" + "".join(items) + "</ul>"


def _render_task(task: dict[str, Any]) -> str:
    missing = bool(task.get("summary_missing"))
    summary_status = task.get("summary_status") or "unavailable"
    if missing:
        summary_class = "missing"
        summary_text = (
            "Summary is stale and needs regeneration."
            if summary_status == "stale"
            else "Summary is missing."
        )
    elif summary_status == "unavailable":
        summary_class = "unavailable"
        summary_text = "Summary is unavailable for this task locally."
    else:
        summary_class = "present"
        summary_text = _text(task.get("summary"))
    task_id = _text(task.get("id"))
    title = _text(task.get("title"), "Untitled task")
    status = _text(task.get("status"))
    priority = _text(task.get("priority"))
    last_update = _text(task.get("last_update"))
    return (
        '<details class="task-card ' + summary_class + '">'
        '<summary><span class="task-title">'
        + title
        + '</span><span class="task-id">'
        + task_id
        + '</span><span class="task-status status-'
        + escape(str(task.get("status") or "unknown"))
        + '">'
        + status
        + '</span><span class="task-date">'
        + last_update
        + "</span></summary>"
        '<div class="task-body">'
        '<div class="summary-line '
        + summary_class
        + '"><strong>Summary:</strong> '
        + summary_text
        + "</div>"
        '<dl class="metadata"><div><dt>Priority</dt><dd>'
        + priority
        + '</dd></div><div><dt>Last activity</dt><dd>'
        + last_update
        + "</dd></div></dl>"
        '<h4>Updates</h4>'
        + _render_updates(task)
        + "</div></details>"
    )


def render_review_html(data: dict[str, Any]) -> str:
    """Render a complete self-contained review page from listing data."""
    tasks = {task["id"]: task for task in data.get("tasks", [])}
    sections = data.get("sections", {})
    rendered_sections = []
    for name in ("open", "closed", "other"):
        task_list = [
            tasks[task_id]
            for task_id in sections.get(name, [])
            if task_id in tasks
        ]
        task_list.sort(key=_task_sort_key, reverse=True)
        cards = "".join(_render_task(task) for task in task_list)
        if not cards:
            cards = '<p class="empty">No tasks in this section.</p>'
        rendered_sections.append(
            '<section><h2>'
            + _text(_SECTION_TITLES[name])
            + '<span class="count">'
            + str(len(task_list))
            + "</span></h2>"
            + cards
            + "</section>"
        )
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
            "* { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif; }",
            "main { max-width:1100px; margin:0 auto; padding:32px 20px 60px; } h1 { margin:0 0 4px; } h2 { display:flex; gap:10px; align-items:center; border-bottom:1px solid var(--line); padding-bottom:8px; margin:30px 0 12px; } h4 { margin:20px 0 6px; }",
            ".subtitle,.muted,.empty { color:var(--muted); } .count { font-size:.8em; color:var(--muted); font-weight:normal; }",
            ".diagnostics { border:1px solid var(--warn); background:color-mix(in srgb,var(--warn) 12%,transparent); padding:12px 16px; margin:20px 0; border-radius:8px; } .diagnostics ul { margin:6px 0 0; padding-left:20px; } .diagnostic-code { color:var(--warn); font-family:ui-monospace,monospace; margin-right:8px; }",
            ".task-card { background:var(--panel); border:1px solid var(--line); border-radius:8px; margin:8px 0; overflow:hidden; } .task-card[open] { border-color:var(--accent); } .task-card > summary { cursor:pointer; list-style:none; display:grid; grid-template-columns:minmax(180px,1fr) minmax(160px,auto) auto auto; gap:12px; align-items:center; padding:12px 14px; } .task-card > summary::-webkit-details-marker { display:none; } .task-card > summary::before { content:'>'; color:var(--muted); font-size:.75em; } .task-card[open] > summary::before { content:'v'; }",
            ".task-title { font-weight:650; } .task-id { color:var(--muted); font:12px ui-monospace,monospace; overflow-wrap:anywhere; } .task-status { font-size:12px; padding:2px 7px; border-radius:99px; border:1px solid var(--line); } .status-active,.status-blocked { color:var(--accent); } .status-closed { color:var(--good); } .task-date { color:var(--muted); white-space:nowrap; font-size:12px; }",
            ".task-body { border-top:1px solid var(--line); padding:14px 18px 18px 40px; } .summary-line { padding:10px 12px; border-left:4px solid var(--good); background:color-mix(in srgb,var(--good) 10%,transparent); border-radius:4px; } .summary-line.missing { border-left-color:var(--bad); background:color-mix(in srgb,var(--bad) 12%,transparent); color:var(--bad); } .summary-line.unavailable { border-left-color:var(--warn); background:color-mix(in srgb,var(--warn) 10%,transparent); color:var(--warn); }",
            ".metadata { display:flex; gap:28px; margin:14px 0; } .metadata div { display:flex; gap:7px; } dt { color:var(--muted); } dd { margin:0; } .updates { list-style:none; padding:0; margin:0; } .updates li { display:flex; gap:12px; padding:4px 0; border-bottom:1px solid color-mix(in srgb,var(--line) 50%,transparent); } .updates time { color:var(--muted); font:12px ui-monospace,monospace; min-width:90px; } .empty { padding:12px; }",
            "@media (max-width:700px) { .task-card > summary { grid-template-columns:1fr auto; } .task-id { grid-column:1 / -1; grid-row:2; } .task-status { grid-column:1; grid-row:3; width:max-content; } .task-date { grid-column:2; grid-row:3; } .metadata { flex-direction:column; gap:4px; } }",
            "</style>",
            "</head>",
            "<body><main>",
            "<h1>Task review</h1>",
            '<p class="subtitle">'
            + _text(data.get("scope"), "project")
            + " scope - "
            + str(task_count)
            + " task(s) - generated from the structured task listing</p>",
            diagnostic_html,
            *rendered_sections,
            "</main>",
            "<script>",
            "// Keep the review focused on one task at a time.",
            "document.querySelectorAll('details.task-card').forEach(function (current) {",
            "  current.addEventListener('toggle', function () {",
            "    if (!current.open) return;",
            "    document.querySelectorAll('details.task-card[open]').forEach(function (other) {",
            "      if (other !== current) other.removeAttribute('open');",
            "    });",
            "  });",
            "});",
            "</script>",
            "</body></html>",
            "",
        ]
    )