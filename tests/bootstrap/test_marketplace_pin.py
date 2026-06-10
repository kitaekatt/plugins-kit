"""Tests for marketplace pin operations — bootstrap.json marketplaces[].pin.

Library-level tests use real temp git repos (same approach as
TestCheckMarketplaceCurrentNoUpstream in test_marketplace_lifecycle.py);
engine-flow tests mock the lifecycle functions like the neighboring
TestMarketplaceAlwaysUpdate / TestEngineMinVersionFlow classes.
"""

import json
import os
import subprocess
import types
from unittest.mock import patch

import pytest

from bootstrap_lib.marketplace_lifecycle import (
    LifecycleResult,
    PinResult,
    VersionCheckResult,
    apply_marketplace_pin,
    check_marketplace_pin,
    load_pin_markers,
    pinned_marketplace_sha,
    release_marketplace_pin,
    resolve_pin,
    save_pin_markers,
)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}

FAKE_SHA = "f7f6276a" * 5  # 40-char fake commit SHA for mocked PinResults


def _git(args, cwd):
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), check=True, capture_output=True,
        text=True, env={**os.environ, **GIT_ENV},
    )


def _commit(repo, fname, content, msg):
    (repo / fname).write_text(content)
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", msg], repo)
    return _git(["rev-parse", "HEAD"], repo).stdout.strip()


def _head(repo):
    return _git(["rev-parse", "HEAD"], repo).stdout.strip()


def _is_detached(repo):
    proc = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"], cwd=str(repo),
        capture_output=True, text=True,
    )
    return proc.returncode != 0


@pytest.fixture
def repos(tmp_path):
    """Origin with two commits on master + a clone. Clone HEAD is at sha2."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(["init", "-q", "-b", "master"], origin)
    sha1 = _commit(origin, "a.txt", "one", "c1")
    sha2 = _commit(origin, "a.txt", "two", "c2")
    clone = tmp_path / "clone"
    _git(["clone", "-q", str(origin), str(clone)], tmp_path)
    return types.SimpleNamespace(origin=origin, clone=clone, sha1=sha1, sha2=sha2)


@pytest.fixture
def pin_paths(tmp_path, repos):
    """Explicit marker + registry paths (never the real ~/.claude)."""
    pins = tmp_path / "marketplace_pins.json"
    km = tmp_path / "known_marketplaces.json"
    km.write_text(json.dumps({
        "my-market": {
            "source": {"source": "git", "url": str(repos.origin)},
            "installLocation": str(repos.clone),
            "lastUpdated": "2026-01-01T00:00:00.000Z",
            "autoUpdate": True,
        }
    }))
    return types.SimpleNamespace(pins=str(pins), km=str(km))


class TestResolvePin:
    def test_resolves_known_sha(self, repos):
        sha, err = resolve_pin(str(repos.clone), repos.sha1)
        assert sha == repos.sha1
        assert err == ""

    def test_resolves_short_sha(self, repos):
        sha, err = resolve_pin(str(repos.clone), repos.sha1[:8])
        assert sha == repos.sha1

    def test_fetches_then_retries_for_unknown_commit(self, repos):
        """A commit pushed to origin after the clone resolves via fetch+retry."""
        sha3 = _commit(repos.origin, "a.txt", "three", "c3")
        sha, err = resolve_pin(str(repos.clone), sha3)
        assert sha == sha3
        assert err == ""

    def test_fetches_then_retries_for_tag(self, repos):
        """A tag created in origin after the clone resolves via fetch+retry."""
        _git(["tag", "v1", repos.sha1], repos.origin)
        sha, err = resolve_pin(str(repos.clone), "v1")
        assert sha == repos.sha1

    def test_unresolvable_returns_actionable_error(self, repos):
        sha, err = resolve_pin(str(repos.clone), "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
        assert sha == ""
        assert "cannot resolve pin" in err
        assert "remove the pin" in err


class TestCheckMarketplacePin:
    def test_mismatch_when_head_elsewhere(self, repos):
        result = check_marketplace_pin(str(repos.clone), repos.sha1)
        assert result.passed is False
        assert result.status == "pin_mismatch"
        assert result.sha == repos.sha1

    def test_passes_when_head_at_pin(self, repos):
        _git(["checkout", "-q", "--detach", repos.sha1], repos.clone)
        result = check_marketplace_pin(str(repos.clone), repos.sha1)
        assert result.passed is True
        assert result.status == "already_pinned"


class TestApplyMarketplacePin:
    def test_checks_out_detached_when_at_wrong_sha(self, repos, pin_paths):
        result = apply_marketplace_pin(
            "my-market", repos.sha1, clone_dir=str(repos.clone),
            pins_path=pin_paths.pins, km_path=pin_paths.km,
        )
        assert result.passed is True
        assert result.status == "pinned"
        assert result.sha == repos.sha1
        assert _head(repos.clone) == repos.sha1
        assert _is_detached(repos.clone)

    def test_already_at_pin_is_no_op(self, repos, pin_paths):
        _git(["checkout", "-q", "--detach", repos.sha1], repos.clone)
        result = apply_marketplace_pin(
            "my-market", repos.sha1, clone_dir=str(repos.clone),
            pins_path=pin_paths.pins, km_path=pin_paths.km,
        )
        assert result.passed is True
        assert result.status == "already_pinned"

    def test_unresolvable_pin_fails_and_writes_nothing(self, repos, pin_paths):
        result = apply_marketplace_pin(
            "my-market", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            clone_dir=str(repos.clone),
            pins_path=pin_paths.pins, km_path=pin_paths.km,
        )
        assert result.passed is False
        assert "cannot resolve pin" in result.message
        assert load_pin_markers(pin_paths.pins) == {}
        km_data = json.loads(open(pin_paths.km).read())
        assert km_data["my-market"]["autoUpdate"] is True  # untouched

    def test_missing_clone_fails_with_guidance(self, tmp_path, pin_paths):
        result = apply_marketplace_pin(
            "my-market", "abc1234", clone_dir=str(tmp_path / "nope"),
            pins_path=pin_paths.pins, km_path=pin_paths.km,
        )
        assert result.passed is False
        assert "clone not found" in result.message
        assert "marketplace add" in result.message

    def test_fetch_then_retry_pins_post_clone_commit(self, repos, pin_paths):
        sha3 = _commit(repos.origin, "a.txt", "three", "c3")
        result = apply_marketplace_pin(
            "my-market", sha3, clone_dir=str(repos.clone),
            pins_path=pin_paths.pins, km_path=pin_paths.km,
        )
        assert result.passed is True
        assert _head(repos.clone) == sha3

    def test_forces_auto_update_false_and_records_prior_once(self, repos, pin_paths):
        apply_marketplace_pin(
            "my-market", repos.sha1, clone_dir=str(repos.clone),
            pins_path=pin_paths.pins, km_path=pin_paths.km,
        )
        km_data = json.loads(open(pin_paths.km).read())
        assert km_data["my-market"]["autoUpdate"] is False
        # Other fields preserved
        assert km_data["my-market"]["installLocation"] == str(repos.clone)
        assert km_data["my-market"]["lastUpdated"] == "2026-01-01T00:00:00.000Z"
        markers = load_pin_markers(pin_paths.pins)
        assert markers["my-market"]["pin"] == repos.sha1
        assert markers["my-market"]["resolved_sha"] == repos.sha1
        assert markers["my-market"]["prior_auto_update"] is True

        # Re-pin to a different SHA while already pinned: prior_auto_update
        # must NOT be overwritten with the now-forced False.
        apply_marketplace_pin(
            "my-market", repos.sha2, clone_dir=str(repos.clone),
            pins_path=pin_paths.pins, km_path=pin_paths.km,
        )
        markers = load_pin_markers(pin_paths.pins)
        assert markers["my-market"]["resolved_sha"] == repos.sha2
        assert markers["my-market"]["prior_auto_update"] is True

    def test_records_prior_none_when_auto_update_absent(self, repos, pin_paths):
        km_data = json.loads(open(pin_paths.km).read())
        del km_data["my-market"]["autoUpdate"]
        open(pin_paths.km, "w").write(json.dumps(km_data))

        apply_marketplace_pin(
            "my-market", repos.sha1, clone_dir=str(repos.clone),
            pins_path=pin_paths.pins, km_path=pin_paths.km,
        )
        markers = load_pin_markers(pin_paths.pins)
        assert markers["my-market"]["prior_auto_update"] is None
        km_data = json.loads(open(pin_paths.km).read())
        assert km_data["my-market"]["autoUpdate"] is False

    def test_resolves_clone_dir_from_registry_when_omitted(self, repos, pin_paths):
        """The engine call path: pin-only entry, clone dir from known_marketplaces."""
        result = apply_marketplace_pin(
            "my-market", repos.sha1,
            pins_path=pin_paths.pins, km_path=pin_paths.km,
        )
        assert result.passed is True
        assert _head(repos.clone) == repos.sha1


class TestReleaseMarketplacePin:
    def _pin_first(self, repos, pin_paths):
        result = apply_marketplace_pin(
            "my-market", repos.sha1, clone_dir=str(repos.clone),
            pins_path=pin_paths.pins, km_path=pin_paths.km,
        )
        assert result.passed is True

    def test_restores_branch_auto_update_and_removes_marker(self, repos, pin_paths):
        self._pin_first(repos, pin_paths)
        result = release_marketplace_pin(
            "my-market", clone_dir=str(repos.clone),
            pins_path=pin_paths.pins, km_path=pin_paths.km,
        )
        assert result.passed is True
        assert result.status == "unpinned"
        assert "restored master" in result.message
        assert not _is_detached(repos.clone)
        km_data = json.loads(open(pin_paths.km).read())
        assert km_data["my-market"]["autoUpdate"] is True  # restored
        assert load_pin_markers(pin_paths.pins) == {}

    def test_null_prior_auto_update_leaves_current_value_alone(self, repos, pin_paths):
        _git(["checkout", "-q", "--detach", repos.sha1], repos.clone)
        save_pin_markers(
            {"my-market": {"pin": repos.sha1, "resolved_sha": repos.sha1,
                           "prior_auto_update": None}},
            pin_paths.pins,
        )
        km_data = json.loads(open(pin_paths.km).read())
        km_data["my-market"]["autoUpdate"] = False
        open(pin_paths.km, "w").write(json.dumps(km_data))

        result = release_marketplace_pin(
            "my-market", clone_dir=str(repos.clone),
            pins_path=pin_paths.pins, km_path=pin_paths.km,
        )
        assert result.passed is True
        km_data = json.loads(open(pin_paths.km).read())
        assert km_data["my-market"]["autoUpdate"] is False  # untouched
        assert load_pin_markers(pin_paths.pins) == {}

    def test_no_marker_entry_is_a_no_op(self, repos, pin_paths):
        result = release_marketplace_pin(
            "my-market", clone_dir=str(repos.clone),
            pins_path=pin_paths.pins, km_path=pin_paths.km,
        )
        assert result.passed is True
        assert "nothing to release" in result.message

    def test_default_branch_fallback_probe_without_origin_head(self, repos, pin_paths, tmp_path):
        """A repo with no remote (no origin/HEAD) falls back to master probing."""
        repo = repos.origin  # has master, no remote
        _git(["checkout", "-q", "--detach", repos.sha1], repo)
        save_pin_markers(
            {"my-market": {"pin": repos.sha1, "resolved_sha": repos.sha1,
                           "prior_auto_update": None}},
            pin_paths.pins,
        )
        result = release_marketplace_pin(
            "my-market", clone_dir=str(repo),
            pins_path=pin_paths.pins, km_path=pin_paths.km,
        )
        assert result.passed is True
        assert "restored master" in result.message
        assert not _is_detached(repo)


class TestPinnedMarketplaceSha:
    def test_returns_short_sha_when_pinned(self, tmp_path):
        pins = tmp_path / "pins.json"
        save_pin_markers({"my-market": {"pin": "v1", "resolved_sha": FAKE_SHA,
                                        "prior_auto_update": True}}, str(pins))
        assert pinned_marketplace_sha("my-market", str(pins)) == FAKE_SHA[:8]

    def test_returns_empty_when_not_pinned(self, tmp_path):
        assert pinned_marketplace_sha("my-market", str(tmp_path / "pins.json")) == ""


class TestEnginePinFlow:
    """Engine-flow tests for _phase_marketplaces pin/unpin wiring.

    Same style as TestMarketplaceAlwaysUpdate: real registry files under a
    monkeypatched HOME, lifecycle functions mocked.
    """

    @pytest.fixture(autouse=True)
    def _clear_pin_run_state(self):
        from bootstrap_lib import engine
        engine._pinned_marketplaces_this_run.clear()
        yield
        engine._pinned_marketplaces_this_run.clear()

    def _setup_home(self, tmp_path, monkeypatch, registered=True):
        plugins_dir = tmp_path / ".claude" / "plugins"
        plugins_dir.mkdir(parents=True)
        km = plugins_dir / "known_marketplaces.json"
        entries = {}
        if registered:
            entries["my-market"] = {
                "source": {"source": "git", "url": "https://example.com"},
                "installLocation": str(tmp_path / "marketplaces" / "my-market"),
                "autoUpdate": True,
            }
        km.write_text(json.dumps(entries))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        return km

    def _run(self, tmp_path, manifest):
        from bootstrap_lib.engine import _process_manifest
        action_entries = []
        ok_entries = []
        failures = _process_manifest(
            manifest, "windows", str(tmp_path / "data"), str(tmp_path / "root"),
            action_entries, ok_entries, plugin_name="test",
        )
        return action_entries, ok_entries, failures

    def test_pin_with_always_update_emits_precedence_warning(self, tmp_path, monkeypatch):
        self._setup_home(tmp_path, monkeypatch)
        manifest = {"marketplaces": [
            {"name": "my-market", "source": "https://example.com",
             "alwaysUpdate": True, "pin": "f7f6276a"}
        ]}
        with patch("bootstrap_lib.marketplace_lifecycle.apply_marketplace_pin",
                   return_value=PinResult(True, "my-market", "pinned", FAKE_SHA, "pinned")) as mock_apply, \
             patch("bootstrap_lib.marketplace_lifecycle.check_marketplace_current") as mock_current, \
             patch("bootstrap_lib.marketplace_lifecycle.update_marketplace") as mock_update:
            action_entries, ok_entries, failures = self._run(tmp_path, manifest)
            mock_apply.assert_called_once_with("my-market", "f7f6276a")
            mock_current.assert_not_called()
            mock_update.assert_not_called()
        assert any("alwaysUpdate ignored while pinned" in e for e in action_entries)
        assert any(f"pinned at {FAKE_SHA[:8]}" in e for e in action_entries)
        assert failures == []

    def test_pin_only_entry_without_source_works_when_registered(self, tmp_path, monkeypatch):
        self._setup_home(tmp_path, monkeypatch)
        manifest = {"marketplaces": [{"name": "my-market", "pin": "f7f6276a"}]}
        with patch("bootstrap_lib.marketplace_lifecycle.apply_marketplace_pin",
                   return_value=PinResult(True, "my-market", "already_pinned", FAKE_SHA, "pinned")) as mock_apply, \
             patch("bootstrap_lib.marketplace_lifecycle.add_marketplace") as mock_add:
            action_entries, ok_entries, failures = self._run(tmp_path, manifest)
            mock_apply.assert_called_once()
            mock_add.assert_not_called()
        # Already at the pin: verbose-only ok entry, no action noise.
        assert any(f"pinned at {FAKE_SHA[:8]}" in e for e in ok_entries)
        assert not any("pinned at" in e for e in action_entries)
        assert failures == []

    def test_pin_only_entry_unregistered_fails(self, tmp_path, monkeypatch):
        self._setup_home(tmp_path, monkeypatch, registered=False)
        manifest = {"marketplaces": [{"name": "my-market", "pin": "f7f6276a"}]}
        with patch("bootstrap_lib.marketplace_lifecycle.apply_marketplace_pin") as mock_apply, \
             patch("bootstrap_lib.marketplace_lifecycle.add_marketplace") as mock_add:
            action_entries, ok_entries, failures = self._run(tmp_path, manifest)
            mock_apply.assert_not_called()
            mock_add.assert_not_called()
        assert any(f["type"] == "marketplace" for f in failures)
        assert any("not registered" in e for e in action_entries)

    def test_pin_unregistered_with_source_adds_then_pins(self, tmp_path, monkeypatch):
        self._setup_home(tmp_path, monkeypatch, registered=False)
        manifest = {"marketplaces": [
            {"name": "my-market", "source": "https://example.com", "pin": "f7f6276a"}
        ]}
        with patch("bootstrap_lib.marketplace_lifecycle.add_marketplace",
                   return_value=LifecycleResult(True, "my-market", "marketplace added")) as mock_add, \
             patch("bootstrap_lib.marketplace_lifecycle.apply_marketplace_pin",
                   return_value=PinResult(True, "my-market", "pinned", FAKE_SHA, "pinned")) as mock_apply:
            action_entries, ok_entries, failures = self._run(tmp_path, manifest)
            mock_add.assert_called_once_with("https://example.com", "my-market")
            mock_apply.assert_called_once_with("my-market", "f7f6276a")
        assert any("added" in e for e in action_entries)
        assert failures == []

    def test_pin_failure_recorded(self, tmp_path, monkeypatch):
        self._setup_home(tmp_path, monkeypatch)
        manifest = {"marketplaces": [{"name": "my-market", "pin": "badc0ffee"}]}
        with patch("bootstrap_lib.marketplace_lifecycle.apply_marketplace_pin",
                   return_value=PinResult(False, "my-market", "error", "", "cannot resolve pin 'badc0ffee'")):
            action_entries, ok_entries, failures = self._run(tmp_path, manifest)
        assert any("pin failed" in e for e in action_entries)
        assert any(f["type"] == "marketplace" and "cannot resolve" in f["message"] for f in failures)

    def test_unpin_releases_then_updates(self, tmp_path, monkeypatch):
        self._setup_home(tmp_path, monkeypatch)
        # Marker at the default (HOME-derived) path records an active pin.
        from bootstrap_lib.marketplace_lifecycle import default_pins_path
        save_pin_markers({"my-market": {"pin": "f7f6276a", "resolved_sha": FAKE_SHA,
                                        "prior_auto_update": True}})
        assert os.path.isfile(default_pins_path())

        manifest = {"marketplaces": [{"name": "my-market", "source": "https://example.com"}]}
        with patch("bootstrap_lib.marketplace_lifecycle.release_marketplace_pin",
                   return_value=PinResult(True, "my-market", "unpinned", "", "restored master")) as mock_rel, \
             patch("bootstrap_lib.marketplace_lifecycle.update_marketplace",
                   return_value=LifecycleResult(True, "my-market", "marketplace updated")) as mock_upd:
            action_entries, ok_entries, failures = self._run(tmp_path, manifest)
            mock_rel.assert_called_once_with("my-market")
            mock_upd.assert_called_once_with("my-market")
        assert any("unpinned, restored master + updated" in e for e in action_entries)
        assert failures == []

    def test_unpin_update_failure_recorded(self, tmp_path, monkeypatch):
        self._setup_home(tmp_path, monkeypatch)
        save_pin_markers({"my-market": {"pin": "f7f6276a", "resolved_sha": FAKE_SHA,
                                        "prior_auto_update": None}})
        manifest = {"marketplaces": [{"name": "my-market", "source": "https://example.com"}]}
        with patch("bootstrap_lib.marketplace_lifecycle.release_marketplace_pin",
                   return_value=PinResult(True, "my-market", "unpinned", "", "restored master")), \
             patch("bootstrap_lib.marketplace_lifecycle.update_marketplace",
                   return_value=LifecycleResult(False, "my-market", "network error")):
            action_entries, ok_entries, failures = self._run(tmp_path, manifest)
        assert any(f["type"] == "marketplace" and "network error" in f["message"] for f in failures)

    def test_pin_in_earlier_manifest_blocks_unpin_in_later_manifest(self, tmp_path, monkeypatch):
        """The layered manifest pins (Step 3c); a plugin manifest's unpinned
        entry for the same marketplace (Step 4) must not unpin or update it."""
        self._setup_home(tmp_path, monkeypatch)
        save_pin_markers({"my-market": {"pin": "f7f6276a", "resolved_sha": FAKE_SHA,
                                        "prior_auto_update": True}})

        pinned_manifest = {"marketplaces": [{"name": "my-market", "pin": "f7f6276a"}]}
        unpinned_manifest = {"marketplaces": [
            {"name": "my-market", "source": "https://example.com", "alwaysUpdate": True}
        ]}
        with patch("bootstrap_lib.marketplace_lifecycle.apply_marketplace_pin",
                   return_value=PinResult(True, "my-market", "already_pinned", FAKE_SHA, "pinned")), \
             patch("bootstrap_lib.marketplace_lifecycle.release_marketplace_pin") as mock_rel, \
             patch("bootstrap_lib.marketplace_lifecycle.check_marketplace_current") as mock_current, \
             patch("bootstrap_lib.marketplace_lifecycle.update_marketplace") as mock_upd:
            self._run(tmp_path, pinned_manifest)
            action_entries, ok_entries, failures = self._run(tmp_path, unpinned_manifest)
            mock_rel.assert_not_called()
            mock_current.assert_not_called()
            mock_upd.assert_not_called()
        assert any("pinned earlier this run" in e for e in ok_entries)
        assert failures == []


class TestEnginePinVersionInteraction:
    """min_version-vs-pin failure message and installed-ahead-of-pin notice."""

    @pytest.fixture(autouse=True)
    def _clear_pin_run_state(self):
        from bootstrap_lib import engine
        engine._pinned_marketplaces_this_run.clear()
        yield
        engine._pinned_marketplaces_this_run.clear()

    def _setup_home(self, tmp_path, monkeypatch, installed_version):
        plugins_dir = tmp_path / ".claude" / "plugins"
        plugins_dir.mkdir(parents=True)
        ip = plugins_dir / "installed_plugins.json"
        ip.write_text(json.dumps({
            "version": 2,
            "plugins": {
                "bootstrap@plugins-kit": [{"scope": "user", "version": installed_version, "installPath": "/cache"}]
            }
        }))
        settings = tmp_path / ".claude" / "settings.json"
        settings.write_text(json.dumps({"enabledPlugins": {"bootstrap@plugins-kit": True}}))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

    def _run(self, tmp_path, manifest):
        from bootstrap_lib.engine import _process_manifest
        action_entries = []
        ok_entries = []
        failures = _process_manifest(
            manifest, "windows", str(tmp_path / "data"), str(tmp_path / "root"),
            action_entries, ok_entries, plugin_name="test",
        )
        return action_entries, ok_entries, failures

    def test_min_version_failure_mentions_pin(self, tmp_path, monkeypatch):
        self._setup_home(tmp_path, monkeypatch, "0.8.3")
        save_pin_markers({"plugins-kit": {"pin": "f7f6276a", "resolved_sha": FAKE_SHA,
                                          "prior_auto_update": True}})
        manifest = {"plugins": [
            {"ref": "plugins-kit:bootstrap", "enabled": True, "scope": "user", "min_version": "0.9.1"}
        ]}
        with patch("bootstrap_lib.marketplace_lifecycle.update_plugin",
                   return_value=LifecycleResult(False, "plugins-kit:bootstrap", "no newer version")), \
             patch("bootstrap_lib.marketplace_lifecycle.check_plugin_version") as mock_ver:
            mock_ver.return_value = type("R", (), {"up_to_date": True})()
            action_entries, ok_entries, failures = self._run(tmp_path, manifest)

        failure = next(f for f in failures if f["type"] == "plugin")
        assert f"pinned at {FAKE_SHA[:8]}" in failure["message"]
        assert "drop the pin" in failure["message"]
        assert any(f"pinned at {FAKE_SHA[:8]}" in e for e in action_entries)

    def test_min_version_failure_without_pin_has_no_pin_note(self, tmp_path, monkeypatch):
        self._setup_home(tmp_path, monkeypatch, "0.8.3")
        manifest = {"plugins": [
            {"ref": "plugins-kit:bootstrap", "enabled": True, "scope": "user", "min_version": "0.9.1"}
        ]}
        with patch("bootstrap_lib.marketplace_lifecycle.update_plugin",
                   return_value=LifecycleResult(False, "plugins-kit:bootstrap", "network error")), \
             patch("bootstrap_lib.marketplace_lifecycle.check_plugin_version") as mock_ver:
            mock_ver.return_value = type("R", (), {"up_to_date": True})()
            action_entries, ok_entries, failures = self._run(tmp_path, manifest)

        failure = next(f for f in failures if f["type"] == "plugin")
        assert "drop the pin" not in failure["message"]

    def test_installed_ahead_of_pin_emits_notice_not_failure(self, tmp_path, monkeypatch):
        self._setup_home(tmp_path, monkeypatch, "0.9.5")
        save_pin_markers({"plugins-kit": {"pin": "f7f6276a", "resolved_sha": FAKE_SHA,
                                          "prior_auto_update": True}})
        manifest = {"plugins": [
            {"ref": "plugins-kit:bootstrap", "enabled": True, "scope": "user"}
        ]}
        ahead = VersionCheckResult(
            up_to_date=True, ref="plugins-kit:bootstrap",
            installed_version="0.9.5", latest_version="0.9.0",
            message="version 0.9.5 (newer than marketplace 0.9.0)",
        )
        with patch("bootstrap_lib.marketplace_lifecycle.check_plugin_version", return_value=ahead), \
             patch("bootstrap_lib.marketplace_lifecycle.update_plugin") as mock_update:
            action_entries, ok_entries, failures = self._run(tmp_path, manifest)
            mock_update.assert_not_called()

        assert any("ahead of the pinned marketplace" in e and FAKE_SHA[:8] in e
                   for e in ok_entries)
        assert not any("ahead" in e for e in action_entries)
        assert failures == []

    def test_installed_ahead_without_pin_stays_silent(self, tmp_path, monkeypatch):
        self._setup_home(tmp_path, monkeypatch, "0.9.5")
        manifest = {"plugins": [
            {"ref": "plugins-kit:bootstrap", "enabled": True, "scope": "user"}
        ]}
        ahead = VersionCheckResult(
            up_to_date=True, ref="plugins-kit:bootstrap",
            installed_version="0.9.5", latest_version="0.9.0",
            message="version 0.9.5 (newer than marketplace 0.9.0)",
        )
        with patch("bootstrap_lib.marketplace_lifecycle.check_plugin_version", return_value=ahead):
            action_entries, ok_entries, failures = self._run(tmp_path, manifest)

        assert not any("ahead" in e for e in ok_entries + action_entries)
        assert failures == []
