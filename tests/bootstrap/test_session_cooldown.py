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
ENV_RESET_SCRIPT = REPO_ROOT / "plugins" / "bootstrap" / "scripts" / "env-reset-cooldown.sh"


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


def _hash_project_dir(value: str, path_override: str | None = None) -> str:
    """Hash a project_dir string the way hash_path() in the reset scripts does:
    sha1sum, falling back to shasum -a 1 when sha1sum is unavailable.

    Used by the seed helpers below so a test seeds a stamp under the exact key
    the script under test will compute -- including on a host with only
    shasum on PATH.
    """
    env = os.environ.copy()
    if path_override is not None:
        env["PATH"] = path_override
    out = subprocess.run(
        [BASH, "-c",
         'if command -v sha1sum >/dev/null 2>&1; then\n'
         '    printf "%s" "$1" | sha1sum | awk \'{print $1}\'\n'
         'elif command -v shasum >/dev/null 2>&1; then\n'
         '    printf "%s" "$1" | shasum -a 1 | awk \'{print $1}\'\n'
         'fi\n',
         "_", value],
        capture_output=True, text=True, env=env,
    )
    return out.stdout.strip()


class TestHashHelperFallback:
    """I10: the seed helpers must hash the same way the scripts do -- sha1sum,
    falling back to shasum -a 1 -- so a host with only shasum on PATH doesn't
    make every seeded test host-dependently red."""

    def test_hash_helper_falls_back_to_shasum_when_sha1sum_is_absent(self, tmp_path: Path) -> None:
        real_shasum = shutil.which("shasum")
        if not real_shasum:
            pytest.skip("no shasum on this host to build the fallback scenario")
        # Build a PATH with every directory that resolves sha1sum removed, but
        # every other dir kept (awk, printf's dependents, shasum itself all
        # stay reachable) -- isolates the ONE fact under test: sha1sum absent.
        kept = [
            d for d in os.environ.get("PATH", "").split(os.pathsep)
            if d and not os.path.exists(os.path.join(d, "sha1sum"))
        ]
        if not any(os.path.exists(os.path.join(d, "shasum")) for d in kept):
            # On standard Linux and Git-for-Windows layouts sha1sum and shasum
            # share /usr/bin, so filtering sha1sum's dirs removes shasum too;
            # the scenario cannot be built there -- skip, do not fail.
            pytest.skip("shasum shares a PATH dir with sha1sum on this host; "
                        "cannot isolate the fallback")
        path_override = os.pathsep.join(kept)
        key = _hash_project_dir("/some/project", path_override=path_override)
        assert key, "seed helper must fall back to shasum when sha1sum is unavailable"


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
        # Match session-bootstrap.sh / reset script hashing: sha1sum, falling
        # back to shasum -a 1 the same way the scripts do.
        key = _hash_project_dir(project_dir)
        assert key, "hashing failed (neither sha1sum nor shasum available)"
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

    def test_honors_claude_bootstrap_data_root_and_loops_marketplaces(self, tmp_path: Path) -> None:
        """I1: with BOOTSTRAP_MARKETPLACE unset, the lever must honor
        CLAUDE_BOOTSTRAP_DATA_ROOT (not just ~/.claude/plugins/data) and act on
        every <data root>/*/bootstrap/ directory holding a matching stamp,
        rather than assuming plugins-kit."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        data_root = tmp_path / "alt-data-root"
        proj = tmp_path / "proj"
        proj.mkdir()
        resolved = subprocess.run(
            [BASH, "-c", f'cd "{proj}" && printf %s "$PWD"'],
            capture_output=True, text=True,
        )
        bash_pwd = resolved.stdout
        assert bash_pwd
        key = _hash_project_dir(bash_pwd)
        assert key
        cooldown_dir = data_root / "mkt-x" / "bootstrap" / "cooldowns"
        cooldown_dir.mkdir(parents=True)
        stamp = cooldown_dir / f"last_run_epoch.{key}"
        stamp.write_text(str(int(time.time())))

        result = subprocess.run(
            [BASH, "-c",
             f'cd "{proj}" && HOME="{fake_home}" CLAUDE_BOOTSTRAP_DATA_ROOT="{data_root}" '
             f'"{BASH}" "{RESET_SCRIPT}"'],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert not stamp.exists(), "lever must find and remove the stamp under mkt-x"
        assert "reset cooldown" in result.stdout

    def test_status_lists_across_data_root_marketplaces(self, tmp_path: Path) -> None:
        """--status must also honor CLAUDE_BOOTSTRAP_DATA_ROOT and list every
        marketplace's cooldowns, not only plugins-kit."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        data_root = tmp_path / "alt-data-root"
        for mkt, proj_name in (("mkt-a", "a"), ("mkt-b", "b")):
            cooldown_dir = data_root / mkt / "bootstrap" / "cooldowns"
            cooldown_dir.mkdir(parents=True)
            key = _hash_project_dir(str(tmp_path / proj_name))
            (cooldown_dir / f"last_run_epoch.{key}").write_text(str(int(time.time())))

        result = subprocess.run(
            [BASH, "-c",
             f'HOME="{fake_home}" CLAUDE_BOOTSTRAP_DATA_ROOT="{data_root}" '
             f'"{BASH}" "{RESET_SCRIPT}" --status'],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "mkt-a" in result.stdout
        assert "mkt-b" in result.stdout

    def test_project_arg_with_trailing_slash_matches_hooks_hash(self, tmp_path: Path) -> None:
        """I2: --project <path>/ must hash the same key session-bootstrap.sh
        computes for $PWD (absolute, no trailing slash) -- not the literal
        string with the slash still attached."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        proj = tmp_path / "trailing"
        proj.mkdir()
        cooldown_file = self._seed_cooldown(fake_home, "plugins-kit", str(proj))

        result = self._run("--project", str(proj) + "/", env_overrides={"HOME": str(fake_home)})
        assert result.returncode == 0, result.stderr
        assert not cooldown_file.exists(), "trailing slash must normalize to the same hash"

    def test_project_arg_dot_matches_cwd_hash(self, tmp_path: Path) -> None:
        """--project . (cwd = the stamped project) must resolve like $PWD does."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        proj = tmp_path / "dotcwd"
        proj.mkdir()
        resolved = subprocess.run(
            [BASH, "-c", f'cd "{proj}" && printf %s "$PWD"'],
            capture_output=True, text=True,
        )
        bash_pwd = resolved.stdout
        assert bash_pwd
        cooldown_file = self._seed_cooldown(fake_home, "plugins-kit", bash_pwd)

        result = subprocess.run(
            [BASH, "-c", f'cd "{proj}" && HOME="{fake_home}" "{BASH}" "{RESET_SCRIPT}" --project .'],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert not cooldown_file.exists()

    def test_project_arg_nonexistent_is_an_error(self, tmp_path: Path) -> None:
        """A non-existent --project must error, not silently miss."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        missing = tmp_path / "does-not-exist"

        result = self._run("--project", str(missing), env_overrides={"HOME": str(fake_home)})
        assert result.returncode != 0
        assert "does-not-exist" in result.stderr or "does-not-exist" in result.stdout

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

    def _seed_alerts(self, fake_home: Path):
        plugin_data = fake_home / ".claude" / "plugins" / "data" / "plugins-kit" / "bootstrap"
        plugin_data.mkdir(parents=True, exist_ok=True)
        alert = plugin_data / "bootstrap_alert.json"
        pending = plugin_data / "bootstrap_display.pending"
        alert.write_text("{}")
        pending.write_text("{}")
        return alert, pending

    def test_clear_alerts_keeps_undelivered_pass_output(self, tmp_path: Path) -> None:
        """--clear-alerts clears the ALERT; the pending file survives.

        bootstrap_display.pending is the only channel any pass has to the
        user, and the shell's pre-Python failure paths write nothing else, so
        deleting it between a pass and the next prompt silently discards that
        pass's verdict -- possibly the message saying bootstrap could not run
        at all. Clearing an alert is the stated purpose; destroying an
        undelivered message was collateral.
        """
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        alert, pending = self._seed_alerts(fake_home)

        result = self._run("--all", "--clear-alerts", env_overrides={"HOME": str(fake_home)})
        assert result.returncode == 0, result.stderr
        assert not alert.exists(), "the alert itself must still be cleared"
        assert pending.exists(), "undelivered pass output must survive"
        assert "--force" in result.stdout, "the user must be told how to delete it"

    def test_clear_alerts_force_deletes_pending(self, tmp_path: Path) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        alert, pending = self._seed_alerts(fake_home)

        result = self._run("--all", "--clear-alerts", "--force",
                           env_overrides={"HOME": str(fake_home)})
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
        key = _hash_project_dir(bash_pwd)
        assert key, "hashing failed (neither sha1sum nor shasum available)"
        # The hook derives MARKETPLACE_NAME from the repo dir basename (PLUGIN_ROOT/../..),
        # so seed under REPO_ROOT.name -- not a hardcoded "plugins-kit" -- to stay correct
        # when run from a differently-named checkout (e.g. the publish mirror plugins-master).
        cd = fake_home / ".claude" / "plugins" / "data" / REPO_ROOT.name / "bootstrap" / "cooldowns"
        cd.mkdir(parents=True, exist_ok=True)
        f = cd / f"last_run_epoch.{key}"
        f.write_text(str(int(time.time())))
        return f

    def _plant_stub_python(self, fake_home: Path, argv_log: Path) -> Path:
        """Plant a stub at the interpreter the hook actually resolves.

        The hook resolves $HOME/.local/bin/python3 and never consults PATH.
        Without a stub there, a test that no longer exits at the gate falls
        through to _provision, which downloads a ~30MB standalone CPython (and
        on MSYS writes the real, NOT HOME-scoped, Windows User PATH registry).
        The stub keeps these tests hermetic and fast, and records the argv the
        engine would have received. It must satisfy the hook's own `-c`
        version probe.
        """
        bin_dir = fake_home / ".local" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        stub = bin_dir / "python3"
        stub.write_text(
            "#!/bin/sh\n"
            'case "$1" in -c) exit 0 ;; esac\n'
            f'printf "%s\\n" "$@" >> "{argv_log}"\n'
            "exit 0\n"
        )
        stub.chmod(0o755)
        return stub

    def _bash_pwd(self, proj: Path) -> str:
        resolved = subprocess.run(
            [BASH, "-c", f'cd "{proj}" && printf %s "$PWD"'],
            capture_output=True, text=True,
        )
        assert resolved.stdout, f"failed to resolve bash PWD: {resolved.stderr}"
        return resolved.stdout

    def test_skips_when_fresh_and_no_registry_change(self, tmp_path: Path) -> None:
        """Fresh cooldown + no newer registry file => the FULL pass is throttled.

        The signal for "throttled" is the cooldown STAMP, not stdout. A
        throttled session no longer exits at the gate: it falls through to the
        always lane (--run-kind always), which emits the ordinary hook JSON on
        its way past. What still must not happen is the full pass's stamp
        write -- that stamp is what the registry `-nt` bypass compares against,
        so an always run advancing it would re-arm the throttle for work it
        never did.
        """
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        proj = tmp_path / "proj"
        proj.mkdir()
        stamp = self._seed_fresh_cooldown(fake_home, self._bash_pwd(proj))
        before = stamp.read_text()
        self._plant_stub_python(fake_home, tmp_path / "argv.txt")
        # No installed_plugins.json / known_marketplaces.json under fake HOME ->
        # `-nt` is false for both -> cooldown is honored.
        result = subprocess.run(
            [BASH, "-c", f'cd "{proj}" && HOME="{fake_home}" "{BASH}" "{SESSION_BOOTSTRAP}"'],
            input="", capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert stamp.read_text() == before, (
            "throttled session rewrote the cooldown stamp; the always lane must "
            "leave it alone so the registry -nt bypass stays armed"
        )
        pending = (fake_home / ".claude" / "plugins" / "data" / REPO_ROOT.name
                   / "bootstrap" / "bootstrap_display.pending")
        assert not pending.exists(), (
            f"throttled session wrote a display payload: {pending.read_text()!r}"
        )

    def test_throttled_session_runs_always_lane(self, tmp_path: Path) -> None:
        """A throttled session invokes the engine with --run-kind always.

        This is the whole point of the fall-through: cheap must-be-current
        work (an env_checks entry declaring `cadence: always`) runs every
        session, while everything else keeps the 60-minute throttle. Pins the
        flag, since the engine's filtering hangs off it entirely.
        """
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        proj = tmp_path / "proj"
        proj.mkdir()
        self._seed_fresh_cooldown(fake_home, self._bash_pwd(proj))
        argv_log = tmp_path / "argv.txt"
        self._plant_stub_python(fake_home, argv_log)
        subprocess.run(
            [BASH, "-c", f'cd "{proj}" && HOME="{fake_home}" "{BASH}" "{SESSION_BOOTSTRAP}"'],
            input="", capture_output=True, text=True, timeout=60,
        )
        # The engine is launched detached; give it a moment to be exec'd.
        deadline = time.time() + 10
        while time.time() < deadline and not argv_log.exists():
            time.sleep(0.1)
        assert argv_log.exists(), "engine was never invoked on the throttled path"
        recorded = argv_log.read_text()
        assert "--run-kind" in recorded, (
            f"throttled session did not pass --run-kind; argv was:\n{recorded}"
        )
        flags = recorded.splitlines()
        assert flags[flags.index("--run-kind") + 1] == "always", (
            f"throttled session passed the wrong run kind; argv was:\n{recorded}"
        )

    def test_future_stamp_runs_full_not_throttled(self, tmp_path: Path) -> None:
        """I3: a stamp with a future epoch (backwards clock jump) must not
        throttle forever. _AGE = now - last_run goes negative and always
        satisfies `-lt $_COOLDOWN_SECS`, so a negative age must be treated as
        expired -- the pass runs full (--run-kind full), not always."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        proj = tmp_path / "proj"
        proj.mkdir()
        cd = (fake_home / ".claude" / "plugins" / "data" / REPO_ROOT.name
              / "bootstrap" / "cooldowns")
        cd.mkdir(parents=True, exist_ok=True)
        key = _hash_project_dir(self._bash_pwd(proj))
        assert key
        future_epoch = int(time.time()) + 100000
        (cd / f"last_run_epoch.{key}").write_text(str(future_epoch))
        argv_log = tmp_path / "argv.txt"
        self._plant_stub_python(fake_home, argv_log)
        subprocess.run(
            [BASH, "-c", f'cd "{proj}" && HOME="{fake_home}" "{BASH}" "{SESSION_BOOTSTRAP}"'],
            input="", capture_output=True, text=True, timeout=60,
        )
        deadline = time.time() + 10
        while time.time() < deadline and not argv_log.exists():
            time.sleep(0.1)
        assert argv_log.exists(), "engine was never invoked with a future-dated stamp"
        recorded = argv_log.read_text()
        flags = recorded.splitlines()
        assert "--run-kind" in flags
        assert flags[flags.index("--run-kind") + 1] == "full", (
            f"a future-dated stamp (negative age) throttled to the always lane "
            f"instead of running full; argv was:\n{recorded}"
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


class TestResetLeverInstall:
    """Both reset levers must land on PATH.

    env-reset-cooldown is what SKILL.md and manifest-reference.md name as the
    "re-converge my machine" lever, but only its sibling was ever installed,
    so a user following that guidance verbatim got `command not found`.
    """

    def test_both_levers_are_installed(self) -> None:
        text = SESSION_BOOTSTRAP.read_text()
        assert "for _lever in bootstrap-reset-cooldown env-reset-cooldown" in text, (
            "both levers must be installed into ~/.local/bin"
        )

    def test_env_reset_resolves_its_sibling_without_the_extension(self) -> None:
        """Installed as a shim the sibling has no .sh, so a hardcoded
        '<dir>/bootstrap-reset-cooldown.sh' misses in exactly the invocation
        the docs recommend."""
        reset = REPO_ROOT / "plugins" / "bootstrap" / "scripts" / "env-reset-cooldown.sh"
        text = reset.read_text()
        assert '"$SCRIPT_DIR/bootstrap-reset-cooldown"' in text, (
            "must also try the extension-less installed shim"
        )
        assert "command -v bootstrap-reset-cooldown" in text, (
            "must fall back to PATH"
        )


@needs_bash
class TestEnvResetScript:
    """I1: env-reset-cooldown.sh's env_state.json reset must also honor
    CLAUDE_BOOTSTRAP_DATA_ROOT and, with BOOTSTRAP_MARKETPLACE unset, act on
    every marketplace under the data root instead of assuming plugins-kit."""

    def test_honors_data_root_and_loops_marketplaces(self, tmp_path: Path) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        data_root = tmp_path / "alt-data-root"
        stamps = []
        for mkt in ("mkt-a", "mkt-b"):
            d = data_root / mkt / "bootstrap"
            d.mkdir(parents=True)
            stamp = d / "env_state.json"
            stamp.write_text("{}")
            stamps.append(stamp)

        result = subprocess.run(
            [BASH, "-c",
             f'HOME="{fake_home}" CLAUDE_BOOTSTRAP_DATA_ROOT="{data_root}" '
             f'"{BASH}" "{ENV_RESET_SCRIPT}"'],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        for stamp in stamps:
            assert not stamp.exists(), f"{stamp} should have been reset"

    def test_status_lists_across_data_root_marketplaces(self, tmp_path: Path) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        data_root = tmp_path / "alt-data-root"
        for mkt in ("mkt-a", "mkt-b"):
            d = data_root / mkt / "bootstrap"
            d.mkdir(parents=True)
            (d / "env_state.json").write_text("{}")

        result = subprocess.run(
            [BASH, "-c",
             f'HOME="{fake_home}" CLAUDE_BOOTSTRAP_DATA_ROOT="{data_root}" '
             f'"{BASH}" "{ENV_RESET_SCRIPT}" --status'],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "mkt-a" in result.stdout
        assert "mkt-b" in result.stdout


class TestLeverExecutableBits:
    """A PATH shim nobody can invoke is the same as no shim at all.

    bootstrap-reset-cooldown.sh shipped mode 100644 for its whole life, so the
    documented bare `bootstrap-reset-cooldown` command only ever worked when
    spelled `bash <path>`. The symlink existed and pointed at a file the shell
    refused to execute.
    """

    def test_installed_levers_are_executable_in_git(self) -> None:
        import subprocess as sp
        for name in ("bootstrap-reset-cooldown", "env-reset-cooldown"):
            rel = f"plugins/bootstrap/scripts/{name}.sh"
            mode = sp.run(["git", "-C", str(REPO_ROOT), "ls-files", "-s", rel],
                          capture_output=True, text=True).stdout.split()
            assert mode and mode[0] == "100755", (
                f"{rel} is committed {mode[0] if mode else '(missing)'}; it is "
                "symlinked onto PATH, so a non-executable mode makes the "
                "documented command fail with 'permission denied'"
            )
