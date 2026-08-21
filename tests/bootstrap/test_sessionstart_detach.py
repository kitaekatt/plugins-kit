"""Contract tests for session-bootstrap.sh's detached-provisioning structure.

MEASURED (2026-07-20, synthetic SessionStart hooks + timed `claude -p`):
Claude Code blocks session readiness on the SessionStart hook's completion --
process exit AND stdout-pipe EOF. A background child that inherits the hook's
stdout holds the session as long as foreground work would; a child with all
fds redirected costs zero. These tests pin the structure that encodes that
finding: the foreground path ends at "gates + emit JSON", and everything else
runs inside _provision, dispatched detached with stdin/stdout/stderr
redirected. Text-level (like the rescue drift test) -- cheap, and any
restructure that breaks the contract has to touch these exact lines.
"""

import re
from pathlib import Path

HOOK = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "bootstrap" / "hooks" / "sessionstart" / "session-bootstrap.sh"
)


def _text() -> str:
    return HOOK.read_text(encoding="utf-8")


class TestDetachedProvisionContract:
    def test_provision_function_defined(self):
        assert re.search(r"^_provision\(\) \{", _text(), re.M), (
            "_provision() must wrap all post-gate provisioning work"
        )

    def test_detached_dispatch_redirects_all_fds(self):
        # The load-bearing line: stdin, stdout AND stderr redirected away from
        # the fds the hook inherited, and backgrounded. Claude Code waits on
        # stdout-pipe EOF across children, so a dispatch that drops any
        # redirection re-blocks session startup.
        #
        # stderr's DESTINATION is deliberately not pinned. Since 0.86.2 it goes
        # to a file so a fatal shell error in _provision is diagnosable instead
        # of silent; a file satisfies the measured constraint just as /dev/null
        # does, because what holds the session is inheriting the parent's PIPE,
        # not holding an fd on disk. What must never come back is an
        # unredirected stderr.
        assert re.search(
            r"_provision </dev/null >/dev/null 2>\S+ &", _text()
        ), "detached dispatch must redirect stdin, stdout and stderr, and background"

    def test_provision_reports_its_own_fatal_shell_errors(self):
        # Regression guard for the bug that hid a fatal `set -u` abort on every
        # Mac for an unknown span. _provision runs detached with its fds
        # redirected, holds its log entries in memory until the end, and runs
        # BEFORE the engine that owns engine_output.log exists -- so without a
        # crash path of its own, a shell-level failure here produces no log
        # line, no stderr, and no user message. Silence then looks exactly like
        # a clean pass, because a healthy pass is also silent.
        text = _text()
        assert "_provision_crash()" in text, (
            "_provision needs its own crash reporter"
        )
        assert re.search(r"trap '.*_provision_crash .*' EXIT", text), (
            "_provision must install an EXIT trap that reports a non-zero exit"
        )
        crash_pos = text.index("_provision_crash() {")
        assert "bootstrap_display.pending" in text[crash_pos:], (
            "the crash path must reach the user via bootstrap_display.pending"
        )

    def test_possibly_empty_arrays_use_the_bash32_guard(self):
        # macOS ships bash 3.2, where "${ARR[@]}" on an EMPTY array is a fatal
        # `set -u` violation rather than an empty word list. ENGINE_FLAGS is
        # empty on every unflagged SessionStart and CURL_FLAGS on everything
        # that is not MinGW/MSYS, so the plain form aborted the wrapper before
        # it ever launched the engine.
        text = _text()
        for name in ("ENGINE_FLAGS", "CURL_FLAGS"):
            guarded = f'${{{name}[@]+"${{{name}[@]}}"}}'
            assert guarded in text, (
                f"{name} must use the bash 3.2-safe guarded expansion"
            )
            # The guarded form CONTAINS the plain form, so strip every guarded
            # occurrence before looking for a bare one left behind.
            assert f'"${{{name}[@]}}"' not in text.replace(guarded, ""), (
                f"{name} may be empty; the plain expansion is fatal on bash 3.2"
            )

    def test_console_mode_stays_synchronous(self):
        # Console mode must run _provision in the main shell (its engine exec
        # and inline output depend on it).
        assert re.search(
            r'if \[ -n "\$FLAG_CONSOLE" \]; then\s*\n\s*_provision\s*\n', _text()
        ), "console mode must invoke _provision synchronously"

    def test_json_emitted_before_provision_definition(self):
        # The fire-and-forget hook JSON must be emitted on the foreground path,
        # before any provisioning work.
        text = _text()
        json_pos = text.index('echo \'{"continue": true, "suppressOutput": true}\'')
        provision_pos = text.index("_provision() {")
        assert json_pos < provision_pos

    def test_gates_stay_foreground(self):
        # The cooldown/session-guard gates must run BEFORE _provision is
        # defined (i.e. on the foreground path) -- they are what keeps a
        # skipped invocation at ~zero cost and prevents concurrent passes.
        text = _text()
        cooldown_gate = text.index('if [ -f "$_COOLDOWN_FILE" ]')
        provision_pos = text.index("_provision() {")
        assert cooldown_gate < provision_pos
