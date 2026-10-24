"""Differential parity: llm_kit.adapters vs the legacy llm_scripting_kit modules.

The legacy side is imported from plugins/llm-scripting-kit/lib with a sys.path
insert scoped to the import. Every scenario is a function of a "side" namespace
(legacy or final) and runs on both with the same inputs; the test asserts the
two outcomes are equal -- results, exception MRO names, messages and attributes,
stderr, and the temp files left behind.

Nothing here makes a network call or starts a real CLI: HTTP goes through a
patched ``urllib.request.urlopen``, subprocess backends through stub runners or
a fake ``subprocess.Popen``, the openai SDK through a fake module, and psutil
through a fake module and fake inspectors.

Covered modules (legacy -> final):

* completion/adapter_capabilities -> adapters.capabilities
* completion/endpoint_profile     -> adapters.endpoint_profile
* harness_adapters                -> adapters.harness_adapters
* completion/claude_runner        -> adapters.claude_runner
* completion/backends             -> adapters.backends
* completion/codex_backend        -> adapters.codex_backend
* completion/opencode_backend     -> adapters.opencode_backend
* account / client                -> adapters.account / adapters.client
* swapper, _swapper_process, swapper_acceptance -> adapters.<same>
* the raw-reader slice of usage_budget (read_claude_pool, read_codex_pool and
  their helpers) -> adapters.usage_sources

Deliberate differences, each pinned by name:

* the CLI command spelled in messages names the llm-kit venv and executable
  (design: "Persisted-state compatibility"); messages are compared with both
  spellings replaced by ``<CLI>``;
* ``codex_backend`` reaches Budget/STATUS_*/POOL_* through ``registry.conserve``
  and ``read_codex_pool`` through ``adapters.usage_sources`` instead of the
  legacy ``usage_budget`` module;
* ``swapper.ProcessNotFound`` / ``InspectionIndeterminate`` / ``SwapperError``
  (and the EXIT_* constants) are defined in the leaf ``adapters._swapper_errors``
  (no swapper <-> _swapper_process cycle) and re-exported by ``swapper``;
* the final codex completion backend passes ``--json`` (usage events) and reads
  ``input_tokens``, ``cache_hit_tokens``, ``output_tokens``, ``total_tokens`` and
  ``reasoning_tokens`` from them; the legacy backend does neither. The recording
  runner drops ``--json`` from the argv it records so the rest of the argv is
  still compared on both sides, ``reasoning_tokens`` is a final-only
  ``LLMResponse`` field, and ``tests/llm-kit/test_codex_usage_json.py`` pins the
  final side (``--json`` present, counts parsed);
* messages name llm-kit where the legacy text named llm-scripting-kit or one of
  its bare verbs: ``final_wording.RENAMES`` pins each by name and
  ``test_final_messages.py`` proves each against the live legacy and final
  sources, so a rename cannot stand in for an unrelated difference.
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import urllib.error
import urllib.request
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from unittest import mock

import pytest

from test_cli_harness import shipped_openrouter_without_style  # noqa: F401 -- fixture
from final_wording import claude_contract_to_final, codex_network_to_final, to_final

_REPO = Path(__file__).resolve().parents[2]
_LEGACY_LIB = _REPO / "plugins" / "llm-scripting-kit" / "lib"


# --------------------------------------------------------------------------
# Loading both sides
# --------------------------------------------------------------------------

# attribute -> (legacy module under llm_scripting_kit, final module)
_MODULES = {
    "types": ("completion.types", "llm_kit.completion.types"),
    "halt": ("completion.halt", "llm_kit.completion.halt"),
    "caps": ("completion.capabilities", "llm_kit.completion.capabilities"),
    "contract_types": ("completion.contract_types", "llm_kit.completion.contract_types"),
    "skill_types": ("completion.skill_context_types", "llm_kit.completion.skill_context_types"),
    "acap": ("completion.adapter_capabilities", "llm_kit.adapters.capabilities"),
    "profile": ("completion.endpoint_profile", "llm_kit.adapters.endpoint_profile"),
    "runner": ("completion.claude_runner", "llm_kit.adapters.claude_runner"),
    "backends": ("completion.backends", "llm_kit.adapters.backends"),
    "codex": ("completion.codex_backend", "llm_kit.adapters.codex_backend"),
    "opencode": ("completion.opencode_backend", "llm_kit.adapters.opencode_backend"),
    "harness": ("harness_adapters", "llm_kit.adapters.harness_adapters"),
    "account": ("account", "llm_kit.adapters.account"),
    "client": ("client", "llm_kit.adapters.client"),
    "swapper": ("swapper", "llm_kit.adapters.swapper"),
    "sproc": ("_swapper_process", "llm_kit.adapters._swapper_process"),
    "sacc": ("swapper_acceptance", "llm_kit.adapters.swapper_acceptance"),
    "usage": ("usage_budget", "llm_kit.adapters.usage_sources"),
    "me": ("model_endpoints", "llm_kit.registry.model_endpoints"),
    "models": ("models", "llm_kit.registry.models"),
    "effort": ("effort", "llm_kit.registry.effort"),
    "constants": ("constants", "llm_kit.registry.constants"),
    "conserve": ("usage_budget", "llm_kit.registry.conserve"),
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
    finally:
        sys.path.remove(str(_LEGACY_LIB))
    final = {attr: importlib.import_module(fin) for attr, (_leg, fin) in _MODULES.items()}
    return (
        types.SimpleNamespace(name="legacy", **legacy),
        types.SimpleNamespace(name="final", **final),
    )


# --------------------------------------------------------------------------
# Outcome normalisation
# --------------------------------------------------------------------------

_MASKED_FIELDS = frozenset({"wall_ms", "started_at", "ended_at"})

# Declared difference: the final Capabilities record carries ``call_modes`` (which
# modes an adapter honors); the frozen legacy record has no such field. Both the
# dataclass shape and its comparable value leave the field out, and a dedicated
# test below pins the final side's values.
# The final LLMResponse also carries ``reported_model`` and the ``estimated_cost_*`` pair (a
# downstream-reported model id and a non-authoritative cost figure); the frozen legacy one has neither.
_FINAL_ONLY_FIELDS = frozenset({
    "call_modes", "reported_model", "estimated_cost_usd", "estimated_cost_source", "reasoning_tokens",
})
# Declared difference (cost-class metadata): the final EndpointEntry carries ``billing_owner`` and
# ``endpoint``, consumed only by llm_kit.registry.cost; the frozen legacy one has neither. Scoped to
# the class, because ``endpoint`` is an ordinary field name elsewhere.
_FINAL_ONLY_CLASS_FIELDS = {"EndpointEntry": frozenset({"billing_owner", "endpoint"})}
_FINAL_ONLY_CLASS_PARAMS = {
    "EndpointEntry": (", billing_owner: 'Optional[str]' = None", ", endpoint: 'Optional[str]' = None"),
}
_CALL_MODES_PARAM = ", call_modes: 'Tuple[str, ...]' = ('completion',)"
# The final LLMResponse also carries ``reasoning_tokens`` (the reasoning share of ``output_tokens``,
# filled by the codex transport from its --json usage events); the frozen legacy one has no such field.
# LLMResponse's final-only trailing parameters (see _FINAL_ONLY_FIELDS).
_RESPONSE_EXTRA_PARAMS = (
    ", reported_model: 'Optional[str]' = None",
    ", estimated_cost_usd: 'Optional[float]' = None",
    ", estimated_cost_source: 'Optional[str]' = None",
    ", reasoning_tokens: 'int' = 0",
)


def _without_final_only_params(text: str) -> str:
    text = text.replace(_CALL_MODES_PARAM, "")
    for param in _RESPONSE_EXTRA_PARAMS:
        text = text.replace(param, "")
    return text
_TEXT_SUBS: list[str] = []


def _text(value: str) -> str:
    for sub in _TEXT_SUBS:
        value = value.replace(sub, "<CLI>")
    # named deliberate wording differences (final_wording.RENAMES); idempotent
    return to_final(value)


def _drop_setting_sources(items: list) -> list:
    """``items`` without the adjacent ``--setting-sources`` ``""`` argv pair that llm-kit's
    claude-cli text-only mode adds (``final_wording.RENAMES`` covers the emitted text)."""
    out: list = []
    for item in items:
        out.append(item)
        if item == "" and len(out) >= 2 and out[-2] == "--setting-sources":
            del out[-2:]
    return out


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
        value = codex_network_to_final(value)  # llm-kit advertises the explicit network-off control
        out = {"__type__": type(value).__name__}
        skipped = _FINAL_ONLY_CLASS_FIELDS.get(type(value).__name__, frozenset())
        for f in dataclasses.fields(value):
            if f.name in _FINAL_ONLY_FIELDS or f.name in skipped:
                continue
            item = getattr(value, f.name)
            out[f.name] = "<masked>" if f.name in _MASKED_FIELDS else norm(item)
        return out
    if isinstance(value, Mapping):
        return ("map", type(value).__name__, [(norm(k), norm(v)) for k, v in value.items()])
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, _drop_setting_sources([norm(v) for v in value]))
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


def pre(value):
    """Normalise now (a recorder snapshots its arguments) and mark it already done."""
    done = norm(value)
    return _Out(done) if isinstance(done, tuple) else done


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


def view(value):
    """Undo ``norm``'s tagging for dicts, lists and tuples so a test can index it."""
    if isinstance(value, tuple) and len(value) == 3 and value[0] == "map":
        return {view(k): view(v) for k, v in value[2]}
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and value[0] in ("list", "tuple", "set", "frozenset")
        and isinstance(value[1], list)
    ):
        return [view(v) for v in value[1]]
    if isinstance(value, tuple):
        return tuple(view(v) for v in value)
    if isinstance(value, list):
        return [view(v) for v in value]
    if isinstance(value, dict):
        return {k: view(v) for k, v in value.items()}
    return value


def expect_ok(res):
    assert res[0] == "ok", res
    return res[1]


def expect_raise(res, name: str, contains: str = ""):
    assert res[0] == "raise", res
    assert name in res[1], res
    assert contains in res[2], res
    return res


# --------------------------------------------------------------------------
# Harness fixtures
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
        "CLAUDE_BOOTSTRAP_DATA_ROOT", "OPENCODE_CONFIG_CONTENT", "LOCAL_KEY",
        "LLM_SCRIPTING_KIT_USAGE_SESSION",
    ):
        monkeypatch.delenv(name, raising=False)
    # api_key binds the user-layer .env path at import, from the REAL home.
    for side in sides:
        monkeypatch.setattr(side.api_key, "USER_ENV_FILE", home / "no-user-layer.env")


class _DetTemp:
    """Deterministic ``tempfile.mkstemp``: same names per side, leftovers visible."""

    def __init__(self, base: Path):
        self.base = base
        self.n = 0

    def reset(self) -> None:
        self.n = 0

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.base.iterdir())

    def __call__(self, suffix=None, prefix=None, dir=None, text=False):
        self.n += 1
        path = self.base / f"{prefix or 'tmp'}{self.n}{suffix or ''}"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
        return fd, str(path)


@pytest.fixture
def par(sides, tmp_path, monkeypatch, capsys):
    """``par(scenario)``: run on both sides, assert equal, return the legacy outcome."""
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
    tmp_dir = tmp_path / "_tmpfiles"
    tmp_dir.mkdir()
    det = _DetTemp(tmp_dir)
    monkeypatch.setattr(tempfile, "mkstemp", det)

    def run(scenario):
        seen = []
        for side in sides:
            det.reset()
            capsys.readouterr()
            res = outcome(scenario, side)
            err = capsys.readouterr().err
            seen.append((res, err, det.leftovers()))
            for leftover in tmp_dir.iterdir():
                leftover.unlink()
        if seen[1] != seen[0]:
            pytest.fail("final differs from legacy at " + first_diff(list(seen[0]), list(seen[1])))
        assert seen[0][0][0] == "ok", seen[0]
        return view(seen[0][0][1])

    return run


# --------------------------------------------------------------------------
# Public-surface comparison
# --------------------------------------------------------------------------

_SIG_PREFIX = re.compile(r"llm_scripting_kit\.|llm_kit\.[a-z_]+\.")


_FN_ADDR = re.compile(r"<function (\w+) at 0x[0-9a-fA-F]+>")


def _sig(fn, owner: str = "") -> str:
    text = _without_final_only_params(_SIG_PREFIX.sub("X.", str(inspect.signature(fn))))
    for param in _FINAL_ONLY_CLASS_PARAMS.get(owner, ()):
        text = text.replace(param, "")
    # Deliberate: codex_backend names Budget through registry.conserve.
    text = text.replace("usage_budget.Budget", "conserve.Budget")
    return _FN_ADDR.sub(r"<function \1>", text)


def _simple(value) -> bool:
    return isinstance(
        value, (str, int, float, bool, bytes, tuple, frozenset, set, list, dict, Path, type(None))
    )


def shape(value):
    """Comparable description of one module-level name."""
    if isinstance(value, type):
        class_skipped = _FINAL_ONLY_CLASS_FIELDS.get(value.__name__, frozenset())
        out = {
            "kind": "class",
            "name": value.__name__,
            "mro": [c.__name__ for c in value.__mro__],
            "methods": {},
            "attrs": {},
        }
        if dataclasses.is_dataclass(value):
            params = value.__dataclass_params__
            out["dataclass"] = (params.frozen, params.eq, params.order)
            out["fields"] = [
                (
                    f.name,
                    _SIG_PREFIX.sub("X.", str(f.type)),
                    f.init,
                    f.default if f.default is not dataclasses.MISSING and _simple(f.default) else "<d>",
                )
                for f in dataclasses.fields(value)
                if f.name not in _FINAL_ONLY_FIELDS and f.name not in class_skipped
            ]
        for key, item in vars(value).items():
            if key.startswith("__") and key.endswith("__") and key not in ("__init__", "__call__"):
                continue
            if dataclasses.is_dataclass(value) and (key in _FINAL_ONLY_FIELDS or key in class_skipped):
                continue
            raw = item.__func__ if isinstance(item, (staticmethod, classmethod)) else item
            if inspect.isfunction(raw):
                with contextlib.suppress(ValueError, TypeError):
                    out["methods"][key] = _sig(raw, value.__name__)
            elif isinstance(item, property):
                out["methods"][key] = "<property>"
            elif _simple(item):
                out["attrs"][key] = norm(item)
        return out
    if inspect.isfunction(value):
        return {"kind": "function", "name": value.__name__, "sig": _sig(value)}
    if inspect.ismodule(value):
        return {"kind": "module"}
    if _simple(value):
        return {"kind": "value", "value": norm(value)}
    if isinstance(value, re.Pattern):
        return {"kind": "value", "value": norm(value)}
    if callable(value):
        return {"kind": "callable", "name": getattr(value, "__name__", type(value).__name__)}
    return {"kind": "value", "value": norm(value)}


def public_surface(mod):
    return {
        name: shape(value)
        for name, value in vars(mod).items()
        if not (name.startswith("__") and name.endswith("__")) and name != "annotations"
    }


def assert_same_surface(legacy_mod, final_mod, *, only_legacy=(), only_final=()):
    left, right = public_surface(legacy_mod), public_surface(final_mod)
    assert set(left) - set(right) == set(only_legacy), sorted(set(left) - set(right))
    assert set(right) - set(left) == set(only_final), sorted(set(right) - set(left))
    for name in sorted(set(left) & set(right)):
        assert right[name] == left[name], name


# (attr, names only the legacy module has, names only the final module has)
_SURFACE_CASES = [
    ("acap", (), ("MODE_COMPLETION", "MODE_WORKSPACE")),
    ("profile", (), ()),
    # Legacy-only: the legacy modules reach Codex through ``bootstrap_lib.codex`` and carry the
    # message and requirement constants for an interpreter whose katsoft-llm-kit is absent or old;
    # the final modules import ``codex_command`` directly and have nothing to be absent.
    ("harness", ("LLM_KIT_CODEX_MIN", "LLM_KIT_CODEX_REQUIREMENT", "_codex_unavailable", "codex_import_failure"),
     ("check_effort", "codex_command")),
    ("runner", (), ()),
    # Final-only: the Claude effort check (check_effort over the harness-wide menu) and the
    # estimated-cost labels / reported-model reader that fill LLMResponse's final-only fields.
    ("backends", (), ("CLAUDE_EFFORT_ORDER", "CLAUDE_HARNESS", "ESTIMATE_SOURCE_CLAUDE_CLI",
                      "ESTIMATE_SOURCE_USAGE_COST", "_claude_reported_model", "_untrusted_native_cost",
                      "check_effort")),
    # The final backend delegates to llm_kit.adapters.codex_command directly, so
    # the legacy probe constant, probe function and its signature import are gone.
    ("codex", ("IGNORE_USER_CONFIG_BOOTSTRAP", "_codex_argv_builder", "codex_import_failure", "inspect",
               "usage_budget"),
     ("CODEX_EFFORT_ORDER", "CODEX_HARNESS", "check_effort", "codex_command", "conserve", "usage_sources",
      "CodexUsage", "_usage_count", "parse_codex_usage_events")),
    ("opencode", (), ()),
    ("account", (), ()),
    ("client", (), ()),
    ("swapper", (), ()),
    ("sproc", (), ()),
    ("sacc", (), ()),
]


@pytest.mark.parametrize("attr,only_legacy,only_final", _SURFACE_CASES)
def test_module_surface_is_identical(sides, attr, only_legacy, only_final):
    legacy, final = sides
    assert_same_surface(
        getattr(legacy, attr), getattr(final, attr),
        only_legacy=only_legacy, only_final=only_final,
    )


_USAGE_SLICE_CALLABLES = (
    "_epoch", "_used_percentage", "_verdict", "_load_json", "_read_model_scoped",
    "read_claude_pool", "_newest_codex_rollout", "_CodexRateLimitReading",
    "_last_codex_rate_limits", "_find_rate_limits", "_find_usage_limit_message",
    "_parse_reset_text", "read_codex_pool",
)
_USAGE_SLICE_DATA = (
    "_HOUR", "_DAY", "_CLAUDE_WINDOW_SECONDS", "CLAUDE_SNAPSHOT", "CODEX_SESSIONS_DIR",
    "_PACE_EPSILON", "_CODEX_TAIL_BYTES", "_CODEX_EXHAUSTION_LATCH_SECONDS", "_RESET_TEXT_RE",
)
_USAGE_POLICY = (
    "evaluate", "evaluate_usage_budget", "pinned_evaluate", "session_key",
    "record_observed_halt", "quota_pool_key", "VERDICT_CACHE", "_load_cache", "_store_cache",
)


def test_usage_sources_carry_exactly_the_raw_reader_slice(sides):
    legacy, final = sides
    left, right = public_surface(legacy.usage), public_surface(final.usage)
    for name in _USAGE_SLICE_CALLABLES + _USAGE_SLICE_DATA:
        assert right[name] == left[name], name
    defined = {
        name
        for name, value in vars(final.usage).items()
        if (inspect.isfunction(value) or isinstance(value, type))
        and getattr(value, "__module__", None) == final.usage.__name__
    }
    assert defined == set(_USAGE_SLICE_CALLABLES)
    for name in _USAGE_POLICY:
        assert name in left, name
        assert name not in right, name


# --------------------------------------------------------------------------
# Capability tables
# --------------------------------------------------------------------------


def test_filled_capability_tables_are_identical(par):
    def scenario(S):
        return {
            "table": S.acap.ADAPTER_CAPABILITIES,
            "accessor": S.acap.adapter_capabilities(),
            "families": [
                S.acap.OPENROUTER_CAPABILITIES, S.acap.CLAUDE_CAPABILITIES,
                S.acap.CODEX_CAPABILITIES, S.acap.OPENCODE_CAPABILITIES,
            ],
            "claude_flags": S.acap._CLAUDE_SYSTEM_PROMPT_FLAGS,
            "claude_text_only": S.acap._CLAUDE_TEXT_ONLY_ARGS,
            "codex_text_only": [
                S.acap._CODEX_TEXT_ONLY_CATALOG_DROP, S.acap._CODEX_TEXT_ONLY_CATALOG_SET,
                S.acap._CODEX_TEXT_ONLY_CONFIG,
            ],
        }

    result = par(scenario)
    assert sorted(result["table"]) == ["claude-cli", "codex-cli", "opencode-cli", "openrouter"]
    assert result["table"]["openrouter"]["adapter"] == "openrouter"
    assert result["claude_flags"]["append"].startswith("--")


def test_adapter_capability_keys_are_the_four_backend_names(sides):
    for side in sides:
        assert sorted(side.acap.ADAPTER_CAPABILITIES) == [
            "claude-cli", "codex-cli", "opencode-cli", "openrouter",
        ]


def test_accessor_returns_a_copy(sides):
    for side in sides:
        first = side.acap.adapter_capabilities()
        first.clear()
        assert side.acap.adapter_capabilities()


# --------------------------------------------------------------------------
# Endpoint profiles
# --------------------------------------------------------------------------

_REGISTRY = (
    "models:\n"
    "  gpu:\n    base_url: http://gpu.invalid/v1\n    model: gpu-m\n"
    "    reasoning_effort: high\n"
    "    routing: {group: q, effort_style: ninfer}\n"
    "  plain:\n    base_url: http://plain.invalid/v1\n    model: m\n"
    "  trusted:\n    base_url: http://trusted.invalid/v1\n    model: tm\n"
    "    frontdoor: true\n    billing: {mode: provider-reported}\n"
    "  local:\n    base_url: http://localhost:9/v1\n    model: lm\n    key_env: LOCAL_KEY\n"
    "  h:\n    harness: codex\n    model: m\n"
)


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / "reg.yaml"
    path.write_text(_REGISTRY, encoding="utf-8")
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(path))
    return path


@pytest.mark.usefixtures("shipped_openrouter_without_style")
def test_resolve_endpoint_profile_over_a_registry(par, registry):
    def scenario(S):
        return {
            name: outcome(S.profile.resolve_endpoint_profile, name)
            for name in ("gpu", "plain", "trusted", "h", "no-such-entry", None)
        }

    result = par(scenario)
    gpu = dict(result["gpu"][1])
    assert gpu["declared_effort"] == "high"


def test_resolve_endpoint_profile_survives_a_broken_registry(par, tmp_path, monkeypatch):
    path = tmp_path / "broken.yaml"
    path.write_text("models: [not, a, map]\n", encoding="utf-8")
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(path))

    def scenario(S):
        return S.profile.resolve_endpoint_profile("gpu")

    result = par(scenario)
    assert result["effort"]["source"] == "none"


def test_profile_from_entry_resolved_and_unresolved(par, registry):
    def scenario(S):
        entry = S.me.EndpointEntry(
            id="gpu", base_url="http://gpu.invalid/v1", model="gpu-m",
            reasoning_effort="high",
            routing=S.me.RoutingConfig(group="q", effort_style="ninfer", effort_style_declared=True),
        )
        return {
            "entry": S.profile.profile_from_entry(entry),
            "same_as_resolved": S.profile.profile_from_entry(entry)
            == S.profile.resolve_endpoint_profile("gpu"),
            "resolved": S.profile.profile_from_resolved(
                {"name": "x", "request_defaults": {"reasoning_effort": "low"},
                 "effort_style": "top-level", "effort_style_source": "frontdoor"}
            ),
            "bare": S.profile.profile_from_resolved({"name": "y"}),
            "bare_named": S.profile.profile_from_resolved({"name": "y"}, endpoint="z"),
            "unresolved": S.profile.unresolved_profile("u"),
            "delivered": S.profile.profile_from_entry(entry).delivered_effort,
        }

    result = par(scenario)
    assert result["delivered"] == "xhigh"


def test_endpoint_capabilities_specialization(par):
    def scenario(S):
        deliver = S.profile.EndpointProfile("gpu", S.effort.EffortDelivery("ninfer", "routing"), "medium")
        nothing = S.profile.EndpointProfile("p", S.effort.EffortDelivery(None, "none"))
        unsupported = S.profile.EndpointProfile("p", S.effort.EffortDelivery("unsupported", "endpoint"))
        out = {}
        for fam in ("OPENROUTER_CAPABILITIES", "CLAUDE_CAPABILITIES", "CODEX_CAPABILITIES"):
            record = getattr(S.acap, fam)
            out[fam] = {
                "deliver": S.profile.endpoint_capabilities(record, deliver),
                "none_is_same": S.profile.endpoint_capabilities(record, nothing) is record,
                "unsupported_is_same": S.profile.endpoint_capabilities(record, unsupported) is record,
                "no_profile_is_same": S.profile.endpoint_capabilities(record, None) is record,
            }
        return out

    result = par(scenario)
    router = result["OPENROUTER_CAPABILITIES"]
    assert router["deliver"]["endpoint"] == "gpu"
    assert "effort" in router["deliver"]["params"]
    assert router["none_is_same"] and router["unsupported_is_same"] and router["no_profile_is_same"]


# --------------------------------------------------------------------------
# Harness adapters
# --------------------------------------------------------------------------


def _harness_entry(S, **kw):
    base = dict(id="h", base_url=None, model="gpt-x", kind=S.me.HARNESS_KIND, harness="codex")
    base.update(kw)
    return S.me.EndpointEntry(**base)


def test_codex_adapter_builds_the_same_argv(par, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    out_file = tmp_path / "out.txt"
    prompt_file = tmp_path / "brief.txt"
    prompt_file.write_text("brief from file", encoding="utf-8")

    def scenario(S):
        adapter = S.harness.CodexAdapter(argv_prefix=("codex",))
        entry = _harness_entry(S)
        eff_entry = _harness_entry(S, effort="high")
        bad_eff_entry = _harness_entry(S, effort="bogus")
        return {
            "plain": outcome(adapter.build_argv, entry, root, prompt="p"),
            "effort_default": outcome(adapter.build_argv, eff_entry, root, prompt="p"),
            "effort_override": outcome(adapter.build_argv, eff_entry, root, prompt="p", effort="low"),
            "bad_effort": outcome(adapter.build_argv, bad_eff_entry, root, prompt="p"),
            "bad_override": outcome(adapter.build_argv, entry, root, prompt="p", effort=""),
            "output_and_dirs": outcome(
                adapter.build_argv, entry, root, prompt="p", output_file=out_file,
                add_dirs=[tmp_path, root],
            ),
            "relative_cwd": outcome(adapter.build_argv, entry, "rel/dir", prompt="p"),
            "relative_output": outcome(adapter.build_argv, entry, root, prompt="p", output_file="o.txt"),
            "relative_add_dir": outcome(adapter.build_argv, entry, root, prompt="p", add_dirs=["x"]),
            "both_prompts": outcome(adapter.build_argv, entry, root, prompt="p", prompt_file=prompt_file),
            "non_text_prompt": outcome(adapter.build_argv, entry, root, prompt=5),
            "invocation_prompt": outcome(adapter.build_invocation, entry, root, prompt="inline"),
            "invocation_file": outcome(adapter.build_invocation, entry, root, prompt_file=prompt_file),
            "invocation_none": outcome(adapter.build_invocation, entry, root),
            "invocation_missing_file": outcome(
                adapter.build_invocation, entry, root, prompt_file=tmp_path / "missing.txt"
            ),
            "wrong_kind": outcome(
                adapter.build_argv, _harness_entry(S, kind=S.me.TRANSPORT_KIND, harness=None,
                                                   base_url="http://x/v1"), root, prompt="p"),
            "wrong_harness": outcome(adapter.build_argv, _harness_entry(S, harness="opencode"), root, prompt="p"),
            "no_model": outcome(adapter.build_argv, _harness_entry(S, model=""), root, prompt="p"),
            "menu": (adapter.accepted_efforts(), S.harness.CODEX_EFFORT_MENU),
            "name": adapter.name,
        }

    result = par(scenario)
    plain = result["plain"]
    assert plain[0] == "ok"
    assert plain[1][:2] == ["codex", "exec"]
    assert "gpt-x" in plain[1]
    assert result["bad_effort"][0] == "raise"
    assert result["relative_cwd"][0] == "raise"


def test_codex_adapter_detection_goes_through_the_canonical_module(par, tmp_path, monkeypatch):
    """Both sides detect through llm_kit.adapters.codex_command (legacy via the alias)."""
    from llm_kit.adapters import codex_command

    root = tmp_path / "root"
    root.mkdir()
    calls = []

    def fake_detect():
        calls.append("detect")
        return types.SimpleNamespace(available=len(calls) % 2 == 1)

    monkeypatch.setattr(codex_command, "detect_codex", fake_detect)

    def scenario(S):
        calls.clear()
        adapter = S.harness.CodexAdapter(argv_prefix=("codex",))
        entry = _harness_entry(S)
        return {
            "first": adapter.is_available(),
            "second": adapter.is_available(),
            "argv": outcome(adapter.build_argv, entry, root, prompt="p"),
        }

    result = par(scenario)
    assert result["first"] is True and result["second"] is False
    assert result["argv"][0] == "ok"


def test_opencode_adapter_builds_the_same_argv(par, tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    import shutil

    def scenario(S):
        entry = _harness_entry(S, harness="opencode")
        adapter = S.harness.OpencodeAdapter(argv_prefix=("opencode",))
        out = {
            "plain": outcome(adapter.build_argv, entry, root, prompt="p"),
            "variant": outcome(adapter.build_argv, entry, root, prompt="p", effort="whatever"),
            "entry_effort": outcome(adapter.build_argv, _harness_entry(S, harness="opencode", effort="max"), root, prompt="p"),
            "empty_effort": outcome(adapter.build_argv, entry, root, prompt="p", effort=""),
            "add_dirs": outcome(adapter.build_argv, entry, root, prompt="p", add_dirs=[root]),
            "output_file": outcome(adapter.build_argv, entry, root, prompt="p", output_file=tmp_path / "o"),
            "relative_cwd": outcome(adapter.build_argv, entry, "rel", prompt="p"),
            "wrong_harness": outcome(adapter.build_argv, _harness_entry(S), root, prompt="p"),
            "invocation": outcome(adapter.build_invocation, entry, root, prompt="hello"),
            "menu": adapter.accepted_efforts(),
        }
        with monkeypatch.context() as m:
            m.setattr(shutil, "which", lambda name: None)
            out["no_cli"] = outcome(S.harness.OpencodeAdapter().build_argv, entry, root, prompt="p")
            out["resolve_none"] = S.harness.resolve_opencode_cli()
            out["detect_none"] = S.harness.detect_opencode()
            out["available_none"] = S.harness.OpencodeAdapter().is_available()
        with monkeypatch.context() as m:
            m.setattr(shutil, "which", lambda name: "/opt/bin/opencode")
            out["resolve_exe"] = S.harness.resolve_opencode_cli()
            out["detect_exe"] = S.harness.detect_opencode()
            out["argv_resolved"] = outcome(S.harness.OpencodeAdapter().build_argv, entry, root, prompt="p")
        with monkeypatch.context() as m:
            m.setattr(shutil, "which", lambda name: "C:\\tools\\opencode.cmd")
            out["resolve_cmd"] = S.harness.resolve_opencode_cli()
        return out

    result = par(scenario)
    assert result["plain"][0] == "ok"
    assert result["menu"] is None
    assert result["no_cli"][0] == "raise"
    assert result["add_dirs"][0] == "raise"


def test_resolve_harness_adapter(par, tmp_path):
    def scenario(S):
        return {
            "codex": S.harness.resolve_harness_adapter(_harness_entry(S)),
            "opencode": S.harness.resolve_harness_adapter(_harness_entry(S, harness="opencode")),
            "unknown": outcome(S.harness.resolve_harness_adapter, _harness_entry(S, harness="gemini")),
            "none_harness": outcome(S.harness.resolve_harness_adapter, _harness_entry(S, harness=None)),
            "transport": outcome(
                S.harness.resolve_harness_adapter,
                _harness_entry(S, kind=S.me.TRANSPORT_KIND, harness=None, base_url="http://x/v1"),
            ),
            "known": S.harness.KNOWN_HARNESSES,
            "effort_order": [S.harness.harness_effort_order(h) for h in ("claude", "codex", "opencode")],
            "unknown_order": outcome(S.harness.harness_effort_order, "gemini"),
        }

    result = par(scenario)
    assert result["unknown"][0] == "raise"


def test_harness_adapter_error_is_the_registry_class(sides):
    legacy, final = sides
    assert final.harness.HarnessAdapterError is final.effort.HarnessAdapterError
    assert final.harness.HarnessAdapterError.__mro__[1] is ValueError


# --------------------------------------------------------------------------
# claude_runner (fake Popen)
# --------------------------------------------------------------------------


class _FakeStdin:
    def __init__(self, error):
        self.error = error
        self.written = []
        self.closed = False

    def write(self, text):
        if self.error is not None:
            raise self.error
        self.written.append(text)

    def close(self):
        self.closed = True


class _FakeProc:
    def __init__(self, script, cmd, kwargs):
        self.script = script
        self.stdin = _FakeStdin(script.get("stdin_error"))
        self.stdout = iter(script.get("stdout", []))
        self.stderr = iter(script.get("stderr", []))
        self.returncode = script.get("rc", 0)
        self.killed = False
        script["_proc"] = self

    def wait(self, timeout=None):
        if self.script.get("hang") and not self.killed:
            time.sleep(0.005)
            raise subprocess.TimeoutExpired("fake", timeout or 0)
        return self.returncode

    def kill(self):
        self.killed = True


@pytest.fixture
def fake_popen(monkeypatch):
    state = {"script": {}, "log": []}

    def factory(cmd, **kwargs):
        state["log"].append({"cmd": list(cmd), "kwargs": pre(kwargs)})
        return _FakeProc(state["script"], cmd, kwargs)

    monkeypatch.setattr(subprocess, "Popen", factory)
    return state


def test_looks_like_hard_stop(par):
    def scenario(S):
        probe = S.runner.looks_like_hard_stop
        return {
            "markers": S.runner.HARD_STOP_STDERR_MARKERS,
            "empty": probe(""),
            "limit": probe("You HIT YOUR LIMIT today"),
            "status": probe('x "api_error_status":429 y'),
            "auth": probe("Invalid Authentication Credentials"),
            "clean": probe("all good"),
            "custom": probe("boom", ("BOOM",)),
            "none": probe("boom", ()),
            "alias": S.runner.run_claude_streaming is S.runner.run_cli_streaming,
        }

    result = par(scenario)
    assert result["limit"] is True and result["clean"] is False and result["alias"] is True


def test_run_cli_streaming_scripts(par, fake_popen, tmp_path):
    def scenario(S):
        out = {}

        def run(key, script, **kwargs):
            fake_popen["script"] = dict(script)
            fake_popen["log"].clear()
            args = dict(log_prefix="[t]", timeout_s=5.0)
            args.update(kwargs)
            result = outcome(S.runner.run_cli_streaming, ["tool", "-x"], "the request", tmp_path, **args)
            proc = fake_popen["script"].get("_proc")
            out[key] = {
                "result": result,
                "popen": list(fake_popen["log"]),
                "stdin": None if proc is None else (proc.stdin.written, proc.stdin.closed),
                "killed": None if proc is None else proc.killed,
            }

        run("ok", {"stdout": ["a\n", "b\n"], "stderr": ["warn\n"], "rc": 3})
        run("env", {"stdout": ["x"]}, env={"A": "1"}, label="other cli")
        run("hard_stop", {"stderr": ['{"api_error_status":429}\n'], "stdout": ["partial"]})
        run("hard_stop_custom_label", {"stderr": ["hit your limit\n"]}, label="codex exec")
        run("hard_stop_disabled", {"stderr": ["hit your limit\n"], "stdout": ["ok"]}, hard_stop_markers=())
        run("custom_markers", {"stderr": ["fatal-x\n"]}, hard_stop_markers=("fatal-x",))
        run("timeout", {"stdout": ["so far"], "stderr": ["slow\n"], "hang": True}, timeout_s=0.03)
        run("broken_pipe", {"stdout": ["ok"], "stdin_error": BrokenPipeError()})
        return out

    result = par(scenario)
    assert result["ok"]["result"][0] == "ok"
    assert result["ok"]["result"][1] == ["a\nb\n", "warn\n", 3]
    assert result["ok"]["stdin"] == [["the request"], True]
    assert result["env"]["popen"][0]["kwargs"]  # env and cwd reach Popen
    for key in ("hard_stop", "hard_stop_custom_label", "custom_markers"):
        assert result[key]["result"][0] == "raise", key
        assert "RuntimeError" in result[key]["result"][1], key
        assert result[key]["killed"] is True, key
    assert "codex exec hard-stop error" in result["hard_stop_custom_label"]["result"][2]
    assert result["hard_stop_disabled"]["result"][0] == "ok"
    assert "AgentTimeoutError" in result["timeout"]["result"][1]
    assert result["timeout"]["result"][4]["stdout"] == "so far"
    assert result["broken_pipe"]["result"][0] == "ok"


# --------------------------------------------------------------------------
# Shared request-building fakes
# --------------------------------------------------------------------------

_SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "integer"}},
    "required": ["a"],
    "additionalProperties": False,
}


def _contract(S, policy, schema=_SCHEMA):
    if policy == "text-only":
        schema = None
    return S.contract_types.OutputContract(id="demo.contract", policy=policy, schema=schema)


def _skill(S, text="SKILL BLOCK", digest=None):
    report = S.skill_types.SkillContextReport(
        resolver="resolver", report_schema="schema", format_version="1",
        digest=digest or sha256(text.encode("utf-8")).hexdigest(),
        estimated_tokens=3, token_budget=100, token_estimate="chars/4", skills=1,
        provenance={"k": "v"},
    )
    return S.skill_types.SkillContext(text=text, report=report)


class _FakeClient:
    """An OpenAI-SDK-shaped client: records create() kwargs and with_options()."""

    def __init__(self, response=None, raises=None):
        self.response = response
        self.raises = raises
        self.calls = []
        self.options = []
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(pre(kwargs))
        if self.raises is not None:
            raise self.raises
        return self.response

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self


def _chat_response(text="hi", finish="stop", reasoning=None, usage="default"):
    if usage == "default":
        usage = types.SimpleNamespace(
            prompt_tokens=11, completion_tokens=7, prompt_tokens_details={"cached_tokens": 4}
        )
    message = types.SimpleNamespace(content=text, reasoning_content=reasoning)
    choice = types.SimpleNamespace(message=message, finish_reason=finish)
    return types.SimpleNamespace(choices=[choice], usage=usage)


@pytest.fixture
def named_registry(tmp_path, monkeypatch):
    path = tmp_path / "reg.yaml"
    path.write_text(_REGISTRY, encoding="utf-8")
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(path))
    return path


# --------------------------------------------------------------------------
# OpenRouterBackend
# --------------------------------------------------------------------------


def test_openrouter_request_building(par, named_registry):
    def scenario(S):
        out = {}

        def run(key, response=None, *, options=None, model="vendor/model", system="SYS",
                user="USER", raises=None, **backend_kwargs):
            client = _FakeClient(response if response is not None else _chat_response(), raises)
            backend = S.backends.OpenRouterBackend(client=client, **backend_kwargs)
            out[key] = {
                "res": outcome(backend.complete, system, user, model=model, options=options),
                "calls": client.calls,
                "options": client.options,
            }

        opts = S.types.BackendOptions
        run("plain")
        run("no_system", system="")
        run("cache_prefix", options=opts(user_cache_prefix="PRE", temperature=0.2, timeout_s=30,
                                         max_tokens=99, client_id="cid"))
        run("client_id_blank", options=opts(client_id=""))
        run("retries_zero", options=opts(max_retries=0))
        run("retries_negative", options=opts(max_retries=-1))
        run("retries_bool", options=opts(max_retries=True))
        run("extras", options=opts(extras={"top_k": 5, "seed": 1}))
        run("effort_plain_endpoint", options=opts(effort="high"), endpoint="plain")
        run("effort_gpu_endpoint", options=opts(effort="high", extras={"top_k": 1}), endpoint="gpu")
        run("effort_gpu_extras_win", options=opts(effort="high", extras={"reasoning_effort": "low"}),
            endpoint="gpu")
        run("effort_gpu_opt_out", options=opts(effort="high", extras={"reasoning_effort": None}),
            endpoint="gpu")
        run("unknown_endpoint", options=opts(effort="high"), endpoint="no-such")
        run("reasoning", _chat_response("answer", reasoning="because"))
        run("empty_stop", _chat_response("", finish="stop"))
        run("empty_length", _chat_response("", finish="length"))
        run("empty_length_reasoning", _chat_response("", finish="length", reasoning="hmm"))
        run("no_usage", _chat_response(usage=None))
        run("cache_hit_attr", _chat_response(usage=types.SimpleNamespace(
            prompt_tokens=5, completion_tokens=2,
            prompt_tokens_details=types.SimpleNamespace(cached_tokens=3))))
        run("cache_hit_deepseek", _chat_response(usage=types.SimpleNamespace(
            prompt_tokens=5, completion_tokens=2, prompt_cache_hit_tokens=2)))
        cost = types.SimpleNamespace(prompt_tokens=5, completion_tokens=2, cost=0.5)
        run("cost_untrusted", _chat_response(usage=cost), endpoint="plain")
        run("cost_trusted", _chat_response(usage=cost), endpoint="trusted")
        run("cost_trusted_unmetered", _chat_response(usage=types.SimpleNamespace(
            prompt_tokens=5, completion_tokens=2, cost=0.0, cost_source="registry-unmetered")),
            endpoint="trusted")
        run("cost_invalid", _chat_response(usage=types.SimpleNamespace(
            prompt_tokens=5, completion_tokens=2, cost=-1)), endpoint="trusted")
        run("cost_dict_usage", _chat_response(usage={"cost": 0.5}), endpoint="trusted")
        run("contract_validated_ok", _chat_response('{"a": 1}'),
            options=opts(output_contract=_contract(S, "validated-result")))
        run("contract_validated_bad", _chat_response("not json"),
            options=opts(output_contract=_contract(S, "validated-result")))
        run("contract_native", _chat_response('{"a": 1}'),
            options=opts(output_contract=_contract(S, "native-required")))
        run("contract_text_only", _chat_response("plain text"),
            options=opts(output_contract=_contract(S, "text-only")))
        run("skill_context", options=opts(skill_context=_skill(S)))
        run("skill_context_bad_digest", options=opts(skill_context=_skill(S, digest="0" * 64)))
        run("skill_context_wrong_type", options=opts(skill_context="not a context"))
        run("raises", raises=RuntimeError("boom 429"))
        run("alias_model", model="no-such-alias")
        return out

    result = par(scenario)
    plain = result["plain"]["res"]
    assert plain[0] == "ok" and plain[1]["text"] == "hi"
    assert result["plain"]["calls"][0]["messages"][0]["content"][0]["cache_control"]
    assert result["no_system"]["calls"][0]["messages"][0]["content"] == ""
    assert result["cache_prefix"]["calls"][0]["user"] == "cid"
    assert "user" not in result["client_id_blank"]["calls"][0]
    assert result["retries_zero"]["options"] == [{"max_retries": 0}]
    assert result["retries_negative"]["res"][0] == "raise"
    assert result["retries_bool"]["res"][0] == "raise"
    assert result["extras"]["res"][1]["forwarded_params"]
    assert result["effort_gpu_endpoint"]["calls"][0]["extra_body"]
    assert "EmptyCompletionError" in result["empty_length"]["res"][1]
    assert result["empty_stop"]["res"][0] == "ok"
    assert result["cost_trusted"]["res"][1]["reported_cost_usd"] == 0.5
    assert result["cost_untrusted"]["res"][1]["reported_cost_usd"] is None
    assert result["contract_validated_ok"]["res"][0] == "ok"
    assert result["contract_validated_bad"]["res"][0] == "raise"
    assert result["skill_context"]["calls"][0]["messages"][0]["content"][0]["text"].startswith("SKILL BLOCK")
    assert result["skill_context_bad_digest"]["res"][0] == "raise"
    assert result["skill_context_wrong_type"]["res"][0] == "raise"


@pytest.mark.usefixtures("shipped_openrouter_without_style")
def test_openrouter_endpoint_introspection(par, named_registry):
    def scenario(S):
        out = {}
        opts = S.types.BackendOptions
        for endpoint in (None, "gpu", "plain", "trusted", "h", "no-such"):
            backend = S.backends.OpenRouterBackend(client=_FakeClient(), endpoint=endpoint)
            out[str(endpoint)] = {
                "profile": outcome(backend.endpoint_profile),
                "capabilities": outcome(backend.endpoint_capabilities),
                "plan_none": outcome(backend.effort_plan),
                "plan_effort": outcome(backend.effort_plan, opts(effort="high")),
                "plan_extras": outcome(
                    backend.effort_plan, opts(effort="medium", extras={"reasoning_effort": "low"})
                ),
                "report": outcome(backend.params_report, opts(effort="high", extras={"top_k": 1})),
                "trusts_cost": outcome(backend._trusts_native_cost),
            }
        backend = S.backends.OpenRouterBackend(client=_FakeClient())
        out["halt"] = {
            "rate": backend.classify_halt(RuntimeError("429 rate limit")),
            "auth": backend.classify_halt(type("AuthenticationError", (Exception,), {})("401")),
            "none": backend.classify_halt(ValueError("plain")),
        }
        out["name"] = (backend.name, S.backends.OpenRouterBackend.capabilities is S.acap.OPENROUTER_CAPABILITIES)
        return out

    result = par(scenario)
    assert result["gpu"]["profile"][0] == "ok"
    assert result["name"] == ["openrouter", True]


def test_openrouter_lazy_client_build(par, monkeypatch, named_registry):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("LOCAL_KEY", "local-secret")
    built = []
    fake_client = _FakeClient(_chat_response("built"))

    def fake_openai(**kwargs):
        built.append(kwargs)
        return fake_client

    module = types.ModuleType("openai")
    module.OpenAI = fake_openai
    monkeypatch.setitem(sys.modules, "openai", module)

    def scenario(S):
        built.clear()
        fake_client.calls.clear()
        out = {}
        backend = S.backends.OpenRouterBackend()
        out["first"] = outcome(backend.complete, "s", "u", model="vendor/model")
        out["second"] = outcome(backend.complete, "s", "u", model="vendor/model")
        out["reused"] = backend.client is fake_client
        out["built"] = list(built)
        built.clear()
        keyed = S.backends.OpenRouterBackend(endpoint="local")
        out["keyed"] = outcome(keyed.complete, "s", "u", model="vendor/model")
        out["keyed_built"] = list(built)
        out["calls"] = list(fake_client.calls)
        return out

    result = par(scenario)
    assert result["first"][0] == "ok" and result["reused"] is True
    assert len(result["built"]) == 1
    assert result["built"][0]["api_key"] == "sk-test"
    assert result["keyed_built"][0]["api_key"] == "local-secret"


def test_openrouter_without_openai_refuses(par, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setitem(sys.modules, "openai", None)

    def scenario(S):
        backend = S.backends.OpenRouterBackend()
        return outcome(backend.complete, "s", "u", model="vendor/model")

    result = par(scenario)
    assert result[0] == "raise" and "ImportError" in result[1]
    assert "openai" in result[2]


# --------------------------------------------------------------------------
# ClaudeCliBackend
# --------------------------------------------------------------------------


def _envelope(result="hello", **extra):
    data = {"result": result, "is_error": False}
    data.update(extra)
    return json.dumps(data)


class _StubRunner:
    """Records every runner call; serves queued ``(stdout, stderr, rc)`` or raises."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, cmd, request, cwd, **kwargs):
        self.calls.append(
            {"cmd": list(cmd), "request": request, "cwd": str(cwd), "kwargs": pre(dict(kwargs))}
        )
        item = self.outputs.pop(0)
        if callable(item):
            item = item()
        if isinstance(item, BaseException):
            raise item
        return item


def test_claude_cli_request_building(par, tmp_path):
    def scenario(S):
        out = {}
        opts = S.types.BackendOptions

        def run(key, outputs, *, options=None, system="SYS", user="USER", model="claude-opus-4-8",
                **backend_kwargs):
            runner = _StubRunner(outputs)
            backend = S.backends.ClaudeCliBackend(
                runner=runner, executable=backend_kwargs.pop("executable", "claude"), **backend_kwargs
            )
            out[key] = {
                "res": outcome(backend.complete, system, user, model=model, options=options),
                "calls": runner.calls,
            }

        ok = [(_envelope("hello", usage={"input_tokens": 10, "output_tokens": 5,
                                         "cache_read_input_tokens": 3}), "", 0)]
        run("happy", ok)
        run("options", [(_envelope(), "", 0)], options=opts(
            effort="medium", allowed_tools="Read", disallowed_tools="Bash Edit", timeout_s=300,
            log_prefix="[gallery]", cwd=tmp_path, system_prompt_mode="append"))
        run("bad_mode", [(_envelope(), "", 0)], options=opts(system_prompt_mode="sideways"))
        run("text_only", [(_envelope(), "", 0)], text_only=True)
        run("text_only_tools", [(_envelope(), "", 0)], text_only=True, options=opts(allowed_tools="Read"))
        run("default_timeout", [(_envelope(), "", 0)], default_timeout_s=42.0)
        run("hard_stop_429", [(json.dumps({"result": "You have hit your limit", "api_error_status": 429}), "", 0)])
        run("hard_stop_401_no_body", [(json.dumps({"result": "", "api_error_status": 401}), "", 0)])
        run("hard_stop_body", [(_envelope("please check authentication_error now"), "", 0)])
        run("is_error", [(json.dumps({"result": "bad thing", "is_error": True}), "", 0)])
        run("nonzero", [("out", "err", 2)])
        run("no_json", [("not json", "", 0)])
        run("no_result", [(json.dumps({"x": 1}), "", 0)])
        run("transient_once", [(json.dumps({"result": "", "api_error_status": 503}), "", 0)])
        run("transient_retry_then_ok", [
            (json.dumps({"result": "", "api_error_status": 502}), "", 0),
            (_envelope("recovered"), "", 0),
        ], retry_max_attempts=2, retry_cooldown_s=0.0)
        run("transient_exhausted", [
            (json.dumps({"result": "", "api_error_status": 500}), "", 1),
            (json.dumps({"result": "", "api_error_status": 500}), "", 0),
        ], retry_max_attempts=2, retry_cooldown_s=0.0)
        # A schema contract is delivered by prompt on the legacy side and natively
        # (--json-schema) on the final side by design; the final side is asserted in
        # test_claude_text_only_settings.py (test_claude_cli_native_contract_*).
        run("contract_text_only", [(_envelope("txt"), "", 0)],
            options=opts(output_contract=_contract(S, "text-only")))
        run("skill_context", [(_envelope(), "", 0)], options=opts(skill_context=_skill(S)))
        run("runner_raises", [RuntimeError("spawn failed")])
        run("extras_dropped", [(_envelope(), "", 0)], options=opts(
            extras={"a": 1}, temperature=0.5, max_tokens=10, cache_salt=3))
        backend = S.backends.ClaudeCliBackend(executable="claude")
        out["misc"] = {
            "name": backend.name,
            "text_only_clone": [
                backend.with_text_only().text_only,
                backend.with_text_only() is not backend,
                S.backends.ClaudeCliBackend(text_only=True).with_text_only().text_only,
            ],
            "halt": [backend.classify_halt(RuntimeError("hit your limit")),
                     backend.classify_halt(RuntimeError('"api_error_status":401')),
                     backend.classify_halt(ValueError("neutral"))],
            "runner_default": backend.runner is S.runner.run_claude_streaming,
            "capabilities": backend.capabilities is S.acap.CLAUDE_CAPABILITIES,
        }
        return out

    result = par(scenario)
    cmd = result["happy"]["calls"][0]["cmd"]
    assert cmd[:2] == ["claude", "-p"] and "--system-prompt" in cmd
    assert result["happy"]["res"][1]["input_tokens"] == 10
    assert "--effort" in result["options"]["calls"][0]["cmd"]
    assert "--append-system-prompt" in result["options"]["calls"][0]["cmd"]
    assert result["bad_mode"]["res"][0] == "raise"
    assert result["text_only"]["res"][0] == "ok"
    assert result["text_only_tools"]["res"][0] == "raise"
    assert result["hard_stop_429"]["res"][0] == "raise"
    assert result["transient_once"]["res"][0] == "raise"
    assert result["transient_retry_then_ok"]["res"][1]["attempts"] == 2
    assert len(result["transient_exhausted"]["calls"]) == 2
    assert result["skill_context"]["res"][0] == "raise"
    assert result["misc"]["runner_default"] is True
    assert result["default_timeout"]["calls"][0]["kwargs"]


def test_claude_cli_executable_resolution(par, monkeypatch):
    import shutil

    def scenario(S):
        out = {}
        for key, found in (("missing", None), ("found", "/opt/bin/claude")):
            with monkeypatch.context() as m:
                m.setattr(shutil, "which", lambda name, _f=found: _f)
                runner = _StubRunner([(_envelope(), "", 0)])
                backend = S.backends.ClaudeCliBackend(runner=runner)
                out[key] = {"res": outcome(backend.complete, "s", "u", model="m"), "calls": runner.calls}
        return out

    result = par(scenario)
    assert result["missing"]["res"][0] == "raise"
    assert result["found"]["calls"][0]["cmd"][0] == "/opt/bin/claude"


def test_claude_cli_claudx_edge(par, monkeypatch):
    """REFUSE edge: under claudx the bootstrap_lib.claudx module is required."""
    monkeypatch.setenv("CLAUDE_PLUGIN_TEST", "1")

    def scenario(S):
        out = {}

        def run(key):
            runner = _StubRunner([(_envelope(), "", 0)])
            backend = S.backends.ClaudeCliBackend(runner=runner, executable="claude")
            out[key] = {"res": outcome(backend.complete, "s", "u", model="m"), "calls": runner.calls}

        good = types.ModuleType("bootstrap_lib.claudx")
        good.nested_claude_args = lambda: ["--plugin-dir", "/dev/plugin"]
        old = types.ModuleType("bootstrap_lib.claudx")
        for key, setup in (
            ("present", lambda m: m.setitem(sys.modules, "bootstrap_lib.claudx", good)),
            ("symbol_missing", lambda m: m.setitem(sys.modules, "bootstrap_lib.claudx", old)),
            ("module_missing", lambda m: m.setitem(sys.modules, "bootstrap_lib.claudx", None)),
            ("lib_missing", lambda m: (m.setitem(sys.modules, "bootstrap_lib", None),
                                       m.setitem(sys.modules, "bootstrap_lib.claudx", None))),
        ):
            with monkeypatch.context() as m:
                setup(m)
                run(key)
        with monkeypatch.context() as m:
            m.delenv("CLAUDE_PLUGIN_TEST")
            m.setitem(sys.modules, "bootstrap_lib.claudx", None)
            run("outside_claudx")
        return out

    result = par(scenario)
    assert result["present"]["calls"][0]["cmd"][:4] == ["claude", "--plugin-dir", "/dev/plugin", "-p"]
    for key in ("symbol_missing", "module_missing", "lib_missing"):
        assert result[key]["res"][0] == "raise", key
        assert "ClaudxSupportError" in result[key]["res"][1], key
        assert result[key]["calls"] == [], key
    assert result["outside_claudx"]["res"][0] == "ok"


def test_claude_cli_timeout_diagnostics(par, tmp_path, monkeypatch):
    monkeypatch.setattr(time, "strftime", lambda fmt, *a: "STAMP")
    blocker = tmp_path / "diag-blocker"
    blocker.write_text("a file, not a directory", encoding="utf-8")

    def scenario(S):
        out = {}
        diag = tmp_path / f"diag-{S.name}"

        def timeout():
            return S.runner.AgentTimeoutError(
                "timed out", cmd=["claude", "-p", "--system-prompt", "SECRET SYSTEM", "x"],
                elapsed_s=7, stdout="partial out", stderr="partial err",
            )

        for key, target in (("dump", diag), ("disabled", None), ("unwritable", blocker / "sub")):
            runner = _StubRunner([timeout])
            backend = S.backends.ClaudeCliBackend(runner=runner, executable="claude", diagnostics_dir=target)
            out[key] = {"res": outcome(backend.complete, "SECRET SYSTEM", "u", model="m")}
        out["files"] = sorted(
            (p.name, p.read_text(encoding="utf-8")) for p in diag.iterdir()
        ) if diag.exists() else []
        return out

    result = par(scenario)
    assert result["dump"]["res"][0] == "raise" and "AgentTimeoutError" in result["dump"]["res"][1]
    assert result["files"] and "<13 chars>" in result["files"][0][1]
    assert "SECRET SYSTEM" not in result["files"][0][1]


def test_agent_timeout_error_is_the_halt_class(sides):
    legacy, final = sides
    import llm_kit.completion.halt as neutral

    assert final.runner.AgentTimeoutError is neutral.AgentTimeoutError
    assert final.opencode.AgentTimeoutError is neutral.AgentTimeoutError
    assert final.backends.AgentTimeoutError is neutral.AgentTimeoutError
    assert [c.__name__ for c in final.runner.AgentTimeoutError.__mro__] == [
        c.__name__ for c in legacy.runner.AgentTimeoutError.__mro__
    ]


# --------------------------------------------------------------------------
# CodexCliBackend
# --------------------------------------------------------------------------

_FUTURE = "2099-01-01T00:00:00Z"


class _CodexRunner:
    """A stub ``codex`` runner: serves ``debug models`` and writes the ``-o`` file."""

    def __init__(self, output="codex-answer", result=("", "", 0), raises=None, delete_output=False,
                 catalog=None):
        self.output = output
        self.result = result
        self.raises = raises
        self.delete_output = delete_output
        self.catalog = catalog
        self.calls = []

    def __call__(self, cmd, request, cwd, **kwargs):
        # Declared difference: the final backend adds --json (see the module docstring).
        record = {"cmd": [part for part in cmd if part != "--json"], "request": request, "cwd": str(cwd),
                  "kwargs": pre(dict(kwargs))}
        if "--output-schema" in cmd:
            record["schema_file"] = Path(cmd[cmd.index("--output-schema") + 1]).read_text(encoding="ascii")
        if "-c" in cmd:
            pairs = [cmd[i + 1] for i in range(len(cmd) - 1) if cmd[i] == "-c"]
            for pair in pairs:
                if pair.startswith("model_catalog_json="):
                    path = Path(pair.split("=", 1)[1].strip("'"))
                    record["catalog_file"] = path.read_text(encoding="ascii")
        self.calls.append(record)
        if cmd[-2:] == ["debug", "models"]:
            return self.catalog if self.catalog is not None else ("{}", "", 0)
        if self.raises is not None:
            raise self.raises
        out_path = Path(cmd[cmd.index("-o") + 1])
        if self.delete_output:
            out_path.unlink()
        elif self.output is not None:
            out_path.write_text(self.output, encoding="utf-8")
        return self.result


def _rollout(directory: Path, events: list, name="rollout-1.jsonl") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def _healthy_event(used=10.0):
    return {"timestamp": _FUTURE, "payload": {"rate_limits": {
        "primary": {"used_percent": used, "window_minutes": 300, "resets_at": 4070908800},
        "secondary": {"used_percent": 1.0, "window_minutes": 10080, "resets_at": 4070908800},
        "credits": {"has_credits": False, "unlimited": False},
    }}}


def _exhausted_events(message=None, timestamp=_FUTURE):
    events = [{"timestamp": timestamp, "payload": {"rate_limits": {
        "primary": None, "secondary": None, "credits": {"has_credits": False, "unlimited": False},
    }}}]
    if message is not None:
        events.append({"timestamp": timestamp, "payload": {"error": {
            "codex_error_info": "usage_limit_exceeded", "message": message}}})
    return events


def test_codex_token_total_and_prompt_folding(par):
    def scenario(S):
        parse = S.codex.parse_codex_token_total
        return {
            "parse": [parse(t) for t in (
                "", "nothing here", "tokens used\n14,214", "tokens used 9", "x tokens used: 1,000 y",
                "tokens used\n\n  \n42\nlater 7", "tokens used\nnone here", "TOKENS USED 5\ntokens used 6",
            )],
            "compose": [S.codex.compose_prompt("sys", "user"), S.codex.compose_prompt("", "user"),
                        S.codex.compose_prompt("sys", "")],
            "constants": [S.codex.PROMPT_SEPARATOR, S.codex.CODEX_EXTRA_KEYS],
        }

    result = par(scenario)
    assert result["parse"][:3] == [None, None, 14214]


def test_codex_request_building(par, tmp_path):
    schema_file = tmp_path / "schema.json"
    schema_file.write_text("{}", encoding="utf-8")
    sessions = tmp_path / "sessions"

    def scenario(S):
        out = {}
        opts = S.types.BackendOptions

        def run(key, runner=None, *, options=None, system="SYS", user="USER", model="gpt-x",
                **backend_kwargs):
            runner = runner or _CodexRunner()
            backend = S.codex.CodexCliBackend(argv_prefix=("codex",), runner=runner, **backend_kwargs)
            out[key] = {
                "res": outcome(backend.complete, system, user, model=model, options=options),
                "calls": runner.calls,
            }

        run("plain")
        run("tokens", _CodexRunner(result=("", "warn\ntokens used\n1,234\n", 0)))
        run("no_system", system="")
        run("options", options=opts(
            effort="high", cwd=tmp_path, timeout_s=77, log_prefix="[x]", temperature=0.3,
            max_tokens=5, allowed_tools="Read", system_prompt_mode="append", cache_salt=2,
            extras={"scratch_dir": str(tmp_path / "scratch"), "add_dirs": [str(tmp_path / "a")],
                    "sandbox": "read-only", "network": False, "unknown_key": 1}))
        run("output_schema_extra", _CodexRunner(output='{"a": 1}'),
            options=opts(extras={"output_schema": str(schema_file)}))
        run("output_schema_bad_json", _CodexRunner(output="not json"),
            options=opts(extras={"output_schema": str(schema_file)}))
        run("default_timeout", default_timeout_s=11.0)
        run("empty_model", model="")
        run("relative_cwd", options=opts(cwd=Path("rel")))
        run("nonzero_no_rollout", _CodexRunner(result=("o", "e", 7)), sessions_dir=sessions / "none")
        _rollout(sessions / "healthy", [_healthy_event()])
        run("nonzero_healthy", _CodexRunner(result=("o", "e", 7)), sessions_dir=sessions / "healthy")
        _rollout(sessions / "exhausted_text", _exhausted_events("try again at Sep 19th, 2099 3:34 PM."))
        run("nonzero_exhausted_text", _CodexRunner(result=("o", "e", 7)),
            sessions_dir=sessions / "exhausted_text")
        _rollout(sessions / "exhausted_latch", _exhausted_events())
        run("nonzero_exhausted_latch", _CodexRunner(result=("o", "e", 7)),
            sessions_dir=sessions / "exhausted_latch")
        _rollout(sessions / "exhausted_stale", _exhausted_events(timestamp="2001-01-01T00:00:00Z"))
        run("nonzero_exhausted_stale", _CodexRunner(result=("o", "e", 7)),
            sessions_dir=sessions / "exhausted_stale")
        run("output_missing", _CodexRunner(delete_output=True))
        run("output_empty", _CodexRunner(output="  \n"))
        run("runner_timeout", _CodexRunner(raises=S.runner.AgentTimeoutError(
            "slow", cmd=["codex"], elapsed_s=3, stdout="o", stderr="e")))
        run("runner_raises", _CodexRunner(raises=RuntimeError("spawn failed")))
        run("contract_validated", _CodexRunner(output='{"a": 1}'),
            options=opts(output_contract=_contract(S, "validated-result")))
        run("contract_violation", _CodexRunner(output="nope"),
            options=opts(output_contract=_contract(S, "validated-result")))
        run("contract_native", _CodexRunner(output='{"a": 1}'),
            options=opts(output_contract=_contract(S, "native-required")))
        run("contract_native_beside_extras", _CodexRunner(output='{"a": 1}'),
            options=opts(output_contract=_contract(S, "native-required"),
                         extras={"output_schema": str(schema_file)}))
        run("contract_non_strict_schema", _CodexRunner(output="{}"), options=opts(
            output_contract=_contract(S, "native-required", {"type": "object", "properties": {"a": {"type": "integer"}}})))
        run("contract_text_only", _CodexRunner(output="txt"),
            options=opts(output_contract=_contract(S, "text-only")))
        run("skill_context", options=opts(skill_context=_skill(S)))
        backend = S.codex.CodexCliBackend(argv_prefix=("codex",))
        err_quota = S.codex.CodexRunError("m", halt_kind="quota", resets_at=5)
        err_plain = S.codex.CodexRunError("hit your limit")
        out["misc"] = {
            "name": backend.name,
            "text_only_clone": [backend.with_text_only().text_only, backend.with_text_only() is not backend],
            "halt": [backend.classify_halt(err_quota), backend.classify_halt(err_plain),
                     backend.classify_halt(RuntimeError("neutral"))],
            "error_attrs": [vars(err_quota), vars(err_plain)],
            "capabilities": backend.capabilities is S.acap.CODEX_CAPABILITIES,
            "runner_default": backend.runner is S.runner.run_cli_streaming,
        }
        return out

    result = par(scenario)
    assert result["plain"]["res"][0] == "ok"
    argv = result["plain"]["calls"][0]["cmd"]
    assert argv[:2] == ["codex", "exec"] and argv[-1] == "-"
    assert result["plain"]["calls"][0]["request"] == "SYS\n\n---\n\nUSER"
    assert result["tokens"]["res"][1]["total_tokens"] == 1234
    assert "--output-schema" not in argv
    assert result["options"]["res"][0] == "ok"
    assert "extras.unknown_key" in result["options"]["res"][1]["dropped_params"]
    assert result["output_schema_extra"]["res"][1]["structured"] == {"a": 1}
    assert result["output_schema_bad_json"]["res"][1]["structured"] is None
    assert result["relative_cwd"]["res"][0] == "raise"
    assert result["nonzero_no_rollout"]["res"][4].get("halt_kind") is None
    assert result["nonzero_healthy"]["res"][4].get("halt_kind") is None
    for key in ("nonzero_exhausted_text", "nonzero_exhausted_latch"):
        assert result[key]["res"][4]["halt_kind"] == "quota", key
        assert result[key]["res"][4]["resets_at"], key
    assert result["nonzero_exhausted_stale"]["res"][4].get("halt_kind") is None
    assert "wrote no output file" in result["output_missing"]["res"][2]
    assert "is empty" in result["output_empty"]["res"][2]
    assert "AgentTimeoutError" in result["runner_timeout"]["res"][1]
    assert result["contract_validated"]["res"][0] == "ok"
    assert result["contract_violation"]["res"][0] == "raise"
    assert result["contract_native"]["calls"][0]["schema_file"]
    assert result["contract_native_beside_extras"]["res"][0] == "raise"
    assert result["contract_non_strict_schema"]["res"][0] == "raise"
    assert result["skill_context"]["res"][0] == "raise"
    assert result["misc"]["halt"][0] == "quota"


def test_codex_text_only_mode(par, tmp_path, monkeypatch):
    import shutil

    catalog = json.dumps({"models": [
        {"slug": "gpt-x", "apply_patch_tool_type": "freeform", "other": 1},
        {"slug": "other-model"},
    ]})

    def scenario(S):
        out = {}
        opts = S.types.BackendOptions

        def run(key, runner, *, options=None, model="gpt-x", **backend_kwargs):
            backend = S.codex.CodexCliBackend(
                runner=runner, text_only=True, **({"argv_prefix": ("codex",)} | backend_kwargs)
            )
            out[key] = {
                "res": outcome(backend.complete, "SYS", "USER", model=model, options=options),
                "calls": runner.calls,
            }

        run("ok", _CodexRunner(catalog=(catalog, "", 0)))
        run("sandbox_read_only_ok", _CodexRunner(catalog=(catalog, "", 0)),
            options=opts(extras={"sandbox": "read-only"}))
        run("sandbox_conflict", _CodexRunner(catalog=(catalog, "", 0)),
            options=opts(extras={"sandbox": "workspace-write"}))
        run("network_conflict", _CodexRunner(catalog=(catalog, "", 0)), options=opts(extras={"network": True}))
        run("no_model", _CodexRunner(catalog=(catalog, "", 0)), model="")
        run("catalog_nonzero", _CodexRunner(catalog=("", "boom", 3)))
        run("catalog_not_json", _CodexRunner(catalog=("<html>", "", 0)))
        run("catalog_unlisted_model", _CodexRunner(catalog=(catalog, "", 0)), model="absent")
        run("catalog_not_mapping", _CodexRunner(catalog=("[1, 2]", "", 0)))
        with monkeypatch.context() as m:
            m.setattr(shutil, "which", lambda name: None)
            run("prefix_unresolved", _CodexRunner(catalog=(catalog, "", 0)), argv_prefix=None)
        with monkeypatch.context() as m:
            m.setattr(shutil, "which", lambda name: "/usr/local/bin/codex")
            run("prefix_resolved", _CodexRunner(catalog=(catalog, "", 0)), argv_prefix=None)
        return out

    result = par(scenario)
    ok_calls = result["ok"]["calls"]
    assert ok_calls[0]["cmd"][-2:] == ["debug", "models"]
    assert "--ignore-user-config" in ok_calls[1]["cmd"]
    assert "gpt-x" in ok_calls[1]["catalog_file"] and "other-model" not in ok_calls[1]["catalog_file"]
    assert result["ok"]["res"][0] == "ok"
    assert "text-only-mode" in result["ok"]["res"][1]["execution_controls_applied"]
    for key in ("sandbox_conflict", "network_conflict", "no_model", "catalog_nonzero",
                "catalog_not_json", "catalog_unlisted_model", "prefix_unresolved"):
        assert result[key]["res"][0] == "raise", key
    assert result["sandbox_read_only_ok"]["res"][0] == "ok"
    assert result["catalog_not_mapping"]["res"][0] == "raise"
    assert result["prefix_resolved"]["res"][0] == "ok"


def test_codex_backend_builds_through_the_canonical_module(par, tmp_path, monkeypatch):
    """Plain and text-only calls build through codex_command on both sides.

    The legacy backend reaches the same module through the bootstrap_lib.codex
    alias; the final backend imports it directly. The builder accepts
    ``ignore_user_config``, so text-only mode runs and no version probe exists.
    """
    catalog = json.dumps({"models": [{"slug": "gpt-x"}]})

    def scenario(S):
        out = {}

        def run(key, runner, **backend_kwargs):
            backend = S.codex.CodexCliBackend(argv_prefix=("codex",), runner=runner, **backend_kwargs)
            out[key] = {"res": outcome(backend.complete, "SYS", "USER", model="gpt-x"), "calls": runner.calls}

        run("plain", _CodexRunner())
        run("text_only", _CodexRunner(catalog=(catalog, "", 0)), text_only=True)
        return out

    result = par(scenario)
    assert result["plain"]["res"][0] == "ok"
    assert result["text_only"]["res"][0] == "ok"
    assert "--ignore-user-config" in result["text_only"]["calls"][-1]["cmd"]


# --------------------------------------------------------------------------
# OpencodeCliBackend
# --------------------------------------------------------------------------


class _OpencodeRunner:
    def __init__(self, result=("answer text", "", 0), raises=None):
        self.result = result
        self.raises = raises
        self.calls = []

    def __call__(self, cmd, request, cwd, **kwargs):
        kwargs = dict(kwargs)
        env = kwargs.pop("env", None)
        record = {"cmd": list(cmd), "request": request, "cwd": str(cwd), "kwargs": pre(kwargs)}
        if env is not None:
            record["env"] = {
                "config": env.get("OPENCODE_CONFIG_CONTENT"),
                "new_keys": sorted(set(env) - set(os.environ)),
                "base_preserved": all(
                    env.get(k) == v for k, v in os.environ.items() if k != "OPENCODE_CONFIG_CONTENT"
                ),
            }
        else:
            record["env"] = None
        self.calls.append(record)
        if self.raises is not None:
            raise self.raises
        return self.result


def test_opencode_helpers(par):
    def scenario(S):
        mod = S.opencode
        env = mod._confined_opencode_env
        return {
            "compose": [mod.compose_prompt("s", "u"), mod.compose_prompt("", "u"), mod.compose_prompt("s", "")],
            "consts": [mod.OPENCODE_PROMPT_SEPARATOR, mod.DEFAULT_OPENCODE_TIMEOUT_S,
                       mod.OPENCODE_FILESYSTEM_POSTURE, mod.PROMPT_SEPARATOR],
            "denied": [mod.denied_permissions(v) for v in (
                None, "", "Edit", "Bash(git *) Task", "Read WebFetch", "Write,NotebookEdit")],
            "env_empty": outcome(env, {}),
            "env_denied": outcome(env, {}, "Edit Bash"),
            "env_merge": outcome(env, {"OPENCODE_CONFIG_CONTENT": json.dumps(
                {"model": "x", "permission": {"read": "allow"}, "agent": {"build": {"mode": "primary"}}})}),
            "env_bad_json": outcome(env, {"OPENCODE_CONFIG_CONTENT": "{nope"}),
            "env_not_object": outcome(env, {"OPENCODE_CONFIG_CONTENT": "[1]"}),
            "env_odd_shapes": outcome(env, {"OPENCODE_CONFIG_CONTENT": json.dumps(
                {"permission": 5, "agent": {"build": {"permission": "x"}}})}),
            "env_agent_not_dict": outcome(env, {"OPENCODE_CONFIG_CONTENT": json.dumps(
                {"agent": 7})}),
            "env_selected_not_dict": outcome(env, {"OPENCODE_CONFIG_CONTENT": json.dumps(
                {"agent": {"build": 3}})}),
            "entry": mod._entry_for_model("some-model"),
        }

    result = par(scenario)
    assert result["denied"][2] == {"edit": "deny"}
    assert result["env_bad_json"][0] == "raise"
    assert result["env_not_object"][0] == "raise"


def test_opencode_request_building(par, tmp_path, monkeypatch):
    import shutil

    def scenario(S):
        out = {}
        opts = S.types.BackendOptions

        def run(key, runner=None, *, options=None, system="SYS", user="USER", model="prov/model",
                **backend_kwargs):
            runner = runner or _OpencodeRunner()
            kwargs = {"argv_prefix": ("opencode",)} | backend_kwargs
            backend = S.opencode.OpencodeCliBackend(runner=runner, **kwargs)
            out[key] = {
                "res": outcome(backend.complete, system, user, model=model, options=options),
                "calls": runner.calls,
            }

        run("plain")
        run("no_system", system="")
        run("options", options=opts(effort="deep", cwd=tmp_path, timeout_s=33, log_prefix="[o]",
                                    extras={"k": 1, "j": 2}, temperature=0.4, allowed_tools="Read"))
        run("deny_known", options=opts(disallowed_tools="Edit Bash Task"))
        run("deny_unknown", options=opts(disallowed_tools="Read WebFetch"))
        run("deny_partial", options=opts(disallowed_tools="Edit Mystery"))
        run("default_timeout", default_timeout_s=9.0)
        run("relative_cwd", options=opts(cwd=Path("rel")))
        run("nonzero", _OpencodeRunner(result=("partial", "bad stuff", 4)))
        run("empty_stdout", _OpencodeRunner(result=("  \n", "err", 0)))
        run("runner_timeout", _OpencodeRunner(raises=S.runner.AgentTimeoutError(
            "slow out secret", cmd=["opencode"], elapsed_s=2, stdout="so", stderr="se")))
        run("runner_raises", _OpencodeRunner(raises=RuntimeError("spawn failed")))
        run("contract_validated", _OpencodeRunner(result=('{"a": 1}', "", 0)),
            options=opts(output_contract=_contract(S, "validated-result")))
        run("contract_violation", _OpencodeRunner(result=("nope", "", 0)),
            options=opts(output_contract=_contract(S, "validated-result")))
        run("contract_native", _OpencodeRunner(result=('{"a": 1}', "", 0)),
            options=opts(output_contract=_contract(S, "native-required")))
        run("skill_context", options=opts(skill_context=_skill(S)))
        adapter = S.harness.OpencodeAdapter(argv_prefix=("custom-open",))
        run("adapter_seam", adapter=adapter, argv_prefix=None)
        with monkeypatch.context() as m:
            m.setattr(shutil, "which", lambda name: None)
            run("no_cli", argv_prefix=None)
        with monkeypatch.context() as m:
            m.setenv("OPENCODE_CONFIG_CONTENT", json.dumps({"keep": "me"}))
            run("inline_config_merge", options=opts(disallowed_tools="Edit"))
        with monkeypatch.context() as m:
            m.setenv("OPENCODE_CONFIG_CONTENT", "{bad")
            run("inline_config_bad", _OpencodeRunner())
        backend = S.opencode.OpencodeCliBackend(argv_prefix=("opencode",))
        out["misc"] = {
            "name": backend.name,
            "posture": backend.filesystem_posture,
            "halt": [backend.classify_halt(RuntimeError("neutral")),
                     backend.classify_halt(S.opencode.OpencodeRunError("x", stdout="rate limit"))],
            "capabilities": backend.capabilities is S.acap.OPENCODE_CAPABILITIES,
            "runner_default": backend.runner is S.runner.run_cli_streaming,
            "run_error_attrs": vars(S.opencode.OpencodeRunError("m", stdout="o", stderr="e", returncode=3, cmd=["a"])),
        }
        return out

    result = par(scenario)
    plain = result["plain"]
    assert plain["res"][0] == "ok" and plain["res"][1]["text"] == "answer text"
    assert plain["calls"][0]["cmd"][:2] == ["opencode", "run"]
    assert plain["calls"][0]["env"]["base_preserved"] is True
    assert result["deny_known"]["res"][1]["execution_controls_applied"]
    assert "disallowed_tools" in result["deny_unknown"]["res"][1]["dropped_params"]
    assert "disallowed_tools" not in result["deny_partial"]["res"][1]["dropped_params"]
    assert result["relative_cwd"]["res"][0] == "raise"
    assert "OpencodeRunError" in result["nonzero"]["res"][1]
    assert "OpencodeRunError" in result["empty_stdout"]["res"][1]
    timeout = result["runner_timeout"]["res"]
    assert "AgentTimeoutError" in timeout[1] and "secret" not in timeout[2]
    assert result["contract_validated"]["res"][0] == "ok"
    assert result["contract_violation"]["res"][0] == "raise"
    assert result["skill_context"]["res"][0] == "raise"
    assert result["adapter_seam"]["calls"][0]["cmd"][0] == "custom-open"
    assert result["no_cli"]["res"][0] == "raise"
    assert "keep" in result["inline_config_merge"]["calls"][0]["env"]["config"]
    assert result["inline_config_bad"]["res"][0] == "raise"


# --------------------------------------------------------------------------
# account (HTTP through a patched urlopen)
# --------------------------------------------------------------------------


class _HttpResponse:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Urlopen:
    """A scripted ``urllib.request.urlopen``: records requests, serves per-URL results."""

    def __init__(self, default=b"{}"):
        self.script = {}
        self.default = default
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append({
            "url": request.full_url,
            "headers": sorted((k.lower(), v) for k, v in request.header_items()),
            "method": request.get_method(),
            "data": request.data,
            "timeout": timeout,
        })
        item = self.script.get(request.full_url, self.default)
        if isinstance(item, BaseException):
            raise item
        return _HttpResponse(item)


@pytest.fixture
def urlopen(monkeypatch):
    fake = _Urlopen()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


def _http_error(code, reason="reason"):
    return urllib.error.HTTPError("http://x", code, reason, {}, None)


def test_account_checks(par, urlopen):
    ok_body = json.dumps({"data": {"label": "key", "usage": 1.5, "limit": 10.0,
                                   "is_free_tier": False, "rate_limit": {"requests": 5}}}).encode()

    def scenario(S):
        out = {}
        base = "http://acct.invalid/api/v1"
        urlopen.calls.clear()

        def serve(item):
            urlopen.script = {f"{base}/auth/key": item, f"{base}/models": item}

        for key, item in (
            ("ok", ok_body), ("list_payload", b"[1, 2]"), ("no_data_key", b'{"x": 1}'),
            ("non_json", b"<html>"), ("non_utf8", b"\xff\xfe\x00"),
            ("e401", _http_error(401)), ("e402", _http_error(402)), ("e500", _http_error(500, "boom")),
            ("url_error", urllib.error.URLError("no route")),
            ("timeout", TimeoutError("slow")), ("os_error", OSError("dns")),
        ):
            serve(item)
            out[key] = {
                "account": outcome(S.account.check_account, "sk-1", timeout=3.0, base_url=base),
                "probe": outcome(S.account.check_models_probe, "sk-1", base_url=base, timeout=4.0),
            }
        serve(ValueError("malformed url"))
        out["value_error"] = {
            "account": outcome(S.account.check_account, "sk-1", base_url=base),
            "probe": outcome(S.account.check_models_probe, "sk-1", base_url=base),
        }
        serve(ok_body)
        out["empty_key"] = {
            "account": outcome(S.account.check_account, ""),
            "probe": outcome(S.account.check_models_probe, "", base_url=base),
            "probe_keyless": outcome(S.account.check_models_probe, "", base_url=base, keyless=True),
        }
        out["default_base"] = outcome(S.account.check_account, "sk-2")
        out["calls"] = list(urlopen.calls)
        return out

    result = par(scenario)
    assert result["ok"]["account"][1]["label"] == "key"
    assert result["e401"]["account"][1]["failure_reason"] == "auth"
    assert result["e402"]["probe"][1]["failure_reason"] == "no_credit"
    assert result["e500"]["account"][0] == "raise"
    assert result["non_utf8"]["account"][0] == "raise"
    assert result["value_error"]["account"][0] == "raise"
    assert result["empty_key"]["probe_keyless"][0] == "ok"
    keyless_call = [c for c in result["calls"] if c["headers"] == []]
    assert keyless_call and all("authorization" not in dict(c["headers"]) for c in keyless_call)


def test_validate_endpoint_modes(par, urlopen):
    def scenario(S):
        out = {}
        urlopen.calls.clear()
        base = "http://v.invalid/v1"
        for mode in ("openrouter", "models-probe", "none", "mystery", None):
            cfg = {"base_url": base, "key_env": "K", "account_check": mode}
            out[str(mode)] = outcome(S.account.validate_endpoint, cfg, "sk", timeout=2.0)
        out["keyless_probe"] = outcome(
            S.account.validate_endpoint,
            {"base_url": base, "key_env": None, "account_check": "models-probe"}, "", timeout=2.0)
        out["no_mode_key"] = outcome(S.account.validate_endpoint, {"base_url": base}, "sk")
        out["no_base"] = outcome(S.account.validate_endpoint, {"account_check": "none"}, "sk")
        out["calls"] = [(c["url"], c["headers"], c["timeout"]) for c in urlopen.calls]
        return out

    result = par(scenario)
    assert result["none"] == ("ok", None)
    assert result["openrouter"][0] == "ok" and result["no_base"][0] == "raise"


def test_endpoint_probes(par, urlopen, named_registry, monkeypatch):
    def scenario(S):
        out = {}
        urlopen.calls.clear()
        urlopen.script = {}
        for key, name in (("default", None), ("gpu", "gpu"), ("local", "local"),
                          ("missing", "no-such"), ("harness", "h")):
            with monkeypatch.context() as m:
                m.setenv("OPENROUTER_API_KEY", "sk-or")
                m.setenv("LOCAL_KEY", "sk-local")
                out[key] = outcome(S.account.probe_endpoint, name, timeout=1.5)
        with monkeypatch.context() as m:
            out["local_no_key"] = outcome(S.account.probe_endpoint, "local")
        urlopen.script = {
            "http://gpu.invalid/v1/models": _http_error(503, "unavailable"),
            "http://plain.invalid/v1/models": urllib.error.URLError("refused"),
            "http://localhost:9/v1/models": TimeoutError("slow"),
        }
        with monkeypatch.context() as m:
            m.setenv("LOCAL_KEY", "sk-local")
            out["gpu_http"] = outcome(S.account.probe_endpoint, "gpu")
            out["plain_url"] = outcome(S.account.probe_endpoint, "plain")
            out["local_timeout"] = outcome(S.account.probe_endpoint, "local")
        entry = S.me.EndpointEntry(id="local", base_url="http://entry.invalid/v1", model="m", key_env="LOCAL_KEY")
        keyless = S.me.EndpointEntry(id="open", base_url="http://open.invalid/v1", model="m")
        urlopen.script = {}
        out["entry_resolver"] = outcome(S.account.probe_entry, entry, timeout=2.0, key_resolver=lambda: "k-from-resolver")
        out["entry_no_key"] = outcome(S.account.probe_entry, entry, timeout=2.0, key_resolver=lambda: None)
        out["entry_resolver_raises"] = outcome(
            S.account.probe_entry, entry, timeout=2.0, key_resolver=lambda: 1 / 0)
        out["entry_keyless"] = outcome(S.account.probe_entry, keyless, timeout=2.0)
        with monkeypatch.context() as m:
            m.setenv("LOCAL_KEY", "sk-env")
            out["entry_layered"] = outcome(S.account.probe_entry, entry, timeout=2.0)
        out["authenticated"] = {
            "keyless": outcome(S.account.get_authenticated, "http://open.invalid/x", label="open",
                               key_env=None, timeout=1.0),
            "missing_key": outcome(S.account.get_authenticated, "http://k.invalid/x", label="local",
                                   key_env="LOCAL_KEY", timeout=1.0),
        }
        out["calls"] = [(c["url"], c["headers"], c["timeout"]) for c in urlopen.calls]
        return out

    result = par(scenario)
    assert result["default"][1]["ok"] is True
    assert result["gpu"][1]["ok"] is True
    assert result["missing"][1]["resolved"] is False
    assert result["harness"][1]["resolved"] is False
    assert result["local_no_key"][1]["decisive"] is False and result["local_no_key"][1]["ok"] is False
    assert result["entry_no_key"][1]["decisive"] is False
    assert result["gpu_http"][1]["detail"].startswith("HTTP 503")
    assert any(h == ["authorization", "Bearer sk-local"] for c in result["calls"] for h in c[1])
    assert any(h == ["authorization", "Bearer k-from-resolver"] for c in result["calls"] for h in c[1])


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------


def test_make_openai_client(par, monkeypatch, named_registry, tmp_path):
    made = []

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            made.append(kwargs)
            self.kwargs = kwargs

    module = types.ModuleType("openai")
    module.OpenAI = _FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", module)

    def scenario(S):
        out = {}
        made.clear()

        def run(key, *args, env=None, **kwargs):
            with monkeypatch.context() as m:
                for name, value in (env or {}).items():
                    m.setenv(name, value)
                before = len(made)
                out[key] = {"res": outcome(S.client.make_openai_client, *args, **kwargs),
                            "made": [pre(k) for k in made[before:]]}

        run("default", env={"OPENROUTER_API_KEY": "sk-or"})
        run("default_no_key")
        run("explicit_key", api_key="given")
        run("named_keyed", endpoint="local", env={"LOCAL_KEY": "sk-local"})
        run("named_keyed_no_key", endpoint="local")
        run("named_keyless", endpoint="gpu")
        run("named_keyless_explicit", endpoint="gpu", api_key="given")
        run("unknown_endpoint", endpoint="no-such")
        run("harness_endpoint", endpoint="h")
        run("retries_zero", env={"OPENROUTER_API_KEY": "k"}, max_retries=0)
        for bad in (-1, True, 1.5, "2"):
            run(f"retries_{bad!r}", env={"OPENROUTER_API_KEY": "k"}, max_retries=bad)
        run("project_root", env={"OPENROUTER_API_KEY": "k"}, project_root=tmp_path)
        with monkeypatch.context() as m:
            m.setitem(sys.modules, "openai", None)
            m.setenv("OPENROUTER_API_KEY", "k")
            out["no_sdk"] = outcome(S.client.make_openai_client)
        out["keyless_const"] = S.client.KEYLESS_API_KEY
        return out

    result = par(scenario)
    assert result["default"]["made"][0]["api_key"] == "sk-or"
    assert result["default"]["made"][0]["max_retries"] == 2
    assert result["default_no_key"]["res"][0] == "raise" and "set-key" in result["default_no_key"]["res"][2]
    assert "<CLI>" in result["default_no_key"]["res"][2]
    assert result["named_keyed"]["made"][0]["api_key"] == "sk-local"
    assert result["named_keyless"]["made"][0]["api_key"] == result["keyless_const"]
    assert result["named_keyed_no_key"]["res"][0] == "raise"
    assert result["retries_zero"]["made"][0]["max_retries"] == 0
    assert result["retries_-1"]["res"][0] == "raise"
    assert result["no_sdk"][0] == "raise" and "openai" in result["no_sdk"][2]


# --------------------------------------------------------------------------
# usage_sources vs the legacy usage_budget readers
# --------------------------------------------------------------------------

_NOW = 1_000_000.0


def _claude_snapshot(path: Path, rate_limits) -> Path:
    path.write_text(json.dumps({"rate_limits": rate_limits}), encoding="utf-8")
    return path


def test_claude_pool_reader(par, tmp_path, monkeypatch):
    snaps = tmp_path / "snaps"
    snaps.mkdir()
    iso = "1970-01-12T14:03:20+00:00"  # epoch 1_001_000 == _NOW + 1000
    rl = {
        "five_hour": {"used_percentage": 10, "resets_at": _NOW + 9000},
        "seven_day": {"used_percentage": 80, "resets_at": _NOW + 6 * 86400},
        "seven_day_opus": {"used_percentage": 100, "resets_at": _NOW + 86400},
        "seven_day_sonnet": {"used_percentage": 50.0, "resets_at": str(int(_NOW + 302400))},
        "seven_day_oauth_apps": {"utilization": 20, "resets_at": iso},
        "seven_day_overage_included": {"used_percent": 20, "resets_at": iso.replace("+00:00", "Z")},
        "dead": {"used_percentage": 5, "resets_at": _NOW - 10},
        "bogus_pool": {"used_percentage": 5, "resets_at": _NOW + 100},
        "bool_used": {"used_percentage": True, "resets_at": _NOW + 100},
        "no_reset": {"used_percentage": 5},
        "bad_reset": {"used_percentage": 5, "resets_at": "tomorrow"},
        "bool_reset": {"used_percentage": 5, "resets_at": True},
        "model_scoped": [
            {"display_name": "Fable", "utilization": 30, "resets_at": iso},
            "not-a-mapping",
            {"display_name": None, "utilization": 1, "resets_at": iso},
            {"display_name": "Opus 4", "utilization": 90, "resets_at": iso},
        ],
    }
    good = _claude_snapshot(snaps / "good.json", rl)
    no_scoped = _claude_snapshot(snaps / "no_scoped.json", {"five_hour": rl["five_hour"]})
    empty_scoped = _claude_snapshot(snaps / "empty_scoped.json", {"model_scoped": []})
    no_limits = snaps / "no_limits.json"
    no_limits.write_text(json.dumps({"other": 1}), encoding="utf-8")
    not_mapping = snaps / "not_mapping.json"
    not_mapping.write_text("[1, 2]", encoding="utf-8")
    malformed = snaps / "malformed.json"
    malformed.write_text("{nope", encoding="utf-8")
    limits_not_mapping = snaps / "limits_not_mapping.json"
    limits_not_mapping.write_text(json.dumps({"rate_limits": [1]}), encoding="utf-8")

    def scenario(S):
        out = {}
        spec = S.conserve.ConserveSpec
        for pool in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet",
                     "seven_day_oauth_apps", "seven_day_overage_included", "dead", "bogus_pool",
                     "bool_used", "no_reset", "bad_reset", "bool_reset", "absent_pool"):
            out[pool] = outcome(S.usage.read_claude_pool, spec(pool=pool), now=_NOW, snapshot=good)
        for name in ("fable", "FABLE ", "Opus 4", "Nobody"):
            out[f"scoped:{name}"] = outcome(
                S.usage.read_claude_pool, spec(pool="model_scoped", display_name=name), now=_NOW, snapshot=good)
        out["scoped:no_name"] = outcome(S.usage.read_claude_pool, spec(pool="model_scoped"), now=_NOW, snapshot=good)
        out["scoped:no_array"] = outcome(
            S.usage.read_claude_pool, spec(pool="model_scoped", display_name="Fable"), now=_NOW, snapshot=no_scoped)
        out["scoped:empty_array"] = outcome(
            S.usage.read_claude_pool, spec(pool="model_scoped", display_name="Fable"), now=_NOW, snapshot=empty_scoped)
        for key, path in (("missing_file", snaps / "nope.json"), ("no_limits", no_limits),
                          ("not_mapping", not_mapping), ("malformed", malformed),
                          ("limits_not_mapping", limits_not_mapping)):
            out[key] = outcome(S.usage.read_claude_pool, spec(pool="five_hour"), now=_NOW, snapshot=path)
        # the default snapshot path is the module global
        with monkeypatch.context() as m:
            m.setattr(S.usage, "CLAUDE_SNAPSHOT", good)
            out["default_path"] = outcome(S.usage.read_claude_pool, spec(pool="five_hour"), now=_NOW)
            m.setattr(S.usage, "CLAUDE_SNAPSHOT", snaps / "elsewhere.json")
            out["default_path_missing"] = outcome(S.usage.read_claude_pool, spec(pool="five_hour"), now=_NOW)
        out["wall_clock"] = outcome(
            lambda: S.usage.read_claude_pool(spec(pool="five_hour"), snapshot=good).status)
        return out

    result = par(scenario)
    assert result["five_hour"][1]["status"] == "available"
    assert result["seven_day"][1]["status"] == "under-quota"
    assert result["seven_day_opus"][1]["status"] == "out-of-quota"
    assert result["dead"][1]["status"] == "no-data"
    assert result["scoped:fable"][1]["status"] == "available"
    assert result["scoped:Nobody"][1]["status"] == "no-data"
    assert result["default_path"][1]["status"] == "available"
    assert result["default_path_missing"][1]["status"] == "no-data"


def test_claude_pool_pacing_boundary(par, tmp_path):
    """Exactly on pace reads as available (the epsilon), one hair behind is under-quota."""
    on_pace = _claude_snapshot(tmp_path / "pace.json", {
        "five_hour": {"used_percentage": 50, "resets_at": _NOW + 9000},
        "seven_day": {"used_percentage": 50.001, "resets_at": _NOW + 302400},
    })

    def scenario(S):
        spec = S.conserve.ConserveSpec
        return {
            pool: outcome(S.usage.read_claude_pool, spec(pool=pool), now=_NOW, snapshot=on_pace)
            for pool in ("five_hour", "seven_day")
        }

    result = par(scenario)
    assert result["five_hour"][1]["status"] == "available"
    assert result["seven_day"][1]["status"] == "under-quota"


def _ts(offset: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_NOW + offset))


def _limits_event(primary=None, secondary=None, credits=None, ts=None, nest=1):
    limits = {"primary": primary, "secondary": secondary}
    if credits is not None:
        limits["credits"] = credits
    event = {"rate_limits": limits}
    for _ in range(nest):
        event = {"payload": event}
    if ts is not None:
        event["timestamp"] = ts
    return event


def test_codex_pool_reader(par, tmp_path, monkeypatch):
    root = tmp_path / "codex"
    window = {"used_percent": 10, "window_minutes": 300, "resets_at": _NOW + 9000}
    cases = {
        "healthy": [_limits_event(window, {"used_percent": 1, "window_minutes": 10080, "resets_at": _NOW + 5})],
        "under": [_limits_event({"used_percent": 80, "window_minutes": 300, "resets_at": _NOW + 15000})],
        "spent": [_limits_event({"used_percent": 100, "window_minutes": 300, "resets_at": _NOW + 100})],
        "dead": [_limits_event({"used_percent": 10, "window_minutes": 300, "resets_at": _NOW - 5})],
        "no_used": [_limits_event({"window_minutes": 300, "resets_at": _NOW + 100})],
        "bool_minutes": [_limits_event({"used_percent": 1, "window_minutes": True, "resets_at": _NOW + 100})],
        "zero_minutes": [_limits_event({"used_percent": 1, "window_minutes": 0, "resets_at": _NOW + 100})],
        "no_minutes": [_limits_event({"used_percent": 1, "resets_at": _NOW + 100})],
        "only_secondary": [_limits_event(None, {"used_percent": 5, "window_minutes": 10080, "resets_at": _NOW + 1000})],
        "deep_nest": [_limits_event(window, nest=5)],
        "too_deep": [_limits_event(window, nest=8)],
        "garbage_lines": [window, "not json at all", {"unrelated": 1}, _limits_event(window)],
        "last_wins": [_limits_event({"used_percent": 99, "window_minutes": 300, "resets_at": _NOW + 9000}),
                      _limits_event(window)],
        "no_event": [{"unrelated": 1}, {"payload": {"x": 2}}],
        "exhausted_future_text": [
            _limits_event(None, None, {"has_credits": False, "unlimited": False}, ts=_ts(-50)),
            {"payload": {"error": {"codex_error_info": "usage_limit_exceeded",
                                   "message": "limit hit; try again at Sep 19th, 2099 3:34 PM."}}},
        ],
        "exhausted_past_text": [
            _limits_event(None, None, {"has_credits": False, "unlimited": False}, ts=_ts(-50)),
            {"payload": {"error": {"codex_error_info": "usage_limit_exceeded",
                                   "message": "try again at Jan 2nd, 2001 12:05 AM"}}},
        ],
        "exhausted_noon_pm": [
            _limits_event(None, None, {"has_credits": False, "unlimited": False}, ts=_ts(-50)),
            {"payload": {"error": {"codex_error_info": "usage_limit_exceeded",
                                   "message": "try again at Dec 31, 2099 12:00 PM"}}},
        ],
        "exhausted_bad_text_recent": [
            _limits_event(None, None, {"has_credits": False, "unlimited": False}, ts=_ts(-50)),
            {"payload": {"error": {"codex_error_info": "usage_limit_exceeded", "message": "garbled"}}},
        ],
        "exhausted_bad_month_recent": [
            _limits_event(None, None, {"has_credits": False, "unlimited": False}, ts=_ts(-50)),
            {"payload": {"error": {"codex_error_info": "usage_limit_exceeded",
                                   "message": "try again at Foo 3rd, 2099 3:34 PM"}}},
        ],
        "exhausted_bad_day": [
            _limits_event(None, None, {"has_credits": False, "unlimited": False}, ts=_ts(-50)),
            {"payload": {"error": {"codex_error_info": "usage_limit_exceeded",
                                   "message": "try again at Feb 31st, 2099 3:34 PM"}}},
        ],
        "exhausted_non_string_message": [
            _limits_event(None, None, {"has_credits": False, "unlimited": False}, ts=_ts(-50)),
            {"payload": {"error": {"codex_error_info": "usage_limit_exceeded", "message": 5}}},
        ],
        "exhausted_in_list": [
            _limits_event(None, None, {"has_credits": False, "unlimited": False}, ts=_ts(-50)),
            {"payload": {"errors": [{"codex_error_info": "usage_limit_exceeded",
                                     "message": "try again at Sep 19th, 2099 3:34 PM."}]}},
        ],
        "exhausted_latched": [_limits_event(None, None, {"has_credits": False, "unlimited": False}, ts=_ts(-100))],
        "exhausted_stale": [_limits_event(None, None, {"has_credits": False, "unlimited": False}, ts=_ts(-100000))],
        "exhausted_no_ts": [_limits_event(None, None, {"has_credits": False, "unlimited": False})],
        "unlimited_credits": [_limits_event(None, None, {"has_credits": False, "unlimited": True})],
        "has_credits": [_limits_event(None, None, {"has_credits": True, "unlimited": False})],
        "credits_not_mapping": [_limits_event(None, None, "oops")],
        "message_superseded": [
            _limits_event(None, None, {"has_credits": False, "unlimited": False}, ts=_ts(-50)),
            {"payload": {"error": {"codex_error_info": "usage_limit_exceeded",
                                   "message": "try again at Sep 19th, 2099 3:34 PM."}}},
            _limits_event(window),
        ],
        "message_before_reading": [
            {"payload": {"error": {"codex_error_info": "usage_limit_exceeded",
                                   "message": "try again at Sep 19th, 2099 3:34 PM."}}},
            _limits_event(None, None, {"has_credits": False, "unlimited": False}, ts=_ts(-100)),
        ],
    }
    for name, events in cases.items():
        directory = root / name
        directory.mkdir(parents=True)
        lines = [e if isinstance(e, str) else json.dumps(e) for e in events]
        (directory / "rollout.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (root / "empty").mkdir()
    (root / "no_jsonl").mkdir()
    (root / "no_jsonl" / "notes.txt").write_text("x", encoding="utf-8")
    # only the tail is read: the reading is older than the scanned window
    big = root / "big"
    big.mkdir()
    (big / "rollout.jsonl").write_text(
        json.dumps(_limits_event(window)) + "\n" + ("filler line\n" * 60000), encoding="utf-8")
    # the newest rollout wins
    newest = root / "newest"
    newest.mkdir()
    old_file, new_file = newest / "a-old.jsonl", newest / "b-new.jsonl"
    old_file.write_text(json.dumps(_limits_event({"used_percent": 99, "window_minutes": 300, "resets_at": _NOW + 9000})) + "\n", encoding="utf-8")
    new_file.write_text(json.dumps(_limits_event(window)) + "\n", encoding="utf-8")
    os.utime(old_file, (1_000_000, 1_000_000))
    os.utime(new_file, (2_000_000, 2_000_000))
    for sub in ("exhausted_no_ts",):
        os.utime(root / sub / "rollout.jsonl", (_NOW - 60, _NOW - 60))

    def scenario(S):
        out = {}
        spec = S.conserve.ConserveSpec
        for name in list(cases) + ["empty", "no_jsonl", "big", "newest", "absent_dir"]:
            for pool in ("seven_day",):
                moment = 4_000_000_000.0 if name == "exhausted_past_text" else _NOW
                out[name] = outcome(S.usage.read_codex_pool, spec(pool=pool), now=moment, sessions_dir=root / name)
        out["secondary_pool"] = outcome(
            S.usage.read_codex_pool, spec(pool="secondary"), now=_NOW, sessions_dir=root / "only_secondary")
        out["primary_pool"] = outcome(
            S.usage.read_codex_pool, spec(pool="primary"), now=_NOW, sessions_dir=root / "healthy")
        out["missing_secondary"] = outcome(
            S.usage.read_codex_pool, spec(pool="secondary"), now=_NOW, sessions_dir=root / "under")
        with monkeypatch.context() as m:
            m.setattr(S.usage, "CODEX_SESSIONS_DIR", root / "healthy")
            out["default_dir"] = outcome(S.usage.read_codex_pool, spec(pool="seven_day"), now=_NOW)
            m.setattr(S.usage, "CODEX_SESSIONS_DIR", root / "empty")
            out["default_dir_empty"] = outcome(S.usage.read_codex_pool, spec(pool="seven_day"), now=_NOW)
        return out

    result = par(scenario)
    status = {k: v[1]["status"] for k, v in result.items() if v[0] == "ok"}
    assert status["healthy"] == "available"
    assert status["under"] == "under-quota"
    assert status["spent"] == "out-of-quota"
    assert status["dead"] == "no-data"
    assert status["deep_nest"] == "available" and status["too_deep"] == "no-data"
    assert status["last_wins"] == "available"
    assert status["exhausted_future_text"] == "out-of-quota"
    assert status["exhausted_past_text"] == "no-data"
    assert status["exhausted_noon_pm"] == "out-of-quota"
    assert status["exhausted_bad_text_recent"] == "out-of-quota"
    assert status["exhausted_latched"] == "out-of-quota"
    assert status["exhausted_stale"] == "no-data"
    assert status["exhausted_no_ts"] == "out-of-quota"
    assert status["unlimited_credits"] == "no-data"
    assert status["message_superseded"] == "available"
    assert status["big"] == "no-data"
    assert status["newest"] == "available"
    assert status["default_dir"] == "available" and status["default_dir_empty"] == "no-data"
    assert status["secondary_pool"] == "available" and status["primary_pool"] == "available"
    assert result["exhausted_future_text"][1]["resets_at"]


def test_usage_source_helpers(par):
    def scenario(S):
        u = S.usage
        return {
            "epoch": [u._epoch(v) for v in (
                None, True, 5, 5.9, "12", " 13 ", "", "  ", "1970-01-01T00:00:10Z",
                "1970-01-01T00:00:10+00:00", "not a date", [], {})],
            "used": [u._used_percentage(w) for w in (
                {}, {"used_percentage": 5}, {"utilization": 6.5}, {"used_percent": 7},
                {"used_percentage": True, "utilization": 3}, {"used_percentage": "5"})],
            "find_limits": [
                u._find_rate_limits(n) for n in (
                    None, [], {"rate_limits": {"a": 1}}, {"x": {"rate_limits": {"b": 2}}},
                    {"rate_limits": 3}, {"x": {"y": {"z": {"rate_limits": {"c": 3}}}}})],
            "find_message": [
                u._find_usage_limit_message(n) for n in (
                    None, {}, {"codex_error_info": "usage_limit_exceeded", "message": "m"},
                    {"codex_error_info": "usage_limit_exceeded"},
                    {"a": [{"codex_error_info": "usage_limit_exceeded", "message": "deep"}]},
                    [{"codex_error_info": "other"}])],
            "parse": [u._parse_reset_text(m) for m in (
                5, "", "no clause", "try again at Sep 19th, 2099 3:34 PM.",
                "try again at september 1, 2099 12:00 AM", "try again at Mar 2nd, 2099 11:59 pm",
                "try again at Zzz 1st, 2099 1:00 AM", "try again at Feb 30th, 2099 1:00 AM")],
            "verdict": [
                u._verdict("p", used, resets, window, now=_NOW)
                for used, resets, window in (
                    (0, _NOW + 100, 1000), (50, _NOW + 500, 1000), (50.0001, _NOW + 500, 1000),
                    (100, _NOW + 500, 1000), (150, _NOW + 500, 1000), (-20, _NOW + 500, 1000),
                    (10, _NOW, 1000), (10, _NOW - 1, 1000), (10, _NOW + 5000, 1000))],
            "consts": [u._HOUR, u._DAY, u._CLAUDE_WINDOW_SECONDS, u._PACE_EPSILON, u._CODEX_TAIL_BYTES,
                       u._CODEX_EXHAUSTION_LATCH_SECONDS, u._RESET_TEXT_RE],
            "paths": [u.CLAUDE_SNAPSHOT.parts[-5:], u.CODEX_SESSIONS_DIR.parts[-2:]],
        }

    result = par(scenario)
    assert result["epoch"][2] == 5 and result["epoch"][4] == 12
    assert result["parse"][3] is not None and result["parse"][0] is None
    assert result["verdict"][0]["status"] == "available"


# --------------------------------------------------------------------------
# swapper: URLs, payload parsing, HTTP client
# --------------------------------------------------------------------------


def test_swapper_errors_are_shared_with_the_process_layer(sides):
    legacy, final = sides
    for side in sides:
        for name in ("ProcessNotFound", "InspectionIndeterminate", "SwapperError"):
            assert getattr(side.swapper, name) is getattr(
                side.sproc, name, getattr(side.swapper, name)
            )
        assert side.sproc.ProcessNotFound is side.swapper.ProcessNotFound
        assert side.sproc.InspectionIndeterminate is side.swapper.InspectionIndeterminate
    # no swapper <-> _swapper_process cycle in the final package: the process layer imports no service
    import ast

    source = Path(final.sproc.__file__).read_text(encoding="utf-8")
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module and node.level
    }
    assert "swapper" not in imported and "_swapper_errors" in imported
    assert [c.__name__ for c in final.swapper.InspectionIndeterminate.__mro__] == [
        c.__name__ for c in legacy.swapper.InspectionIndeterminate.__mro__
    ]


def test_swapper_urls_and_payloads(par, named_registry):
    def scenario(S):
        sw = S.swapper
        running_ok = json.dumps({"running": [
            {"model": "m", "state": "ready", "proxy": "http://127.0.0.1:5", "cmd": "c"},
            {"model": "n", "state": "ready", "proxy": "http://127.0.0.1:6"},
        ]}).encode()
        bad_payloads = [b"\xff", b"nope", b"[1]", b'{"running": 3}', b'{"running": [1]}',
                        b'{"running": [{"model": "m", "state": "s"}]}',
                        b'{"running": [{"model": "", "state": "s", "proxy": "p"}]}',
                        b'{"running": [{"model": "m", "state": "s", "proxy": "p", "cmd": 3}]}']
        return {
            "loopback": [sw.is_loopback_url(u) for u in (
                "http://localhost:1", "http://LOCALHOST", "http://127.0.0.1:1", "http://[::1]:1",
                "http://10.0.0.1:1", "http://example.com", "not a url", "http://", "http://127.0.0.2")],
            "normalize": [outcome(sw.normalize_swapper_url, u) for u in (
                "http://h:1", " http://h:1/ ", "http://h:1/v1", "https://h/v1/", "ftp://h", "h:1", "http://",
                "")],
            "resolve": [
                outcome(sw.resolve_swapper_url, endpoint=None, url=None),
                outcome(sw.resolve_swapper_url, endpoint="gpu", url="http://x"),
                outcome(sw.resolve_swapper_url, endpoint=None, url="http://h:9/v1"),
                outcome(sw.resolve_swapper_url, endpoint="gpu", url=None),
                outcome(sw.resolve_swapper_url, endpoint="h", url=None),
                outcome(sw.resolve_swapper_url, endpoint="no-such", url=None),
            ],
            "parse_ok": outcome(sw.parse_running, running_ok),
            "parse_bad": [outcome(sw.parse_running, b) for b in bad_payloads],
            "parse_empty": outcome(sw.parse_running, b'{"running": []}'),
            "ports": [outcome(lambda u=u: sw.SwapperClient(u).port) for u in (
                "http://h:8080", "http://h", "https://h/v1", "http://h:99999")],
            "locality": [sw.SwapperClient(u).is_local for u in ("http://localhost:1", "http://remote:1")],
            "consts": [sw.EXIT_FAILURE, sw.EXIT_USAGE, sw.EXIT_INDETERMINATE, sw.SWAPPER_NAME,
                       sw.SERVER_NAMES, sw.MLX_SERVER_MODULE, sw.UNLOAD_PATH, sw.LISTENER_GRACE_S],
            "exit_codes": {name: getattr(sw, name).exit_code for name in (
                "SwapperError", "SwapperUsageError", "NonLocalTargetError", "SwapperHTTPError",
                "SwapperProtocolError", "ModelNotRunningError", "SafetyRefusal", "ProcessChangedError",
                "LaunchRefused", "TerminationFailed", "UnloadIncomplete", "ProcessNotFound",
                "InspectionIndeterminate")},
        }

    result = par(scenario)
    assert result["loopback"][:4] == [True, True, True, True] and result["loopback"][4] is False
    assert result["resolve"][3] == ("ok", "http://gpu.invalid")
    assert result["exit_codes"]["LaunchRefused"] == 3 and result["exit_codes"]["InspectionIndeterminate"] == 5


def test_swapper_client_transport(par, urlopen):
    def scenario(S):
        sw = S.swapper
        out = {}
        calls = []
        running = [json.dumps({"running": [{"model": "m", "state": "ready", "proxy": "http://127.0.0.1:5"}]}).encode()]

        def transport(method, url, timeout):
            calls.append((method, url, timeout))
            if method == "POST":
                return b""
            return running.pop(0) if len(running) > 1 else running[0]

        client = sw.SwapperClient("http://127.0.0.1:8080/v1", timeout_s=2.5, transport=transport)
        out["base"] = client.base_url
        out["running"] = outcome(client.running)
        empty = json.dumps({"running": []}).encode()
        running[:] = [json.dumps({"running": [{"model": "m", "state": "ready", "proxy": "http://127.0.0.1:5"}]}).encode(),
                      empty]
        out["unload"] = outcome(client.unload_all, settle_s=0.0, poll_s=0.0)
        running[:] = [json.dumps({"running": [{"model": "m", "state": "r", "proxy": "http://127.0.0.1:5"}]}).encode()]
        out["unload_incomplete"] = outcome(client.unload_all, settle_s=0.0, poll_s=0.0)
        remote = sw.SwapperClient("http://remote.invalid:8080", transport=transport)
        out["unload_remote"] = outcome(remote.unload_all)
        out["running_remote"] = outcome(remote.running)
        out["calls"] = list(calls)
        # default urllib transport
        urlopen.calls.clear()
        base = "http://127.0.0.1:8080"
        urlopen.script = {
            f"{base}/running": b'{"running": []}',
            f"{base}{sw.UNLOAD_PATH}": b"",
        }
        default = sw.SwapperClient(base, timeout_s=1.0)
        out["default_running"] = outcome(default.running)
        out["default_unload"] = outcome(default.unload_all, settle_s=0.0, poll_s=0.0)
        for key, item in (("http_error", _http_error(500)), ("url_error", urllib.error.URLError("refused")),
                          ("os_error", OSError("boom"))):
            urlopen.script = {f"{base}/running": item}
            out[key] = outcome(default.running)
        out["urlopen_calls"] = [(c["url"], c["method"], c["data"], c["timeout"]) for c in urlopen.calls]
        return out

    result = par(scenario)
    assert result["running"][0] == "ok" and result["unload"][0] == "ok"
    assert result["unload_incomplete"][0] == "raise" and "UnloadIncomplete" in result["unload_incomplete"][1]
    assert result["unload_remote"][0] == "raise" and "NonLocalTargetError" in result["unload_remote"][1]
    assert result["running_remote"][0] == "ok"
    assert result["http_error"][0] == "raise" and "SwapperHTTPError" in result["http_error"][1]
    assert any(c[1] == "POST" for c in result["urlopen_calls"])


# --------------------------------------------------------------------------
# swapper: process rules over a fake inspector
# --------------------------------------------------------------------------


class _FakeInspector:
    """The ``Inspector`` protocol over a scripted process table."""

    def __init__(self, S, procs, listens=None, *, owner="1000", wait=None, signal_errors=None,
                 process_errors=None, listen_errors=None, die_on=()):
        self.S = S
        self.table = {p.pid: p for p in procs}
        self.listens = listens or {}
        self.owner = owner
        self.wait = list(wait or [])
        self.signal_errors = list(signal_errors or [])
        self.process_errors = dict(process_errors or {})
        self.listen_errors = dict(listen_errors or {})
        self.die_on = set(die_on)
        self.log = []

    def current_owner(self):
        return self.owner

    def process(self, pid):
        self.log.append(("process", pid))
        error = self.process_errors.get(pid)
        if error is not None:
            if isinstance(error, list):
                if error:
                    item = error.pop(0)
                    if item is not None:
                        raise item
            else:
                raise error
        if pid not in self.table:
            raise self.S.swapper.ProcessNotFound(f"no process {pid}")
        return self.table[pid]

    def processes(self):
        return tuple(self.table.values())

    def listen_addrs(self, pid):
        error = self.listen_errors.get(pid)
        if error is not None:
            raise error
        if pid not in self.table:
            raise self.S.swapper.ProcessNotFound(f"no process {pid}")
        return frozenset(self.listens.get(pid, ()))

    def signal(self, pid, create_time, sig):
        self.log.append(("signal", pid, create_time, sig))
        if self.signal_errors:
            error = self.signal_errors.pop(0)
            if error is not None:
                raise error
        if sig in self.die_on:
            self.table.pop(pid, None)
            self.listens.pop(pid, None)

    def wait_gone(self, pid, create_time, timeout_s):
        self.log.append(("wait", pid, timeout_s))
        return self.wait.pop(0) if self.wait else True


def _rec(S, pid, name, *, ppid=1, owner="1000", ctime=10.0, cmdline=None):
    return S.sproc.ProcessRecord(pid=pid, ppid=ppid, name=name, owner=owner, create_time=ctime + pid,
                                 cmdline=cmdline)


def _world(S, **overrides):
    """llama-swap (100, :8080) with one model server (200, :5800) and assorted others."""
    procs = [
        _rec(S, 1, "init", ppid=0),
        _rec(S, 100, "llama-swap.exe", ppid=1),
        _rec(S, 200, "llama-server", ppid=100),
        _rec(S, 300, "ninfer-serve", ppid=1),
        _rec(S, 400, "python", ppid=1, cmdline=("python", "-m", "mlx_lm.server")),
        _rec(S, 500, "bash", ppid=1),
        _rec(S, 600, "other", ppid=1, owner="2000"),
    ]
    listens = {100: [("127.0.0.1", 8080)], 200: [("127.0.0.1", 5800)], 300: [("0.0.0.0", 5900)],
               400: [("127.0.0.1", 5950)], 600: [("127.0.0.1", 7000)]}
    return procs, listens


def _client(S, models=("m1",), proxy="http://127.0.0.1:5800", url="http://127.0.0.1:8080"):
    body = json.dumps({"running": [
        {"model": m, "state": "ready", "proxy": proxy, "cmd": "c"} for m in models]}).encode()
    return S.swapper.SwapperClient(url, transport=lambda method, u, t: body)


def test_locate_and_terminate_managed_model(par):
    def scenario(S):
        out = {}

        def build(mutate=None, **kwargs):
            procs, listens = _world(S)
            if mutate:
                procs, listens = mutate(procs, listens)
            return _FakeInspector(S, procs, listens, **kwargs)

        def drop(pid):
            return lambda procs, listens: ([p for p in procs if p.pid != pid], listens)

        def replace(pid, **fields):
            def apply(procs, listens):
                return ([dataclasses.replace(p, **fields) if p.pid == pid else p for p in procs], listens)
            return apply

        def add_listener(pid, addr):
            def apply(procs, listens):
                listens = {k: list(v) for k, v in listens.items()}
                listens.setdefault(pid, []).append(addr)
                return procs, listens
            return apply

        def two_swappers(procs, listens):
            procs = procs + [_rec(S, 101, "llama-swap", ppid=1)]
            listens = {**listens, 101: [("0.0.0.0", 8080)]}
            return procs, listens

        def second_child(procs, listens):
            return procs + [_rec(S, 201, "llama-server", ppid=100)], {**listens, 201: [("127.0.0.1", 5800)]}

        def unreadable_child(procs, listens):
            return ([dataclasses.replace(p, ppid=None) if p.pid == 200 else p for p in procs], listens)

        cases = {
            "ok": (build(), {}),
            "not_running": (build(), {"models": ()}),
            "duplicate_model": (build(), {"models": ("m1", "m1")}),
            "remote_proxy": (build(), {"proxy": "http://10.1.1.1:5800"}),
            "remote_swapper": (build(), {"url": "http://remote.invalid:8080"}),
            "no_swapper": (build(drop(100)), {}),
            "swapper_owner_unknown": (build(replace(100, owner=None)), {}),
            "other_owner_swapper": (build(replace(100, owner="2000")), {}),
            "two_swappers": (build(two_swappers), {}),
            "no_child": (build(drop(200)), {}),
            "two_children": (build(second_child), {}),
            "unreadable_child": (build(unreadable_child), {}),
            "child_other_owner": (build(replace(200, owner="2000")), {}),
            "child_extra_listener": (build(add_listener(200, ("::1", 5800))), {}),
            "identity_changed": (build(process_errors={200: [S.swapper.ProcessNotFound("gone")]}), {}),
        }
        for name, (insp, kwargs) in cases.items():
            client = _client(S, **kwargs)
            out[f"locate:{name}"] = {"res": outcome(S.swapper.locate_managed_model, client, "m1", inspector=insp),
                                     "log": insp.log}

        def term(name, insp, *, client=None, **kwargs):
            client = client or _client(S)
            out[f"terminate:{name}"] = {
                "res": outcome(S.swapper.terminate_model, client, "m1", inspector=insp, **kwargs),
                "log": insp.log,
            }

        term("graceful", build(wait=[True]))
        term("escalated", build(wait=[False, True]))
        term("failed", build(wait=[False, False]))
        term("exits_before_kill", build(wait=[False], signal_errors=[None, S.swapper.ProcessNotFound("gone")]))
        term("first_signal_missing", build(signal_errors=[S.swapper.ProcessNotFound("gone")]))
        term("reused_before_term", build(process_errors={200: [None, S.swapper.ProcessNotFound("x")]}))
        term("reused_before_kill", build(wait=[False], process_errors={200: [None, None, S.swapper.ProcessNotFound("x")]}))
        term("denied", build(signal_errors=[S.swapper.InspectionIndeterminate("denied")]))
        term("negative_grace", build(), grace_s=-1)
        term("negative_wait", build(), kill_wait_s=-1)
        term("remote", build(), client=_client(S, url="http://remote.invalid:8080"))
        term("custom_times", build(wait=[False, True]), grace_s=0.5, kill_wait_s=0.25)
        return out

    result = par(scenario)
    assert result["locate:ok"]["res"][0] == "ok" and result["locate:ok"]["res"][1]["pid"] == 200
    for name, exception in (
        ("not_running", "ModelNotRunningError"), ("duplicate_model", "SafetyRefusal"),
        ("remote_proxy", "SafetyRefusal"), ("remote_swapper", "NonLocalTargetError"),
        ("no_swapper", "SafetyRefusal"), ("swapper_owner_unknown", "InspectionIndeterminate"),
        ("two_swappers", "SafetyRefusal"), ("no_child", "SafetyRefusal"),
        ("two_children", "SafetyRefusal"), ("unreadable_child", "InspectionIndeterminate"),
        ("identity_changed", "ProcessChangedError"),
    ):
        assert result[f"locate:{name}"]["res"][0] == "raise", name
        assert exception in result[f"locate:{name}"]["res"][1], name
    assert result["terminate:graceful"]["res"][1]["signal"] == "SIGTERM"
    assert result["terminate:escalated"]["res"][1]["escalated"] is True
    assert "TerminationFailed" in result["terminate:failed"]["res"][1]
    assert result["terminate:exits_before_kill"]["res"][1]["escalated"] is False
    assert "ProcessChangedError" in result["terminate:first_signal_missing"]["res"][1]
    assert "ProcessChangedError" in result["terminate:reused_before_term"]["res"][1]
    assert result["terminate:reused_before_kill"]["res"][1]["escalated"] is False
    assert "InspectionIndeterminate" in result["terminate:denied"]["res"][1]
    assert "SwapperUsageError" in result["terminate:negative_grace"]["res"][1]
    assert "NonLocalTargetError" in result["terminate:remote"]["res"][1]
    assert ["signal", 200, 210.0, "SIGKILL"] in result["terminate:escalated"]["log"]


def test_find_strays_classification_and_launch_guard(par):
    def scenario(S):
        out = {}
        sw = S.swapper

        def run(key, fn, procs_mutate=None, **insp_kwargs):
            procs, listens = _world(S)
            if procs_mutate:
                procs = procs_mutate(procs)
            insp = _FakeInspector(S, procs, listens, **insp_kwargs)
            out[key] = {"res": outcome(fn, insp), "log": insp.log}

        run("strays", lambda i: sw.find_strays(inspector=i))
        run("strays_other_uid", lambda i: sw.find_strays(inspector=i, uid=2000))
        run("strays_unreadable_name", lambda i: sw.find_strays(inspector=i),
            lambda ps: ps + [_rec(S, 700, None, ppid=1)])
        run("strays_unreadable_owner", lambda i: sw.find_strays(inspector=i),
            lambda ps: ps + [_rec(S, 700, "llama-server", ppid=1, owner=None)])
        run("strays_python_no_cmdline", lambda i: sw.find_strays(inspector=i),
            lambda ps: ps + [_rec(S, 700, "python3", ppid=1, cmdline=None)])
        run("strays_under_swapper_chain", lambda i: sw.find_strays(inspector=i),
            lambda ps: ps + [_rec(S, 700, "bash", ppid=200), _rec(S, 701, "llama-server", ppid=700)])
        run("strays_cycle", lambda i: sw.find_strays(inspector=i),
            lambda ps: ps + [_rec(S, 700, "llama-server", ppid=701), _rec(S, 701, "bash", ppid=700)])
        run("strays_unreadable_parent", lambda i: sw.find_strays(inspector=i),
            lambda ps: ps + [_rec(S, 700, "llama-server", ppid=None)])
        run("strays_parent_missing", lambda i: sw.find_strays(inspector=i),
            lambda ps: ps + [_rec(S, 700, "llama-server", ppid=9999)])
        out["classify"] = [
            outcome(sw.classify_server, _rec(S, 1, name, cmdline=cmd)) for name, cmd in (
                ("llama-server", None), ("ninfer-serve.exe", None), (None, None), ("bash", None),
                ("python", None), ("Python3", ("x",)),
                ("python", ("python", "-m", "mlx_lm.server")), ("python", ("/opt/mlx_lm/server.py",)),
                ("python", ("C:\\x\\mlx_lm.server",)), ("python", ("python", "other.py")))
        ]
        run("launch_under_swapper", lambda i: sw.check_launch_allowed(caller_pid=200, inspector=i))
        run("launch_beside_swapper", lambda i: sw.check_launch_allowed(caller_pid=500, inspector=i))
        run("launch_no_swapper", lambda i: sw.check_launch_allowed(caller_pid=500, inspector=i),
            lambda ps: [p for p in ps if p.pid not in (100, 200)])
        run("launch_other_user_swapper", lambda i: sw.check_launch_allowed(caller_pid=500, inspector=i),
            lambda ps: [dataclasses.replace(p, owner="2000") if p.pid == 100 else p for p in ps])
        run("launch_caller_missing", lambda i: sw.check_launch_allowed(caller_pid=9999, inspector=i))
        run("launch_unreadable_name", lambda i: sw.check_launch_allowed(caller_pid=500, inspector=i),
            lambda ps: ps + [_rec(S, 700, None, ppid=1)])
        run("launch_unreadable_owner", lambda i: sw.check_launch_allowed(caller_pid=500, inspector=i),
            lambda ps: [dataclasses.replace(p, owner=None) if p.pid == 100 else p for p in ps])
        run("launch_default_pid", lambda i: sw.check_launch_allowed(inspector=i))
        return out

    result = par(scenario)
    assert [p["pid"] for p in result["strays"]["res"][1]] == [300, 400]
    assert result["strays_other_uid"]["res"][1] == []
    assert result["strays_unreadable_name"]["res"][0] == "raise"
    assert result["strays_cycle"]["res"][0] == "ok"
    assert result["classify"][0] == ("ok", True) and result["classify"][2] == ("ok", None)
    assert result["launch_under_swapper"]["res"][0] == "ok"
    assert "LaunchRefused" in result["launch_beside_swapper"]["res"][1]
    assert result["launch_no_swapper"]["res"][0] == "ok"
    assert "SwapperUsageError" in result["launch_caller_missing"]["res"][1]


def test_terminate_listener(par):
    def scenario(S):
        out = {}
        sw = S.swapper

        def run(key, port=5900, *, mutate=None, free=(False, True), kwargs=None, **insp_kwargs):
            procs, listens = _world(S)
            if mutate:
                procs, listens = mutate(procs, listens)
            insp = _FakeInspector(S, procs, listens, **insp_kwargs)
            sequence = list(free)

            def port_free(p):
                insp.log.append(("port_free", p))
                return sequence.pop(0) if len(sequence) > 1 else sequence[0]

            out[key] = {
                "res": outcome(sw.terminate_listener, port, inspector=insp, port_free=port_free,
                               settle_s=0.0, **(kwargs or {})),
                "log": insp.log,
            }

        run("free_port", 5999, free=(True,))
        both = {"SIGTERM", "SIGKILL"}
        run("bind_free_but_listener", 5900, free=(True,), wait=[True], die_on=both)
        run("terminated", 5900, wait=[True], die_on=both)
        run("escalated", 5900, wait=[False, True], die_on={"SIGKILL"})
        run("failed", 5900, wait=[False, False])
        run("still_occupied", 5900, free=(False,), wait=[True])
        run("not_recognized", 8080)
        run("swapper_child", 5800)
        run("unreadable_listener", 5900, free=(True,), listen_errors={500: S.swapper.InspectionIndeterminate("denied")})
        run("unreadable_during_stop", 5900, listen_errors={500: S.swapper.InspectionIndeterminate("denied")})
        run("two_listeners", 5900, mutate=lambda p, l: (p + [_rec(S, 301, "ninfer-serve", ppid=1)],
                                                         {**l, 301: [("::", 5900)]}))
        run("other_owner_ignored", 7000, free=(True,))
        run("invalid_port_low", 0)
        run("invalid_port_high", 70000)
        run("negative_grace", 5900, kwargs={"grace_s": -1})
        run("pid_reused_before_signal", 5900, process_errors={300: [S.swapper.ProcessNotFound("reused")]})
        run("signal_race", 5900, signal_errors=[S.swapper.ProcessNotFound("gone")])
        run("exits_before_kill", 5900, wait=[False], die_on=both,
            signal_errors=[None, S.swapper.ProcessNotFound("gone")])
        return out

    result = par(scenario)
    assert result["free_port"]["res"][1]["action"] == "none"
    assert result["terminated"]["res"][1]["action"] == "terminated"
    assert result["terminated"]["res"][1]["signal"] == "SIGTERM"
    assert result["escalated"]["res"][1]["escalated"] is True
    assert "TerminationFailed" in result["failed"]["res"][1]
    assert "SafetyRefusal" in result["still_occupied"]["res"][1]
    assert "SafetyRefusal" in result["not_recognized"]["res"][1]
    assert "SafetyRefusal" in result["swapper_child"]["res"][1]
    assert "InspectionIndeterminate" in result["unreadable_listener"]["res"][1]
    assert "SwapperUsageError" in result["invalid_port_low"]["res"][1]
    assert "ProcessChangedError" in result["signal_race"]["res"][1]
    assert "ProcessChangedError" in result["pid_reused_before_signal"]["res"][1]
    assert result["exits_before_kill"]["res"][0] == "ok"
    assert result["other_owner_ignored"]["res"][1]["action"] == "none"
    assert "SafetyRefusal" in result["two_listeners"]["res"][1]


# --------------------------------------------------------------------------
# _swapper_process: psutil inspector, normalize_name, port probe
# --------------------------------------------------------------------------


class _FakePsutil(types.ModuleType):
    """A psutil stand-in over a table ``{pid: {...}}`` with per-attribute denials."""

    CONN_LISTEN = "LISTEN"
    STATUS_ZOMBIE = "zombie"

    def __init__(self, table, current=1000):
        super().__init__("psutil")
        self.table = table
        self.current = current
        mod = self

        class NoSuchProcess(Exception):
            pass

        class ZombieProcess(NoSuchProcess):
            pass

        class AccessDenied(Exception):
            pass

        class Process:
            def __init__(self, pid=None):
                pid = mod.current if pid is None else pid
                spec = mod.table.get(pid)
                if spec is None:
                    raise NoSuchProcess(pid)
                if spec.get("zombie_on_open"):
                    raise ZombieProcess(pid)
                self.pid = pid
                self.spec = spec

            def _read(self, attr, value):
                if attr in self.spec.get("deny", ()):
                    raise AccessDenied(attr)
                if attr in self.spec.get("gone", ()):
                    raise NoSuchProcess(self.pid)
                return value

            def oneshot(self):
                return contextlib.nullcontext()

            def ppid(self):
                return self._read("ppid", self.spec["ppid"])

            def create_time(self):
                return self._read("create_time", self.spec["ctime"])

            def name(self):
                return self._read("name", self.spec["name"])

            def cmdline(self):
                return self._read("cmdline", self.spec.get("cmdline", []))

            def username(self):
                return self._read("username", self.spec["user"])

            def uids(self):
                return self._read("uids", types.SimpleNamespace(real=self.spec["uid"]))

            def status(self):
                return self._read("status", self.spec.get("status", "running"))

            def net_connections(self, kind="tcp"):
                self._read("net_connections", None)
                return [
                    types.SimpleNamespace(
                        status=mod.CONN_LISTEN if listening else "ESTABLISHED",
                        laddr=types.SimpleNamespace(ip=ip, port=port) if ip else None,
                    )
                    for ip, port, listening in self.spec.get("conns", [])
                ]

            def terminate(self):
                mod.sent.append((self.pid, "terminate"))
                self._read("terminate", None)

            def kill(self):
                mod.sent.append((self.pid, "kill"))
                self._read("kill", None)

        self.NoSuchProcess = NoSuchProcess
        self.ZombieProcess = ZombieProcess
        self.AccessDenied = AccessDenied
        self.Process = Process
        self.sent = []

    def process_iter(self):
        return [
            _Vanishing(self, pid) if spec.get("iter_gone") or spec.get("zombie_on_open") else self.Process(pid)
            for pid, spec in self.table.items()
        ]


class _Vanishing:
    def __init__(self, ps, pid):
        self.pid = pid
        self._ps = ps

    def oneshot(self):
        raise self._ps.NoSuchProcess(self.pid)


def test_psutil_inspector(par, monkeypatch):
    table = {
        1000: {"ppid": 1, "ctime": 5.0, "name": "me", "user": "alice", "uid": 1000, "cmdline": ["me"]},
        2000: {"ppid": 1, "ctime": 20.0, "name": "srv", "user": "alice", "uid": 1000,
               "cmdline": ["srv", "--port", "5"], "conns": [("127.0.0.1", 5, True), ("0.0.0.0", 6, False),
                                                             (None, 7, True)]},
        2100: {"ppid": 2000, "ctime": 21.0, "name": "denied-name", "user": "alice", "uid": 1000,
               "deny": ["name", "cmdline", "username", "uids"]},
        2200: {"ppid": 1, "ctime": 22.0, "name": "ppid-denied", "user": "alice", "uid": 1000,
               "deny": ["ppid", "create_time"]},
        2300: {"ppid": 1, "ctime": 23.0, "name": "iter-gone", "user": "alice", "uid": 1000, "iter_gone": True},
        2400: {"ppid": 1, "ctime": 24.0, "name": "zombie", "user": "alice", "uid": 1000, "zombie_on_open": True},
        2500: {"ppid": 1, "ctime": 25.0, "name": "zombie-status", "user": "alice", "uid": 1000, "status": "zombie"},
        2600: {"ppid": 1, "ctime": 26.0, "name": "kill-denied", "user": "alice", "uid": 1000,
               "deny": ["terminate", "kill"]},
        2700: {"ppid": 1, "ctime": 27.0, "name": "net-denied", "user": "alice", "uid": 1000, "deny": ["net_connections"]},
        2800: {"ppid": 1, "ctime": 28.0, "name": "status-denied", "user": "alice", "uid": 1000, "deny": ["status"]},
    }

    def scenario(S):
        out = {}
        ps = _FakePsutil(table)
        with monkeypatch.context() as m:
            m.setitem(sys.modules, "psutil", ps)
            insp = S.sproc.PsutilInspector()
            out["owner"] = outcome(insp.current_owner)
            for pid in (2000, 2100, 2400, 9999):
                out[f"process:{pid}"] = outcome(insp.process, pid)
            out["processes"] = outcome(insp.processes)
            for pid in (2000, 2400, 2700, 9999):
                out[f"listen:{pid}"] = outcome(insp.listen_addrs, pid)
            out["signal:term"] = outcome(insp.signal, 2000, 20.0, "SIGTERM")
            out["signal:kill"] = outcome(insp.signal, 2000, 20.0, "SIGKILL")
            out["signal:reused"] = outcome(insp.signal, 2000, 99.0, "SIGTERM")
            out["signal:bad"] = outcome(insp.signal, 2000, 20.0, "SIGHUP")
            out["signal:missing"] = outcome(insp.signal, 9999, 1.0, "SIGTERM")
            out["signal:denied"] = outcome(insp.signal, 2600, 26.0, "SIGKILL")
            out["sent"] = list(ps.sent)
            out["wait:gone"] = insp.wait_gone(9999, 1.0, 0.0)
            out["wait:reused"] = insp.wait_gone(2000, 99.0, 0.0)
            out["wait:zombie"] = insp.wait_gone(2500, 25.0, 0.0)
            out["wait:alive"] = insp.wait_gone(2000, 20.0, 0.0)
            out["wait:alive_negative"] = insp.wait_gone(2000, 20.0, -5.0)
            out["wait:denied"] = insp.wait_gone(2800, 28.0, 0.0)
        with monkeypatch.context() as m:
            m.setitem(sys.modules, "psutil", None)
            out["no_psutil"] = outcome(S.sproc.PsutilInspector)
        return out

    result = par(scenario)
    assert result["process:2000"][1]["name"] == "srv"
    assert result["process:2100"][1]["owner"] is None and result["process:2100"][1]["name"] is None
    assert "ProcessNotFound" in result["process:9999"][1]
    assert "InspectionIndeterminate" in result["process:2400"][1]
    assert result["listen:2000"][1] == [["127.0.0.1", 5]]
    assert result["wait:gone"] is True and result["wait:alive"] is False
    assert result["sent"] == [[2000, "terminate"], [2000, "kill"], [2600, "kill"]]
    assert "InspectionIndeterminate" in result["no_psutil"][1]
    assert "InspectionIndeterminate" in result["signal:denied"][1]


def test_process_helpers_and_port_probe(par, monkeypatch):
    import errno
    import socket

    class _Sock:
        log = []
        bind_errors = {}

        def __init__(self, family=socket.AF_INET, *a, **k):
            self.family = family

        def setsockopt(self, *a):
            _Sock.log.append(("setsockopt", self.family))

        def bind(self, addr):
            _Sock.log.append(("bind", addr))
            error = _Sock.bind_errors.get(addr[0])
            if error is not None:
                raise error

        def close(self):
            _Sock.log.append(("close",))

    def scenario(S):
        out = {
            "names": [S.sproc.normalize_name(n) for n in (None, "a.exe", "A.EXE", "a", "exe", "")],
            "consts": [S.sproc.SIGTERM, S.sproc.SIGKILL, S.sproc.SIGNALS],
        }
        for key, connect_ok, bind_errors in (
            ("free", False, {}),
            ("connect_succeeds", True, {}),
            ("v4_bind_fails", False, {"127.0.0.1": OSError(errno.EADDRINUSE, "in use")}),
            ("wildcard_bind_fails", False, {"0.0.0.0": OSError(errno.EADDRINUSE, "in use")}),
            ("v6_in_use", False, {"::": OSError(errno.EADDRINUSE, "in use")}),
            ("v6_denied", False, {"::": OSError(errno.EACCES, "denied")}),
            ("v6_unsupported", False, {"::": OSError(errno.EAFNOSUPPORT, "no v6")}),
        ):
            _Sock.log = []
            _Sock.bind_errors = bind_errors
            attempts = []

            def create_connection(addr, timeout=None, _ok=connect_ok):
                attempts.append(addr)
                if _ok:
                    return contextlib.nullcontext()
                raise OSError("refused")

            with monkeypatch.context() as m:
                m.setattr(socket, "create_connection", create_connection)
                m.setattr(socket, "socket", _Sock)
                out[key] = {"free": S.sproc.port_is_free(5555), "connect": list(attempts), "log": list(_Sock.log)}
        return out

    result = par(scenario)
    assert result["names"] == [None, "a", "A", "a", "exe", ""]
    assert result["free"]["free"] is True
    assert result["connect_succeeds"]["free"] is False
    assert result["v4_bind_fails"]["free"] is False
    assert result["v6_in_use"]["free"] is False
    assert result["v6_unsupported"]["free"] is True


# --------------------------------------------------------------------------
# swapper_acceptance
# --------------------------------------------------------------------------


class _DeadThread:
    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


def _answer_for(prompt):
    match = re.search(r"What is (\d+) plus (\d+)\?", prompt)
    return str(int(match.group(1)) + int(match.group(2))) if match else None


def _raises(make):
    def urlopen_stub(request, timeout=None):
        raise make(request)

    return urlopen_stub


def test_swapper_acceptance_pieces(par, urlopen):
    class _Resp:
        def __init__(self, status, headers, body):
            self.status = status
            self.headers = headers
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def scenario(S):
        sa = S.sacc
        out = {}
        out["consts"] = [sa.EXIT_OK, sa.EXIT_FAILED, sa.EXIT_CONFIG]
        out["operands"] = [sa.operands(n) for n in range(0, 8)]
        out["chat_body"] = sa.chat_body("m", "hello", 7)
        out["service_root"] = [outcome(sa.service_root, u) for u in ("http://h:1/v1/", "http://h", "ftp://h", "")]
        report = sa.AcceptanceReport("swapper")
        report.add("one", True, "fine")
        report.add("two", False, "bad thing")
        report.add("three", False, "skipped leg", skipped=True)
        report.notes += ["note a", "note b"]
        empty = sa.AcceptanceReport("x")
        out["report"] = {
            "json": report.to_json(), "text": report.to_text(), "passed": report.passed,
            "exit": report.exit_code, "empty_text": empty.to_text(), "empty_exit": empty.exit_code,
        }
        result = sa.HttpResult(200, {}, b'{"a": 1}')
        out["result_json"] = [result.json(), sa.HttpResult(200, {}, b"nope").json(), sa.HttpResult(0, {}, b"", "e").json()]
        content = lambda body: sa.message_content(sa.HttpResult(200, {}, body))
        out["content"] = [content(json.dumps({"choices": [{"message": {"content": "x"}}]}).encode()),
                          content(json.dumps({"choices": [{"message": {}}]}).encode()),
                          content(b'{"choices": []}'), content(b"nope"), content(b'{"choices": [5]}')]
        # http_request over a patched urlopen
        urlopen.calls.clear()
        urlopen.script = {}
        fake = lambda status, headers, body: (lambda request, timeout=None: _Resp(status, headers, body))
        for key, behaviour in (
            ("ok", fake(200, {"Content-Type": "application/json"}, b'{"x": 1}')),
            ("http_error", _raises(lambda request: urllib.error.HTTPError(
                request.full_url, 503, "busy", {"X-A": "b"}, io.BytesIO(b"err")))),
            ("conn_error", _raises(lambda request: urllib.error.URLError("refused"))),
            ("empty_message", _raises(lambda request: OSError())),
        ):
            with mock.patch.object(urllib.request, "urlopen", behaviour):
                get = sa.http_request("GET", "http://h/x", timeout=2.0)
                post = sa.http_request("POST", "http://h/y", {"k": 1}, timeout=3.0)
            out[f"http:{key}"] = [get, post]
        return out

    result = par(scenario)
    assert result["consts"] == [0, 1, 2]
    assert result["operands"][0] == [23, 39]
    assert result["report"]["exit"] == 1 and result["report"]["empty_exit"] == 0
    assert result["http:http_error"][0]["status"] == 503
    assert result["http:conn_error"][0]["status"] == 0


class _SwapperServer:
    """A scripted swapper: /v1/models, /running and chat completions."""

    def __init__(self, S, *, models=("mA", "mB"), running=("mA",), wrong_answers=False,
                 models_status=200, running_status=200, chat_status=200):
        self.S = S
        self.models = models
        self.running = running
        self.wrong = wrong_answers
        self.statuses = (models_status, running_status, chat_status)
        self.calls = []

    def __call__(self, method, url, payload=None, *, timeout=30.0):
        S = self.S
        self.calls.append((method, url, payload, timeout))

        def reply(status, doc):
            return S.sacc.HttpResult(status, {}, json.dumps(doc).encode())

        if url.endswith("/v1/models"):
            return reply(self.statuses[0], {"data": [{"id": m} for m in self.models]})
        if url.endswith("/running"):
            if self.running is None:
                return S.sacc.HttpResult(404, {}, b"")
            return reply(self.statuses[1], {"running": [{"model": m} for m in self.running]})
        if url.endswith("/v1/chat/completions"):
            prompt = payload["messages"][0]["content"]
            answer = _answer_for(prompt)
            if answer is None:
                answer = "x" * 300
            elif self.wrong:
                answer = "0"
            return reply(self.statuses[2], {"choices": [{"message": {"content": answer}}]})
        return S.sacc.HttpResult(0, {}, b"", "unexpected")


def test_run_swapper_acceptance(par, monkeypatch):
    def scenario(S):
        out = {}
        for key, server_kwargs, rounds in (
            ("healthy", {}, 3),
            ("single_round", {}, 1),
            ("wrong_answers", {"wrong_answers": True}, 2),
            ("chat_fails", {"chat_status": 500}, 2),
            ("discovery_fails", {"models_status": 500}, 1),
            ("not_swapper", {"running": None}, 1),
            ("one_model", {"models": ("mA",)}, 1),
            ("resident_wrong", {"running": ("mB",)}, 2),
            ("rounds_zero", {}, 0),
        ):
            server = _SwapperServer(S, **server_kwargs)
            with monkeypatch.context() as m:
                m.setattr(S.sacc, "http_request", server)
                m.setattr(S.sacc, "_spawn", lambda fn: (_DeadThread(), [fn()]))
                m.setattr(S.sacc.time, "sleep", lambda s: None)
                report = outcome(S.sacc.run_swapper_acceptance, "http://127.0.0.1:8080/v1", rounds,
                                 settle_s=0.0, poll_s=0.0, busy_setup_s=0.0, request_timeout=5.0)
                out[key] = {"report": report, "calls": [(c[0], c[1], c[3]) for c in server.calls]}
        with monkeypatch.context() as m:
            m.setattr(S.sacc, "http_request", _SwapperServer(S))
            out["bad_url"] = outcome(S.sacc.run_swapper_acceptance, "ftp://nope", 1)
            out["running_models"] = [
                outcome(S.sacc.running_models, "http://r"),
            ]
        return out

    result = par(scenario)

    def failed(key):
        return [c["name"] for c in result[key]["report"][1]["checks"] if not c["passed"] and not c["skipped"]]

    healthy = result["healthy"]["report"]
    assert healthy[0] == "ok" and healthy[1]["kind"] == "swapper"
    names = [c["name"] for c in healthy[1]["checks"]]
    assert any(n.startswith("round 3") for n in names) and any("overlap" in n for n in names)
    assert not any(n.startswith("round") for n in failed("healthy"))
    assert any(n.startswith("round") and "correctness" in n for n in failed("wrong_answers"))
    assert any("correctness" in n for n in failed("chat_fails"))
    assert result["rounds_zero"]["report"][0] == "raise"
    assert "AcceptanceConfigError" in result["rounds_zero"]["report"][1]
    assert result["bad_url"][0] == "raise"
    assert failed("discovery_fails") == ["discovery: /v1/models answers"]
    assert failed("not_swapper") == ["discovery: /running answers"]
    assert failed("one_model") == ["discovery: two models to alternate"]

