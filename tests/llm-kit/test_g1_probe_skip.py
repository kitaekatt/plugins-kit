"""G1: a protocol 2 request that does not opt in to consent checking spends no harness auth probe.

The probe asks the claude or codex CLI how it is authenticated, which costs seconds. Only
the consent layer needs the answer (to hold the call to a policy), so a request without the
``consent`` field skips it and reports the cost class as unprobed, as `models catalog`
does without `--probe-auth`: ``class`` is null and the reason says the auth was not probed.
"""

from __future__ import annotations

import pytest

from llm_kit.adapters import harness_auth
from llm_kit.registry import cost
from test_cli_harness import home  # noqa: F401 -- the fixture `world` depends on
from test_invoke_protocol import allow, call, world  # noqa: F401 -- world is a fixture


@pytest.fixture()
def probes(world):  # noqa: F811
    """Record every harness auth probe; the world's fake answers subscription."""
    seen: list = []

    def fake(harnesses, env=None, *, cwd):
        seen.append(sorted(h.lower() for h in harnesses))
        return {h.lower(): cost.HarnessAuth(cost.SUBSCRIPTION, "fake subscription") for h in harnesses}

    world.monkeypatch.setattr(harness_auth, "probe_harness_auth", fake)
    return seen


def _refuse_any_probe(world) -> None:
    def boom(*_a, **_k):
        raise AssertionError("the harness auth probe ran for a request that did not opt in to consent")

    world.monkeypatch.setattr(harness_auth, "probe_harness_auth", boom)


@pytest.mark.parametrize("selection,harness", [("draft.generate.primary", "codex"), ("chat", "claude")])
def test_a_request_without_consent_never_probes_and_reports_the_class_unprobed(world, capsys, selection, harness):
    """Red on revert: probe unconditionally in dispatch/invoke.py and the AssertionError fails the call."""
    allow()
    _refuse_any_probe(world)
    code, doc, err = call(world, capsys, selection=selection)
    assert code == 0, (doc, err)
    assert doc["cost"]["class"] is None
    assert doc["cost"]["reason"] == f"{harness} cost class depends on its authentication, which was not probed"
    assert doc["consent"] == {"opted_in": False, "applied": False, "scope": None}


def test_an_explicit_consent_false_is_not_an_opt_in_and_does_not_probe(world, capsys):
    _refuse_any_probe(world)
    code, doc, err = call(world, capsys, selection="chat", consent=False)
    assert code == 0, (doc, err)
    assert doc["cost"]["class"] is None


def test_a_request_without_consent_to_a_transport_entry_still_classifies_without_a_probe(world, capsys):
    """Only claude and codex harness classes depend on the probe; a transport classifies from the registry alone."""
    _refuse_any_probe(world)
    code, doc, err = call(world, capsys, selection="art")
    assert code == 0, (doc, err)
    assert doc["cost"]["class"] == "metered" and doc["cost"]["reason"] == "no registry entry: billing undeclared"


@pytest.mark.parametrize("consent", [True, {"scope": "databench"}])
def test_an_opted_in_request_still_probes_once_and_reports_the_class(world, capsys, probes, consent):
    allow()
    code, doc, err = call(world, capsys, selection="draft.generate.primary", consent=consent)
    assert code == 0, (doc, err)
    assert probes == [["codex"]]
    assert doc["cost"]["class"] == "subscription"
    assert doc["consent"]["opted_in"] is True


def test_an_opted_in_request_is_still_refused_on_a_class_the_policy_denies(world, capsys, probes):
    allow(subscription=False)
    code, doc, err = call(world, capsys, selection="chat", consent=True)
    assert code == 4 and doc is None and err["error"]["kind"] == "consent"
    assert probes == [["claude"]]
    assert world.runner_calls == 0
