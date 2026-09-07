"""Unit + behavioral-parity tests for the manifest normalization layer.

Covers ``engine._normalize_tool_entry`` -- the single parse-time choke point
(called at the top of ``_process_tool_entry``, through which every tool entry
flows: layered user/project manifests, per-plugin manifests, and engine
self-setup). Two canonicalizations, per analysis-dividing-line.md section 4:

  1. install.<os> string          -> {"command": <s>, "elevated": false}
     ("manual" sentinel preserved as {"command": "manual", ...}).
  2. download.<os[-arch]>.scoop    -> install.<os>.scoop (deprecated-but-read).

The suite pins three properties the design requires:
  * legacy and canonical spellings produce IDENTICAL engine behavior;
  * scoop wins over a url download AND over an install command (precedence);
  * normalization composes with manifest_merge (runs AFTER merge).

Fixtures are modeled on p4-kit's manifest (scoop + "manual") and bootstrap's
own manifest (download.url + install strings).
"""

import bootstrap_lib.engine as engine
import bootstrap_lib.tool_check as tool_check
import bootstrap_lib.path_check as path_check
import bootstrap_lib.path_repair as path_repair
import bootstrap_lib.tool_paths as tool_paths
import bootstrap_lib.downloader as downloader
import bootstrap_lib.scoop as scoop_mod
import bootstrap_lib.platform_detect as platform_detect
from bootstrap_lib.manifest_merge import merge_manifests


def _stub(monkeypatch):
    """Neutralize side effects: PATH writes, tool_paths state, repair_path."""
    monkeypatch.setattr(path_check, "add_path_to_shell_config", lambda d: (True, "stub"))
    monkeypatch.setattr(tool_paths, "record", lambda *a, **k: None)
    monkeypatch.setattr(path_repair, "repair_path", lambda: None)


def _stub_amd64(monkeypatch):
    """Pin arch detection to amd64.

    _resolve_download_def keys on f"{current_os}-{detect_arch()}", and
    detect_arch() reads the REAL host CPU (platform.machine()), independent
    of whatever current_os a test passes in. Fixtures below use the
    "windows-amd64" spelling (p4-kit's real manifest key) with a faked
    current_os="windows", so on any non-amd64 host (e.g. Apple Silicon)
    detect_arch() returns "arm64" and the key silently fails to match unless
    pinned here.
    """
    monkeypatch.setattr(platform_detect, "detect_arch", lambda: "amd64")


# --------------------------------------------------------------------------- #
# 1. _normalize_tool_entry unit behavior
# --------------------------------------------------------------------------- #

class TestNormalizeInstallStrings:
    def test_string_install_becomes_command_object(self):
        out = engine._normalize_tool_entry(
            {"name": "direnv", "install": {"ubuntu": "apt-get install -y direnv"}},
            "ubuntu",
        )
        assert out["install"]["ubuntu"] == {
            "command": "apt-get install -y direnv", "elevated": False,
        }

    def test_manual_sentinel_preserved_as_command(self):
        out = engine._normalize_tool_entry(
            {"name": "p4", "install": {"ubuntu": "manual"}}, "ubuntu",
        )
        # Sentinel semantics preserved: downstream keys on command == "manual".
        assert out["install"]["ubuntu"] == {"command": "manual", "elevated": False}

    def test_skip_sentinel_becomes_skip_object(self):
        # "skip" (not applicable on this OS) canonicalizes to {"skip": true} --
        # NEVER to {"command": "skip"} (design-os-not-applicable.md ruling).
        out = engine._normalize_tool_entry(
            {"name": "tmux", "install": {"windows": "skip"}}, "windows",
        )
        assert out["install"]["windows"] == {"skip": True}

    def test_skip_object_form_passes_through(self):
        out = engine._normalize_tool_entry(
            {"name": "tmux", "install": {"windows": {"skip": True}}}, "windows",
        )
        assert out["install"]["windows"] == {"skip": True}

    def test_skip_input_not_mutated(self):
        entry = {"name": "tmux", "install": {"windows": "skip"}}
        engine._normalize_tool_entry(entry, "windows")
        assert entry["install"]["windows"] == "skip"  # original intact

    def test_all_os_keys_canonicalized_not_just_host(self):
        out = engine._normalize_tool_entry(
            {"name": "t", "install": {
                "macos": "brew install t", "ubuntu": "apt install t",
                "windows": "manual"}},
            "macos",
        )
        assert all(isinstance(v, dict) and "command" in v
                   for v in out["install"].values())

    def test_already_canonical_command_object_passthrough(self):
        entry = {"name": "t", "install": {
            "ubuntu": {"command": "curl x | sh", "elevated": True}}}
        out = engine._normalize_tool_entry(entry, "ubuntu")
        assert out["install"]["ubuntu"] == {"command": "curl x | sh", "elevated": True}

    def test_input_not_mutated(self):
        entry = {"name": "t", "install": {"ubuntu": "apt install t"}}
        engine._normalize_tool_entry(entry, "ubuntu")
        assert entry["install"]["ubuntu"] == "apt install t"  # original intact

    def test_non_dict_entry_returned_unchanged(self):
        assert engine._normalize_tool_entry("not-a-dict", "ubuntu") == "not-a-dict"


class TestNormalizeScoop:
    def test_download_scoop_os_key_moves_to_install(self):
        out = engine._normalize_tool_entry(
            {"name": "jj", "download": {"windows": {"scoop": "main/jj"}}},
            "windows",
        )
        assert out["install"]["windows"] == {"scoop": "main/jj"}
        # scoop stripped from the download block (canonical form owns it).
        assert "scoop" not in out["download"]["windows"]

    def test_download_scoop_os_arch_key_moves_to_os_install(self, monkeypatch):
        # p4-kit's real spelling: download.windows-amd64.scoop.
        _stub_amd64(monkeypatch)
        out = engine._normalize_tool_entry(
            {"name": "p4", "download": {"windows-amd64": {"scoop": "main/p4"}}},
            "windows",
        )
        assert out["install"]["windows"] == {"scoop": "main/p4"}

    def test_scoop_takes_precedence_over_install_command(self, monkeypatch):
        # p4-kit shape: install.windows "manual" + a scoop download. Dispatch
        # runs scoop before the install command, so canonical install.windows
        # must be the scoop spec, NOT the "manual" command.
        _stub_amd64(monkeypatch)
        out = engine._normalize_tool_entry(
            {"name": "p4",
             "install": {"windows": "manual", "macos": "brew install --cask perforce"},
             "download": {"windows-amd64": {"scoop": "main/p4"}}},
            "windows",
        )
        assert out["install"]["windows"] == {"scoop": "main/p4"}
        # non-host OS keys still canonicalize as command objects.
        assert out["install"]["macos"] == {
            "command": "brew install --cask perforce", "elevated": False}

    def test_scoop_not_promoted_on_non_matching_host(self):
        # On macOS a windows-only scoop entry is left where it is (dead here),
        # exactly as the pre-normalization scoop resolution behaved.
        out = engine._normalize_tool_entry(
            {"name": "p4", "download": {"windows-amd64": {"scoop": "main/p4"}}},
            "macos",
        )
        assert "windows" not in out.get("install", {})
        assert out["download"]["windows-amd64"] == {"scoop": "main/p4"}

    def test_download_url_sibling_survives_scoop_promotion(self):
        out = engine._normalize_tool_entry(
            {"name": "t", "download": {
                "windows": {"scoop": "main/t", "url": "http://x/y", "sha256": "ab"}}},
            "windows",
        )
        assert out["install"]["windows"] == {"scoop": "main/t"}
        # the url/sha half stays in download for the url-download strategy.
        assert out["download"]["windows"] == {"url": "http://x/y", "sha256": "ab"}

    def test_skip_sentinel_beats_a_same_os_legacy_scoop_download(self):
        # design-os-not-applicable.md: skip beats everything, incl. a same-OS
        # download. Step 2's promotion must not clobber a skip already parked
        # at install[current_os] by step 1.
        out = engine._normalize_tool_entry(
            {"name": "t", "install": {"windows": "skip"},
             "download": {"windows": {"scoop": "main/t"}}},
            "windows",
        )
        assert out["install"]["windows"] == {"skip": True}

    def test_elevated_flag_on_the_download_side_survives_scoop_promotion(self):
        # manifest-reference.md: {"scoop": "bucket/pkg", "elevated": true} is a
        # legitimate admin-gated scoop download. Promoting it into install[os]
        # must not drop the elevated flag by extracting only "scoop".
        out = engine._normalize_tool_entry(
            {"name": "t", "download": {
                "windows": {"scoop": "extras/tailscale", "elevated": True}}},
            "windows",
        )
        assert out["install"]["windows"] == {
            "scoop": "extras/tailscale", "elevated": True}


# --------------------------------------------------------------------------- #
# 2. Behavioral parity: legacy spelling vs canonical spelling
# --------------------------------------------------------------------------- #

class TestParityInstallCommand:
    """A legacy install string and its canonical command-object equal produce
    identical engine behavior (install command runs on a miss)."""

    def _run(self, tool_def, tmp_path, monkeypatch):
        _stub(monkeypatch)
        monkeypatch.setenv("PATH", "/usr/bin")  # tool absent

        def fake_install(cmd):
            (tmp_path / "tool").write_text("#!/bin/sh\n")
            return (True, "installed")

        monkeypatch.setattr(tool_check, "run_install", fake_install)
        tools_installed = []
        failure = engine._process_tool_entry(
            tool_def, "ubuntu", "/data", "", [], [], tools_installed, plugin_name="p",
        )
        return failure, tools_installed

    def test_legacy_and_canonical_install_command_match(self, tmp_path, monkeypatch):
        legacy = {"name": "tool", "installPath": str(tmp_path),
                  "install": {"ubuntu": "apt install tool"}}
        f1, t1 = self._run(legacy, tmp_path, monkeypatch)

        (tmp_path / "tool").unlink()  # reset between runs
        canonical = {"name": "tool", "installPath": str(tmp_path),
                     "install": {"ubuntu": {"command": "apt install tool", "elevated": False}}}
        f2, t2 = self._run(canonical, tmp_path, monkeypatch)

        assert f1 is None and f2 is None
        assert t1 == t2
        assert "`apt install tool`" in t1[0][1]


class TestParityManualSentinel:
    def test_legacy_and_canonical_manual_both_surface_manual_item(self, tmp_path, monkeypatch):
        _stub(monkeypatch)
        monkeypatch.setenv("PATH", "/usr/bin")  # tool absent

        def run_both(install_block):
            action_entries = []
            failure = engine._process_tool_entry(
                {"name": "p4", "install": install_block},
                "ubuntu", "/data", "", action_entries, [], [], plugin_name="p",
            )
            return failure, action_entries

        f_legacy, a_legacy = run_both({"ubuntu": "manual"})
        f_canon, a_canon = run_both({"ubuntu": {"command": "manual", "elevated": False}})

        for failure in (f_legacy, f_canon):
            assert failure["install_state"] == "manual_install"
            assert failure["install_cmd"] is None  # manual is not fix-all eligible
        assert any("manual install required" in a for a in a_legacy)
        assert a_legacy == a_canon


class TestParityScoop:
    """Legacy download.scoop and canonical install.<os>.scoop install identically."""

    def _run(self, tool_def, tmp_path, monkeypatch):
        _stub(monkeypatch)
        _stub_amd64(monkeypatch)
        monkeypatch.setenv("PATH", str(tmp_path))  # tool absent from PATH
        monkeypatch.setattr(scoop_mod, "ensure_scoop",
                            lambda: scoop_mod.ScoopResult(True, None, "already installed"))
        monkeypatch.setattr(scoop_mod, "scoop_install",
                            lambda pkg, tool_name=None: scoop_mod.ScoopResult(
                                True, str(tmp_path / "p4.exe"), f"installed {pkg}"))
        monkeypatch.setattr(tool_check, "run_install",
                            lambda cmd: (_ for _ in ()).throw(
                                AssertionError("install command must not run when scoop applies")))
        tools_installed = []
        failure = engine._process_tool_entry(
            tool_def, "windows", "/data", "", [], [], tools_installed, plugin_name="p4-kit",
        )
        return failure, tools_installed

    def test_legacy_and_canonical_scoop_match(self, tmp_path, monkeypatch):
        legacy = {"name": "p4",
                  "install": {"windows": "manual"},
                  "download": {"windows-amd64": {"scoop": "main/p4"}}}
        f1, t1 = self._run(legacy, tmp_path, monkeypatch)

        canonical = {"name": "p4",
                     "install": {"windows": {"scoop": "main/p4"}}}
        f2, t2 = self._run(canonical, tmp_path, monkeypatch)

        assert f1 is None and f2 is None
        assert t1 == t2
        assert "via scoop" in t1[0][1]


class TestScoopOverUrlPrecedence:
    """Pin the soft spot the fable audit flagged: an entry declaring BOTH a
    scoop package and a url+sha256 download resolves via SCOOP, never the url --
    in both the legacy (download.scoop + download.url) and canonical
    (install.scoop + download.url) spellings."""

    def _run(self, tool_def, tmp_path, monkeypatch):
        _stub(monkeypatch)
        _stub_amd64(monkeypatch)
        monkeypatch.setenv("PATH", str(tmp_path))  # tool absent
        monkeypatch.setattr(scoop_mod, "ensure_scoop",
                            lambda: scoop_mod.ScoopResult(True, None, "already installed"))
        monkeypatch.setattr(scoop_mod, "scoop_install",
                            lambda pkg, tool_name=None: scoop_mod.ScoopResult(
                                True, str(tmp_path / "t.exe"), f"installed {pkg}"))
        monkeypatch.setattr(downloader, "download_and_install",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("url download must not run when scoop applies")))
        tools_installed = []
        failure = engine._process_tool_entry(
            tool_def, "windows", "/data", "", [], [], tools_installed, plugin_name="p",
        )
        return failure, tools_installed

    def test_legacy_scoop_and_url_prefers_scoop(self, tmp_path, monkeypatch):
        legacy = {"name": "t", "download": {
            "windows-amd64": {"scoop": "main/t", "url": "http://x/y", "sha256": "ab"}}}
        failure, tools_installed = self._run(legacy, tmp_path, monkeypatch)
        assert failure is None
        assert "via scoop" in tools_installed[0][1]

    def test_canonical_scoop_and_url_prefers_scoop(self, tmp_path, monkeypatch):
        canonical = {"name": "t",
                     "install": {"windows": {"scoop": "main/t"}},
                     "download": {"windows-amd64": {"url": "http://x/y", "sha256": "ab"}}}
        failure, tools_installed = self._run(canonical, tmp_path, monkeypatch)
        assert failure is None
        assert "via scoop" in tools_installed[0][1]


# --------------------------------------------------------------------------- #
# 3. Composition with manifest_merge (normalization runs AFTER merge)
# --------------------------------------------------------------------------- #

class TestMergeComposition:
    """A user layer overriding one field of a legacy-spelled entry must
    deep-merge correctly BEFORE normalization canonicalizes the result.
    Normalization runs at _process_tool_entry (after _load_layered_manifests
    merges), so the merge only ever sees raw legacy dicts/strings."""

    def test_override_one_os_field_of_legacy_entry(self):
        base = {"tools": [{"name": "t", "install": {
            "macos": "brew install t-old", "ubuntu": "apt install t"}}]}
        user = {"tools": [{"name": "t", "install": {
            "macos": "brew install t-new"}}]}  # override just macos

        merged = merge_manifests(base, user)
        tool = merged["tools"][0]
        # merge is a raw deep-merge: override wins for macos, ubuntu preserved.
        assert tool["install"] == {
            "macos": "brew install t-new", "ubuntu": "apt install t"}

        # THEN normalization canonicalizes the merged result.
        norm = engine._normalize_tool_entry(tool, "macos")
        assert norm["install"]["macos"] == {
            "command": "brew install t-new", "elevated": False}
        assert norm["install"]["ubuntu"] == {
            "command": "apt install t", "elevated": False}

    def test_user_adds_scoop_download_over_legacy_install(self, monkeypatch):
        # base declares only an install string; user layer adds a scoop download.
        _stub_amd64(monkeypatch)
        base = {"tools": [{"name": "p4", "install": {"windows": "manual"}}]}
        user = {"tools": [{"name": "p4", "download": {
            "windows-amd64": {"scoop": "main/p4"}}}]}

        merged = merge_manifests(base, user)
        tool = merged["tools"][0]
        norm = engine._normalize_tool_entry(tool, "windows")
        # scoop (from the merged download) wins over the "manual" install string.
        assert norm["install"]["windows"] == {"scoop": "main/p4"}

    def test_canonical_override_of_legacy_base(self):
        # base legacy string, user canonical object for the same OS: override
        # wins at the scalar level, then normalization is a passthrough.
        base = {"tools": [{"name": "t", "install": {"ubuntu": "apt install old"}}]}
        user = {"tools": [{"name": "t", "install": {
            "ubuntu": {"command": "curl new | sh", "elevated": True}}}]}
        merged = merge_manifests(base, user)
        norm = engine._normalize_tool_entry(merged["tools"][0], "ubuntu")
        assert norm["install"]["ubuntu"] == {"command": "curl new | sh", "elevated": True}
