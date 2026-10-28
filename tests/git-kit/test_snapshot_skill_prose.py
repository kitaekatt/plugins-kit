"""Contracts for the background (snapshot) mode prose of git-code-review.

The git skill must tell the main thread to freeze, launch ONE background runner
and return to work; the p4 skill, rendered from the same generator, has its own
shelf-based background mode but no land step. Both skills are generated, so
these assert the rendered files.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GIT_SKILL = REPO_ROOT / "plugins/git-kit/skills/git-code-review/SKILL.md"
P4_SKILL = REPO_ROOT / "plugins/p4-kit/skills/p4-code-review/SKILL.md"

# Strings only the git skill may carry. Each is specific enough that it cannot
# occur in p4 prose by accident (a bare "snapshot" or "land" can).
GIT_ONLY = (
    "snapshot_review.py",
    "snapshot_review.py\" freeze",
    "--snapshot",
    "--runner",
    "--inline",
    "run_in_background: true",
    "background_review",
    "snapshot_review.py\" land",
    "drop <bundle.snapshot.commit> --tree-only",
)


def _git() -> str:
    return GIT_SKILL.read_text(encoding="utf-8")


def _technique(body: str) -> str:
    """The background_review technique, up to the narration block."""
    return body.split("    - id: background_review\n", 1)[1].split("  narration:\n", 1)[0]


def test_main_thread_freezes_then_launches_one_background_runner() -> None:
    tech = _technique(_git())
    assert "snapshot_review.py\" freeze --paths" in tech
    assert "`subagent_type: general-purpose`, `run_in_background: true`" in tech
    assert "--snapshot <sha> --runner" in tech
    # Freeze precedes the launch, which precedes the hand-back.
    assert tech.index("freeze --paths") < tech.index("run_in_background: true")
    assert tech.index("run_in_background: true") < tech.index("On hand-back")


def test_main_thread_returns_to_work_without_waiting() -> None:
    tech = _technique(_git())
    assert "RETURN TO\n            WORK" in tech
    assert "Do not wait, poll, sleep" in tech


def test_runner_brief_forbids_asking_editing_and_landing() -> None:
    tech = _technique(_git())
    assert "Never ask the user, edit, commit, land or push" in tech
    assert "drop <sha> --tree-only" in tech


def test_handback_delta_and_land_flow() -> None:
    tech = _technique(_git())
    assert "ask every `?` gate in ONE turn" in tech
    assert "--snapshot <snap2> --base <snap1>" in tech
    assert "snapshot_review.py\" land <latest sha> -F <msgfile>" in tech
    assert "drop <superseded sha>" in tech


def test_runner_mode_overrides_steps_3_5_9_and_10() -> None:
    body = _git()
    assert "Runner mode: skip this step entirely and never ask the user" in body  # step 3
    assert "Runner mode: never ask. Render a NEEDS THE USER gate as `[?]`" in body  # step 5
    assert "Runner mode (`--runner`): stop after rendering." in body  # steps 9-10
    assert "--snapshot <sha> [--base <prev>]" in body  # step 1/2 input


def test_endpoint_lane_bash_calls_run_in_background() -> None:
    assert "Run every endpoint-lane Bash call with `run_in_background: true`" in _git()


@pytest.mark.parametrize("needle", GIT_ONLY)
def test_git_skill_carries_the_background_surface(needle: str) -> None:
    assert needle in _git()


# p4 renders its own background mode (tests/p4-kit/test_snapshot_skill_prose.py)
# but has no commit-based land step; only git's land/commit vocabulary is absent.
@pytest.mark.parametrize(
    "needle", ("snapshot_review.py\" land", "drop <bundle.snapshot.commit> --tree-only")
)
def test_p4_skill_carries_no_git_land_step(needle: str) -> None:
    assert needle not in P4_SKILL.read_text(encoding="utf-8")


def test_snapshot_launcher_uses_the_interpreter_standard() -> None:
    # Same launcher form as prepare_review.py in the skill body: the guarded
    # BOOTSTRAP_PYTHON expression plus the harness-substituted plugin root.
    body = _git()
    expected = (
        '"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" '
        '"${CLAUDE_PLUGIN_ROOT}/scripts/snapshot_review.py"'
    )
    assert expected in body
    assert "uv run" not in _technique(body)


def test_landing_is_not_automatic_and_names_its_authority() -> None:
    tech = _technique(_git())
    assert "Landing is not automatic." in tech
    assert "Land only when the user asks to commit" in tech
    assert "a CLAUDE.md that covers\n            the reviewed paths requires committing after review" in tech
    assert "(name that CLAUDE.md as the authority)" in tech
    assert "reviewed and ready, and land it when asked" in tech
    # The land command is only ever reached through that gate.
    assert tech.index("Landing is not automatic.") < tech.index("snapshot_review.py\" land")
    assert "Landed with `snapshot_review.py land` only on the user's request or a named CLAUDE.md requirement" in tech


def test_committed_ranges_go_to_the_runner_with_a_base_and_no_land() -> None:
    body = _git()
    tech = _technique(body)
    assert "A merge or rebase in progress is neither: run `full_review` inline." in tech
    assert "Committed range: skip step 2" in tech
    assert "git rev-parse --abbrev-ref @{upstream}" in tech
    assert "`git merge-base <a> <b>`" in tech
    assert "A committed range has nothing to land." in tech
    # full_review's mode rule no longer sends a committed range inline.
    assert "or a merge or rebase is in progress, follow technique `background_review`" in body
    assert "or the target is a committed range" not in body


def test_main_thread_pointer_leads_the_body_and_the_intro_leads_with_background() -> None:
    body = _git()
    head = body.split("# Git Code Review\n", 1)[1]
    assert head.lstrip().startswith("**Main thread: go straight to technique `background_review`.")
    assert "directly in conversation" not in body
    assert "runs in the BACKGROUND by default" in head.split("```yaml", 1)[0]
