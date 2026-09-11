"""Contract for plugins-kit's repo-local version-pairing submit gate.

The shared submit-gate parser feeds both git-kit and p4-kit.  This test keeps
the repository instruction declarative while pinning the command, scope, and
the non-vacuous evidence rule for review inputs that are not the Git index.
"""

from pathlib import Path

from bootstrap_lib.code_review.claude_mds import collect_submit_gates


REPO_ROOT = Path(__file__).resolve().parents[2]


def _gates_for(*relative_paths: str) -> list[dict]:
    return collect_submit_gates(
        [str(REPO_ROOT / "CLAUDE.md")],
        [str(REPO_ROOT / path) for path in relative_paths],
        REPO_ROOT,
    )


def test_plugin_change_exposes_existing_version_checks_to_both_review_kits() -> None:
    gates = _gates_for("plugins/git-kit/scripts/prepare_review.py")

    matching = [
        gate for gate in gates
        if "version-bumped since the last publish" in gate["summary"]
    ]
    assert len(matching) == 1
    gate = matching[0]
    assert gate["scope_paths"] == [
        "plugins/",
        ".claude-plugin/marketplace.json",
    ]
    assert "scripts/pre-commit-version-check.sh" in gate["rationale"]
    assert "Git index" in gate["rationale"]
    assert "Perforce changelist" in gate["rationale"]
    assert "empty staged run is not evidence" in gate["rationale"]


def test_version_gate_ignores_changes_outside_its_inputs() -> None:
    gates = _gates_for("docs/reference/testing.md")

    assert not any(
        "version-bumped since the last publish" in gate["summary"]
        for gate in gates
    )
