"""Tests for the per-project cooldown logic in session-bootstrap.sh and the
bootstrap-reset-cooldown helper script.

These pin the regression: Bug 2 in the statusline-not-installed-per-project
report. The cooldown used to live in a single file (`last_run_epoch`) shared
across every project, so launching claude in project B within 5 minutes of
project A would silently skip B's bootstrap. Cooldown must now be keyed by
project_dir, the throttle bumped to 3600s, and skips logged with a reset hint.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SESSION_BOOTSTRAP = REPO_ROOT / "plugins" / "bootstrap" / "hooks" / "sessionstart" / "session-bootstrap.sh"
RESET_SCRIPT = REPO_ROOT / "plugins" / "bootstrap" / "scripts" / "bootstrap-reset-cooldown.sh"


def _find_bash() -> str | None:
    """Find a POSIX-compatible bash. On Windows, prefer Git Bash over WSL bash
    (which lives at C:\\Windows\\System32\\bash.exe and can't access this VHDX)."""
    candidates = []
    if os.name == "nt":
        candidates.extend([
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
        ])
    found = shutil.which("bash")
    if found:
        candidates.append(found)
    for c in candidates:
        if c and Path(c).exists() and "WindowsApps" not in c and "System32" not in c:
            return c
    return None


BASH = _find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available on this platform")


class TestCooldownContract:
    """Static checks on session-bootstrap.sh — cheap and platform-independent."""

    def test_cooldown_is_per_project(self) -> None:
        text = SESSION_BOOTSTRAP.read_text()
        assert "_COOLDOWN_DIR=" in text, "cooldown dir variable missing"
        assert "last_run_epoch.$_PROJECT_KEY" in text, (
            "cooldown file must be keyed by project hash; "
            "shared global file regresses bug-report Bug 2"
        )

    def test_cooldown_window_is_one_hour(self) -> None:
        text = SESSION_BOOTSTRAP.read_text()
        assert "_COOLDOWN_SECS=3600" in text, (
            "cooldown bumped to 3600s now that it's per-project; "
            "reset via bootstrap-reset-cooldown when needed"
        )

    def test_skip_is_silent(self) -> None:
        text = SESSION_BOOTSTRAP.read_text()
        assert "cooldown: skipped" not in text, (
            "cooldown skips must not emit a log line — throttle is not a "
            "remediation, and the entry was leaking into user-visible bootstrap output"
        )
        # Reset helper is still referenced elsewhere in the script (install path,
        # comments) so users have a path to force a re-run when needed.
        assert "bootstrap-reset-cooldown" in text, "reset tool reference should remain"

    def test_reset_script_installed_to_local_bin(self) -> None:
        text = SESSION_BOOTSTRAP.read_text()
        assert "_RESET_SRC=" in text, "session-bootstrap should install the reset helper"
        assert "bootstrap-reset-cooldown" in text

    def test_cooldown_bypassed_on_registry_change(self) -> None:
        """A plugin install/update rewrites installed_plugins.json; a marketplace
        add/refresh rewrites known_marketplaces.json. The cooldown gate must
        bypass the throttle when either is newer than the cooldown stamp, so the
        new version's deps/shared-libs get provisioned promptly instead of after
        the throttle expires. Pins the stale-shared-lib regression."""
        text = SESSION_BOOTSTRAP.read_text()
        assert "_INSTALLED_PLUGINS=" in text, "registry path var missing"
        assert "_KNOWN_MARKETPLACES=" in text, "marketplace registry path var missing"
        # The gate must use mtime comparison (-nt) against the cooldown file so a
        # registry rewrite re-arms a real bootstrap pass.
        assert '! "$_INSTALLED_PLUGINS" -nt "$_COOLDOWN_FILE"' in text, (
            "cooldown gate must bypass when installed_plugins.json is newer "
            "than the cooldown stamp (version bump must not be throttled)"
        )
        assert '! "$_KNOWN_MARKETPLACES" -nt "$_COOLDOWN_FILE"' in text, (
            "cooldown gate must bypass when known_marketplaces.json is newer "
            "than the cooldown stamp"
        )

    def test_session_guard_bypassed_on_registry_change(self) -> None:
        """The Layer-1 session_id guard must ALSO bypass when a registry file is
        newer than the guard stamp (or there's an unresolved alert). Otherwise
        `claude --resume` re-presents the original session_id and the guard skips
        the pass even right after an update landed, so the new version is never
        provisioned — the two-restart trap. Mirrors the cooldown gate's bypass."""
        text = SESSION_BOOTSTRAP.read_text()
        assert '! "$_INSTALLED_PLUGINS" -nt "$_GUARD_FILE"' in text, (
            "session guard must bypass when installed_plugins.json is newer than "
            "the guard stamp (a resumed session after an update must re-run)"
        )
        assert '! "$_KNOWN_MARKETPLACES" -nt "$_GUARD_FILE"' in text, (
            "session guard must bypass when known_marketplaces.json is newer than "
            "the guard stamp"
        )


@needs_bash
class TestResetScript:
    """Behavioral tests for bootstrap-reset-cooldown.sh."""

    def _run(self, *args: str, env_overrides: dict | None = None) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        if env_overrides:
            env.update(env_overrides)
        return subprocess.run(
            [BASH, str(RESET_SCRIPT), *args],
            capture_output=True,
            text=True,
            env=env,
        )

    def _seed_cooldown(self, fake_home: Path, marketplace: str, project_dir: str) -> Path:
        """Create a stand-in cooldown file by replicating the script's hash."""
        # Match session-bootstrap.sh / reset script hashing: sha1 of the path string.
        out = subprocess.run(
            [BASH, "-c", f'printf "%s" "{project_dir}" | sha1sum | awk \'{{print $1}}\''],
            capture_output=True, text=True,
        )
        key = out.stdout.strip()
        assert key, f"sha1 hashing failed: {out.stderr}"
        cooldown_dir = fake_home / ".claude" / "plugins" / "data" / marketplace / "bootstrap" / "cooldowns"
        cooldown_dir.mkdir(parents=True, exist_ok=True)
        f = cooldown_dir / f"last_run_epoch.{key}"
        f.write_text(str(int(time.time())))
        return f

    def test_default_resets_current_project(self, tmp_path: Path) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        proj = tmp_path / "myproj"
        proj.mkdir()
        # Resolve the project path the way bash will see it after `cd` (e.g.
        # /c/Users/... on Git Bash for Windows), and seed the cooldown under
        # the hash of that string so default mode locates it.
        resolved = subprocess.run(
            [BASH, "-c", f'cd "{proj}" && printf %s "$PWD"'],
            capture_output=True, text=True,
        )
        bash_pwd = resolved.stdout
        assert bash_pwd, f"failed to resolve bash PWD: {resolved.stderr}"
        cooldown_file = self._seed_cooldown(fake_home, "plugins-kit", bash_pwd)
        assert cooldown_file.exists()

        result = subprocess.run(
            [BASH, "-c", f'cd "{proj}" && HOME="{fake_home}" "{BASH}" "{RESET_SCRIPT}"'],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert not cooldown_file.exists(), "default mode should reset CWD's cooldown"
        assert "reset cooldown" in result.stdout

    def test_explicit_project(self, tmp_path: Path) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        proj = tmp_path / "explicit"
        proj.mkdir()
        cooldown_file = self._seed_cooldown(fake_home, "plugins-kit", str(proj))

        result = self._run("--project", str(proj), env_overrides={"HOME": str(fake_home)})
        assert result.returncode == 0, result.stderr
        assert not cooldown_file.exists()

    def test_all_resets_every_project(self, tmp_path: Path) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        proj_a = tmp_path / "a"
        proj_b = tmp_path / "b"
        proj_a.mkdir()
        proj_b.mkdir()
        f_a = self._seed_cooldown(fake_home, "plugins-kit", str(proj_a))
        f_b = self._seed_cooldown(fake_home, "plugins-kit", str(proj_b))

        result = self._run("--all", env_overrides={"HOME": str(fake_home)})
        assert result.returncode == 0, result.stderr
        assert not f_a.exists()
        assert not f_b.exists()

    def test_status_reports_without_writes(self, tmp_path: Path) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        proj = tmp_path / "p"
        proj.mkdir()
        f = self._seed_cooldown(fake_home, "plugins-kit", str(proj))

        result = self._run("--status", env_overrides={"HOME": str(fake_home)})
        assert result.returncode == 0, result.stderr
        assert f.exists(), "--status must not delete cooldown files"
        assert "age=" in result.stdout

    def test_clear_alerts(self, tmp_path: Path) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        plugin_data = fake_home / ".claude" / "plugins" / "data" / "plugins-kit" / "bootstrap"
        plugin_data.mkdir(parents=True)
        alert = plugin_data / "bootstrap_alert.json"
        pending = plugin_data / "bootstrap_display.pending"
        alert.write_text("{}")
        pending.write_text("{}")

        result = self._run("--all", "--clear-alerts", env_overrides={"HOME": str(fake_home)})
        assert result.returncode == 0, result.stderr
        assert not alert.exists()
        assert not pending.exists()

    def test_unknown_arg_errors(self) -> None:
        result = self._run("--bogus")
        assert result.returncode == 2
        assert "unknown argument" in result.stderr


@needs_bash
class TestCooldownGateBehavior:
    """Behavioral check of session-bootstrap.sh's cooldown gate.

    Only the SKIP path is exercised here: it exits at the gate, BEFORE any
    python-install / PATH-registry / engine-fork work, so it's side-effect-free
    and safe to run hermetically (HOME pointed at a tmp dir). The RUN/bypass path
    can't be exercised in a test -- past the gate the script downloads standalone
    Python and writes the real Windows User PATH registry -- so the registry-change
    bypass is pinned by TestCooldownContract.test_cooldown_bypassed_on_registry_change
    (static) plus bash's well-defined `-nt` mtime semantics.
    """

    def _seed_fresh_cooldown(self, fake_home: Path, bash_pwd: str) -> Path:
        out = subprocess.run(
            [BASH, "-c", f'printf "%s" "{bash_pwd}" | sha1sum | awk \'{{print $1}}\''],
            capture_output=True, text=True,
        )
        key = out.stdout.strip()
        assert key, f"sha1 hashing failed: {out.stderr}"
        # The hook derives MARKETPLACE_NAME from the repo dir basename (PLUGIN_ROOT/../..),
        # so seed under REPO_ROOT.name -- not a hardcoded "plugins-kit" -- to stay correct
        # when run from a differently-named checkout (e.g. the publish mirror plugins-master).
        cd = fake_home / ".claude" / "plugins" / "data" / REPO_ROOT.name / "bootstrap" / "cooldowns"
        cd.mkdir(parents=True, exist_ok=True)
        f = cd / f"last_run_epoch.{key}"
        f.write_text(str(int(time.time())))
        return f

    def _bash_pwd(self, proj: Path) -> str:
        resolved = subprocess.run(
            [BASH, "-c", f'cd "{proj}" && printf %s "$PWD"'],
            capture_output=True, text=True,
        )
        assert resolved.stdout, f"failed to resolve bash PWD: {resolved.stderr}"
        return resolved.stdout

    def test_skips_when_fresh_and_no_registry_change(self, tmp_path: Path) -> None:
        """Fresh cooldown + no newer registry file => silent throttle. The skip
        path prints nothing to stdout (the run-path JSON is emitted only after
        the gate), so empty stdout == throttled."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        proj = tmp_path / "proj"
        proj.mkdir()
        self._seed_fresh_cooldown(fake_home, self._bash_pwd(proj))
        # No installed_plugins.json / known_marketplaces.json under fake HOME ->
        # `-nt` is false for both -> cooldown is honored.
        result = subprocess.run(
            [BASH, "-c", f'cd "{proj}" && HOME="{fake_home}" "{BASH}" "{SESSION_BOOTSTRAP}"'],
            input="", capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "", (
            f"expected a silent cooldown skip, got stdout: {result.stdout!r}"
        )

    def test_session_guard_skips_repeat_same_session(self, tmp_path: Path) -> None:
        """Same session_id + no newer registry file => Layer-1 guard skips
        silently (exits before the run path, so empty stdout == skipped). Pins
        that the registry-bypass added to Layer 1 doesn't break the normal skip."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        proj = tmp_path / "proj"
        proj.mkdir()
        # Seed last_session_id under the hook's data dir (keyed by REPO_ROOT.name,
        # matching how the hook derives MARKETPLACE_NAME from PLUGIN_ROOT/../..).
        data = fake_home / ".claude" / "plugins" / "data" / REPO_ROOT.name / "bootstrap"
        data.mkdir(parents=True, exist_ok=True)
        (data / "last_session_id").write_text("sid-abc")
        # No installed_plugins.json / known_marketplaces.json under fake HOME ->
        # `-nt` is false for both -> the guard is honored and skips.
        result = subprocess.run(
            [BASH, "-c", f'cd "{proj}" && HOME="{fake_home}" "{BASH}" "{SESSION_BOOTSTRAP}"'],
            input='{"session_id":"sid-abc"}', capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "", (
            f"expected a silent session-guard skip, got stdout: {result.stdout!r}"
        )
