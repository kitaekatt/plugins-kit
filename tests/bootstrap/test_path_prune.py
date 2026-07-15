"""Tests for dead-Windows-User-PATH detection (bootstrap_lib/path_prune.py).

Two things here are load-bearing, in this order:

  * the DURABILITY state machine -- caching the scan must never turn "the user
    declined once" into "never mentioned again". This is the whole reason the
    cache stores the RESULT rather than "already reported", and it is what most
    of TestScanCaching is about;
  * the false-DEAD asymmetry -- a false "alive" costs one stale entry nobody
    notices; a false "dead" silently deletes a PATH entry the user needs. Every
    ambiguous case must resolve to alive.

The registry itself is faked throughout: the guard in tests/conftest.py exists
precisely because this suite once wrote the developer's real PATH 30 times.
"""

import json

import pytest

import bootstrap_lib.path_prune as pp


class TestReadUserPath:
    def test_the_skip_flag_suppresses_the_read_not_just_writes(self, monkeypatch):
        """REGRESSION GUARD. The registry ignores HOME isolation, so a scan
        inside a test reads the DEVELOPER's PATH -- every engine test then
        inherits whatever dead junk that machine has and reports a finding
        unrelated to the test. This turned 25 engine tests red the first time
        the scan shipped; conftest sets the flag for every test, and this is
        what makes the flag actually cover reads."""
        monkeypatch.setenv("BOOTSTRAP_SKIP_REGISTRY", "1")
        assert pp.read_user_path() is None

    def test_scan_is_inert_when_the_registry_is_off_limits(self, tmp_path, monkeypatch):
        """None (no verdict), not [] (ran and clean) -- the caller must not
        report a check that never ran as one that passed."""
        monkeypatch.setenv("BOOTSTRAP_SKIP_REGISTRY", "1")
        assert pp.scan(str(tmp_path)) is None


class TestSplitEntries:
    def test_entries_keep_their_exact_spelling(self):
        """The prune removes entries BY TEXT, so normalizing here would hand the
        runner a string that does not appear in the registry."""
        raw = "C:\\a\\;%FOO%\\bin;C:\\B"
        assert pp.split_entries(raw) == ["C:\\a\\", "%FOO%\\bin", "C:\\B"]

    def test_empty_segments_are_dropped(self):
        assert pp.split_entries("C:\\a;;;C:\\b;") == ["C:\\a", "C:\\b"]


class TestDeadEntries:
    def test_reports_only_the_dead_in_path_order(self, tmp_path):
        alive1 = tmp_path / "one"; alive1.mkdir()
        alive2 = tmp_path / "two"; alive2.mkdir()
        raw = ";".join([str(alive1), str(tmp_path / "gone"), str(alive2)])
        assert pp.dead_entries(raw) == [str(tmp_path / "gone")]


class TestScanCaching:
    """The state machine that makes a declined prune re-offer itself."""

    def _fake_path(self, monkeypatch, raw):
        monkeypatch.setattr(pp, "read_user_path", lambda: (raw, 2))

    def test_scan_caches_the_result_and_the_hash(self, tmp_path, monkeypatch):
        self._fake_path(monkeypatch, str(tmp_path / "gone"))
        assert pp.scan(str(tmp_path)) == [str(tmp_path / "gone")]
        body = json.loads(open(pp.stamp_path(str(tmp_path))).read())
        assert body["dead"] == [str(tmp_path / "gone")]
        assert body["path_hash"] == pp.path_hash(str(tmp_path / "gone"))

    def test_an_unchanged_path_is_not_rescanned(self, tmp_path, monkeypatch):
        """The cheap half: no filesystem probe per entry when nothing moved."""
        self._fake_path(monkeypatch, str(tmp_path / "gone"))
        pp.scan(str(tmp_path))
        monkeypatch.setattr(pp, "dead_entries",
                            lambda raw: pytest.fail("must not rescan"))
        assert pp.scan(str(tmp_path)) == [str(tmp_path / "gone")]

    def test_declining_the_prune_does_not_silence_the_finding(self, tmp_path, monkeypatch):
        """REGRESSION GUARD, and the reason the cache holds the RESULT rather
        than a "reported" flag. Cache the latter and this sequence -- user
        declines, PATH unchanged, scan skipped -- means the problem is detected
        once and never mentioned again."""
        self._fake_path(monkeypatch, str(tmp_path / "gone"))
        first = pp.scan(str(tmp_path))
        assert first
        # Session 2, 3, ... nothing changed, user did nothing.
        for _ in range(3):
            assert pp.scan(str(tmp_path)) == first

    def test_pruning_self_clears_the_finding(self, tmp_path, monkeypatch):
        """No 'fixed' ritual: the PATH changes, so the hash misses, so the
        rescan finds it clean on its own."""
        self._fake_path(monkeypatch, str(tmp_path / "gone"))
        assert pp.scan(str(tmp_path))
        alive = tmp_path / "real"; alive.mkdir()
        self._fake_path(monkeypatch, str(alive))
        assert pp.scan(str(tmp_path)) == []

    def test_a_new_dead_entry_is_picked_up(self, tmp_path, monkeypatch):
        alive = tmp_path / "real"; alive.mkdir()
        self._fake_path(monkeypatch, str(alive))
        assert pp.scan(str(tmp_path)) == []
        self._fake_path(monkeypatch, f"{alive};{tmp_path / 'new-gone'}")
        assert pp.scan(str(tmp_path)) == [str(tmp_path / "new-gone")]

    def test_a_corrupt_stamp_forces_a_rescan_rather_than_crashing(self, tmp_path, monkeypatch):
        open(pp.stamp_path(str(tmp_path)), "w").write("{not json")
        self._fake_path(monkeypatch, str(tmp_path / "gone"))
        assert pp.scan(str(tmp_path)) == [str(tmp_path / "gone")]

    def test_no_registry_path_is_no_verdict(self, tmp_path, monkeypatch):
        """Non-Windows, or a machine with no User Path value at all. None, not
        [] -- see test_scan_is_inert_when_the_registry_is_off_limits."""
        monkeypatch.setattr(pp, "read_user_path", lambda: None)
        assert pp.scan(str(tmp_path)) is None

    def test_a_clean_path_is_an_empty_verdict_not_none(self, tmp_path, monkeypatch):
        """The distinction that keeps 'scan ran, all good' reportable."""
        alive = tmp_path / "real"; alive.mkdir()
        self._fake_path(monkeypatch, str(alive))
        assert pp.scan(str(tmp_path)) == []
