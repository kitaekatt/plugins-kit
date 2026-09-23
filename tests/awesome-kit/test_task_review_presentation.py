"""Presentation regressions for the task review HTML artifact."""

from __future__ import annotations

from task_system.review_html import render_review_html


def _task(
    key: str,
    *,
    project: str,
    last_update: str,
    updates: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "key": key,
        "id": f"tmp/{key}",
        "status": "active",
        "priority": "P1",
        "last_update": last_update,
        "title": key,
        "summary": f"Summary for {key}.",
        "summary_status": "present",
        "summary_missing": False,
        "project": project,
        "updates": updates or [],
    }


def test_projects_keep_listing_order_and_omit_empty_sections() -> None:
    older = _task("older", project="alpha", last_update="2026-09-20")
    newer = _task("newer", project="zulu", last_update="2026-09-23")
    data = {
        "scope": "all",
        "tasks": [older, newer],
        "projects": [
            {
                "name": "zulu",
                "root": "projects/zulu",
                "tasks": ["newer"],
                "sections": {"open": ["newer"], "closed": [], "other": []},
            },
            {
                "name": "alpha",
                "root": "projects/alpha",
                "tasks": ["older"],
                "sections": {"open": ["older"], "closed": [], "other": []},
            },
        ],
    }

    html = render_review_html(data)

    assert html.index(">zulu</span>") < html.index(">alpha</span>")
    assert "Closed tasks" not in html
    assert "No tasks in this section." not in html


def test_task_name_hover_and_focus_reveal_only_the_summary() -> None:
    task = _task(
        "projector",
        project="home",
        last_update="2026-08-11",
        updates=[
            {
                "date": "2026-08-11",
                "detail": (
                    "update: status = 'active'; priority = 'P1'; "
                    "description = 'Finish the projector work'; "
                    "skills_to_invoke = ['home-domain']"
                ),
            }
        ],
    )
    data = {
        "scope": "project",
        "tasks": [task],
        "projects": [
            {
                "name": "home",
                "root": "projects/home",
                "tasks": ["projector"],
                "sections": {"open": ["projector"], "closed": [], "other": []},
            }
        ],
    }

    html = render_review_html(data)

    assert '<span class="task-title" tabindex="0" aria-describedby="task-summary-projector">projector</span>' in html
    assert '<span class="summary-card" id="task-summary-projector" role="tooltip"><strong>Summary:</strong> Summary for projector.</span>' in html
    assert ".task-name-wrap:hover .summary-card" in html
    assert ".task-title:focus + .summary-card" in html
    assert ".project-card { background:var(--panel); border:1px solid var(--line); border-radius:4px; margin:2px 0; }" in html
    assert ".project-card { background:var(--panel); border:1px solid var(--line); border-radius:4px; margin:2px 0; overflow:hidden; }" not in html
    assert "Finish the projector work" not in html
    assert "update: status" not in html
    assert "Updates" not in html
    assert ".updates" not in html
    assert "update-field" not in html
    assert "status-active" in html
    assert "2026-08-11" in html
    assert "details.task-card" not in html


def test_missing_stale_and_unavailable_summaries_remain_clear() -> None:
    base = _task("review", project="home", last_update="2026-09-23")
    missing = {**base, "summary": "", "summary_status": "missing", "summary_missing": True}
    stale = {**base, "summary": "Old text", "summary_status": "stale", "summary_missing": True}
    unavailable = {**base, "summary": "", "summary_status": "unavailable"}

    for task, expected_class, expected_text in (
        (missing, "missing", "Summary is missing."),
        (stale, "missing", "Summary is stale and needs regeneration."),
        (unavailable, "unavailable", "Summary is unavailable for this task locally."),
    ):
        data = {
            "scope": "project",
            "tasks": [task],
            "projects": [{"name": "home", "root": "projects/home", "tasks": ["review"], "sections": {"open": ["review"]}}],
        }
        html = render_review_html(data)
        assert f'<div class="task-card {expected_class}">' in html
        assert expected_text in html
