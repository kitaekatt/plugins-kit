"""The optional consent layer: scope intersection, the missing-file default, and the opt-in surfaces.

Principle under test: consent governs caller-selected execution policy. A cost class is
information that is always reported; the layer is a policy a caller may apply, never
one the library applies on its own. The opt-in surfaces are protocol 2 request field
``consent``, ``llm-kit models describe --consent [--consent-scope X]`` and the API call
``consent.policy(scope=...)``.

What each group pins, and what reverting breaks:

* scope intersection -- red when ``effective`` stops ANDing the scope over the global
  policy (a scope would then widen the ceiling, or ignore a restriction);
* missing-file default -- red when ``load_config`` creates a file, or when the default
  stops being local+subscription on, metered off;
* protocol 2 opt-in -- red when ``dispatch/invoke.py`` stops enforcing the policy for a
  request that sends ``consent`` (the fake transports are then called).
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

import llm_kit.cli as cli
from llm_kit.adapters import harness_auth
from llm_kit.completion import invoke_protocol
from llm_kit.dispatch import declaration, invoke as invoke_mod
from llm_kit.registry import consent as consent_mod
from llm_kit.registry import cost
from llm_kit.registry.consent import (
    ConsentConfig,
    ConsentConfigError,
    effective,
    policy,
    save_config,
)
from test_cli_harness import home, run, write  # noqa: F401 -- home is a fixture
from test_invoke_protocol import allow, call, world  # noqa: F401 -- world is a fixture

ALL_ON = {"local": True, "subscription": True, "metered": True}


def _config(globals_: dict, scopes: dict | None = None) -> ConsentConfig:
    return ConsentConfig(globals_=globals_, scopes=scopes or {}, source="file", path=Path("/consent.json"))


def _allowed(result: "consent_mod.Policy") -> dict:
    return {name: state.allowed for name, state in result.classes.items()}


# --- scope intersection: effective = global AND scope.get(class, true) ------------------------------


@pytest.mark.parametrize("global_value", [True, False])
@pytest.mark.parametrize("scope_value", [True, False, None])
def test_effective_is_the_global_value_and_the_scope_value_with_absent_meaning_inherit(global_value, scope_value):
    stated = {} if scope_value is None else {"metered": scope_value}
    config = _config({"metered": global_value}, {"s": stated})
    result = effective(config, "s")
    expected = global_value and (True if scope_value is None else scope_value)
    assert result.classes["metered"].allowed is expected
    assert result.classes["metered"].global_allowed is global_value
    assert result.classes["metered"].scope_allowed is scope_value


def test_a_scope_restricts_and_never_widens_the_global_ceiling():
    config = _config(
        {"local": True, "subscription": False, "metered": False},
        {"s": {"local": False, "subscription": True, "metered": True}},
    )
    result = effective(config, "s")
    assert _allowed(result) == {"local": False, "subscription": False, "metered": False}
    # the global policy is untouched by the scope
    assert _allowed(effective(config)) == {"local": True, "subscription": False, "metered": False}


def test_an_unknown_scope_inherits_the_global_policy_whole():
    config = _config({"metered": True}, {"other": {"metered": False}})
    assert _allowed(effective(config, "nobody")) == _allowed(effective(config)) == {
        "local": True, "subscription": True, "metered": True,
    }


def test_a_scope_on_that_global_blocks_is_shown_blocked_with_the_global_command():
    state = effective(_config({"metered": False}, {"s": {"metered": True}}), "s").classes["metered"]
    assert state.allowed is False and state.blocked_by_global is True
    assert state.provenance == "global"
    assert state.commands == ("llm-kit consent set --metered on",)  # the GLOBAL command, not a scoped one


def test_a_scope_off_names_the_scoped_command_and_both_when_global_is_also_off():
    only_scope = effective(_config({"metered": True}, {"s": {"metered": False}}), "s").classes["metered"]
    assert only_scope.provenance == "scope" and only_scope.blocked_by_global is False
    assert only_scope.commands == ("llm-kit consent set --scope s --metered on",)
    both = effective(_config({"metered": False}, {"s": {"metered": False}}), "s").classes["metered"]
    assert both.commands == ("llm-kit consent set --metered on", "llm-kit consent set --scope s --metered on")


def test_provenance_names_the_layer_that_decided_the_value():
    config = _config({"subscription": True}, {"s": {"metered": True, "local": False}})
    by = {name: state.provenance for name, state in effective(config, "s").classes.items()}
    # local: the scope turned it off; subscription: stated by the global file; metered: global default off blocks the scope
    assert by == {"local": "scope", "subscription": "global", "metered": "default"}


def test_scope_names_are_validated():
    for bad in ("", "  ", " x", "x ", 3):
        with pytest.raises(ValueError, match="scope"):
            effective(_config({}), bad)
        with pytest.raises(ValueError, match="scope"):
            policy(bad)
    assert effective(_config({}), None).scope is None


def test_the_policy_api_applies_a_scope_from_the_file(home):  # noqa: F811
    save_config(globals_=ALL_ON, scopes={"task-kit": {"metered": False}})
    assert policy().allows("metered") is True
    assert policy("task-kit").allows("metered") is False
    assert policy("task-kit").allows("subscription") is True
    assert policy("task-kit").allowed == frozenset({"local", "subscription"})


def test_an_unknown_class_is_a_value_error():
    with pytest.raises(ValueError, match="free"):
        effective(_config({})).allows("free")


# --- the missing file: the default policy, provenance default, nothing created -----------------------


def test_with_no_file_the_policy_is_local_and_subscription_on_metered_off_and_nothing_is_created(home):  # noqa: F811
    result = policy()
    assert _allowed(result) == {"local": True, "subscription": True, "metered": False}
    assert {state.provenance for state in result.classes.values()} == {"default"}
    assert result.source == "default"
    assert not (home / ".config").exists() and not (home / ".claude").exists()
    scoped = policy("anything")  # a scope with no file inherits the default
    assert _allowed(scoped) == _allowed(result)
    assert not (home / ".config").exists()


def test_a_missing_file_is_not_an_error_but_a_present_bad_one_is(home, tmp_path):  # noqa: F811
    assert policy().source == "default"
    bad = write(tmp_path / "bad.json", "{broken")
    with pytest.raises(ConsentConfigError):
        policy(path=bad)


# --- protocol 2: the consent field -----------------------------------------------------------------------


def test_a_request_that_opts_in_is_enforced_before_any_runner_call(world, capsys):
    """Red on revert: remove the opt-in enforcement in dispatch/invoke.py and the fake transport is called."""
    code, doc, err = call(world, capsys, selection="art", consent=True)  # metered: off in the default policy
    assert code == 4 and doc is None
    assert err["error"]["kind"] == "consent"
    message = err["error"]["message"]
    assert "metered" in message and "the global policy" in message
    assert "`llm-kit consent set --metered on`" in message and "nothing was dispatched" in message
    assert world.runner_calls == 0


def test_the_default_policy_allows_subscription_for_a_request_that_opts_in(world, capsys):
    code, doc, err = call(world, capsys, selection="chat", consent=True)  # no consent file at all
    assert code == 0, (doc, err)
    assert doc["cost"]["class"] == "subscription"
    assert doc["consent"] == {"opted_in": True, "applied": True, "scope": None}
    assert world.runner_calls == 1


def test_a_scoped_request_is_held_to_the_scope_over_the_global_policy(world, capsys):
    save_config(globals_=ALL_ON, scopes={"databench": {"metered": False}})
    code, doc, err = call(world, capsys, selection="art", consent={"scope": "databench"})
    assert code == 4 and err["error"]["kind"] == "consent"
    message = err["error"]["message"]
    assert "scope 'databench'" in message and "`llm-kit consent set --scope databench --metered on`" in message
    assert world.runner_calls == 0
    # the same request under another scope, and under the global policy, is allowed
    assert call(world, capsys, selection="art", consent={"scope": "orchestrate"})[0] == 0
    assert call(world, capsys, selection="art", consent=True)[0] == 0


def test_a_scope_on_that_the_global_policy_blocks_is_refused_naming_the_global_command(world, capsys):
    save_config(globals_={"local": True, "subscription": True, "metered": False}, scopes={"databench": {"metered": True}})
    code, _doc, err = call(world, capsys, selection="art", consent={"scope": "databench"})
    assert code == 4 and err["error"]["kind"] == "consent"
    assert "`llm-kit consent set --metered on`" in err["error"]["message"]
    assert "--scope" not in err["error"]["message"].split("run ")[1]  # the scope never widens global
    assert world.runner_calls == 0


def test_the_response_reports_the_scope_that_was_applied(world, capsys):
    allow()
    code, doc, _err = call(world, capsys, selection="chat", consent={"scope": "task-kit"})
    assert code == 0
    assert doc["consent"] == {"opted_in": True, "applied": True, "scope": "task-kit"}


def test_a_misconfigured_consent_file_is_a_configuration_refusal_when_opted_in(world, capsys, home):  # noqa: F811
    write(home / ".config" / "llm-kit" / "consent.json", "{broken")
    code, doc, err = call(world, capsys, selection="chat", consent=True)
    assert code == 4 and doc is None
    assert err["error"]["kind"] == "configuration" and "not valid JSON" in err["error"]["message"]
    assert world.runner_calls == 0


@pytest.mark.parametrize(
    "bad",
    ["yes", "true", 1, 0, 1.5, "", [], [True], {}, {"scope": ""}, {"scope": "  "}, {"scope": " x"}, {"scope": 3},
     {"scope": None}, {"scope": "a", "extra": 1}, {"scopes": "a"}],
)
def test_a_malformed_consent_field_is_a_protocol_refusal(world, capsys, bad):
    code, doc, err = call(world, capsys, selection="chat", consent=bad)
    assert code == 4 and doc is None
    assert err["error"]["kind"] == "protocol" and "consent" in err["error"]["message"]
    assert world.runner_calls == 0


def test_consent_together_with_an_allow_list_is_a_protocol_refusal(world, capsys):
    """Protocol 2 has no per-call allow list, so `allow` is an unknown key and refused as protocol."""
    code, _doc, err = call(world, capsys, selection="chat", consent=True, allow=["local"])
    assert code == 4 and err["error"]["kind"] == "protocol" and "allow" in err["error"]["message"]
    assert world.runner_calls == 0


def test_a_missing_file_never_blocks_a_request_that_opts_in_but_a_present_bad_one_does(world, capsys, home):  # noqa: F811
    assert call(world, capsys, selection="chat", consent=True)[0] == 0
    write(home / ".config" / "llm-kit" / "consent.json", json.dumps({"schema": 1, "usage": True, "spend": True}))
    code, _doc, err = call(world, capsys, selection="chat", consent=True)
    assert code == 4 and err["error"]["kind"] == "configuration" and "usage/spend" in err["error"]["message"]


@pytest.mark.parametrize("omitted", ["absent", False, None])
def test_a_request_without_the_field_does_not_opt_in(world, capsys, omitted):
    request: dict = {} if omitted == "absent" else {"consent": omitted}
    code, doc, err = call(world, capsys, selection="chat", **request)
    assert code == 0, (doc, err)  # no consent file, subscription class: not refused
    assert doc["consent"] == {"opted_in": False, "applied": False, "scope": None}


def test_a_request_without_consent_never_consults_the_layer(world, capsys, home):  # noqa: F811
    """A request without `consent` ignores the layer, even a broken file."""
    write(home / ".config" / "llm-kit" / "consent.json", "{broken")
    code, doc, err = call(world, capsys, selection="art")  # metered, broken file, no opt-in
    assert code == 0, (doc, err)
    assert doc["consent"] == {"opted_in": False, "applied": False, "scope": None}
    assert world.runner_calls == 1
    # an opt-in is still enforced, and the broken file is then loud
    code, doc, err = call(world, capsys, selection="art", consent=True)
    assert code == 4 and doc is None and err["error"]["kind"] == "configuration"


def test_classification_is_reported_with_its_reason_whether_or_not_the_layer_is_used(world, capsys):
    _code, plain, _ = call(world, capsys, selection="art")
    assert plain["cost"]["class"] == "metered" and plain["cost"]["reason"]
    allow()
    _code, opted, _ = call(world, capsys, selection="art", consent=True)
    assert opted["cost"]["class"] == "metered" and opted["cost"]["reason"] == plain["cost"]["reason"]


def test_a_codex_or_claude_model_on_subscription_is_not_blocked_by_the_default_policy(world, capsys):
    for selection in ("chat", "draft.generate.primary"):
        code, doc, err = call(world, capsys, selection=selection, consent=True)
        assert code == 0, (selection, doc, err)
        assert doc["cost"]["class"] == "subscription"


def test_a_harness_on_metered_auth_is_refused_by_the_default_policy(world, capsys, monkeypatch):
    monkeypatch.setattr(
        harness_auth, "probe_harness_auth",
        lambda harnesses, env=None, *, cwd: {h.lower(): cost.HarnessAuth(cost.METERED, "ANTHROPIC_API_KEY is set") for h in harnesses},
    )
    code, _doc, err = call(world, capsys, selection="chat", consent=True)
    assert code == 4 and err["error"]["kind"] == "consent" and "metered" in err["error"]["message"]
    assert world.runner_calls == 0


def test_the_request_schema_describes_the_consent_field():
    keys = invoke_protocol.describe_invoke_schema()
    assert "consent" in keys["keys"] and "scope" in keys["keys"]["consent"]
    assert "reason" in keys["result"]["cost"] and "consent" in keys["result"]
    assert invoke_protocol.parse_consent_field({}) is None
    assert invoke_protocol.parse_consent_field({"consent": True}) == invoke_protocol.ConsentRequest(None)
    assert invoke_protocol.parse_consent_field({"consent": {"scope": "x"}}).scope == "x"
    assert invoke_protocol.parse_consent_field({"consent": False}) is None


# --- the CLI opt-in: models describe --consent / --allow ------------------------------------------------


_REGISTRY = """\
default: lan
models:
  lan: {base_url: http://lan.invalid/v1, model: m-lan, billing: {mode: unmetered, owner: self}}
  paid: {base_url: http://paid.invalid/v1, model: m-paid}
  cl: {harness: claude, model: opus, tier: 2, family: alpha}
"""


@pytest.fixture()
def described(home, tmp_path, monkeypatch, capsys):  # noqa: F811
    """`llm-kit models describe` against a pinned registry: reachable everywhere, harness auth fixed."""
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(write(tmp_path / "registry.yaml", _REGISTRY)))
    reach = importlib.import_module("llm_kit.dispatch.reachability")
    monkeypatch.setattr(
        declaration, "check_many",
        lambda entries, **_: {n: reach.Reachability("reachable", "models-endpoint", "ok") for n in entries},
    )
    monkeypatch.setattr(
        harness_auth, "probe_harness_auth",
        lambda harnesses, env=None, *, cwd: {h.lower(): cost.HarnessAuth(cost.SUBSCRIPTION, "fixture") for h in harnesses},
    )

    def go(*argv: str):
        out = run(cli.main, ["models", "describe", "lan", "paid", "cl", "--caller", "process", "--json", *argv], capsys)
        return out.code, (json.loads(out.out) if out.out.strip() else None), out.err

    return go


def test_describe_reports_every_class_and_reason_and_the_optional_note_without_consent(described):
    code, doc, _err = described()
    assert code == 0
    classes = {e["id"]: e["cost"]["class"] for e in doc["rendered_entries"]}
    assert classes == {"lan": "local", "paid": "metered", "cl": "subscription"}
    assert all(e["cost"]["reason"] for e in doc["rendered_entries"])
    assert "switched_off" not in doc  # nothing is filtered unless the caller opts in or passes --allow
    assert doc["consent"]["optional"] is True and doc["consent"]["in_use"] is False
    assert "--consent" in doc["consent"]["opt_in"]["describe_flags"]


def test_describe_text_prints_one_line_saying_the_layer_is_optional(home, tmp_path, monkeypatch, capsys, described):  # noqa: F811
    out = run(cli.main, ["models", "describe", "lan", "paid", "--caller", "process"], capsys)
    lines = [line for line in out.out.splitlines() if line.startswith("Consent layer:")]
    assert out.code == 0 and len(lines) == 1 and "optional" in lines[0] and "--consent" in lines[0]


def test_describe_consent_switches_off_what_the_default_policy_does_not_allow(described, home):  # noqa: F811
    code, doc, _err = described("--consent")
    assert code == 0
    assert [e["id"] for e in doc["rendered_entries"]] == ["lan", "cl"]
    off = doc["switched_off"]
    assert [d["id"] for d in off] == ["paid"] and off[0]["cost_class"] == "metered"
    assert "`llm-kit consent set --metered on`" in off[0]["detail"]  # names the class and the exact command
    assert doc["consent"]["in_use"] is True and doc["consent"]["scope"] is None
    assert doc["consent"]["policy"]["classes"]["metered"]["allowed"] is False
    assert not (home / ".config").exists()  # opting in read the default and created nothing


def test_describe_consent_scope_applies_the_scope_over_the_global_policy(described):
    save_config(globals_=ALL_ON, scopes={"orchestrate": {"subscription": False}})
    code, doc, _err = described("--consent", "--consent-scope", "orchestrate")
    assert code == 0 and [e["id"] for e in doc["rendered_entries"]] == ["lan", "paid"]
    assert [d["id"] for d in doc["switched_off"]] == ["cl"]
    assert "`llm-kit consent set --scope orchestrate --subscription on`" in doc["switched_off"][0]["detail"]
    assert doc["consent"]["scope"] == "orchestrate"
    code, doc, _err = described("--consent")  # the global policy has everything on
    assert [e["id"] for e in doc["rendered_entries"]] == ["lan", "paid", "cl"] and "switched_off" not in doc


def test_describe_allow_is_the_callers_own_filter_and_never_reads_the_layer(described, home):  # noqa: F811
    write(home / ".config" / "llm-kit" / "consent.json", "{broken")  # a broken layer is invisible to --allow
    code, doc, _err = described("--allow", "local,subscription")
    assert code == 0 and [e["id"] for e in doc["rendered_entries"]] == ["lan", "cl"]
    assert [d["id"] for d in doc["switched_off"]] == ["paid"]
    assert "consent set" not in doc["switched_off"][0]["detail"]  # no layer, so no layer command
    assert doc["consent"]["in_use"] is False


def test_describe_rejects_the_retired_class_names_in_allow(described):
    code, _doc, err = described("--allow", "free,usage")
    assert code == 2 and "free" in err


def test_describe_allow_and_consent_together_is_an_error(described):
    code, doc, err = described("--allow", "local", "--consent")
    assert code == 2 and doc is None and "alternatives" in err


def test_describe_consent_scope_needs_consent(described):
    code, doc, err = described("--consent-scope", "x")
    assert code == 2 and doc is None and "--consent" in err


def test_describe_consent_with_a_broken_file_is_a_configuration_error_and_without_it_is_fine(described, home):  # noqa: F811
    write(home / ".config" / "llm-kit" / "consent.json", "{broken")
    assert described()[0] == 0  # not opted in: the file is never read
    code, doc, err = described("--consent")
    assert code == 2 and doc is None and json.loads(err)["error"]["kind"] == "configuration"


def test_the_discovery_types_extend_rather_than_change_the_plain_ones():
    """A plain describe keeps the plain types, so its output (and its dataclass shape) is unchanged."""
    assert issubclass(declaration.DiscoveryRanking, declaration.Ranking)
    assert issubclass(declaration.CostedEntryState, declaration.EntryState)
    assert "consent" not in declaration.Ranking.__dataclass_fields__
    assert "cost_class" not in declaration.EntryState.__dataclass_fields__


def test_catalog_reports_classification_without_applying_the_layer(home, tmp_path, capsys):  # noqa: F811
    from llm_kit.registry import project_models as pm

    root = tmp_path / "proj"
    root.mkdir()
    pm.write_project_models(
        {
            "version": 1,
            "catalog": [{"id": "d", "label": "D", "endpoint": "openrouter", "model": "deepseek/deepseek-v4.1-flash"}],
            "selections": {"k": {"model": "d", "effort": None}},
        },
        project_root=str(root),
    )
    write(home / ".config" / "llm-kit" / "consent.json", "{broken")  # never consulted by a catalogue
    out = run(cli.main, ["models", "catalog", "--project-root", str(root)], capsys)
    assert out.code == 0
    doc = json.loads(out.out)
    assert set(doc["models"][0]["cost"]) == {"class", "reason"} and doc["models"][0]["cost"]["class"] == "metered"
    assert doc["consent"]["optional"] is True and doc["consent"]["in_use"] is False
