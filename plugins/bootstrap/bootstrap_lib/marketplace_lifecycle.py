"""Marketplace and plugin lifecycle operations using Claude Code CLI.

Wraps `claude plugin marketplace` and `claude plugin` commands for
marketplace and plugin management (add, remove, update, install, etc.).
"""

import json
import os
import shutil
import subprocess
import sys
from typing import NamedTuple, Optional


class LifecycleResult(NamedTuple):
    passed: bool
    ref: str
    message: str


class VersionCheckResult(NamedTuple):
    up_to_date: bool
    ref: str
    installed_version: str
    latest_version: str  # empty string if unknown
    message: str


def _query_system_shell_for_claude(is_windows: bool) -> Optional[str]:
    """Ask the OS shell directly where the claude binary lives.

    This bypasses the inherited PATH (which can be stale in hook subshells —
    e.g. git-bash launched from VS Code before `npm install -g` updated the
    User PATH). On Windows we use PowerShell's Get-Command, which queries the
    Machine+User PATH from the registry. On Unix we use a login bash, which
    sources the user's profile.
    """
    try:
        if is_windows:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "$ErrorActionPreference='SilentlyContinue'; (Get-Command claude).Source"],
                capture_output=True, text=True, timeout=10,
            )
        else:
            result = subprocess.run(
                ["bash", "-lc", "command -v claude"],
                capture_output=True, text=True, timeout=10,
            )
        if result.returncode == 0:
            path = result.stdout.strip().strip('"').strip("'")
            if path and os.path.isfile(path):
                return path
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        pass
    return None


def _find_claude_cli() -> Optional[str]:
    """Find the claude CLI binary.

    Resolution order:
      1. CLAUDE_REAL_BIN env var (set by Claude Code at runtime)
      2. CLAUDE_CODE_EXECPATH env var (alternative set by Claude Code)
      3. shutil.which("claude") on the current PATH
      4. System shell query (PowerShell on Windows, login bash on Unix) —
         sees the real User+Machine PATH even when our process inherited a
         stale one (e.g. git-bash hook launched before `npm install -g`
         updated the Windows User PATH).
      5. Well-known install locations as a final fallback.
    """
    is_windows = sys.platform == "win32" or "MSYSTEM" in os.environ

    real_bin = os.environ.get("CLAUDE_REAL_BIN")
    if real_bin:
        if os.path.isfile(real_bin):
            return real_bin
        # Some shells strip the .cmd/.exe suffix from the env var on Windows.
        if is_windows:
            for ext in (".cmd", ".exe", ".bat"):
                candidate = real_bin + ext
                if os.path.isfile(candidate):
                    return candidate

    exec_path = os.environ.get("CLAUDE_CODE_EXECPATH")
    if exec_path and os.path.isfile(exec_path):
        return exec_path

    path = shutil.which("claude")
    if path:
        return path

    discovered = _query_system_shell_for_claude(is_windows)
    if discovered:
        return discovered

    candidates = []
    if is_windows:
        appdata = os.environ.get("APPDATA")
        localappdata = os.environ.get("LOCALAPPDATA")
        userprofile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        if appdata:
            candidates.append(os.path.join(appdata, "npm", "claude.cmd"))
            candidates.append(os.path.join(appdata, "npm", "claude.exe"))
        if localappdata:
            candidates.append(os.path.join(localappdata, "Programs", "claude", "claude.exe"))
        candidates.append(os.path.join(userprofile, ".local", "bin", "claude.exe"))
        candidates.append(os.path.join(userprofile, ".local", "bin", "claude.cmd"))
    else:
        home = os.path.expanduser("~")
        candidates.extend([
            os.path.join(home, ".local", "bin", "claude"),
            "/usr/local/bin/claude",
            "/opt/homebrew/bin/claude",
        ])

    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    return None


def _run_claude(args: list, timeout: int = 120) -> tuple:
    """Run a claude CLI command. Returns (success, stdout, stderr)."""
    claude = _find_claude_cli()
    if not claude:
        return False, "", "claude CLI not found"
    # Suppress git credential prompts so marketplace updates don't block
    # non-interactive sessions when using HTTPS remotes.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(
            [claude] + args,
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        return result.returncode == 0, result.stdout, result.stderr
    except (subprocess.SubprocessError, OSError) as e:
        return False, "", str(e)


# --- Marketplace operations ---

def check_marketplace_exists(name: str) -> LifecycleResult:
    """Check if a marketplace is registered and cloned in known_marketplaces.json.

    A marketplace entry without installLocation means the JSON entry exists
    (e.g. from json_entries merge) but the repo hasn't been cloned yet.
    """
    km_path = os.path.expanduser("~/.claude/plugins/known_marketplaces.json")
    try:
        with open(km_path, "r") as f:
            data = json.load(f)
        if name in data and data[name].get("installLocation"):
            return LifecycleResult(passed=True, ref=name, message="marketplace exists")
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return LifecycleResult(passed=False, ref=name, message="marketplace not found")


def add_marketplace(source_url: str, name: str = "") -> LifecycleResult:
    """Add a marketplace via `claude plugin marketplace add`."""
    ok, stdout, stderr = _run_claude(["plugin", "marketplace", "add", source_url])
    ref = name or source_url
    if ok:
        return LifecycleResult(passed=True, ref=ref, message="marketplace added")
    return LifecycleResult(passed=False, ref=ref, message=f"add failed: {stderr.strip()}")


def remove_marketplace(name: str) -> LifecycleResult:
    """Remove a marketplace via `claude plugin marketplace remove`."""
    ok, stdout, stderr = _run_claude(["plugin", "marketplace", "remove", name])
    if ok:
        return LifecycleResult(passed=True, ref=name, message="marketplace removed")
    return LifecycleResult(passed=False, ref=name, message=f"remove failed: {stderr.strip()}")


def check_marketplace_current(name: str) -> LifecycleResult:
    """Check if a marketplace clone is up to date with its remote.

    Does a git fetch and compares local HEAD to remote tracking branch.
    Returns passed=True if already current, passed=False if behind.
    """
    km_path = os.path.expanduser("~/.claude/plugins/known_marketplaces.json")
    try:
        with open(km_path, "r") as f:
            data = json.load(f)
        install_loc = data.get(name, {}).get("installLocation", "")
        if not install_loc or not os.path.isdir(install_loc):
            return LifecycleResult(passed=False, ref=name, message="clone not found")
    except (FileNotFoundError, json.JSONDecodeError):
        return LifecycleResult(passed=False, ref=name, message="known_marketplaces.json not found")

    try:
        # Fetch latest from remote
        subprocess.run(
            ["git", "fetch", "--quiet"],
            cwd=install_loc, capture_output=True, text=True, timeout=60,
        )
        # Compare local HEAD to upstream. Check returncodes (B17): a repo
        # without an upstream tracking branch makes `rev-parse @{u}` fail,
        # which used to read as remote="" != local -> "updates available" ->
        # a doomed update attempt every pass. Treat "can't determine" as
        # current rather than stale.
        local_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=install_loc, capture_output=True, text=True, timeout=10,
        )
        remote_proc = subprocess.run(
            ["git", "rev-parse", "@{u}"],
            cwd=install_loc, capture_output=True, text=True, timeout=10,
        )
        if local_proc.returncode != 0:
            return LifecycleResult(passed=True, ref=name, message="cannot read local HEAD; skipping update check")
        if remote_proc.returncode != 0:
            return LifecycleResult(passed=True, ref=name, message="no upstream tracking branch; skipping update check")
        if local_proc.stdout.strip() == remote_proc.stdout.strip():
            return LifecycleResult(passed=True, ref=name, message="up to date")
        return LifecycleResult(passed=False, ref=name, message="updates available")
    except (subprocess.SubprocessError, OSError) as e:
        return LifecycleResult(passed=False, ref=name, message=f"check failed: {e}")


def update_marketplace(name: str = "") -> LifecycleResult:
    """Update a marketplace via `claude plugin marketplace update`.

    Falls back to `git pull` when the CLI fails with "already exists" — a known
    Claude Code CLI bug where `plugin marketplace update` attempts `git clone`
    into a directory that already contains the marketplace clone.
    """
    args = ["plugin", "marketplace", "update"]
    if name:
        args.append(name)
    ok, stdout, stderr = _run_claude(args)
    ref = name or "all"
    if ok:
        return LifecycleResult(passed=True, ref=ref, message="marketplace updated")

    # Fallback: if the CLI tried to clone into an existing directory, git pull directly.
    if "already exists" in stderr and name:
        km_path = os.path.expanduser("~/.claude/plugins/known_marketplaces.json")
        try:
            with open(km_path, "r") as f:
                km_data = json.load(f)
            install_loc = km_data.get(name, {}).get("installLocation", "")
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            install_loc = ""

        if install_loc:
            try:
                pull = subprocess.run(
                    ["git", "pull"],
                    cwd=install_loc, capture_output=True, text=True, timeout=60,
                )
                if pull.returncode == 0:
                    return LifecycleResult(passed=True, ref=ref, message="marketplace updated (git pull fallback)")
                return LifecycleResult(passed=False, ref=ref, message=f"git pull fallback failed: {pull.stderr.strip()}")
            except (subprocess.SubprocessError, OSError) as e:
                return LifecycleResult(passed=False, ref=ref, message=f"git pull fallback error: {e}")

    return LifecycleResult(passed=False, ref=ref, message=f"update failed: {stderr.strip()}")


# --- Plugin operations ---

def check_plugin_installed(plugin_ref: str) -> LifecycleResult:
    """Check if a plugin is installed in the global installed_plugins.json.

    Args:
        plugin_ref: Plugin reference in marketplace:plugin format
    """
    ip_path = os.path.expanduser("~/.claude/plugins/installed_plugins.json")
    try:
        with open(ip_path, "r") as f:
            data = json.load(f)
        plugins = data.get("plugins", {})
        # Check both marketplace:plugin and plugin@marketplace formats
        # since Claude Code CLI uses plugin@marketplace internally
        if plugin_ref in plugins:
            return LifecycleResult(passed=True, ref=plugin_ref, message="installed")
        # Try the CLI format (plugin@marketplace)
        if ":" in plugin_ref:
            marketplace, plugin_name = plugin_ref.split(":", 1)
            cli_ref = f"{plugin_name}@{marketplace}"
            if cli_ref in plugins:
                return LifecycleResult(passed=True, ref=plugin_ref, message="installed")
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return LifecycleResult(passed=False, ref=plugin_ref, message="not installed")


class ScopeCheckResult(NamedTuple):
    matches: bool
    ref: str
    installed_scope: str  # empty if not installed
    message: str


def check_plugin_scope(plugin_ref: str, desired_scope: str) -> ScopeCheckResult:
    """Check if a plugin is installed at the desired scope.

    Args:
        plugin_ref: Plugin reference in marketplace:plugin format
        desired_scope: Desired scope (user, project, local)

    Returns:
        ScopeCheckResult with matches=True if installed scope equals desired scope.
    """
    cli_ref = _to_cli_ref(plugin_ref)
    ip_path = os.path.expanduser("~/.claude/plugins/installed_plugins.json")
    try:
        with open(ip_path, "r") as f:
            data = json.load(f)
        plugins = data.get("plugins", {})
        # Try both ref formats
        entries = plugins.get(cli_ref) or plugins.get(plugin_ref) or []
        if entries:
            installed_scope = entries[0].get("scope", "")
            if installed_scope == desired_scope:
                return ScopeCheckResult(
                    matches=True, ref=plugin_ref,
                    installed_scope=installed_scope,
                    message=f"scope {installed_scope} (correct)",
                )
            return ScopeCheckResult(
                matches=False, ref=plugin_ref,
                installed_scope=installed_scope,
                message=f"installed at {installed_scope}, want {desired_scope}",
            )
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return ScopeCheckResult(
        matches=True, ref=plugin_ref,
        installed_scope="",
        message="not installed (skipping scope check)",
    )


def install_plugin(plugin_ref: str, scope: str = "user") -> LifecycleResult:
    """Install a plugin via `claude plugin install`.

    Args:
        plugin_ref: Plugin reference in marketplace:plugin format
        scope: Installation scope (user, project, local)
    """
    # Claude CLI uses plugin@marketplace format
    cli_ref = _to_cli_ref(plugin_ref)
    ok, stdout, stderr = _run_claude(["plugin", "install", cli_ref, "--scope", scope])
    if ok:
        return LifecycleResult(passed=True, ref=plugin_ref, message="installed")
    return LifecycleResult(passed=False, ref=plugin_ref, message=f"install failed: {stderr.strip()}")


def uninstall_plugin(plugin_ref: str, scope: str = "user") -> LifecycleResult:
    """Uninstall a plugin via `claude plugin uninstall`."""
    cli_ref = _to_cli_ref(plugin_ref)
    ok, stdout, stderr = _run_claude(["plugin", "uninstall", cli_ref, "--scope", scope])
    if ok:
        return LifecycleResult(passed=True, ref=plugin_ref, message="uninstalled")
    return LifecycleResult(passed=False, ref=plugin_ref, message=f"uninstall failed: {stderr.strip()}")


def update_plugin(plugin_ref: str, scope: str = "user") -> LifecycleResult:
    """Update a plugin via `claude plugin update`."""
    cli_ref = _to_cli_ref(plugin_ref)
    ok, stdout, stderr = _run_claude(["plugin", "update", cli_ref, "--scope", scope])
    if ok:
        return LifecycleResult(passed=True, ref=plugin_ref, message="updated")
    return LifecycleResult(passed=False, ref=plugin_ref, message=f"update failed: {stderr.strip()}")


def ensure_registry_scope(plugin_ref: str, desired_scope: str) -> bool:
    """Ensure installed_plugins.json has the correct scope for a plugin.

    The CLI reads scope from this file for update/uninstall commands.
    If the scope is stale (e.g., says 'project' when the plugin is actually
    at 'user' scope), CLI commands fail. This fixes the data before we run them.

    Returns True if the scope was already correct or was updated.

    Write discipline: the registry is shared with Claude Code itself and its
    mtime arms the SessionStart cooldown's registry-change bypass, so we only
    write when an entry actually changed (a no-op rewrite every pass would
    re-arm a full bootstrap pass every session), and we write atomically
    (tmp + os.replace) so a crash mid-write can't truncate the file every
    plugin depends on.
    """
    cli_ref = _to_cli_ref(plugin_ref)
    ip_path = os.path.expanduser("~/.claude/plugins/installed_plugins.json")
    try:
        with open(ip_path, "r") as f:
            data = json.load(f)
        plugins = data.get("plugins", {})
        entries = plugins.get(cli_ref) or plugins.get(plugin_ref)
        if not entries:
            return True  # not in registry, nothing to fix
        changed = False
        for entry in entries:
            if entry.get("scope") != desired_scope:
                entry["scope"] = desired_scope
                changed = True
        if not changed:
            return True  # already correct, leave the file (and its mtime) alone
        from .atomic_write import write_atomic
        write_atomic(ip_path, json.dumps(data, indent=2) + "\n")
        return True
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False


def _to_cli_ref(plugin_ref: str) -> str:
    """Convert marketplace:plugin to plugin@marketplace format for CLI."""
    if ":" in plugin_ref:
        marketplace, plugin_name = plugin_ref.split(":", 1)
        return f"{plugin_name}@{marketplace}"
    return plugin_ref


def _version_greater(a: str, b: str) -> bool:
    """Return True if version a > version b using simple numeric tuple comparison."""
    def _parse(v: str):
        parts = []
        for p in v.split("."):
            try:
                parts.append(int(p))
            except ValueError:
                parts.append(0)
        return tuple(parts)
    return _parse(a) > _parse(b)


def check_plugin_version(plugin_ref: str) -> VersionCheckResult:
    """Check if the installed plugin version matches the latest marketplace version.

    Returns up_to_date=True if current or version cannot be determined.
    Returns up_to_date=False only when a definitive newer version is available.
    """
    cli_ref = _to_cli_ref(plugin_ref)
    marketplace = plugin_ref.split(":", 1)[0] if ":" in plugin_ref else None
    plugin_name = plugin_ref.split(":", 1)[1] if ":" in plugin_ref else plugin_ref

    # Get installed version
    ip_path = os.path.expanduser("~/.claude/plugins/installed_plugins.json")
    installed_version = ""
    try:
        with open(ip_path, "r") as f:
            data = json.load(f)
        installs = data.get("plugins", {}).get(cli_ref, [])
        if installs:
            installed_version = installs[0].get("version", "")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    if not installed_version:
        return VersionCheckResult(
            up_to_date=True, ref=plugin_ref,
            installed_version="", latest_version="",
            message="not installed (skipping version check)",
        )

    if not marketplace:
        return VersionCheckResult(
            up_to_date=True, ref=plugin_ref,
            installed_version=installed_version, latest_version="",
            message=f"version {installed_version} (no marketplace)",
        )

    # Get latest version from marketplace index
    km_path = os.path.expanduser("~/.claude/plugins/known_marketplaces.json")
    latest_version = ""
    try:
        with open(km_path, "r") as f:
            km_data = json.load(f)
        install_location = km_data.get(marketplace, {}).get("installLocation", "")
        if install_location:
            mkt_path = os.path.join(install_location, ".claude-plugin", "marketplace.json")
            with open(mkt_path, "r") as f:
                mkt_data = json.load(f)
            for entry in mkt_data.get("plugins", []):
                if entry.get("name") == plugin_name:
                    latest_version = entry.get("version", "")
                    break
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    if not latest_version:
        return VersionCheckResult(
            up_to_date=True, ref=plugin_ref,
            installed_version=installed_version, latest_version="",
            message=f"version {installed_version} (marketplace version unknown)",
        )

    if installed_version == latest_version:
        return VersionCheckResult(
            up_to_date=True, ref=plugin_ref,
            installed_version=installed_version, latest_version=latest_version,
            message=f"version {installed_version} (current)",
        )

    # Compare versions directionally — only outdated if latest > installed
    if not _version_greater(latest_version, installed_version):
        return VersionCheckResult(
            up_to_date=True, ref=plugin_ref,
            installed_version=installed_version, latest_version=latest_version,
            message=f"version {installed_version} (newer than marketplace {latest_version})",
        )

    return VersionCheckResult(
        up_to_date=False, ref=plugin_ref,
        installed_version=installed_version, latest_version=latest_version,
        message=f"installed {installed_version}, latest {latest_version}",
    )


def check_plugin_min_version(plugin_ref: str, min_version: str) -> VersionCheckResult:
    """Check whether the installed plugin version satisfies a minimum version constraint.

    Returns up_to_date=True when the constraint is satisfied (installed >= min_version),
    when the plugin is not installed (skipped), or when min_version is empty. Returns
    up_to_date=False only when the installed version is definitively older than required.

    Version comparison is numeric-semver only (see _version_greater): dotted numeric
    parts are compared as int tuples; non-numeric parts coerce to 0. Pre-release
    suffixes and other non-numeric tags are not supported.
    """
    if not min_version:
        return VersionCheckResult(
            up_to_date=True, ref=plugin_ref,
            installed_version="", latest_version=min_version,
            message="no min_version declared",
        )

    cli_ref = _to_cli_ref(plugin_ref)
    ip_path = os.path.expanduser("~/.claude/plugins/installed_plugins.json")
    installed_version = ""
    try:
        with open(ip_path, "r") as f:
            data = json.load(f)
        installs = data.get("plugins", {}).get(cli_ref) or data.get("plugins", {}).get(plugin_ref) or []
        if installs:
            installed_version = installs[0].get("version", "")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    if not installed_version:
        return VersionCheckResult(
            up_to_date=True, ref=plugin_ref,
            installed_version="", latest_version=min_version,
            message="not installed (skipping min_version check)",
        )

    # Satisfied when installed == min_version or installed > min_version.
    if installed_version == min_version or _version_greater(installed_version, min_version):
        return VersionCheckResult(
            up_to_date=True, ref=plugin_ref,
            installed_version=installed_version, latest_version=min_version,
            message=f"installed {installed_version} satisfies >= {min_version}",
        )

    return VersionCheckResult(
        up_to_date=False, ref=plugin_ref,
        installed_version=installed_version, latest_version=min_version,
        message=f"installed {installed_version} < required {min_version}",
    )


def check_plugin_enabled(plugin_ref: str) -> LifecycleResult:
    """Check if a plugin is currently enabled in settings.json enabledPlugins."""
    cli_ref = _to_cli_ref(plugin_ref)
    settings_path = os.path.expanduser("~/.claude/settings.json")
    try:
        with open(settings_path, "r") as f:
            data = json.load(f)
        if data.get("enabledPlugins", {}).get(cli_ref) is True:
            return LifecycleResult(passed=True, ref=plugin_ref, message="enabled")
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return LifecycleResult(passed=False, ref=plugin_ref, message="not enabled")


def check_plugin_enabled_at_scope(plugin_ref: str, scope: str, project_dir: str = None) -> LifecycleResult:
    """Check if a plugin is enabled at a specific scope by reading the settings file directly.

    Instead of trusting installed_plugins.json (which can have stale scope metadata),
    this reads the actual settings file for the requested scope.

    Args:
        plugin_ref: Plugin reference in marketplace:plugin format
        scope: Desired scope (user, project)
        project_dir: Project directory (required for project scope)
    """
    cli_ref = _to_cli_ref(plugin_ref)
    home = os.environ.get("HOME") or os.path.expanduser("~")

    if scope == "user":
        settings_path = os.path.join(home, ".claude", "settings.json")
    elif scope == "project" and project_dir:
        settings_path = os.path.join(project_dir, ".claude", "settings.json")
    else:
        return LifecycleResult(passed=False, ref=plugin_ref, message=f"unknown scope '{scope}' or missing project_dir")

    try:
        with open(settings_path, "r") as f:
            data = json.load(f)
        if data.get("enabledPlugins", {}).get(cli_ref) is True:
            return LifecycleResult(passed=True, ref=plugin_ref, message=f"enabled at {scope} scope")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return LifecycleResult(passed=False, ref=plugin_ref, message=f"not enabled at {scope} scope")


def enable_plugin_in_claude(plugin_ref: str) -> LifecycleResult:
    """Enable a plugin in Claude Code via `claude plugin enable`."""
    cli_ref = _to_cli_ref(plugin_ref)
    ok, stdout, stderr = _run_claude(["plugin", "enable", cli_ref])
    if ok:
        return LifecycleResult(passed=True, ref=plugin_ref, message="enabled in Claude Code")
    return LifecycleResult(passed=False, ref=plugin_ref, message=f"enable failed: {stderr.strip()}")


def disable_plugin_in_claude(plugin_ref: str) -> LifecycleResult:
    """Disable a plugin in Claude Code via `claude plugin disable`."""
    cli_ref = _to_cli_ref(plugin_ref)
    ok, stdout, stderr = _run_claude(["plugin", "disable", cli_ref])
    if ok:
        return LifecycleResult(passed=True, ref=plugin_ref, message="disabled in Claude Code")
    return LifecycleResult(passed=False, ref=plugin_ref, message=f"disable failed: {stderr.strip()}")
