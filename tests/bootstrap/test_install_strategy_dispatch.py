"""Unit tests for the install-strategy dispatch table.

Covers the table-dispatch refactor of ``engine._process_tool_entry``: the
ordered ``engine._INSTALL_STRATEGIES`` table, the ``_StrategyOutcome``
terminal/fall-through contract, and the precedence between strategies
(resolve -> scoop -> url download -> install command), including the
url-download failure -> install-command fall-through.

Behavioral parity of each individual strategy with the former inline branches
is covered by test_tool_path_linkage.py::TestProcessToolEntry; this file
focuses on the *dispatch* seam (ordering + short-circuit semantics).
"""

import bootstrap_lib.engine as engine
import bootstrap_lib.tool_check as tool_check
import bootstrap_lib.path_check as path_check
import bootstrap_lib.path_repair as path_repair
import bootstrap_lib.tool_paths as tool_paths
import bootstrap_lib.downloader as downloader
import bootstrap_lib.scoop as scoop_mod


def _stub(monkeypatch):
    """Neutralize side effects: PATH writes, tool_paths state, repair_path."""
    monkeypatch.setattr(path_check, "add_path_to_shell_config", lambda d: (True, "stub"))
    monkeypatch.setattr(tool_paths, "record", lambda *a, **k: None)
    monkeypatch.setattr(path_repair, "repair_path", lambda: None)


class TestStrategyTableShape:
    def test_table_is_ordered_tuple(self):
        assert isinstance(engine._INSTALL_STRATEGIES, tuple)
        names = [f.__name__ for f in engine._INSTALL_STRATEGIES]
        assert names == [
            "_strategy_skip",
            "_strategy_requires",
            "_strategy_resolve",
            "_strategy_scoop",
            "_strategy_brew",
            "_strategy_apt",
            "_strategy_url_download",
            "_strategy_install_command",
        ]

    def test_install_command_is_last(self):
        # The install command is the final fallback and is always terminal.
        assert engine._INSTALL_STRATEGIES[-1] is engine._strategy_install_command


class TestOutcomeContract:
    def test_terminal_stops_dispatch(self, monkeypatch):
        """First terminal strategy wins; later strategies never run."""
        called = []

        def s1(ctx):
            called.append("s1")
            return engine._StrategyOutcome(True, {"install_state": "from_s1"})

        def s2(ctx):
            called.append("s2")
            return engine._StrategyOutcome(True, None)

        monkeypatch.setattr(engine, "_INSTALL_STRATEGIES", (s1, s2))
        failure = engine._process_tool_entry(
            {"name": "x"}, "linux", "/data", "", [], [], [], plugin_name="p",
        )
        assert called == ["s1"]
        assert failure == {"install_state": "from_s1"}

    def test_fallthrough_advances_to_next(self, monkeypatch):
        """A non-terminal outcome advances the dispatcher; the next terminal
        outcome's failure is returned and strategies after it do not run."""
        called = []

        def s1(ctx):
            called.append("s1")
            return engine._StrategyOutcome(False)

        def s2(ctx):
            called.append("s2")
            return engine._StrategyOutcome(True, {"install_state": "from_s2"})

        def s3(ctx):
            called.append("s3")
            return engine._StrategyOutcome(True, None)

        monkeypatch.setattr(engine, "_INSTALL_STRATEGIES", (s1, s2, s3))
        failure = engine._process_tool_entry(
            {"name": "x"}, "linux", "/data", "", [], [], [], plugin_name="p",
        )
        assert called == ["s1", "s2"]
        assert failure == {"install_state": "from_s2"}

    def test_all_fallthrough_returns_none(self, monkeypatch):
        """If no strategy is terminal, the dispatcher falls off the end and
        returns None (no failure)."""
        monkeypatch.setattr(engine, "_INSTALL_STRATEGIES",
                            (lambda ctx: engine._StrategyOutcome(False),
                             lambda ctx: engine._StrategyOutcome(False)))
        failure = engine._process_tool_entry(
            {"name": "x"}, "linux", "/data", "", [], [], [], plugin_name="p",
        )
        assert failure is None


class TestPrecedence:
    def test_resolve_short_circuits_before_download_and_install(self, tmp_path, monkeypatch):
        """A tool that already resolves never reaches scoop/download/install."""
        _stub(monkeypatch)
        tool = tmp_path / "drawio"
        tool.write_text("#!/bin/sh\n")
        monkeypatch.setenv("PATH", str(tmp_path))

        def boom_dl(*a, **k):
            raise AssertionError("download must not run when the tool already resolves")

        def boom_install(cmd):
            raise AssertionError("install must not run when the tool already resolves")

        monkeypatch.setattr(downloader, "download_and_install", boom_dl)
        monkeypatch.setattr(tool_check, "run_install", boom_install)

        ok_entries = []
        failure = engine._process_tool_entry(
            {"name": "drawio", "installPath": str(tmp_path),
             "download": {"linux": {"url": "http://x/y", "sha256": "deadbeef"}},
             "install": {"linux": "pkg install drawio"}},
            "linux", "/data", "", [], ok_entries, [], plugin_name="p",
        )
        assert failure is None
        assert any("drawio: ok" in e for e in ok_entries)

    def test_scoop_precedes_install_command(self, tmp_path, monkeypatch):
        """download.scoop is fulfilled by Scoop; the install command never runs."""
        _stub(monkeypatch)
        monkeypatch.setenv("PATH", str(tmp_path))  # tool deliberately absent
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
            {"name": "p4",
             "download": {"windows": {"scoop": "main/p4"}},
             "install": {"windows": "winget install p4"}},
            "windows", "/data", "", [], [], tools_installed, plugin_name="p4-kit",
        )
        assert failure is None
        assert tools_installed and "via scoop" in tools_installed[0][1]

    def test_scoop_recheck_pass_off_path_still_links_dir(self, tmp_path, monkeypatch):
        """A scoop install that resolves via an installPath candidate, but
        whose dir isn't on bare-name PATH, must still get that dir linked --
        otherwise it's recorded installed while unreachable by bare name."""
        _stub(monkeypatch)
        monkeypatch.setenv("PATH", "/usr/bin")  # deliberately NOT tmp_path
        monkeypatch.setattr(scoop_mod, "ensure_scoop",
                            lambda: scoop_mod.ScoopResult(True, None, "already installed"))

        def fake_install(pkg, tool_name=None):
            (tmp_path / "p4").write_text("#!/bin/sh\n")
            return scoop_mod.ScoopResult(True, str(tmp_path / "p4"), f"installed {pkg}")
        monkeypatch.setattr(scoop_mod, "scoop_install", fake_install)

        added = []
        monkeypatch.setattr(path_check, "add_path_to_shell_config",
                            lambda d: added.append(d) or (True, "added"))

        tools_installed = []
        failure = engine._process_tool_entry(
            {"name": "p4", "installPath": str(tmp_path),
             "download": {"windows": {"scoop": "main/p4"}}},
            "windows", "/data", "", [], [], tools_installed, plugin_name="p4-kit",
        )
        assert failure is None
        assert added == [str(tmp_path)]

    def test_url_download_precedes_install_command(self, tmp_path, monkeypatch):
        """A successful url download short-circuits the install command."""
        _stub(monkeypatch)
        monkeypatch.setenv("PATH", str(tmp_path))

        def _fake_download(*a, **k):
            # The download must actually land the tool: the re-check after a
            # fix is authoritative, so a download that produced nothing is a
            # failure, not a short-circuit.
            landed = tmp_path / "tool"
            landed.write_text("#!/bin/sh\n")
            landed.chmod(0o755)
            return downloader.DownloadResult(True, str(landed), "downloaded")

        monkeypatch.setattr(downloader, "download_and_install", _fake_download)
        monkeypatch.setattr(tool_check, "run_install",
                            lambda cmd: (_ for _ in ()).throw(
                                AssertionError("install command must not run after a good download")))

        tools_installed = []
        failure = engine._process_tool_entry(
            {"name": "tool",
             "download": {"linux": {"url": "http://x/y", "sha256": "abc123"}},
             "install": {"linux": "pkg install tool"}},
            "linux", "/data", "", [], [], tools_installed, plugin_name="p",
        )
        assert failure is None
        assert tools_installed and "downloaded to" in tools_installed[0][1]

    def test_url_download_failure_falls_through_to_install(self, tmp_path, monkeypatch):
        """A failed url download logs and falls through to the install command,
        which then succeeds via re-check."""
        _stub(monkeypatch)
        monkeypatch.setenv("PATH", "/usr/bin")  # tmp dir deliberately off PATH
        monkeypatch.setattr(downloader, "download_and_install",
                            lambda *a, **k: downloader.DownloadResult(
                                False, None, "sha256 mismatch: expected X, got Y"))

        def fake_install(cmd):
            (tmp_path / "tool").write_text("#!/bin/sh\n")  # install makes it appear
            return (True, "installed")

        monkeypatch.setattr(tool_check, "run_install", fake_install)

        action_entries, tools_installed = [], []
        failure = engine._process_tool_entry(
            {"name": "tool", "installPath": str(tmp_path),
             "download": {"linux": {"url": "http://x/y", "sha256": "abc123"}},
             "install": {"linux": "pkg install tool"}},
            "linux", "/data", "", action_entries, [], tools_installed, plugin_name="p",
        )
        assert failure is None
        assert any("download failed" in a for a in action_entries)
        assert tools_installed and "`pkg install tool`" in tools_installed[0][1]


class TestSkipSentinel:
    """The "skip" install sentinel: install.<os> == "skip" means "not applicable
    on this OS" -- the entry short-circuits BEFORE resolve (no check subprocess,
    no install, no failure) with a verbose-only ok line. Per-OS: other OSes'
    fulfillments are untouched. Ruling: design-os-not-applicable.md."""

    def test_skip_short_circuits_before_resolve_no_check(self, monkeypatch):
        # check_tool must NEVER be called for a skipped entry -- the skip
        # strategy sits before resolve in the dispatch table.
        _stub(monkeypatch)
        monkeypatch.setattr(tool_check, "check_tool",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("check_tool must not run for a skipped entry")))

        ok_entries, action_entries = [], []
        failure = engine._process_tool_entry(
            {"name": "tmux", "install": {"windows": "skip"}},
            "windows", "/data", "", action_entries, ok_entries, [], plugin_name="p",
        )
        assert failure is None
        assert len(ok_entries) == 1
        assert "tmux" in ok_entries[0] and "skipped" in ok_entries[0]
        assert "windows" in ok_entries[0]

    def test_skip_is_per_os_other_os_still_installs(self, tmp_path, monkeypatch):
        # windows "skip" does not affect an ubuntu pass: the apt fulfillment
        # runs exactly as before.
        import bootstrap_lib.apt as apt_mod
        _stub(monkeypatch)
        monkeypatch.setenv("PATH", str(tmp_path))

        def fake_install(pkg, timeout=600):
            (tmp_path / "tmux").write_text("#!/bin/sh\n")
            return apt_mod.AptResult(True, False, f"installed {pkg} via apt")

        monkeypatch.setattr(apt_mod, "apt_install", fake_install)

        tools_installed = []
        failure = engine._process_tool_entry(
            {"name": "tmux", "installPath": str(tmp_path),
             "install": {"ubuntu": {"apt": "tmux"}, "windows": "skip"}},
            "ubuntu", "/data", "", [], [], tools_installed, plugin_name="p",
        )
        assert failure is None
        assert tools_installed and "via apt" in tools_installed[0][1]

    def test_omitted_os_key_still_fails_no_install_cmd(self, monkeypatch):
        # Omission is NOT redefined: an ubuntu-only install map on windows with
        # the tool missing still surfaces the load-bearing FAILED item.
        _stub(monkeypatch)
        monkeypatch.setenv("PATH", "/usr/bin")  # tool absent

        action_entries = []
        failure = engine._process_tool_entry(
            {"name": "tmux", "install": {"ubuntu": {"apt": "tmux"}}},
            "windows", "/data", "", action_entries, [], [], plugin_name="p",
        )
        assert failure is not None
        assert failure["install_state"] == "no_install_cmd"
        assert any("FAILED" in a for a in action_entries)
        assert engine._is_auto_fixable(failure) is False

    def test_skip_wins_over_same_os_download(self, monkeypatch):
        # "skip" beats a same-OS download block: the downloader is never invoked.
        _stub(monkeypatch)
        monkeypatch.setattr(tool_check, "check_tool",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("check_tool must not run for a skipped entry")))
        monkeypatch.setattr(downloader, "download_and_install",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("download must not run for a skipped entry")))

        failure = engine._process_tool_entry(
            {"name": "ffmpeg", "install": {"windows": "skip"},
             "download": {"windows": {"url": "http://x/y", "sha256": "ab"}}},
            "windows", "/data", "", [], [], [], plugin_name="p",
        )
        assert failure is None

    def test_manual_sentinel_semantics_unchanged(self, monkeypatch):
        # "manual" keeps its wanted-but-user-installs semantics: verify-on-PATH,
        # manual-attention manual_install failure when missing.
        _stub(monkeypatch)
        monkeypatch.setenv("PATH", "/usr/bin")  # tool absent

        action_entries = []
        failure = engine._process_tool_entry(
            {"name": "helix", "install": {"windows": "manual"}},
            "windows", "/data", "", action_entries, [], [], plugin_name="p",
        )
        assert failure is not None
        assert failure["install_state"] == "manual_install"
        assert failure["install_cmd"] is None
        assert any("manual install required" in a for a in action_entries)
        assert engine._is_auto_fixable(failure) is False

    def test_skip_line_is_verbose_only(self, monkeypatch):
        # The skip line goes to ok_entries (the verbose-only channel, shown only
        # with --verbose / log_success_checks) -- NEVER to action_entries, which
        # always display.
        _stub(monkeypatch)
        monkeypatch.setattr(tool_check, "check_tool",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("check_tool must not run for a skipped entry")))

        ok_entries, action_entries = [], []
        failure = engine._process_tool_entry(
            {"name": "w3m", "install": {"windows": "skip"}},
            "windows", "/data", "", action_entries, ok_entries, [], plugin_name="p",
        )
        assert failure is None
        assert action_entries == []
        assert len(ok_entries) == 1
