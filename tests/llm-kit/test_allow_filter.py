"""Cost-class ``allow`` filter on ``describe`` / ``run`` (S1 U3; checks C14-C18 of
docs/planning/llm-kit/cost-class-consent-plan.md).

Entries are built directly, reachability is injected, and harness authentication
is injected (``harness_auth=``) or the probe is replaced by a recording fake, so
no test starts a CLI or reads credentials. ``classify`` is wrapped by a spy that
delegates to the real classifier unless a test pins a verdict per entry id.
"""

from __future__ import annotations

import json

import pytest

from llm_kit.adapters import harness_auth as harness_auth_module
from llm_kit.cli import models as models_cli
from llm_kit.dispatch import declaration as d
from llm_kit.dispatch.factory import BackendSelection
from llm_kit.dispatch.reachability import Reachability
from llm_kit.registry import cost
from llm_kit.registry.cost import METERED, SUBSCRIPTION, CostVerdict, HarnessAuth
from llm_kit.registry.model_endpoints import HARNESS_KIND, TRANSPORT_KIND, EndpointEntry

# Literals captured from the declaration module BEFORE the allow filter existed
# (commit a82c0b45), for the declaration built by ``_baseline_entries``.
PRE_RENDER = (
    'Declared models (ordered by pace): choose one; default is marked.\n'
    '  qwen   transport/openrouter n/a (unpaced)   [default]\n'
    '  sol    codex          n/a (unpaced)\n'
    '  opus   claude/claude-cli n/a (unpaced)\n'
    '  haiku  claude/claude-cli n/a (unpaced)\n'
    'Rule: take the first usable entry of the ordered list (the default); a run is explainable from the declared list plus the logged pace readings.\n'
    'Re-select: only a classified halt (quota, rate limit, auth, insufficient credit, launch/transport) moves to the next usable entry; a quota or credit halt records the entry out of quota until its reset. A task error, schema-invalid output, or an unclassified timeout stays a failed attempt.'
)
PRE_TO_JSON = json.loads(r'''{
  "caller": "process",
  "default": "qwen",
  "rendered_entries": [
    {
      "declared_index": 0,
      "default": true,
      "drive": "openrouter",
      "family": null,
      "harness": null,
      "id": "qwen",
      "is_self": false,
      "kind": "transport",
      "model": "qwen-model",
      "pace": null,
      "pace_note": "n/a (unpaced)",
      "reachability": "reachable",
      "resets_at": null,
      "resolved": true,
      "shares_quota_with": [],
      "tier": null,
      "usability": null,
      "usable": true
    },
    {
      "declared_index": 1,
      "default": false,
      "drive": "codex-cli",
      "family": null,
      "harness": "codex",
      "id": "sol",
      "is_self": false,
      "kind": "harness",
      "model": "sol-model",
      "pace": null,
      "pace_note": "n/a (unpaced)",
      "reachability": "reachable",
      "resets_at": null,
      "resolved": true,
      "shares_quota_with": [],
      "tier": null,
      "usability": null,
      "usable": true
    },
    {
      "declared_index": 2,
      "default": false,
      "drive": "claude-cli",
      "family": null,
      "harness": "claude",
      "id": "opus",
      "is_self": false,
      "kind": "harness",
      "model": "opus-model",
      "pace": null,
      "pace_note": "n/a (unpaced)",
      "reachability": "reachable",
      "resets_at": null,
      "resolved": true,
      "shares_quota_with": [],
      "tier": null,
      "usability": null,
      "usable": true
    },
    {
      "declared_index": 3,
      "default": false,
      "drive": "claude-cli",
      "family": null,
      "harness": "claude",
      "id": "haiku",
      "is_self": false,
      "kind": "harness",
      "model": "haiku-model",
      "pace": null,
      "pace_note": "n/a (unpaced)",
      "reachability": "reachable",
      "resets_at": null,
      "resolved": true,
      "shares_quota_with": [],
      "tier": null,
      "usability": null,
      "usable": true
    }
  ],
  "rule": "Rule: take the first usable entry of the ordered list (the default); a run is explainable from the declared list plus the logged pace readings.\nRe-select: only a classified halt (quota, rate limit, auth, insufficient credit, launch/transport) moves to the next usable entry; a quota or credit halt records the entry out of quota until its reset. A task error, schema-invalid output, or an unclassified timeout stays a failed attempt."
}''')
PRE_FLOOR_STR = (
    'no usable routing target in [qwen, sol, ghost] for a process caller:\n'
    '  qwen: excluded (ruled out by the caller)\n'
    '  sol: excluded (ruled out by the caller)\n'
    '  ghost: unresolved (no registry entry)'
)
PRE_FLOOR_TO_JSON = json.loads(r'''{
  "caller": "process",
  "dispositions": [
    {
      "declared_index": 0,
      "detail": "ruled out by the caller",
      "disposition": "excluded",
      "id": "qwen",
      "resets_at": null
    },
    {
      "declared_index": 1,
      "detail": "ruled out by the caller",
      "disposition": "excluded",
      "id": "sol",
      "resets_at": null
    },
    {
      "declared_index": 2,
      "detail": "no registry entry",
      "disposition": "unresolved",
      "id": "ghost",
      "resets_at": null
    }
  ],
  "kind": "no-usable-routing-target",
  "message": "no usable routing target in [qwen, sol, ghost] for a process caller:\n  qwen: excluded (ruled out by the caller)\n  sol: excluded (ruled out by the caller)\n  ghost: unresolved (no registry entry)"
}''')


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def H(entry_id, harness="claude"):
    return EndpointEntry(
        id=entry_id, base_url=None, model=f"{entry_id}-model", kind=HARNESS_KIND, harness=harness
    )


def T(entry_id, unmetered=False):
    return EndpointEntry(
        id=entry_id, base_url=f"http://{entry_id}.invalid/v1", model=f"{entry_id}-model",
        billing_mode="unmetered" if unmetered else None,
        billing_owner="self" if unmetered else None,
    )


def reach(entries):
    return {
        n: Reachability(status="reachable", checked="cli-version", detail="reachable")
        for n in entries
    }


def _baseline_entries():
    return {
        "qwen": T("qwen"), "sol": H("sol", "codex"), "opus": H("opus", "claude"),
        "haiku": H("haiku", "claude"),
    }


BASELINE = ["qwen", "sol", "opus", "haiku"]


def describe(names, entries, **kw):
    kw.setdefault("caller", "process")
    kw.setdefault("reachability_cache", reach(entries))
    return d.describe(names, entries=entries, **kw)


class Spy:
    def __init__(self):
        self.classified = []
        self.normalized = 0
        self.probes = []
        self.verdicts = {}  # entry id -> (class, reason); unset entries use the real classifier
        self.auth = {}  # harness -> HarnessAuth for the recording probe


@pytest.fixture
def spy(monkeypatch):
    s = Spy()
    real_classify, real_normalize = cost.classify, cost.normalize_allow

    def classify(entry, **kw):
        s.classified.append(getattr(entry, "id", None))
        if entry is not None and entry.id in s.verdicts:
            return CostVerdict(*s.verdicts[entry.id])
        return real_classify(entry, **kw)

    def normalize_allow(allow):
        s.normalized += 1
        return real_normalize(allow)

    def probe(harnesses, env=None, *, cwd):
        names = list(harnesses)
        s.probes.append((names, cwd))
        return {h: s.auth.get(h, HarnessAuth(SUBSCRIPTION, f"probed {h}")) for h in names}

    monkeypatch.setattr(cost, "classify", classify)
    monkeypatch.setattr(cost, "normalize_allow", normalize_allow)
    monkeypatch.setattr(harness_auth_module, "probe_harness_auth", probe)
    return s


def _pin_three(spy):
    spy.verdicts.update({
        "free1": ("local", "stub-free"), "use1": ("subscription", "stub-usage"),
        "sp1": ("metered", "stub-spend"),
    })
    return {"free1": T("free1"), "use1": H("use1", "claude"), "sp1": H("sp1", "codex")}


# --------------------------------------------------------------------------
# C14: switched-off entries are shown, not hidden
# --------------------------------------------------------------------------


def test_switched_off_shown(spy):
    entries = _pin_three(spy)
    ranking = describe(["use1", "free1", "sp1"], entries, allow=["local"])

    lines = ranking.render().split("\n")
    assert lines[1].startswith("  free1")
    assert lines[2] == "Switched off: use1 (subscription), sp1 (metered)"
    assert "\n".join(lines[3:]) == ranking.rule
    assert [e.id for e in ranking.rendered_entries] == ["free1"]
    off = ranking.to_json()["switched_off"]
    assert off == [
        {"id": "use1", "declared_index": 0, "disposition": "switched-off",
         "detail": "subscription not allowed: stub-usage", "resets_at": None, "cost_class": "subscription"},
        {"id": "sp1", "declared_index": 2, "disposition": "switched-off",
         "detail": "metered not allowed: stub-spend", "resets_at": None, "cost_class": "metered"},
    ]
    assert [x.id for x in ranking.switched_off] == ["use1", "sp1"]
    assert ranking.switched_off == tuple(
        x for x in ranking.dispositions if x.disposition == d.DISPOSITION_SWITCHED_OFF
    )


def test_every_class_allowed_switches_nothing_off(spy):
    entries = _pin_three(spy)
    ranking = describe(["use1", "free1", "sp1"], entries, allow=["local", "subscription", "metered"])
    assert ranking.switched_off == ()
    assert "Switched off" not in ranking.render()
    assert "switched_off" not in ranking.to_json()


def test_switched_off_with_the_real_classifier_and_injected_auth(spy):
    entries = {
        "qwen": T("qwen", unmetered=True), "opus": H("opus", "claude"),
        "sol": H("sol", "codex"), "paid": T("paid"),
    }
    auth = {"claude": HarnessAuth(SUBSCRIPTION, "claude.ai"), "codex": HarnessAuth(METERED, "api key")}
    names = ["qwen", "opus", "sol", "paid"]

    free_only = describe(names, entries, allow=["local"], harness_auth=auth)
    assert [(x.id, x.cost_class) for x in free_only.switched_off] == [
        ("opus", "subscription"), ("sol", "metered"), ("paid", "metered"),
    ]
    assert all(x.detail.startswith(f"{x.cost_class} not allowed: ") for x in free_only.switched_off)
    assert [e.id for e in free_only.rendered_entries] == ["qwen"]

    with_usage = describe(names, entries, allow=["local", "subscription"], harness_auth=auth)
    assert [x.id for x in with_usage.switched_off] == ["sol", "paid"]
    assert [e.id for e in with_usage.rendered_entries] == ["qwen", "opus"]
    assert spy.probes == []  # supplied authentication is never re-probed


def test_floor_itemises_switched_off(spy):
    entries = _pin_three(spy)
    with pytest.raises(d.NoUsableRoutingTarget) as caught:
        describe(["use1", "sp1", "ghost", "free1"], entries, allow=["local"], exclude=["free1"])
    exc = caught.value
    assert exc.switched_off_ids == ("use1", "sp1")
    assert str(exc) == (
        "no usable routing target in [use1, sp1, ghost, free1] for a process caller:\n"
        "  use1: switched-off (subscription not allowed: stub-usage)\n"
        "  sp1: switched-off (metered not allowed: stub-spend)\n"
        "  ghost: unresolved (no registry entry)\n"
        "  free1: excluded (ruled out by the caller)"
    )
    by_id = {x["id"]: x for x in exc.to_json()["dispositions"]}
    assert by_id["use1"]["cost_class"] == "subscription"
    assert by_id["sp1"]["cost_class"] == "metered"
    assert "cost_class" not in by_id["ghost"] and "cost_class" not in by_id["free1"]


def test_switched_off_ids_empty_without_allow():
    with pytest.raises(d.NoUsableRoutingTarget) as caught:
        describe(["ghost"], {})
    assert caught.value.switched_off_ids == ()


# --------------------------------------------------------------------------
# C15: allow=None changes nothing
# --------------------------------------------------------------------------


def _no_filter_work(spy):
    assert spy.classified == []
    assert spy.normalized == 0
    assert spy.probes == []


def test_allow_none_byte_identical(spy):
    entries = _baseline_entries()
    ranking = describe(BASELINE, entries)
    assert ranking.render() == PRE_RENDER
    assert ranking.to_json() == PRE_TO_JSON
    assert ranking.switched_off == ()
    with pytest.raises(d.NoUsableRoutingTarget) as caught:
        describe(["qwen", "sol", "ghost"], entries, exclude=["qwen", "sol"])
    assert str(caught.value) == PRE_FLOOR_STR
    assert caught.value.to_json() == PRE_FLOOR_TO_JSON
    _no_filter_work(spy)


def test_allow_none_ignores_supplied_harness_auth(spy):
    entries = _baseline_entries()
    auth = {"claude": HarnessAuth(METERED, "x"), "codex": HarnessAuth(METERED, "x")}
    ranking = describe(BASELINE, entries, harness_auth=auth)
    assert ranking.render() == PRE_RENDER
    assert ranking.to_json() == PRE_TO_JSON
    _no_filter_work(spy)


def test_all_classes_allowed_matches_the_baseline_bytes(spy):
    ranking = describe(BASELINE, _baseline_entries(), allow=("local", "subscription", "metered"))
    assert ranking.render() == PRE_RENDER
    assert ranking.to_json() == PRE_TO_JSON


def test_run_without_allow_does_no_classification(spy):
    entries = {"qwen": T("qwen"), "opus": H("opus")}
    backend = _Scripted("openrouter", [_response()])
    result = d.run(
        ["qwen", "opus"], d.RunRequest(system="s", prompt="p"), entries=entries,
        backend_factory=_factory({"qwen": backend, "opus": _Scripted("claude-cli", [])}),
        reachability_cache=reach(entries),
    )
    assert result.status == d.RUN_COMPLETED
    _no_filter_work(spy)


# --------------------------------------------------------------------------
# Where the check sits
# --------------------------------------------------------------------------


def test_check_follows_the_unroutable_and_hidden_checks(spy):
    # Session caller: a transport has no agent loop, so it stays unroutable and hidden.
    entries = {"qwen": T("qwen"), "opus": H("opus"), "sol": H("sol", "codex")}
    ranking = describe(
        ["qwen", "opus", "sol", "ghost", "gone"], entries, caller="session",
        allow=["subscription"], exclude=["gone"], harness_auth={"claude": HarnessAuth(SUBSCRIPTION, "s"),
                                                       "codex": HarnessAuth(METERED, "m")},
    )
    assert spy.classified == ["opus", "sol"]  # qwen unroutable, ghost unresolved, gone excluded
    assert [x.id for x in ranking.switched_off] == ["sol"]
    assert [e.id for e in ranking.rendered_entries] == ["opus"]
    assert "qwen" not in ranking.render()  # still hidden


def test_check_precedes_the_requirements_match(spy, monkeypatch):
    from llm_kit.completion import requirements as requirements_module

    monkeypatch.setattr(requirements_module, "match_capabilities", lambda record, req: False)
    spy.verdicts.update({"off": ("metered", "stub-spend"), "on": ("local", "stub-free")})
    entries = {"off": T("off"), "on": T("on")}
    with pytest.raises(d.NoUsableRoutingTarget) as caught:
        describe(["off", "on"], entries, allow=["local"], requirements=["effort"])
    by_id = {x.id: x for x in caught.value.dispositions}
    assert by_id["off"].disposition == d.DISPOSITION_SWITCHED_OFF  # not requirements-mismatch
    assert by_id["on"].disposition == d.DISPOSITION_REQUIREMENTS_MISMATCH


# --------------------------------------------------------------------------
# C18: a switched-off entry is never probed
# --------------------------------------------------------------------------


def test_switched_off_not_probed(spy, monkeypatch):
    spy.verdicts.update({"on": ("local", "stub-free"), "off": ("metered", "stub-spend")})
    probed = []

    def check_many(to_probe, *, timeout, project_root):
        probed.append(sorted(to_probe))
        return {n: Reachability(status="reachable", checked="x", detail="reachable") for n in to_probe}

    monkeypatch.setattr(d, "check_many", check_many)
    ranking = d.describe(
        ["off", "on"], caller="process", entries={"off": T("off"), "on": T("on")}, allow=["local"]
    )
    assert probed == [["on"]]
    assert [x.id for x in ranking.switched_off] == ["off"]


# --------------------------------------------------------------------------
# Lazy, one-time harness authentication
# --------------------------------------------------------------------------


def test_auth_probe_is_lazy_once_per_harness_and_only_claude_codex(spy, tmp_path):
    entries = {
        "qwen": T("qwen", unmetered=True), "o1": H("o1", "claude"), "o2": H("o2", "Claude"),
        "sol": H("sol", "codex"), "oc": H("oc", "opencode"),
    }
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    describe(["qwen", "o1", "o2", "sol", "oc"], entries, allow=["local", "subscription"],
             project_root=tmp_path / "link")
    cwd = real.resolve()  # the probe directory is the RESOLVED project root
    assert spy.probes == [(["claude"], cwd), (["codex"], cwd)]  # opencode never probed


def test_no_probe_without_a_claude_or_codex_entry_met(spy):
    entries = {"qwen": T("qwen", unmetered=True), "oc": H("oc", "opencode"), "o1": H("o1", "claude")}
    describe(["qwen", "oc"], entries, allow=["local"])
    assert spy.probes == []
    # An excluded harness entry is not classified, so not probed either.
    with pytest.raises(d.NoUsableRoutingTarget):
        describe(["o1"], entries, allow=["local"], exclude=["o1"])
    assert spy.probes == []


def test_describe_probe_cwd_defaults_to_the_process_directory(spy, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    describe(["o1"], {"o1": H("o1", "claude")}, allow=["subscription"])
    assert spy.probes == [(["claude"], tmp_path.resolve())]


def test_unknown_probe_result_is_metered(spy):
    spy.auth["claude"] = HarnessAuth("unknown", "claude auth status failed")
    ranking = describe(["o1", "q"], {"o1": H("o1"), "q": T("q", unmetered=True)}, allow=["local", "subscription"])
    assert [x.id for x in ranking.switched_off] == ["o1"]
    assert ranking.switched_off[0].cost_class == "metered"


def test_unknown_allow_class_is_a_value_error(spy):
    with pytest.raises(ValueError):
        describe(["q"], {"q": T("q")}, allow=["local", "bogus"])
    with pytest.raises(ValueError):
        d.run(["q"], d.RunRequest(system="s", prompt="p"), entries={"q": T("q")}, allow=["bogus"])


# --------------------------------------------------------------------------
# run(): the filter survives re-selection and floors without dispatching
# --------------------------------------------------------------------------


class _Halt(Exception):
    def __init__(self, kind):
        super().__init__(f"halt {kind}")
        self.kind = kind


class _Scripted:
    def __init__(self, name, outcomes):
        self.name = name
        self.outcomes = list(outcomes)
        self.calls = 0

    def complete(self, system, user, *, model, options=None):
        self.calls += 1
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    def classify_halt(self, exc):
        return getattr(exc, "kind", None)


def _response():
    from llm_kit.completion.types import LLMResponse

    return LLMResponse(text="ok", model="m", input_tokens=1, output_tokens=1)


def _factory(backends, kind=TRANSPORT_KIND):
    def factory(name, **_kw):
        return BackendSelection(
            endpoint=name, kind=kind, backend=backends[name], model=f"{name}-model", effort=None
        )

    return factory


def test_run_generator_allow_survives_retry(spy):
    entries = {"a": T("a", unmetered=True), "b": T("b"), "c": T("c", unmetered=True)}
    backends = {
        "a": _Scripted("openrouter", [_Halt("quota")]),
        "b": _Scripted("openrouter", [_response()]),
        "c": _Scripted("openrouter", [_response()]),
    }
    result = d.run(
        ["a", "b", "c"], d.RunRequest(system="s", prompt="p"), entries=entries,
        backend_factory=_factory(backends), reachability_cache=reach(entries), max_attempts=2,
        allow=(cls for cls in ["local"]),  # one-shot: a second normalization would see nothing
    )
    assert result.status == d.RUN_COMPLETED
    assert result.entry == "c"  # b is metered: switched off on the retry as well
    assert [backends[n].calls for n in "abc"] == [1, 0, 1]


def test_run_floors_all_switched_off(spy):
    entries = {"a": T("a"), "b": T("b")}
    backends = {n: _Scripted("openrouter", [_response()]) for n in "ab"}
    with pytest.raises(d.NoUsableRoutingTarget) as caught:
        d.run(
            ["a", "b"], d.RunRequest(system="s", prompt="p"), entries=entries,
            backend_factory=_factory(backends), reachability_cache=reach(entries), allow=["local"],
        )
    assert caught.value.switched_off_ids == ("a", "b")
    assert [backends[n].calls for n in "ab"] == [0, 0]


def test_run_probes_auth_once_for_the_whole_run(spy, tmp_path):
    entries = {"o1": H("o1", "claude"), "o2": H("o2", "claude"), "o3": H("o3", "claude")}
    backends = {
        "o1": _Scripted("claude-cli", [_Halt("quota")]),
        "o2": _Scripted("claude-cli", [_Halt("quota")]),
        "o3": _Scripted("claude-cli", [_response()]),
    }
    result = d.run(
        ["o1", "o2", "o3"], d.RunRequest(system="s", prompt="p"), entries=entries,
        project_root=str(tmp_path), backend_factory=_factory(backends, HARNESS_KIND),
        reachability_cache=reach(entries), max_attempts=3, allow=["subscription"],
    )
    assert result.entry == "o3"
    assert spy.probes == [(["claude"], tmp_path.resolve())]


def test_run_probe_cwd_is_the_workspace_then_project_root(spy, tmp_path):
    ws, proj = tmp_path / "ws", tmp_path / "proj"
    ws.mkdir()
    proj.mkdir()
    entries = {"o1": H("o1", "claude")}
    backends = {"o1": _Scripted("claude-cli", [_response(), _response()])}
    for request, root, expected in (
        (d.RunRequest(system="s", prompt="p", workspace=ws), str(proj), ws),
        (d.RunRequest(system="s", prompt="p"), str(proj), proj),
    ):
        spy.probes.clear()
        d.run(
            ["o1"], request, entries=entries, project_root=root,
            backend_factory=_factory(backends, HARNESS_KIND), reachability_cache=reach(entries),
            allow=["subscription"],
        )
        assert spy.probes == [(["claude"], expected.resolve())]


# --------------------------------------------------------------------------
# CLI: models describe --allow
# --------------------------------------------------------------------------


class _FakeRanking:
    def render(self):
        return "rendered"

    def to_json(self):
        return {}


def _cli(monkeypatch, capsys, *argv):
    seen = []

    def fake_describe(ids, **kw):
        seen.append(kw)
        if "allow" in kw:
            cost.normalize_allow(kw["allow"])
        return _FakeRanking()

    monkeypatch.setattr(models_cli, "describe", fake_describe)
    code = models_cli.run(["describe", "a", "b", *argv])
    return code, seen, capsys.readouterr()


def test_cli_allow_is_split_and_passed(monkeypatch, capsys):
    code, seen, _out = _cli(monkeypatch, capsys, "--allow", "local,subscription", "--allow", "metered")
    assert code == 0
    assert seen[0]["allow"] == ["local", "subscription", "metered"]


def test_cli_without_allow_passes_no_keyword(monkeypatch, capsys):
    code, seen, _out = _cli(monkeypatch, capsys)
    assert code == 0
    assert "allow" not in seen[0]


def test_cli_unknown_allow_class_is_a_usage_error(monkeypatch, capsys):
    code, _seen, out = _cli(monkeypatch, capsys, "--allow", "local,bogus")
    assert code == 2
    assert "bogus" in out.err
