"""One unreachable marketplace must produce ONE actionable issue.

Regression guard for a reported message: a user whose manifest declared a
private marketplace they had no SSH access to, plus four plugins from it, got
FIVE issues -- the marketplace add failure, then one "plugin not found in
marketplace" per plugin -- each embedding the full multi-line git/SSH output
verbatim, and each repeated on a second message surface.

Three separate defects produced that, and each has tests here:

* the plugins phase had no notion of a prerequisite, so it attempted (and
  reported) every plugin from a marketplace that had just failed to add;
* raw CLI output went into the failure `message`, which is collated onto ONE
  display line, so a single failure could blow up the whole message;
* nothing told the user what to DO about a durable, credential-shaped fault.

The static width audit in test_message_width.py cannot see any of this: the
oversized text arrives by interpolation, which that AST scan reads as
PLACEHOLDER. These are the runtime counterpart.
"""

import pytest

from bootstrap_lib import engine, marketplace_lifecycle
from bootstrap_lib.marketplace_lifecycle import (
    CAUSE_MAX, LifecycleResult, summarize_cli_error,
)


SSH_STDERR = """\u2718 Failed to add marketplace: Failed to clone marketplace repository: \
SSH authentication failed. Please ensure your SSH keys are configured for GitHub, \
or use an HTTPS URL instead.

Original error: Cloning into 'C:\\Users\\admin\\.claude\\plugins\\marketplaces\\temp_1786470153701'...
git@github.com: Permission denied (publickey).
fatal: Could not read from remote repository.

Please make sure you have the correct access rights
and the repository exists."""


class _RecordingContext:
    """Minimal _ManifestContext stand-in capturing what a phase reported."""

    def __init__(self, manifest):
        self.manifest = manifest
        self.prefix = ""
        self.project_dir = None
        self.actions = []
        self.oks = []
        self.quiets = []
        self.failures = []
        self.unusable_marketplaces = set()

    def action(self, message, display=None, detail=None):
        self.actions.append(message)

    def ok(self, message):
        self.oks.append(message)

    def quiet(self, message):
        self.quiets.append(message)

    def fail(self, entry, display=None, detail=None, **failure):
        self.actions.append(entry)
        self.failures.append(dict(failure, _entry=entry, _display=display))


MANIFEST = {
    "marketplaces": [{"name": "gated", "source": "git@github.com:acme/gated.git"}],
    "plugins": [
        {"ref": "gated:core"},
        {"ref": "gated:designer"},
        {"ref": "gated:engineer"},
        {"ref": "gated:prototyping"},
    ],
}


@pytest.fixture
def cli_present(monkeypatch):
    monkeypatch.setattr(
        marketplace_lifecycle, "_find_claude_cli", lambda: "/usr/local/bin/claude"
    )


def _stub_local_settings_calls(monkeypatch):
    """Neutralise the loop's tail collaborators.

    `check_plugin_enabled_at_scope` and `enable_plugin_in_claude` sit past every
    `install_result.passed` guard, so a test that stubs only the install path
    still reaches them -- and `enable_plugin_in_claude` spawns a real
    `claude plugin enable` through whatever `_find_claude_cli` was patched to.
    That is inert on Windows (the fake path raises FileNotFoundError) and a
    genuine mutation of the developer's live config anywhere else, which is the
    worst shape of test bug: green on the authoring machine.
    """
    monkeypatch.setattr(
        marketplace_lifecycle, "check_plugin_enabled_at_scope",
        lambda ref, scope, project_dir: LifecycleResult(
            passed=True, ref=ref, message="stub"),
    )
    monkeypatch.setattr(
        marketplace_lifecycle, "enable_plugin_in_claude",
        lambda ref: LifecycleResult(passed=True, ref=ref, message="stub"),
    )
    monkeypatch.setattr(
        marketplace_lifecycle, "check_plugin_enabled",
        lambda ref: LifecycleResult(passed=False, ref=ref, message="stub"),
    )
    monkeypatch.setattr(
        marketplace_lifecycle, "disable_plugin_in_claude",
        lambda ref: LifecycleResult(passed=True, ref=ref, message="stub"),
    )
    monkeypatch.setattr(
        marketplace_lifecycle, "disable_plugin_at_scope",
        lambda ref, scope, project_dir: LifecycleResult(
            passed=True, ref=ref, message="stub"),
    )
    monkeypatch.setattr(
        marketplace_lifecycle, "ensure_registry_scope",
        lambda ref, scope: type(
            "S", (), {"added": False, "refused": False, "passed": True,
                      "message": "ok"})(),
    )


def _stub_add_fails(monkeypatch, *, marketplace_on_disk=False):
    """The marketplace add fails; `marketplace_on_disk` says whether a clone
    from an earlier session survives and could still serve installs."""
    monkeypatch.setattr(
        marketplace_lifecycle, "check_marketplace_exists",
        lambda name: LifecycleResult(
            passed=marketplace_on_disk, ref=name, message="stub"),
    )
    monkeypatch.setattr(
        marketplace_lifecycle, "add_marketplace",
        lambda source, name="": marketplace_lifecycle._cli_failure(
            "add", name or source, SSH_STDERR),
    )


class TestErrorSummary:
    """Raw CLI output is classified to one clause; the raw text goes to detail."""

    def test_ssh_denial_is_named(self):
        assert summarize_cli_error(SSH_STDERR) == (
            "SSH authentication failed - no access to the repository"
        )

    @pytest.mark.parametrize("stderr,expected", [
        ("fatal: repository not found", "repository not found"),
        ("ssh: Could not resolve host github.com", "network unreachable"),
        ("Plugin \"core\" not found in marketplace \"gated\".",
         "not listed in the marketplace"),
    ])
    def test_common_causes_are_classified(self, stderr, expected):
        assert summarize_cli_error(stderr) == expected

    def test_short_unrecognised_error_is_kept_whole(self):
        assert summarize_cli_error("something odd happened") == "something odd happened"

    def test_unrecognised_error_never_exceeds_the_cause_budget(self):
        long_line = "totally unrecognised failure " * 10
        assert len(summarize_cli_error(long_line)) <= CAUSE_MAX

    def test_no_ellipsis_is_ever_emitted(self):
        # Mirrors the invariant test_messages.py pins for derive_short: a cut-off
        # marker is never acceptable output.
        for probe in ("totally unrecognised failure " * 10, SSH_STDERR, "x" * 500):
            assert "..." not in summarize_cli_error(probe)
            assert "\u2026" not in summarize_cli_error(probe)

    def test_empty_output_is_reported_rather_than_blank(self):
        assert summarize_cli_error("") == "no output from the CLI"

    def test_stdout_is_classified_and_kept_in_detail(self, monkeypatch):
        monkeypatch.setattr(
            marketplace_lifecycle, "_run_claude",
            lambda args: (False, "fatal: repository not found", ""),
        )

        result = marketplace_lifecycle.add_marketplace("https://example.com", "gated")

        assert "repository not found" in result.message
        assert "repository not found" in result.detail

    def test_failure_message_is_single_line_and_detail_holds_the_raw_text(self):
        result = marketplace_lifecycle._cli_failure("add", "gated", SSH_STDERR)
        assert not result.passed
        assert "\n" not in result.message
        assert result.message.startswith("add failed (")
        assert "publickey" not in result.message
        assert "publickey" in result.detail


class TestCascadeSuppression:
    def test_plugins_of_an_unreachable_marketplace_are_not_attempted(
            self, cli_present, monkeypatch):
        _stub_add_fails(monkeypatch)

        _stub_local_settings_calls(monkeypatch)
        monkeypatch.setattr(
            marketplace_lifecycle, "check_plugin_installed",
            lambda ref: LifecycleResult(passed=False, ref=ref, message="stub"),
        )

        def must_not_run(*a, **kw):
            raise AssertionError(
                "the plugins phase attempted an install from a marketplace that "
                "could not be added -- each attempt is a doomed CLI spawn and an "
                "SSH round-trip against a host that already denied us"
            )

        monkeypatch.setattr(marketplace_lifecycle, "install_plugin", must_not_run)

        ctx = _RecordingContext(MANIFEST)
        engine._phase_marketplaces(ctx)
        engine._phase_plugins(ctx)

        assert ctx.unusable_marketplaces == {"gated"}

    def test_one_root_fault_produces_one_failure(self, cli_present, monkeypatch):
        _stub_add_fails(monkeypatch)
        _stub_local_settings_calls(monkeypatch)
        monkeypatch.setattr(
            marketplace_lifecycle, "check_plugin_installed",
            lambda ref: LifecycleResult(passed=False, ref=ref, message="stub"),
        )

        ctx = _RecordingContext(MANIFEST)
        engine._phase_marketplaces(ctx)
        engine._phase_plugins(ctx)

        assert len(ctx.failures) == 1, (
            "four plugins from one unreachable marketplace must not each become "
            "their own issue"
        )
        assert ctx.failures[0]["type"] == "marketplace"

    def test_the_skip_is_reported_once_naming_the_owning_entry(
            self, cli_present, monkeypatch):
        _stub_add_fails(monkeypatch)
        _stub_local_settings_calls(monkeypatch)
        monkeypatch.setattr(
            marketplace_lifecycle, "check_plugin_installed",
            lambda ref: LifecycleResult(passed=False, ref=ref, message="stub"),
        )

        ctx = _RecordingContext(MANIFEST)
        engine._phase_marketplaces(ctx)
        engine._phase_plugins(ctx)

        skips = [a for a in ctx.actions if "skipped" in a]
        assert len(skips) == 1
        assert "skipped 4 installs" in skips[0]
        assert "marketplace gated unavailable" in skips[0]

    def test_a_surviving_clone_is_not_treated_as_unusable(
            self, cli_present, monkeypatch):
        """The key is "unusable", not "the add failed".

        An add can fail while a clone from an earlier session is still on disk
        and still able to serve installs. Suppressing those plugins would hide
        work that would have succeeded, which is worse than the noise this
        change removes.
        """
        _stub_add_fails(monkeypatch, marketplace_on_disk=True)

        ctx = _RecordingContext(MANIFEST)
        engine._report_marketplace_add_failure(
            ctx, "gated", "git@github.com:acme/gated.git",
            marketplace_lifecycle.add_marketplace("git@...", "gated"))

        assert ctx.failures, "the add failure is still reported"
        assert ctx.unusable_marketplaces == set(), (
            "a marketplace whose clone is present must not suppress its plugins"
        )

    def test_a_failed_refresh_of_a_present_marketplace_suppresses_nothing(
            self, cli_present, monkeypatch):
        """alwaysUpdate failing is not a cascade root: the stale clone still
        installs. Only a marketplace that is absent AND unaddable is."""
        monkeypatch.setattr(
            marketplace_lifecycle, "check_marketplace_exists",
            lambda name: LifecycleResult(passed=True, ref=name, message="stub"),
        )
        monkeypatch.setattr(
            marketplace_lifecycle, "check_marketplace_current",
            lambda name: LifecycleResult(passed=False, ref=name,
                                         message="updates available"),
        )
        monkeypatch.setattr(
            marketplace_lifecycle, "update_marketplace",
            lambda name="": marketplace_lifecycle._cli_failure(
                "update", name, "ssh: Could not resolve host github.com"),
        )
        monkeypatch.setattr(marketplace_lifecycle, "load_pin_markers", lambda: {})

        manifest = {"marketplaces": [
            {"name": "gated", "source": "git@github.com:acme/gated.git",
             "alwaysUpdate": True},
        ]}
        ctx = _RecordingContext(manifest)
        engine._phase_marketplaces(ctx)

        assert ctx.failures, "the failed refresh is still reported"
        assert ctx.unusable_marketplaces == set()

    def test_plugins_from_other_marketplaces_are_untouched(
            self, cli_present, monkeypatch):
        _stub_add_fails(monkeypatch)
        _stub_local_settings_calls(monkeypatch)
        installed = []
        monkeypatch.setattr(
            marketplace_lifecycle, "check_plugin_installed",
            lambda ref: LifecycleResult(passed=False, ref=ref, message="stub"),
        )
        monkeypatch.setattr(
            marketplace_lifecycle, "install_plugin",
            lambda ref, scope="user", project_dir=None: installed.append(ref)
            or LifecycleResult(passed=True, ref=ref, message="installed"),
        )

        ctx = _RecordingContext({
            "plugins": [{"ref": "gated:core"}, {"ref": "healthy:tool"}],
        })
        ctx.unusable_marketplaces = {"gated"}
        engine._phase_plugins(ctx)

        assert installed == ["healthy:tool"]

    def test_local_only_work_still_happens_for_a_skipped_marketplace(
            self, cli_present, monkeypatch):
        """The skip covers the INSTALL, not the entry.

        Disabling a plugin, fixing its scope, enabling it at one -- all of that
        is local settings work that succeeds whether or not the marketplace is
        reachable. An earlier revision dropped the whole entry, so an
        `enabled: false` declaration silently never took effect while the line
        reported only a skip.
        """
        _stub_add_fails(monkeypatch)
        _stub_local_settings_calls(monkeypatch)
        disabled = []
        monkeypatch.setattr(
            marketplace_lifecycle, "check_plugin_enabled",
            lambda ref: LifecycleResult(passed=True, ref=ref, message="stub"),
        )
        monkeypatch.setattr(
            marketplace_lifecycle, "disable_plugin_at_scope",
            lambda ref, scope, project_dir: disabled.append(ref) or LifecycleResult(
                passed=True, ref=ref, message="disabled"),
        )
        monkeypatch.setattr(
            marketplace_lifecycle, "check_plugin_installed",
            lambda ref: LifecycleResult(passed=True, ref=ref, message="stub"),
        )

        ctx = _RecordingContext({
            "plugins": [{"ref": "gated:core", "enabled": False}],
        })
        ctx.unusable_marketplaces = {"gated"}
        engine._phase_plugins(ctx)

        assert disabled == ["gated:core"]
        assert ctx.failures == []


class TestTheSurvivingIssueIsReadableAndActionable:
    def test_no_message_surface_carries_the_raw_cli_output(
            self, cli_present, monkeypatch):
        """The whole point: the multi-line blob reaches the LOG, and nothing else.

        `message`/`user_msg`/`agent_msg` all feed collated or per-line message
        surfaces; the raw text belongs in the log and the pass record.
        """
        _stub_add_fails(monkeypatch)
        ctx = _RecordingContext(MANIFEST)
        engine._phase_marketplaces(ctx)

        failure = ctx.failures[0]
        for field in ("message", "user_msg", "agent_msg", "_entry", "_display"):
            value = failure[field]
            assert "publickey" not in value, f"{field} carries raw CLI output"
            assert "\n" not in value, f"{field} spans lines"

        assert any("publickey" in q for q in ctx.quiets), (
            "the raw output must still be recoverable from the log"
        )

    def test_the_user_line_names_a_way_out(self, cli_present, monkeypatch):
        """An SSH denial is durable -- without an exit it re-reports forever."""
        _stub_add_fails(monkeypatch)
        ctx = _RecordingContext(MANIFEST)
        engine._phase_marketplaces(ctx)

        user_msg = ctx.failures[0]["user_msg"]
        assert "gated" in user_msg
        assert '"enabled": false' in user_msg

    def test_the_agent_line_asks_rather_than_retrying(self, cli_present, monkeypatch):
        _stub_add_fails(monkeypatch)
        ctx = _RecordingContext(MANIFEST)
        engine._phase_marketplaces(ctx)

        agent_msg = ctx.failures[0]["agent_msg"]
        assert "Ask the user" in agent_msg
        assert "do not retry" in agent_msg.lower()


class TestCliOutputIsDecodedAsUtf8:
    """The CLI emits UTF-8; a locale-default decode mangles it into mojibake.

    Observed in the reported message as `\u00e2\u0153\u02d8` where the CLI had written
    `\u2718` -- cp1252 applied to UTF-8 bytes. The mangled text was then baked into
    the failure message and carried to every surface.
    """

    def test_run_claude_decodes_utf8(self, monkeypatch):
        seen = {}

        class _Completed:
            returncode = 0
            stdout = "\u2718 ok"
            stderr = ""

        def fake_run(cmd, **kwargs):
            seen.update(kwargs)
            return _Completed()

        monkeypatch.setattr(marketplace_lifecycle, "_find_claude_cli",
                            lambda: "/usr/local/bin/claude")
        monkeypatch.setattr(marketplace_lifecycle.subprocess, "run", fake_run)

        ok, stdout, _ = marketplace_lifecycle._run_claude(["plugin", "list"])

        assert ok
        assert seen.get("encoding") == "utf-8", (
            "without an explicit encoding, text=True decodes with the locale "
            "preferred encoding -- cp1252 on Windows"
        )
        assert seen.get("errors") == "replace", (
            "undecodable output must degrade, never raise inside a hook"
        )
        assert stdout == "\u2718 ok"
