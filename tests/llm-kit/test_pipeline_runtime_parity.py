"""Differential parity: llm_kit.pipeline.runtime vs the legacy content_pipeline modules.

The legacy side is ``content_pipeline.llm.{platform, backends, gate,
spend_ledger, yaml_extract}`` and ``content_pipeline.cli.budget`` (with
``llm_scripting_kit`` behind them), imported from plugins/content-pipeline-kit/lib and
plugins/llm-scripting-kit/lib under a sys.path insert scoped to this module.
Every scenario is a function of a "side" (legacy or final) and runs on both
with the same inputs; the test asserts the two outcomes are equal -- results,
exception MRO names, messages and attributes, and stderr.

Nothing here makes a network call or starts a real CLI: completion backends are
fakes (the legacy side patches the ``llm_scripting_kit.completion`` classes the
wrappers construct, the final side injects a ``BackendFactory``), the routing
declaration is a fake, and HOME / USERPROFILE point at a temp directory.

Covered (legacy -> final):

* llm/platform      -> pipeline.runtime.platform
* llm/backends      -> pipeline.runtime.backends (wrapper slice)
* llm/gate          -> pipeline.runtime.gate
* llm/spend_ledger  -> pipeline.runtime.spend_ledger (storage plumbing)
* llm/yaml_extract  -> pipeline.runtime.yaml_extract
* cli/budget        -> pipeline.runtime.budget
* llm/__init__      -> pipeline.runtime (package re-exports)

Deliberate differences, each pinned by its own named test:

* a wrapper takes an optional ``factory`` field; the legacy wrapper has none
  (``test_wrappers_final_only_surface``);
* a ``None`` timeout or retry option is omitted from the concrete constructor,
  so the adapter default applies; legacy forwarded the ``None``
  (``test_none_timeout_and_retry_mean_adapter_default``);
* with no declaration and no configured ``default_endpoint``, ``route`` and ``routed_model``
  raise ``ConfigurationError`` before any backend runs; the legacy pair fell back to the
  OpenRouter entry (no shipped default, L3; ``test_no_declaration_is_refused_not_defaulted``,
  ``test_blank_removed_env_is_ignored_then_refused``, ``test_model_endpoint_routed_model_needs_a_declaration``);
* the "shared lib absent / too old" translations of the contract seam, the
  declaration probe and the effort seam are gone: the neutral types are in
  ``llm_kit.completion`` and cannot be absent from the same package.
"""

from __future__ import annotations

import contextlib
import dataclasses
import enum
import importlib
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import types
import warnings
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

import pytest

from final_wording import floors_in_text_to_final, max_tokens_default_to_final, to_final

_REPO = Path(__file__).resolve().parents[2]
_LSK_LIB = _REPO / "plugins" / "llm-scripting-kit" / "lib"
_CPK_LIB = _REPO / "plugins" / "content-pipeline-kit" / "lib"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "spend_schema"

_LEGACY_ROOTS = ("content_pipeline", "llm_scripting_kit")


# --------------------------------------------------------------------------
# Loading both sides
# --------------------------------------------------------------------------


def _side(name: str, mods: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(name=name, **mods)


@pytest.fixture(scope="module")
def sides():
    saved_path = list(sys.path)
    before = set(sys.modules)
    sys.path.insert(0, str(_LSK_LIB))
    sys.path.insert(0, str(_CPK_LIB))
    imp = importlib.import_module
    try:
        legacy = _side(
            "legacy",
            dict(
                pkg=imp("content_pipeline.llm"),
                platform=imp("content_pipeline.llm.platform"),
                backends=imp("content_pipeline.llm.backends"),
                gate=imp("content_pipeline.llm.gate"),
                spend=imp("content_pipeline.llm.spend_ledger"),
                yaml_extract=imp("content_pipeline.llm.yaml_extract"),
                budget=imp("content_pipeline.cli.budget"),
                vc=imp("content_pipeline.validate.contract"),
                ctypes=imp("llm_scripting_kit.completion.contract_types"),
                halt=imp("llm_scripting_kit.completion.halt"),
                ccomp=imp("llm_scripting_kit.completion"),
                reg_entry=imp("llm_scripting_kit.model_endpoints"),
                models=imp("llm_scripting_kit.models"),
                account=imp("llm_scripting_kit.account"),
                halt_path="llm_scripting_kit.completion.halt",
            ),
        )
        final = _side(
            "final",
            dict(
                pkg=imp("llm_kit.pipeline.runtime"),
                platform=imp("llm_kit.pipeline.runtime.platform"),
                backends=imp("llm_kit.pipeline.runtime.backends"),
                gate=imp("llm_kit.pipeline.runtime.gate"),
                spend=imp("llm_kit.pipeline.runtime.spend_ledger"),
                yaml_extract=imp("llm_kit.pipeline.runtime.yaml_extract"),
                budget=imp("llm_kit.pipeline.runtime.budget"),
                vc=imp("llm_kit.pipeline.core.validate.contract"),
                ctypes=imp("llm_kit.completion.contract_types"),
                halt=imp("llm_kit.completion.halt"),
                ccomp=None,
                reg_entry=imp("llm_kit.registry.model_endpoints"),
                models=imp("llm_kit.registry.models"),
                account=imp("llm_kit.dispatch.reachability"),
                halt_path="llm_kit.completion.halt",
            ),
        )
        yield legacy, final
    finally:
        sys.path[:] = saved_path
        for mod in set(sys.modules) - before:
            if mod.split(".")[0] in _LEGACY_ROOTS:
                del sys.modules[mod]


# --------------------------------------------------------------------------
# Outcome normalisation
# --------------------------------------------------------------------------

_MASKED_FIELDS = frozenset({
    "wall_ms", "started_at", "ended_at", "created_at", "lease_expires_at",
    "as_of", "swept_at", "resolved_at", "since", "presented_at", "at",
})
# Fields one side's wrapper has and the other does not (named, pinned below).
# ``factory``: the final wrappers take an optional factory. ``_cap_endpoint``: the final
# transport wrappers cache the endpoint they resolve their default output cap from.
_FINAL_ONLY_FIELDS = frozenset({"factory", "_cap_endpoint"})
_ID_RE = re.compile(r"\b[0-9a-f]{32}\b")
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\+00:00|Z)?")


def _text(value: str) -> str:
    # named deliberate wording differences (final_wording.RENAMES); idempotent
    return to_final(_TS_RE.sub("<ts>", _ID_RE.sub("<id>", value)))


class _Out(tuple):
    """An ``outcome`` result: already normalised, so ``norm`` passes it through."""


def norm(value):
    """Reduce a value to comparable plain data, keeping class names."""
    if isinstance(value, _Out):
        return tuple(value)
    if isinstance(value, BaseException):
        return _exc(value)
    if isinstance(value, enum.Enum):
        return ("enum", type(value).__name__, norm(value.value))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = max_tokens_default_to_final(value)  # declared: the pipeline's output cap is unset by default
        out = {"__type__": type(value).__name__}
        for f in dataclasses.fields(value):
            if f.name in _FINAL_ONLY_FIELDS:
                continue
            item = getattr(value, f.name)
            out[f.name] = "<masked>" if f.name in _MASKED_FIELDS else norm(item)
        return out
    if isinstance(value, Mapping):
        return ("map", [(norm(k), norm(v)) for k, v in value.items()])
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


def expect_ok(res):
    assert res[0] == "ok", res
    return res[1]


def expect_raise(res, name: str, contains: str = ""):
    assert res[0] == "raise", res
    assert name in res[1], res
    assert contains in res[2], res
    return res


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """No host state: HOME is a temp dir and every routing variable is cleared."""
    home = tmp_path / "_home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name in (
        "CONTENT_PIPELINE_LLM_MODELS", "CONTENT_PIPELINE_LLM_BACKEND",
        "CONTENT_PIPELINE_LLM_MODEL", "CONTENT_PIPELINE_LLM_ENDPOINT",
        "CONTENT_PIPELINE_SPEND_LEDGER", "MODEL_ENDPOINTS_REGISTRY",
    ):
        monkeypatch.delenv(name, raising=False)


class Ctx:
    """What a scenario receives: the side, a fresh work directory, and a patch helper."""

    def __init__(self, side, work: Path, stack: contextlib.ExitStack):
        self.side = side
        self.work = work
        self._stack = stack

    def patch(self, target, name, value):
        self._stack.enter_context(mock.patch.object(target, name, value))
        return value

    def sleeps(self):
        """Record ``platform.time.sleep`` calls instead of sleeping."""
        seen = []
        self.patch(self.side.platform.time, "sleep", seen.append)
        return seen

    def warnings(self):
        rec = self._stack.enter_context(warnings.catch_warnings(record=True))
        warnings.simplefilter("always")
        return rec


@pytest.fixture
def par(sides, tmp_path, capsys):
    """``par(scenario)``: run on both sides, assert equal, return the legacy outcome."""

    counter = [0]

    def run(scenario):
        seen = []
        counter[0] += 1
        work = tmp_path / f"_work_{counter[0]}"
        for side in sides:
            _rmtree(work)
            work.mkdir()
            capsys.readouterr()
            with contextlib.ExitStack() as stack:
                res = outcome(scenario, Ctx(side, work, stack))
            err = capsys.readouterr().err
            seen.append((res, err))
        if seen[1] != seen[0]:
            pytest.fail("final differs from legacy at " + first_diff(list(seen[0]), list(seen[1])))
        return seen[0][0]

    return run


@pytest.fixture
def split(sides, tmp_path, capsys):
    """``split(scenario)``: run on both sides and return ``(legacy, final)`` unequal-allowed.

    For a declared difference only: the test asserts each side's own outcome."""

    counter = [0]

    def run(scenario):
        seen = []
        counter[0] += 1
        work = tmp_path / f"_split_{counter[0]}"
        for side in sides:
            _rmtree(work)
            work.mkdir()
            capsys.readouterr()
            with contextlib.ExitStack() as stack:
                seen.append(outcome(scenario, Ctx(side, work, stack)))
            capsys.readouterr()
        return seen[0], seen[1]

    return run


def _rmtree(path: Path) -> None:
    def on_error(func, target, _exc):
        os.chmod(target, 0o700)
        func(target)

    if path.exists():
        shutil.rmtree(path, onerror=on_error)


def _tree_text(root: Path) -> list:
    return sorted(
        (p.relative_to(root).as_posix(), _text(p.read_text(encoding="utf-8")))
        for p in root.rglob("*")
        if p.is_file()
    )


PRICING = {
    "m": {"input": 3.0, "output": 15.0, "cache_hit": 0.3},
    "n": {"input": 1.0, "output": 2.0},
}


# --------------------------------------------------------------------------
# Surface
# --------------------------------------------------------------------------


def _own(module):
    return {
        n
        for n in dir(module)
        if not n.startswith("_")
        and getattr(getattr(module, n), "__module__", "") == module.__name__
    }


@pytest.mark.parametrize("key", ["platform", "backends", "gate", "spend", "yaml_extract", "budget"])
def test_surface_matches(sides, key):
    legacy, final = sides
    lm, fm = getattr(legacy, key), getattr(final, key)
    if hasattr(lm, "__all__") or hasattr(fm, "__all__"):
        assert list(lm.__all__) == list(fm.__all__), key
    assert _own(lm) == _own(fm), key


def test_package_surface_matches(sides):
    legacy, final = sides
    assert list(legacy.pkg.__all__) == list(final.pkg.__all__)
    for name in legacy.pkg.__all__:
        assert hasattr(final.pkg, name), name
    assert final.pkg.OutputContract is final.ctypes.OutputContract
    assert legacy.pkg.OutputContract is legacy.ctypes.OutputContract
    with pytest.raises(AttributeError):
        getattr(final.pkg, "nonexistent_symbol")


def test_module_constants_match(sides):
    legacy, final = sides
    names = [
        "BASE_SECONDS", "CAP_SECONDS", "GIVE_UP_SECONDS", "RECOVER_AFTER", "TRANSIENT_KINDS",
    ]
    for name in names:
        assert getattr(legacy.gate, name) == getattr(final.gate, name), name
    for name in ("MODELS_ENV", "REMOVED_ROUTING_ENVS", "OPENCODE_FILESYSTEM_POSTURE"):
        assert getattr(legacy.backends, name) == getattr(final.backends, name), name
    for name in (
        "NANO_PER_USD", "SCHEMA_VERSION", "LEDGER_STATES", "ORPHAN_RECLAIM_MODES",
        "LEDGER_PATH_ENV", "STRUCTURAL_CLAUSES", "_SCHEMA",
    ):
        assert getattr(legacy.spend, name) == getattr(final.spend, name), name
    for name in ("SPEND_CAP", "SPEND_HALT"):
        assert getattr(legacy.budget, name) == getattr(final.budget, name), name
    for name in (
        "HALT_AUTH", "HALT_RATE_LIMIT", "HALT_INSUFFICIENT_CREDIT", "HALT_QUOTA",
        "HALT_BACKPRESSURE", "HALT_UNREACHABLE", "_CONTRACT_SYMBOLS",
    ):
        assert getattr(legacy.platform, name) == getattr(final.platform, name), name


# --------------------------------------------------------------------------
# Response helpers
# --------------------------------------------------------------------------


def _resp(side, text="hello", **kw):
    base = dict(text=text, model="m", input_tokens=100, output_tokens=50)
    base.update(kw)
    return side.platform.LLMResponse(**base)


# --------------------------------------------------------------------------
# call_llm
# --------------------------------------------------------------------------


def test_call_llm_success_cost_and_budget(par):
    def sc(ctx):
        p = ctx.side.platform
        backend = ctx.side.backends.MockBackend(
            responses=[{"text": "hello", "input_tokens": 1_000_000, "output_tokens": 100_000}]
        )
        budget = p.CostBudget(limit=100.0)
        resp = p.call_llm(
            backend, "sys", "user", model="m", pricing=PRICING, cost_budget=budget,
            identifier="u1",
        )
        return resp, budget, backend.calls, p.response_cost("m", resp, pricing=PRICING)

    res = par(sc)
    expect_ok(res)


def test_call_llm_budget_and_unknown_model(par):
    def over_input(ctx):
        p = ctx.side.platform
        backend = ctx.side.backends.MockBackend(responses=["x"])
        return p.call_llm(
            backend, "s" * 400, "u" * 400, model="m", input_budgets={"m": 10}, identifier="big"
        )

    expect_raise(par(over_input), "BudgetExceededError", "budget exceeded for 'big'")

    def over_cost(ctx):
        p = ctx.side.platform
        backend = ctx.side.backends.MockBackend(
            responses=[{"text": "x", "input_tokens": 2_000_000, "output_tokens": 0}]
        )
        return p.call_llm(
            backend, "s", "u", model="m", pricing=PRICING,
            cost_budget=p.CostBudget(limit=1.0), identifier="costly",
        )

    expect_raise(par(over_cost), "BudgetExceededError", "costly")

    def unknown(ctx):
        p = ctx.side.platform
        backend = ctx.side.backends.MockBackend(responses=["x"])
        return p.call_llm(backend, "s", "u", model="nope", pricing=PRICING)

    expect_raise(par(unknown), "KeyError", "Unknown model 'nope'")


def test_call_llm_cache_roundtrip(par):
    def sc(ctx):
        p = ctx.side.platform
        cache = ctx.work / "cache"
        backend = ctx.side.backends.MockBackend(responses=["first", "second"])
        # Declared difference: the final default output cap is unset, so a default-options key
        # differs from the legacy one that hashed 4096 (tests/llm-kit/test_pipeline_cap.py pins it).
        # An explicit 4096 keys identically on both sides, which is what is compared here.
        opts = p.BackendOptions(max_tokens=4096)
        a = p.call_llm(backend, "s", "u", model="m", options=opts, cache_dir=cache, pricing=PRICING)
        b = p.call_llm(backend, "s", "u", model="m", options=opts, cache_dir=cache, pricing=PRICING)
        c = p.call_llm(backend, "s", "u2", model="m", options=opts, cache_dir=cache, pricing=PRICING)
        return a, b, c, len(backend.calls), _tree_text(cache)

    res = par(sc)
    a, b, c, n, tree = expect_ok(res)[1]
    assert b["from_cache"] is True and n == 2 and len(tree[1]) == 2


def test_call_llm_retry_empty_and_halt(par):
    def retried(ctx):
        p = ctx.side.platform
        sleeps = ctx.sleeps()
        backend = ctx.side.backends.MockBackend(responses=[RuntimeError("flaky"), "ok"])
        resp = p.call_llm(backend, "s", "u", model="m", retries=2, retry_sleep=1.5)
        return resp, sleeps

    expect_ok(par(retried))

    def exhausted(ctx):
        p = ctx.side.platform
        ctx.sleeps()
        backend = ctx.side.backends.MockBackend(responses=[ValueError("a"), ValueError("b")])
        return p.call_llm(backend, "s", "u", model="m", retries=1)

    expect_raise(par(exhausted), "ValueError", "b")

    def empty(ctx):
        p = ctx.side.platform
        ctx.sleeps()
        backend = ctx.side.backends.MockBackend(
            responses=[{"text": "  ", "output_tokens": 40, "reasoning": "thinking"}] * 2
        )
        return p.call_llm(backend, "s", "u", model="m", retries=1)

    expect_raise(par(empty), "EmptyCompletionError")

    def halted(ctx):
        p = ctx.side.platform
        backend = ctx.side.backends.MockBackend(responses=[RuntimeError('boom authentication_error')])
        return p.call_llm(backend, "s", "u", model="m", retries=3)

    expect_raise(par(halted), "PipelineHaltError")

    def scripted_halt(ctx):
        p = ctx.side.platform
        backend = ctx.side.backends.MockBackend(
            responses=[p.PipelineHaltError(p.HALT_RATE_LIMIT, "slow down", retry_after_s=4.0)]
        )
        return p.call_llm(backend, "s", "u", model="m", retries=3)

    expect_raise(par(scripted_halt), "PipelineHaltError", "slow down")


def test_call_llm_callers_own_deadline_is_not_a_halt(par):
    def sc(ctx):
        p = ctx.side.platform
        ctx.sleeps()
        backend = ctx.side.backends.MockBackend(
            responses=[TimeoutError("took too long"), "recovered"]
        )
        return p.call_llm(
            backend, "s", "u", model="m", retries=1,
            options=p.BackendOptions(timeout_s=5.0),
        )

    expect_ok(par(sc))


def test_call_llm_observer_stream(par):
    def sc(ctx):
        p = ctx.side.platform
        ctx.sleeps()
        seen = []
        backend = ctx.side.backends.MockBackend(responses=[RuntimeError("x"), "ok"])
        p.call_llm(
            backend, "s", "u", model="m", retries=1, identifier="id1",
            options=p.BackendOptions(temperature=0.5, max_tokens=64, effort="low"),
            on_attempt=seen.append,
        )
        return seen

    expect_ok(par(sc))


def test_call_llm_reported_cost_comes_first(par):
    def sc(ctx):
        p = ctx.side.platform
        reported = _resp(ctx.side, reported_cost_usd=0.5, reported_cost_source="provider")
        malformed = _resp(ctx.side, reported_cost_usd=0.5, reported_cost_source="bogus")
        cached = _resp(
            ctx.side, reported_cost_usd=0.5, reported_cost_source="provider", from_cache=True
        )
        backend = ctx.side.backends.MockBackend(responses=[reported])
        budget = p.CostBudget()
        p.call_llm(backend, "s", "u", model="m", pricing=PRICING, cost_budget=budget)
        return (
            budget.spent,
            p.response_cost("m", reported, pricing=PRICING),
            p.response_cost("m", reported, pricing=None),
            p.response_cost("m", malformed, pricing=PRICING),
            p.response_cost("m", malformed, pricing=None),
            p.response_cost("m", cached, pricing=PRICING),
            p.valid_reported_cost(0.25, "registry-unmetered"),
            p.valid_reported_cost(-1.0, "provider"),
            p.valid_reported_cost(None, "provider"),
        )

    expect_ok(par(sc))


def test_exception_usage_is_charged_before_classification(par):
    class Boom(RuntimeError):
        output_tokens = 1_000_000
        input_tokens = 0
        model = "n"

    def sc(ctx):
        p = ctx.side.platform
        backend = ctx.side.backends.MockBackend(responses=[Boom("provider failed")])
        budget = p.CostBudget()
        try:
            p.call_llm(
                backend, "s", "u", model="m", pricing=PRICING, cost_budget=budget, retries=0
            )
        except Boom:
            pass
        return budget.spent

    assert expect_ok(par(sc)) == 2.0


def test_classifiers_and_text_helpers(par):
    class WithKind(Exception):
        halt_kind = "quota"

    def sc(ctx):
        p = ctx.side.platform
        texts = [
            "", "rate limit exceeded", "authentication_error: bad", "insufficient credits",
            "hello", '{"api_error_status":429}', "you hit your limit", 'api_error_status:401',
        ]
        excs = [
            RuntimeError("authentication_error"), WithKind("x"),
            RuntimeError("wrapped"), ValueError("nothing"),
        ]
        wrapped = RuntimeError("outer")
        wrapped.__cause__ = RuntimeError("hit your limit")
        return (
            [p.classify_halt_text(t) for t in texts],
            [p.classify_openai_exception(e) for e in excs + [wrapped]],
            [p._is_callers_own_deadline(e) for e in (TimeoutError(), ValueError(), WithKind())],
            [p.is_likely_reasoning_exhaustion(t, n) for t, n in (("", 5), (" ", 0), ("x", 9))],
            p.describe_likely_reasoning_exhaustion(
                _resp(ctx.side, text="", output_tokens=30, reasoning="r" * 300)
            ),
            p.describe_likely_reasoning_exhaustion(_resp(ctx.side)),
            p.model_alias("m"),
            p.model_alias("openrouter/some-model:free"),
            p.estimate_request_tokens("abc", "defg"),
            p.estimate_cost("m", 10, 10, 4, pricing=PRICING),
        )

    expect_ok(par(sc))


def test_apiname_error_classes_named_by_class(par):
    def sc(ctx):
        p = ctx.side.platform
        timeout_like = type("APITimeoutError", (Exception,), {})("t")
        agent_like = type("AgentTimeoutError", (Exception,), {})("t")
        real_agent = ctx.side.halt.AgentTimeoutError.__new__(ctx.side.halt.AgentTimeoutError)
        return (
            p._is_callers_own_deadline(timeout_like),
            p._is_callers_own_deadline(agent_like),
            p._is_callers_own_deadline(real_agent),
        )

    expect_ok(par(sc))


# --------------------------------------------------------------------------
# submit_validated and output contracts
# --------------------------------------------------------------------------


def _validator(side):
    rej = side.vc.Rejection

    def validate(value, context):
        if isinstance(value, dict) and value.get("n", 0) < 2:
            return [rej(kind="too_small", detail="n must be at least 2", rule_id="r1")]
        return []

    return validate


def test_submit_validated_loop(par):
    def sc(ctx):
        p = ctx.side.platform
        ctx.sleeps()
        backend = ctx.side.backends.MockBackend(
            responses=['{"n": 1}', "not json", '{"n": 5}']
        )
        result = p.submit_validated(
            backend=backend, system="s", user="u", model="m", parse_fn=json.loads,
            validators=[_validator(ctx.side)], max_attempts=3, identifier="sv",
        )
        return result, [c["user"] for c in backend.calls]

    expect_ok(par(sc))


def test_submit_validated_exhaustion_and_spec_errors(par):
    def exhausted(ctx):
        p = ctx.side.platform
        backend = ctx.side.backends.MockBackend(responses=['{"n": 1}'] * 2)
        return p.submit_validated(
            backend=backend, system="s", user="u", model="m", parse_fn=json.loads,
            validators=[_validator(ctx.side)], max_attempts=2,
        )

    expect_ok(par(exhausted))

    def no_parse(ctx):
        return ctx.side.platform.ValidationSpec()

    expect_raise(par(no_parse), "ValueError", "needs a parse_fn")

    def evaluate(ctx):
        p = ctx.side.platform
        spec = p.ValidationSpec(parse_fn=json.loads, validators=[_validator(ctx.side)])
        return [
            p.evaluate_submission(t, spec) for t in ('{"n": 3}', '{"n": 0}', "oops", "")
        ]

    expect_ok(par(evaluate))


def _contract(side, policy="validated-result"):
    kwargs = {}
    if policy != "text-only":
        kwargs["schema"] = {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
            "additionalProperties": False,
        }
    return side.ctypes.OutputContract(id="t.c", policy=policy, **kwargs)


def test_output_contract_submit_paths(par):
    def valid(ctx):
        p = ctx.side.platform
        backend = ctx.side.backends.MockBackend(responses=['{"n": 3}'])
        return p.submit_validated(
            backend=backend, system="s", user="u", model="m",
            output_contract=_contract(ctx.side), max_attempts=1,
        )

    expect_ok(par(valid))

    def invalid_then_valid(ctx):
        p = ctx.side.platform
        ctx.sleeps()
        backend = ctx.side.backends.MockBackend(responses=['{"n": "x"}', '{"n": 4}'])
        return p.submit_validated(
            backend=backend, system="s", user="u", model="m",
            output_contract=_contract(ctx.side), max_attempts=2,
        )

    expect_ok(par(invalid_then_valid))

    def native_required_needs_report(ctx):
        p = ctx.side.platform
        backend = ctx.side.backends.MockBackend(responses=['{"n": 3}'])
        return p.submit_validated(
            backend=backend, system="s", user="u", model="m",
            output_contract=_contract(ctx.side, "native-required"), max_attempts=1,
        )

    expect_raise(
        par(native_required_needs_report), "StructuredContractSupportError",
        "native delivery cannot be proven",
    )

    def text_only(ctx):
        p = ctx.side.platform
        backend = ctx.side.backends.MockBackend(responses=["fine"])
        return p.submit_validated(
            backend=backend, system="s", user="u", model="m", parse_fn=lambda t: t,
            output_contract=_contract(ctx.side, "text-only"), max_attempts=1,
        )

    expect_ok(par(text_only))

    def contract_and_parse(ctx):
        return ctx.side.platform.ValidationSpec(
            parse_fn=json.loads, output_contract=_contract(ctx.side)
        )

    expect_raise(par(contract_and_parse), "ValueError", "replaces parse_fn")

    def not_a_contract(ctx):
        return ctx.side.platform.ValidationSpec(parse_fn=json.loads, output_contract=object())

    expect_raise(par(not_a_contract), "TypeError", "must be an llm_scripting_kit OutputContract")


def test_contract_seam_frontier(sides):
    legacy, final = sides
    ls = legacy.platform._contract_seam()
    fs = final.platform._contract_seam()
    assert sorted(vars(ls)) == sorted(vars(fs))
    assert fs.OutputContract is final.ctypes.OutputContract
    assert fs.BackendOptions.__module__ == "llm_kit.completion.types"
    for name in legacy.platform._CONTRACT_SYMBOLS:
        assert hasattr(fs, name), name


def test_cache_key_and_response_cache_agree(par):
    def sc(ctx):
        p = ctx.side.platform
        # An explicit 4096: the default is unset in the final (see test_call_llm_cache_roundtrip).
        key = p.build_cache_key(
            backend="mock", model="m", system="s", user="u", options=p.BackendOptions(max_tokens=4096)
        )
        cache = p.ResponseCache(ctx.work / "rc")
        stored = cache.store(key, _resp(ctx.side, "payload"))
        skipped = cache.store("k2", _resp(ctx.side, "  "))
        return key, stored, skipped, cache.lookup(key), cache.lookup("missing")

    expect_ok(par(sc))


# --------------------------------------------------------------------------
# Backpressure classifier probe (DEGRADE, kept as-is)
# --------------------------------------------------------------------------


def _block(monkeypatch, name):
    for mod in list(sys.modules):
        if mod == name or mod.startswith(name + "."):
            monkeypatch.delitem(sys.modules, mod)
    monkeypatch.setitem(sys.modules, name, None)  # import raises ImportError


def test_backpressure_probe_present(par):
    def sc(ctx):
        p = ctx.side.platform
        fn = p._backpressure_classifier()
        exc = RuntimeError("Error code: 429 - overloaded. retry after 12 seconds")
        return fn.__name__, fn(exc), fn(ValueError("nothing"))

    res = par(sc)
    assert expect_ok(res)[1][0] == "classify_backpressure"


def _reworded(message: str) -> str:
    """A message in its final wording and floor (final_wording; idempotent)."""
    return floors_in_text_to_final(to_final(message))


def test_backpressure_probe_absent_and_stale_warn_once(sides, monkeypatch):
    legacy, final = sides
    seen = {}
    for side, block in (
        (legacy, "llm_scripting_kit"),
        (final, "llm_kit.completion"),
    ):
        monkeypatch.setattr(side.platform, "_BACKPRESSURE_WARNED", set())
        with monkeypatch.context() as m:
            _block(m, block)
            with warnings.catch_warnings(record=True) as rec:
                warnings.simplefilter("always")
                assert side.platform._backpressure_classifier() is None
                assert side.platform._backpressure_classifier() is None
            absent = [(w.category.__name__, _reworded(str(w.message))) for w in rec]

        monkeypatch.setattr(side.platform, "_BACKPRESSURE_WARNED", set())
        with monkeypatch.context() as m:
            halt_name = side.halt_path
            comp_name = halt_name.rsplit(".", 1)[0]
            halt_mod = types.ModuleType(halt_name)  # no classify_backpressure
            comp_mod = types.ModuleType(comp_name)
            comp_mod.__path__ = []
            comp_mod.halt = halt_mod
            for n, mod in ((comp_name, comp_mod), (halt_name, halt_mod)):
                m.setitem(sys.modules, n, mod)
            with warnings.catch_warnings(record=True) as rec:
                warnings.simplefilter("always")
                assert side.platform._backpressure_classifier() is None
                assert side.platform._backpressure_classifier() is None
            stale = [(w.category.__name__, _reworded(str(w.message))) for w in rec]
        seen[side.name] = (absent, stale)
    assert seen["legacy"] == seen["final"]
    assert len(seen["final"][0]) == 1 and "absent" in seen["final"][0][0][1]
    assert len(seen["final"][1]) == 1 and "predates" in seen["final"][1][0][1]


# --------------------------------------------------------------------------
# AdaptiveGate
# --------------------------------------------------------------------------


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _gate(side, clock, **kw):
    notes = []
    gate = side.gate.AdaptiveGate(
        kw.pop("jobs", 4), notes.append, clock=clock.time, sleep=clock.sleep,
        jitter=lambda: 0.5, **kw,
    )
    return gate, notes


def test_gate_backs_off_and_recovers(par):
    def sc(ctx):
        p = ctx.side.platform
        clock = _Clock()
        gate, notes = _gate(ctx.side, clock, recover_after=2)
        outcomes = []
        script = [
            p.PipelineHaltError(p.HALT_BACKPRESSURE, "queue full", retry_after_s=7.0),
            p.PipelineHaltError(p.HALT_RATE_LIMIT, "429"),
            "a", "b", "c", "d",
        ]

        def fn():
            item = script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        for label in ("w1", "w2", "w3", "w4"):
            outcomes.append(gate.run(fn, label=label))
        return (
            outcomes, notes, clock.sleeps, gate.limit, gate.refusals, gate.in_flight,
            gate._admitted, gate._epoch,
        )

    expect_ok(par(sc))


def test_gate_gives_up_and_stays_given_up(par):
    def sc(ctx):
        p = ctx.side.platform
        clock = _Clock()
        gate, notes = _gate(ctx.side, clock, give_up_s=100.0, base_s=60.0, cap_s=60.0)

        def fn():
            raise p.PipelineHaltError(p.HALT_BACKPRESSURE, "still busy")

        first = outcome(gate.run, fn, label="x")
        second = outcome(gate.run, lambda: "never")
        return first, second, notes, clock.sleeps, gate.gave_up

    expect_ok(par(sc))


def test_gate_non_backpressure_and_predicates(par):
    def sc(ctx):
        p = ctx.side.platform
        gate_mod = ctx.side.gate
        clock = _Clock()
        gate, notes = _gate(ctx.side, clock)
        boom = outcome(gate.run, lambda: (_ for _ in ()).throw(ValueError("plain")))
        not_halt = outcome(gate_mod.halt_retry_after, ValueError("x"))
        hint = gate_mod.halt_retry_after(p.PipelineHaltError(p.HALT_RATE_LIMIT, "m", retry_after_s=3.0))
        gate2, _ = _gate(
            ctx.side, clock, is_backpressure=lambda e: isinstance(e, KeyError),
            retry_after=None, not_admitted=lambda e: isinstance(e, KeyError),
        )
        seq = [KeyError("k"), "done"]

        def fn():
            item = seq.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        value = gate2.run(fn)
        return (
            boom, not_halt, hint, value, clock.sleeps, gate2.limit, gate2.refusals,
            [gate_mod.is_transient_halt(e) for e in (
                p.PipelineHaltError(p.HALT_AUTH, "a"), p.PipelineHaltError(p.HALT_BACKPRESSURE, "b"),
                ValueError("v"),
            )],
        )

    expect_ok(par(sc))


def test_call_llm_gated_and_endpoint_busy(par):
    def sc(ctx):
        p = ctx.side.platform
        clock = _Clock()
        gate, notes = _gate(ctx.side, clock, jobs=2, recover_after=1)
        backend = ctx.side.backends.MockBackend(
            responses=[p.PipelineHaltError(p.HALT_BACKPRESSURE, "busy", retry_after_s=2.0), "fine"]
        )
        resp = ctx.side.gate.call_llm_gated(
            gate, backend, "s", "u", model="m", retries=0
        )
        return resp, notes, clock.sleeps, gate.limit

    expect_ok(par(sc))


def test_gate_concurrency_limit_blocks_extra_callers(sides):
    for side in sides:
        gate = side.gate.AdaptiveGate(1)
        entered = threading.Event()
        release = threading.Event()
        results = []

        def holder():
            def body():
                entered.set()
                release.wait(timeout=10)
                return "held"

            results.append(gate.run(body))

        t1 = threading.Thread(target=holder)
        t1.start()
        assert entered.wait(timeout=10)
        t2 = threading.Thread(target=lambda: results.append(gate.run(lambda: "second")))
        t2.start()
        deadline = 0
        while gate.waiting == 0 and deadline < 2000:
            threading.Event().wait(0.005)
            deadline += 1
        assert gate.waiting == 1, side.name
        release.set()
        t1.join(timeout=10)
        t2.join(timeout=10)
        assert sorted(results) == ["held", "second"], side.name


# --------------------------------------------------------------------------
# budget
# --------------------------------------------------------------------------


def test_budget_stop_surface(par):
    def sc(ctx):
        b = ctx.side.budget
        p = ctx.side.platform
        s = ctx.side.spend
        ledger = s.create_ledger(ctx.work / "l.db", cap_usd=1.0, run_id="r")

        def preflight_halt():
            raise p.PipelineHaltError(p.HALT_AUTH, "expired")

        stops = {}
        stops["preflight_halt"] = outcome(b.preflight_check, preflight_halt)
        stops["preflight_ok"] = outcome(b.preflight_check, lambda: None)
        stops["preflight_other"] = outcome(
            b.preflight_check, lambda: (_ for _ in ()).throw(KeyError("k"))
        )

        def under(exc):
            with b.spend_stop(["a"], ["b", "c"], unit_id="u7"):
                raise exc

        cap = s.SpendCapExceeded(identifier="i", measured=2.0, budget=1.0, model="m")
        halted = s.SpendLedgerHalted(
            identifier="i", measured=2.0, budget=1.0, model="m", reason="r", detail="d"
        )
        stops["cap"] = outcome(under, cap)
        stops["halted"] = outcome(under, halted)
        stops["operational"] = outcome(under, sqlite3.OperationalError("locked"))
        stops["value"] = outcome(under, ValueError("cfg"))
        stops["check_clean"] = outcome(b.check_response, "all fine")
        stops["check_halt"] = outcome(b.check_response, "Error: rate limit exceeded")
        stops["check_obj"] = outcome(
            b.check_response, types.SimpleNamespace(text="authentication_error")
        )
        stops["check_str"] = outcome(b.check_response, 12345)
        return stops

    expect_ok(par(sc))


# --------------------------------------------------------------------------
# yaml_extract
# --------------------------------------------------------------------------


def test_yaml_extract(par):
    samples = [
        "```yaml\nitems:\n  - a\n  - b\n```",
        "```yml linenos\nk: v\n```",
        "```\nk: v\n```",
        "plain: yaml\nother: 2",
        "Here you go:\nitems:\n  - 1\n",
        "no yaml {{{ at all:",
        "- a\n- b",
        "",
        "```yaml\nk: [unclosed\n```",
        "```YAML\nUp: 1\n```",
    ]

    def sc(ctx):
        y = ctx.side.yaml_extract
        out = []
        for text in samples:
            out.append(outcome(y.strip_fence, text))
            out.append(outcome(y.extract_yaml, text))
            out.append(outcome(y.extract_yaml, text, anchor="items:"))
            out.append(outcome(y.extract_mapping, text))
            out.append(outcome(y.extract_mapping, text, required_keys=["k"], anchor="items:"))
        return out

    expect_ok(par(sc))


# --------------------------------------------------------------------------
# Spend ledger
# --------------------------------------------------------------------------


def _rows(path: Path):
    conn = sqlite3.connect(str(path))
    try:
        conn.row_factory = sqlite3.Row
        out = {}
        for table in ("schema_version", "policy", "halt", "halt_history", "ledger", "leaks"):
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            out[table] = [
                {k: ("<masked>" if k in _MASKED_FIELDS else norm(r[k])) for k in r.keys()}
                for r in rows
            ]
        return out
    finally:
        conn.close()


def test_spend_ledger_lifecycle(par):
    def sc(ctx):
        s = ctx.side.spend
        path = ctx.work / "ledger.db"
        ledger = s.create_ledger(
            path, cap_usd=1.0, run_id="run-1", meta={"b": 2, "a": 1}, busy_timeout_ms=1500,
        )
        r1 = ledger.reserve(0.25, scope="sc", identifier="i1", model="m", ttl_s=30)
        r2 = ledger.reserve(0.5, identifier="i2")
        over = outcome(ledger.reserve, 0.5, identifier="i3")
        ledger.settle(r1, 0.2)
        ledger.settle(r2, None)
        r3 = ledger.reserve(0.1, identifier="i4")
        overbilled = outcome(ledger.settle, r3, 0.4)  # overbilled: halts, then raises
        halted = outcome(ledger.reserve, 0.01)
        status = ledger.status()
        forced = outcome(ledger.resume)
        ledger.resume(force=True)
        r4 = outcome(ledger.reserve, 0.01, identifier="i5")
        reopened = s.open_ledger(path, synchronous="NORMAL", orphan_reclaim="lease")
        return (
            r1, r2, over, overbilled, halted, status, forced, r4, ledger.status(),
            reopened.status(),
            [reopened.cap_usd, reopened.run_id, reopened.schema_version,
             reopened.busy_timeout_ms, reopened.halt_on_overbilled,
             reopened.orphan_reclaim, reopened.reclaim_batch_limit],
            _rows(path),
        )

    expect_ok(par(sc))


def test_spend_ledger_halt_reclaim_and_leaks(par):
    def sc(ctx):
        s = ctx.side.spend
        ledger = s.create_ledger(
            ctx.work / "l.db", cap_usd=2.0, run_id="r2", orphan_reclaim="lease",
            reclaim_batch_limit=1,
        )
        short = ledger.reserve(0.5, identifier="short", ttl_s=0.001)
        ledger.reserve(0.25, identifier="forever")
        threading.Event().wait(0.05)
        ledger.reserve(0.1, identifier="triggers_sweep")
        late = outcome(ledger.settle, short, 0.5)  # reclaimed: its cost is a leak
        report = ledger.reclaim_orphans(batch_limit=5)
        ledger.halt("manual", "operator")
        h = outcome(ledger.check_halted, identifier="x")
        renewed = outcome(ledger.renew, short, ttl_s=5)
        return late, report, h, renewed, ledger.status(), _rows(ctx.work / "l.db")

    expect_ok(par(sc))


def test_spend_ledger_refusals(par):
    def sc(ctx):
        s = ctx.side.spend
        path = ctx.work / "x.db"
        out = {}
        out["no_file"] = outcome(s.open_ledger, ctx.work / "absent.db")
        out["bad_cap"] = outcome(s.create_ledger, ctx.work / "a.db", cap_usd=0, run_id="r")
        out["no_run"] = outcome(s.create_ledger, ctx.work / "b.db", cap_usd=1, run_id="")
        out["bad_mode"] = outcome(
            s.create_ledger, ctx.work / "c.db", cap_usd=1, run_id="r",
            orphan_reclaim="sometimes",
        )
        out["bad_batch"] = outcome(
            s.create_ledger, ctx.work / "d.db", cap_usd=1, run_id="r",
            reclaim_batch_limit=-1,
        )
        ledger = s.create_ledger(path, cap_usd=1, run_id="r")
        out["exists"] = outcome(s.create_ledger, path, cap_usd=1, run_id="r")
        out["zero"] = outcome(ledger.reserve, 0)
        out["negative_ttl"] = outcome(ledger.reserve, 0.1, ttl_s=-1)
        junk = ctx.work / "junk.db"
        junk.write_bytes(b"not a database")
        out["junk"] = outcome(s.open_ledger, junk)
        # tamper: a changed cap under an open handle is refused inside the next transaction
        conn = sqlite3.connect(str(path))
        conn.execute("UPDATE policy SET cap_nano = cap_nano * 2")
        conn.commit()
        conn.close()
        out["cap_changed"] = outcome(ledger.reserve, 0.1)
        conn = sqlite3.connect(str(path))
        conn.execute("UPDATE policy SET cap_nano = cap_nano / 2")
        conn.execute("UPDATE schema_version SET version = 9")
        conn.commit()
        conn.close()
        out["version_9"] = outcome(s.open_ledger, path)
        conn = sqlite3.connect(str(path))
        conn.execute("UPDATE schema_version SET version = 1")
        conn.execute(
            "INSERT INTO ledger (id, generation, state, reserved, settled, scope, identifier, "
            "model, created_at) VALUES ('bad', 0, 'weird', 5, NULL, '', '', '', 'x')"
        )
        conn.commit()
        conn.close()
        out["structure"] = outcome(ledger.reserve, 0.1)
        return out

    expect_ok(par(sc))


def test_spend_ledger_env_and_estimates(par):
    def sc(ctx):
        s = ctx.side.spend
        path = ctx.work / "env.db"
        s.create_ledger(path, cap_usd=3.0, run_id="envrun")
        return (
            s.spend_ledger_from_env({}),
            s.spend_ledger_from_env({"CONTENT_PIPELINE_SPEND_LEDGER": "  "}),
            s.spend_ledger_from_env({s.LEDGER_PATH_ENV: str(path)}).status(),
            s.usd_to_nano(0.1), outcome(s.usd_to_nano, "0.0000000011"), s.nano_to_usd(123456789),
            outcome(s.usd_to_nano, float("nan")),
            outcome(s.usd_to_nano, -1),
            s.estimate_reservation(
                "m", input_tokens=400, max_output_tokens=100, pricing=PRICING,
                cache_hit_tokens=50,
            ),
            outcome(
                s.estimate_reservation, "nope", input_tokens=1, max_output_tokens=1,
                pricing=PRICING,
            ),
        )

    expect_ok(par(sc))


def test_network_path_warning_uses_this_modules_name(par):
    def sc(ctx):
        s = ctx.side.spend
        rec = ctx.warnings()
        ctx.patch(s, "looks_like_network_path", lambda p: True)
        s.create_ledger(ctx.work / "n.db", cap_usd=1, run_id="net")
        s.create_ledger(ctx.work / "q.db", cap_usd=1, run_id="net2", warn_on_network_path=False)
        ctx.patch(s, "looks_like_network_path", lambda p: False)
        s.create_ledger(ctx.work / "o.db", cap_usd=1, run_id="net3")
        return [(w.category.__name__, _text(str(w.message))) for w in rec]

    warnings_seen = expect_ok(par(sc))[1]
    assert len(warnings_seen) == 1 and warnings_seen[0][1][0] == "RuntimeWarning"


def test_looks_like_network_path_is_defined_in_this_namespace(sides):
    legacy, final = sides
    assert "looks_like_network_path" in vars(final.spend)
    for path in ("C:/tmp/x", "//host/share/x", "\\\\host\\share\\x", "relative/x"):
        assert legacy.spend.looks_like_network_path(path) == final.spend.looks_like_network_path(path)


def test_call_llm_with_spend_guard(par):
    def sc(ctx):
        p = ctx.side.platform
        ctx.sleeps()
        ledger = ctx.side.spend.create_ledger(ctx.work / "g.db", cap_usd=0.5, run_id="guard")
        backend = ctx.side.backends.MockBackend(
            responses=[
                RuntimeError("transient"),
                {"text": "ok", "input_tokens": 1000, "output_tokens": 1000},
                {"text": "next", "input_tokens": 1000, "output_tokens": 1000},
            ]
        )
        first = p.call_llm(
            backend, "s", "u", model="m", pricing=PRICING, retries=1, spend=ledger,
            spend_reserve_usd=0.1, identifier="u1",
        )
        again = outcome(
            p.call_llm, backend, "s", "u2", model="m", pricing=PRICING, spend=ledger,
            spend_reserve_usd=10.0, identifier="u2",
        )
        return first, again, ledger.status(), _rows(ctx.work / "g.db")

    expect_ok(par(sc))


def test_budget_translation_over_a_real_guarded_call(par):
    def sc(ctx):
        p = ctx.side.platform
        ledger = ctx.side.spend.create_ledger(ctx.work / "g2.db", cap_usd=0.01, run_id="bs")
        backend = ctx.side.backends.MockBackend(responses=["x"])

        def attempt():
            with ctx.side.budget.spend_stop(["done"], ["rest"], unit_id="U"):
                return p.call_llm(
                    backend, "s", "u", model="m", pricing=PRICING, spend=ledger,
                    spend_reserve_usd=5.0,
                )

        return outcome(attempt)

    expect_raise(expect_ok(par(sc)), "BudgetStop", "spend_cap")


# --------------------------------------------------------------------------
# Spend ledger schema fixtures (fresh create, legacy vs llm-kit, byte-equal)
# --------------------------------------------------------------------------

_SYNCHRONOUS_VALUES = ("FULL", "NORMAL", "OFF", "EXTRA")


def schema_dump(spend, directory: Path) -> dict:
    """sqlite_master plus the schema_version table and pragmas of a fresh ledger."""
    out: dict = {"sqlite_master": None, "schema_version": None, "database_pragmas": None,
                 "connection_pragmas": {}}
    path = directory / "fresh.db"
    ledger = spend.create_ledger(path, cap_usd=1.0, run_id="fixture-run")
    conn = sqlite3.connect(str(path))
    try:
        out["sqlite_master"] = [
            {"type": t, "name": n, "tbl_name": tn, "sql": sql}
            for t, n, tn, sql in conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY rowid"
            )
        ]
        out["schema_version"] = [list(r) for r in conn.execute("SELECT version FROM schema_version")]
        out["database_pragmas"] = {
            name: conn.execute(f"PRAGMA {name}").fetchone()[0]
            for name in (
                "journal_mode", "user_version", "application_id", "page_size", "encoding",
                "auto_vacuum", "foreign_keys",
            )
        }
    finally:
        conn.close()
    for value in _SYNCHRONOUS_VALUES:
        handle = spend.open_ledger(path, synchronous=value, busy_timeout_ms=2500)
        with handle._connect() as c:  # noqa: SLF001 - the pragma posture is the subject
            out["connection_pragmas"][value] = {
                "synchronous": c.execute("PRAGMA synchronous").fetchone()[0],
                "busy_timeout": c.execute("PRAGMA busy_timeout").fetchone()[0],
                "journal_mode": c.execute("PRAGMA journal_mode").fetchone()[0],
                "isolation_level": c.isolation_level,
                "row_factory": getattr(c.row_factory, "__name__", repr(c.row_factory)),
            }
    return out


def _canonical(dump: dict) -> bytes:
    return (json.dumps(dump, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


_FIXTURE_FILES = {
    "sqlite_master": "fresh_create.sqlite_master.json",
    "schema_version": "fresh_create.schema_version.json",
    "database_pragmas": "fresh_create.database_pragmas.json",
    "connection_pragmas": "fresh_create.connection_pragmas.json",
}


def test_regenerate_spend_schema_fixtures_from_legacy(sides, tmp_path):
    """Writes the fixtures from the LEGACY module; runs only with the env var set."""
    import os

    if os.environ.get("LLM_KIT_REGEN_SPEND_FIXTURES") != "1":
        pytest.skip("set LLM_KIT_REGEN_SPEND_FIXTURES=1 to rewrite the fixtures from legacy")
    legacy, _final = sides
    dump = schema_dump(legacy.spend, tmp_path)
    _FIXTURES.mkdir(parents=True, exist_ok=True)
    for key, name in _FIXTURE_FILES.items():
        (_FIXTURES / name).write_bytes(_canonical({key: dump[key]}))


@pytest.mark.parametrize("which", ["legacy", "final"])
def test_fresh_create_schema_matches_fixture_bytes(sides, tmp_path, which):
    legacy, final = sides
    side = legacy if which == "legacy" else final
    dump = schema_dump(side.spend, tmp_path)
    for key, name in _FIXTURE_FILES.items():
        expected = (_FIXTURES / name).read_bytes()
        assert _canonical({key: dump[key]}) == expected, f"{which}: {key} differs from {name}"


def test_fixture_pins_the_posture(sides, tmp_path):
    dump = json.loads((_FIXTURES / _FIXTURE_FILES["connection_pragmas"]).read_text())
    posture = dump["connection_pragmas"]
    assert posture["FULL"]["synchronous"] == 2 and posture["NORMAL"]["synchronous"] == 1
    assert posture["OFF"]["synchronous"] == 0 and posture["EXTRA"]["synchronous"] == 3
    assert all(v["isolation_level"] is None for v in posture.values())
    assert all(v["journal_mode"] == "wal" for v in posture.values())
    master = json.loads((_FIXTURES / _FIXTURE_FILES["sqlite_master"]).read_text())
    names = [row["name"] for row in master["sqlite_master"]]
    assert "schema_version" in names and "ledger" in names and "leaks" in names


def test_schema_ddl_is_identical_statement_by_statement(sides):
    legacy, final = sides
    assert legacy.spend._SCHEMA == final.spend._SCHEMA
    assert [s.encode("ascii") for s in legacy.spend._SCHEMA] == [
        s.encode("ascii") for s in final.spend._SCHEMA
    ]
    assert legacy.spend.STRUCTURAL_CLAUSES == final.spend.STRUCTURAL_CLAUSES


# --------------------------------------------------------------------------
# Wrappers: construction through a factory
# --------------------------------------------------------------------------


def _delegate_class(response=None, raises=None, halt=None):
    class Delegate:
        built: list = []

        def __init__(self, **kwargs):
            Delegate.built.append(kwargs)
            self.kwargs = kwargs
            self.calls = []

        def complete(self, system, user, *, model, options):
            self.calls.append((system, user, model, options))
            if raises is not None:
                raise raises
            return response

        def classify_halt(self, exc):
            return halt if halt is not None else "delegated:" + type(exc).__name__

    return Delegate


_CLASS_NAMES = {
    "openrouter": "OpenRouterBackend",
    "model-endpoint": "OpenRouterBackend",
    "claude-cli": "ClaudeCliBackend",
    "codex-cli": "CodexCliBackend",
    "opencode-cli": "OpencodeCliBackend",
}


def _make(ctx, wrapper_name, delegate_cls, **options):
    """Build a wrapper on the current side, with the delegate class standing in for the adapter."""
    side = ctx.side
    cls = getattr(side.backends, wrapper_name)
    if side.name == "legacy":
        for attr in ("OpenRouterBackend", "ClaudeCliBackend", "CodexCliBackend", "OpencodeCliBackend"):
            ctx.patch(side.ccomp, attr, delegate_cls)
        return cls(**options)
    from llm_kit.dispatch.pipeline_backends import ConfiguredBackendFactory

    factory = ConfiguredBackendFactory(classes={n: delegate_cls for n in _CLASS_NAMES})
    return cls(**options, factory=factory)


_WRAPPERS = [
    ("OpenRouterBackend", dict(endpoint="e1", project_root=Path("/p"), client="C")),
    ("OpenRouterBackend", {}),
    ("ModelEndpointBackend", dict(endpoint="local", project_root=Path("/p"), client="C")),
    ("ClaudeCliBackend", {}),
    ("ClaudeCliBackend", dict(
        default_timeout_s=33.0, retry_max_attempts=4, retry_cooldown_s=2.5,
        diagnostics_dir=Path("/d"), executable="claude-x", runner=len,
    )),
    ("CodexCliBackend", {}),
    ("CodexCliBackend", dict(default_timeout_s=11.0, argv_prefix=("codex", "x"), runner=len)),
    ("OpencodeCliBackend", {}),
    ("OpencodeCliBackend", dict(argv_prefix=("oc",), runner=len)),
]


@pytest.mark.parametrize("wrapper, options", _WRAPPERS)
def test_wrapper_hands_the_same_constructor_kwargs_to_the_backend(par, wrapper, options):
    def sc(ctx):
        response = types.SimpleNamespace(
            text="t", model="m", input_tokens=1, output_tokens=2, cache_hit_tokens=3,
            wall_ms=4, attempts=1, from_cache=False,
        )
        delegate = _delegate_class(response=response)
        backend = _make(ctx, wrapper, delegate, **options)
        built_before = len(delegate.built)
        resp = backend.complete("sys", "user", model="m")
        backend.complete("sys", "user", model="m")
        halt = backend.classify_halt(ValueError("v"))
        return {
            "built_before": built_before,
            "built": [dict(sorted(k.items())) for k in delegate.built],
            "name": backend.name,
            "resp": resp,
            "halt": halt,
            "delegate_options": norm(backend._delegate.calls[0][3]),
        }

    res = par(sc)
    out = expect_ok(res)[1]
    assert out is not None


def test_omitted_runner_stays_omitted(par):
    def sc(ctx):
        delegate = _delegate_class(response=types.SimpleNamespace(
            text="t", model="m", input_tokens=0, output_tokens=0, cache_hit_tokens=0,
            wall_ms=0, attempts=1, from_cache=False))
        for wrapper in ("ClaudeCliBackend", "CodexCliBackend", "OpencodeCliBackend"):
            _make(ctx, wrapper, delegate).complete("s", "u", model="m")
        return [sorted(k) for k in delegate.built]

    res = par(sc)
    for keys in expect_ok(res)[1]:
        assert "runner" not in keys


def test_wrapper_builds_once_under_a_concurrent_first_wave(par):
    def sc(ctx):
        delegate = _delegate_class(response=types.SimpleNamespace(
            text="t", model="m", input_tokens=0, output_tokens=0, cache_hit_tokens=0,
            wall_ms=0, attempts=1, from_cache=False))
        backend = _make(ctx, "OpenRouterBackend", delegate, endpoint="e")
        gate = threading.Barrier(16)
        errors = []

        def worker():
            try:
                gate.wait(timeout=10)
                backend.complete("s", "u", model="m")
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        return len(delegate.built), errors

    assert expect_ok(par(sc)) == ("tuple", [1, ("list", [])])


def test_wrapper_adapts_response_error_report_and_cost(par):
    class Err:
        def to_json(self):
            return {"code": "e1", "message": "m"}

    def sc(ctx):
        raw = types.SimpleNamespace(
            text="body", model="gpt-x", input_tokens=7, output_tokens=8, cache_hit_tokens=1,
            wall_ms=9, attempts=2, from_cache=False, total_tokens=15, reasoning="why",
            finish_reason="length", status="completed", error=Err(),
            dropped_params=("a",), forwarded_params=("b",), execution_controls_applied=("c",),
            structured={"k": 1}, started_at="s", ended_at="e",
            reported_cost_usd=0.75, reported_cost_source="provider",
            output_contract={"valid": True},
        )
        bare = types.SimpleNamespace(
            text="body", model="gpt-x", input_tokens=7, output_tokens=8, cache_hit_tokens=1,
            wall_ms=9, attempts=2, from_cache=False,
        )
        half = types.SimpleNamespace(
            text="t", model="m", input_tokens=1, output_tokens=1, cache_hit_tokens=0,
            wall_ms=0, attempts=1, from_cache=False,
            reported_cost_usd=0.5, reported_cost_source=None,
        )
        out = []
        for response in (raw, bare, half):
            backend = _make(ctx, "OpenRouterBackend", _delegate_class(response=response))
            out.append(backend.complete("s", "u", model="m"))
        return out

    expect_ok(par(sc))


def test_wrapper_options_cross_the_seam_field_for_field(par):
    def sc(ctx):
        p = ctx.side.platform
        response = types.SimpleNamespace(
            text="t", model="m", input_tokens=1, output_tokens=1, cache_hit_tokens=0,
            wall_ms=0, attempts=1, from_cache=False)
        delegate = _delegate_class(response=response)
        backend = _make(ctx, "ClaudeCliBackend", delegate)
        options = p.BackendOptions(
            max_tokens=11, temperature=0.3, timeout_s=9.0, cache_salt=2,
            user_cache_prefix="pre", effort="high", allowed_tools=("Read",), cwd="/w",
            client_id="cid", log_prefix="lp", extras={"x": 1},
        )
        backend.complete("s", "u", model="m", options=options)
        return norm(backend._delegate.calls[0][3])

    out = dict(expect_ok(par(sc))[1])
    assert out["max_tokens"] == 11 and out["effort"] == "high" and out["log_prefix"] == "lp"


def test_wrapper_contract_violation_becomes_structural_error(par):
    def sc(ctx):
        side = ctx.side
        contract = _contract(side)
        failed = types.SimpleNamespace(
            text="bad", model="m", input_tokens=3, output_tokens=4, cache_hit_tokens=0,
            wall_ms=1, attempts=1, from_cache=False)
        violation = side.platform._contract_seam().OutputContractViolation.__new__(
            side.platform._contract_seam().OutputContractViolation
        )
        violation.response = failed
        violation.args = ("violated",)
        delegate = _delegate_class(raises=violation)
        backend = _make(ctx, "OpenRouterBackend", delegate)
        opts = side.platform.BackendOptions(output_contract=contract)
        return backend.complete("s", "u", model="m", options=opts)

    res = par(sc)
    expect_raise(res, "StructuralOutputError")


def test_wrapper_contract_options_carry_the_contract(par):
    def sc(ctx):
        side = ctx.side
        response = types.SimpleNamespace(
            text="{}", model="m", input_tokens=1, output_tokens=1, cache_hit_tokens=0,
            wall_ms=0, attempts=1, from_cache=False)
        delegate = _delegate_class(response=response)
        backend = _make(ctx, "OpenRouterBackend", delegate)
        opts = side.platform.BackendOptions(output_contract=_contract(side))
        backend.complete("s", "u", model="m", options=opts)
        sent = backend._delegate.calls[0][3]
        return norm(sent), type(sent).__module__.split(".")[-1]

    expect_ok(par(sc))


def test_wrapper_contract_type_is_checked(par):
    def sc(ctx):
        side = ctx.side
        response = types.SimpleNamespace(
            text="x", model="m", input_tokens=1, output_tokens=1, cache_hit_tokens=0,
            wall_ms=0, attempts=1, from_cache=False)
        backend = _make(ctx, "OpenRouterBackend", _delegate_class(response=response))
        return backend.complete(
            "s", "u", model="m", options=side.platform.BackendOptions(output_contract=object())
        )

    expect_raise(par(sc), "TypeError", "must be an llm_scripting_kit OutputContract")


# ---- model-endpoint backend ---------------------------------------------


def _entry(id_, harness="", drive="", model=""):
    return types.SimpleNamespace(
        id=id_, harness=harness, drive=drive or id_, model=model,
        reasoning_effort="medium",
    )


def _patch_registry(ctx, entry_id="reg-default", effort="medium", refuse=False, fail=False):
    side = ctx.side

    def resolve_registry_entry(name):
        if refuse:
            raise ValueError("endpoint 'x' is a harness entry, not an HTTP endpoint")
        if fail:
            raise RuntimeError("registry unreadable")
        return types.SimpleNamespace(id=name or entry_id, reasoning_effort=effort)

    ctx.patch(side.reg_entry, "resolve_registry_entry", resolve_registry_entry)


def _patch_probe(ctx, ok=True, detail="ok", record=None):
    side = ctx.side
    record = [] if record is None else record

    def probe(endpoint, **kw):
        record.append((endpoint, dict(kw)))
        return types.SimpleNamespace(ok=ok, endpoint=endpoint, base_url="http://x", detail=detail)

    ctx.patch(side.account, "probe_endpoint", probe)
    return record


def test_model_endpoint_entry_id_probe_and_failures(par):
    def sc(ctx):
        side = ctx.side
        m = side.backends.ModelEndpointBackend
        _patch_registry(ctx)
        record = _patch_probe(ctx)
        explicit = m(endpoint="given")
        empty = m()
        probe_empty = empty.probe(timeout=3.0)
        probe_root = m(endpoint="x", project_root=Path("/proj")).probe()
        return explicit._entry_id(), empty._entry_id(), empty.endpoint, probe_empty, probe_root, record

    expect_ok(par(sc))

    def unreadable(ctx):
        _patch_registry(ctx, fail=True)
        backend = ctx.side.backends.ModelEndpointBackend()
        return backend._entry_id(), backend.endpoint

    expect_ok(par(unreadable))

    def harness(ctx):
        _patch_registry(ctx, refuse=True)
        return ctx.side.backends.ModelEndpointBackend()._entry_id()

    expect_raise(par(harness), "ValueError", "is a harness entry")


def test_model_endpoint_classify_halt_connection_errors(par):
    def sc(ctx):
        _patch_registry(ctx)
        response = types.SimpleNamespace(
            text="t", model="m", input_tokens=0, output_tokens=0, cache_hit_tokens=0,
            wall_ms=0, attempts=1, from_cache=False)
        backend = _make(ctx, "ModelEndpointBackend", _delegate_class(response=response), endpoint="e")
        return [
            backend.classify_halt(exc)
            for exc in (ConnectionError("c"), TimeoutError("t"), ValueError("v"))
        ]

    expect_ok(par(sc))


def _fake_seam(plan_calls):
    def plan_effort(extras, effort, style):
        plan_calls.append((dict(extras or {}), effort, style))
        body = dict(extras or {})
        if effort is not None and "reasoning_effort" not in body:
            body["reasoning_effort"] = effort
        return types.SimpleNamespace(extra_body=body)

    def resolve_profile(entry_id, project_root=None):
        return types.SimpleNamespace(
            declared_effort="high", effort=types.SimpleNamespace(style="ninfer"),
        )

    return plan_effort, resolve_profile


def test_model_endpoint_effort_resolution(par):
    def sc(ctx):
        side = ctx.side
        calls = []
        _patch_registry(ctx, effort="low")
        ctx.patch(side.backends, "_effort_seam", lambda: _fake_seam(calls))
        response = types.SimpleNamespace(
            text="t", model="m", input_tokens=0, output_tokens=0, cache_hit_tokens=0,
            wall_ms=0, attempts=1, from_cache=False)
        delegate = _delegate_class(response=response)
        backend = _make(ctx, "ModelEndpointBackend", delegate, endpoint="e")
        P = side.platform.BackendOptions
        eff = [
            backend.effective_options(),
            backend.effective_options(P(effort="max")),
            backend.effective_options(P(extras={"reasoning_effort": None})),
        ]
        backend.complete("s", "u", model="m")
        backend.complete("s", "u", model="m", options=P(effort="xhigh", extras={"a": 1}))
        return eff, calls, [norm(c[3]) for c in backend._delegate.calls], backend._effort

    expect_ok(par(sc))


def test_model_endpoint_legacy_effort_path_without_the_seam(par):
    def sc(ctx):
        side = ctx.side
        _patch_registry(ctx, effort="low")
        ctx.patch(side.backends, "_effort_seam", lambda: None)
        response = types.SimpleNamespace(
            text="t", model="m", input_tokens=0, output_tokens=0, cache_hit_tokens=0,
            wall_ms=0, attempts=1, from_cache=False)
        backend = _make(ctx, "ModelEndpointBackend", _delegate_class(response=response), endpoint="e")
        P = side.platform.BackendOptions
        out = [
            backend.effective_options(),
            backend.effective_options(P(extras={"reasoning_effort": "x"})),
            backend.effective_options(P(extras={"reasoning_effort": None})),
        ]
        backend.complete("s", "u", model="m")
        return out, norm(backend._delegate.calls[0][3])

    expect_ok(par(sc))


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


def _fake_declaration(record, entry):
    def describe(names, project_root=None, caller=None, **extra):
        record.append((list(names), project_root, caller, dict(extra)))
        return types.SimpleNamespace(default=entry)

    return types.SimpleNamespace(describe=describe, CALLER_PROCESS="process")


def _route_ctx(ctx, entry, record=None):
    record = [] if record is None else record
    b = ctx.side.backends
    b.reset_declared_entry_cache()
    ctx.patch(b, "_declaration_module", lambda: _fake_declaration(record, entry))
    return record


def test_route_default_and_mock_wins(par, monkeypatch):
    def sc(ctx):
        b = ctx.side.backends
        b.reset_declared_entry_cache()
        mock_backend = b.MockBackend(responses=["x"])
        sentinel = b.OpenRouterBackend(endpoint="mine")
        return (
            b.route(openrouter=sentinel) is sentinel,
            b.route(mock=mock_backend) is mock_backend,
            b.route(mock=mock_backend, openrouter=sentinel) is mock_backend,
            b.declared_model_names(),
        )

    expect_ok(par(sc))


def test_no_declaration_is_refused_not_defaulted(split):
    """Declared difference (no shipped default, L3): the legacy pair fell back to OpenRouter."""

    def sc(ctx):
        b = ctx.side.backends
        b.reset_declared_entry_cache()
        return [
            outcome(b.route),
            outcome(b.routed_model, "slug/model"),
            outcome(b.routed_model, "slug/model", backend_name="openrouter"),
        ]

    legacy, final = split(sc)
    route, routed, routed_named = expect_ok(legacy)[1]
    assert expect_ok(route)["name"] == "openrouter"
    assert expect_ok(routed) == "slug/model" and expect_ok(routed_named) == "slug/model"
    for res in expect_ok(final)[1]:
        expect_raise(res, "ConfigurationError", "No model is declared")


def test_route_mock_wins_before_the_declaration_is_read(par, monkeypatch):
    monkeypatch.setenv("CONTENT_PIPELINE_LLM_BACKEND", "claude")
    monkeypatch.setenv("CONTENT_PIPELINE_LLM_MODELS", "a,b")

    def sc(ctx):
        b = ctx.side.backends

        def boom():
            raise AssertionError("declaration read")

        ctx.patch(b, "_declaration_module", boom)
        m = b.MockBackend()
        return b.route(mock=m) is m

    assert expect_ok(par(sc)) is True


@pytest.mark.parametrize(
    "env",
    [
        {"CONTENT_PIPELINE_LLM_BACKEND": "claude"},
        {"CONTENT_PIPELINE_LLM_MODEL": "x", "CONTENT_PIPELINE_LLM_ENDPOINT": "y"},
    ],
)
def test_route_refuses_removed_env_names(par, monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    def sc(ctx):
        b = ctx.side.backends
        return [outcome(b.route), outcome(b.routed_model, "m")]

    first, second = expect_ok(par(sc))[1]
    for res in (first, second):
        expect_raise(res, "ConfigurationError", "no longer select a backend or model")


def test_blank_removed_env_is_ignored_then_refused(split, monkeypatch):
    """A blank removed setting is not refused as stale; the declaration floor then applies.

    Declared difference (no shipped default, L3): legacy returned the OpenRouter default."""
    monkeypatch.setenv("CONTENT_PIPELINE_LLM_BACKEND", "   ")

    def sc(ctx):
        b = ctx.side.backends
        return [outcome(b.route), outcome(b.routed_model, "m")]

    legacy, final = split(sc)
    first, second = expect_ok(legacy)[1]
    assert first[0] == "ok" and second[0] == "ok"
    for res in expect_ok(final)[1]:
        expect_raise(res, "ConfigurationError", "No model is declared")


def test_route_declaration_selection(par, monkeypatch):
    monkeypatch.setenv("CONTENT_PIPELINE_LLM_MODELS", "claude-x, codex-y")

    def sc(ctx):
        b = ctx.side.backends
        out = {}
        for key, entry in {
            "claude": _entry("claude-x", harness="Claude", model="cm"),
            "codex": _entry("codex-y", harness="codex", model="xm"),
            "opencode": _entry("oc", harness="opencode", model="om"),
            "openrouter": _entry("openrouter", model="orm"),
            "endpoint": _entry("local-1", model="lm"),
        }.items():
            record = _route_ctx(ctx, entry)
            _patch_registry(ctx)
            _patch_probe(ctx)
            backend = b.route()
            out[key] = (backend, record, b.routed_model("req"), b.routed_model(""),
                        b.declared_backend_and_model(["a"]))
        return out

    expect_ok(par(sc))


def test_route_openrouter_entry_returns_the_callers_instance(par, monkeypatch):
    monkeypatch.setenv("CONTENT_PIPELINE_LLM_MODELS", "openrouter")

    def sc(ctx):
        b = ctx.side.backends
        _route_ctx(ctx, _entry("openrouter", model="entry-model"))
        mine = b.OpenRouterBackend(endpoint="pinned")
        return (
            b.route(openrouter=mine) is mine,
            b.route(openrouter=mine, models=["x"]) is mine,
            b.routed_model("slug"),
            b.routed_model(""),
        )

    expect_ok(par(sc))


def test_route_probe_failure_refuses_before_returning(par, monkeypatch):
    monkeypatch.setenv("CONTENT_PIPELINE_LLM_MODELS", "local-1")

    def sc(ctx):
        b = ctx.side.backends
        _route_ctx(ctx, _entry("local-1", model="lm"))
        record = _patch_probe(ctx, ok=False, detail="connection refused")
        return outcome(b.route), record

    result, record = expect_ok(par(sc))[1]
    expect_raise(result, "LLMUnavailableError", "model endpoint 'local-1' is unavailable")
    assert record[0] == "list" and len(record[1]) == 1


def test_route_explicit_models_win_and_are_validated(par, monkeypatch):
    monkeypatch.setenv("CONTENT_PIPELINE_LLM_MODELS", "from-env")

    def sc(ctx):
        b = ctx.side.backends
        record = _route_ctx(ctx, _entry("claude-x", harness="claude", model="cm"))
        names = [
            b.declared_model_names(),
            b.declared_model_names(["  a ", "", "b"]),
            outcome(b.declared_model_names, []),
            outcome(b.declared_model_names, ["", "  "]),
        ]
        routed = b.route(models=["explicit-1", "explicit-2"])
        b.route(models=["explicit-1", "explicit-2"])  # memoized: one describe
        b.route()  # env declaration: another
        return names, routed, record

    out = expect_ok(par(sc))[1]
    assert len(out[2]) == 2


def test_route_output_contract_requirements_and_memo_key(par, monkeypatch):
    monkeypatch.setenv("CONTENT_PIPELINE_LLM_MODELS", "claude-x")

    def sc(ctx):
        b = ctx.side.backends
        record = _route_ctx(ctx, _entry("claude-x", harness="claude", model="cm"))
        seam = ctx.side.platform._contract_seam()
        native = _contract(ctx.side, "native-required")
        valid = _contract(ctx.side, "validated-result")
        b.route()
        b.route(output_contract=native)
        b.route(output_contract=native)
        b.route(output_contract=valid)
        bad = outcome(b.route, output_contract=object())
        b.reset_declared_entry_cache()
        b.route()
        return record, bad, seam.contract_requirements(native)

    expect_ok(par(sc))


def test_resolve_declaration_and_backend_for_entry(par):
    def sc(ctx):
        b = ctx.side.backends
        record = []
        entry = _entry("claude-x", harness="claude", drive="claude-cli", model="cm")
        ctx.patch(b, "_declaration_module", lambda: _fake_declaration(record, entry))
        return (
            b.resolve_declaration(["a", "b"], project_root="/p"),
            b.resolve_declaration(["a"], requirements={"structured_output": "native"}),
            record,
            b.declared_backend_and_model(["a"], project_root="/q"),
            b._backend_for_entry(_entry("x", harness="")),
            b._backend_for_entry(_entry("openrouter")),
            b._backend_for_entry(_entry("somewhere")),
        )

    expect_ok(par(sc))


def test_model_endpoint_routed_model_needs_a_declaration(split):
    """Declared difference (no shipped default, L3): the legacy fallback to the registry default
    entry for a ``model-endpoint`` backend is unreachable in the final, which refuses first."""

    def sc(ctx):
        side = ctx.side
        b = side.backends
        _patch_registry(ctx)
        ctx.patch(side.models, "resolve_model", lambda model, endpoint=None: "registry-model")
        return outcome(b.routed_model, "slug", backend_name="model-endpoint")

    legacy, final = split(sc)
    assert expect_ok(expect_ok(legacy)) == "registry-model"
    expect_raise(expect_ok(final), "ConfigurationError", "No model is declared")


# --------------------------------------------------------------------------
# Deliberate differences
# --------------------------------------------------------------------------


def test_wrappers_final_only_surface(sides):
    legacy, final = sides
    from llm_kit.completion.factory_protocols import BackendSpec

    for name in (
        "OpenRouterBackend", "ModelEndpointBackend", "ClaudeCliBackend",
        "CodexCliBackend", "OpencodeCliBackend",
    ):
        l_fields = [f.name for f in dataclasses.fields(getattr(legacy.backends, name)) if f.init]
        f_fields = [f.name for f in dataclasses.fields(getattr(final.backends, name)) if f.init]
        assert f_fields == l_fields + ["factory"], name
    captured = []

    class Factory:
        def create(self, spec):
            captured.append(spec)
            return _delegate_class(response=None)()

    backend = final.backends.CodexCliBackend(
        default_timeout_s=4.0, argv_prefix=("a",), factory=Factory()
    )
    backend._backend()
    backend._backend()
    assert len(captured) == 1
    spec = captured[0]
    assert isinstance(spec, BackendSpec) and spec.backend == "codex-cli"
    assert spec.default_timeout_s == 4.0 and spec.argv_prefix == ("a",) and spec.runner is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.runner = len  # type: ignore[misc]


def test_none_timeout_and_retry_mean_adapter_default(sides):
    legacy, final = sides
    delegate = _delegate_class(response=None)
    from llm_kit.dispatch.pipeline_backends import ConfiguredBackendFactory

    factory = ConfiguredBackendFactory(classes={n: delegate for n in _CLASS_NAMES})
    final.backends.ClaudeCliBackend(
        default_timeout_s=None, retry_max_attempts=None, retry_cooldown_s=None,
        factory=factory,
    )._backend()
    final.backends.CodexCliBackend(default_timeout_s=None, factory=factory)._backend()
    claude_kwargs, codex_kwargs = delegate.built
    assert sorted(claude_kwargs) == ["diagnostics_dir", "executable"]
    assert sorted(codex_kwargs) == ["argv_prefix"]


def test_default_factory_is_the_configured_factory(sides):
    legacy, final = sides
    from llm_kit.dispatch import pipeline_backends

    seen = []

    class Recording(pipeline_backends.ConfiguredBackendFactory):
        def __init__(self, *a, **kw):
            seen.append("built")
            super().__init__({"openrouter": _delegate_class(response=None)})

    with mock.patch.object(final.backends, "ConfiguredBackendFactory", Recording):
        final.backends.OpenRouterBackend(endpoint="e")._backend()
    assert seen == ["built"]


def test_declaration_module_seam_names_the_routing_layer(sides):
    legacy, final = sides
    assert legacy.backends._declaration_module().__name__ == "llm_scripting_kit.declaration"
    assert final.backends._declaration_module().__name__ == "llm_kit.dispatch.declaration"
    for name in ("describe", "run", "RunRequest", "NoUsableRoutingTarget", "CALLER_PROCESS"):
        assert hasattr(final.backends._declaration_module(), name)


# --------------------------------------------------------------------------
# End to end through the real adapter (injected OpenAI-compatible client)
# --------------------------------------------------------------------------


class _FakeClient:
    """Records ``chat.completions.create`` calls; answers like the OpenAI SDK."""

    def __init__(self, content="router-response", finish_reason="stop"):
        self.sink = []
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.sink.append(kwargs)
                message = types.SimpleNamespace(content=content, reasoning_content=None)
                choice = types.SimpleNamespace(message=message, finish_reason=finish_reason)
                usage = types.SimpleNamespace(
                    prompt_tokens=100, completion_tokens=20,
                    prompt_tokens_details={"cached_tokens": 40},
                )
                return types.SimpleNamespace(choices=[choice], usage=usage)

        self.chat = types.SimpleNamespace(completions=_Completions())


def test_default_factory_drives_the_real_openai_compatible_adapter(par):
    def sc(ctx):
        p = ctx.side.platform
        client = _FakeClient()
        backend = ctx.side.backends.OpenRouterBackend(client=client)
        budget = p.CostBudget()
        resp = p.call_llm(
            backend, "sys", "user", model="test/slug",
            options=p.BackendOptions(max_tokens=256, temperature=0.7),
            pricing={"test/slug": {"input": 1.0, "output": 2.0}}, cost_budget=budget,
            cache_dir=ctx.work / "cache",
        )
        again = p.call_llm(
            backend, "sys", "user", model="test/slug",
            options=p.BackendOptions(max_tokens=256, temperature=0.7),
            pricing={"test/slug": {"input": 1.0, "output": 2.0}}, cost_budget=budget,
            cache_dir=ctx.work / "cache",
        )
        return resp, again, client.sink, budget.spent, backend.name, backend.classify_halt(
            RuntimeError("authentication_error")
        )

    expect_ok(par(sc))


def test_default_factory_refuses_a_reasoning_truncation_like_legacy(par):
    def sc(ctx):
        client = _FakeClient(content="", finish_reason="length")
        backend = ctx.side.backends.OpenRouterBackend(client=client)
        # Explicit: with no cap named the final sends none, and the message says so instead of
        # naming max_tokens=4096 (pinned in tests/llm-kit/test_pipeline_cap.py).
        options = ctx.side.platform.BackendOptions(max_tokens=4096)
        return backend.complete("s", "u", model="test/slug", options=options)

    expect_raise(par(sc), "RuntimeError", "finish_reason=length")


def test_default_factory_model_endpoint_refuses_an_unknown_entry_like_legacy(par):
    def sc(ctx):
        _patch_registry(ctx)
        client = _FakeClient()
        backend = ctx.side.backends.ModelEndpointBackend(endpoint="reg-x", client=client)
        resp = backend.complete("s", "u", model="test/slug")
        return resp, client.sink, backend._entry_id()

    expect_raise(par(sc), "EndpointResolveError", "unknown endpoint 'reg-x'")
