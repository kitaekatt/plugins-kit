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
    # The repo-wide invariant gates deliberately judge the REAL tree; point
    # them at the fixture so these tests neither read nor depend on it.
    monkeypatch.setattr(publish, "REAL_PLUGINS_DIR", root / "plugins")
    monkeypatch.setattr(publish, "GENERATE_ORCHESTRATION_PY",
                        _stub_script(tmp_path, "orchestration_ok.py", 0))
    return root


def _stub_script(tmp_path: Path, name: str, exit_code: int, message: str = "") -> Path:
    """A stand-in for a checker CLI publish shells out to.

    The real generate_orchestration.py reads its three inputs from module-level
    constants pinned to this repo, so it cannot be aimed at a fixture. What
    publish.py owns is the WIRING -- that a non-zero check refuses the publish
    and surfaces the checker's output -- and that is what these stubs exercise.
    """
    script = tmp_path / name
    script.write_text(
        "import sys\n"
        f"sys.stdout.write({message!r})\n"
        f"sys.exit({exit_code})\n")
    return script


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


class TestDevOnlyExclusion:
    """`published: false` is a standing decision that a plugin does not ship.

    So its commits are EXCLUDED from the release rather than being a reason to
    refuse the whole publish. The old behaviour refused and told the operator to
    cherry-pick by hand, which let one team's in-flight work block another
    team's finished work and moved the filtering to a human doing it against
    master with no verification. The one case that still refuses is a commit
    that mixes dev-only and shippable files, where either choice is wrong.
    """

    def test_dev_only_commits_are_excluded_not_refused(self, repo):
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")
        (repo / "plugins" / "dev-kit" / "feature.py").write_text("wip\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "dev-kit: experimental feature")

        bumps, excluded = publish.preflight()

        assert bumps == ["pub-kit: 1.0.0 -> 1.1.0"]
        assert len(excluded) == 1
        assert set(next(iter(excluded.values()))) == {"dev-kit"}

    def test_a_mixed_commit_still_refuses(self, repo):
        """Excluding it would withhold shippable work; including it would put
        dev-only files on master. Neither is safe to pick automatically."""
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")
        (repo / "plugins" / "dev-kit" / "feature.py").write_text("wip\n")
        (repo / "plugins" / "pub-kit" / "shipped.py").write_text("real\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "dev-kit wip plus pub-kit change")

        with pytest.raises(publish.PublishError) as exc:
            publish.preflight()

        message = str(exc.value)
        assert "BOTH" in message
        assert "dev-kit" in message
        assert "Split the commit" in message

    def test_allows_a_clean_publish_ready_range(self, repo):
        """A range with no dev-only commits excludes nothing."""
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")

        bumps, excluded = publish.preflight()

        assert bumps == ["pub-kit: 1.0.0 -> 1.1.0"]
        assert excluded == {}

    def test_dev_only_plugins_alone_do_not_trip_the_guard(self, repo):
        """A dev-only plugin merely EXISTING is normal; only commits touching
        one in the publish range are excluded."""
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")
        bumps, excluded = publish.preflight()
        assert bumps
        assert excluded == {}

    def test_allow_dev_only_ships_the_named_plugins_commits(self, repo):
        """--allow-dev-only is the operator's explicit decision to ship
        finished dev-only work master's tree needs -- it moves those commits
        from excluded to included."""
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")
        (repo / "plugins" / "dev-kit" / "feature.py").write_text("done\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "dev-kit: finished refactor slice")

        bumps, excluded = publish.preflight(allow_dev_only={"dev-kit"})

        assert bumps == ["pub-kit: 1.0.0 -> 1.1.0"]
        assert excluded == {}

    def test_allow_dev_only_does_not_cover_unnamed_plugins(self, repo):
        """Allowing one dev-only plugin must not silently include another's."""
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")
        _write_manifest(repo, "other-kit", "0.1.0", published=False)
        (repo / "plugins" / "other-kit" / "done.py").write_text("done\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "other-kit done")
        (repo / "plugins" / "dev-kit" / "feature.py").write_text("wip\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "dev-kit experimental")

        bumps, excluded = publish.preflight(allow_dev_only={"other-kit"})

        assert bumps == ["pub-kit: 1.0.0 -> 1.1.0"]
        # other-kit rode along; dev-kit is still held back.
        assert len(excluded) == 1
        assert set(next(iter(excluded.values()))) == {"dev-kit"}

    def test_allow_dev_only_rejects_non_dev_only_names(self, repo):
        """Naming a published (or unknown) plugin is an operator error, not a
        no-op -- refuse loudly."""
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")

        with pytest.raises(publish.PublishError, match="not dev-only"):
            publish.preflight(allow_dev_only={"pub-kit"})


class TestRepoInvariantGates:
    """The checks that exist as ESCAPABLE pre-commit hooks (--no-verify,
    PLUGINS_KIT_SKIP_BUMP_CHECK=1) and so were enforced nowhere before the
    publish gate re-ran them. Skipping them on dev is sanctioned; shipping the
    result is not."""

    def test_pyproject_drift_blocks_a_publish(self, repo):
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")
        (repo / "plugins" / "pub-kit" / "pyproject.toml").write_text(
            '[project]\nname = "pub-kit"\nversion = "1.0.0"\n')
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "pub-kit: add pyproject (drifted)")

        with pytest.raises(publish.PublishError) as exc:
            publish.preflight()

        message = str(exc.value)
        assert "pyproject.toml" in message
        assert "pub-kit: pyproject.toml=1.0.0 plugin.json=1.1.0" in message

    def test_pyproject_gate_ignores_the_commit_time_escape_hatch(self, repo, monkeypatch):
        """PLUGINS_KIT_SKIP_BUMP_CHECK is a commit-time allowance. Honouring it
        here would leave the invariant enforced nowhere at all."""
        monkeypatch.setenv("PLUGINS_KIT_SKIP_BUMP_CHECK", "1")
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")
        (repo / "plugins" / "pub-kit" / "pyproject.toml").write_text(
            '[project]\nname = "pub-kit"\nversion = "1.0.0"\n')
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "pub-kit: add pyproject (drifted)")

        with pytest.raises(publish.PublishError, match="pyproject.toml"):
            publish.preflight()

    def test_pyproject_in_sync_passes(self, repo):
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")
        (repo / "plugins" / "pub-kit" / "pyproject.toml").write_text(
            '[project]\nname = "pub-kit"\nversion = "1.1.0"\n')
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "pub-kit: add pyproject")

        assert publish.preflight()[0] == ["pub-kit: 1.0.0 -> 1.1.0"]

    def test_orchestration_drift_blocks_a_publish(self, repo, tmp_path, monkeypatch):
        monkeypatch.setattr(
            publish, "GENERATE_ORCHESTRATION_PY",
            _stub_script(tmp_path, "orchestration_drift.py", 1,
                         "-  tier: low\n+  tier: high\n"))
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")

        with pytest.raises(publish.PublishError) as exc:
            publish.preflight()

        message = str(exc.value)
        assert "orchestration policy is not current" in message
        assert "--write" in message          # says how to fix it
        assert "+  tier: high" in message    # surfaces the checker's own diff


class TestPerPluginBumpGate:
    """_require_version_bump only asserts SOMETHING was bumped. Every published
    plugin whose files changed must be bumped, or its changes ship under a
    version string the cache will never refetch (gotcha 3)."""

    def test_changed_plugin_without_a_bump_blocks_and_is_named(self, repo):
        _write_manifest(repo, "other-kit", "0.1.0")
        (repo / "plugins" / "pub-kit" / "engine.py").write_text("changed\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "pub-kit: engine change; other-kit 0.1.0")

        with pytest.raises(publish.PublishError) as exc:
            publish.preflight()

        message = str(exc.value)
        assert "pub-kit: files changed, still 1.0.0" in message
        assert "other-kit" not in message  # new plugin, nothing to diverge from

    def test_reports_every_offender_at_once(self, repo):
        _write_manifest(repo, "other-kit", "0.1.0")
        _write_manifest(repo, "third-kit", "3.0.0")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "add other-kit and third-kit")
        _git(repo, "push", "-q", "origin", "dev")
        _git(repo, "checkout", "-q", "master")
        _git(repo, "merge", "-q", "--ff-only", "dev")
        _git(repo, "push", "-q", "origin", "master")
        _git(repo, "checkout", "-q", "dev")

        _bump(repo, "other-kit", "0.2.0", "other-kit 0.2.0")
        (repo / "plugins" / "pub-kit" / "engine.py").write_text("changed\n")
        (repo / "plugins" / "third-kit" / "lib.py").write_text("changed\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "pub-kit + third-kit changes, no bumps")

        with pytest.raises(publish.PublishError) as exc:
            publish.preflight()

        message = str(exc.value)
        assert "pub-kit: files changed, still 1.0.0" in message
        assert "third-kit: files changed, still 3.0.0" in message

    def test_changed_plugin_with_a_bump_passes(self, repo):
        (repo / "plugins" / "pub-kit" / "engine.py").write_text("changed\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "pub-kit: engine change")
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")

        assert publish.preflight()[0] == ["pub-kit: 1.0.0 -> 1.1.0"]

    def test_dev_only_plugin_changed_without_a_bump_does_not_block(self, repo):
        """A published: false plugin has no consumers and no cache entry, so
        there is nothing for a stale version string to strand."""
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")
        (repo / "plugins" / "dev-kit" / "feature.py").write_text("wip\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "dev-kit: experimental feature")

        # --allow-dev-only is what lets its commits into the range at all; the
        # per-plugin bump gate must not then demand a bump it cannot mean.
        assert publish.preflight(allow_dev_only={"dev-kit"})[0] == [
            "pub-kit: 1.0.0 -> 1.1.0"]


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


class TestIndexScopeGuard:
    """The page ships to a PUBLIC repo, so a dropped --marketplace does not merely
    misreport -- it publishes every other marketplace installed on the generating
    machine. Checked against the artifact, not trusted to the invocation."""

    def _page(self, plugins, order=("plugins-kit",)):
        data = {"plugins": plugins, "marketplace_order": list(order)}
        return f"<script>\nconst data = {json.dumps(data)};\nfunction el() {{}}\n</script>"

    def test_clean_page_passes(self):
        page = self._page([{"marketplace": "plugins-kit", "name": "pub-kit"}])
        assert publish.check_index_scope(page) == []

    def test_foreign_marketplace_flagged(self):
        page = self._page(
            [{"marketplace": "plugins-kit", "name": "pub-kit"},
             {"marketplace": "private-plugins", "name": "secret-kit"}],
            order=("plugins-kit", "private-plugins"))

        problems = publish.check_index_scope(page)

        assert any("private-plugins" in p and "--marketplace" in p for p in problems)

    def test_foreign_marketplace_in_order_only_flagged(self):
        """An empty foreign column still names the marketplace on the page."""
        page = self._page([{"marketplace": "plugins-kit", "name": "pub-kit"}],
                          order=("plugins-kit", "private-plugins"))
        assert any("private-plugins" in p for p in publish.check_index_scope(page))

    def test_embedded_state_flagged(self):
        page = self._page([{"marketplace": "plugins-kit", "name": "pub-kit", "state": "on"}])
        assert any("--public" in p for p in publish.check_index_scope(page))

    def test_unparseable_page_is_a_problem_not_a_pass(self):
        """An output-shape change must fail loudly; a silent pass would retire the
        guard without anyone noticing."""
        assert publish.check_index_scope("<html>no data block</html>")
