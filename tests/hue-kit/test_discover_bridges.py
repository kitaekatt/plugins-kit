"""_discover_bridges: dedup by IP, and the cloud-to-mDNS fallback.

Both probes (_discover_via_cloud, _discover_via_mdns) are stubbed --
no network, no real mDNS socket.
"""

from __future__ import annotations


class TestDedup:
    """Results are deduped by IP -- a bridge id repeated under the same IP
    (or two entries sharing an IP with different ids) must collapse to one
    entry in the returned list."""

    def test_duplicate_ips_collapse_to_one_entry(self, hue_cli, monkeypatch):
        monkeypatch.setattr(hue_cli, "_discover_via_cloud", lambda timeout=10: [
            {"id": "bridge-a", "ip": "192.0.2.10", "port": 443},
            {"id": "bridge-a-dup", "ip": "192.0.2.10", "port": 443},
            {"id": "bridge-b", "ip": "192.0.2.20", "port": 443},
        ])

        bridges, cloud_err = hue_cli._discover_bridges()

        assert cloud_err is None
        assert sorted(b["ip"] for b in bridges) == ["192.0.2.10", "192.0.2.20"]
        assert len(bridges) == 2

    def test_entries_missing_an_ip_are_dropped_not_deduped_wrongly(
            self, hue_cli, monkeypatch):
        monkeypatch.setattr(hue_cli, "_discover_via_cloud", lambda timeout=10: [
            {"id": "no-ip", "ip": None, "port": 443},
            {"id": "bridge-b", "ip": "192.0.2.20", "port": 443},
        ])

        bridges, _ = hue_cli._discover_bridges()

        assert [b["ip"] for b in bridges] == ["192.0.2.20"]


class TestCloudToMdnsFallback:
    """A cloud probe failure falls back to mDNS, and the cloud exception is
    reported back to the caller (non-None) even though mDNS rescued the
    lookup -- distinct from a clean cloud success (cloud_err is None)."""

    def test_cloud_failure_falls_back_to_mdns_results(self, hue_cli, monkeypatch):
        def _boom(timeout=10):
            raise RuntimeError("cloud unreachable")

        monkeypatch.setattr(hue_cli, "_discover_via_cloud", _boom)
        monkeypatch.setattr(hue_cli, "_discover_via_mdns", lambda: [
            {"id": "mdns-bridge", "ip": "192.0.2.30", "port": 443},
        ])

        bridges, cloud_err = hue_cli._discover_bridges()

        assert isinstance(cloud_err, RuntimeError)
        assert [b["ip"] for b in bridges] == ["192.0.2.30"]

    def test_cloud_success_never_calls_mdns(self, hue_cli, monkeypatch):
        def _boom():
            raise AssertionError("mDNS must not be reached on cloud success")

        monkeypatch.setattr(hue_cli, "_discover_via_cloud",
                            lambda timeout=10: [
                                {"id": "cloud-bridge", "ip": "192.0.2.40",
                                 "port": 443}])
        monkeypatch.setattr(hue_cli, "_discover_via_mdns", _boom)

        bridges, cloud_err = hue_cli._discover_bridges()

        assert cloud_err is None
        assert [b["ip"] for b in bridges] == ["192.0.2.40"]

    def test_cloud_success_with_empty_list_still_falls_back_to_mdns(
            self, hue_cli, monkeypatch):
        """An empty cloud result (not an exception) is also a "nothing
        found" case that must still try mDNS -- cloud_err stays None since
        the cloud call itself did not fail."""
        monkeypatch.setattr(hue_cli, "_discover_via_cloud", lambda timeout=10: [])
        monkeypatch.setattr(hue_cli, "_discover_via_mdns", lambda: [
            {"id": "mdns-bridge", "ip": "192.0.2.50", "port": 443},
        ])

        bridges, cloud_err = hue_cli._discover_bridges()

        assert cloud_err is None
        assert [b["ip"] for b in bridges] == ["192.0.2.50"]
