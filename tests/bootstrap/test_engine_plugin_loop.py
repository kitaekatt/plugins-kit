"""Tests for the per-plugin loop's failure isolation and Phase-1/Phase-2
identity key (bootstrap slice 7, unit U2, increments I1 + I2 + I5).

Failure isolation: before this, only a JSON parse error inside
_bootstrap_single_plugin was caught. A plain file where the per-plugin data
dir should be a directory (NotADirectoryError from os.makedirs), a
PermissionError, or any exception raised by a phase handler crashed the whole
pass -- every already-processed plugin's log block, the display, the
elevation queue, and the engine_ran_version stamp were lost, and the crash
handler cleared the cooldown so the crash repeated every session.

Identity key: the Phase-1/Phase-2 "already processed" set used to be keyed by
ref alone, so a plugin updated mid-pass (Phase 1 processed v1, the registry
now shows v2) was excluded from Phase 2's rescan even though its new
dependencies/shared libs were never processed at the new version.
"""

import json
from types import SimpleNamespace

import pytest

import bootstrap_lib.engine as engine
from bootstrap_lib.plugin_resolve import PluginInfo


def _plugin(tmp_path, name, manifest, *, version="1.0", marketplace="mkt"):
    install = tmp_path / name
    install.mkdir()
    (install / "bootstrap.json").write_text(json.dumps(manifest), encoding="utf-8")
    return PluginInfo(name=name, install_path=str(install), version=version, marketplace=marketplace)


class TestPerPluginFailureIsolation:
    def test_crash_in_one_plugin_does_not_lose_the_next(self, tmp_path, monkeypatch):
        crashy = _plugin(tmp_path, "crashy", {"tools": [{"name": "x"}]})
        good = _plugin(tmp_path, "good", {})

        real_process_manifest = engine._process_manifest

        def fake_process_manifest(manifest, *a, **k):
            if k.get("plugin_name") == "crashy":
                raise RuntimeError("boom")
            return real_process_manifest(manifest, *a, **k)

        monkeypatch.setattr(engine, "_process_manifest", fake_process_manifest)

        data_dir = str(tmp_path / "data" / "mkt" / "bootstrap")
        all_failures, display_sections, deferred_plugin_logs = [], [], []
        args = SimpleNamespace(project_dir=None)

        engine._bootstrap_single_plugin_isolated(
            crashy, "linux", data_dir, all_failures, False,
            display_sections, deferred_plugin_logs, args, engine_version="1.0",
        )
        engine._bootstrap_single_plugin_isolated(
            good, "linux", data_dir, all_failures, False,
            display_sections, deferred_plugin_logs, args, engine_version="1.0",
        )

        sections = {h: (a, o) for h, a, o in display_sections}
        crashy_header = next(h for h in sections if "crashy" in h)
        crashy_actions, _ = sections[crashy_header]
        assert any("FAILED" in e and "RuntimeError" in e and "boom" in e for e in crashy_actions)

        # The second (good) plugin's block was still written -- the pass continued.
        good_header = next(h for h in sections if "good" in h)
        assert good_header is not None

        # The crash is recorded in the pass record (all_failures), not just displayed.
        crashes = [f for f in all_failures if f["type"] == "plugin_crash"]
        assert len(crashes) == 1
        assert crashes[0]["plugin"] == "crashy"
        assert "boom" in crashes[0]["message"]

        # Both plugins produced a deferred log block.
        logged_names = [label for _dd, label, _entries in deferred_plugin_logs]
        assert any("crashy" in n for n in logged_names)
        assert any("good" in n for n in logged_names)

    def test_isolated_wrapper_is_transparent_on_success(self, tmp_path):
        good = _plugin(tmp_path, "plain", {})
        data_dir = str(tmp_path / "data" / "mkt" / "bootstrap")
        all_failures, display_sections, deferred_plugin_logs = [], [], []
        args = SimpleNamespace(project_dir=None)

        engine._bootstrap_single_plugin_isolated(
            good, "linux", data_dir, all_failures, False,
            display_sections, deferred_plugin_logs, args, engine_version="1.0",
        )
        assert all_failures == []
        assert any("plain" in h for h, _a, _o in display_sections)


class TestManifestShapeValidation:
    """A syntactically valid bootstrap.json that decodes to a non-mapping
    (`[]` or `null`) must fail the same way a JSON-syntax error does, not
    crash inside .get()."""

    def test_array_manifest_is_a_shape_failure(self, tmp_path):
        install = tmp_path / "badshape"
        install.mkdir()
        (install / "bootstrap.json").write_text("[]", encoding="utf-8")
        pi = PluginInfo(name="badshape", install_path=str(install), version="1.0", marketplace="mkt")

        all_failures, display, deferred = [], [], []
        engine._bootstrap_single_plugin(
            pi, "linux", str(tmp_path / "data" / "mkt" / "bootstrap"), all_failures,
            False, display, deferred, SimpleNamespace(project_dir=None), engine_version="1.0",
        )

        parse_failures = [f for f in all_failures if f["type"] == "manifest_parse"]
        assert len(parse_failures) == 1
        assert "not a JSON object" in parse_failures[0]["message"]
        assert display and "PARSE FAILED" in display[0][1][0]

    def test_null_manifest_is_a_shape_failure(self, tmp_path):
        install = tmp_path / "nullshape"
        install.mkdir()
        (install / "bootstrap.json").write_text("null", encoding="utf-8")
        pi = PluginInfo(name="nullshape", install_path=str(install), version="1.0", marketplace="mkt")

        all_failures, display, deferred = [], [], []
        engine._bootstrap_single_plugin(
            pi, "linux", str(tmp_path / "data" / "mkt" / "bootstrap"), all_failures,
            False, display, deferred, SimpleNamespace(project_dir=None), engine_version="1.0",
        )
        assert any(f["type"] == "manifest_parse" for f in all_failures)

    def test_shape_failure_does_not_block_a_later_plugin(self, tmp_path):
        # Later plugins are processed regardless -- the shape check RETURNS
        # early rather than raising, so nothing about it can poison shared
        # accumulator lists for a subsequent call.
        bad = _plugin(tmp_path, "badshape", [])  # array manifest -> shape failure
        good = _plugin(tmp_path, "good", {})

        all_failures, display, deferred = [], [], []
        data_dir = str(tmp_path / "data" / "mkt" / "bootstrap")
        args = SimpleNamespace(project_dir=None)
        engine._bootstrap_single_plugin(bad, "linux", data_dir, all_failures, False,
                                         display, deferred, args, engine_version="1.0")
        engine._bootstrap_single_plugin(good, "linux", data_dir, all_failures, False,
                                         display, deferred, args, engine_version="1.0")
        assert any("good" in h for h, _a, _o in display)


class TestLayeredManifestShapeValidation:
    @pytest.fixture
    def isolated_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        return tmp_path

    def test_array_layer_surfaces_shape_error(self, isolated_home, tmp_path):
        project = tmp_path / "project"
        project_claude = project / ".claude"
        project_claude.mkdir(parents=True)
        bad = project_claude / "bootstrap.json"
        bad.write_text("[]", encoding="utf-8")

        merged, errors = engine._load_layered_manifests(str(project))

        assert merged == {}
        assert len(errors) == 1
        assert errors[0]["path"] == str(bad)
        assert "not a JSON object" in errors[0]["error"]

    def test_array_layer_does_not_block_other_layers(self, isolated_home, tmp_path):
        user_claude = isolated_home / ".claude"
        user_claude.mkdir()
        (user_claude / "bootstrap.json").write_text(
            json.dumps({"plugins": [{"ref": "a:b", "scope": "user"}]})
        )
        project = tmp_path / "project"
        project_claude = project / ".claude"
        project_claude.mkdir(parents=True)
        (project_claude / "bootstrap.json").write_text("null", encoding="utf-8")

        merged, errors = engine._load_layered_manifests(str(project))

        assert merged["plugins"][0]["ref"] == "a:b"
        assert len(errors) == 1


class TestPhase2IdentityKey:
    def test_changed_version_is_reprocessed(self, tmp_path):
        pi_v1 = PluginInfo(name="b", install_path="/cache/b/1.0", version="1.0", marketplace="mkt")
        pi_v2 = PluginInfo(name="b", install_path="/cache/b/2.0", version="2.0", marketplace="mkt")

        processed = {engine._plugin_processed_key(pi_v1)}
        new_plugins = engine._phase2_new_plugins([pi_v2], processed)

        assert new_plugins == [pi_v2]

    def test_same_version_is_not_reprocessed(self, tmp_path):
        pi = PluginInfo(name="b", install_path="/cache/b/1.0", version="1.0", marketplace="mkt")
        processed = {engine._plugin_processed_key(pi)}
        assert engine._phase2_new_plugins([pi], processed) == []

    def test_key_uses_colon_separated_ref(self):
        pi = PluginInfo(name="b", install_path="/x", version="1.0", marketplace="mkt")
        assert engine._plugin_processed_key(pi) == ("mkt:b", "1.0", "/x")

    def test_key_without_marketplace(self):
        pi = PluginInfo(name="b", install_path="/x", version="1.0", marketplace="")
        assert engine._plugin_processed_key(pi) == ("b", "1.0", "/x")


class TestPluginHeaders:
    """One _plugin_headers(...) -> (log_label, display_header) shared by every
    site that used to build this pair inline -- so bootstrap's own section on
    the manifest-parse-failure and requires_bootstrap early-return paths keeps
    the `(engine <running>)` disambiguation _plugin_log_label applies."""

    def test_malformed_manifest_for_bootstrap_itself_carries_both_versions(self, tmp_path):
        install = tmp_path / "install"
        install.mkdir()
        (install / "bootstrap.json").write_text("{bad json", encoding="utf-8")

        # data_dir must equal the derived plugin_data_dir for bootstrap-on-itself
        # (_plugin_data_dir keys by marketplace/name) so _plugin_log_label's
        # is_self branch fires.
        data_dir = tmp_path / "data" / "plugins-kit" / "bootstrap"
        data_dir.mkdir(parents=True)

        pi = PluginInfo(name="bootstrap", install_path=str(install), version="0.14.0",
                        marketplace="plugins-kit")
        all_failures, display, deferred = [], [], []
        engine._bootstrap_single_plugin(
            pi, "linux", str(data_dir), all_failures, False,
            display, deferred, SimpleNamespace(project_dir=None), engine_version="0.15.0",
        )

        # The LOG label (deferred_plugin_logs) is where the "(engine X)"
        # disambiguation is meant to survive -- the display header always uses
        # the marketplace-qualified "mkt:name@version" form once a marketplace
        # is set, same as the working normal-processing path.
        assert len(deferred) == 1
        log_label = deferred[0][1]
        assert "0.14.0" in log_label
        assert "(engine 0.15.0)" in log_label

    def test_requires_bootstrap_gate_carries_both_versions_for_self(self, tmp_path):
        install = tmp_path / "install"
        install.mkdir()
        (install / "bootstrap.json").write_text(
            json.dumps({"requires_bootstrap": "99.0.0"}), encoding="utf-8"
        )
        data_dir = tmp_path / "data" / "plugins-kit" / "bootstrap"
        data_dir.mkdir(parents=True)

        pi = PluginInfo(name="bootstrap", install_path=str(install), version="0.14.0",
                        marketplace="plugins-kit")
        all_failures, display, deferred = [], [], []
        engine._bootstrap_single_plugin(
            pi, "linux", str(data_dir), all_failures, False,
            display, deferred, SimpleNamespace(project_dir=None), engine_version="0.15.0",
        )

        assert len(deferred) == 1
        log_label = deferred[0][1]
        assert "0.14.0" in log_label
        assert "(engine 0.15.0)" in log_label

    def test_no_marketplace_display_header_also_carries_disambiguation(self, tmp_path):
        # When a plugin has no recorded marketplace (e.g. a --plugin-dir dev
        # install), the display header falls back to the log label itself, so
        # the disambiguation is visible there too.
        install = tmp_path / "install"
        install.mkdir()
        (install / "bootstrap.json").write_text("{bad json", encoding="utf-8")
        data_dir = tmp_path / "data" / "mkt" / "bootstrap"
        data_dir.mkdir(parents=True)

        pi = PluginInfo(name="bootstrap", install_path=str(install), version="0.14.0",
                        marketplace="")
        all_failures, display, deferred = [], [], []
        engine._bootstrap_single_plugin(
            pi, "linux", str(data_dir), all_failures, False,
            display, deferred, SimpleNamespace(project_dir=None), engine_version="0.15.0",
        )
        assert len(display) == 1
        header = display[0][0]
        assert "0.14.0" in header
        assert "(engine 0.15.0)" in header
