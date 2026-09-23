"""Tests for llm_scripting_kit.declaration: describe(), run(), order_by_pace.

The design is docs/planning/quota-resilient-dispatch/declaration-format-design.md
(D4 "The one API", D5 pace and the ordering rule, D6 re-selection, owner
directions 13-17). Every test injects its entry map, its reachability results
and its quota verdicts, so nothing here reads the host's config, spawns a CLI,
or opens a socket.
"""

from __future__ import annotations

import logging
import subprocess
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pytest

from llm_scripting_kit import declaration as decl
from llm_scripting_kit.completion.halt import (
    HALT_AUTH,
    HALT_QUOTA,
)
from llm_scripting_kit.model_endpoints import HARNESS_KIND, TRANSPORT_KIND, EndpointEntry
from llm_scripting_kit.models import EndpointResolveError
from llm_scripting_kit.reachability import (
    STATUS_REACHABLE,
    STATUS_UNKNOWN,
    STATUS_UNREACHABLE,
    Reachability,
)
from llm_scripting_kit.usage_budget import (
    STATUS_AVAILABLE,
    STATUS_NO_DATA,
    STATUS_OUT_OF_QUOTA,
    STATUS_UNDER_QUOTA,
    Budget,
    ConserveSpec,
)

SEVEN_DAY = ConserveSpec(pool="seven_day")
RESET = 1_790_000_000  # 2026-09-21 14:13:20 UTC


def _harness(entry_id, harness="claude", *, paced=False, model=None, tier=None, family=None):
    return EndpointEntry(
        id=entry_id,
        base_url=None,
        model=model or f"{entry_id}-model",
        kind=HARNESS_KIND,
        harness=harness,
        tier=tier,
        family=family,
        conserve_usage=SEVEN_DAY if paced else None,
    )


def _transport(entry_id):
    return EndpointEntry(
        id=entry_id, base_url=f"http://{entry_id}.invalid/v1", model=f"{entry_id}-model"
    )


def _reach(status=STATUS_REACHABLE):
    return Reachability(status=status, checked="cli-version", detail=status)


@pytest.fixture
def quota(monkeypatch):
    """Pin every paced entry's verdict and fresh reading per test.

    ``pinned[id]`` is the stored status; ``fresh[id]`` is the (remaining,
    window_remaining) pair an unpinned evaluate() would read now. An entry
    absent from ``fresh`` reads as no data.
    """

    class Control:
        pinned: dict[str, str] = {}
        fresh: dict[str, tuple[Optional[float], Optional[float]]] = {}
        resets: dict[str, int] = {}

    control = Control()

    def pinned_evaluate(entry_id, spec, harness, **_kw):
        status = control.pinned.get(entry_id, STATUS_NO_DATA)
        return Budget(
            status=status, pool=spec.pool, detail=status,
            resets_at=control.resets.get(entry_id, RESET),
        )

    def fresh_reading(entry_id, spec, harness):
        remaining, window = control.fresh.get(entry_id, (None, None))
        if remaining is None:
            return Budget(status=STATUS_NO_DATA, pool=spec.pool, detail="none")
        return Budget(
            status=STATUS_AVAILABLE, pool=spec.pool, detail="fresh",
            remaining=remaining, window_remaining=window, resets_at=RESET,
        )

    monkeypatch.setattr(decl, "pinned_evaluate", pinned_evaluate)
    monkeypatch.setattr(decl, "_fresh_reading", fresh_reading)
    return control


@pytest.fixture
def no_probe(monkeypatch):
    """Fail loudly if describe() probes anything the cache did not answer."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("describe() probed live despite a complete cache")

    monkeypatch.setattr(decl, "check_many", refuse)


# ---------------------------------------------------------------------------
# order_by_pace -- D5's one ordering rule, pure
# ---------------------------------------------------------------------------


@dataclass
class _Paced:
    id: str
    pace: Optional[float]


class TestOrderByPace:
    def test_the_owners_example(self):
        # D5: opus at 76% and astra at 120%; the two unpaced local entries keep
        # their places and only the paced pair swaps.
        items = [
            _Paced("qwen3.8-5090", None),
            _Paced("opus", 0.76),
            _Paced("astra", 1.20),
            _Paced("qwen3.8-m5pro", None),
        ]
        assert [i.id for i in decl.order_by_pace(items)] == [
            "qwen3.8-5090", "astra", "opus", "qwen3.8-m5pro",
        ]

    def test_ties_keep_declaration_order(self):
        items = [_Paced("fable", 0.76), _Paced("astra", None), _Paced("opus", 0.76)]
        assert [i.id for i in decl.order_by_pace(items)] == ["fable", "astra", "opus"]

    def test_unpaced_entries_never_move(self):
        items = [_Paced("a", None), _Paced("b", 0.1), _Paced("c", None), _Paced("d", 3.0)]
        assert [i.id for i in decl.order_by_pace(items)] == ["a", "d", "c", "b"]

    def test_zero_pace_sorts_last_among_paced(self):
        items = [_Paced("spent", 0.0), _Paced("x", None), _Paced("ok", 0.5)]
        assert [i.id for i in decl.order_by_pace(items)] == ["ok", "x", "spent"]

    def test_pure(self):
        items = [_Paced("a", 0.1), _Paced("b", 0.9)]
        decl.order_by_pace(items)
        assert [i.id for i in items] == ["a", "b"]


# ---------------------------------------------------------------------------
# describe() -- ordering, pace, default
# ---------------------------------------------------------------------------


class TestDescribeOrdering:
    def test_owners_example_through_describe(self, quota, no_probe):
        entries = {
            "qwen3.8-5090": _transport("qwen3.8-5090"),
            "opus": _harness("opus", paced=True),
            "astra": _harness("astra", "codex", paced=True),
            "qwen3.8-m5pro": _transport("qwen3.8-m5pro"),
        }
        quota.pinned = {"opus": STATUS_UNDER_QUOTA, "astra": STATUS_AVAILABLE}
        quota.fresh = {"opus": (0.38, 0.5), "astra": (0.6, 0.5)}
        ranking = decl.describe(
            list(entries), caller="process", entries=entries,
            reachability_cache={name: _reach() for name in entries},
        )
        assert [e.id for e in ranking.rendered_entries] == [
            "qwen3.8-5090", "astra", "opus", "qwen3.8-m5pro",
        ]
        by_id = {e.id: e for e in ranking.rendered_entries}
        assert by_id["opus"].pace == pytest.approx(0.76)
        assert by_id["astra"].pace == pytest.approx(1.2)
        assert by_id["qwen3.8-5090"].pace is None
        assert ranking.default.id == "qwen3.8-5090"
        assert [e.declared_index for e in ranking.rendered_entries] == [0, 2, 1, 3]

    def test_the_d5_render_example(self, quota, no_probe):
        # fable and opus read the same seven_day pool, so they tie at 76% and
        # keep declared order; astra and sol are out of quota and stay visible
        # with their reset time; sonnet is unpaced.
        entries = {
            "fable": _harness("fable", paced=True),
            "astra": _harness("astra", "codex", paced=True),
            "sol": _harness("sol", "codex", paced=True),
            "opus": _harness("opus", paced=True),
            "sonnet": _harness("sonnet"),
        }
        quota.pinned = {
            "fable": STATUS_UNDER_QUOTA, "opus": STATUS_UNDER_QUOTA,
            "astra": STATUS_OUT_OF_QUOTA, "sol": STATUS_OUT_OF_QUOTA,
        }
        quota.fresh = {"fable": (0.38, 0.5), "opus": (0.38, 0.5)}
        ranking = decl.describe(
            list(entries), caller="session", self_ref="opus", entries=entries,
            reachability_cache={name: _reach() for name in entries},
        )
        assert [e.id for e in ranking.rendered_entries] == [
            "fable", "astra", "sol", "opus", "sonnet",
        ]
        assert ranking.default.id == "fable"
        text = ranking.render()
        lines = {line.split()[0]: line for line in text.splitlines() if line.startswith("  ")}
        assert "[default]" in lines["fable"]
        assert "pace 76%" in lines["fable"]
        assert "shares seven_day with opus" in lines["fable"]
        assert "[author]" in lines["opus"] and "[default]" not in lines["opus"]
        assert "out of quota until 2026-09-21 14:13 UTC" in lines["astra"]
        assert "n/a (unpaced)" in lines["sonnet"]
        assert "claude/agent" in lines["fable"]

    def test_fresh_zero_with_usable_pin_is_zero_pace_and_still_usable(self, quota, no_probe):
        entries = {"opus": _harness("opus", paced=True), "fable": _harness("fable", paced=True)}
        quota.pinned = {"opus": STATUS_AVAILABLE, "fable": STATUS_AVAILABLE}
        quota.fresh = {"opus": (0.0, 0.5), "fable": (0.2, 0.5)}
        ranking = decl.describe(
            ["opus", "fable"], caller="session", entries=entries,
            reachability_cache={"opus": _reach(), "fable": _reach()},
        )
        assert [e.id for e in ranking.rendered_entries] == ["fable", "opus"]
        opus = ranking.rendered_entries[1]
        assert opus.pace == 0.0 and opus.usable

    def test_about_to_reset_has_no_pace_and_keeps_its_place(self, quota, no_probe):
        entries = {"opus": _harness("opus", paced=True), "fable": _harness("fable", paced=True)}
        quota.pinned = {"opus": STATUS_AVAILABLE, "fable": STATUS_AVAILABLE}
        quota.fresh = {"opus": (0.5, 0.005), "fable": (0.9, 0.5)}
        ranking = decl.describe(
            ["opus", "fable"], caller="session", entries=entries,
            reachability_cache={"opus": _reach(), "fable": _reach()},
        )
        assert ranking.rendered_entries[0].id == "opus"
        assert ranking.rendered_entries[0].pace is None
        assert "about to reset" in ranking.render()

    def test_no_same_provider_warning(self, quota, no_probe):
        # R6: the single-provider constraint is retired.
        entries = {"sol": _harness("sol", "codex"), "luna": _harness("luna", "codex")}
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            ranking = decl.describe(
                ["sol", "luna"], caller="session", entries=entries,
                reachability_cache={"sol": _reach(), "luna": _reach()},
            )
        assert [e.id for e in ranking.rendered_entries] == ["sol", "luna"]


# ---------------------------------------------------------------------------
# describe() -- RENDER / SKIP / FLOOR (directions 13-17)
# ---------------------------------------------------------------------------


def _mixed_entries():
    return {
        "sol": _harness("sol", "codex"),
        "gpt-transport": _transport("gpt-transport"),
        "ruled-out": _harness("ruled-out", "codex"),
        "needs-json": _harness("needs-json", "claude"),
        "spent": _harness("spent", "codex", paced=True),
        "down": _harness("down", "opencode"),
    }


class TestFilteredRender:
    DECLARED = ["typo", "sol", "gpt-transport", "ruled-out", "needs-json", "spent", "down"]

    def _describe(self, quota, **kw):
        quota.pinned = {"spent": STATUS_OUT_OF_QUOTA}
        entries = _mixed_entries()
        caps = {
            "codex-cli": {"params": {"effort": {}}},
            "claude-cli": {"params": {}},
            "opencode-cli": {"params": {"effort": {}}},
        }
        return decl.describe(
            self.DECLARED, caller="session", entries=entries,
            exclude={"ruled-out"}, requirements={"params": ["effort"]},
            capabilities=caps,
            reachability_cache={
                "sol": _reach(), "down": _reach(STATUS_UNREACHABLE), "spent": _reach(),
            },
            **kw,
        )

    def test_render_hides_unresolved_unroutable_mismatch_and_excluded(self, quota):
        ranking = self._describe(quota)
        assert [e.id for e in ranking.rendered_entries] == ["sol", "spent", "down"]
        text = ranking.render()
        for hidden in ("typo", "gpt-transport", "ruled-out", "needs-json"):
            assert hidden not in text

    def test_unreachable_stays_visible_and_is_not_usable(self, quota):
        ranking = self._describe(quota)
        down = next(e for e in ranking.rendered_entries if e.id == "down")
        assert down.reachability == STATUS_UNREACHABLE
        assert down.usable is False
        assert "unreachable" in ranking.render()

    def test_out_of_quota_stays_visible_with_its_reset(self, quota):
        ranking = self._describe(quota)
        spent = next(e for e in ranking.rendered_entries if e.id == "spent")
        assert spent.usable is False
        assert spent.usability == STATUS_OUT_OF_QUOTA
        assert "out of quota until 2026-09-21 14:13 UTC" in ranking.render()

    def test_dispositions_cover_every_declared_entry_in_order(self, quota):
        ranking = self._describe(quota)
        assert [(d.id, d.disposition) for d in ranking.dispositions] == [
            ("typo", decl.DISPOSITION_UNRESOLVED),
            ("sol", decl.DISPOSITION_USABLE),
            ("gpt-transport", decl.DISPOSITION_UNROUTABLE),
            ("ruled-out", decl.DISPOSITION_EXCLUDED),
            ("needs-json", decl.DISPOSITION_REQUIREMENTS_MISMATCH),
            ("spent", decl.DISPOSITION_OUT_OF_QUOTA),
            ("down", decl.DISPOSITION_UNREACHABLE),
        ]

    def test_skip_is_silent(self, quota, capsys, caplog, recwarn):
        caplog.set_level(logging.DEBUG)
        self._describe(quota)
        captured = capsys.readouterr()
        assert captured.out == "" and captured.err == ""
        assert caplog.records == []
        assert len(recwarn) == 0

    def test_a_transport_is_routable_for_a_process_caller(self, quota):
        entries = {"gpt-transport": _transport("gpt-transport")}
        ranking = decl.describe(
            ["gpt-transport"], caller="process", entries=entries,
            reachability_cache={"gpt-transport": _reach()},
        )
        assert ranking.default.id == "gpt-transport"
        assert ranking.default.drive == "openrouter"


class TestFloor:
    def test_floor_itemises_every_entry_in_declaration_order(self, quota):
        quota.pinned = {"spent": STATUS_OUT_OF_QUOTA}
        entries = _mixed_entries()
        with pytest.raises(decl.NoUsableRoutingTarget) as excinfo:
            decl.describe(
                ["typo", "spent", "gpt-transport", "ruled-out", "down"],
                caller="session", entries=entries, exclude=["ruled-out"],
                reachability_cache={"down": _reach(STATUS_UNREACHABLE), "spent": _reach()},
            )
        err = excinfo.value
        assert [(d.id, d.disposition) for d in err.dispositions] == [
            ("typo", "unresolved"),
            ("spent", "out-of-quota"),
            ("gpt-transport", "unroutable"),
            ("ruled-out", "excluded"),
            ("down", "unreachable"),
        ]
        message = str(err)
        positions = [message.index(name) for name in ("typo", "spent", "gpt-transport", "ruled-out", "down")]
        assert positions == sorted(positions)
        assert "out-of-quota until 2026-09-21 14:13 UTC" in message

    def test_all_out_of_quota_reaches_the_floor(self, quota, no_probe):
        # R22: out of quota does not count toward the usable set.
        entries = {"sol": _harness("sol", "codex", paced=True), "opus": _harness("opus", paced=True)}
        quota.pinned = {"sol": STATUS_OUT_OF_QUOTA, "opus": STATUS_OUT_OF_QUOTA}
        with pytest.raises(decl.NoUsableRoutingTarget):
            decl.describe(["sol", "opus"], caller="session", entries=entries, reachability_cache={})

    def test_unknown_reachability_is_usable_fail_open(self, quota, no_probe):
        entries = {"sol": _harness("sol", "codex")}
        ranking = decl.describe(
            ["sol"], caller="process", entries=entries,
            reachability_cache={"sol": _reach(STATUS_UNKNOWN)},
        )
        assert ranking.default.id == "sol"

    def test_shadowed_core_id_is_itemised_not_raised_at_load(self, quota, no_probe):
        # R8: a core id whose MERGED entry is a transport is reserved-core
        # malformed; it contributes to the floor only.
        entries = {"opus": _transport("opus")}
        with pytest.raises(decl.NoUsableRoutingTarget) as excinfo:
            decl.describe(["opus"], caller="process", entries=entries, reachability_cache={})
        assert excinfo.value.dispositions[0].disposition == decl.DISPOSITION_SHADOWED_CORE

    def test_structural_errors_still_raise(self):
        from bootstrap_lib.model_declaration import DeclarationError

        with pytest.raises(DeclarationError):
            decl.describe([], caller="session", entries={})
        with pytest.raises(DeclarationError):
            decl.describe(["sol", "sol"], caller="session", entries={})

    def test_a_scalar_reads_as_one_entry(self, quota, no_probe):
        entries = {"sol": _harness("sol", "codex")}
        ranking = decl.describe("sol", caller="session", entries=entries,
                                reachability_cache={"sol": _reach()})
        assert [e.id for e in ranking.rendered_entries] == ["sol"]


class TestCheckRegistryEntry:
    def test_core_id_with_other_harness_is_shadowed(self):
        assert decl.check_registry_entry("opus", _harness("opus", "codex")) is not None

    def test_core_id_with_base_url_is_shadowed(self):
        assert decl.check_registry_entry("fable", _transport("fable")) is not None

    def test_core_id_on_claude_is_fine(self):
        assert decl.check_registry_entry("haiku", _harness("haiku", "claude")) is None

    def test_non_core_id_is_never_checked(self):
        assert decl.check_registry_entry("sol", _transport("sol")) is None

    def test_absent_entry_is_not_shadowed(self):
        assert decl.check_registry_entry("opus", None) is None


# ---------------------------------------------------------------------------
# describe() -- rule text, author, requirements, reachability cache
# ---------------------------------------------------------------------------


class TestRuleText:
    def _ranking(self, caller, **kw):
        entries = {"sol": _harness("sol", "codex"), "opus": _harness("opus")}
        return decl.describe(
            ["sol", "opus"], caller=caller, entries=entries,
            reachability_cache={"sol": _reach(), "opus": _reach()}, **kw,
        )

    def test_session_rule_carries_choice_announce_and_trigger(self, quota):
        rule = self._ranking("session").rule
        assert "any usable entry may be chosen" in rule
        assert 'route: <unit> -> <entry>; <reason>' in rule
        assert "non-zero exit" in rule
        assert "exited 0" in rule and "task failure" in rule
        assert "fresh worktree" in rule

    def test_process_rule_names_classified_halts_only(self, quota):
        rule = self._ranking("process").rule
        assert "first usable entry" in rule
        assert "classified halt" in rule
        assert "stays a failed attempt" in rule

    def test_rule_is_part_of_the_render(self, quota):
        ranking = self._ranking("session")
        assert ranking.rule in ranking.render()

    def test_independence_text_only_when_an_author_is_rendered(self, quota):
        assert "Independence" not in self._ranking("session").rule
        ranking = self._ranking("session", self_ref="opus")
        assert "prefer a non-author entry" in ranking.rule

    def test_author_matches_by_model_id(self, quota):
        ranking = self._ranking("session", self_ref="opus-model")
        assert [e.id for e in ranking.rendered_entries if e.is_self] == ["opus"]

    def test_caller_must_be_named(self):
        with pytest.raises(ValueError):
            decl.describe(["sol"], caller="robot", entries={})


class _Backend:
    def __init__(self, name):
        self.name = name


@dataclass
class _Selection:
    endpoint: str
    kind: str
    backend: Any
    model: str
    effort: Optional[str] = None


class TestFactoryAndCache:
    def test_backend_factory_resolves_and_keys_capabilities_by_backend_name(self, quota):
        seen = []

        def factory(name, **_kw):
            seen.append(name)
            if name == "nope":
                raise EndpointResolveError("unknown endpoint 'nope'")
            return _Selection(name, HARNESS_KIND, _Backend("codex-cli"), "m")

        caps = {"codex-cli": {"guarantees": ["filesystem.write"]}}
        ranking = decl.describe(
            ["nope", "a"], caller="process", entries={}, backend_factory=factory,
            requirements={"denies": ["filesystem.write"]}, capabilities=caps,
            reachability_cache={"a": _reach()},
        )
        assert seen == ["nope", "a"]
        assert ranking.default.id == "a"

    def test_missing_advertisement_is_a_requirements_mismatch(self, quota):
        def factory(name, **_kw):
            return _Selection(name, HARNESS_KIND, _Backend("unadvertised"), "m")

        with pytest.raises(decl.NoUsableRoutingTarget) as excinfo:
            decl.describe(
                ["a"], caller="process", entries={}, backend_factory=factory,
                requirements={"params": ["effort"]}, capabilities={},
                reachability_cache={"a": _reach()},
            )
        assert excinfo.value.dispositions[0].disposition == "requirements-mismatch"

    def test_cache_misses_are_probed_once_and_written_back(self, quota, monkeypatch):
        calls = []

        def check_many(entries, **_kw):
            calls.append(sorted(entries))
            return {name: _reach() for name in entries}

        monkeypatch.setattr(decl, "check_many", check_many)
        entries = {"sol": _harness("sol", "codex"), "luna": _harness("luna", "codex")}
        cache = {"sol": _reach()}
        decl.describe(["sol", "luna"], caller="process", entries=entries, reachability_cache=cache)
        decl.describe(["sol", "luna"], caller="process", entries=entries, reachability_cache=cache)
        assert calls == [["luna"]]
        assert cache["luna"].status == STATUS_REACHABLE

    def test_hidden_entries_are_never_probed(self, quota, monkeypatch):
        calls = []

        def check_many(entries, **_kw):
            calls.append(sorted(entries))
            return {name: _reach() for name in entries}

        monkeypatch.setattr(decl, "check_many", check_many)
        entries = {"sol": _harness("sol", "codex"), "t": _transport("t")}
        decl.describe(["typo", "t", "sol"], caller="session", entries=entries, exclude=["x"])
        assert calls == [["sol"]]

    def test_to_json_carries_rendered_entries_only(self, quota, no_probe):
        entries = {"sol": _harness("sol", "codex")}
        payload = decl.describe(
            ["typo", "sol"], caller="session", entries=entries,
            reachability_cache={"sol": _reach()},
        ).to_json()
        assert [e["id"] for e in payload["rendered_entries"]] == ["sol"]
        assert "dispositions" not in payload and "names" not in payload
        assert payload["default"] == "sol"
        assert "rule" in payload


# ---------------------------------------------------------------------------
# run() -- unattended dispatch, halts, attempt limit, workspace rule
# ---------------------------------------------------------------------------


class _Halt(Exception):
    def __init__(self, kind, resets_at=None):
        super().__init__(f"halt {kind}")
        self.kind = kind
        self.resets_at = resets_at


class _ScriptedBackend:
    def __init__(self, name, outcomes, on_call=None):
        self.name = name
        self.outcomes = list(outcomes)
        self.calls = 0
        self.on_call = on_call

    def complete(self, system, user, *, model, options=None):
        self.calls += 1
        if self.on_call:
            self.on_call()
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def classify_halt(self, exc):
        return getattr(exc, "kind", None)


def _factory_for(backends):
    def factory(name, **_kw):
        if name not in backends:
            raise EndpointResolveError(f"unknown endpoint '{name}'")
        return _Selection(name, HARNESS_KIND, backends[name], f"{name}-model")

    return factory


class TestRun:
    def _entries(self):
        return {
            "sol": _harness("sol", "codex", paced=True),
            "opus": _harness("opus", paced=False),
        }

    def test_first_usable_entry_runs(self, quota):
        backends = {"sol": _ScriptedBackend("codex-cli", ["ok"]), "opus": _ScriptedBackend("claude-cli", [])}
        result = decl.run(
            ["sol", "opus"], decl.RunRequest(system="s", prompt="p"),
            entries=self._entries(), backend_factory=_factory_for(backends),
            reachability_cache={"sol": _reach(), "opus": _reach()},
        )
        assert result.status == decl.RUN_COMPLETED
        assert result.entry == "sol" and result.response == "ok"
        assert backends["opus"].calls == 0

    def test_quota_halt_records_verdict_excludes_and_moves_on(self, quota, monkeypatch):
        recorded = []
        monkeypatch.setattr(
            decl, "record_observed_halt",
            lambda entry_id, spec, **kw: recorded.append((entry_id, spec, kw.get("resets_at"))),
        )
        backends = {
            "sol": _ScriptedBackend("codex-cli", [_Halt(HALT_QUOTA, resets_at=RESET)]),
            "opus": _ScriptedBackend("claude-cli", ["ok"]),
        }
        attempts = []
        result = decl.run(
            ["sol", "opus"], decl.RunRequest(system="s", prompt="p"),
            entries=self._entries(), backend_factory=_factory_for(backends),
            reachability_cache={"sol": _reach(), "opus": _reach()},
            max_attempts=2, on_attempt=attempts.append,
        )
        assert result.status == decl.RUN_COMPLETED and result.entry == "opus"
        assert recorded == [("sol", SEVEN_DAY, RESET)]
        assert [(a.entry, a.halt) for a in attempts] == [("sol", HALT_QUOTA), ("opus", None)]
        assert all(hasattr(a, "pace") for a in attempts)

    def test_auth_halt_moves_on_without_a_verdict(self, quota, monkeypatch):
        recorded = []
        monkeypatch.setattr(decl, "record_observed_halt", lambda *a, **k: recorded.append(a))
        backends = {
            "sol": _ScriptedBackend("codex-cli", [_Halt(HALT_AUTH)]),
            "opus": _ScriptedBackend("claude-cli", ["ok"]),
        }
        result = decl.run(
            ["sol", "opus"], decl.RunRequest(system="s", prompt="p"),
            entries=self._entries(), backend_factory=_factory_for(backends),
            reachability_cache={"sol": _reach(), "opus": _reach()}, max_attempts=3,
        )
        assert result.entry == "opus"
        assert recorded == []

    def test_quota_halt_on_the_last_attempt_is_attempt_limit_not_floor(self, quota, monkeypatch):
        # R24: max_attempts bounds executions; it is never reported as the floor.
        monkeypatch.setattr(decl, "record_observed_halt", lambda *a, **k: None)
        backends = {
            "sol": _ScriptedBackend("codex-cli", [_Halt(HALT_QUOTA)]),
            "opus": _ScriptedBackend("claude-cli", ["ok"]),
        }
        result = decl.run(
            ["sol", "opus"], decl.RunRequest(system="s", prompt="p"),
            entries=self._entries(), backend_factory=_factory_for(backends),
            reachability_cache={"sol": _reach(), "opus": _reach()}, max_attempts=1,
        )
        assert result.status == decl.RUN_ATTEMPT_LIMIT
        assert backends["opus"].calls == 0

    def test_every_entry_halting_propagates_the_floor(self, quota, monkeypatch):
        # R27: halt -> exclude -> describe() again -> floor when nothing is left.
        monkeypatch.setattr(decl, "record_observed_halt", lambda *a, **k: None)
        backends = {
            "sol": _ScriptedBackend("codex-cli", [_Halt(HALT_QUOTA)]),
            "opus": _ScriptedBackend("claude-cli", [_Halt(HALT_QUOTA)]),
        }
        with pytest.raises(decl.NoUsableRoutingTarget) as excinfo:
            decl.run(
                ["sol", "opus"], decl.RunRequest(system="s", prompt="p"),
                entries=self._entries(), backend_factory=_factory_for(backends),
                reachability_cache={"sol": _reach(), "opus": _reach()}, max_attempts=5,
            )
        assert [d.disposition for d in excinfo.value.dispositions] == ["excluded", "excluded"]

    def test_a_task_error_stays_a_failed_attempt(self, quota):
        backends = {
            "sol": _ScriptedBackend("codex-cli", [RuntimeError("bad output")]),
            "opus": _ScriptedBackend("claude-cli", ["ok"]),
        }
        result = decl.run(
            ["sol", "opus"], decl.RunRequest(system="s", prompt="p"),
            entries=self._entries(), backend_factory=_factory_for(backends),
            reachability_cache={"sol": _reach(), "opus": _reach()}, max_attempts=3,
        )
        assert result.status == decl.RUN_FAILED
        assert backends["opus"].calls == 0

    def test_a_launch_failure_marks_unreachable_and_moves_on(self, quota):
        cache = {"sol": _reach(), "opus": _reach()}
        backends = {
            "sol": _ScriptedBackend("codex-cli", [FileNotFoundError("codex")]),
            "opus": _ScriptedBackend("claude-cli", ["ok"]),
        }
        result = decl.run(
            ["sol", "opus"], decl.RunRequest(system="s", prompt="p"),
            entries=self._entries(), backend_factory=_factory_for(backends),
            reachability_cache=cache, max_attempts=3,
        )
        assert result.entry == "opus"
        assert cache["sol"].status == STATUS_UNREACHABLE

    def test_run_propagates_a_floor_before_any_attempt(self, quota):
        with pytest.raises(decl.NoUsableRoutingTarget):
            decl.run(["typo"], decl.RunRequest(system="s", prompt="p"), entries={},
                     reachability_cache={})


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "kept.txt").write_text("original\n")
    _git(root, "add", "kept.txt")
    _git(root, "commit", "-q", "-m", "init")
    (root / "launch-dirty.txt").write_text("user work\n")
    return root


class TestWorkspaceRule:
    def test_a_halted_unit_is_reset_before_reselection(self, quota, repo, monkeypatch):
        # R30: a re-run never lands on another model's partial edits.
        monkeypatch.setattr(decl, "record_observed_halt", lambda *a, **k: None)
        seen_by_second = {}

        def scribble():
            (repo / "kept.txt").write_text("partial edit\n")
            (repo / "new-file.txt").write_text("partial\n")

        def inspect():
            seen_by_second["kept"] = (repo / "kept.txt").read_text()
            seen_by_second["new"] = (repo / "new-file.txt").exists()
            seen_by_second["dirty"] = (repo / "launch-dirty.txt").read_text()

        backends = {
            "sol": _ScriptedBackend("codex-cli", [_Halt(HALT_QUOTA)], on_call=scribble),
            "opus": _ScriptedBackend("claude-cli", ["ok"], on_call=inspect),
        }
        attempts = []
        result = decl.run(
            ["sol", "opus"], decl.RunRequest(system="s", prompt="p", workspace=repo),
            entries=TestRun()._entries(), backend_factory=_factory_for(backends),
            reachability_cache={"sol": _reach(), "opus": _reach()},
            max_attempts=2, on_attempt=attempts.append,
        )
        assert result.status == decl.RUN_COMPLETED
        assert seen_by_second == {"kept": "original\n", "new": False, "dirty": "user work\n"}
        assert "reset" in attempts[0].workspace_action

    def test_a_workspace_that_cannot_be_reset_stops_reselection(self, quota, tmp_path, monkeypatch):
        monkeypatch.setattr(decl, "record_observed_halt", lambda *a, **k: None)
        backends = {
            "sol": _ScriptedBackend("codex-cli", [_Halt(HALT_QUOTA)]),
            "opus": _ScriptedBackend("claude-cli", ["ok"]),
        }
        not_git = tmp_path / "plain"
        not_git.mkdir()
        result = decl.run(
            ["sol", "opus"], decl.RunRequest(system="s", prompt="p", workspace=not_git),
            entries=TestRun()._entries(), backend_factory=_factory_for(backends),
            reachability_cache={"sol": _reach(), "opus": _reach()}, max_attempts=2,
        )
        assert result.status == decl.RUN_FAILED
        assert backends["opus"].calls == 0
        assert "workspace" in result.detail


def test_bootstrap_lib_absent_and_too_old_messages_differ(monkeypatch):
    import builtins
    import sys

    real_import = builtins.__import__

    def absent(name, *args, **kwargs):
        if name == "bootstrap_lib" or name.startswith("bootstrap_lib."):
            raise ImportError("no bootstrap_lib")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", absent)
    with pytest.raises(decl.DeclarationSupportError) as absent_exc:
        decl._model_declaration()
    monkeypatch.setattr(builtins, "__import__", real_import)

    import types

    stub = types.ModuleType("bootstrap_lib.model_declaration")
    monkeypatch.setitem(sys.modules, "bootstrap_lib.model_declaration", stub)
    import bootstrap_lib

    monkeypatch.setattr(bootstrap_lib, "model_declaration", stub, raising=False)
    with pytest.raises(decl.DeclarationSupportError) as old_exc:
        decl._model_declaration()
    assert str(absent_exc.value) != str(old_exc.value)
    assert "not linked" in str(absent_exc.value)
    assert "0.129.0" in str(old_exc.value)


class TestHiddenIdsNeverLeaveTheFloor:
    """Directions 13, 16, 17: only the floor may name a hidden id."""

    def _ranking(self):
        entries = {"sol": _harness("sol", "codex"), "ruled-out": _harness("ruled-out", "codex")}
        return decl.describe(
            ["typo-id", "ruled-out", "sol"], caller="session", entries=entries,
            exclude=["ruled-out"], reachability_cache={"sol": _reach()},
        )

    def test_to_json_names_no_hidden_id(self, quota):
        import json

        text = json.dumps(self._ranking().to_json())
        assert "typo-id" not in text and "ruled-out" not in text
        assert "sol" in text

    def test_repr_names_no_hidden_id(self, quota):
        text = repr(self._ranking())
        assert "typo-id" not in text and "ruled-out" not in text

    def test_the_floor_still_itemises_every_hidden_id(self, quota):
        entries = {"ruled-out": _harness("ruled-out", "codex")}
        with pytest.raises(decl.NoUsableRoutingTarget) as excinfo:
            decl.describe(["typo-id", "ruled-out"], caller="session", entries=entries,
                          exclude=["ruled-out"], reachability_cache={})
        payload = excinfo.value.to_json()
        assert [d["id"] for d in payload["dispositions"]] == ["typo-id", "ruled-out"]


# ---------------------------------------------------------------------------
# A session caller that CAN dispatch transports says so (carried fix from the
# step 5 review): code-review lanes reach a transport entry through the lane
# runner, so describe must not hide one from them. Default is unchanged.
# ---------------------------------------------------------------------------


class TestSessionDispatchableTransport:
    def _entries(self):
        return {"sol": _harness("sol", "codex"), "gpt-transport": _transport("gpt-transport")}

    def _cache(self):
        return {"sol": _reach(), "gpt-transport": _reach()}

    def test_default_session_caller_still_hides_a_transport(self, quota, no_probe):
        ranking = decl.describe(
            ["gpt-transport", "sol"], caller="session", entries=self._entries(),
            reachability_cache=self._cache(),
        )
        assert [e.id for e in ranking.rendered_entries] == ["sol"]
        assert ranking.dispositions[0].disposition == decl.DISPOSITION_UNROUTABLE

    def test_dispatchable_transport_keeps_it_in_the_session_menu(self, quota, no_probe):
        ranking = decl.describe(
            ["gpt-transport", "sol"], caller="session", entries=self._entries(),
            reachability_cache=self._cache(), dispatchable=("transport",),
        )
        assert [e.id for e in ranking.rendered_entries] == ["gpt-transport", "sol"]
        assert ranking.default.id == "gpt-transport"
        assert ranking.default.drive == "openrouter"
        assert "transport/openrouter" in ranking.render()
        # harness entries keep their in-session drive
        assert ranking.rendered_entries[1].drive == "codex exec"

    def test_a_transport_only_declaration_is_usable_when_dispatchable(self, quota, no_probe):
        entries = {"gpt-transport": _transport("gpt-transport")}
        ranking = decl.describe(
            ["gpt-transport"], caller="session", entries=entries,
            reachability_cache={"gpt-transport": _reach()}, dispatchable=["transport"],
        )
        assert ranking.default.id == "gpt-transport"

    def test_an_unknown_dispatchable_kind_is_refused(self, quota):
        with pytest.raises(ValueError, match="dispatchable"):
            decl.describe(["sol"], caller="session", entries=self._entries(), dispatchable=("harness",))
