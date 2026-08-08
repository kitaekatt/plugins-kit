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
import os
import shutil
import subprocess
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


# --------------------------------------------------------------------------
# DEFECT 1 -- a pre-commit check must judge the INDEX, not the working tree
#
# The bypass these tests close: edit the decision half, regenerate the
# fingerprint (tree is now self-consistent), stage ONLY the yaml. Comparing the
# tree against itself passed, and the authorship gate never fired because the
# fingerprint was not in the index.
# --------------------------------------------------------------------------


_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        env=_GIT_ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output = proc.stdout.decode("utf-8", "replace")
    assert proc.returncode == 0, f"git {' '.join(args)} failed:\n{output}"
    return output


def _insert_rung(data):
    data["ladders"][0]["rungs"].insert(
        1, {"id": "smuggled", "model": "haiku", "criteria": [["known"]]}
    )


@pytest.fixture
def git_repo(tmp_path):
    """A real git repo whose HEAD carries a consistent policy + baseline +
    principles. Committed clean, so anything staged afterwards is the change
    under test."""
    repo = tmp_path / "repo"
    policy = repo / guard.POLICY_REL
    policy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_REPO_ROOT / guard.POLICY_REL, policy)

    principles = repo / PRINCIPLES_REL
    principles.parent.mkdir(parents=True, exist_ok=True)
    principles.write_text("# principles\n", encoding="utf-8")

    guard.update(repo)

    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init")
    assert guard.check(repo) == [], "fixture did not start clean"
    return repo


def test_partial_staging_of_a_decision_half_edit_is_blocked(git_repo):
    """DEFECT 1, the bypass itself. Edit the decision half, regenerate the
    baseline, stage ONLY the yaml -> must FAIL. The working tree agrees with
    itself here, so a tree-vs-tree comparison passes and the authorship gate
    never fires; only reading the index catches it."""
    _edit_policy(git_repo, _insert_rung)
    guard.update(git_repo)
    _git(git_repo, "add", guard.POLICY_REL)  # fingerprint deliberately unstaged

    problems = guard.check(git_repo)
    assert problems, (
        "staging a decision-half edit without its regenerated baseline must fail"
    )
    assert any(guard.UPDATE_COMMAND in p for p in problems)


def test_partial_staging_stays_blocked_on_later_commits(git_repo):
    """The bypass was sticky: once the tree was self-consistent, EVERY later
    commit passed too. Commit the partially-staged change, then stage something
    unrelated -- the index is now internally inconsistent and must still fail."""
    _edit_policy(git_repo, _insert_rung)
    guard.update(git_repo)
    _git(git_repo, "add", guard.POLICY_REL)
    _git(git_repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "sneak")

    (git_repo / "unrelated.txt").write_text("hello\n", encoding="utf-8")
    _git(git_repo, "add", "unrelated.txt")

    assert guard.check(git_repo), (
        "an index whose policy and baseline disagree must keep failing"
    )


def test_principles_then_derive_staged_fully_passes_in_a_real_repo(git_repo):
    """DEFECT 1 negative control. Change a principle, re-derive, regenerate the
    baseline, stage all three -> passes, no ceremony."""
    (git_repo / PRINCIPLES_REL).write_text(
        "# principles\n\nP9: new rung.\n", encoding="utf-8"
    )
    _edit_policy(git_repo, _insert_rung)
    guard.update(git_repo)
    _git(git_repo, "add", guard.POLICY_REL, guard.FINGERPRINT_REL, PRINCIPLES_REL)

    assert guard.check(git_repo) == []


def test_machine_half_only_staged_edit_passes_in_a_real_repo(git_repo):
    """DEFECT 1 regression guard. The index-based comparison must not make the
    check fire on the machine half, which is derived from nothing. This is the
    false positive that would get the check disabled."""
    def mutate(d):
        d["capacity"]["max_age_minutes"] = 45
        d["backends"][1]["gotchas"].append("a new codex gotcha")

    _edit_policy(git_repo, mutate)
    _git(git_repo, "add", guard.POLICY_REL)

    assert guard.check(git_repo) == []


def test_unstaged_working_tree_edit_does_not_block_an_unrelated_commit(git_repo):
    """The flip side of judging the index: a decision-half edit left UNSTAGED is
    not part of the commit, so an unrelated staged change is not blocked by it.
    (It is caught the moment it is staged -- see the bypass test above.)"""
    _edit_policy(git_repo, _insert_rung)
    (git_repo / "unrelated.txt").write_text("hello\n", encoding="utf-8")
    _git(git_repo, "add", "unrelated.txt")

    assert guard.check(git_repo) == []


def test_index_reads_beat_a_sabotaged_working_tree(git_repo):
    """POSITIVE CONTROL for the index read itself: with the yaml staged, a
    mutation made to the working tree AFTER staging is invisible to the check.
    If this passes only because the check still reads the working tree, the
    previous tests prove nothing."""
    _git(git_repo, "add", guard.POLICY_REL)  # no-op stage, index == HEAD
    _edit_policy(git_repo, _insert_rung)  # tree now drifted, index is not

    assert guard.check(git_repo, staged=[guard.POLICY_REL]) == []


def test_deleting_the_policy_from_the_index_is_a_problem(git_repo):
    """A path staged for deletion has no index entry. That must be reported,
    not swallowed as a pass."""
    _git(git_repo, "rm", "-q", "--cached", guard.POLICY_REL)
    problems = guard.check(git_repo)
    assert problems
    assert any("index" in p for p in problems)


# --------------------------------------------------------------------------
# DEFECT 2 -- a git failure must not silently disable the authorship gate
# --------------------------------------------------------------------------


class _FakeSubprocess:
    """Stands in for the subprocess module inside the guard only, so a
    simulated git failure cannot leak into the rest of the test run."""

    SubprocessError = subprocess.SubprocessError
    PIPE = subprocess.PIPE
    DEVNULL = subprocess.DEVNULL
    STDOUT = subprocess.STDOUT

    def __init__(self, run):
        self.run = run


def test_staged_paths_returns_none_when_git_raises(git_repo, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("git is not on PATH")

    monkeypatch.setattr(guard, "subprocess", _FakeSubprocess(boom))
    assert guard.staged_paths(git_repo) is None


def test_staged_paths_returns_none_on_nonzero_exit(git_repo, monkeypatch):
    def failing(cmd, *args, **kwargs):
        proc = subprocess.run(cmd, *args, **kwargs)
        proc.returncode = 128
        return proc

    monkeypatch.setattr(guard, "subprocess", _FakeSubprocess(failing))
    assert guard.staged_paths(git_repo) is None


def test_staged_paths_positive_control(git_repo):
    """POSITIVE CONTROL for the two tests above: with git working, staged_paths
    returns an actual list, so None genuinely means 'could not determine'."""
    _git(git_repo, "add", guard.POLICY_REL)
    assert guard.staged_paths(git_repo) == []  # a no-op stage changes nothing

    (git_repo / "unrelated.txt").write_text("hello\n", encoding="utf-8")
    _git(git_repo, "add", "unrelated.txt")
    assert guard.staged_paths(git_repo) == ["unrelated.txt"]


def test_check_reports_a_problem_when_git_fails(git_repo, monkeypatch):
    """DEFECT 2. git present but failing means the authorship gate could not be
    evaluated. Report it; never assume it is satisfied."""
    monkeypatch.setattr(guard, "staged_paths", lambda repo_root: None)

    problems = guard.check(git_repo)
    assert problems, "an un-evaluated gate must not read as a pass"
    assert any("git" in p for p in problems)


def test_git_failure_does_not_mask_a_real_drift(git_repo, monkeypatch):
    """The git problem is additive, not a short-circuit: the drift gate still
    runs and still reports."""
    monkeypatch.setattr(guard, "staged_paths", lambda repo_root: None)
    _edit_policy(git_repo, _insert_rung)

    problems = guard.check(git_repo)
    assert any("git" in p for p in problems)
    assert any(guard.UPDATE_COMMAND in p for p in problems)


def test_outside_a_git_repo_the_authorship_gate_is_skipped_silently(fake_repo):
    """DEFECT 2 negative control. No .git at all -- git-scoped checks degrade to
    advisory here (other workspaces are Perforce). No problem is reported, and
    the comparison stays on the working tree."""
    assert not (fake_repo / ".git").exists()
    assert guard.staged_paths(fake_repo) == []
    assert guard.check(fake_repo) == []


def test_outside_a_git_repo_the_drift_gate_still_fires(fake_repo):
    """POSITIVE CONTROL for the test above: skipping the authorship gate must
    not skip the drift gate."""
    _edit_policy(fake_repo, _insert_rung)
    problems = guard.check(fake_repo)
    assert problems
    assert any(guard.UPDATE_COMMAND in p for p in problems)


# --------------------------------------------------------------------------
# DEFECT 3 -- malformed policy YAML must not escape as an uncaught traceback
#
# check_orchestration_drift.py is the last step of pre-commit-version-check.sh
# and runs on every commit. It still failed CLOSED before this fix (the
# process crashed non-zero, so a bad commit was still blocked), but the
# author saw a raw Python traceback naming internal functions instead of one
# of this file's clean, actionable problem blocks.
# --------------------------------------------------------------------------


_MALFORMED_YAML = "resolution: >-\n  ok line\n bad indent line\nkey: [unclosed\n"


def test_malformed_policy_yaml_is_reported_not_raised(fake_repo):
    """POSITIVE CONTROL. A syntactically broken policy YAML must come back as
    a problem from check(), never as an escaping exception -- and the check
    must still fail closed (non-empty problems)."""
    (fake_repo / guard.POLICY_REL).write_text(_MALFORMED_YAML, encoding="utf-8")

    problems = guard.check(fake_repo, staged=[])  # must not raise

    assert problems, "malformed YAML must still fail closed"
    assert any("could not be parsed as YAML" in p for p in problems)


def test_malformed_policy_yaml_from_the_index_is_reported_not_raised(git_repo):
    """Same defect, but through the from-index path (`POLICY_REL (index)`),
    which is the path the original crash traceback actually came through."""
    (git_repo / guard.POLICY_REL).write_text(_MALFORMED_YAML, encoding="utf-8")
    _git(git_repo, "add", guard.POLICY_REL)

    problems = guard.check(git_repo)  # must not raise

    assert problems, "malformed staged YAML must still fail closed"
    assert any(guard.POLICY_REL in p for p in problems)


def test_parse_policy_raises_valueerror_naming_the_label_on_bad_yaml():
    """Unit-level check on parse_policy itself: a YAML syntax error becomes a
    ValueError carrying the caller-supplied label, not a raw yaml.YAMLError."""
    with pytest.raises(ValueError, match="mylabel"):
        guard.parse_policy(_MALFORMED_YAML, "mylabel")
