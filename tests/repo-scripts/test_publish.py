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
import random
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


class TestRangeBaseSurvivesNonReleaseCommits:
    """Master carries commits that are not releases; the boundary must survive.

    An infra-drift sync or a reconcile records no Published-From trailer
    because neither is a release. Reading only master's tip loses the boundary
    the moment one lands, and the loss is silent: range_base falls back to the
    ancient merge base, against which everything the last release shipped looks
    like master-side content, so a routine publish is refused as a reconcile.
    """

    @staticmethod
    def _project(repo: Path, published_from: str, subject: str = "publish") -> None:
        """Put a projection commit carrying the trailer on master."""
        _git(repo, "checkout", "-q", "master")
        (repo / "shipped.txt").write_text(f"from {published_from}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm",
             f"{subject}\n\n{publish.PUBLISHED_FROM} {published_from}")
        _git(repo, "push", "-q", "origin", "master")
        _git(repo, "checkout", "-q", "dev")

    def test_trailer_on_the_tip_is_found(self, repo):
        head = _git(repo, "rev-parse", "dev")
        self._project(repo, head)
        _git(repo, "fetch", "-q", "origin")
        assert publish.range_base() == head

    def test_trailer_below_a_non_release_commit_is_still_found(self, repo):
        head = _git(repo, "rev-parse", "dev")
        self._project(repo, head)
        _git(repo, "checkout", "-q", "master")
        (repo / "infra.txt").write_text("infra sync, not a release\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "infra sync: carry policy to master")
        _git(repo, "push", "-q", "origin", "master")
        _git(repo, "checkout", "-q", "dev")
        _git(repo, "fetch", "-q", "origin")
        assert publish.range_base() == head

    def test_no_trailer_anywhere_falls_back_to_master(self, repo):
        assert publish.range_base() == f"{publish.REMOTE}/{publish.MASTER_BRANCH}"

    def test_a_trailer_naming_a_non_ancestor_falls_back(self, repo):
        """A rewritten dev must not silently narrow the range."""
        self._project(repo, "0" * 40)
        _git(repo, "fetch", "-q", "origin")
        assert publish.range_base() == f"{publish.REMOTE}/{publish.MASTER_BRANCH}"


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


class TestMasterOnlyGuardAsksDevHistory:
    """Master receiving dev content after the base is not a reconcile.

    An infra sync or a hand reconcile carries dev content to master without
    recording a publish point, so master's blob differs from the base while
    being a state dev already holds. Comparing against the base alone reports
    those paths as master-only and refuses a publish that would discard
    nothing. What separates the two cases is whether master's blob is a state
    dev held AND one master did not choose over content dev wrote later -- mere
    presence in dev's history clears a master-side revert, which sits on
    earlier dev content on purpose.

    The last pair of tests is the hard one, because by content the two are the
    same picture: master sits on a state dev's tip has moved past. What
    separates them is which branch moved. Master giving up later dev content is
    a master-side decision a publish would undo; dev going back to content it
    published before is dev superseding its own work, and master still holding
    the later content it was handed loses nothing.
    """

    @staticmethod
    def _project(repo: Path) -> None:
        """Master takes dev's tree and records the dev commit it came from."""
        _git(repo, "push", "-q", "origin", "dev")
        shipped = _git(repo, "rev-parse", "dev")
        _git(repo, "checkout", "-q", "--detach", "origin/master")
        _git(repo, "read-tree", "--reset", "-u", "dev")
        _git(repo, "commit", "-qm",
             f"publish: projected\n\nPublished-From: {shipped}")
        _git(repo, "push", "-q", "origin", "HEAD:refs/heads/master")
        _git(repo, "checkout", "-q", "dev")
        _git(repo, "fetch", "-q", "origin")

    def _base(self, repo: Path) -> None:
        """Put a projection on master so range_base has a boundary to find."""
        _bump(repo, "pub-kit", "1.1.0", "pub-kit 1.1.0")
        self._project(repo)

    @staticmethod
    def _on_master(repo: Path, path: str, text: str, subject: str) -> None:
        _git(repo, "checkout", "-q", "master")
        _git(repo, "reset", "-q", "--hard", "origin/master")
        (repo / path).write_text(text)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", subject)
        _git(repo, "push", "-q", "origin", "master")
        _git(repo, "checkout", "-q", "dev")
        _git(repo, "fetch", "-q", "origin")

    def test_content_dev_already_carried_is_not_master_only(self, repo):
        """The false positive: an infra sync brings dev's content to master
        AFTER the recorded base, and dev then moves further ahead. Master's
        blob differs from the base's, but dev has held it, so a publish taking
        dev's version discards nothing."""
        self._base(repo)
        (repo / "shared.txt").write_text("dev wrote this\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "add shared.txt")
        _git(repo, "push", "-q", "origin", "dev")
        self._on_master(repo, "shared.txt", "dev wrote this\n",
                        "infra sync: carry shared.txt to master")
        (repo / "shared.txt").write_text("dev moved on\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "advance shared.txt")

        assert publish._master_only_paths() == []

    def test_content_dev_never_had_is_still_master_only(self, repo):
        """The refusal the guard exists for must survive the fix: a hotfix
        written straight on master has a blob dev's history never held."""
        self._base(repo)
        (repo / "shared.txt").write_text("dev wrote this\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "add shared.txt")
        _git(repo, "push", "-q", "origin", "dev")
        self._on_master(repo, "shared.txt", "hotfixed straight on master\n",
                        "hotfix shared.txt")

        assert publish._master_only_paths() == ["shared.txt"]

    def test_a_master_side_deletion_is_still_reported(self, repo):
        """No blob to look for, and a publish would resurrect the file. The
        operator judges it, as before the reachability check existed."""
        self._base(repo)
        _git(repo, "checkout", "-q", "master")
        _git(repo, "reset", "-q", "--hard", "origin/master")
        _git(repo, "rm", "-q", "dev/notes.md")
        _git(repo, "commit", "-qm", "drop the scratch notes")
        _git(repo, "push", "-q", "origin", "master")
        _git(repo, "checkout", "-q", "dev")
        _git(repo, "fetch", "-q", "origin")

        assert publish._master_only_paths() == ["dev/notes.md"]

    def test_content_dev_took_through_a_merge_counts(self, repo):
        """Dev can reach a state through a merge rather than a direct commit.
        The walk must follow the parent the merge actually took its content
        from, or content dev demonstrably holds reads as master-only."""
        self._base(repo)
        _git(repo, "checkout", "-qb", "side")
        (repo / "shared.txt").write_text("written on the side\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "side: add shared.txt")
        _git(repo, "checkout", "-q", "dev")
        _git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
        _git(repo, "branch", "-q", "-D", "side")
        _git(repo, "push", "-q", "origin", "dev")
        self._on_master(repo, "shared.txt", "written on the side\n",
                        "infra sync: carry shared.txt to master")
        # Dev must move on, or the path is identical on both sides and never
        # reaches the guard at all.
        (repo / "shared.txt").write_text("dev moved on\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "advance shared.txt")

        assert publish._master_only_paths() == []

    def test_a_master_side_revert_is_master_only(self, repo):
        """The retraction path, which the root CLAUDE.md documents as the way
        to withdraw a bad publish: revert on master. Dev ships v1, an infra
        sync carries it to master, dev ships a broken v2, that is synced too,
        and master is then reverted to v1. Master's blob is a state dev's
        history holds, so mere historical presence clears the path and the next
        publish silently restores v2 -- undoing the retraction."""
        self._base(repo)
        (repo / "shared.txt").write_text("v1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "add shared.txt v1")
        _git(repo, "push", "-q", "origin", "dev")
        self._on_master(repo, "shared.txt", "v1\n", "infra sync: carry v1")
        (repo / "shared.txt").write_text("v2 broken\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "shared.txt v2")
        _git(repo, "push", "-q", "origin", "dev")
        self._on_master(repo, "shared.txt", "v2 broken\n", "infra sync: carry v2")
        self._on_master(repo, "shared.txt", "v1\n", "revert shared.txt to v1")

        assert publish._master_only_paths() == ["shared.txt"]

    def test_a_revert_back_to_the_published_state_is_master_only(self, repo):
        """The cheap base comparison cannot clear a path master MOVED. Dev
        ships v1 and it is published, so the base itself records v1; dev then
        ships a broken v2, an infra sync carries it, and master is reverted to
        v1 -- back to the very blob the base holds. Master's state matches the
        base's, yet master gave up v2 deliberately and a publish restores it."""
        self._base(repo)
        (repo / "shared.txt").write_text("v1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "add shared.txt v1")
        self._project(repo)
        (repo / "shared.txt").write_text("v2 broken\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "shared.txt v2")
        _git(repo, "push", "-q", "origin", "dev")
        self._on_master(repo, "shared.txt", "v2 broken\n", "infra sync: carry v2")
        self._on_master(repo, "shared.txt", "v1\n", "revert shared.txt to v1")

        assert publish._master_only_paths() == ["shared.txt"]

    def test_a_retraction_of_a_just_published_state_is_master_only(self, repo):
        """The retraction whose whole evidence sits on the publish point. Dev
        ships A, ships B, and master is reverted to A with no infra sync in
        between -- so the only master commit after the release is the revert
        itself, and the state master gave up (B) is the one the release put
        there.

        A range measured from the projection EXCLUDES that commit, which would
        leave master looking like it holds earlier content having given nothing
        up, and clear a publish that restores exactly what the retraction
        withdrew. The state master held at the publish point has to be counted
        explicitly.
        """
        self._base(repo)
        (repo / "shared.txt").write_text("A\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "add shared.txt A")
        self._project(repo)
        (repo / "shared.txt").write_text("B\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "shared.txt B")
        self._project(repo)
        self._on_master(repo, "shared.txt", "A\n", "revert shared.txt to A")

        assert publish._master_only_paths() == ["shared.txt"]

    def test_a_dev_side_revert_to_published_content_is_not_master_only(self, repo):
        """The mirror image of the two tests above, and it must come out the
        other way. Dev publishes A, publishes B, then reverts itself back to A.
        Master's blob is B -- content dev's tip has moved past, exactly as in a
        master-side revert -- but master never gave anything up: it was handed A
        and then B, and B is the newest content it ever received. Dev superseded
        B itself, so a publish discards no master-side decision.

        This is the projection flavour, so it also pins the range boundary:
        every master-side question must be measured from the projection commit.
        Measured from the dev sha the trailer names, the range reaches back past
        the divergence and sweeps in the earlier projection that carried A,
        making a path master has not touched since the release read as one
        master moved.
        """
        self._base(repo)
        (repo / "shared.txt").write_text("A\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "add shared.txt A")
        self._project(repo)
        (repo / "shared.txt").write_text("B\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "shared.txt B")
        self._project(repo)
        (repo / "shared.txt").write_text("A\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "revert shared.txt to A")
        _git(repo, "push", "-q", "origin", "dev")

        assert publish._master_only_paths() == []

    def test_a_dev_side_revert_over_synced_content_is_not_master_only(self, repo):
        """The same dev-side revert, with master receiving A and then B by infra
        sync rather than by projection, so master has demonstrably TOUCHED the
        path since the publish point and the full check has to settle it.

        This pins the ordering: dev's states must be ranked by when dev
        INTRODUCED them, not by where they last appear. Dev's revert puts A back
        at dev's tip, so ranking by most recent appearance calls A dev's newest
        content and B older than it -- and master holding B then reads as master
        sitting on superseded content it chose, which is the master-side revert
        verdict applied to the wrong branch's move.
        """
        self._base(repo)
        (repo / "shared.txt").write_text("A\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "add shared.txt A")
        _git(repo, "push", "-q", "origin", "dev")
        self._on_master(repo, "shared.txt", "A\n", "infra sync: carry A")
        (repo / "shared.txt").write_text("B\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "shared.txt B")
        _git(repo, "push", "-q", "origin", "dev")
        self._on_master(repo, "shared.txt", "B\n", "infra sync: carry B")
        (repo / "shared.txt").write_text("A\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "revert shared.txt to A")
        _git(repo, "push", "-q", "origin", "dev")

        assert publish._master_only_paths() == []

    def test_a_retraction_stops_refusing_once_it_is_published(self, repo):
        """A master-side revert is master-only until dev carries it, and no
        longer. Dev ships A, an infra sync carries B, master is reverted to A --
        the retraction the guard refuses on -- and the retraction is then
        back-ported to dev and PUBLISHED, so master's A is what the release
        itself put there. Dev moves on to C.

        Master has done nothing since that release, so there is nothing left to
        refuse. This pins the boundary for every master-side question: it is the
        projection commit, not the dev sha the projection names. The dev sha is
        not an ancestor of master, so a range starting there walks back past the
        release and finds the withdrawn B again -- refusing the same retraction
        forever, after it was resolved exactly as the documented procedure says.
        """
        self._base(repo)
        (repo / "shared.txt").write_text("A\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "add shared.txt A")
        _git(repo, "push", "-q", "origin", "dev")
        self._on_master(repo, "shared.txt", "A\n", "infra sync: carry A")
        (repo / "shared.txt").write_text("B\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "shared.txt B")
        _git(repo, "push", "-q", "origin", "dev")
        self._on_master(repo, "shared.txt", "B\n", "infra sync: carry B")
        self._on_master(repo, "shared.txt", "A\n", "revert shared.txt to A")
        # The retraction is back-ported to dev and published, which makes
        # master's state the release's own output.
        (repo / "shared.txt").write_text("A\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "back-port the retraction to dev")
        # The retraction ships as a release, which is what the recovery
        # procedure prescribes; the bump is what gives that release a tree
        # master does not already have.
        _bump(repo, "pub-kit", "1.2.0", "pub-kit 1.2.0")
        self._project(repo)
        (repo / "shared.txt").write_text("C\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "shared.txt C")

        assert publish._master_only_paths() == []


# --- the master-only guard, judged against an independent model ------------

# A small alphabet on purpose. The guard's defects all lived where two states
# COLLIDE -- master arriving at a blob dev also holds, dev returning to content
# it published before -- and a generator drawing paths and contents from a wide
# space produces those shapes almost never.
_PROP_PATHS = ("alpha.txt", "beta.txt")
_PROP_CONTENTS = ("A", "B", "C")
# Content master writes that dev's history never holds: the hotfix.
_PROP_HOTFIX = "H"
_PUB_MANIFEST = "plugins/pub-kit/.claude-plugin/plugin.json"
# Bounded on purpose: each case builds a real repo. These two numbers are the
# whole runtime knob, and `test_the_corpus_covers_every_shape` is what stops a
# tweak to either from quietly dropping the cases that matter.
_PROP_SEEDS = 12
_PROP_LENGTH = 14


class _BranchModel:
    """What each branch holds, tracked by DECIDING it rather than reading git.

    This is the oracle, and it is only worth having because it knows something
    the implementation must INFER. The generator below decides which branch
    writes which content, so the model records authorship directly; publish.py
    has to recover the same picture from rev-list ranges, blob identity and a
    trailer. Every defect this guard has shipped lived in that inference layer,
    which is exactly what comparing the two layers tests.

    The bookkeeping per path is: the order in which DEV INTRODUCED each content
    (its first appearance on dev, which is what makes a dev-side revert a
    return to old content rather than new content), master's current content,
    the content master was handed at the last publish point, and every content
    master has written since.
    """

    def __init__(self) -> None:
        self.dev: dict[str, str] = {}
        self.master: dict[str, str | None] = {}
        self.intro: dict[str, list[str]] = {}
        self.boundary: dict[str, str | None] = {}
        self.since: dict[str, list[str | None]] = {}
        # Every state master has ever held, across publish points. Not part of
        # the rule -- only `sensitivities` uses it, to notice when the guard's
        # answer DEPENDS on the window ending where it does.
        self.ever: dict[str, list[str | None]] = {}

    def seed(self) -> None:
        """The fixture's initial commit, which both branches start from."""
        self.dev_write(_PUB_MANIFEST, "1.0.0")
        self.master[_PUB_MANIFEST] = "1.0.0"

    def dev_write(self, path: str, content: str) -> None:
        self.dev[path] = content
        order = self.intro.setdefault(path, [])
        if content not in order:
            order.append(content)

    def project(self) -> None:
        """Master takes dev's whole tree; this is the recorded publish point."""
        for path in self.intro:
            self.master[path] = self.dev.get(path)
            self.boundary[path] = self.dev.get(path)
            self.since[path] = []
            self.ever.setdefault(path, []).append(self.dev.get(path))

    def master_write(self, path: str, content: str | None) -> None:
        self.master[path] = content
        self.since.setdefault(path, []).append(content)
        self.ever.setdefault(path, []).append(content)

    def master_only(self) -> list[str]:
        """The paths the rule calls master-only, computed from the bookkeeping.

        The rule, in one sentence: master's current content is master-only iff
        master itself gave up content that DEV INTRODUCED LATER than the
        content master holds.

        Three consequences, none of them special cases:

        * Content dev never introduced (a hotfix) is master-only outright --
          there is nothing later for master to have given up, and losing it is
          the loss the guard exists to refuse.
        * A path master gave up entirely (a deletion) is master-only, because
          master abandoned content it held. A path master NEVER held -- one dev
          added after the publish point -- is not: master gave up nothing.
        * A dev-side revert clears. Master was handed each state in turn and
          gave up none of them, so the newest content master held is the one it
          still holds, and nothing master decided is at risk.

        The known limit falls straight out of the same sentence and is asserted
        rather than excluded: when dev reverts and an infra sync carries that
        revert to master, master's own move is backwards too, so master DID
        give up content dev introduced later and the verdict is a refusal. No
        signal in either history separates that from a master-side retraction,
        and refusing is the safe direction.
        """
        return sorted(p for p, shape in self.shapes().items()
                      if not shape.startswith("cleared"))

    def shapes(self) -> dict[str, str]:
        """The verdict per path, LABELLED by which clause of the rule decided.

        The labels are what `test_the_corpus_covers_every_shape` checks, so a
        generator that stops producing master-side retractions fails loudly
        instead of passing on easy sequences.
        """
        verdict = {}
        for path in sorted(set(self.intro) | set(self.master)):
            held_now = self.master.get(path)
            if held_now == self.dev.get(path):
                continue  # the branches agree; git diff never offers the path
            order = self.intro.get(path, [])
            ever_held = [c for c in
                         [self.boundary.get(path), *self.since.get(path, [])]
                         if c is not None]
            if held_now is None:
                # Master gave the path up only if master ever had it to give.
                # A path dev added after the publish point is master's to lose
                # only once master has held it.
                verdict[path] = "deletion" if ever_held else "cleared-never-held"
            elif held_now not in order:
                verdict[path] = "hotfix"
            else:
                current = order.index(held_now)
                ranks = [order.index(c) for c in ever_held if c in order]
                if max(ranks, default=current) > current:
                    verdict[path] = "master-gave-up-later"
                elif current > order.index(self.dev[path]):
                    verdict[path] = "cleared-dev-side-revert"
                else:
                    verdict[path] = "cleared-dev-ahead"
        return verdict

    def sensitivities(self) -> set[str]:
        """Which of the guard's two range decisions the verdict DEPENDS on.

        Two of the four historical defects were range errors, and neither
        changes any verdict unless the sequence reaches a state where the
        window's edges matter. A corpus can hold every shape in `shapes` and
        still never reach one, so these are tracked separately and asserted.

        The publish-point state matters when master's only move since the
        release is a retraction OF what the release placed -- drop that state
        and master looks like it gave up nothing. The window's start matters
        when sweeping in master's history from BEFORE the release would find
        content master has since been handed again, which is how a back-ported
        retraction gets refused forever.
        """
        out = set()
        for path, order in self.intro.items():
            held_now = self.master.get(path)
            if held_now == self.dev.get(path) or held_now not in order:
                continue
            current = order.index(held_now)

            def refuses(states, _order=order, _current=current):
                ranks = [_order.index(c) for c in states if c in _order]
                return max(ranks, default=_current) > _current

            window = [c for c in [self.boundary.get(path),
                                  *self.since.get(path, [])] if c]
            if refuses(window) != refuses(
                    [c for c in self.since.get(path, []) if c]):
                out.add("publish-point-state-decides")
            if refuses(window) != refuses(
                    [c for c in self.ever.get(path, []) if c]):
                out.add("publish-window-start-decides")
        return out


def _plan(seed: int, length: int) -> list[tuple]:
    """A random but legal interleaving of the operations the harness supports.

    Legality is what the shadow model is for: git refuses an empty commit, so a
    write must actually change the branch it is written to, a deletion needs a
    file to delete, and master may only touch a path dev has created.

    An operation KIND is drawn first and its argument second, rather than
    drawing from one pooled list of candidates. Pooling looks equivalent and is
    not: master's candidate list grows as its history does, so the pooled draw
    slides toward all-master sequences that never publish and never let dev
    move -- exactly the sequences with nothing interesting in them. Fixing the
    kind weights holds the mix steady however long the sequence runs.

    Master's content is drawn by FLAVOUR for the same reason. The four
    master-side moves the guard has to tell apart are named -- an infra sync
    carrying dev's content, a retraction to content dev introduced earlier, a
    return to content master itself held before, and a hotfix dev never had --
    so each stays common instead of depending on a lucky collision in a
    three-letter alphabet.
    """
    rng = random.Random(seed)
    shadow = _BranchModel()
    shadow.seed()
    ops: list[tuple] = [("project",)]
    shadow.dev_write(_PUB_MANIFEST, "1.1.0")
    shadow.project()
    minor = 1
    kinds = ["dev", "project", "mwrite", "mdel"]
    kind_weights = [4, 3, 4, 1]
    flavours = ["sync", "retract", "revert", "hotfix", "any"]
    flavour_weights = [3, 3, 2, 1, 2]
    while len(ops) < length:
        op: tuple | None = None
        while op is None:
            kind = rng.choices(kinds, kind_weights)[0]
            if kind == "dev":
                path = rng.choice(_PROP_PATHS)
                op = ("dev", path, rng.choice(
                    [c for c in _PROP_CONTENTS if c != shadow.dev.get(path)]))
            elif kind == "project":
                op = ("project",)
            elif kind == "mwrite":
                live = [p for p in _PROP_PATHS if p in shadow.dev]
                if not live:
                    continue
                path = rng.choice(live)
                here = shadow.master.get(path)
                order = shadow.intro.get(path, [])
                pools = {
                    "sync": [shadow.dev[path]],
                    "retract": order[:order.index(here)] if here in order else [],
                    "revert": [c for c in [shadow.boundary.get(path),
                                           *shadow.since.get(path, [])] if c],
                    "hotfix": [_PROP_HOTFIX],
                    "any": [*_PROP_CONTENTS, _PROP_HOTFIX],
                }
                flavour = rng.choices(flavours, flavour_weights)[0]
                cands = ([c for c in pools[flavour] if c != here]
                         or [c for c in pools["any"] if c != here])
                op = ("mwrite", path, rng.choice(cands))
            else:
                live = [p for p in _PROP_PATHS
                        if shadow.master.get(p) is not None]
                if not live:
                    continue
                op = ("mdel", rng.choice(live))
        ops.append(op)
        if op[0] == "dev":
            shadow.dev_write(op[1], op[2])
        elif op[0] == "project":
            minor += 1
            shadow.dev_write(_PUB_MANIFEST, f"1.{minor}.0")
            shadow.project()
        elif op[0] == "mwrite":
            shadow.master_write(op[1], op[2])
        else:
            shadow.master_write(op[1], None)
    return ops


class TestMasterOnlyGuardAgainstAModel:
    """A property test over random operation sequences.

    The scenario tests above each pin one hand-written story, so the guard is
    checked on exactly the stories somebody thought of -- and all four defects
    this function has shipped lived in the gaps between them, every one found
    by building a fixture rather than by reading the code. This closes the gaps
    by generating the stories instead.

    The oracle is `_BranchModel`, which computes the expected verdict from
    bookkeeping the GENERATOR filled in. It is emphatically not the
    implementation: the model is told who wrote what, publish.py has to work it
    out from git. A property test whose expected value came from the code under
    test would prove nothing at all.

    Bounded on purpose. Each case builds a real repo and runs tens of git
    commands, so the sequences are short and few enough to keep this class's
    contribution to the suite in the tens of seconds.
    """

    _harness = TestMasterOnlyGuardAsksDevHistory

    def _apply(self, repo: Path, op: tuple, minor: list[int]) -> None:
        """Drive one operation through the existing harness helpers."""
        if op[0] == "dev":
            _, path, content = op
            (repo / path).write_text(f"{content}\n")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", f"dev: {path} = {content}")
            _git(repo, "push", "-q", "origin", "dev")
        elif op[0] == "project":
            minor[0] += 1
            _bump(repo, "pub-kit", f"1.{minor[0]}.0", f"pub-kit 1.{minor[0]}.0")
            self._harness._project(repo)
        elif op[0] == "mwrite":
            _, path, content = op
            self._harness._on_master(repo, path, f"{content}\n",
                                     f"master: {path} = {content}")
        else:
            _, path = op
            _git(repo, "checkout", "-q", "master")
            _git(repo, "reset", "-q", "--hard", "origin/master")
            _git(repo, "rm", "-q", path)
            _git(repo, "commit", "-qm", f"master: drop {path}")
            _git(repo, "push", "-q", "origin", "master")
            _git(repo, "checkout", "-q", "dev")
            _git(repo, "fetch", "-q", "origin")

    @pytest.mark.parametrize("seed", range(_PROP_SEEDS))
    def test_the_guard_agrees_with_the_model(self, repo, seed):
        ops = _plan(seed, _PROP_LENGTH)
        model = _BranchModel()
        model.seed()
        minor = [0]
        for step, op in enumerate(ops):
            self._apply(repo, op, minor)
            if op[0] == "dev":
                model.dev_write(op[1], op[2])
            elif op[0] == "project":
                model.dev_write(_PUB_MANIFEST, f"1.{minor[0]}.0")
                model.project()
            elif op[0] == "mwrite":
                model.master_write(op[1], op[2])
            else:
                model.master_write(op[1], None)
            expected = model.master_only()
            actual = sorted(publish._master_only_paths())
            # Print the whole sequence, not just the failing step: the state a
            # disagreement needs is built by everything before it, and a seed
            # nobody can replay by eye is a mystery rather than a bug report.
            story = "\n".join(
                f"  {'>>' if i == step else '  '} {o}" for i, o in
                enumerate(ops[:step + 1]))
            assert actual == expected, (
                f"seed {seed}, step {step}\n{story}\n"
                f"  model    : {expected}\n  publish.py: {actual}")

    def test_the_corpus_covers_every_shape(self):
        """The generator's own guard, and it runs on the model alone.

        A property test is worth only what its corpus contains, and the shapes
        that matter here are rare: a master-side retraction needs master to
        write a path twice within one publish window while dev advances in
        between, which a small alphabet produces only every few sequences. If a
        change to the weights, the seed count or the length stops producing
        one, the sequences above would still all pass -- on stories that never
        reach the clause four defects lived in. This fails instead, without
        touching git.
        """
        seen = set()
        sensitive = set()
        for seed in range(_PROP_SEEDS):
            model = _BranchModel()
            model.seed()
            minor = 0
            for op in _plan(seed, _PROP_LENGTH):
                if op[0] == "dev":
                    model.dev_write(op[1], op[2])
                elif op[0] == "project":
                    minor += 1
                    model.dev_write(_PUB_MANIFEST, f"1.{minor}.0")
                    model.project()
                elif op[0] == "mwrite":
                    model.master_write(op[1], op[2])
                else:
                    model.master_write(op[1], None)
                seen.update(model.shapes().values())
                sensitive.update(model.sensitivities())
        assert seen >= {
            # Master wrote content dev's history never held.
            "hotfix",
            # Master dropped a file it held.
            "deletion",
            # Master gave up content dev introduced later -- the retraction,
            # and the same shape the known limit produces when an infra sync
            # carries a dev-side revert to master.
            "master-gave-up-later",
            # Dev went back to content it published before, master still holds
            # the later state it was handed, and nothing is at risk.
            "cleared-dev-side-revert",
            # Master holds a state dev has simply moved on from.
            "cleared-dev-ahead",
        }, sorted(seen)
        assert sensitive == {"publish-point-state-decides",
                             "publish-window-start-decides"}, sorted(sensitive)


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


class TestFastForwardSafety:
    """A fast-forward moves dev's TREE wholesale, so it bypasses the hold-back.

    The exclusion set cannot guard it: `excluded` is populated per COMMIT from
    the publish range, so a range touching no dev-only plugin leaves it empty
    while the dev-only files still sit in dev's tree. Guarding on the tree is
    the only thing that closes it.
    """

    def test_refuses_fast_forward_while_a_dev_only_plugin_exists(self, repo):
        assert publish._fast_forward_is_safe() is False

    def test_does_not_read_git_so_a_failing_ls_tree_cannot_read_as_safe(
            self, repo, monkeypatch):
        """The guard protects a push to a PUBLIC master, so it must have no
        branch on which a git failure looks like an all-clear."""
        def _boom(*a, **k):
            raise AssertionError("_fast_forward_is_safe consulted git")
        monkeypatch.setattr(publish, "git", _boom)

        assert publish._fast_forward_is_safe() is False

    def test_allows_fast_forward_when_no_plugin_is_dev_only(self, repo, monkeypatch):
        monkeypatch.setattr(publish, "local_plugins",
                            lambda: {"pub-kit": {"name": "pub-kit",
                                                 "published": True}})
        assert publish._fast_forward_is_safe() is True

    def _wire(self, monkeypatch, safe):
        calls = []
        monkeypatch.setattr(publish, "git", lambda *a, **k: calls.append(a) or "")
        monkeypatch.setattr(publish, "_master_is_ancestor_of_dev", lambda: True)
        monkeypatch.setattr(publish, "_fast_forward_is_safe", lambda: safe)
        monkeypatch.setattr(publish, "_publish_projection",
                            lambda excluded: calls.append(("PROJECTED",)))
        return calls

    def test_push_and_merge_projects_when_the_tree_is_unsafe(self, monkeypatch):
        """The regression this guard exists for: master an ancestor of dev and
        nothing excluded -- precisely the state a master -> dev merge-back
        produces -- while dev-only files still sit in dev's tree."""
        calls = self._wire(monkeypatch, safe=False)

        publish.push_and_merge({})

        assert ("PROJECTED",) in calls
        assert not any(a[:1] == ("checkout",) for a in calls), \
            "fast-forward path was taken -- it would ship dev-only files"

    def test_push_and_merge_still_fast_forwards_when_the_tree_is_safe(self, monkeypatch):
        """The guard must not cost the fast-forward in the case it was for."""
        calls = self._wire(monkeypatch, safe=True)

        publish.push_and_merge({})

        assert ("PROJECTED",) not in calls
        assert ("merge", "--ff-only", publish.DEV_BRANCH) in calls


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
