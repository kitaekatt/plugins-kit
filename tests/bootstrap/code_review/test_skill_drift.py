"""Cross-kit drift test for the two code-review SKILL.md files.

git-kit:git-code-review and p4-kit:p4-code-review run the same multi-agent
review pipeline (same profiles, subagents, guardrails, issue schema,
submit-gate semantics); only the VCS front-half differs. Historically the
shared back-half drifted by accident -- one kit's SKILL.md got a fix or a
clarifying sentence the other never received (findings G6/G7 of the
2026-06-09 architecture review).

This test pins the INVARIANT subtrees of the two skills' YAML blocks to
each other. It deliberately does NOT compare whole files: steps 1/3/10,
scope, narration counters for VCS-specific lists, output templates, and
similar sections legitimately diverge per VCS.

Legitimate per-VCS noun differences inside otherwise-shared prose (CL vs
diff, depot paths vs repo-relative paths, ...) are canonicalized by
``_normalize`` before comparison, so the test only fires on real semantic
drift.

Lives in tests/bootstrap/code_review/ because the invariant is the shared
review-pipeline contract embodied by bootstrap_lib/code_review -- neither
kit owns it, mirroring the cross-plugin vendoring drift tests already in
tests/bootstrap/.
"""

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
GIT_SKILL = _REPO_ROOT / "plugins/git-kit/skills/git-code-review/SKILL.md"
P4_SKILL = _REPO_ROOT / "plugins/p4-kit/skills/p4-code-review/SKILL.md"

# Canonicalization of legitimate per-VCS nouns. Applied uniformly to both
# sides; each rule maps one kit's noun onto the other's (or both onto a
# placeholder) so shared prose compares equal while real edits still differ.
_NOUN_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bCL\b"), "diff"),                 # p4's changeset noun
    (re.compile(r"\bthis range\b"), "this diff"),    # git's changeset noun
    (re.compile(r"\bdepot paths\b"), "file paths"),
    (re.compile(r"\brepo-relative paths\b"), "file paths"),
    (re.compile(r"<depot or local path>"), "<path>"),
    (re.compile(r"<repo-relative or absolute path>"), "<path>"),
    (re.compile(r"\brepo root\b"), "workspace root"),
]


def _normalize(node):
    if isinstance(node, str):
        for pattern, repl in _NOUN_RULES:
            node = pattern.sub(repl, node)
        return node
    if isinstance(node, dict):
        return {k: _normalize(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_normalize(v) for v in node]
    return node


def _load_skill(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"```yaml\n(.*?)```", text, re.DOTALL)
    assert m, f"{path}: no ```yaml block found"
    data = yaml.safe_load(m.group(1))
    assert "technique_skill" in data, f"{path}: no technique_skill root"
    return data["technique_skill"]


@pytest.fixture(scope="module")
def git_skill() -> dict:
    return _load_skill(GIT_SKILL)


@pytest.fixture(scope="module")
def p4_skill() -> dict:
    return _load_skill(P4_SKILL)


# Top-level subtrees that must stay equal (after noun canonicalization).
INVARIANT_SUBTREES = [
    "review_profiles",
    "subagents",
    "false_positive_guardrails",
    "agent_assumptions",
    "issue_format",
]


@pytest.mark.parametrize("key", INVARIANT_SUBTREES)
def test_invariant_subtree_equal(git_skill, p4_skill, key):
    assert key in git_skill, f"git-code-review SKILL.md lost its `{key}` subtree"
    assert key in p4_skill, f"p4-code-review SKILL.md lost its `{key}` subtree"
    assert _normalize(git_skill[key]) == _normalize(p4_skill[key]), (
        f"`{key}` drifted between git-code-review and p4-code-review. "
        f"These subtrees are shared pipeline contract: edit BOTH SKILL.mds "
        f"(or extend _NOUN_RULES if the difference is a new legitimate "
        f"per-VCS noun)."
    )


# submit_gates: rendering and authoring_format are shared contract;
# `description` legitimately differs (pre-push vs pre-submit framing and a
# git-only cross-reference note), so it is excluded.
@pytest.mark.parametrize("key", ["rendering", "authoring_format"])
def test_submit_gates_shared_fields_equal(git_skill, p4_skill, key):
    g = git_skill["submit_gates"][key]
    p = p4_skill["submit_gates"][key]
    assert _normalize(g) == _normalize(p), (
        f"submit_gates.{key} drifted between the two code-review SKILL.mds"
    )


def test_narration_note_equal(git_skill, p4_skill):
    assert _normalize(git_skill["narration"]["note"]) == _normalize(
        p4_skill["narration"]["note"]
    )


# Narration templates for the shared (VCS-neutral) pipeline phases, keyed
# by their `when` clause. Templates for VCS-specific phases (range vs CL
# resolution, untracked vs unreconciled, auto-shelve) are excluded.
SHARED_NARRATION_WHENS = [
    "After step 3, before step 4 (M >= 1)",
    "After step 3, before step 4 (M = 0)",
    "Before step 5 (G >= 1)",
    "Before step 6",
    "After step 6, before step 7 (X >= 1)",
    "After step 6 (X = 0)",
    "After step 7, before step 9",
]


def _template_for(skill: dict, when: str) -> str:
    matches = [t for t in skill["narration"]["templates"] if t["when"] == when]
    assert len(matches) == 1, f"expected exactly one template for when={when!r}"
    return matches[0]["template"]


@pytest.mark.parametrize("when", SHARED_NARRATION_WHENS)
def test_shared_narration_templates_equal(git_skill, p4_skill, when):
    g = _template_for(git_skill, when)
    p = _template_for(p4_skill, when)
    assert _normalize(g) == _normalize(p), (
        f"narration template for {when!r} drifted between the two "
        f"code-review SKILL.mds"
    )


# Narration variables for the shared pipeline counters. VCS-specific
# variables (<range>, <CL>, <U>, <U_added>, <V>, <auto_or_explicit>) are
# excluded -- they name different bundle fields by design.
SHARED_NARRATION_VARIABLES = [
    "<N>", "<M>", "<G>",
    "<X>", "<B>", "<C>", "<Y>",
    "<P>", "<R>", "<K>", "<RK>", "<reviewer_summary>",
]


@pytest.mark.parametrize("var", SHARED_NARRATION_VARIABLES)
def test_shared_narration_variables_equal(git_skill, p4_skill, var):
    g = git_skill["narration"]["variables"]
    p = p4_skill["narration"]["variables"]
    assert var in g, f"git-code-review narration lost variable {var}"
    assert var in p, f"p4-code-review narration lost variable {var}"
    assert _normalize(g[var]) == _normalize(p[var]), (
        f"narration variable {var} drifted between the two code-review "
        f"SKILL.mds"
    )


# Regression pins for the specific G6 drift bugs: these assert the CORRECT
# value, not just cross-file equality, so the pair can't drift in lockstep
# back to the broken text.
def test_validator_input_references_chunk_diff_not_full_diff(git_skill, p4_skill):
    """No full diff exists anywhere since chunking -- only per-chunk files.

    G6: p4's validator input said "the full diff", which is unfulfillable.
    """
    for skill, name in ((git_skill, "git"), (p4_skill, "p4")):
        validator = [s for s in skill["subagents"] if s["name"] == "validator"][0]
        assert "chunk diff" in validator["input"], (
            f"{name} validator input must reference the chunk diff"
        )
        assert "full diff" not in validator["input"]


def test_checklist_does_not_claim_first_match_profile_selection(git_skill, p4_skill):
    """G6: profile selection is an inference call, not first-match regex."""
    for skill in (git_skill, p4_skill):
        checklist = skill["techniques"][0]["checklist"]
        assert not any("first match" in item for item in checklist)
