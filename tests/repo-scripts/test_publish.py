"""Tests for scripts/publish.py -- the marketplace release flow.

Every test drives a REAL temporary git repo (work tree + bare origin) rather
than mocking git. The whole value of this script is its refusals, and a refusal
that only works against a mock is not a refusal -- the failures it guards
against (publishing a dev-only plugin, a merge that isn't a fast-forward, a bump
that never happened) are all git-shaped.

The publish/verify paths are deliberately NOT exercised end-to-end here: they
push and merge. Preflight is the safety net, so preflight is what is covered.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "publish.py"


def _load_publish():
    spec = importlib.util.spec_from_file_location("publish", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publish = _load_publish()


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


def _write_manifest(root: Path, name: str, version: str, published: bool = True) -> None:
    manifest_dir = root / "plugins" / name / ".claude-plugin"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    data = {"name": name, "version": version}
    if not published:
        data["published"] = False
    (manifest_dir / "plugin.json").write_text(json.dumps(data, indent=2) + "\n")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A miniature plugins-kit: master + dev, a bare origin, two plugins.

    `pub-kit` is published; `dev-kit` is published: false -- the pair the
    dev-only refusal keys on.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "master")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")

    _write_manifest(root, "pub-kit", "1.0.0")
    _write_manifest(root, "dev-kit", "0.1.0", published=False)
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"plugins": [{"name": "pub-kit", "version": "1.0.0"}]}, indent=2))
    (root / "index.html").write_text('{"name": "pub-kit", "version": "1.0.0"}')

    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initial")
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-q", "origin", "master")
    _git(root, "checkout", "-qb", "dev")
    _git(root, "push", "-q", "origin", "dev")

    # The module resolves these at import from its own location; repoint them.
    monkeypatch.setattr(publish, "REPO_ROOT", root)
    monkeypatch.setattr(publish, "PLUGINS_DIR", root / "plugins")
    monkeypatch.setattr(publish, "MARKETPLACE_JSON",
                        root / ".claude-plugin" / "marketplace.json")
    monkeypatch.setattr(publish, "INDEX_HTML", root / "index.html")
    return root


def _bump(repo: Path, name: str, version: str, message: str) -> None:
    manifest = repo / "plugins" / name / ".claude-plugin" / "plugin.json"
    data = json.loads(manifest.read_text())
    data["version"] = version
    manifest.write_text(json.dumps(data, indent=2) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


class TestPreflightRefusals:
    def test_refuses_dirty_tree(self, repo):
        (repo / "scratch.txt").write_text("uncommitted")
        with pytest.raises(publish.PublishError, match="working tree is dirty"):
            publish.preflight()

    def test_refuses_wrong_branch(self, repo):
        _git(repo, "checkout", "-q", "master")
        with pytest.raises(publish.PublishError, match="not on dev"):
            publish.preflight()

    def test_refuses_when_nothing_to_publish(self, repo):
        with pytest.raises(publish.PublishError, match="nothing to publish"):
            publish.preflight()

    def test_refuses_merge_without_a_version_bump(self, repo):
        """The cache keys on version: a merge with no bump ships nothing, so
        shipping it would look like success and change nothing for users."""
        (repo / "plugins" / "pub-kit" / "README.md").write_text("docs only\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "docs: tweak")

        with pytest.raises(publish.PublishError, match="no published plugin's version differs"):
            publish.preflight()

    def test_refuses_non_fast_forward(self, repo):
        """master having commits dev lacks is a reconcile, not a publish."""
        _git(repo, "checkout", "-q", "master")
        (repo / "hotfix.txt").write_text("landed straight on master\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "hotfix")
        _git(repo, "push", "-q", "origin", "master")
        _git(repo, "checkout", "-q", "dev")

        with pytest.raises(publish.PublishError, match="would not fast-forward"):
            publish.preflight()


class TestDevOnlyRefusal:
    """The refusal that matters most: merging a dev-only plugin's commits puts
    its files on master. The marketplace regenerator filters the LISTING, but
    not the files -- so this must be caught before the merge."""

    def test_refuses_commits_touching_a_dev_only_plugin(self, repo):
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")
        (repo / "plugins" / "dev-kit" / "feature.py").write_text("wip\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "dev-kit: experimental feature")

        with pytest.raises(publish.PublishError) as exc:
            publish.preflight()

        message = str(exc.value)
        assert "dev-only" in message
        assert "dev-kit" in message
        assert "cherry-pick" in message
        assert "experimental feature" in message  # names the offending commit

    def test_allows_a_clean_publish_ready_range(self, repo):
        """The same range without the dev-only commit must pass -- otherwise the
        guard is just refusing everything."""
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")

        bumps = publish.preflight()

        assert bumps == ["pub-kit: 1.0.0 -> 1.1.0"]

    def test_dev_only_plugins_alone_do_not_trip_the_guard(self, repo):
        """A dev-only plugin merely EXISTING is normal; only commits touching
        one in the publish range are the problem."""
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")
        assert publish.preflight()  # dev-kit exists, untouched -> fine

    def test_allow_dev_only_ships_the_named_plugins_commits(self, repo):
        """--allow-dev-only is the operator's explicit decision to ship
        finished dev-only work master's tree needs."""
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")
        (repo / "plugins" / "dev-kit" / "feature.py").write_text("done\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "dev-kit: finished refactor slice")

        bumps = publish.preflight(allow_dev_only={"dev-kit"})

        assert bumps == ["pub-kit: 1.0.0 -> 1.1.0"]

    def test_allow_dev_only_does_not_cover_unnamed_plugins(self, repo):
        """Allowing one dev-only plugin must not silently wave through
        another's commits."""
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")
        _write_manifest(repo, "other-kit", "0.1.0", published=False)
        (repo / "plugins" / "other-kit" / "done.py").write_text("done\n")
        (repo / "plugins" / "dev-kit" / "feature.py").write_text("wip\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "other-kit done; dev-kit experimental")

        with pytest.raises(publish.PublishError, match="dev-kit"):
            publish.preflight(allow_dev_only={"other-kit"})

    def test_allow_dev_only_rejects_non_dev_only_names(self, repo):
        """Naming a published (or unknown) plugin is an operator error, not a
        no-op -- refuse loudly."""
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")

        with pytest.raises(publish.PublishError, match="not dev-only"):
            publish.preflight(allow_dev_only={"pub-kit"})


class TestVersionReads:
    def test_version_at_ref(self, repo):
        _bump(repo, "pub-kit", "2.0.0", "pub-kit 2.0.0")
        assert publish.version_at("origin/master", "pub-kit") == "1.0.0"
        assert publish.version_at("dev", "pub-kit") == "2.0.0"

    def test_version_at_ref_is_none_for_unknown_plugin(self, repo):
        assert publish.version_at("origin/master", "no-such-kit") is None

    def test_is_published_defaults_true(self):
        assert publish.is_published({"name": "x"})
        assert publish.is_published({"name": "x", "published": True})
        assert not publish.is_published({"name": "x", "published": False})


class TestVerify:
    def test_flags_marketplace_disagreeing_with_plugin_json(self, repo):
        """The drift the whole flow exists to prevent: a page/listing that
        disagrees with the manifest it was generated from."""
        _write_manifest(repo, "pub-kit", "9.9.9")

        problems = publish.verify()

        assert any("marketplace.json has pub-kit=1.0.0" in p for p in problems)
        assert any("index.html does not show pub-kit 9.9.9" in p for p in problems)

    def test_flags_a_dev_only_plugin_leaking_into_the_listing(self, repo):
        marketplace = repo / ".claude-plugin" / "marketplace.json"
        marketplace.write_text(json.dumps({"plugins": [
            {"name": "pub-kit", "version": "1.0.0"},
            {"name": "dev-kit", "version": "0.1.0"},
        ]}))

        problems = publish.verify()

        assert any("dev-only plugin dev-kit is listed" in p for p in problems)
