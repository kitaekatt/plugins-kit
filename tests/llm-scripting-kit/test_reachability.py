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


def test_module_declares_no_dead_statuses_tuple():
    """I12: reachability._STATUSES was defined and never referenced anywhere
    in plugins/ or tests/. Once removed, it must stay gone."""
    assert not hasattr(reach_mod, "_STATUSES")
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

    def test_a_resolve_failure_is_unknown_not_unreachable(self):
        """I7: an unknown endpoint or a broken registry means no request was
        ever attempted -- the three-way contract says that is STATUS_UNKNOWN
        ("I could not check"), not STATUS_UNREACHABLE ("I checked and it is
        down")."""
        unresolved = EndpointProbe(
            ok=False, endpoint="nope", base_url=None,
            detail="unknown endpoint 'nope' (known: openrouter)", resolved=False,
        )
        with patch.object(reach_mod, "probe_endpoint", return_value=unresolved):
            result = check_transport("nope")
        assert result.status == STATUS_UNKNOWN
        assert "unknown endpoint" in result.detail

    def test_reachability_docstring_does_not_overclaim_no_credential_is_read(self):
        """I9: the class docstring used to claim "nothing here ever reads
        [a key]", which is false -- check_transport delegates to
        account.probe_endpoint, which resolves a key via get_api_key for a
        keyed endpoint and sends it as a Bearer token. Only the RESULT
        (``detail``) is guaranteed key-free."""
        doc = Reachability.__doc__
        assert "detail" in doc
        assert "never carries" in doc
        assert "never reads one to begin with" not in doc

    def test_a_network_failure_after_a_successful_resolve_is_still_unreachable(self):
        """The existing behavior for a real, attempted request that failed."""
        bad = EndpointProbe(
            ok=False, endpoint="local", base_url="http://h/v1",
            detail="unreachable: refused", resolved=True,
        )
        with patch.object(reach_mod, "probe_endpoint", return_value=bad):
            result = check_transport("local")
        assert result.status == STATUS_UNREACHABLE


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


# ---------------------------------------------------------------------------
# Marked front doors: backend-health preference within ONE client deadline (U3)
# ---------------------------------------------------------------------------

import json as _json_mod  # noqa: E402

import pytest as _pytest  # noqa: E402


@_pytest.fixture
def marked_registry(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    path = tmp_path / "reg.yaml"
    path.write_text(
        "models:\n"
        "  fd:\n    base_url: http://fd.invalid:4000/v1\n    model: grp\n    frontdoor: true\n"
        "  plain:\n    base_url: http://plain.invalid:8000/v1\n    model: grp\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(path))
    return path


def _health_body(status="reachable", group="grp", protocol=1):
    return _json_mod.dumps(
        {
            "protocol": protocol,
            "frontdoor_status": "ok",
            "checked_at": "2026-09-28T00:00:00+00:00",
            "groups": {
                group: {
                    "status": status,
                    "deployments": [
                        {"id": "local", "status": "unreachable", "checked": "models-endpoint", "detail": "refused"},
                        {"id": "paid", "status": "reachable", "checked": "models-endpoint", "detail": "ok"},
                    ],
                }
            },
        }
    ).encode()


class _Recorder:
    """Records every leg's timeout and burns it on a fake clock."""

    def __init__(self, health=None, health_exc=None, fallback=None):
        self.now = 0.0
        self.health = health
        self.health_exc = health_exc
        self.fallback = fallback or EndpointProbe(
            ok=True, endpoint="fd", base_url="http://fd.invalid:4000/v1", detail="ok"
        )
        self.legs = []  # (kind, timeout)
        self.health_url = None

    def clock(self):
        return self.now

    def fetch_health(self, url, *, label, key_env, timeout, project_root):
        self.legs.append(("health", timeout))
        self.health_url = url
        self.now += timeout  # worst case: the leg burns its whole socket budget
        if self.health_exc is not None:
            return b"", self.health_exc
        return self.health, None

    def probe(self, name, *, timeout, project_root):
        self.legs.append(("models", timeout))
        self.now += timeout
        return self.fallback


def _install(monkeypatch, rec):
    monkeypatch.setattr(reach_mod, "_fetch_health", rec.fetch_health)
    monkeypatch.setattr(reach_mod, "probe_endpoint", rec.probe)
    monkeypatch.setattr(reach_mod, "_monotonic", rec.clock)


class TestMarkedFrontdoorBackendHealth:
    def test_marked_frontdoor_prefers_decisive_backend_health(self, marked_registry, monkeypatch):
        rec = _Recorder(health=_health_body("unreachable"))
        _install(monkeypatch, rec)
        result = check_transport("fd", timeout=5.0)
        assert result.status == STATUS_UNREACHABLE
        assert result.checked == "frontdoor-backends"
        # per-deployment truth stays visible in the detail
        assert "local=unreachable" in result.detail and "paid=reachable" in result.detail
        assert [kind for kind, _ in rec.legs] == ["health"]  # no /models leg
        assert rec.health_url.startswith("http://fd.invalid:4000/health/backends?budget_ms=")
        assert "/v1/" not in rec.health_url
        ok = _Recorder(health=_health_body("reachable"))
        _install(monkeypatch, ok)
        assert check_transport("fd", timeout=5.0).status == STATUS_REACHABLE

    def test_unmarked_entry_never_asks_for_backend_health(self, marked_registry, monkeypatch):
        rec = _Recorder(health=_health_body("unreachable"))
        rec.fallback = EndpointProbe(ok=True, endpoint="plain", base_url="x", detail="ok")
        _install(monkeypatch, rec)
        result = check_transport("plain", timeout=5.0)
        assert result.checked == "models-endpoint"
        assert rec.legs == [("models", 5.0)]

    @pytest.mark.parametrize(
        "kind",
        ["unknown", "exception", "malformed", "protocol", "missing-group"],
    )
    def test_marked_frontdoor_unknown_health_falls_back_within_one_deadline(
        self, marked_registry, monkeypatch, kind
    ):
        import urllib.error

        health, exc = _health_body("unknown"), None
        if kind == "exception":
            health, exc = None, urllib.error.URLError("404")
        elif kind == "malformed":
            health = b"<html>not json</html>"
        elif kind == "protocol":
            health = _health_body("reachable", protocol=99)
        elif kind == "missing-group":
            health = _health_body("reachable", group="other")
        rec = _Recorder(health=health, health_exc=exc)
        _install(monkeypatch, rec)
        result = check_transport("fd", timeout=5.0)
        assert result.status == STATUS_REACHABLE  # the /models fallback answered
        assert result.checked == "frontdoor-backends+models-fallback"
        assert [k for k, _ in rec.legs] == ["health", "models"]
        assert sum(t for _, t in rec.legs) <= 5.0 + 1e-9
        assert all(t > 0 for _, t in rec.legs)

    @pytest.mark.parametrize("outer", [0.05, 0.2, 1.0, 2.0, 5.0, 30.0])
    def test_backend_health_inner_timeout_is_strictly_less_than_outer_budget(
        self, marked_registry, monkeypatch, outer
    ):
        rec = _Recorder(health=_health_body("reachable"))
        _install(monkeypatch, rec)
        check_transport("fd", timeout=outer)
        budget_ms = int(rec.health_url.rsplit("budget_ms=", 1)[1])
        (kind, socket_timeout), = rec.legs
        assert kind == "health"
        assert budget_ms >= 1
        assert budget_ms / 1000.0 < socket_timeout <= outer

    def test_health_plus_fallback_never_exceeds_one_client_budget(
        self, marked_registry, monkeypatch
    ):
        for outer in (0.5, 1.0, 5.0, 12.0):
            rec = _Recorder(health=_health_body("unknown"))
            _install(monkeypatch, rec)
            check_transport("fd", timeout=outer)
            assert [k for k, _ in rec.legs] == ["health", "models"]
            assert sum(t for _, t in rec.legs) <= outer + 1e-9, (outer, rec.legs)

    def test_no_positive_inner_budget_skips_health_and_uses_models(
        self, marked_registry, monkeypatch
    ):
        rec = _Recorder(health=_health_body("unreachable"))
        _install(monkeypatch, rec)
        result = check_transport("fd", timeout=0.001)
        assert [k for k, _ in rec.legs] == ["models"]
        assert result.checked == "models-endpoint"
