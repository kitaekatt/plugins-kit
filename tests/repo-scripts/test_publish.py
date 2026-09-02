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
from types import SimpleNamespace

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
    # A `dev/` directory alongside a `dev` BRANCH, as the real repo has. Every
    # `git diff <ref> dev` without a trailing "--" is then ambiguous and git
    # refuses it outright, which is a publish that fails on its first command.
    (root / "dev").mkdir()
    (root / "dev" / "notes.md").write_text("shared dev scratch\n")

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


class TestDirtyGateIgnoresDevOnlyPlugins:
    """A dev-only plugin's uncommitted work must not block a publish.

    It reaches no consumer on two independent counts -- the plugin is absent
    from marketplace.json, and the change is uncommitted -- so refusing on it
    makes a shared tree unpublishable for a reason the operator cannot act on
    without committing another session's half-finished work.
    """

    @staticmethod
    def _bump_only_pub_kit(repo: Path) -> None:
        """Bump pub-kit WITHOUT `git add -A`.

        The shared _bump helper stages everything, which would sweep this
        class's deliberately-dirty dev-only file into the commit and trip the
        mixed-commit refusal -- a different gate than the one under test.
        """
        manifest = repo / "plugins" / "pub-kit" / ".claude-plugin" / "plugin.json"
        data = json.loads(manifest.read_text())
        data["version"] = "1.1.0"
        manifest.write_text(json.dumps(data, indent=2) + "\n")
        _git(repo, "add", "--", "plugins/pub-kit/.claude-plugin/plugin.json")
        _git(repo, "commit", "-qm", "pub-kit 1.1.0")

    def test_dirty_dev_only_plugin_does_not_refuse(self, repo):
        (repo / "plugins" / "dev-kit" / "scratch.py").write_text("wip\n")
        self._bump_only_pub_kit(repo)
        bumps, _excluded = publish.preflight()
        assert any("pub-kit" in bump for bump in bumps)

    def test_dirty_dev_only_tests_do_not_refuse(self, repo):
        tests = repo / "tests" / "dev-kit"
        tests.mkdir(parents=True, exist_ok=True)
        (tests / "test_wip.py").write_text("# wip\n")
        self._bump_only_pub_kit(repo)
        bumps, _excluded = publish.preflight()
        assert any("pub-kit" in bump for bump in bumps)

    def test_dirty_dev_only_paths_are_not_even_reported(self, repo):
        """The operator is told nothing about work that cannot affect anyone."""
        (repo / "plugins" / "dev-kit" / "scratch.py").write_text("wip\n")
        (repo / "scratch.txt").write_text("uncommitted")
        with pytest.raises(publish.PublishError) as excinfo:
            publish.preflight()
        message = str(excinfo.value)
        assert "scratch.txt" in message
        assert "dev-kit" not in message

    def test_a_published_plugin_still_refuses(self, repo):
        """The gate keeps working where it protects someone."""
        (repo / "plugins" / "pub-kit" / "scratch.py").write_text("wip\n")
        with pytest.raises(publish.PublishError, match="working tree is dirty"):
            publish.preflight()

    def test_a_renamed_dev_only_path_is_judged_by_its_destination(self, repo):
        src = repo / "plugins" / "dev-kit" / "moved.py"
        src.write_text("x\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "dev-kit file")
        # Bump BEFORE the rename: `git mv` stages it, and `git commit` with no
        # pathspec takes the whole index, so a rename staged first rides into
        # the pub-kit commit and trips the mixed-commit gate instead.
        self._bump_only_pub_kit(repo)
        _git(repo, "mv", "plugins/dev-kit/moved.py", "plugins/dev-kit/renamed.py")
        bumps, _excluded = publish.preflight()
        assert any("pub-kit" in bump for bump in bumps)

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

    def test_refuses_master_only_content(self, repo):
        """A hotfix committed straight on master would be DISCARDED by a
        publish, which takes dev's version of every shippable file. That is a
        reconcile, and it is the only thing master being 'ahead' can mean that
        actually costs anything."""
        _git(repo, "checkout", "-q", "master")
        (repo / "hotfix.txt").write_text("landed straight on master\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "hotfix")
        _git(repo, "push", "-q", "origin", "master")
        _git(repo, "checkout", "-q", "dev")

        with pytest.raises(publish.PublishError,
                           match="holds content dev does not"):
            publish.preflight()

    def test_master_commits_dev_lacks_are_fine_without_content_drift(self, repo):
        """The regression this whole design exists for. A filtered release
        leaves master carrying a commit dev will never see, permanently. The
        old ancestry check read that as a reconcile and refused EVERY later
        publish, even though master holds nothing dev lacks."""
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")
        _git(repo, "push", "-q", "origin", "dev")
        # A commit on master with dev's exact content: ahead by a sha, not by
        # anything anyone would lose.
        shipped = _git(repo, "rev-parse", "dev")
        _git(repo, "checkout", "-q", "--detach", "origin/master")
        _git(repo, "read-tree", "--reset", "-u", "dev")
        _git(repo, "commit", "-qm",
             f"publish: projected\n\nPublished-From: {shipped}")
        _git(repo, "push", "-q", "origin", "HEAD:refs/heads/master")
        _git(repo, "checkout", "-q", "dev")
        _bump(repo, "pub-kit", "1.2.0", "pub-kit 1.2.0")

        publish.preflight()  # must not raise


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

    def test_shipping_dev_only_commits_is_the_cli_default(self, repo):
        """`publish.py --check` with no flags ships every dev-only plugin's
        commits, including a MIXED one that used to be refused outright.

        preflight() still takes an explicit allow-set -- the default lives at
        the CLI, which is why this drives main() rather than preflight().
        """
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")
        # One commit touching a dev-only plugin AND a shippable file: the exact
        # shape _refuse_mixed_dev_only_commit() exists for.
        (repo / "plugins" / "dev-kit" / "feature.py").write_text("done\n")
        (repo / "plugins" / "pub-kit" / "shared.py").write_text("shared\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "cross-plugin slice")

        assert publish.main(["--check"]) == 0

    def test_exclude_dev_only_restores_the_refusal(self, repo):
        """Asking for an exclusion is asking for a divergent master tree, and a
        mixed commit is where that cannot be honoured silently."""
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")
        (repo / "plugins" / "dev-kit" / "feature.py").write_text("done\n")
        (repo / "plugins" / "pub-kit" / "shared.py").write_text("shared\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "cross-plugin slice")

        assert publish.main(["--check", "--exclude-dev-only", "dev-kit"]) != 0

    def test_exclude_dev_only_rejects_non_dev_only_names(self, repo):
        """Naming a published (or unknown) plugin is an operator error."""
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")

        assert publish.main(["--check", "--exclude-dev-only", "pub-kit"]) != 0

    def test_allow_dev_only_rejects_non_dev_only_names(self, repo):
        """Naming a published (or unknown) plugin is an operator error, not a
        no-op -- refuse loudly."""
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")

        with pytest.raises(publish.PublishError, match="not dev-only"):
            publish.preflight(allow_dev_only={"pub-kit"})


class TestProjectionRelease:
    """The filtered release, end to end against a real bare origin.

    This replaced a cherry-pick replay that could not survive its own output:
    replaying gave each shipped commit a new sha, so the originals stayed in
    the range forever and the next publish re-picked work master already had,
    dying on a duplication conflict. The projection is idempotent by
    construction, which is what these assert.
    """

    def _range(self, repo):
        (repo / "plugins" / "dev-kit" / "notes.md").write_text("dev-only\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "dev-kit: notes")
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")
        return publish.preflight()[1]

    def test_ships_dev_content_and_holds_the_dev_only_plugin_back(self, repo):
        excluded = self._range(repo)
        publish.push_and_merge(excluded)

        assert publish.version_at("origin/master", "pub-kit") == "1.1.0"
        assert publish.version_at("origin/master", "dev-kit") == "0.1.0"
        on_master = _git(repo, "ls-tree", "-r", "--name-only", "origin/master")
        assert "plugins/dev-kit/notes.md" not in on_master.splitlines()
        assert "plugins/dev-kit/.claude-plugin/plugin.json" in on_master.splitlines()

    def test_stamps_the_dev_commit_it_was_built_from(self, repo):
        excluded = self._range(repo)
        expected = _git(repo, "rev-parse", "dev")
        publish.push_and_merge(excluded)

        message = _git(repo, "log", "-1", "--format=%B", "origin/master")
        assert f"Published-From: {expected}" in message

    def test_a_second_publish_of_the_same_work_is_a_no_op(self, repo):
        """The regression. A replay conflicted here; a projection has nothing
        left to do, and preflight sees an empty range rather than commits it
        already shipped."""
        excluded = self._range(repo)
        publish.push_and_merge(excluded)
        before = _git(repo, "rev-parse", "origin/master")

        with pytest.raises(publish.PublishError, match="nothing to publish"):
            publish.preflight()
        assert _git(repo, "rev-parse", "origin/master") == before

    def test_verify_passes_after_a_projection(self, repo):
        excluded = self._range(repo)
        publish.push_and_merge(excluded)

        leaked = [p for p in publish.verify()
                  if "differs from" in p or "did not land" in p]
        assert leaked == []


class TestRangeBase:
    """Where `..dev` starts.

    A projection release stamps the dev sha it was built from onto master's
    commit. Reading that back is what stops the range -- and therefore the
    dev-only exclusion list and the bump gates -- from growing without bound
    once master's history stops being an ancestor of dev's.
    """

    def test_falls_back_to_master_without_a_trailer(self, repo):
        assert publish.range_base() == "origin/master"

    def test_reads_the_published_from_trailer(self, repo):
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")
        _git(repo, "push", "-q", "origin", "dev")
        shipped = _git(repo, "rev-parse", "dev")

        _git(repo, "checkout", "-q", "--detach", "origin/master")
        _git(repo, "read-tree", "--reset", "-u", "dev")
        _git(repo, "commit", "-qm",
             f"publish: projected\n\nPublished-From: {shipped}")
        _git(repo, "push", "-q", "origin", "HEAD:refs/heads/master")
        _git(repo, "checkout", "-q", "dev")
        _git(repo, "fetch", "-q", "origin")

        assert publish.range_base() == shipped
        assert publish._range_commits() == []

    def test_ignores_a_trailer_naming_an_unreachable_commit(self, repo):
        """Fail OPEN: an unusable trailer must widen the range back to plain
        ancestry, never narrow it -- a narrower range silently drops a commit
        from the release."""
        _git(repo, "checkout", "-q", "--detach", "origin/master")
        _git(repo, "commit", "-q", "--allow-empty", "-m",
             "publish: projected\n\nPublished-From: " + "0" * 40)
        _git(repo, "push", "-q", "origin", "HEAD:refs/heads/master")
        _git(repo, "checkout", "-q", "dev")
        _git(repo, "fetch", "-q", "origin")

        assert publish.range_base() == "origin/master"


class TestHeldBackPaths:
    """The projection takes dev's tree, so every dev-only file needs putting
    back -- and the two halves need opposite treatment."""

    def test_splits_by_whether_master_has_the_file(self, repo):
        (repo / "plugins" / "dev-kit" / "new.py").write_text("unshipped\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "dev-kit: new file")

        on_master, dev_new = publish._held_back_paths({"dev-kit"})

        assert "plugins/dev-kit/.claude-plugin/plugin.json" in on_master
        assert dev_new == ["plugins/dev-kit/new.py"]

    def test_no_dev_only_plugins_holds_nothing_back(self, repo):
        assert publish._held_back_paths(set()) == ([], [])


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


class TestChangedPluginsUsesNetDiff:
    """A plugin master already holds byte-for-byte is not "changed".

    After a filtered release master carries cherry-picked equivalents of dev
    commits. Those commits stay in the publish range and still name the
    plugin's files, so a commit-walk alone reports them as unbumped -- and the
    demanded version bump would ship nothing, burning a version number and
    pushing a no-op refetch to every consumer.
    """

    def test_identical_plugin_is_not_reported_as_changed(self, monkeypatch):
        import publish

        monkeypatch.setattr(publish, "local_plugins",
                            lambda: {"alpha": {"version": "1.0.0"}})
        monkeypatch.setattr(publish, "_range_commits", lambda: ["deadbee"])
        monkeypatch.setattr(publish, "_commit_files",
                            lambda sha: ["plugins/alpha/file.py"])
        # git diff --quiet exits 0 -> no net difference.
        monkeypatch.setattr(publish.subprocess, "run",
                            lambda *a, **k: SimpleNamespace(returncode=0))
        assert publish._changed_plugins() == set()

    def test_genuinely_differing_plugin_is_still_reported(self, monkeypatch):
        import publish

        monkeypatch.setattr(publish, "local_plugins",
                            lambda: {"alpha": {"version": "1.0.0"}})
        monkeypatch.setattr(publish, "_range_commits", lambda: ["deadbee"])
        monkeypatch.setattr(publish, "_commit_files",
                            lambda sha: ["plugins/alpha/file.py"])
        # exit 1 -> the trees differ, so the bump rule must still bite.
        monkeypatch.setattr(publish.subprocess, "run",
                            lambda *a, **k: SimpleNamespace(returncode=1))
        assert publish._changed_plugins() == {"alpha"}
