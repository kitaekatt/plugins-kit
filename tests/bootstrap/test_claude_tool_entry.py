"""Guards on bootstrap's own `claude` tool entry.

Bootstrap drives marketplace and plugin lifecycle through the `claude` CLI, so
the standalone CLI is a hard dependency. These tests pin the two properties of
that manifest entry that are easy to "simplify" into breakage, both of which
failed silently in production before the entry was filled in.
"""

import json
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MANIFEST = os.path.join(REPO_ROOT, "plugins", "bootstrap", "bootstrap.json")


@pytest.fixture(scope="module")
def claude_tool():
    with open(MANIFEST, encoding="utf-8") as fh:
        manifest = json.load(fh)
    for tool in manifest["tools"]:
        if tool["name"] == "claude":
            return tool
    pytest.fail("bootstrap.json declares no `claude` tool entry")


class TestClaudeIsAutoInstallable:
    def test_every_platform_has_a_real_install_command(self, claude_tool):
        """No platform may regress to the "manual" sentinel.

        A "manual" value routes to the manual_install branch, which only prints
        an instruction and installs nothing -- the exact state that left every
        marketplace and plugin step failing with "claude CLI not found" on
        machines that had only the VS Code extension.
        """
        for os_key in ("windows", "macos", "ubuntu"):
            cmd = claude_tool["install"][os_key]
            assert cmd != "manual", f"{os_key} regressed to the manual sentinel"
            assert cmd.strip(), f"{os_key} install command is empty"

    def test_install_path_is_declared(self, claude_tool):
        """installPath is what makes the post-install PATH linkage work.

        check_tool consults installPath before PATH, so the re-check after
        install resolves a concrete path even though the running session's PATH
        predates the install. That concrete path is what
        _link_tool_dir_to_path needs to persist the directory onto PATH; with
        no installPath the re-check falls through to shutil.which, finds
        nothing, and the install is reported as a failure.
        """
        assert claude_tool.get("installPath") == "~/.local/bin"


class TestWindowsCommandShellCompatibility:
    def test_windows_command_invokes_powershell_explicitly(self, claude_tool):
        """run_install executes install commands under bash on Windows.

        Git-for-Windows bash, not PowerShell or cmd -- so the documented
        `irm https://claude.ai/install.ps1 | iex` one-liner is a syntax error
        there (`irm` is a PowerShell alias). The command must therefore shell
        out to powershell itself. This is invisible in review and fails only on
        real Windows machines, which is why it is pinned.
        """
        cmd = claude_tool["install"]["windows"]
        assert "powershell" in cmd, "Windows install must invoke powershell explicitly"
        assert "-Command" in cmd, "powershell needs -Command to run the install one-liner"

    def test_unix_commands_are_shell_pipelines(self, claude_tool):
        for os_key in ("macos", "ubuntu"):
            cmd = claude_tool["install"][os_key]
            assert cmd.startswith("curl "), f"{os_key} should use the native installer"
            assert "| bash" in cmd, f"{os_key} native installer is piped into bash"


class TestNativeInstallerIsUsed:
    def test_no_package_manager_backends(self, claude_tool):
        """Native installs auto-update in the background; brew/winget do not.

        Anthropic documents winget, Homebrew, apt/dnf/apk installs as requiring
        manual updates. Bootstrap installing `claude` through one of those
        would pin the user to whatever version shipped that day, forever, with
        bootstrap owning the staleness.
        """
        for os_key, spec in claude_tool["install"].items():
            rendered = json.dumps(spec).lower()
            for backend in ("winget", "brew", "scoop", "apt"):
                assert backend not in rendered, (
                    f"{os_key} uses {backend}; native installer required for auto-update"
                )
