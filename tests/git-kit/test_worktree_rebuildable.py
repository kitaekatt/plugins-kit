"""Approved ignored outputs; run: uv run --extra dev pytest tests/git-kit/test_worktree_rebuildable.py -q.

Triggers: build leftovers, unmatched ignored data, unsafe glob approval, cutover resume.
"""

from __future__ import annotations

import json
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


def publish_settings(obj, tmp_path: Path, change) -> None:
    """Push a settings change from another clone, so the main checkout falls behind."""
    publisher = tmp_path / "publisher"
    git(obj.repo, "clone", "--branch", "main", obj.url, str(publisher))
    change(publisher)
    git(publisher, "add", "-A")
    git(publisher, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "publish settings")
    git(publisher, "push", "origin", "main")


def test_release_reads_rebuildable_from_upstream_not_stale_checkout(lifecycle, tmp_path) -> None:
    """Trigger: upstream approves a rebuildable glob; the main checkout was not pulled."""
    from test_worktree_lifecycle import holder

    obj = lifecycle

    def approve_build_cache(publisher: Path) -> None:
        ignore = publisher / ".gitignore"
        ignore.write_text(ignore.read_text() + "build-cache/\n", encoding="utf-8")
        manifest = publisher / ".claude/bootstrap.json"
        data = json.loads(manifest.read_text())
        data["git_kit"]["worktrees"]["pool"]["rebuildable"] = ["build-cache/"]
        manifest.write_text(json.dumps(data), encoding="utf-8")

    publish_settings(obj, tmp_path, approve_build_cache)
    stale = json.loads((obj.repo / ".claude/bootstrap.json").read_text())
    assert "rebuildable" not in stale["git_kit"]["worktrees"]["pool"]
    obj.maintain()
    taken = obj.take("stale-checkout", "s", process=holder())
    output = Path(taken["path"]) / "build-cache/index.js"
    output.parent.mkdir()
    output.write_text("rebuild me", encoding="utf-8")
    assert obj.release(taken["claim"])["state"] == "released"


def test_unreadable_upstream_settings_fail_loudly(lifecycle, tmp_path) -> None:
    """Trigger: upstream settings are not valid JSON while the checkout copy still is."""
    import worktree_lifecycle as lc

    obj = lifecycle
    publish_settings(obj, tmp_path, lambda publisher: (publisher / ".claude/bootstrap.json").write_text("{bad", encoding="utf-8"))
    git(obj.repo, "fetch", "origin")
    with pytest.raises(ValueError, match="refs/remotes/origin/main"):
        lc.Lifecycle(obj.repo)
