"""Differential parity: llm_kit.dispatch vs the legacy modules it was taken from.

The legacy side is imported from plugins/llm-scripting-kit/lib (and, for the
construction builder, plugins/content-pipeline-kit/lib) with a sys.path insert
scoped to the import. Every scenario is a function of a "side" (legacy or
final) and runs on both with the same inputs; the test asserts the two
outcomes are equal -- results, exception MRO names, messages and attributes,
stderr, and every file left in the scenario's work directory, byte for byte.

Nothing here makes a network call or starts a real CLI: reachability goes
through patched seams, backends are fakes, and HOME / USERPROFILE point at a
temp directory for every test.

Covered (legacy -> final):

* declaration                 -> dispatch.declaration (describe, run, Ranking,
                                 NoUsableRoutingTarget, _execution_event)
* observer                    -> dispatch.observer
* reachability                -> dispatch.reachability
* seats                       -> dispatch.seats
* usage_budget (policy slice) -> dispatch.usage_budget
* review_lane                 -> dispatch.review_lane
* completion/factory          -> dispatch.factory
* content_pipeline.llm.backends construction + probe slices
                              -> dispatch.pipeline_backends / dispatch.reachability

Deliberate differences, each pinned by name:

* the CLI command spelled in messages names the llm-kit venv and executable
  (design: "Persisted-state compatibility"); messages are compared with both
  spellings replaced by ``<CLI>``;
* the construction builder is NEW (no legacy function exists), so it is
  compared with what the legacy content_pipeline wrappers hand to the
  concrete constructor, and with the instance legacy ``create_backend``
  builds for the default harnesses.
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import types
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

import pytest

from final_wording import claude_contract_to_final, to_final

_REPO = Path(__file__).resolve().parents[2]
_LEGACY_LIB = _REPO / "plugins" / "llm-scripting-kit" / "lib"
_PIPELINE_LIB = _REPO / "plugins" / "content-pipeline-kit" / "lib"
_PACING_NOW = 1_000_000.0
_WEEK = 7 * 24 * 3600


# --------------------------------------------------------------------------
# Loading both sides
# --------------------------------------------------------------------------

# attribute -> (legacy module under llm_scripting_kit, final module)
_MODULES = {
    "decl": ("declaration", "llm_kit.dispatch.declaration"),
    "observer": ("observer", "llm_kit.dispatch.observer"),
    "reach": ("reachability", "llm_kit.dispatch.reachability"),
    "seats": ("seats", "llm_kit.dispatch.seats"),
    "usage": ("usage_budget", "llm_kit.dispatch.usage_budget"),
    "review": ("review_lane", "llm_kit.dispatch.review_lane"),
    "factory": ("completion.factory", "llm_kit.dispatch.factory"),
    "models": ("models", "llm_kit.registry.models"),
    "me": ("model_endpoints", "llm_kit.registry.model_endpoints"),
    "conserve": ("usage_budget", "llm_kit.registry.conserve"),
    "sources": ("usage_budget", "llm_kit.adapters.usage_sources"),
    "halt": ("completion.halt", "llm_kit.completion.halt"),
    "contract": ("completion.contract", "llm_kit.completion.contract"),
    "ctypes": ("completion.contract_types", "llm_kit.completion.contract_types"),
    "types": ("completion.types", "llm_kit.completion.types"),
    "acap": ("completion.adapter_capabilities", "llm_kit.adapters.capabilities"),
    "backends": ("completion.backends", "llm_kit.adapters.backends"),
    "codex": ("completion.codex_backend", "llm_kit.adapters.codex_backend"),
    "opencode": ("completion.opencode_backend", "llm_kit.adapters.opencode_backend"),
    "account": ("account", "llm_kit.adapters.account"),
    "constants": ("constants", "llm_kit.registry.constants"),
    "api_key": ("api_key", "llm_kit.registry.api_key"),
}


@pytest.fixture(scope="module")
def sides():
    sys.path.insert(0, str(_LEGACY_LIB))
    try:
        legacy = {
            attr: importlib.import_module(f"llm_scripting_kit.{leg}")
            for attr, (leg, _fin) in _MODULES.items()
        }
        # The legacy pipeline wrappers reach the shared lib through this facade
        # at call time.
        importlib.import_module("llm_scripting_kit.completion")
    finally:
        sys.path.remove(str(_LEGACY_LIB))
    final = {attr: importlib.import_module(fin) for attr, (_leg, fin) in _MODULES.items()}
    return (
        types.SimpleNamespace(name="legacy", **legacy),
        types.SimpleNamespace(name="final", **final),
    )


@pytest.fixture(scope="module")
def pipeline_backends(sides):
    """The legacy content_pipeline wrappers (imported with both legacy libs on the path)."""
    sys.path.insert(0, str(_LEGACY_LIB))
    sys.path.insert(0, str(_PIPELINE_LIB))
    try:
        return importlib.import_module("content_pipeline.llm.backends")
    finally:
        sys.path.remove(str(_PIPELINE_LIB))
        sys.path.remove(str(_LEGACY_LIB))


# --------------------------------------------------------------------------
# Outcome normalisation
# --------------------------------------------------------------------------

_MASKED_FIELDS = frozenset({"wall_ms", "started_at", "ended_at"})
_TEXT_SUBS: list[str] = []


def _text(value: str) -> str:
    for sub in _TEXT_SUBS:
        value = value.replace(sub, "<CLI>")
    # named deliberate wording differences (final_wording.RENAMES); idempotent
    return to_final(value)


# Fields llm-kit added after the legacy suite (cost-class filter). They hold their
# default unless a caller passes ``allow``, which no scenario here does (the legacy
# side has no such argument), so the differential ignores them only while default;
# a non-default value still reaches the comparison. ``test_allow_filter.py`` pins the
# allow=None output byte for byte against literals captured before the filter existed.
_FINAL_ONLY_DEFAULT_FIELDS = {("Disposition", "cost_class"): None, ("Ranking", "switched_off"): ()}


class _Out(tuple):
    """An ``outcome`` result: already normalised, so ``norm`` passes it through."""


def norm(value):
    """Reduce a value to comparable plain data, keeping class names."""
    if isinstance(value, _Out):
        return tuple(value)
    if isinstance(value, BaseException):
        return _exc(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = claude_contract_to_final(value)  # llm-kit delivers a claude contract natively
        out = {"__type__": type(value).__name__}
        for f in dataclasses.fields(value):
            item = getattr(value, f.name)
            key = (type(value).__name__, f.name)
            if key in _FINAL_ONLY_DEFAULT_FIELDS and item == _FINAL_ONLY_DEFAULT_FIELDS[key]:
                continue
            out[f.name] = "<masked>" if f.name in _MASKED_FIELDS else norm(item)
        return out
    if isinstance(value, Mapping):
        return ("map", type(value).__name__,
                [(norm(k), "<masked>" if k in _MASKED_FIELDS else norm(v)) for k, v in value.items()])
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, [norm(v) for v in value])
    if isinstance(value, (set, frozenset)):
        return (type(value).__name__, sorted((norm(v) for v in value), key=repr))
    if isinstance(value, Path):
        return ("Path", _text(str(value)))
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, (bytes, int, float, bool)) or value is None:
        return value
    if isinstance(value, type):
        return ("type", value.__name__)
    if isinstance(value, re.Pattern):
        return ("re", value.pattern, value.flags)
    if isinstance(value, types.SimpleNamespace):
        return ("ns", {k: norm(v) for k, v in vars(value).items()})
    if callable(value):
        return ("callable", getattr(value, "__name__", ""))
    return ("obj", type(value).__name__)


def _exc(exc: BaseException):
    cause = exc.__cause__
    return _Out((
        "raise",
        [c.__name__ for c in type(exc).__mro__],
        _text(str(exc)),
        None if cause is None else ([c.__name__ for c in type(cause).__mro__], _text(str(cause))),
        {k: norm(v) for k, v in sorted(vars(exc).items()) if not k.startswith("_")},
    ))


def first_diff(left, right, path="$"):
    """Where two normalised values first differ, as ``path: left != right``."""
    if type(left) is not type(right):
        return f"{path}: {left!r} != {right!r}"
    if isinstance(left, (tuple, list)):
        if len(left) != len(right):
            return f"{path}: length {len(left)} != {len(right)}: {left!r} != {right!r}"
        for index, (a, b) in enumerate(zip(left, right)):
            found = first_diff(a, b, f"{path}[{index}]")
            if found:
                return found
        return ""
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right), key=repr):
            if key not in left or key not in right:
                return f"{path}.{key!r}: present on one side only"
            found = first_diff(left[key], right[key], f"{path}.{key}")
            if found:
                return found
        return ""
    return "" if left == right else f"{path}: {left!r} != {right!r}"


def outcome(fn, *args, **kwargs):
    try:
        return _Out(("ok", norm(fn(*args, **kwargs))))
    except Exception as exc:  # noqa: BLE001 - parity compares any exception
        return _exc(exc)


def expect_raise(res, name: str, contains: str = ""):
    assert res[0] == "raise", res
    assert name in res[1], res
    assert contains in res[2], res
    return res


def _rmtree(path: Path) -> None:
    """Remove a tree, including the read-only objects a git repository holds."""

    def on_error(func, target, _exc):
        os.chmod(target, 0o700)
        func(target)

    if path.exists():
        shutil.rmtree(path, onerror=on_error)


def _tree(root: Path) -> list:
    """Every file under ``root`` (outside .git) with its bytes."""
    found = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if ".git" in rel.parts or not path.is_file():
            continue
        found.append((rel.as_posix(), path.read_bytes()))
    return found


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch, sides):
    """No host state: HOME is a temp dir and every variable the code reads is cleared."""
    home = tmp_path / "_home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name in (
        "MODEL_ENDPOINTS_REGISTRY", "OPENROUTER_API_KEY", "CLAUDE_PLUGIN_TEST",
        "CLAUDE_BOOTSTRAP_DATA_ROOT", "LLM_SCRIPTING_KIT_USAGE_SESSION",
        "CLAUDE_CODE_SESSION_ID", "LOCAL_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    for side in sides:
        monkeypatch.setattr(side.api_key, "USER_ENV_FILE", home / "no-user-layer.env")


class Ctx:
    """What a scenario receives: the side, a fresh work directory, and a patch helper."""

    def __init__(self, side, work: Path, stack: contextlib.ExitStack):
        self.side = side
        self.work = work
        self._stack = stack

    def patch(self, target, name, value):
        self._stack.enter_context(mock.patch.object(target, name, value))
        return value


@pytest.fixture
def par(sides, tmp_path, capsys):
    """``par(scenario)``: run on both sides, assert equal, return the legacy outcome value."""
    _TEXT_SUBS[:] = sorted(
        {
            s
            for side in sides
            for s in (
                side.constants.cli_command(),
                side.constants.CLI_COMMAND,
                getattr(side.constants, "REDIRECTED_CLI_COMMAND", ""),
            )
            if s
        },
        key=len,
        reverse=True,
    )
    work = tmp_path / "_work"

    def run(scenario):
        seen = []
        for side in sides:
            _rmtree(work)
            work.mkdir()
            capsys.readouterr()
            with contextlib.ExitStack() as stack:
                res = outcome(scenario, Ctx(side, work, stack))
            err = capsys.readouterr().err
            seen.append((res, err, _tree(work)))
        if seen[1] != seen[0]:
            pytest.fail("final differs from legacy at " + first_diff(list(seen[0]), list(seen[1])))
        return seen[0][0]

    return run


def ok(res):
    assert res[0] == "ok", res
    return res[1]


# --------------------------------------------------------------------------
# Builders (each takes the side's own classes)
# --------------------------------------------------------------------------

RESET = 1_790_000_000


def H(side, entry_id, harness="claude", *, paced=False, model=None, tier=None, family=None,
      spec=None):
    return side.me.EndpointEntry(
        id=entry_id, base_url=None, model=model or f"{entry_id}-model",
        kind=side.me.HARNESS_KIND, harness=harness, tier=tier, family=family,
        conserve_usage=spec if spec is not None else (
            side.conserve.ConserveSpec(pool="seven_day") if paced else None
        ),
    )


def T(side, entry_id):
    return side.me.EndpointEntry(
        id=entry_id, base_url=f"http://{entry_id}.invalid/v1", model=f"{entry_id}-model"
    )


def R(side, status="reachable", detail=None):
    return side.reach.Reachability(status=status, checked="cli-version", detail=detail or status)


class Control:
    """Pins every paced entry's verdict and fresh reading, as the legacy tests do."""

    def __init__(self, side):
        self.side = side
        self.pinned: dict = {}
        self.fresh: dict = {}

    def install(self, ctx: Ctx):
        side = self.side

        def pinned_evaluate(entry_id, spec, harness, **_kw):
            status = self.pinned.get(entry_id, side.conserve.STATUS_NO_DATA)
            return side.conserve.Budget(
                status=status, pool=spec.pool, detail=status, resets_at=RESET,
            )

        def fresh_reading(entry_id, spec, harness):
            remaining, window = self.fresh.get(entry_id, (None, None))
            if remaining is None:
                return side.conserve.Budget(
                    status=side.conserve.STATUS_NO_DATA, pool=spec.pool, detail="none"
                )
            return side.conserve.Budget(
                status=side.conserve.STATUS_AVAILABLE, pool=spec.pool, detail="fresh",
                remaining=remaining, window_remaining=window, resets_at=RESET,
            )

        ctx.patch(side.decl, "pinned_evaluate", pinned_evaluate)
        ctx.patch(side.decl, "_fresh_reading", fresh_reading)
        return self


def _quota(ctx, **statuses):
    c = Control(ctx.side).install(ctx)
    c.pinned.update(statuses)
    return c


# --------------------------------------------------------------------------
# declaration.describe
# --------------------------------------------------------------------------


def test_describe_orders_by_pace_and_renders(par):
    def scenario(ctx):
        s = ctx.side
        q = _quota(
            ctx, sol=s.conserve.STATUS_AVAILABLE, opus=s.conserve.STATUS_UNDER_QUOTA,
            fable=s.conserve.STATUS_OUT_OF_QUOTA,
        )
        q.fresh.update({"sol": (0.30, 0.60), "opus": (0.50, 0.40)})
        entries = {
            "qwen": T(s, "qwen"), "sol": H(s, "sol", "codex", paced=True),
            "opus": H(s, "opus", paced=True), "haiku": H(s, "haiku"),
            "fable": H(s, "fable", paced=True),
        }
        cache = {n: R(s) for n in entries}
        ranking = s.decl.describe(
            ["qwen", "sol", "opus", "haiku", "fable"], caller="process", entries=entries,
            reachability_cache=cache,
        )
        return ranking, ranking.render(), ranking.to_json(), ranking.default.id, cache

    _ranking, rendered, payload, default, _cache = view(par(scenario)[1])
    assert default == "qwen"
    assert "out of quota until" in rendered
    assert [e["id"] for e in payload["rendered_entries"]][:2] == ["qwen", "opus"]


def view(value):
    """Undo ``norm``'s tagging so a test can index into a result."""
    if isinstance(value, tuple) and len(value) == 3 and value[0] == "map":
        return {view(k): view(v) for k, v in value[2]}
    if (
        isinstance(value, tuple) and len(value) == 2
        and value[0] in ("list", "tuple", "set", "frozenset") and isinstance(value[1], list)
    ):
        return [view(v) for v in value[1]]
    if isinstance(value, tuple):
        return tuple(view(v) for v in value)
    if isinstance(value, list):
        return [view(v) for v in value]
    if isinstance(value, dict):
        return {k: view(v) for k, v in value.items()}
    return value


def test_describe_pace_table(par):
    """D5's "has a pace" table: about-to-reset, spent, no reading, unpaced, and a real pace."""

    def scenario(ctx):
        s = ctx.side
        avail = s.conserve.STATUS_AVAILABLE
        q = _quota(ctx, a=avail, b=avail, c=avail, d=avail, e=avail)
        q.fresh.update({"a": (0.5, 0.005), "b": (0.0, 0.5), "c": (0.5, 0.015), "e": (0.25, 0.25)})
        entries = {n: H(s, n, "claude", paced=True) for n in "abcde"}
        entries["f"] = H(s, "f", "claude")
        cache = {n: R(s) for n in entries}
        ranking = s.decl.describe(list("abcdef"), caller="process", entries=entries, reachability_cache=cache)
        return {e.id: (e.pace, e.pace_note) for e in ranking.rendered_entries}, ranking.render()

    paces, rendered = view(ok(par(scenario)))
    assert paces["a"] == [None, "about to reset"] and paces["b"] == [0.0, ""]
    assert paces["c"][0] is not None and paces["d"] == [None, "n/a (no reading)"]
    assert paces["f"] == [None, "n/a (unpaced)"] and "about to reset" in rendered


def test_describe_session_caller_self_and_shared_quota(par):
    def scenario(ctx):
        s = ctx.side
        q = _quota(ctx, opus=s.conserve.STATUS_AVAILABLE, sonnet=s.conserve.STATUS_UNDER_QUOTA,
                   sol=s.conserve.STATUS_AVAILABLE)
        q.fresh.update({"opus": (0.9, 0.5), "sonnet": (0.4, 0.5), "sol": (0.5, 0.5)})
        entries = {
            "opus": H(s, "opus", paced=True), "sonnet": H(s, "sonnet", paced=True),
            "sol": H(s, "sol", "codex", paced=True), "qwen": T(s, "qwen"),
            "oc": H(s, "oc", "opencode"),
        }
        cache = {n: R(s) for n in entries}
        ranking = s.decl.describe(
            ["sonnet", "sol", "opus", "qwen", "oc"], caller="session", self_ref="opus",
            entries=entries, reachability_cache=cache,
        )
        with_transport = s.decl.describe(
            ["sonnet", "qwen"], caller="session", entries=entries, reachability_cache=cache,
            dispatchable=("transport",),
        )
        return ranking, ranking.render(), ranking.to_json(), with_transport.to_json()

    _ranking, rendered, _payload, _t = view(par(scenario)[1])
    assert "[author]" in rendered and "shares seven_day with" in rendered


def test_describe_floor_itemises_every_disposition(par):
    def scenario(ctx):
        s = ctx.side
        _quota(ctx, sol=s.conserve.STATUS_OUT_OF_QUOTA)
        entries = {
            "sol": H(s, "sol", "codex", paced=True), "haiku": H(s, "haiku"),
            "fable": H(s, "fable", "codex"), "qwen": T(s, "qwen"), "gone": H(s, "gone"),
        }
        cache = {"haiku": R(s, "unreachable", "claude not found"), "gone": R(s)}
        return s.decl.describe(
            ["typo", "sol", "haiku", "fable", "qwen", "gone"], caller="session",
            entries=entries, reachability_cache=cache, exclude=["gone"],
        )

    res = par(scenario)
    expect_raise(res, "NoUsableRoutingTarget", "no usable routing target")
    for word in ("unresolved", "out-of-quota", "unreachable", "shadowed-core", "unroutable", "excluded"):
        assert word in res[2], word


def test_describe_floor_to_json(par):
    def scenario(ctx):
        s = ctx.side
        try:
            s.decl.describe(
                ["a"], caller="process", entries={"a": H(s, "a")},
                reachability_cache={"a": R(s, "unreachable", "dead")},
            )
        except s.decl.NoUsableRoutingTarget as floor:
            return floor.to_json(), floor.names, floor.caller
        raise AssertionError("expected the floor")

    ok(par(scenario))


def test_describe_argument_errors(par):
    def scenario(ctx):
        s = ctx.side
        out = {}
        entries = {"a": H(s, "a")}
        for label, call in (
            ("caller", lambda: s.decl.describe(["a"], caller="batch", entries=entries)),
            ("kind", lambda: s.decl.describe(["a"], caller="session", entries=entries,
                                             dispatchable=("quantum",))),
            ("empty", lambda: s.decl.describe([], caller="process", entries=entries)),
            ("dup", lambda: s.decl.describe(["a", "a"], caller="process", entries=entries)),
            ("nonstr", lambda: s.decl.describe([1], caller="process", entries=entries)),
        ):
            out[label] = outcome(call)
        return out

    got = view(ok(par(scenario)))
    assert set(got) == {"caller", "kind", "empty", "dup", "nonstr"}
    assert all(v[0] == "raise" for v in got.values())


def test_describe_requirements_match_adapter_advertisement(par):
    def scenario(ctx):
        s = ctx.side
        _quota(ctx)
        entries = {"qwen": T(s, "qwen"), "opus": H(s, "opus"), "sol": H(s, "sol", "codex")}
        cache = {n: R(s) for n in entries}
        results = {}
        for label, req in (("effort", ["effort"]), ("max_tokens", ["max_tokens"]),
                           ("guarantee", {"guarantees": ["filesystem-write"]})):
            results[label] = outcome(
                lambda req=req: s.decl.describe(
                    ["qwen", "opus", "sol"], caller="process", entries=entries,
                    reachability_cache=cache, requirements=req,
                ).to_json()
            )
        return results

    ok(par(scenario))


def test_describe_probes_only_what_can_be_dispatched(par):
    def scenario(ctx):
        s = ctx.side
        calls = []

        def fake_check_many(to_probe, *, timeout, project_root):
            calls.append((sorted(to_probe), timeout, project_root))
            return {n: R(s, "reachable") for n in to_probe}

        ctx.patch(s.decl, "check_many", fake_check_many)
        q = _quota(ctx, sol=s.conserve.STATUS_OUT_OF_QUOTA, opus=s.conserve.STATUS_AVAILABLE)
        q.fresh["opus"] = (0.5, 0.5)
        entries = {"sol": H(s, "sol", "codex", paced=True), "opus": H(s, "opus", paced=True),
                   "haiku": H(s, "haiku")}
        cache = {}
        ranking = s.decl.describe(["sol", "opus", "haiku"], caller="process", entries=entries,
                                  reachability_cache=cache, project_root="/p")
        return calls, sorted(cache), ranking.to_json()

    res = par(scenario)
    assert view(res[1])[0] == [[["haiku", "opus"], 5.0, "/p"]]


def test_describe_resolves_through_the_factory_and_registry_when_not_injected(par):
    def scenario(ctx):
        s = ctx.side
        _quota(ctx)
        entries = {"opus": H(s, "opus")}
        ctx.patch(s.models, "discover_model_entries", lambda **kw: entries)
        seen = []

        def factory(name, **kw):
            seen.append((name, kw))
            if name == "openrouter":
                return types.SimpleNamespace(kind="transport", backend=types.SimpleNamespace(name="openrouter"),
                                             model="m")
            raise s.models.EndpointResolveError(f"unknown endpoint {name}")

        ctx.patch(s.decl, "check_many", lambda to_probe, **kw: {n: R(s) for n in to_probe})
        ranking = s.decl.describe(
            ["openrouter", "opus", "nope"], caller="process", backend_factory=factory,
            project_root="/root",
        )
        return seen, ranking.to_json()

    ok(par(scenario))


def test_describe_real_pacing_against_a_snapshot(par):
    """No stubs between describe and the readers: a fake snapshot file, a session pin."""

    def scenario(ctx):
        s = ctx.side
        snap = ctx.work / "rate-limits.json"
        snap.write_text(json.dumps({"rate_limits": {"seven_day": {
            "used_percentage": 30, "resets_at": int(_PACING_NOW + _WEEK / 2)}}}))
        ctx.patch(s.usage, "CLAUDE_SNAPSHOT", snap)
        ctx.patch(s.usage, "VERDICT_CACHE", ctx.work / "verdicts" / "usage-verdicts.json")
        ctx.patch(os, "environ", {**os.environ, "CLAUDE_CODE_SESSION_ID": "sess-1"})
        ctx.patch(s.usage.time, "time", lambda: _PACING_NOW)
        entries = {"opus": H(s, "opus", paced=True), "haiku": H(s, "haiku")}
        cache = {n: R(s) for n in entries}
        ranking = s.decl.describe(["opus", "haiku"], caller="process", entries=entries,
                                  reachability_cache=cache)
        return ranking.to_json(), ranking.render()

    assert "available" in view(par(scenario)[1])[1]


# --------------------------------------------------------------------------
# declaration.run
# --------------------------------------------------------------------------


class _Halt(Exception):
    def __init__(self, kind, resets_at=None):
        super().__init__(f"halt {kind}")
        self.kind = kind
        self.resets_at = resets_at


class _Scripted:
    def __init__(self, name, outcomes, on_call=None, controls=()):
        self.name = name
        self.outcomes = list(outcomes)
        self.calls = []
        self.on_call = on_call
        self.controls = controls

    def complete(self, system, user, *, model, options=None):
        self.calls.append((system, user, model, norm(options)))
        if self.on_call:
            self.on_call()
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    def classify_halt(self, exc):
        return getattr(exc, "kind", None)


def _factory_for(side, backends):
    def factory(name, **_kw):
        if name not in backends:
            raise side.models.EndpointResolveError(f"unknown endpoint '{name}'")
        return side.factory.BackendSelection(
            endpoint=name, kind=side.me.HARNESS_KIND, backend=backends[name], model=f"{name}-model",
            effort=None,
        )

    return factory


def _run_entries(s):
    return {"sol": H(s, "sol", "codex", paced=True), "opus": H(s, "opus"),
            "kimi": H(s, "kimi", "claude", paced=True)}


def _response(s, text="ok"):
    return s.types.LLMResponse(text=text, model="m", input_tokens=3, output_tokens=2)


def test_run_first_usable_entry_completes(par):
    def scenario(ctx):
        s = ctx.side
        _quota(ctx)
        backends = {"sol": _Scripted("codex-cli", [_response(s)]), "opus": _Scripted("claude-cli", [])}
        result = s.decl.run(
            ["sol", "opus"], s.decl.RunRequest(system="s", prompt="p"), entries=_run_entries(s),
            backend_factory=_factory_for(s, backends),
            reachability_cache={"sol": R(s), "opus": R(s)},
        )
        return result, [b.calls for b in backends.values()]

    ok(par(scenario))


@pytest.mark.parametrize("kind", ["quota", "insufficient_credit"])
def test_run_quota_halt_writes_the_pool_verdict_byte_for_byte(par, kind):
    """A real record_observed_halt: the verdict-cache file is compared as bytes."""

    def scenario(ctx):
        s = ctx.side
        _quota(ctx)
        cache_file = ctx.work / "v" / "usage-verdicts.json"
        ctx.patch(s.usage, "VERDICT_CACHE", cache_file)
        ctx.patch(s.usage.time, "time", lambda: _PACING_NOW)
        ctx.patch(os, "environ", {**os.environ, "LLM_SCRIPTING_KIT_USAGE_SESSION": "run-sess"})
        backends = {
            "sol": _Scripted("codex-cli", [_Halt(kind, resets_at=RESET)]),
            "kimi": _Scripted("claude-cli", [_response(s)]),
            "opus": _Scripted("claude-cli", [_response(s)]),
        }
        entries = _run_entries(s)
        attempts = []
        result = s.decl.run(
            ["sol", "kimi", "opus"], s.decl.RunRequest(system="s", prompt="p"), entries=entries,
            backend_factory=_factory_for(s, backends),
            reachability_cache={n: R(s) for n in entries}, max_attempts=3,
            on_attempt=attempts.append,
        )
        return result, attempts

    res = par(scenario)
    assert view(res[1])[0]["status"] == "completed"


def test_run_halt_spends_every_entry_sharing_the_pool(par):
    def scenario(ctx):
        s = ctx.side
        _quota(ctx)
        cache_file = ctx.work / "v" / "usage-verdicts.json"
        ctx.patch(s.usage, "VERDICT_CACHE", cache_file)
        ctx.patch(os, "environ", {**os.environ, "CLAUDE_CODE_SESSION_ID": "pool-sess"})
        entries = {
            "opus": H(s, "opus", paced=True), "sonnet": H(s, "sonnet", paced=True),
            "sol": H(s, "sol", "codex", paced=True),
        }
        backends = {"opus": _Scripted("claude-cli", [_Halt("quota", resets_at=RESET)]),
                    "sol": _Scripted("codex-cli", [_response(s)])}
        result = s.decl.run(
            ["opus", "sonnet", "sol"], s.decl.RunRequest(system="s", prompt="p"), entries=entries,
            backend_factory=_factory_for(s, {**backends, "sonnet": _Scripted("claude-cli", [])}),
            reachability_cache={n: R(s) for n in entries}, max_attempts=3,
        )
        return result, json.loads(cache_file.read_text())

    res = par(scenario)
    stored = view(res[1])[1]
    assert set(stored["verdicts"]) == {"opus", "sonnet"}


def test_run_attempt_limit_floor_task_error_and_launch_failure(par):
    def scenario(ctx):
        s = ctx.side
        _quota(ctx)
        out = {}
        entries = _run_entries(s)
        cache = lambda: {n: R(s) for n in entries}  # noqa: E731

        def go(backends, **kw):
            return s.decl.run(
                ["sol", "opus"], s.decl.RunRequest(system="s", prompt="p"), entries=entries,
                backend_factory=_factory_for(s, backends), reachability_cache=kw.pop("cache", cache()),
                **kw,
            )

        ctx.patch(s.decl, "record_observed_halt", lambda *a, **k: None)
        out["limit"] = outcome(lambda: go(
            {"sol": _Scripted("codex-cli", [_Halt("quota")]), "opus": _Scripted("claude-cli", [_response(s)])},
            max_attempts=1))
        out["floor"] = outcome(lambda: go(
            {"sol": _Scripted("codex-cli", [_Halt("quota")]), "opus": _Scripted("claude-cli", [_Halt("auth")])},
            max_attempts=5))
        out["task_error"] = outcome(lambda: go(
            {"sol": _Scripted("codex-cli", [RuntimeError("bad")]), "opus": _Scripted("claude-cli", [])},
            max_attempts=3))
        launch_cache = cache()
        out["launch"] = outcome(lambda: go(
            {"sol": _Scripted("codex-cli", [FileNotFoundError("codex")]),
             "opus": _Scripted("claude-cli", [_response(s)])},
            max_attempts=3, cache=launch_cache))
        out["launch_cache"] = norm(launch_cache)
        out["bad_attempts"] = outcome(lambda: go({}, max_attempts=0))
        return out

    ok(par(scenario))


def test_run_resets_a_workspace_before_reselecting(par):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")

    def scenario(ctx):
        s = ctx.side
        _quota(ctx)
        ctx.patch(s.decl, "record_observed_halt", lambda *a, **k: None)
        repo = ctx.work / "ws"
        repo.mkdir()
        subprocess.run([git, "-C", str(repo), "init", "-q"], check=True)
        (repo / "keep.txt").write_text("keep\n")

        def litter():
            (repo / "stray.txt").write_text("stray\n")
            (repo / "keep.txt").write_text("changed\n")

        backends = {
            "sol": _Scripted("codex-cli", [_Halt("quota")], on_call=litter),
            "opus": _Scripted("claude-cli", [_response(s)]),
        }
        entries = _run_entries(s)
        attempts = []
        result = s.decl.run(
            ["sol", "opus"], s.decl.RunRequest(system="s", prompt="p", workspace=repo),
            entries=entries, backend_factory=_factory_for(s, backends),
            reachability_cache={n: R(s) for n in entries}, max_attempts=3,
            on_attempt=attempts.append,
        )
        return result, attempts, (repo / "keep.txt").read_text(), (repo / "stray.txt").exists()

    res = par(scenario)
    assert view(res[1])[2] == "keep\n" and view(res[1])[3] is False


def test_run_workspace_that_cannot_be_snapshotted_ends_the_run(par):
    def scenario(ctx):
        s = ctx.side
        _quota(ctx)
        ctx.patch(s.decl, "record_observed_halt", lambda *a, **k: None)
        entries = _run_entries(s)
        backends = {"sol": _Scripted("codex-cli", [_Halt("quota")]),
                    "opus": _Scripted("claude-cli", [_response(s)])}
        return s.decl.run(
            ["sol", "opus"],
            s.decl.RunRequest(system="s", prompt="p", workspace=ctx.work / "not-a-repo"),
            entries=entries, backend_factory=_factory_for(s, backends),
            reachability_cache={n: R(s) for n in entries}, max_attempts=3,
        )

    ok(par(scenario))


def test_run_observer_events_and_support_errors(par):
    def scenario(ctx):
        s = ctx.side
        _quota(ctx)
        entries = _run_entries(s)

        class Recorder:
            def __init__(self):
                self.events = []

            def emit(self, event, **fields):
                self.events.append((event, norm(fields)))

        out = {}
        for label, outcomes in (("completed", [_response(s)]), ("halted", [_Halt("auth")])):
            rec = Recorder()
            backends = {"sol": _Scripted("codex-cli", list(outcomes)),
                        "opus": _Scripted("claude-cli", [_response(s)])}
            out[label] = (outcome(lambda: s.decl.run(
                ["sol", "opus"], s.decl.RunRequest(system="s", prompt="p"), entries=entries,
                backend_factory=_factory_for(s, backends),
                reachability_cache={n: R(s) for n in entries}, max_attempts=2, observer=rec,
            )), rec.events)
        out["isinstance"] = (
            isinstance(Recorder(), s.observer.ExecutionObserver),
            isinstance(object(), s.observer.ExecutionObserver),
        )
        # An old bootstrap_lib: the probe refuses before any dispatch.
        old = types.ModuleType("bootstrap_lib.execution_event")
        old.SUPPORTED_SCHEMAS = frozenset({"other/v9"})
        old.usage_payload = lambda **kw: None
        with mock.patch.dict(sys.modules, {"bootstrap_lib.execution_event": old}):
            out["too_old"] = outcome(lambda: s.decl._execution_event())
        broken = types.ModuleType("bootstrap_lib.execution_event")
        broken.SUPPORTED_SCHEMAS = frozenset({"plugins-kit.execution-event/v1"})
        broken.usage_payload = lambda: None
        with mock.patch.dict(sys.modules, {"bootstrap_lib.execution_event": broken}):
            out["bad_signature"] = outcome(lambda: s.decl._execution_event())
        with mock.patch.dict(sys.modules, {"bootstrap_lib": None}):
            out["absent"] = outcome(lambda: s.decl._execution_event())
        return out

    res = par(scenario)
    got = view(res[1])
    assert got["completed"][1][0][0] == "dispatch-selected"
    assert got["isinstance"] == [True, False]


def test_run_requirements_output_contracts_and_armed_calls(par):
    def scenario(ctx):
        s = ctx.side
        _quota(ctx)
        entries = {"opus": H(s, "opus"), "sol": H(s, "sol", "codex")}
        seen = []
        real = s.decl.describe

        def spy(names, **kw):
            seen.append(norm(kw.get("requirements")))
            return real(names, **kw)

        ctx.patch(s.decl, "describe", spy)
        cache = {n: R(s) for n in entries}
        contract = s.ctypes.OutputContract(id="c.text", policy="text-only")
        out = {}

        def go(backends, *, options=None, **kw):
            return s.decl.run(
                ["opus", "sol"], s.decl.RunRequest(system="s", prompt="p", options=options),
                entries=entries, backend_factory=_factory_for(s, backends),
                reachability_cache=dict(cache), **kw,
            )

        plain = lambda: {"opus": _Scripted("claude-cli", [_response(s)]),  # noqa: E731
                         "sol": _Scripted("codex-cli", [_response(s)])}
        out["contract_merged"] = outcome(lambda: go(
            plain(), options=s.types.BackendOptions(output_contract=contract)))
        out["contract_conflict"] = outcome(lambda: go(
            plain(), options=s.types.BackendOptions(output_contract=contract),
            requirements={"structured_output": {"policies": ["validated-result"]}}))
        violation = s.contract.OutputContractViolation(_response(s))
        out["violation"] = outcome(lambda: go(
            {"opus": _Scripted("claude-cli", [violation]), "sol": _Scripted("codex-cli", [])},
            options=s.types.BackendOptions(output_contract=contract)))

        class Armed(s.backends.ClaudeCliBackend):
            def complete(self, system, user, *, model, options=None):
                applied = ("text-only-mode",) if self.text_only else ()
                return s.types.LLMResponse(text="ok", model="m", execution_controls_applied=applied)

        class Dishonest(s.backends.ClaudeCliBackend):
            def complete(self, system, user, *, model, options=None):
                return s.types.LLMResponse(text="ok", model="m", execution_controls_applied=())

        for label, cls in (("armed", Armed), ("armed_dishonest", Dishonest)):
            out[label] = outcome(lambda cls=cls: go(
                {"opus": cls(), "sol": _Scripted("codex-cli", [])},
                requirements={"guarantees": ["filesystem-write"]},
            ))
        out["seen"] = seen
        return out

    got = view(ok(par(scenario)))
    assert got["contract_merged"][0] == "ok" and got["contract_conflict"][0] == "raise"
    assert got["violation"][1]["status"] == "failed" and got["armed"][1]["status"] == "completed"
    assert got["armed_dishonest"][0] == "raise" and "armed control" in got["armed_dishonest"][2]


# --------------------------------------------------------------------------
# reachability
# --------------------------------------------------------------------------


def _probe(side, ok=True, resolved=True, detail="ok"):
    return side.account.EndpointProbe(
        ok=ok, endpoint="fd", base_url="http://fd.invalid/v1", detail=detail, resolved=resolved
    )


def _frontdoor_ep(**over):
    ep = {"name": "fd", "base_url": "http://fd.invalid/v1", "frontdoor": True, "default": "d",
          "models": {"d": {"slug": "grp"}}, "key_env": None}
    ep.update(over)
    return ep


def _health_body(status="reachable", protocol=1):
    return json.dumps({"protocol": protocol, "groups": {"grp": {
        "status": status, "deployments": [{"id": "a", "status": status}]}}}).encode()


def test_reachability_transport_paths(par):
    def scenario(ctx):
        s = ctx.side
        out = {}
        clock = iter(float(i) for i in range(1000))
        ctx.patch(s.reach, "_monotonic", lambda: next(clock) * 0.01)
        cases = {
            "plain_ok": (None, None, _probe(s)),
            "plain_down": (None, None, _probe(s, ok=False, detail="refused")),
            "unresolved": (None, None, _probe(s, ok=False, resolved=False, detail="no such endpoint")),
            "fd_reachable": (_frontdoor_ep(), (_health_body("reachable"), None), _probe(s)),
            "fd_unreachable": (_frontdoor_ep(), (_health_body("unreachable"), None), _probe(s)),
            "fd_unknown_falls_back": (_frontdoor_ep(), (_health_body("unknown"), None), _probe(s)),
            "fd_bad_protocol": (_frontdoor_ep(), (_health_body("reachable", protocol=2), None), _probe(s)),
            "fd_failed_request": (_frontdoor_ep(), (b"", OSError("boom")), _probe(s)),
            "fd_garbage": (_frontdoor_ep(), (b"not json", None), _probe(s, ok=False, detail="x")),
            "fd_no_group": (_frontdoor_ep(models={}), None, _probe(s)),
            "not_marked": (_frontdoor_ep(frontdoor=False), None, _probe(s)),
        }
        for label, (ep, health, probe) in cases.items():
            def resolve(name, **kw):
                if ep is None:
                    raise s.models.EndpointResolveError("no endpoint")
                return ep

            fetched = []

            def fetch(url, **kw):
                fetched.append((url, norm(kw)))
                return health

            probed = []

            def probe_endpoint(name, **kw):
                probed.append((name, kw))
                return probe

            with mock.patch.object(s.models, "resolve_endpoint", resolve), \
                    mock.patch.object(s.reach, "_fetch_health", fetch), \
                    mock.patch.object(s.reach, "probe_endpoint", probe_endpoint):
                out[label] = (outcome(lambda: s.reach.check_transport("fd", timeout=5.0, project_root="/r")),
                              fetched, probed)
        return out

    got = view(ok(par(scenario)))
    verdicts = {label: (v[0][1]["status"], v[0][1]["checked"]) for label, v in got.items()}
    assert verdicts["plain_ok"] == ("reachable", "models-endpoint")
    assert verdicts["unresolved"] == ("unknown", "models-endpoint")
    assert verdicts["fd_reachable"] == ("reachable", "frontdoor-backends")
    assert verdicts["fd_unreachable"] == ("unreachable", "frontdoor-backends")
    assert verdicts["fd_unknown_falls_back"] == ("reachable", "frontdoor-backends+models-fallback")


def test_reachability_harness_paths(par):
    def scenario(ctx):
        s = ctx.side
        out = {}
        runs = []

        def fake_run(argv, **kw):
            runs.append(list(argv))
            tail = argv[0]
            if "bad" in tail:
                return types.SimpleNamespace(returncode=3, stdout=b"")
            if "slow" in tail:
                raise subprocess.TimeoutExpired(argv, 1)
            if "gone" in tail:
                raise FileNotFoundError(tail)
            return types.SimpleNamespace(returncode=0, stdout=b"\n  tool 1.2.3\nmore\n")

        ctx.patch(s.reach.subprocess, "run", fake_run)
        for label, which in (("found", "/bin/claude-ok"), ("missing", None), ("bad", "/bin/claude-bad"),
                             ("slow", "/bin/claude-slow"), ("gone", "/bin/claude-gone")):
            with mock.patch.object(s.reach.shutil, "which", lambda name, which=which: which):
                out[f"claude_{label}"] = outcome(lambda: s.reach.check_harness("claude", timeout=3))
        with mock.patch.object(s.reach.shutil, "which", lambda name: "/bin/codex-ok"), \
                mock.patch.dict(sys.modules, {"bootstrap_lib.codex": None}):
            out["codex_fallback"] = outcome(lambda: s.reach.check_harness("codex"))
        with mock.patch.object(s.reach.shutil, "which", lambda name: None), \
                mock.patch.dict(sys.modules, {"bootstrap_lib.codex": None}):
            out["codex_missing"] = outcome(lambda: s.reach.check_harness("codex"))
        fake_codex = types.ModuleType("bootstrap_lib.codex")
        fake_codex.detect_codex = lambda timeout: types.SimpleNamespace(available=True, reason="codex 9.9")
        with mock.patch.dict(sys.modules, {"bootstrap_lib.codex": fake_codex}):
            out["codex_detected"] = outcome(lambda: s.reach.check_harness("codex"))
        for label, prefix in (("opencode_found", ["/bin/oc-ok"]), ("opencode_missing", None)):
            with mock.patch.object(s.reach, "resolve_opencode_cli", lambda prefix=prefix: prefix):
                out[label] = outcome(lambda: s.reach.check_harness("opencode"))
        out["unknown_harness"] = outcome(lambda: s.reach.check_harness("zed"))
        out["none_harness"] = outcome(lambda: s.reach.check_harness(None))
        out["runs"] = runs
        return out

    ok(par(scenario))


def test_reachability_entry_dispatch_and_concurrent_checks(par):
    def scenario(ctx):
        s = ctx.side
        out = {}
        ctx.patch(s.reach, "check_harness", lambda h, timeout: R(s, "reachable", f"h:{h}:{timeout}"))
        ctx.patch(s.reach, "check_transport", lambda n, timeout, project_root: R(s, "reachable",
                                                                                  f"t:{n}:{timeout}:{project_root}"))
        out["harness"] = s.reach.check_entry({"kind": "harness", "harness": "codex"}, "x", timeout=1)
        out["transport"] = s.reach.check_entry({"kind": "transport"}, "qwen", timeout=2, project_root="/p")

        def boom(*a, **k):
            raise RuntimeError("kaboom")

        with mock.patch.object(s.reach, "check_transport", boom):
            out["raises"] = s.reach.check_entry({"kind": "transport"}, "qwen")
        out["many"] = s.reach.check_many(
            {"a": {"kind": "harness", "harness": "claude"}, "b": {"kind": "transport"}}, timeout=4,
        )
        out["empty"] = s.reach.check_many({})
        return out

    ok(par(scenario))


def test_probe_model_endpoint_matches_the_legacy_wrapper_probe(sides, pipeline_backends, monkeypatch):
    legacy, final = sides
    calls = []

    def recorder(name, **kw):
        calls.append((name, kw))
        return "probe-result"

    monkeypatch.setattr(legacy.account, "probe_endpoint", recorder)
    wrapper = pipeline_backends.ModelEndpointBackend(endpoint="local", project_root=Path("/proj"))
    legacy_result = wrapper.probe(timeout=1.5)
    legacy_calls, calls[:] = list(calls), []
    wrapper_no_root = pipeline_backends.ModelEndpointBackend(endpoint="local")
    wrapper_no_root.probe()
    legacy_calls += calls
    calls[:] = []

    monkeypatch.setattr(final.reach, "probe_endpoint", recorder)
    final_result = final.reach.probe_model_endpoint("local", timeout=1.5, project_root=Path("/proj"))
    final.reach.probe_model_endpoint("local")
    assert (final_result, calls) == (legacy_result, legacy_calls)
    assert legacy_calls[1][1] == {"timeout": 2.0, "project_root": None}


# --------------------------------------------------------------------------
# seats
# --------------------------------------------------------------------------


def test_seats_discovery_orders_probes_and_pacing(par):
    def scenario(ctx):
        s = ctx.side
        entries = [
            H(s, "self", "claude", model="self-model", tier=2, family="alpha"),
            H(s, "same-family", model="same-model", tier=2, family="alpha"),
            H(s, "beside-z", model="beside-z-model", tier=2, family="beta"),
            H(s, "up-low", model="up-low-model", tier=3, family="beta"),
            H(s, "up-high", model="up-high-model", tier=4, family="alpha", paced=True),
            H(s, "up-spent", model="up-spent-model", tier=4, family="gamma", paced=True),
            H(s, "up-slow", model="up-slow-model", tier=4, family="delta", paced=True),
            H(s, "lower", model="lower-model", tier=1, family="beta"),
            H(s, "down-probe", model="dp-model", tier=3, family="eps"),
            H(s, "loose", model="loose-model"),
            T(s, "transport"),
        ]
        registry = s.me.EndpointRegistry(entries={e.id: e for e in entries})
        ctx.patch(s.seats, "load_model_config", lambda project_root=None: {"endpoints": {}})
        probes = []

        def check_many(to_probe, *, timeout, project_root):
            probes.append((list(to_probe), timeout, project_root))
            return {
                n: (R(s, "unknown") if n == "down-probe" else R(s, "unreachable") if n == "beside-z"
                    else R(s)) for n in to_probe
            }

        ctx.patch(s.seats, "check_many", check_many)
        verdicts = {"up-high": s.conserve.STATUS_UNDER_QUOTA, "up-spent": s.conserve.STATUS_OUT_OF_QUOTA,
                    "up-slow": s.conserve.STATUS_AVAILABLE}

        def pinned(entry_id, spec, harness, **kw):
            return s.conserve.Budget(status=verdicts[entry_id], pool=spec.pool, detail="d", resets_at=RESET)

        ctx.patch(s.seats, "pinned_evaluate", pinned)
        result = s.seats.discover_seats("self", registry=registry, timeout=2.5, project_root="/r")
        return result, result.to_json(), probes

    res = par(scenario)
    assert [seat["endpoint"] for seat in view(res[1])[1]["seats"]][:1] == ["up-slow"]


def test_seats_resolution_errors(par):
    def scenario(ctx):
        s = ctx.side
        entries = [
            H(s, "a", model="shared", tier=2, family="x"), H(s, "b", model="shared", tier=2, family="y"),
            H(s, "loose", model="loose-model"), T(s, "net"),
            H(s, "haiku", model="claude-haiku-9", tier=1, family="anthropic"),
        ]
        registry = s.me.EndpointRegistry(entries={e.id: e for e in entries})
        ctx.patch(s.seats, "load_model_config", lambda project_root=None: {"endpoints": {}})
        ctx.patch(s.seats, "check_many", lambda to_probe, **kw: {})
        return {
            ref: outcome(lambda ref=ref: s.seats.discover_seats(ref, registry=registry))
            for ref in ("shared", "missing", "loose", "net", "haiku")
        }

    ok(par(scenario))


def test_seats_real_pacing_reads_the_snapshot(par):
    def scenario(ctx):
        s = ctx.side
        snap = ctx.work / "rl.json"
        snap.write_text(json.dumps({"rate_limits": {"seven_day": {
            "used_percentage": 100, "resets_at": int(_PACING_NOW + 3600)}}}))
        ctx.patch(s.usage, "CLAUDE_SNAPSHOT", snap)
        ctx.patch(s.usage, "VERDICT_CACHE", ctx.work / "c" / "v.json")
        ctx.patch(os, "environ", {**os.environ, "CLAUDE_CODE_SESSION_ID": "seat-sess"})
        ctx.patch(s.usage.time, "time", lambda: _PACING_NOW)
        entries = [H(s, "me", tier=2, family="a"), H(s, "boss", tier=4, family="b", paced=True)]
        registry = s.me.EndpointRegistry(entries={e.id: e for e in entries})
        ctx.patch(s.seats, "load_model_config", lambda project_root=None: {"endpoints": {}})
        ctx.patch(s.seats, "check_many", lambda to_probe, **kw: {n: R(s) for n in to_probe})
        return s.seats.discover_seats("me", registry=registry).to_json()

    res = par(scenario)
    assert len(view(res[1])["out_of_quota"]) == 1


# --------------------------------------------------------------------------
# usage_budget: the pacing policy
# --------------------------------------------------------------------------


def _write_claude_snapshot(path: Path, *, used, resets_in, pool="seven_day"):
    path.write_text(json.dumps({"rate_limits": {pool: {
        "used_percentage": used, "resets_at": int(_PACING_NOW + resets_in)}}}))


def test_evaluate_dispatches_by_harness_with_fake_snapshots(par):
    def scenario(ctx):
        s = ctx.side
        snap = ctx.work / "rl.json"
        sessions = ctx.work / "sessions"
        sessions.mkdir()
        ctx.patch(s.usage, "CLAUDE_SNAPSHOT", snap)
        ctx.patch(s.usage, "CODEX_SESSIONS_DIR", sessions)
        seven = s.conserve.ConserveSpec(pool="seven_day")
        out = {}
        out["no_snapshot"] = s.usage.evaluate(seven, "claude", now=_PACING_NOW)
        for label, used in (("ahead", 10), ("on_pace", 50), ("behind", 80), ("spent", 100)):
            _write_claude_snapshot(snap, used=used, resets_in=_WEEK / 2)
            out[f"claude_{label}"] = s.usage.evaluate(seven, "Claude ", now=_PACING_NOW)
        _write_claude_snapshot(snap, used=10, resets_in=-5)
        out["dead_window"] = s.usage.evaluate(seven, "claude", now=_PACING_NOW)
        out["no_rollout"] = s.usage.evaluate(seven, "codex", now=_PACING_NOW)
        (sessions / "r.jsonl").write_text(json.dumps({"payload": {"rate_limits": {"primary": {
            "used_percent": 60, "window_minutes": 300, "resets_at": int(_PACING_NOW + 9000)}}}}) + "\n")
        out["codex"] = s.usage.evaluate(seven, "codex", now=_PACING_NOW)
        out["opencode"] = s.usage.evaluate(seven, "opencode", now=_PACING_NOW)
        out["transport"] = s.usage.evaluate(seven, None, now=_PACING_NOW)
        out["alias"] = s.usage.evaluate_usage_budget is s.usage.evaluate
        return out

    got = view(ok(par(scenario)))
    assert got["alias"] is True
    assert [got[k]["status"] for k in ("claude_ahead", "claude_on_pace", "claude_behind", "claude_spent")] == [
        "available", "available", "under-quota", "out-of-quota"]
    assert got["no_snapshot"]["status"] == got["dead_window"]["status"] == got["opencode"]["status"] == "no-data"
    assert got["codex"]["status"] == "under-quota"


def test_session_key_precedence(par):
    def scenario(ctx):
        s = ctx.side
        out = {}
        for label, env in (
            ("none", {}), ("blank", {"CLAUDE_CODE_SESSION_ID": "  "}),
            ("claude", {"CLAUDE_CODE_SESSION_ID": " c1 "}),
            ("override", {"CLAUDE_CODE_SESSION_ID": "c1", "LLM_SCRIPTING_KIT_USAGE_SESSION": "u1"}),
        ):
            out[label] = s.usage.session_key(env)
        out["process_env"] = s.usage.session_key()
        return out

    got = view(ok(par(scenario)))
    assert got["override"] == "u1" and got["claude"] == "c1" and got["none"] is None


def test_pinned_evaluate_pins_for_the_session_and_stores_legacy_bytes(par):
    def scenario(ctx):
        s = ctx.side
        snap = ctx.work / "rl.json"
        ctx.patch(s.usage, "CLAUDE_SNAPSHOT", snap)
        cache = ctx.work / "pinned" / "usage-verdicts.json"
        env = {"CLAUDE_CODE_SESSION_ID": "s-1"}
        seven = s.conserve.ConserveSpec(pool="seven_day")
        out = {}
        _write_claude_snapshot(snap, used=10, resets_in=_WEEK / 2)
        out["first"] = s.usage.pinned_evaluate("opus", seven, "claude", now=_PACING_NOW,
                                               cache_path=cache, environ=env)
        # The pool is now spent, but the AVAILABLE verdict stays pinned: the
        # design's stated stale-AVAILABLE cost.
        _write_claude_snapshot(snap, used=100, resets_in=_WEEK / 2)
        out["pinned_available"] = s.usage.pinned_evaluate("opus", seven, "claude", now=_PACING_NOW + 5,
                                                          cache_path=cache, environ=env)
        out["file"] = cache.read_text()
        # A different spec for the same id recomputes.
        out["other_spec"] = s.usage.pinned_evaluate(
            "opus", s.conserve.ConserveSpec(pool="five_hour"), "claude", now=_PACING_NOW,
            cache_path=cache, environ=env)
        # A different session key discards the stored verdicts.
        out["new_session"] = s.usage.pinned_evaluate("opus", seven, "claude", now=_PACING_NOW + 5,
                                                     cache_path=cache, environ={"CLAUDE_CODE_SESSION_ID": "s-2"})
        # No key: nothing is pinned and nothing is written.
        before = cache.read_text()
        out["keyless"] = s.usage.pinned_evaluate("opus", seven, "claude", now=_PACING_NOW, cache_path=cache,
                                                 environ={})
        out["keyless_wrote"] = cache.read_text() != before
        return out

    got = view(ok(par(scenario)))
    assert got["pinned_available"]["status"] == "available"
    assert got["keyless_wrote"] is False
    assert got["file"].startswith('{"session_key": "s-1"')


def test_pinned_evaluate_recomputes_expired_deprioritized_and_normalises_floats(par):
    def scenario(ctx):
        s = ctx.side
        snap = ctx.work / "rl.json"
        ctx.patch(s.usage, "CLAUDE_SNAPSHOT", snap)
        cache = ctx.work / "c.json"
        env = {"LLM_SCRIPTING_KIT_USAGE_SESSION": "exp"}
        seven = s.conserve.ConserveSpec(pool="seven_day")
        out = {}
        _write_claude_snapshot(snap, used=90, resets_in=_WEEK / 2)
        out["under"] = s.usage.pinned_evaluate("o", seven, "claude", now=_PACING_NOW, cache_path=cache, environ=env)
        _write_claude_snapshot(snap, used=5, resets_in=_WEEK / 2)
        # Still inside the stored window: pinned UNDER-QUOTA.
        out["still_under"] = s.usage.pinned_evaluate("o", seven, "claude", now=_PACING_NOW + 10,
                                                     cache_path=cache, environ=env)
        # After the stored window reset: recomputed, which can only restore capacity.
        _write_claude_snapshot(snap, used=5, resets_in=_WEEK / 2 + 400000)
        out["after_reset"] = s.usage.pinned_evaluate("o", seven, "claude", now=_PACING_NOW + _WEEK,
                                                     cache_path=cache, environ=env)
        # A float resets_at in the stored file is normalised once.
        cache.write_text(json.dumps({"session_key": "exp", "verdicts": {"f": {
            "spec": seven.to_json(), "budget": {"status": "under-quota", "pool": "seven_day",
                                                "detail": "d", "remaining": 0.1, "window_remaining": 0.5,
                                                "resets_at": _PACING_NOW + 99.7}}}}))
        out["float"] = s.usage.pinned_evaluate("f", seven, "claude", now=_PACING_NOW, cache_path=cache, environ=env)
        cache.write_text("not json")
        out["corrupt"] = s.usage.pinned_evaluate("f", seven, "claude", now=_PACING_NOW, cache_path=cache, environ=env)
        cache.write_text(json.dumps({"session_key": "exp", "verdicts": {"f": {"spec": seven.to_json(),
                                                                              "budget": {"status": "bogus"}}}}))
        out["bogus_status"] = s.usage.pinned_evaluate("f", seven, "claude", now=_PACING_NOW, cache_path=cache,
                                                      environ=env)
        return out

    got = view(ok(par(scenario)))
    assert got["still_under"]["status"] == "under-quota" and got["float"]["resets_at"] == int(_PACING_NOW + 99.7)


def test_record_observed_halt_spends_the_whole_pool_and_latches(par):
    def scenario(ctx):
        s = ctx.side
        cache = ctx.work / "h" / "usage-verdicts.json"
        env = {"CLAUDE_CODE_SESSION_ID": "halt-sess"}
        seven = s.conserve.ConserveSpec(pool="seven_day")
        fable = s.conserve.ConserveSpec(pool="model_scoped", display_name="Fable")
        entries = {
            "opus": H(s, "opus", paced=True), "sonnet": H(s, "sonnet", paced=True),
            "fable": H(s, "fable", spec=fable), "sol": H(s, "sol", "codex", paced=True),
            "haiku": H(s, "haiku"),
        }
        out = {}
        out["keyless"] = s.usage.record_observed_halt("opus", seven, entries=entries, environ={})
        out["pool"] = s.usage.record_observed_halt("opus", seven, entries=entries, resets_at=RESET.__int__(),
                                                   now=_PACING_NOW, cache_path=cache, environ=env)
        out["latched"] = s.usage.record_observed_halt("sol", seven, entries=None, now=_PACING_NOW,
                                                      cache_path=cache, environ=env)
        out["file"] = cache.read_text()
        out["keys"] = {k: s.usage.quota_pool_key(getattr(e, "harness", None), e.conserve_usage)
                       for k, e in entries.items()}
        out["no_spec"] = s.usage.quota_pool_key("claude", None)
        out["no_harness"] = s.usage.quota_pool_key("  ", seven)
        return out

    got = view(ok(par(scenario)))
    stored = json.loads(got["file"])["verdicts"]
    assert set(stored) == {"opus", "sonnet", "sol"}
    assert stored["sonnet"]["budget"]["detail"].endswith("which shares this pool")
    assert stored["sol"]["budget"]["resets_at"] == int(_PACING_NOW + 5 * 3600)


def test_verdict_cache_path_env_names_and_reader_forwarding(sides):
    """Legacy path and env spelling survive; reader names forward to usage_sources."""
    legacy, final = sides
    assert final.usage.VERDICT_CACHE.parts[-6:] == legacy.usage.VERDICT_CACHE.parts[-6:]
    assert final.usage.VERDICT_CACHE.name == "usage-verdicts.json"
    assert final.usage.VERDICT_CACHE.parent.name == "llm-scripting-kit"
    assert "LLM_SCRIPTING_KIT_USAGE_SESSION" in Path(final.usage.__file__).read_text(encoding="utf-8")
    # Forwarding: set, read and delete a reader name through the policy module.
    sentinel = object()
    original = final.sources.read_codex_pool
    final.usage.read_codex_pool = sentinel
    try:
        assert final.sources.read_codex_pool is sentinel
        assert final.usage.read_codex_pool is sentinel
    finally:
        final.usage.read_codex_pool = original
    assert final.sources.read_codex_pool is original
    with pytest.raises(AttributeError):
        final.usage.no_such_name  # noqa: B018


def test_verdict_cache_default_path_resolves_under_home(tmp_path):
    """HOME-relative: a child interpreter with HOME and USERPROFILE exported to tmp."""
    code = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(_LEGACY_LIB)!r}); sys.path.insert(1, {str(_REPO / 'packages' / 'katsoft-llm-kit' / 'src')!r})\n"
        f"sys.path.insert(2, {str(_REPO / 'plugins' / 'bootstrap')!r})\n"
        "from llm_scripting_kit import usage_budget as legacy\n"
        "from llm_kit.dispatch import usage_budget as final\n"
        "print(json.dumps([str(legacy.VERDICT_CACHE), str(final.VERDICT_CACHE)]))\n"
    )
    home = tmp_path / "h"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "_BOOTSTRAP_GUARD_SKIP_REEXEC": "1"}
    proc = subprocess.run([sys.executable, "-I", "-c", code], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    legacy_path, final_path = json.loads(proc.stdout.splitlines()[-1])
    assert legacy_path == final_path
    assert Path(final_path) == home / ".claude" / "plugins" / "data" / "plugins-kit" / "llm-scripting-kit" / "usage-verdicts.json"


# --------------------------------------------------------------------------
# factory (completion/factory -> dispatch.factory)
# --------------------------------------------------------------------------


def test_create_backend_and_transport_backend(par):
    def scenario(ctx):
        s = ctx.side
        config = {"endpoints": {
            "h-claude": {"harness": "claude", "model": "m-c", "effort": "high"},
            "h-codex": {"harness": "codex", "model": "m-x"},
            "h-oc": {"harness": "OpenCode", "model": "m-o", "effort": "low"},
            "h-bad": {"harness": "zed", "model": "m-z"},
            "h-nomodel": {"harness": "claude"},
            "h-badeffort": {"harness": "claude", "model": "m", "effort": " "},
        }}
        ctx.patch(s.factory, "load_model_config", lambda project_root=None: config)
        ctx.patch(s.factory, "default_endpoint_name", lambda cfg: "h-claude")
        ctx.patch(s.factory, "discover_model_entries", lambda **kw: {"t-entry": T(s, "t-entry")})
        ctx.patch(s.factory, "resolve_endpoint", lambda name, **kw: {
            "name": name, "base_url": "http://x.invalid/v1", "request_defaults": {"reasoning_effort": "high"},
            "effort_style": "top-level", "key_env": None, "models": {}, "default": "d"})

        def resolve_model(model, *, cheap, config, endpoint, project_root):
            if model == "bogus":
                raise s.models.ModelResolveError("no such model")
            return f"resolved:{model}:{cheap}:{endpoint}"

        ctx.patch(s.factory, "resolve_model", resolve_model)
        out = {}
        for label, call in (
            ("default", lambda: s.factory.create_backend()),
            ("claude", lambda: s.factory.create_backend("h-claude", model="override")),
            ("codex", lambda: s.factory.create_backend("h-codex", project_root="/p")),
            ("opencode", lambda: s.factory.create_backend("h-oc")),
            ("bad_harness", lambda: s.factory.create_backend("h-bad")),
            ("no_model", lambda: s.factory.create_backend("h-nomodel")),
            ("bad_effort", lambda: s.factory.create_backend("h-badeffort")),
            ("transport", lambda: s.factory.create_backend("t-entry", cheap=True, project_root="/q")),
            ("transport_only_harness", lambda: s.factory.create_transport_backend("h-claude")),
            ("transport_only_ok", lambda: s.factory.create_transport_backend("t-entry", model="x")),
            ("transport_bad_model", lambda: s.factory.create_transport_backend("t-entry", model="bogus")),
            ("create_bad_model", lambda: s.factory.create_backend("t-entry", model="bogus")),
        ):
            out[label] = outcome(call)
        return out

    got = view(ok(par(scenario)))
    assert got["codex"][1]["backend"]["__type__"] == "CodexCliBackend"
    assert got["bad_harness"][0] == got["no_model"][0] == got["transport_only_harness"][0] == "raise"
    assert got["transport_bad_model"][3] is not None  # the seam's ModelResolveError is chained


def test_endpoint_resolver_over_the_configured_factory_equals_create_backend(par):
    """``EndpointResolver(ConfiguredBackendFactory())`` selects exactly what legacy ``create_backend`` does.

    The resolver is jobs' seam over an injected ``BackendFactory``: same registry
    resolution, model, effort and capability choices, same constructed backends,
    same errors, but built through ``factory.create(BackendSpec)``.
    """

    def scenario(ctx):
        s = ctx.side
        config = {"endpoints": {
            "h-claude": {"harness": "claude", "model": "m-c", "effort": "high"},
            "h-codex": {"harness": "codex", "model": "m-x"},
            "h-oc": {"harness": "OpenCode", "model": "m-o", "effort": "low"},
            "h-bad": {"harness": "zed", "model": "m-z"},
            "h-nomodel": {"harness": "claude"},
            "h-badeffort": {"harness": "claude", "model": "m", "effort": " "},
        }}
        resolved = {
            "t-entry": {"name": "t-entry", "base_url": "http://x.invalid/v1",
                        "request_defaults": {"reasoning_effort": "high"}, "effort_style": "top-level",
                        "effort_style_source": "endpoint"},
            "t-plain": {"name": "t-plain", "base_url": "http://x.invalid/v1", "request_defaults": {}},
        }

        def resolve_endpoint(name, **kw):
            if name not in resolved:
                raise s.models.EndpointResolveError(f"no endpoint {name!r}")
            return dict(resolved[name])

        def resolve_model(model, *, cheap, config, endpoint, project_root):
            if model == "bogus":
                raise s.models.ModelResolveError("no such model")
            return f"resolved:{model}:{cheap}:{endpoint}"

        modules = [s.factory]
        if s.name == "final":
            modules.append(importlib.import_module("llm_kit.dispatch.endpoint_resolver"))
        for module in modules:
            for attr, value in (
                ("load_model_config", lambda project_root=None: config),
                ("default_endpoint_name", lambda cfg: "h-claude"),
                ("discover_model_entries", lambda **kw: {"t-entry": T(s, "t-entry"), "t-plain": T(s, "t-plain")}),
                ("resolve_endpoint", resolve_endpoint),
                ("resolve_model", resolve_model),
            ):
                if hasattr(module, attr):
                    ctx.patch(module, attr, value)
        if s.name == "legacy":
            build = s.factory.create_backend
        else:
            from llm_kit.dispatch.endpoint_resolver import EndpointResolver  # noqa: PLC0415
            from llm_kit.dispatch.pipeline_backends import ConfiguredBackendFactory  # noqa: PLC0415

            build = EndpointResolver(ConfiguredBackendFactory())
        out = {}
        for label, call in (
            ("default", lambda: build()),
            ("claude", lambda: build("h-claude", model="override")),
            ("codex", lambda: build("h-codex", project_root="/p")),
            ("opencode", lambda: build("h-oc")),
            ("bad_harness", lambda: build("h-bad")),
            ("no_model", lambda: build("h-nomodel")),
            ("bad_effort", lambda: build("h-badeffort")),
            ("transport", lambda: build("t-entry", cheap=True, project_root="/q")),
            ("transport_plain", lambda: build("t-plain")),
            ("unknown", lambda: build("nope")),
            ("bad_model", lambda: build("t-entry", model="bogus")),
        ):
            out[label] = outcome(call)
        return out

    got = view(ok(par(scenario)))
    assert got["codex"][1]["backend"]["__type__"] == "CodexCliBackend"
    assert got["transport"][1]["backend"]["__type__"] == "OpenRouterBackend"
    assert got["bad_harness"][0] == got["no_model"][0] == got["unknown"][0] == got["bad_model"][0] == "raise"


# --------------------------------------------------------------------------
# review_lane
# --------------------------------------------------------------------------

_LANE = "reviewer_b_diff_only_bugs"
_LANE_AGENT = "reviewer_a_claude_md_compliance"


def _lane_world(ctx, *, backend, harness="claude", kind=None, capabilities=None, window=None):
    s = ctx.side
    entry = (H(s, "lane-ep", harness) if kind != "transport" else T(s, "lane-ep"))
    if window is not None:
        entry = dataclasses.replace(entry, context_window=window)
    entries = {"lane-ep": entry}
    ctx.patch(s.models, "discover_model_entries", lambda **kw: entries)
    ctx.patch(s.review, "discover_model_entries", lambda **kw: entries)
    ctx.patch(s.decl, "check_many", lambda to_probe, **kw: {n: R(s) for n in to_probe})
    _quota(ctx)
    caps = capabilities or (s.acap.CLAUDE_CAPABILITIES if harness == "claude" else s.acap.CODEX_CAPABILITIES)
    selected_kind = entry.kind

    def create_backend(name, **kw):
        if name != "lane-ep":
            raise s.models.EndpointResolveError(f"unknown endpoint '{name}'")
        return s.factory.BackendSelection(
            endpoint=name, kind=selected_kind, backend=backend, model="lane-model", effort=None,
            capabilities=caps,
        )

    ctx.patch(s.review, "create_backend", create_backend)
    return entry


_ISSUES = json.dumps([{"file": "a.py", "lines": "3", "reason": "bug", "description": "d"}])


class _LaneBackend:
    def __init__(self, outcomes, name="claude-cli"):
        self.name = name
        self.outcomes = list(outcomes)
        self.requests = []

    def complete(self, system, user, *, model, options=None):
        self.requests.append((system, user, model, norm(options)))
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out

    def classify_halt(self, exc):
        return None


def test_run_lane_builds_the_request_and_the_envelope(par):
    def scenario(ctx):
        s = ctx.side
        out = {}
        for label, lane, effort, outcomes in (
            ("plain", _LANE, "high", [types.SimpleNamespace(text=_ISSUES, model="served", input_tokens=5,
                                                            output_tokens=7, dropped_params=(),
                                                            finish_reason="stop")]),
            ("agent_loop", _LANE_AGENT, "high", [types.SimpleNamespace(text="[]", model=None, input_tokens=1,
                                                                       output_tokens=1, dropped_params=())]),
            ("repair", _LANE, "high", [types.SimpleNamespace(text="nope", model="m", input_tokens=1,
                                                             output_tokens=1, dropped_params=(),
                                                             finish_reason="length"),
                                       types.SimpleNamespace(text="still nope", model="m", input_tokens=1,
                                                             output_tokens=1, dropped_params=(),
                                                             finish_reason="length")]),
            ("repaired", _LANE, "high", [types.SimpleNamespace(text="nope", model="m", input_tokens=1,
                                                               output_tokens=1, dropped_params=()),
                                         types.SimpleNamespace(text=_ISSUES, model="m", input_tokens=1,
                                                               output_tokens=1, dropped_params=())]),
            ("dropped", _LANE, "high", [types.SimpleNamespace(text=_ISSUES, model="m", input_tokens=1,
                                                              output_tokens=1, dropped_params=("effort",))]),
            ("halt", _LANE, "high", [s.halt.HaltError("quota", "limit hit")]),
            ("boom", _LANE, "high", [RuntimeError("kaboom")]),
            ("timeout", _LANE, "high", [s.halt.AgentTimeoutError("deadline", cmd=["claude"], elapsed_s=12.0, stdout="", stderr="")]),
        ):
            backend = _LaneBackend(outcomes)
            with contextlib.ExitStack() as stack:
                sub = Ctx(s, ctx.work, stack)
                _lane_world(sub, backend=backend)
                res = outcome(lambda: s.review.run_lane(
                    lane=lane, model="lane-ep", effort=effort, diff_text="diff --git a/a.py b/a.py\n+x = 1\n",
                    files=["a.py"], description="change", claimed_files=["z.py"],
                    mechanical_findings=[{"file": "a.py", "line": 1, "check": "c", "text": "t"}],
                    claude_mds_by_file={"a.py": ["CLAUDE.md"]}, project_root="/proj", timeout_s=12.0,
                ))
            out[label] = (res, backend.requests)
        return out

    got = view(ok(par(scenario)))
    assert got["plain"][0][0] == "ok" and got["plain"][0][1]["attempts"] == 1
    assert len(got["repair"][1]) == 2 and got["timeout"][0][0] == "raise"


def test_run_lane_refusals(par):
    def scenario(ctx):
        s = ctx.side
        out = {}
        resp = types.SimpleNamespace(text="[]", model="m", input_tokens=1, output_tokens=1, dropped_params=())
        cases = {
            "agent_alias": dict(lane=_LANE, model="opus", effort="high"),
            "unknown_lane": dict(lane="nope", model="lane-ep", effort="high"),
            "ineligible": dict(lane="validator", model="lane-ep", effort="high"),
            "blank_effort": dict(lane=_LANE, model="lane-ep", effort=" "),
            "typo": dict(lane=_LANE, model="typo", effort="high"),
            "codex_effort": dict(lane=_LANE, model="lane-ep", effort="turbo", harness="codex",
                                 backend_name="codex-cli"),
            "agent_loop_on_transport": dict(lane=_LANE_AGENT, model="lane-ep", effort="high", kind="transport"),
            "window": dict(lane=_LANE, model="lane-ep", effort="high", window=10),
            "no_effort_cap": dict(lane=_LANE, model="lane-ep", effort="high", caps="none"),
        }
        for label, spec in cases.items():
            spec = dict(spec)
            harness = spec.pop("harness", "claude")
            backend_name = spec.pop("backend_name", "claude-cli")
            kind = spec.pop("kind", None)
            window = spec.pop("window", None)
            caps = spec.pop("caps", None)
            if caps == "none":
                caps = dataclasses.replace(s.acap.CLAUDE_CAPABILITIES, params={})
            with contextlib.ExitStack() as stack:
                sub = Ctx(s, ctx.work, stack)
                _lane_world(sub, backend=_LaneBackend([resp], backend_name), harness=harness, kind=kind,
                            window=window, capabilities=caps)
                out[label] = outcome(lambda: s.review.run_lane(
                    diff_text="d" * 200, project_root=None, **spec))
        # A transport lane needs the openai SDK; with it absent the lane refuses by name.
        with contextlib.ExitStack() as stack:
            sub = Ctx(s, ctx.work, stack)
            _lane_world(sub, backend=_LaneBackend([resp], "openrouter"), kind="transport")
            with mock.patch.dict(sys.modules, {"openai": None}):
                out["no_sdk"] = outcome(lambda: s.review.run_lane(
                    lane=_LANE, model="lane-ep", effort="high", diff_text="d"))
        return out

    got = view(ok(par(scenario)))
    for label in ("agent_alias", "unknown_lane", "ineligible", "blank_effort", "typo", "codex_effort",
                  "agent_loop_on_transport", "window", "no_effort_cap", "no_sdk"):
        assert got[label][0] == "raise", label


def test_review_lane_chunk_index_derivation(par):
    def scenario(ctx):
        s = ctx.side
        bundle = {
            "bundle_dir": "/b", "description": "desc", "project_root": "/proj",
            "claimed_files": [{"identifier": "x.py"}, {"identifier": "y.py"}],
            "diff_chunks": [{"path": "chunk0.diff", "files": ["a.py", "b.py"],
                             "mechanical_scan": {"files": [{"file": "a.py"}]}}, {"path": "c1.diff"}],
        }
        out = {}
        for lane in ("reviewer_a_claude_md_compliance", "reviewer_b_diff_only_bugs",
                     "reviewer_c_introduced_code", "validator"):
            out[lane] = outcome(lambda lane=lane: s.review.derive_from_chunk_index(bundle, 0, lane))
        out["second"] = outcome(lambda: s.review.derive_from_chunk_index(bundle, 1, _LANE))
        for label, bad in (
            ("not_list", {**bundle, "diff_chunks": "x"}), ("empty", {**bundle, "diff_chunks": []}),
            ("no_dir", {**bundle, "bundle_dir": ""}),
            ("no_path", {**bundle, "diff_chunks": [{"files": []}]}),
            ("bad_files", {**bundle, "diff_chunks": [{"path": "p", "files": "a"}]}),
            ("bad_claim", {**bundle, "claimed_files": [{"identifier": 3}]}),
            ("bad_scan", {**bundle, "diff_chunks": [{"path": "p", "mechanical_scan": {"files": 3}}]}),
            ("bad_desc", {**bundle, "description": 3}), ("bad_root", {**bundle, "project_root": 3}),
            ("not_mapping", {**bundle, "diff_chunks": [3]}),
        ):
            out[label] = outcome(lambda bad=bad: s.review.derive_from_chunk_index(bad, 0, _LANE))
        out["range"] = outcome(lambda: s.review.derive_from_chunk_index(bundle, 9, _LANE))
        return out

    ok(par(scenario))


def test_review_lane_cli_parsing_and_main(par, capsys):
    def scenario(ctx):
        s = ctx.side
        out = {}
        for label, argv in (
            ("no_chunk", ["--lane", _LANE, "--model", "m", "--effort", "high"]),
            ("index_without_bundle", ["--lane", _LANE, "--model", "m", "--effort", "high", "--chunk-index", "0"]),
            ("conflict", ["--lane", _LANE, "--model", "m", "--effort", "high", "--chunk-index", "0",
                          "--bundle", "b.json", "--file", "a.py"]),
        ):
            try:
                s.review._parse_args(argv)
            except SystemExit as exc:
                out[label] = ("exit", exc.code)
        args = s.review._parse_args(["--lane", _LANE, "--model", "m", "--effort", "high", "--chunk", "c.diff",
                                     "--mechanical-finding", '{"a": 1}', "--timeout", "3"])
        out["args"] = vars(args)
        bare = s.review._parse_args(["--lane", _LANE, "--model", "m", "--effort", "high", "--chunk", "c.diff"])
        out["bare_findings"] = bare.mechanical_findings
        out["consts"] = (s.review.EXIT_OK, s.review.EXIT_LANE_FAILED, s.review.EXIT_USAGE,
                         s.review.DEFAULT_TIMEOUT_S, s.review.REVIEW_TEMPERATURE,
                         s.review.DEFAULT_MAX_OUTPUT_TOKENS, s.review.CHARS_PER_TOKEN,
                         s.review.AGENT_LOOP_TOOLS)
        chunk = ctx.work / "c.diff"
        chunk.write_text("diff --git a/a.py b/a.py\n+x\n")
        backend = _LaneBackend([types.SimpleNamespace(text=_ISSUES, model="m", input_tokens=1, output_tokens=2,
                                                      dropped_params=())])
        with contextlib.ExitStack() as stack:
            sub = Ctx(s, ctx.work, stack)
            _lane_world(sub, backend=backend)
            code = s.review.main(["--lane", _LANE, "--model", "lane-ep", "--effort", "high",
                                  "--chunk", str(chunk), "--file", "a.py"])
        out["main_code"] = code
        out["requests"] = backend.requests
        with contextlib.ExitStack() as stack:
            sub = Ctx(s, ctx.work, stack)
            _lane_world(sub, backend=_LaneBackend([]))
            out["main_missing_model"] = s.review.main(["--lane", _LANE, "--model", "typo", "--effort", "high",
                                                       "--chunk", str(chunk)])
        return out

    # main() prints the envelope (with wall_ms) to stdout; stderr and files are compared by `par`.
    ok(par(scenario))


# --------------------------------------------------------------------------
# construction builder vs the legacy content_pipeline wrappers
# --------------------------------------------------------------------------

_SPEC_FIELDS = (
    "endpoint", "project_root", "client", "default_timeout_s", "retry_max_attempts", "retry_cooldown_s",
    "diagnostics_dir", "executable", "argv_prefix", "runner",
)


def _my_runner(*a, **k):  # a distinctive runner callable
    return "", "", 0


_CLIENT = object()

# (wrapper class name, wrapper constructor kwargs)
_WRAPPER_CASES = [
    ("OpenRouterBackend", {}),
    ("OpenRouterBackend", {"endpoint": "e1"}),
    ("OpenRouterBackend", {"endpoint": "e1", "project_root": Path("/proj"), "client": _CLIENT}),
    ("ModelEndpointBackend", {"endpoint": "local"}),
    ("ModelEndpointBackend", {"endpoint": "local", "project_root": Path("/proj"), "client": _CLIENT}),
    ("ClaudeCliBackend", {}),
    ("ClaudeCliBackend", {"runner": _my_runner}),
    ("ClaudeCliBackend", {"default_timeout_s": 12.5, "retry_max_attempts": 3, "retry_cooldown_s": 1.5,
                          "diagnostics_dir": Path("/diag"), "executable": "/bin/claude", "runner": _my_runner}),
    ("ClaudeCliBackend", {"executable": "claude-x"}),
    ("CodexCliBackend", {}),
    ("CodexCliBackend", {"argv_prefix": ("codex", "--x")}),
    ("CodexCliBackend", {"default_timeout_s": 7.0, "argv_prefix": ("a",), "runner": _my_runner}),
    ("OpencodeCliBackend", {}),
    ("OpencodeCliBackend", {"argv_prefix": ("oc",), "runner": _my_runner}),
    ("OpencodeCliBackend", {"default_timeout_s": 33.0}),
]


class _Capture:
    """A stand-in concrete constructor that records the exact keywords it is called with."""

    def __init__(self, label):
        self.label = label
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(
            name=self.label, kwargs=kwargs, classify_halt=lambda exc: None,
        )


def _spec_from_wrapper(final, wrapper):
    values = {
        f.name: getattr(wrapper, f.name)
        for f in dataclasses.fields(wrapper)
        if f.init and f.name in _SPEC_FIELDS
    }
    return final.BackendSpec(backend=wrapper.name, **values)


@pytest.mark.parametrize("wrapper_name,kwargs", _WRAPPER_CASES, ids=lambda v: v if isinstance(v, str) else "")
def test_construction_builder_matches_legacy_constructor_calls(sides, pipeline_backends, wrapper_name, kwargs):
    legacy, final = sides
    import llm_scripting_kit.completion as legacy_completion  # noqa: PLC0415 -- already loaded
    from llm_kit.dispatch import pipeline_backends as pb  # noqa: PLC0415

    wrapper = getattr(pipeline_backends, wrapper_name)(**kwargs)
    captures = {n: _Capture(n) for n in ("OpenRouterBackend", "ClaudeCliBackend", "CodexCliBackend",
                                         "OpencodeCliBackend")}
    with contextlib.ExitStack() as stack:
        for n, cap in captures.items():
            stack.enter_context(mock.patch.object(legacy_completion, n, cap))
        assert all(not cap.calls for cap in captures.values())  # constructing the wrapper builds nothing
        wrapper._backend()
    legacy_calls = {n: cap.calls for n, cap in captures.items() if cap.calls}
    assert len(legacy_calls) == 1

    classes = {
        "openrouter": _Capture("OpenRouterBackend"), "model-endpoint": None, "claude-cli": _Capture("ClaudeCliBackend"),
        "codex-cli": _Capture("CodexCliBackend"), "opencode-cli": _Capture("OpencodeCliBackend"),
    }
    classes["model-endpoint"] = classes["openrouter"]
    factory = pb.ConfiguredBackendFactory(classes=classes)
    spec = _spec_from_wrapper(pb, wrapper)
    built = factory.create(spec)
    final_calls = {c.label: c.calls for c in classes.values() if c.calls}
    assert final_calls == legacy_calls
    assert built.kwargs == next(iter(legacy_calls.values()))[0]
    # The kwargs are what the legacy wrapper passed -- and `runner` is present only when it was given.
    (only_kwargs,) = next(iter(final_calls.values()))
    assert ("runner" in only_kwargs) == ("runner" in kwargs)
    if "runner" not in kwargs:
        assert "runner" not in legacy_calls[next(iter(legacy_calls))][0]


def test_omitted_runner_is_never_passed_as_none(sides):
    from llm_kit.dispatch import pipeline_backends as pb  # noqa: PLC0415

    for backend in ("claude-cli", "codex-cli", "opencode-cli"):
        spec = pb.BackendSpec(backend=backend)
        assert "runner" not in pb.construction_kwargs(spec)
        capture = _Capture("c")
        pb.ConfiguredBackendFactory(classes={backend: capture}).create(spec)
        assert capture.calls and all("runner" not in call for call in capture.calls)


def test_default_harness_instances_equal_legacy_create_backend(sides, par):
    def scenario(ctx):
        s = ctx.side
        config = {"endpoints": {
            "h-claude": {"harness": "claude", "model": "m"}, "h-codex": {"harness": "codex", "model": "m"},
            "h-oc": {"harness": "opencode", "model": "m"},
        }}
        ctx.patch(s.factory, "load_model_config", lambda project_root=None: config)
        ctx.patch(s.factory, "discover_model_entries", lambda **kw: {})
        ctx.patch(s.factory, "resolve_endpoint", lambda name, **kw: {"name": name, "base_url": "http://x/v1"})
        ctx.patch(s.factory, "resolve_model", lambda *a, **k: "m")
        legacy_made = {n: s.factory.create_backend(n).backend for n in ("h-claude", "h-codex", "h-oc")}
        legacy_made["openrouter"] = s.factory.create_backend("t-or").backend
        if s.name == "legacy":
            built = dict(legacy_made)
        else:
            from llm_kit.dispatch import pipeline_backends as pb  # noqa: PLC0415

            factory = pb.ConfiguredBackendFactory()
            built = {
                "h-claude": factory.create(pb.BackendSpec(backend="claude-cli")),
                "h-codex": factory.create(pb.BackendSpec(backend="codex-cli")),
                "h-oc": factory.create(pb.BackendSpec(backend="opencode-cli")),
                "openrouter": factory.create(pb.BackendSpec(backend="openrouter", endpoint="t-or")),
            }
        return {n: norm(b) for n, b in built.items()}, {n: norm(b) for n, b in legacy_made.items()}

    built, made = view(ok(par(scenario)))
    assert built == made and set(built) == {"h-claude", "h-codex", "h-oc", "openrouter"}


def test_builder_refuses_unknown_backends_and_stray_options(sides):
    from llm_kit.dispatch import pipeline_backends as pb  # noqa: PLC0415

    with pytest.raises(ValueError, match="unknown backend 'nope'"):
        pb.construction_kwargs(pb.BackendSpec(backend="nope"))
    with pytest.raises(ValueError, match="does not take argv_prefix"):
        pb.construction_kwargs(pb.BackendSpec(backend="claude-cli", argv_prefix=("x",)))
    with pytest.raises(ValueError, match="does not take executable"):
        pb.construction_kwargs(pb.BackendSpec(backend="openrouter", executable="x"))
    with pytest.raises(ValueError, match="non-empty backend name"):
        pb.BackendSpec(backend=" ")
    with pytest.raises(ValueError, match="no constructor injected for backend 'codex-cli'"):
        pb.ConfiguredBackendFactory(classes={"claude-cli": _Capture("c")}).create(
            pb.BackendSpec(backend="codex-cli"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        pb.BackendSpec(backend="claude-cli").backend = "x"  # type: ignore[misc]


def test_default_factory_maps_names_to_the_adapter_classes():
    from llm_kit.adapters import backends as adapters_backends  # noqa: PLC0415
    from llm_kit.adapters import codex_backend, opencode_backend  # noqa: PLC0415
    from llm_kit.completion import factory_protocols as fp  # noqa: PLC0415
    from llm_kit.dispatch import pipeline_backends as pb  # noqa: PLC0415

    factory = pb.ConfiguredBackendFactory()
    assert isinstance(factory, fp.BackendFactory) and isinstance(factory, fp.CapabilityProvider)
    built = {
        "openrouter": (factory.create(pb.BackendSpec("openrouter", endpoint="e")), adapters_backends.OpenRouterBackend),
        "model-endpoint": (factory.create(pb.BackendSpec("model-endpoint", endpoint="e")),
                           adapters_backends.OpenRouterBackend),
        "claude-cli": (factory.create(pb.BackendSpec("claude-cli", runner=_my_runner)), adapters_backends.ClaudeCliBackend),
        "codex-cli": (factory.create(pb.BackendSpec("codex-cli")), codex_backend.CodexCliBackend),
        "opencode-cli": (factory.create(pb.BackendSpec("opencode-cli")), opencode_backend.OpencodeCliBackend),
    }
    for name, (instance, cls) in built.items():
        assert type(instance) is cls, name
    assert built["claude-cli"][0].runner is _my_runner
    # An omitted runner leaves the adapter's own default runner in place.
    from llm_kit.adapters import claude_runner  # noqa: PLC0415

    assert factory.create(pb.BackendSpec("claude-cli")).runner is claude_runner.run_claude_streaming


def test_capability_provider_serves_the_legacy_advertisement(sides):
    legacy, final = sides
    from llm_kit.dispatch import pipeline_backends as pb  # noqa: PLC0415

    provider = pb.ConfiguredBackendFactory()
    assert norm(provider.capability_records()) == norm(legacy.acap.adapter_capabilities())
    assert norm(provider.capability_record("codex-cli")) == norm(legacy.acap.adapter_capabilities()["codex-cli"])
    assert provider.capability_record("model-endpoint") is None


def test_lazy_backend_builds_nothing_until_used_and_exactly_once(sides, pipeline_backends):
    legacy, _final = sides
    import llm_scripting_kit.completion as legacy_completion  # noqa: PLC0415
    from llm_kit.dispatch import pipeline_backends as pb  # noqa: PLC0415

    gate = threading.Barrier(16)

    def hammer(get):
        results = []

        def worker():
            gate.wait()
            results.append(get())

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

    legacy_cap = _Capture("ClaudeCliBackend")
    with mock.patch.object(legacy_completion, "ClaudeCliBackend", legacy_cap):
        wrapper = pipeline_backends.ClaudeCliBackend(runner=_my_runner)
        assert legacy_cap.calls == []
        legacy_results = hammer(wrapper._backend)

    final_cap = _Capture("ClaudeCliBackend")
    factory = pb.ConfiguredBackendFactory(classes={"claude-cli": final_cap})
    lazy = pb.LazyBackend(pb.BackendSpec("claude-cli", runner=_my_runner), factory)
    assert final_cap.calls == []
    final_results = hammer(lazy.get)
    assert len(legacy_cap.calls) == len(final_cap.calls) == 1
    assert len({id(r) for r in legacy_results}) == len({id(r) for r in final_results}) == 1
    assert pb.build_backend(pb.BackendSpec("claude-cli"), factory) is not None


# --------------------------------------------------------------------------
# module surfaces
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attr,extra",
    [
        ("decl", {"DISPOSITION_SWITCHED_OFF"}), ("observer", set()), ("seats", set()),
        ("usage", set()),
        ("reach", {"EndpointProbe", "probe_model_endpoint"}),
    ],
)
def test_public_surface_matches_legacy(sides, attr, extra):
    legacy, final = sides
    assert set(getattr(final, attr).__all__) == set(getattr(legacy, attr).__all__) | extra


# Names a legacy module holds only as the imports its moved code needed.
_LEGACY_ONLY: dict = {
    "decl": set(), "reach": set(), "seats": set(), "review": set(), "factory": set(),
    # stdlib imports only the moved raw readers used
    "usage": {"dataclass", "datetime", "re"},
    "observer": set(),
}


@pytest.mark.parametrize("attr", ["decl", "reach", "seats", "usage", "review", "factory", "observer"])
def test_module_level_names_match_legacy(sides, attr):
    """Every name a legacy test could patch or read on the module exists on the final one."""
    legacy, final = sides
    lnames = {n for n in dir(getattr(legacy, attr)) if not n.startswith("__")}
    fnames = {n for n in dir(getattr(final, attr)) if not n.startswith("__")}
    missing = lnames - fnames
    assert missing <= _LEGACY_ONLY[attr], sorted(missing - _LEGACY_ONLY[attr])


def test_dispatch_endpoint_profile_reexports_the_adapter_objects():
    from llm_kit.adapters import endpoint_profile as owner  # noqa: PLC0415
    from llm_kit.dispatch import endpoint_profile as policy  # noqa: PLC0415

    for name in owner.__all__:
        assert getattr(policy, name) is getattr(owner, name), name
    assert set(policy.__all__) == set(owner.__all__)
