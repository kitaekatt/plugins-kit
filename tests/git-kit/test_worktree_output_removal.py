"""Run: uv run --extra dev pytest tests/git-kit/test_worktree_output_removal.py -q.

Triggers: large ignored trees, partial approvals, symlink parents, delete failure,
and work arriving after a durable removal reservation. All repositories are fixtures.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import worktree_safety as safety
from test_worktree_lifecycle import git, lifecycle
from test_worktree_migration import legacy, migration


def test_large_outputs_use_one_delete_per_directory(git_repo, monkeypatch) -> None:
    git_repo.commit_file(".gitignore", "node_modules/\ndist/\n*.tsbuildinfo\n")
    path = git_repo.path.parent / "large-worktree"
    git(git_repo.path, "worktree", "add", "--detach", str(path))
    for directory in ("node_modules", "packages/x/dist"):
        for index in range(1000):
            target = path / directory / f"pkg-{index // 20}" / f"file-{index}"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("rebuild")
    (path / "build.tsbuildinfo").write_text("cache")
    reproducible, unique = safety.outputs(path, [], ["node_modules/", "packages/*/dist/", "*.tsbuildinfo"])
    assert not unique and len(reproducible) == 2001
    run, unlink = subprocess.run, Path.unlink
    recursive, singles, git_calls = [], [], []

    def counted_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        if argv[0] == "git":
            git_calls.append(argv)
        else:
            recursive.append(argv)
        return run(argv, **kwargs)

    def counted_unlink(target: Path, *args, **kwargs) -> None:
        singles.append(target)
        unlink(target, *args, **kwargs)

    monkeypatch.setattr(safety.subprocess, "run", counted_run)
    monkeypatch.setattr(Path, "unlink", counted_unlink)
    safety.remove_outputs(path, reproducible)
    assert len(recursive) == 2
    assert singles == [path / "build.tsbuildinfo"]
    assert len(git_calls) <= 6
    assert all(not (path / name).exists() for name in ("node_modules", "packages/x/dist", "build.tsbuildinfo"))


def test_windows_delete_quotes_path_and_propagates_failure(tmp_path, monkeypatch) -> None:
    target = tmp_path / "build %cache% & more!"

    def failed_run(argv: str | list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        command = argv if isinstance(argv, str) else subprocess.list2cmdline(argv)
        assert '"%GIT_KIT_OUTPUT_PATH%"' in command and '\\"' not in command
        assert kwargs["env"]["GIT_KIT_OUTPUT_PATH"] == str(target)
        assert str(target) not in command
        assert kwargs["check"] is True
        raise subprocess.CalledProcessError(1, argv, stderr="delete denied")

    with monkeypatch.context() as patch:
        patch.setattr(safety.sys, "platform", "win32")
        patch.setattr(safety.subprocess, "run", failed_run)
        with pytest.raises(subprocess.CalledProcessError, match="non-zero exit status"):
            safety.remove_directory(target)


@pytest.mark.parametrize("parent", ["output-parent", "worktree-root"])
def test_output_removal_refuses_symlinked_parent(git_repo, tmp_path, parent: str) -> None:
    git_repo.commit_file(".gitignore", "output/\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    if parent == "output-parent":
        (outside / "keep").write_text("unique")
        (git_repo.path / "output").symlink_to(outside, target_is_directory=True)
        root, name, kept = git_repo.path, "output/keep", outside / "keep"
    else:
        root = outside / "linked-worktree"
        root.symlink_to(git_repo.path, target_is_directory=True)
        (git_repo.path / "output").mkdir()
        kept = git_repo.path / "output/keep"
        kept.write_text("unique")
        name = "output/keep"
    with pytest.raises(ValueError, match="symlink"):
        safety.remove_outputs(root, [name])
    assert kept.read_text() == "unique"


@pytest.mark.parametrize("protected", ["unique-ignored", "untracked", "tracked"])
def test_partial_output_approval_preserves_other_data(git_repo, protected: str) -> None:
    git_repo.commit_file(".gitignore", "output/\n")
    directory = git_repo.path / "output"
    directory.mkdir()
    (directory / "approved").write_text("rebuild")
    (directory / "keep").write_text("unique")
    if protected == "tracked":
        git(git_repo.path, "add", "-f", "output/keep")
        git(git_repo.path, "commit", "-qm", "keep tracked data")
    elif protected == "untracked":
        git_repo.commit_file(".gitignore", "output/*\n!output/keep\n")
    safety.remove_outputs(git_repo.path, ["output/approved"])
    assert not (directory / "approved").exists()
    assert (directory / "keep").read_text() == "unique"


@pytest.mark.parametrize("operation", ["maintenance", "migrate", "parked-recheck"])
def test_delete_failure_parks_only_failed_record(migration, monkeypatch, operation: str) -> None:
    obj = migration
    if operation == "migrate":
        for name in ("first", "second"):
            path = legacy(obj, name)
            (path / "output").mkdir()
            (path / "output/build").write_text("rebuild")
        inventory = obj.migrate()
        failed_path = Path(inventory["candidates"][0]["path"])
    else:
        config = obj.settings.raw
        config["pool"]["size"] = 3
        obj.configure(config)
        obj.maintain()
        for record in obj.read_records()[:2]:
            record["state"] = "parked" if operation == "parked-recheck" else "released"
            obj.save(record)
        config["pool"]["size"] = 1
        obj.configure(config)
        failed_path = Path(next(r for r in obj.read_records() if r["state"] != "ready")["path"])
    remove = safety.remove_outputs
    failures = []

    def fail_one(path: Path, names: list[str]) -> None:
        if path == failed_path:
            failures.append(path)
            raise subprocess.CalledProcessError(1, ["rm", "-rf", "--", str(path / "output")], stderr="delete denied")
        remove(path, names)

    monkeypatch.setattr(safety, "remove_outputs", fail_one)
    result = obj.migrate(apply=True) if operation == "migrate" else obj.maintain()
    records = [r for r in obj.read_records() if r["state"] != "ready"]
    failed = next(r for r in records if r["path"] == str(failed_path))
    assert failed["state"] == "parked" and failed["worker"] is None
    assert "delete denied" in str(failed["safety"]["blockers"])
    assert failed_path.exists() and (failed_path / "output").exists()
    assert len(records) == 2 and sum(r["state"] == "removed" for r in records) == 1
    assert failures == [failed_path]
    assert result.get("outcome", result.get("phase")) in {"complete", "metadata"}


def test_maintenance_removal_rechecks_after_reservation(lifecycle, monkeypatch) -> None:
    obj = lifecycle
    config = obj.settings.raw
    config["pool"]["size"] = 2
    obj.configure(config)
    obj.maintain()
    record = obj.read_records()[0]
    record["state"] = "released"
    obj.save(record)
    config["pool"]["size"] = 1
    obj.configure(config)
    path, save = Path(record["path"]), obj.save

    def reserve(value: dict) -> None:
        save(value)
        if value["operation"].get("name") == "remove" and value["operation"]["phase"] == "reserved":
            (path / "new-work").write_text("arrived after reservation")

    monkeypatch.setattr(obj, "save", reserve)
    obj.maintain()
    assert obj.get(record["id"])["state"] == "parked"
    assert (path / "new-work").read_text() == "arrived after reservation"
    assert (path / "output").exists()
