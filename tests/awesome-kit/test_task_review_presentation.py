"""Presentation regressions for the task review HTML artifact."""

from __future__ import annotations

from datetime import datetime

import pytest

from task_system.review_html import MIN_BRIGHTNESS, age_brightness, render_review_html


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


def test_deferred_section_renders_between_open_and_closed() -> None:
    opened = _task("opened", project="alpha", last_update="2026-09-20")
    held = _task("held", project="alpha", last_update="2026-09-19")
    held["status"] = "deferred"
    finished = _task("finished", project="alpha", last_update="2026-09-18")
    finished["status"] = "closed"
    data = {
        "scope": "project",
        "tasks": [opened, held, finished],
        "projects": [
            {
                "name": "alpha",
                "root": "projects/alpha",
                "tasks": ["opened", "held", "finished"],
                "sections": {
                    "open": ["opened"],
                    "deferred": ["held"],
                    "closed": ["finished"],
                    "other": [],
                },
            },
        ],
    }

    html = render_review_html(data)

    assert "Deferred tasks" in html
    assert html.index("Open tasks") < html.index("Deferred tasks")
    assert html.index("Deferred tasks") < html.index("Closed tasks")
    assert '<span class="task-status status-deferred">deferred</span>' in html


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
        assert f'<div class="task-card {expected_class}" ' in html
        assert expected_text in html


NOW = datetime(2026, 9, 24, 12, 0)


@pytest.mark.parametrize(
    ("last_activity", "expected"),
    [
        ("2026-09-24 12:00", 1.0),  # just now
        ("2026-09-24 08:00", 1.0),  # exactly 4 hours: still full
        ("2026-09-21 22:00", 1.0 - 0.5 * (1.0 - MIN_BRIGHTNESS)),  # 62 h: halfway
        ("2026-09-19 12:00", MIN_BRIGHTNESS),  # exactly 120 hours
        ("2026-07-01 12:00", MIN_BRIGHTNESS),  # far older
        ("2026-09-24", 1.0),  # date only reads as noon
        (None, MIN_BRIGHTNESS),  # undated
    ],
)
def test_age_brightness_interpolates_between_fresh_and_stale(
    last_activity: str | None, expected: float
) -> None:
    assert age_brightness(last_activity, NOW) == pytest.approx(expected)


def test_task_rows_carry_their_age_brightness() -> None:
    fresh = _task("fresh", project="alpha", last_update="2026-09-24")
    fresh["last_activity"] = "2026-09-24 11:00"
    stale = _task("stale", project="alpha", last_update="2026-07-01")
    stale["last_activity"] = "2026-07-01 12:00"
    data = {
        "scope": "project",
        "tasks": [fresh, stale],
        "sections": {"open": ["fresh", "stale"], "closed": [], "other": []},
    }

    html = render_review_html(data, now=NOW)

    assert 'style="--age-brightness:1.00"' in html
    assert f'style="--age-brightness:{MIN_BRIGHTNESS:.2f}"' in html
    assert "opacity:var(--age-brightness,1)" in html
