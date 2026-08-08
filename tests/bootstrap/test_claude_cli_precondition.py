"""The marketplace/plugin phases stand down as one line when `claude` is absent.

Regression guard for the fan-out: before this gate, every marketplace and
plugin entry independently re-resolved the CLI, independently failed, and
independently reported. A user with ten declared plugins saw a numbered list of
ten broken things -- nine of which were the same fact restated, printed ABOVE
the one line that named the cause -- and a fix-all prompt offering to retry all
ten operations that could not possibly succeed.
"""

import pytest

from bootstrap_lib import engine


class _RecordingContext:
    """Minimal _ManifestContext stand-in capturing what a phase reported."""

    def __init__(self, manifest):
        self.manifest = manifest
        self.prefix = ""
        self.project_dir = None
        self.actions = []
        self.oks = []
        self.failures = []

    def action(self, message, display=None, detail=None):
        self.actions.append(message)

    def ok(self, message):
        self.oks.append(message)

    def fail(self, entry, display=None, detail=None, **failure):
        self.failures.append(entry)


MANIFEST = {
    "marketplaces": [
        {"name": "plugins-kit", "source": "https://example.invalid/a.git"},
        {"name": "other", "source": "https://example.invalid/b.git"},
    ],
    "plugins": [
        {"ref": "plugins-kit:bootstrap"},
        {"ref": "plugins-kit:git-kit"},
        {"ref": "plugins-kit:p4-kit"},
    ],
}


@pytest.fixture
def cli_missing(monkeypatch):
    from bootstrap_lib import marketplace_lifecycle

    monkeypatch.setattr(marketplace_lifecycle, "_find_claude_cli", lambda: None)


@pytest.fixture
def cli_present(monkeypatch):
    from bootstrap_lib import marketplace_lifecycle

    monkeypatch.setattr(
        marketplace_lifecycle, "_find_claude_cli", lambda: "/usr/local/bin/claude"
    )


class TestPhasesStandDownWhenCliMissing:
    def test_plugins_phase_emits_one_entry_and_no_failures(self, cli_missing):
        ctx = _RecordingContext(MANIFEST)
        engine._phase_plugins(ctx)

        assert len(ctx.actions) == 1, "three plugins must not produce three reports"
        assert ctx.failures == [], (
            "the tools phase owns the actionable report; per-entry failures here "
            "would pad the fix-all list with impossible operations"
        )
        assert "skipped 3 entries" in ctx.actions[0]
        assert "claude CLI unavailable" in ctx.actions[0]

    def test_marketplaces_phase_emits_one_entry_and_no_failures(self, cli_missing):
        ctx = _RecordingContext(MANIFEST)
        engine._phase_marketplaces(ctx)

        assert len(ctx.actions) == 1
        assert ctx.failures == []
        assert "skipped 2 entries" in ctx.actions[0]

    def test_singular_entry_is_not_reported_as_entries(self, cli_missing):
        ctx = _RecordingContext({"plugins": [{"ref": "plugins-kit:bootstrap"}]})
        engine._phase_plugins(ctx)

        assert "skipped 1 entry" in ctx.actions[0]


class TestResolverIsNotMemoized:
    def test_install_mid_pass_is_observed(self, monkeypatch):
        """A miss must not be cached: the tools phase can install claude
        after an early probe, and a stale negative would then suppress every
        marketplace and plugin operation for the rest of the pass."""
        from bootstrap_lib import marketplace_lifecycle

        results = [None, "/usr/local/bin/claude"]
        monkeypatch.setattr(
            marketplace_lifecycle, "_find_claude_cli", lambda: results.pop(0)
        )

        assert marketplace_lifecycle.resolve_claude_cli() is None
        assert marketplace_lifecycle.resolve_claude_cli() == "/usr/local/bin/claude"


class _ReachedTheLoop(Exception):
    """Raised by a stubbed collaborator to prove the gate passed control on."""


class TestGateIsInertWhenCliPresent:
    def test_plugins_phase_proceeds_past_the_gate(self, cli_present, monkeypatch):
        """With the CLI resolvable, control must reach the per-plugin loop.

        Asserted by making the loop's first collaborator raise: driving the
        whole loop would mean stubbing the entire registry/version/scope
        pipeline, which tests those collaborators rather than this gate.
        """
        from bootstrap_lib import marketplace_lifecycle

        def reached(plugin_ref):
            raise _ReachedTheLoop(plugin_ref)

        monkeypatch.setattr(marketplace_lifecycle, "check_plugin_installed", reached)

        ctx = _RecordingContext(MANIFEST)
        with pytest.raises(_ReachedTheLoop) as excinfo:
            engine._phase_plugins(ctx)

        assert str(excinfo.value) == "plugins-kit:bootstrap"
        assert not any("skipped" in a for a in ctx.actions)

    def test_marketplaces_phase_proceeds_past_the_gate(self, cli_present, monkeypatch):
        from bootstrap_lib import marketplace_lifecycle

        def reached(name):
            raise _ReachedTheLoop(name)

        monkeypatch.setattr(marketplace_lifecycle, "check_marketplace_exists", reached)

        ctx = _RecordingContext(MANIFEST)
        with pytest.raises(_ReachedTheLoop):
            engine._phase_marketplaces(ctx)

        assert not any("skipped" in a for a in ctx.actions)
