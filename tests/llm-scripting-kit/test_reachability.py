"""Tests for llm_scripting_kit.reachability -- never an LLM call.

Every transport check is mocked at ``probe_endpoint`` (itself already tested
against a mocked ``urllib.request.urlopen`` in test_account.py) and every
harness check is mocked at CLI resolution / ``subprocess.run`` /
``bootstrap_lib.codex.detect_codex``. Nothing here spawns a real subprocess or
opens a real socket.

The central invariant under test: STATUS_UNKNOWN ("I could not check") must
never collapse into STATUS_UNREACHABLE ("I checked and it is down"). See
TestUnknownNeverCollapsesToUnreachable and TestCheckHarnessCodexFallback.
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from llm_scripting_kit import reachability as reach_mod
from llm_scripting_kit.account import EndpointProbe
from llm_scripting_kit.reachability import (
    STATUS_REACHABLE,
    STATUS_UNKNOWN,
    STATUS_UNREACHABLE,
    Reachability,
    check_entry,
    check_harness,
    check_many,
    check_transport,
)


# ---------------------------------------------------------------------------
# check_transport -- metadata probe only, never a completion
# ---------------------------------------------------------------------------


class TestCheckTransport:
    def test_reachable_delegates_to_probe_endpoint(self):
        ok = EndpointProbe(ok=True, endpoint="local", base_url="http://h/v1", detail="ok")
        with patch.object(reach_mod, "probe_endpoint", return_value=ok) as mock_probe:
            result = check_transport("local", timeout=3.0, project_root="/proj")
        mock_probe.assert_called_once_with("local", timeout=3.0, project_root="/proj")
        assert result.status == STATUS_REACHABLE
        assert result.checked == "models-endpoint"
        assert result.detail == "ok"

    def test_unreachable_surfaces_the_probe_detail(self):
        bad = EndpointProbe(ok=False, endpoint="local", base_url="http://h/v1", detail="unreachable: refused")
        with patch.object(reach_mod, "probe_endpoint", return_value=bad):
            result = check_transport("local")
        assert result.status == STATUS_UNREACHABLE
        assert "refused" in result.detail

    def test_never_raises_even_if_probe_endpoint_would_have(self):
        # probe_endpoint itself never raises (see account.py); this just pins
        # that this module does not add a raising path of its own.
        ok = EndpointProbe(ok=True, endpoint="x", base_url="http://h/v1", detail="ok")
        with patch.object(reach_mod, "probe_endpoint", return_value=ok):
            check_transport("x")  # must not raise


# ---------------------------------------------------------------------------
# check_harness -- CLI resolution + --version, never a model run
# ---------------------------------------------------------------------------


class TestCheckHarnessUnsupported:
    def test_unknown_harness_is_status_unknown_not_unreachable(self):
        """No known method exists for this name -- nothing was checked."""
        result = check_harness("not-a-real-harness")
        assert result.status == STATUS_UNKNOWN
        assert result.checked == "cli-version"
        assert "not-a-real-harness" in result.detail

    def test_none_harness_is_status_unknown(self):
        result = check_harness(None)
        assert result.status == STATUS_UNKNOWN
        assert "<none>" in result.detail


def _completed(stdout: bytes, returncode: int = 0):
    proc = MagicMock()
    proc.stdout = stdout
    proc.returncode = returncode
    return proc


class TestCheckHarnessClaude:
    def test_not_on_path(self):
        with patch.object(reach_mod.shutil, "which", return_value=None):
            result = check_harness("claude")
        assert result.status == STATUS_UNREACHABLE
        assert "not found on PATH" in result.detail

    def test_resolved_and_runnable(self):
        with patch.object(reach_mod.shutil, "which", return_value="/usr/local/bin/claude"), \
             patch.object(reach_mod.subprocess, "run", return_value=_completed(b"2.1.0 (Claude Code)\n")) as mock_run:
            result = check_harness("claude", timeout=4.0)
        assert result.status == STATUS_REACHABLE
        assert result.detail == "2.1.0 (Claude Code)"
        args, kwargs = mock_run.call_args
        assert args[0] == ["/usr/local/bin/claude", "--version"]
        assert kwargs["timeout"] == 4.0

    def test_nonzero_exit_is_unreachable(self):
        with patch.object(reach_mod.shutil, "which", return_value="/bin/claude"), \
             patch.object(reach_mod.subprocess, "run", return_value=_completed(b"boom", returncode=1)):
            result = check_harness("claude")
        assert result.status == STATUS_UNREACHABLE
        assert "exited 1" in result.detail

    def test_timeout_is_unreachable(self):
        with patch.object(reach_mod.shutil, "which", return_value="/bin/claude"), \
             patch.object(
                 reach_mod.subprocess, "run",
                 side_effect=subprocess.TimeoutExpired(cmd="claude --version", timeout=2.0),
             ):
            result = check_harness("claude", timeout=2.0)
        assert result.status == STATUS_UNREACHABLE
        assert "timed out" in result.detail

    def test_case_insensitive_and_trims_whitespace(self):
        with patch.object(reach_mod.shutil, "which", return_value="/bin/claude"), \
             patch.object(reach_mod.subprocess, "run", return_value=_completed(b"1.0.0")):
            result = check_harness(" Claude ")
        assert result.status == STATUS_REACHABLE


class TestCheckHarnessOpencode:
    def test_not_on_path(self):
        with patch.object(reach_mod, "resolve_opencode_cli", return_value=None):
            result = check_harness("opencode")
        assert result.status == STATUS_UNREACHABLE
        assert "not found on PATH" in result.detail

    def test_resolved_and_runnable(self):
        with patch.object(reach_mod, "resolve_opencode_cli", return_value=("/usr/bin/opencode",)), \
             patch.object(reach_mod.subprocess, "run", return_value=_completed(b"opencode 0.9.0\n")) as mock_run:
            result = check_harness("opencode")
        assert result.status == STATUS_REACHABLE
        assert result.detail == "opencode 0.9.0"
        assert mock_run.call_args.args[0] == ["/usr/bin/opencode", "--version"]

    def test_windows_cmd_prefix_is_passed_through(self):
        with patch.object(reach_mod, "resolve_opencode_cli", return_value=("cmd", "/c", "C:/opencode.cmd")), \
             patch.object(reach_mod.subprocess, "run", return_value=_completed(b"opencode 0.9.0")) as mock_run:
            check_harness("opencode")
        assert mock_run.call_args.args[0] == ["cmd", "/c", "C:/opencode.cmd", "--version"]


class TestCheckHarnessCodex:
    def test_delegates_to_bootstrap_lib_detect_codex_when_importable(self):
        from bootstrap_lib.codex import CodexDetection

        detection = CodexDetection(available=True, reason="codex-cli 0.146.0", version=(0, 146, 0))
        with patch("bootstrap_lib.codex.detect_codex", return_value=detection) as mock_detect:
            result = check_harness("codex", timeout=7.0)
        mock_detect.assert_called_once_with(timeout=7.0)
        assert result.status == STATUS_REACHABLE
        assert result.checked == "cli-version"
        assert result.detail == "codex-cli 0.146.0"

    def test_unavailable_codex_is_unreachable_not_unknown(self):
        """bootstrap_lib importable, detect_codex ran, and said no -- a real verdict."""
        from bootstrap_lib.codex import CodexDetection

        detection = CodexDetection(available=False, reason="`codex` not found on PATH")
        with patch("bootstrap_lib.codex.detect_codex", return_value=detection):
            result = check_harness("codex")
        assert result.status == STATUS_UNREACHABLE
        assert "not found on PATH" in result.detail


class TestCheckHarnessCodexFallback:
    """DEFECT 2 (regression coverage): bootstrap_lib missing must fall back to
    the same PATH + --version check claude/opencode use, not report a verdict
    on its own absence. Reproduces the live false negative: codex-cli
    installed and working, llm-scripting-kit's optional bootstrap_lib link
    absent.
    """

    def _unimportable_bootstrap_lib(self):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "bootstrap_lib.codex":
                raise ImportError("no module named bootstrap_lib.codex")
            return real_import(name, *args, **kwargs)

        return patch("builtins.__import__", side_effect=fake_import)

    def test_falls_back_to_path_probe_and_reports_reachable(self):
        with self._unimportable_bootstrap_lib(), \
             patch.object(reach_mod.shutil, "which", return_value="/usr/local/bin/codex"), \
             patch.object(reach_mod.subprocess, "run", return_value=_completed(b"codex-cli 0.150.1\n")) as mock_run:
            result = check_harness("codex", timeout=6.0)
        assert result.status == STATUS_REACHABLE
        assert result.checked == "cli-version"
        assert result.detail == "codex-cli 0.150.1"
        args, kwargs = mock_run.call_args
        assert args[0] == ["/usr/local/bin/codex", "--version"]
        assert kwargs["timeout"] == 6.0

    def test_falls_back_and_reports_unreachable_when_codex_truly_absent(self):
        with self._unimportable_bootstrap_lib(), \
             patch.object(reach_mod.shutil, "which", return_value=None):
            result = check_harness("codex")
        assert result.status == STATUS_UNREACHABLE
        assert "not found on PATH" in result.detail

    def test_missing_bootstrap_lib_is_never_reported_as_a_verdict_on_its_own(self):
        """The old (defect) behavior: ImportError alone -> unreachable, no fallback."""
        with self._unimportable_bootstrap_lib(), \
             patch.object(reach_mod.shutil, "which", return_value="/bin/codex"), \
             patch.object(reach_mod.subprocess, "run", return_value=_completed(b"codex-cli 0.150.1")):
            result = check_harness("codex")
        assert "bootstrap_lib" not in result.detail
        assert result.status == STATUS_REACHABLE


# ---------------------------------------------------------------------------
# check_entry -- dispatch by the `endpoints` JSON shape
# ---------------------------------------------------------------------------


class TestCheckEntry:
    def test_transport_entry_dispatches_to_check_transport(self):
        entry = {"kind": "transport", "base_url": "http://h/v1", "key_env": None}
        ok = EndpointProbe(ok=True, endpoint="local", base_url="http://h/v1", detail="ok")
        with patch.object(reach_mod, "probe_endpoint", return_value=ok) as mock_probe:
            result = check_entry(entry, "local", timeout=1.5)
        mock_probe.assert_called_once_with("local", timeout=1.5, project_root=None)
        assert result.status == STATUS_REACHABLE

    def test_harness_entry_dispatches_to_check_harness(self):
        entry = {"kind": "harness", "harness": "codex", "model": "gpt-5-codex"}
        from bootstrap_lib.codex import CodexDetection

        detection = CodexDetection(available=True, reason="codex-cli 0.1.0")
        with patch("bootstrap_lib.codex.detect_codex", return_value=detection):
            result = check_entry(entry, "sol", timeout=2.0)
        assert result.status == STATUS_REACHABLE
        assert result.checked == "cli-version"


class TestUnknownNeverCollapsesToUnreachable:
    """DEFECT 1 (regression coverage): a check that could not run must surface
    as STATUS_UNKNOWN through the full dispatch path, never as
    STATUS_UNREACHABLE -- a consumer gating on "not reachable" must not be
    able to read "could not determine" as "down".
    """

    def test_check_entry_maps_an_unexpected_exception_to_unknown(self):
        entry = {"kind": "transport", "base_url": "http://h/v1"}

        def _boom(*_a, **_kw):
            raise RuntimeError("network stack exploded")

        with patch.object(reach_mod, "probe_endpoint", side_effect=_boom):
            result = check_entry(entry, "local", timeout=1.0)
        assert result.status == STATUS_UNKNOWN
        assert result.status != STATUS_UNREACHABLE
        assert "network stack exploded" in result.detail

    def test_unsupported_harness_is_unknown_through_check_entry(self):
        entry = {"kind": "harness", "harness": "some-future-harness"}
        result = check_entry(entry, "future", timeout=1.0)
        assert result.status == STATUS_UNKNOWN
        assert result.status != STATUS_UNREACHABLE

    def test_codex_missing_bootstrap_lib_is_never_unknown_after_the_fallback_fix(self):
        """The fixed behavior for the SPECIFIC defect reported live: codex now
        reaches a real verdict via fallback rather than reporting unknown (or
        the old, worse bug: reporting unreachable) on a missing optional dep.
        """
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "bootstrap_lib.codex":
                raise ImportError("no module named bootstrap_lib.codex")
            return real_import(name, *args, **kwargs)

        entry = {"kind": "harness", "harness": "codex", "model": "gpt-5-codex"}
        with patch("builtins.__import__", side_effect=fake_import), \
             patch.object(reach_mod.shutil, "which", return_value="/usr/local/bin/codex"), \
             patch.object(reach_mod.subprocess, "run", return_value=_completed(b"codex-cli 0.150.1")):
            result = check_entry(entry, "sol", timeout=1.0)
        assert result.status == STATUS_REACHABLE


# ---------------------------------------------------------------------------
# check_many -- concurrent, per-entry, never raises
# ---------------------------------------------------------------------------


class TestCheckMany:
    def test_empty_input(self):
        assert check_many({}) == {}

    def test_checks_every_entry_and_keys_by_name(self):
        entries = {
            "openrouter": {"kind": "transport", "base_url": "http://a/v1"},
            "sol": {"kind": "harness", "harness": "codex"},
            "no-such-harness": {"kind": "harness", "harness": "bogus"},
        }
        ok = EndpointProbe(ok=True, endpoint="openrouter", base_url="http://a/v1", detail="ok")
        from bootstrap_lib.codex import CodexDetection

        detection = CodexDetection(available=True, reason="codex-cli 0.1.0")
        with patch.object(reach_mod, "probe_endpoint", return_value=ok), \
             patch("bootstrap_lib.codex.detect_codex", return_value=detection):
            results = check_many(entries, timeout=1.0)
        assert set(results) == set(entries)
        assert results["openrouter"].status == STATUS_REACHABLE
        assert results["sol"].status == STATUS_REACHABLE
        assert results["no-such-harness"].status == STATUS_UNKNOWN

    def test_to_json_shape(self):
        r = Reachability(status=STATUS_REACHABLE, checked="models-endpoint", detail="ok")
        assert r.to_json() == {"status": "reachable", "checked": "models-endpoint", "detail": "ok"}
