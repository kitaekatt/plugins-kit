"""Contracts for the background (frozen-shelf) mode prose of p4-code-review.

The p4 skill must tell the main thread to freeze the CL's shelf, launch ONE
background runner and return to work, and must never have the agent submit.
The skill is generated, so these assert the rendered file.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
P4_SKILL = REPO_ROOT / "plugins/p4-kit/skills/p4-code-review/SKILL.md"
LAUNCHER = (
    '"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" '
    '"${CLAUDE_PLUGIN_ROOT}/scripts/snapshot_review.py"'
)


def _body() -> str:
    return P4_SKILL.read_text(encoding="utf-8")


def _technique() -> str:
    return _body().split("    - id: background_review\n", 1)[1].split("  narration:\n", 1)[0]


def test_background_is_the_default_mode():
    body = _body()
    assert "follow technique `background_review` on the main thread" in body
    assert "`--inline` runs the steps below in the foreground instead" in body


def test_main_thread_freezes_then_launches_one_background_runner():
    tech = _technique()
    assert LAUNCHER + " freeze <CL>" in tech
    assert "`subagent_type: general-purpose`, `run_in_background: true`" in tech
    assert "`<CL> --runner --snapshot <record>`" in tech
    assert tech.index(" freeze <CL>") < tech.index("run_in_background: true") < tech.index("On hand-back")


def test_main_thread_returns_to_work_without_waiting():
    tech = _technique()
    assert "RETURN TO WORK" in tech
    assert "Do not wait, poll, sleep" in tech


def test_runner_never_asks_or_mutates_the_cl():
    tech = _technique()
    assert "Never ask the user, edit, reconcile, reopen,\n              shelve or submit" in tech
    assert "drop <record> --tree-only" in tech


def test_existing_shelf_is_never_overwritten_without_the_user():
    tech = _technique()
    assert "never overwrite that shelf yourself" in tech
    assert "--use-shelf" in tech


def test_handback_verifies_and_asks_once():
    tech = _technique()
    assert LAUNCHER + " verify <record>" in tech
    assert "in ONE turn (one AskUserQuestion)" in tech


def test_submit_stays_the_users_and_is_reported_against_the_snapshot():
    tech = _technique()
    assert "This skill never submits." in tech
    assert "ready for the user's own\n            `p4 submit -c <CL>`" in tech
    assert "land" not in tech


def test_runner_mode_overrides_steps_1_3_5_and_10():
    body = _body()
    assert "Runner mode (`<CL> --runner [--snapshot <record>]`): the CL is given; never prompt." in body
    assert "Runner mode: never ask and never reconcile or reopen." in body
    assert "Runner mode: never ask. Render a NEEDS THE USER gate as `[?]`" in body
    assert "Runner mode (`--runner`): after this cleanup, stop." in body
    assert LAUNCHER + " drop <bundle.snapshot.record> --tree-only" in body


def test_endpoint_lane_bash_calls_run_in_background():
    assert "Run every endpoint-lane Bash call with `run_in_background: true`" in _body()


def test_no_git_snapshot_vocabulary_leaks_into_p4():
    body = _body()
    for needle in ("--paths", "--base", "snapshot.commit", "land <", "-F <msgfile>", "git-kit"):
        assert needle not in _technique(), needle
    assert "uv run" not in _technique()
    assert "git-kit:git-code-review" not in body


def test_main_thread_pointer_leads_the_body_and_the_intro_leads_with_background():
    body = _body()
    head = body.split("# P4 Code Review\n", 1)[1]
    assert head.lstrip().startswith("**Main thread: go straight to technique `background_review`.")
    assert "directly in conversation" not in body
    assert "runs in the BACKGROUND by default" in head.split("```yaml", 1)[0]


def test_submitted_and_other_client_cls_go_to_the_runner_without_a_freeze():
    tech = _technique()
    assert "submitted or belongs to another client" in tech
    assert "skip step 2 and launch the runner with `<CL> --runner` and no `--snapshot`" in tech
    assert "`prepare_review.py <CL> [--snapshot <record>]`" in tech
    assert "run full_review inline" not in tech
