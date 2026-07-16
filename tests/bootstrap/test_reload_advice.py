"""Tests for the reload/restart advisory helpers in bootstrap_lib.engine.

These pin the "nag only when bootstrap can PROVE the running session is missing
plugin code" contract: a plugin INSTALLED during the pass (Step 4b) is not yet
loaded by Claude Code, so it earns a nag. The restart-vs-reload branch is the
MEASURED rule (references/plugin-reload-lifecycle.md): only a SessionStart hook
forces a restart (a fresh session re-fires it); everything else -- event hooks,
skills, commands -- goes live via /reload-plugins. A plugin merely updated at
session start is NOT in this list and so is never nagged here.
"""

import json

from bootstrap_lib.engine import (
    _bootstrap_stale_advice,
    _plugin_ships_sessionstart_hook,
    _reload_advice,
    _resolve_newly_installed,
)
from bootstrap_lib.plugin_resolve import PluginInfo


def _plugin(tmp_path, name, *, hooks=None, hooks_in_plugin_json=False):
    """Build a plugin install dir. ``hooks`` is the event->config map to register
    (via hooks/hooks.json by default, or plugin.json when hooks_in_plugin_json)."""
    install = tmp_path / name
    (install / ".claude-plugin").mkdir(parents=True)
    manifest = {"name": name, "version": "1.0"}
    if hooks and hooks_in_plugin_json:
        manifest["hooks"] = hooks
    (install / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    if hooks and not hooks_in_plugin_json:
        (install / "hooks").mkdir()
        (install / "hooks" / "hooks.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    return PluginInfo(name=name, install_path=str(install), version="1.0", marketplace="mkt")


class TestShipsSessionStartHook:
    def test_sessionstart_in_hooks_json(self, tmp_path):
        pi = _plugin(tmp_path, "ss", hooks={"SessionStart": [{"hooks": []}]})
        assert _plugin_ships_sessionstart_hook(pi.install_path) is True

    def test_sessionstart_in_plugin_json(self, tmp_path):
        pi = _plugin(tmp_path, "ssp", hooks={"SessionStart": [{"hooks": []}]}, hooks_in_plugin_json=True)
        assert _plugin_ships_sessionstart_hook(pi.install_path) is True

    def test_empty_sessionstart_array_is_false(self, tmp_path):
        # SessionStart present but empty registers no actual hook -> no restart.
        pi = _plugin(tmp_path, "empty-ss", hooks={"SessionStart": []})
        assert _plugin_ships_sessionstart_hook(pi.install_path) is False

    def test_event_hook_only_is_false(self, tmp_path):
        # A UserPromptSubmit hook reloads via /reload-plugins -> no restart needed.
        pi = _plugin(tmp_path, "ups", hooks={"UserPromptSubmit": [{"hooks": []}]})
        assert _plugin_ships_sessionstart_hook(pi.install_path) is False

    def test_no_hooks(self, tmp_path):
        pi = _plugin(tmp_path, "plain")
        assert _plugin_ships_sessionstart_hook(pi.install_path) is False

    def test_missing_files_is_false(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert _plugin_ships_sessionstart_hook(str(empty)) is False


class TestReloadAdvice:
    def test_none_when_no_new_plugins(self):
        assert _reload_advice([]) is None

    def test_restart_when_a_new_plugin_has_sessionstart(self, tmp_path):
        plugins = [
            _plugin(tmp_path, "skillonly"),
            _plugin(tmp_path, "hooky", hooks={"SessionStart": [{"hooks": []}]}),
        ]
        msg = _reload_advice(plugins)
        assert msg is not None
        assert "restart Claude" in msg
        assert "your IDE" in msg
        # Informational phrasing -- the user decides when to restart.
        assert "ACTION" not in msg
        # Plain instruction -- no confusing internals.
        assert "SessionStart" not in msg and "re-fire" not in msg
        # Both names listed.
        assert "hooky" in msg and "skillonly" in msg

    def test_reload_when_only_event_hooks_or_skills(self, tmp_path):
        plugins = [
            _plugin(tmp_path, "skillonly"),
            _plugin(tmp_path, "ups", hooks={"UserPromptSubmit": [{"hooks": []}]}),
        ]
        msg = _reload_advice(plugins)
        assert msg is not None
        assert "/reload-plugins" in msg
        assert "restart Claude" not in msg


class TestResolveNewlyInstalled:
    """The registry before/after diff that drives Step 4d -- pins the cache-kit
    regression: a plugin installed via a layered `plugins:` manifest lands in the
    registry before Step 4's scan (so it's absorbed by Step 4, never appearing in
    Step 4b's new_plugins) AND has no bootstrap.json (so list_enabled_plugins never
    returns it). install_path must therefore come from the registry, not the
    processed plugin lists."""

    def test_detects_plugin_installed_this_pass(self):
        before = {"bootstrap@plugins-kit", "git-kit@plugins-kit"}
        after = {
            "bootstrap@plugins-kit": "/cache/bootstrap/0.14.0",
            "git-kit@plugins-kit": "/cache/git-kit/0.1.0",
            "cache-kit@plugins-kit": "/cache/cache-kit/0.5.1",
        }
        out = _resolve_newly_installed(before, after)
        assert [pi.name for pi in out] == ["cache-kit"]
        assert out[0].install_path == "/cache/cache-kit/0.5.1"
        assert out[0].marketplace == "plugins-kit"

    def test_no_change_is_empty(self):
        after = {"bootstrap@plugins-kit": "/cache/bootstrap"}
        assert _resolve_newly_installed({"bootstrap@plugins-kit"}, after) == []

    def test_updated_not_installed_is_not_flagged(self):
        # An already-present plugin (a version bump) is not in after-minus-before.
        before = {"bootstrap@plugins-kit"}
        after = {"bootstrap@plugins-kit": "/cache/bootstrap/0.15.0"}
        assert _resolve_newly_installed(before, after) == []

    def test_missing_installpath_is_skipped(self):
        # A newly-present ref with no installPath in the registry is skipped, not crashed.
        assert _resolve_newly_installed(set(), {"ghost@mkt": None}) == []


class TestBootstrapStaleAdvice:
    """The bootstrap self-staleness restart nag: when the registry records a newer
    bootstrap than the one running this session, /reload-plugins won't re-fire its
    SessionStart pass -- only a restart will."""

    def _registry(self, tmp_path, version):
        reg = tmp_path / "installed_plugins.json"
        reg.write_text(
            json.dumps({"plugins": {"bootstrap@plugins-kit": [{"version": version, "installPath": "/x"}]}}),
            encoding="utf-8",
        )
        return str(reg)

    def test_notices_when_registry_newer(self, tmp_path):
        msg = _bootstrap_stale_advice("0.14.0", "bootstrap", "plugins-kit", self._registry(tmp_path, "0.15.0"))
        assert msg is not None
        assert "0.15.0" in msg
        assert "restart Claude" in msg
        # Plain instruction -- no confusing internals about SessionStart re-firing.
        assert "SessionStart" not in msg and "reload-plugins" not in msg

    def test_silent_when_equal(self, tmp_path):
        assert _bootstrap_stale_advice("0.14.0", "bootstrap", "plugins-kit", self._registry(tmp_path, "0.14.0")) is None

    def test_silent_when_running_ahead(self, tmp_path):
        # dev tree ahead of cache -> no false nag (the self-guarding direction)
        assert _bootstrap_stale_advice("0.14.0", "bootstrap", "plugins-kit", self._registry(tmp_path, "0.9.0")) is None

    def test_numeric_semver_not_string_compare(self, tmp_path):
        # 0.9.0 vs 0.14.0: a string compare would call "0.9" > "0.14"; numeric says 14 > 9.
        assert _bootstrap_stale_advice("0.9.0", "bootstrap", "plugins-kit", self._registry(tmp_path, "0.14.0")) is not None

    def test_silent_when_registry_missing(self, tmp_path):
        assert _bootstrap_stale_advice("0.14.0", "bootstrap", "plugins-kit", str(tmp_path / "nope.json")) is None

    def test_silent_when_no_running_version(self, tmp_path):
        assert _bootstrap_stale_advice("", "bootstrap", "plugins-kit", self._registry(tmp_path, "0.15.0")) is None


class TestEmitNoRelayDirective:
    """emit_success_response must NOT wrap reload/restart notices in an
    'ACTION REQUIRED -- surface this now' relay directive. The directive made the
    session's Claude present a routine update notice as urgent; whether and when
    to restart is the user's call, so notices ride in the body as ordinary
    display lines (visible in systemMessage and additionalContext alike)."""

    def _emit(self, tmp_path, log_content="some bootstrap log"):
        from bootstrap_lib.engine import emit_success_response
        out = tmp_path / "pending.json"
        emit_success_response(
            log_content, label="mkt:bootstrap@1.0", output_file=str(out),
        )
        return json.loads(out.read_text(encoding="utf-8"))

    def test_notice_rides_in_body_without_directive(self, tmp_path):
        notice = "--- mkt:bootstrap@1.0 notice: bootstrap was updated to 9.9.9; it will load next time you restart Claude (or your IDE). ---"
        r = self._emit(tmp_path, f"{notice}\nsome bootstrap log")
        ac = r["hookSpecificOutput"]["additionalContext"]
        assert "ACTION REQUIRED" not in ac
        assert "surface this to the user" not in ac.lower()
        assert notice in ac
        assert notice in r["systemMessage"]

    def test_additional_context_matches_system_message(self, tmp_path):
        r = self._emit(tmp_path)
        assert r["hookSpecificOutput"]["additionalContext"] == r["systemMessage"]
        assert r["systemMessage"].startswith("mkt:bootstrap@1.0 -> bootstrap complete")
