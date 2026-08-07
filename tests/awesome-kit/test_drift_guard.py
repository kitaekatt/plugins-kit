"""Spec for the orchestration decision-half drift guard.

The rule lives in scripts/check_orchestration_drift.py and is loaded from
there rather than reimplemented -- that script is what the pre-commit hook
runs, and a second copy here could pass while the gate that actually blocks
the commit disagreed.

Every negative assertion in this file has a POSITIVE CONTROL: a test that
constructs the state the check is supposed to reject and asserts it IS
rejected. A drift guard that cannot be made to fail on demand is not a guard.
"""

import copy
import importlib.util
import shutil
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "check_orchestration_drift.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_orchestration_drift", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_checker()


# --------------------------------------------------------------------------
# Fixtures: a throwaway repo root carrying real content
# --------------------------------------------------------------------------


@pytest.fixture
def fake_repo(tmp_path):
    """A minimal repo root: the real policy, the real baseline, a principles file."""
    policy = tmp_path / guard.POLICY_REL
    policy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_REPO_ROOT / guard.POLICY_REL, policy)

    principles = tmp_path / guard.PRINCIPLES_DIR_REL / "tier-principles.md"
    principles.parent.mkdir(parents=True, exist_ok=True)
    principles.write_text("# principles\n", encoding="utf-8")

    guard.update(tmp_path)
    return tmp_path


def _edit_policy(repo_root: Path, mutate) -> None:
    """Load, mutate in place, and write back the policy YAML."""
    path = repo_root / guard.POLICY_REL
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=False), encoding="utf-8"
    )


PRINCIPLES_REL = f"{guard.PRINCIPLES_DIR_REL}/tier-principles.md"


# --------------------------------------------------------------------------
# The shipped state
# --------------------------------------------------------------------------


def test_committed_baseline_matches_the_shipped_policy():
    """The baseline in the repo is current. Fails when someone edits the
    decision half without regenerating it -- which is the whole point."""
    policy = guard.load_policy(_REPO_ROOT / guard.POLICY_REL)
    stored = guard.read_baseline(_REPO_ROOT / guard.FINGERPRINT_REL)
    assert stored is not None, "no baseline committed"
    assert stored == guard.fingerprint(policy)


def test_repo_passes_with_nothing_staged():
    assert guard.check(_REPO_ROOT, staged=[]) == []


def test_decision_keys_do_not_include_the_machine_half():
    assert "backends" not in guard.DECISION_KEYS
    assert "capacity" not in guard.DECISION_KEYS


# --------------------------------------------------------------------------
# POSITIVE CONTROL -- the violating states must fail
# --------------------------------------------------------------------------


def test_positive_control_decision_half_changed_principles_untouched(fake_repo):
    """POSITIVE CONTROL. Decision half edited, baseline not regenerated,
    nothing under the principles dir touched -> the check FAILS."""
    assert guard.check(fake_repo, staged=[]) == [], "fixture did not start clean"

    _edit_policy(
        fake_repo,
        lambda d: d["ladders"][0]["rungs"].insert(
            1, {"id": "smuggled", "model": "haiku", "criteria": [["known"]]}
        ),
    )

    problems = guard.check(fake_repo, staged=[guard.POLICY_REL])
    assert problems, "a changed decision half with no principles change must fail"
    assert any(guard.UPDATE_COMMAND in p for p in problems), (
        "the failure must name the one command that updates the baseline"
    )


def test_positive_control_regenerating_the_baseline_alone_still_fails(fake_repo):
    """POSITIVE CONTROL. The obvious way to route around the drift check is to
    regenerate the baseline and stage it. That must still fail -- the baseline
    lives beside the principles, so it must not count as a principles change."""
    _edit_policy(fake_repo, lambda d: d.__setitem__("resolution", "last match wins"))
    guard.update(fake_repo)

    problems = guard.check(
        fake_repo, staged=[guard.POLICY_REL, guard.FINGERPRINT_REL]
    )
    assert problems, "regenerating the baseline must not satisfy its own gate"
    assert any("ONE-WAY" in p for p in problems)


def test_positive_control_missing_baseline_fails(fake_repo):
    (fake_repo / guard.FINGERPRINT_REL).unlink()
    problems = guard.check(fake_repo, staged=[])
    assert problems
    assert any(guard.UPDATE_COMMAND in p for p in problems)


# --------------------------------------------------------------------------
# The legitimate flows must pass without ceremony
# --------------------------------------------------------------------------


def test_principles_then_derive_passes(fake_repo):
    """Change a principle, re-derive the data, regenerate the baseline, stage
    all three -> passes. No ceremony, or people route around the check."""
    (fake_repo / PRINCIPLES_REL).write_text(
        "# principles\n\nP9: new rung.\n", encoding="utf-8"
    )
    _edit_policy(
        fake_repo,
        lambda d: d["ladders"][0]["rungs"].insert(
            1, {"id": "smuggled", "model": "haiku", "criteria": [["known"]]}
        ),
    )
    guard.update(fake_repo)

    assert guard.check(
        fake_repo,
        staged=[guard.POLICY_REL, guard.FINGERPRINT_REL, PRINCIPLES_REL],
    ) == []


def test_lexicon_change_also_counts_as_a_principles_change(fake_repo):
    lexicon_rel = f"{guard.PRINCIPLES_DIR_REL}/lexicon.md"
    (fake_repo / lexicon_rel).write_text("# lexicon\n", encoding="utf-8")
    _edit_policy(fake_repo, lambda d: d.__setitem__("resolution", "first match wins"))
    guard.update(fake_repo)

    assert guard.check(
        fake_repo, staged=[guard.POLICY_REL, guard.FINGERPRINT_REL, lexicon_rel]
    ) == []


# --------------------------------------------------------------------------
# False positives -- each of these would get the check disabled
# --------------------------------------------------------------------------


def test_machine_half_edit_does_not_change_the_fingerprint():
    """A `backends` / `capacity` edit is not derived from anything. It must not
    move the fingerprint at all."""
    policy = guard.load_policy(_REPO_ROOT / guard.POLICY_REL)
    before = guard.fingerprint(policy)

    mutated = copy.deepcopy(policy)
    mutated["backends"].append(
        {"id": "invented", "name": "Invented CLI", "detect": {"always": True}}
    )
    mutated["backends"][0]["name"] = "renamed"
    mutated["capacity"]["max_age_minutes"] = 999
    mutated["capacity"]["tier_overrides"] = {"top": "unavailable"}

    assert guard.fingerprint(mutated) == before


def test_machine_half_only_commit_passes(fake_repo):
    """The false positive that would get this check disabled: an agent editing
    only the machine half of orchestration.yaml stages that file and must not
    be asked for a principles change."""
    def mutate(d):
        d["capacity"]["max_age_minutes"] = 45
        d["backends"][1]["gotchas"].append("a new codex gotcha")

    _edit_policy(fake_repo, mutate)
    assert guard.check(fake_repo, staged=[guard.POLICY_REL]) == []


def test_reflowing_a_block_scalar_does_not_trip_the_check(fake_repo):
    """Whitespace folding: re-wrapping a `>-` paragraph changes bytes and
    nothing else (the renderer folds it anyway)."""
    policy = guard.load_policy(fake_repo / guard.POLICY_REL)
    before = guard.fingerprint(policy)
    policy["resolution"] = "  ".join(str(policy["resolution"]).split()) + "\n"
    assert guard.fingerprint(policy) == before


# --------------------------------------------------------------------------
# Fingerprint properties
# --------------------------------------------------------------------------


def test_fingerprint_is_order_sensitive_within_a_ladder():
    """Ordered elimination: reordering rungs IS a policy change."""
    policy = guard.load_policy(_REPO_ROOT / guard.POLICY_REL)
    before = guard.fingerprint(policy)
    mutated = copy.deepcopy(policy)
    mutated["ladders"][0]["rungs"].reverse()
    assert guard.fingerprint(mutated) != before


@pytest.mark.parametrize("key", guard.DECISION_KEYS)
def test_every_decision_key_is_covered_by_the_fingerprint(key):
    """POSITIVE CONTROL, per key: touching any one of them moves the hash."""
    policy = guard.load_policy(_REPO_ROOT / guard.POLICY_REL)
    before = guard.fingerprint(policy)
    mutated = copy.deepcopy(policy)
    value = mutated[key]
    if isinstance(value, dict):
        value["_probe"] = "x"
    elif isinstance(value, list):
        value.append({"id": "_probe"})
    else:
        mutated[key] = str(value) + " probe"
    assert guard.fingerprint(mutated) != before, f"{key} is not fingerprinted"


def test_baseline_roundtrips(tmp_path):
    path = tmp_path / "fp.txt"
    guard.write_baseline(path, "deadbeef")
    assert guard.read_baseline(path) == "deadbeef"
