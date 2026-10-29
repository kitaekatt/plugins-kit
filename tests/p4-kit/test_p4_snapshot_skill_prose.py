"""Contracts for the background (frozen-shelf) mode prose of p4-code-review.

The p4 skill must tell the main thread to freeze the CL, launch ONE background
runner and return to work, and must never have the agent submit. It must also
say who may be asked what: a Claude-invoked review asks no pre-launch question,
a user-invoked one may ask for the CL and for how to handle a shelf the review
did not create. The skill is generated, so these assert the rendered files: the
SKILL.md front door holds the main-thread technique and references/full-review.md
holds the runner's and the inline path's procedure.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
P4_SKILL = REPO_ROOT / "plugins/p4-kit/skills/p4-code-review/SKILL.md"
P4_FULL = REPO_ROOT / "plugins/p4-kit/skills/p4-code-review/references/full-review.md"
LAUNCHER = (
    '"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" '
    '"${CLAUDE_PLUGIN_ROOT}/scripts/snapshot_review.py"'
)


def _body() -> str:
    return P4_SKILL.read_text(encoding="utf-8")


def _full() -> str:
    return P4_FULL.read_text(encoding="utf-8")


def _technique() -> str:
    """The background_review technique, up to the end of the front door's YAML block."""
    return _body().split("    - id: background_review\n", 1)[1].split("\n```\n", 1)[0]


def _step(number: int) -> str:
    """One numbered step of the front door's technique."""
    tech = _technique()
    start = tech.index(f"        - n: {number}\n")
    nxt = tech.find(f"        - n: {number + 1}\n", start)
    return tech[start:] if nxt == -1 else tech[start:nxt]


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_background_is_the_default_mode():
    body = _body()
    assert "(technique `background_review`)" in body
    assert "`--inline` runs `full_review` (references/full-review.md) in the foreground" in body
    assert "follow technique `background_review` on the main thread" in _full()


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


def test_runner_brief_sends_the_runner_to_the_full_procedure():
    brief = _flat(_step(3))
    assert "then read the skill's references/full-review.md and follow `full_review` in runner mode" in brief


def test_handback_verifies_and_asks_once():
    tech = _technique()
    assert LAUNCHER + " verify <record>" in tech
    assert "in ONE turn (one AskUserQuestion)" in tech
    assert "(references/declined-ledger.md, Record, using the runner's bundle_dir)" in _flat(tech)


def test_submit_stays_the_users_and_is_reported_against_the_snapshot():
    tech = _technique()
    assert "This skill never submits." in tech
    assert "ready for the user's own\n            `p4 submit -c <CL>`" in tech
    assert "land" not in tech


def test_runner_mode_overrides_steps_1_3_5_and_10():
    body = _full()
    assert "Runner mode (`<CL> --runner [--snapshot <record>]`): the CL is given; never prompt." in body
    assert "Runner mode: never ask and never reconcile or reopen." in body
    assert "Runner mode: never ask. Render a NEEDS THE USER gate as `[?]`" in body
    assert "Runner mode (`--runner`): after this cleanup, stop." in body
    assert LAUNCHER.replace("${CLAUDE_PLUGIN_ROOT}", "${P4_KIT_ROOT:?requires a bootstrap engine pass; run bootstrap run}") + " drop <bundle.snapshot.record> --tree-only" in body


def test_endpoint_lane_bash_calls_run_in_background():
    assert "Run every endpoint-lane Bash call with `run_in_background: true`" in _full()


def test_no_git_snapshot_vocabulary_leaks_into_p4():
    for needle in ("--paths", "--base", "snapshot.commit", "land <", "-F <msgfile>", "git-kit"):
        assert needle not in _technique(), needle
    assert "uv run" not in _technique()
    assert "git-kit:git-code-review" not in _body() + _full()


def test_main_thread_pointer_leads_the_body_and_the_intro_leads_with_background():
    body = _body()
    head = body.split("# P4 Code Review\n", 1)[1]
    assert head.lstrip().startswith("**Main thread: technique `background_review` below is everything you need.")
    assert "directly in conversation" not in body
    assert "runs in the BACKGROUND by default" in head.split("```yaml", 1)[0]


def test_submitted_and_other_client_cls_go_to_the_runner_without_a_freeze():
    tech = _technique()
    assert "submitted or belongs to another client" in tech
    assert "skip step 2 and launch the runner with `<CL> --runner` and no `--snapshot`" in tech
    assert "`prepare_review.py <CL> [--snapshot <record>]`" in tech
    assert "run full_review inline" not in tech


# ----------------------------------------------------- invoker-dependent questions


def test_the_skill_says_how_a_user_invocation_is_told_from_claudes():
    rule = _flat(_technique().split("      steps:\n", 1)[0])
    assert "USER-invoked: the turn carries a `<command-name>` tag naming this skill" in rule
    assert "CLAUDE-invoked: every other start -- you called the Skill tool, or you are the `--runner` agent" in rule
    assert "When unsure, treat the review as Claude-invoked." in rule
    assert "A Claude-invoked review asks no pre-launch question" in rule


def test_claude_invoked_picks_the_cl_and_user_invoked_asks_when_unspecified():
    step = _flat(_step(1))
    assert "A CL given in the arguments is used as given." in step
    assert "a Claude-invoked review picks the CL itself and asks nothing" in step
    assert "`p4 opened <file>` names a file's CL" in step
    assert "A user-invoked review asks which CL, once" in step
    # The inline path states the same rule rather than prompting unconditionally.
    full = _flat(_full())
    assert "a Claude-invoked review picks the CL and asks nothing; a user-invoked review asks which CL" in full
    assert "else list pending CLs and prompt the user" not in full


def test_a_shelf_the_review_created_is_reshelved_silently():
    step = _flat(_step(2))
    assert "A shelf this review created that went stale" in step
    assert "replaced from the open files with no question (`reshelved`)" in step
    assert "freeze again only after the earlier runner for that CL has handed back" in step


def test_a_foreign_shelf_is_never_touched_and_the_workspace_is_reviewed():
    step = _flat(_step(2))
    assert "The CL's shelf is the user's unless this review created it." in step
    assert "A shelf the review did NOT create that differs from the workspace is never touched." in step
    assert "add `--foreign-shelf workspace` when the review is Claude-invoked" in step
    assert "freeze copies the CL's open workspace files into the snapshot and reports `source: workspace`" in step
    assert "the review reads those copies, so editing meanwhile stays safe" in step


def test_only_a_user_invoked_review_may_ask_how_to_handle_a_foreign_shelf():
    step = _flat(_step(2))
    assert "Without the flag a user-invoked freeze exits 3 naming the files: ask the user once how to proceed" in step
    for option in ("(run it again with `--foreign-shelf workspace`)", "`p4 shelve -f -c <CL>`", "(`--use-shelf`)"):
        assert option in step, option
    # The agent never force-shelves on its own: replacing is the user's own command.
    assert "they run or approve `p4 shelve -f -c <CL>`" in step
    assert "never overwrite that shelf yourself" not in step


def test_the_checklist_carries_the_invoker_and_shelf_rules():
    checklist = _flat(_technique().split("      checklist:\n", 1)[1])
    assert "a Claude-invoked review asked no pre-launch question" in checklist
    assert "a shelf the review did not create never touched" in checklist
