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


class TestStandDownPreservesPinIntent:
    """The gate must not drop CLI-independent state on its way out.

    Because the resolver is un-memoized, the CLI can appear mid-pass (the
    tools phase installs it). A manifest that stood down here can therefore be
    followed by one that runs the loop -- and if this stand-down forgot the
    pin, that later manifest's unpinned entry would take the
    load_pin_markers() branch and silently release a pin the user declared.
    """

    def test_declared_pins_are_recorded_despite_standing_down(self, cli_missing):
        pinned = engine._pinned_marketplaces_this_run
        before = set(pinned)
        try:
            pinned.clear()
            ctx = _RecordingContext({
                "marketplaces": [
                    {"name": "pinned-mkt", "source": "https://example.invalid/a.git",
                     "pin": "abc123"},
                    {"name": "unpinned-mkt", "source": "https://example.invalid/b.git"},
                ]
            })
            engine._phase_marketplaces(ctx)

            assert "pinned-mkt" in pinned, (
                "a declared pin must survive the stand-down or a later manifest "
                "can silently unpin it"
            )
            assert "unpinned-mkt" not in pinned, "only declared pins are recorded"
        finally:
            pinned.clear()
            pinned.update(before)


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
        """Stub load_pin_markers, NOT check_marketplace_exists.

        For an unpinned entry the loop consults load_pin_markers() BEFORE
        check_marketplace_exists, and a marker hit runs release_marketplace_pin
        + update_marketplace -- a real `git checkout` in the developer's live
        marketplace clone -- then `continue`s. Stubbing only the later call
        therefore lets the first entry mutate real state while the test still
        passes on the second. Gate on the earliest real-state call instead.
        """
        from bootstrap_lib import marketplace_lifecycle

        def reached():
            raise _ReachedTheLoop("load_pin_markers")

        monkeypatch.setattr(marketplace_lifecycle, "load_pin_markers", reached)

        ctx = _RecordingContext(MANIFEST)
        with pytest.raises(_ReachedTheLoop):
            engine._phase_marketplaces(ctx)

        assert not any("skipped" in a for a in ctx.actions)
