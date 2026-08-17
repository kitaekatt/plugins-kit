"""The claude-plugin-test session contract.

Two mechanisms carry session isolation for a dev-tree test session, and both are
one-directional: absent the environment variables, every path here must behave
exactly as it did before.

1. CLAUDE_BOOTSTRAP_DATA_ROOT redirects everything bootstrap owns.
2. CLAUDE_PLUGIN_TEST makes the CACHE copy of each hook stand down, so the
   dev-tree copy owns the session (both copies load -- --plugin-dir does not
   shadow a marketplace install).
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "plugins" / "bootstrap"
SESSION_HOOK = BOOTSTRAP / "hooks" / "sessionstart" / "session-bootstrap.sh"
DISPLAY_HOOK = BOOTSTRAP / "hooks" / "userpromptsubmit" / "bootstrap-display.sh"

sys.path.insert(0, str(BOOTSTRAP))


class TestDataRootRedirect:
    """bootstrap_guard.data_dir is the venv resolution path used by
    reexec_under_plugin_venv, so a guard that ignored the redirect would re-exec
    every shared-lib script into the PRODUCTION venv inside a test session."""

    def test_default_is_the_canonical_location(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_BOOTSTRAP_DATA_ROOT", raising=False)
        from bootstrap_lib.bootstrap_guard import data_dir
        assert str(data_dir("p4-kit")).endswith(
            os.path.join("data", "plugins-kit", "p4-kit"))

    def test_redirect_moves_the_whole_tree(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(tmp_path))
        from bootstrap_lib.bootstrap_guard import data_dir
        assert data_dir("p4-kit") == tmp_path / "plugins-kit" / "p4-kit"

    def test_redirect_honors_a_foreign_marketplace(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(tmp_path))
        from bootstrap_lib.bootstrap_guard import data_dir
        assert data_dir("thing", "other-mkt") == tmp_path / "other-mkt" / "thing"


def _run_hook(hook: Path, plugin_root: Path, env_extra: dict, console: bool):
    """Run a COPY of a hook placed at plugin_root, so PLUGIN_ROOT is what we choose."""
    dest = plugin_root / hook.relative_to(BOOTSTRAP)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(hook.read_bytes())
    env = dict(os.environ)
    env.update(env_extra)
    args = ["bash", str(dest)] + (["--console"] if console else [])
    return subprocess.run(args, capture_output=True, text=True, timeout=120, env=env,
                          stdin=subprocess.DEVNULL)


@pytest.mark.parametrize("hook,console", [(SESSION_HOOK, True), (DISPLAY_HOOK, False)])
class TestStandDown:
    def test_cache_copy_stands_down_in_a_test_session(self, hook, console, tmp_path):
        cache_root = tmp_path / ".claude" / "plugins" / "cache" / "plugins-kit" / "bootstrap" / "9.9.9"
        res = _run_hook(hook, cache_root, {
            "CLAUDE_PLUGIN_TEST": "1",
            "CLAUDE_BOOTSTRAP_DATA_ROOT": str(tmp_path / "data-dev"),
        }, console)
        assert res.returncode == 0
        # It must not have provisioned anything: standing down means no data tree.
        assert not (tmp_path / "data-dev").exists(), \
            f"cache copy provisioned despite stand-down: {res.stdout[:400]}"

    def test_guard_requires_both_the_flag_and_a_cache_root(self, hook, console):
        """The negative case is asserted on SOURCE, not by execution, on purpose.

        Running either hook without CLAUDE_PLUGIN_TEST performs a real bootstrap
        pass -- persistent PATH writes, tool installs, marketplace refreshes --
        which a test must never do to the machine running it. So this pins the
        guard's shape instead: it must test BOTH the flag and a cache-shaped
        PLUGIN_ROOT, which is what makes it one-directional (an unset flag can
        never suppress a normal session).
        """
        src = hook.read_text(encoding="utf-8")
        assert 'if [ -n "${CLAUDE_PLUGIN_TEST:-}" ] && case "$PLUGIN_ROOT" in' in src
        assert "*/.claude/plugins/cache/*) true ;; *) false ;; esac; then" in src
