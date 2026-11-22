"""Approved ignored outputs; run: uv run --extra dev pytest tests/git-kit/test_worktree_rebuildable.py -q.

Triggers: build leftovers, unmatched ignored data, unsafe glob approval, cutover resume.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import worktree_safety as safety
from worktree_config import validate_config
from test_worktree_lifecycle import git, lifecycle
from test_worktree_migration import legacy, migration


PATTERNS = ["packages/*/dist/", "*.tsbuildinfo", "web/dist", ".venv.sync.sha256", "node_modules/"]
OUTPUTS = ["packages/a/dist/index.js", "packages/b/dist/deep/map", "packages/a/cache.tsbuildinfo",
           "web/dist/bundle.js", ".venv.sync.sha256", "stray/node_modules/pkg/index.js"]


def make_outputs(path: Path) -> None:
    for name in OUTPUTS:
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("rebuild me", encoding="utf-8")


def test_outputs_use_gitignore_globs_and_preserve_unmatched(git_repo) -> None:
    git_repo.commit_file(".gitignore", "dist/\n*.tsbuildinfo\n.venv.sync.sha256\nnode_modules/\nlocal.secret\n")
    make_outputs(git_repo.path)
    (git_repo.path / "local.secret").write_text("unique", encoding="utf-8")
    reproducible, unique = safety.outputs(git_repo.path, [], PATTERNS)
    assert set(reproducible) == set(OUTPUTS)
    assert unique == ["local.secret"]
    assert safety.outputs(git_repo.path, ["web/dist"])[0] == ["web/dist/bundle.js"]


@pytest.mark.parametrize("pattern", ["/dist/**", "C:/dist/*", "../dist", "packages/../dist", "**/../*", "!../secret"])
def test_rebuildable_rejects_escaping_globs(git_repo, pattern: str) -> None:
    raw = {"pool": {"setup": [{"id": "build", "argv": ["build"]}], "rebuildable": [pattern]}}
    with pytest.raises(ValueError, match="inside the worktree|positive relative"):
        validate_config(raw, git_repo.path)


@pytest.mark.parametrize("pattern", ["tracked.txt", "*.txt", "**/*.txt", "packages/*/dist/", "packages/**"])
def test_rebuildable_rejects_tracked_matches(git_repo, pattern: str) -> None:
    (git_repo.path / "packages/a/dist").mkdir(parents=True)
    git_repo.commit_file("tracked.txt", "unique")
    git_repo.commit_file("packages/a/dist/keep.txt", "also unique")
    raw = {"pool": {"setup": [{"id": "build", "argv": ["build"]}], "rebuildable": [pattern]}}
    with pytest.raises(ValueError, match="rebuildable glob matches tracked"):
        validate_config(raw, git_repo.path)


@pytest.mark.parametrize("unique", [False, True], ids=["only-rebuildable", "unmatched-secret"])
def test_migration_rebuildable_safety_and_resume(migration, unique: bool) -> None:
    obj = migration
    ignore = obj.repo / ".gitignore"
    ignore.write_text(ignore.read_text() + "dist/\n*.tsbuildinfo\n.venv.sync.sha256\nnode_modules/\n")
    approved = obj.settings.raw
    approved["pool"]["rebuildable"] = PATTERNS
    obj.configure(approved)
    git(obj.repo, "add", ".gitignore", ".claude/bootstrap.json")
    git(obj.repo, "commit", "-qm", "approve rebuildable outputs")
    git(obj.repo, "push", "origin", "main")
    path = legacy(obj, "build-leftovers")
    make_outputs(path)
    if unique:
        (path / "local.secret").write_text("preserve", encoding="utf-8")
    obj.migrate()
    result = obj.migrate(apply=True)
    record = obj.read_records()[0]
    assert record["state"] == ("parked" if unique else "removed")
    assert record["safety"]["safe"] is not unique
    assert set(record["safety"]["reproducible"]) == set(OUTPUTS)
    assert record["safety"]["unique_ignored"] == (["local.secret"] if unique else [])
    assert path.exists() is unique
    resumed = obj.migrate(apply=True)
    before, after = result["candidates"][0], resumed["candidates"][0]
    assert {k: v for k, v in after.items() if k != "safety"} == {k: v for k, v in before.items() if k != "safety"}
    assert {k: v for k, v in after["safety"].items() if k != "at"} == {k: v for k, v in before["safety"].items() if k != "at"}
    assert resumed["parked_recheck"]["rechecked"] == int(unique)
    if unique:
        assert (path / "local.secret").read_text() == "preserve"
        assert all((path / name).exists() for name in OUTPUTS)


def test_proposal_does_not_approve_rebuildable(lifecycle) -> None:
    assert not lifecycle.configure()["proposal"]["pool"].get("rebuildable")
