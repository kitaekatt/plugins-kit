"""_resolve_bridge_ip's precedence: HUE_BRIDGE_IP env, then the cache file,
then discovery -- each higher source must win over, and short-circuit,
every source below it.
"""

from __future__ import annotations

import pytest


class TestEnvWinsOverEverything:
    def test_env_wins_over_a_populated_cache(self, hue_cli, tmp_path, monkeypatch):
        cache = tmp_path / "bridge-ip.txt"
        cache.write_text("192.0.2.9\n")
        monkeypatch.setattr(hue_cli, "BRIDGE_IP_CACHE", cache)
        monkeypatch.setattr(hue_cli, "_discover_bridges",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("must not discover -- env is set")))
        # _pin_bridge_env (autouse) already set HUE_BRIDGE_IP="192.0.2.1"

        assert hue_cli._resolve_bridge_ip() == "192.0.2.1"


class TestCacheWinsOverDiscovery:
    def test_cache_used_when_env_unset(self, hue_cli, tmp_path, monkeypatch):
        monkeypatch.delenv("HUE_BRIDGE_IP", raising=False)
        cache = tmp_path / "bridge-ip.txt"
        cache.write_text("192.0.2.9\n")
        monkeypatch.setattr(hue_cli, "BRIDGE_IP_CACHE", cache)

        def _boom(*a, **k):
            raise AssertionError("must not discover -- the cache has an IP")
        monkeypatch.setattr(hue_cli, "_discover_bridges", _boom)

        assert hue_cli._resolve_bridge_ip() == "192.0.2.9"

    def test_blank_cache_file_falls_through_to_discovery(
            self, hue_cli, tmp_path, monkeypatch):
        """A cache file that exists but holds only whitespace must not be
        treated as a cached IP -- resolution must still fall through to
        discovery."""
        monkeypatch.delenv("HUE_BRIDGE_IP", raising=False)
        cache = tmp_path / "bridge-ip.txt"
        cache.write_text("   \n")
        monkeypatch.setattr(hue_cli, "BRIDGE_IP_CACHE", cache)
        monkeypatch.setattr(hue_cli, "_discover_bridges",
                            lambda *a, **k: ([{"id": "b", "ip": "192.0.2.77",
                                               "port": 443}], None))
        monkeypatch.setattr(hue_cli, "_cache_bridge_ip", lambda ip: None)

        assert hue_cli._resolve_bridge_ip() == "192.0.2.77"


class TestDiscoveryIsTheLastResort:
    def test_discovery_used_when_env_unset_and_no_cache_file(
            self, hue_cli, tmp_path, monkeypatch):
        monkeypatch.delenv("HUE_BRIDGE_IP", raising=False)
        monkeypatch.setattr(hue_cli, "BRIDGE_IP_CACHE", tmp_path / "no-such-cache.txt")
        monkeypatch.setattr(hue_cli, "_discover_bridges",
                            lambda *a, **k: ([{"id": "only", "ip": "192.0.2.88",
                                               "port": 443}], None))
        monkeypatch.setattr(hue_cli, "_cache_bridge_ip", lambda ip: None)

        assert hue_cli._resolve_bridge_ip() == "192.0.2.88"

    def test_no_bridge_found_anywhere_raises(self, hue_cli, tmp_path, monkeypatch):
        monkeypatch.delenv("HUE_BRIDGE_IP", raising=False)
        monkeypatch.setattr(hue_cli, "BRIDGE_IP_CACHE", tmp_path / "no-such-cache.txt")
        monkeypatch.setattr(hue_cli, "_discover_bridges", lambda *a, **k: ([], None))
        monkeypatch.setattr(hue_cli, "_mdns_available", lambda: True)

        with pytest.raises(SystemExit):
            hue_cli._resolve_bridge_ip()
