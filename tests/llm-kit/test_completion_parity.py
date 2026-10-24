"""Differential parity: llm_kit.completion vs the legacy neutral completion slice.

The legacy modules are llm_scripting_kit.completion.{capabilities, contract,
contract_types, halt, prompt_fold, requirements, results, skill_context,
skill_context_types, types} plus llm_scripting_kit.request_protocol. The final
package must give the same results, exceptions and messages for the same
inputs, and must expose the same module surface. Every scenario runs once per
side and the two outcome lists are compared; a scenario that only produced
errors (or only successes) on both sides would prove little, so each test also
pins the shape of what it compared.
"""

from __future__ import annotations

import copy
import dataclasses
import importlib
import inspect
import json
import contextlib
import math
import pickle
import subprocess
import sys
import types as _pytypes
from collections.abc import Mapping
from pathlib import Path

import pytest

from final_wording import max_tokens_default_to_final, to_final

_REPO = Path(__file__).resolve().parents[2]
_LEGACY_LIB = _REPO / "plugins" / "llm-scripting-kit" / "lib"
_FINAL_LIB = __import__("pathlib").Path(__import__("llm_kit").__file__).resolve().parent.parent

COMPLETION_MODULES = (
    "capabilities",
    "contract",
    "contract_types",
    "halt",
    "prompt_fold",
    "requirements",
    "results",
    "skill_context",
    "skill_context_types",
    "types",
)
ALL_MODULES = COMPLETION_MODULES + ("request_protocol",)


class _Side:
    """One implementation: its modules as attributes."""

    def __init__(self, label: str, modules: dict) -> None:
        self.label = label
        self.modules = modules
        for name, mod in modules.items():
            setattr(self, name, mod)


def _load_legacy() -> _Side:
    sys.path.insert(0, str(_LEGACY_LIB))
    try:
        mods = {
            name: importlib.import_module(f"llm_scripting_kit.completion.{name}")
            for name in COMPLETION_MODULES
        }
        mods["request_protocol"] = importlib.import_module(
            "llm_scripting_kit.request_protocol"
        )
        mods["adapter_capabilities"] = importlib.import_module(
            "llm_scripting_kit.completion.adapter_capabilities"
        )
    finally:
        sys.path.remove(str(_LEGACY_LIB))
    return _Side("legacy", mods)


def _load_final() -> _Side:
    mods = {
        name: importlib.import_module(f"llm_kit.completion.{name}")
        for name in ALL_MODULES
    }
    return _Side("final", mods)


LEGACY = _load_legacy()
FINAL = _load_final()


# --------------------------------------------------------------------------
# Normalization: turn side-specific objects into comparable plain data.
# --------------------------------------------------------------------------


_LEGACY_ACTIVE = [False]


@contextlib.contextmanager
def legacy_wording():
    """While active, ``norm`` words legacy text in its final wording.

    The named deliberate wording differences live in ``final_wording.RENAMES``
    (each proved against the live sources by ``test_final_messages.py``).
    """
    _LEGACY_ACTIVE[0] = True
    try:
        yield
    finally:
        _LEGACY_ACTIVE[0] = False


_MAX_TOKENS_TYPE_TEXT = {"int": "Optional[int]", "<class 'int'>": "typing.Optional[int]"}


def norm(value, depth: int = 0):
    if depth > 25:
        raise AssertionError("normalization depth exceeded")
    d = depth + 1
    if isinstance(value, str):
        return to_final(value) if _LEGACY_ACTIVE[0] else value
    if value is None or isinstance(value, (bool, int, bytes)):
        return value
    if isinstance(value, float):
        return "nan" if math.isnan(value) else value
    if isinstance(value, BaseException):
        return (
            "exc",
            type(value).__name__,
            norm(str(value), d),
            norm(dict(vars(value)), d),
            type(value.__cause__).__name__ if value.__cause__ is not None else None,
        )
    if isinstance(value, type):
        return ("type", value.__name__)
    if dataclasses.is_dataclass(value):
        if _LEGACY_ACTIVE[0]:
            value = max_tokens_default_to_final(value, params=False)  # declared: llm-kit's output cap is unset by default
        return (
            "dc",
            type(value).__name__,
            {
                f.name: norm(getattr(value, f.name), d)
                for f in dataclasses.fields(value)
                if f.name not in _FINAL_ONLY_FIELDS
            },
        )
    if isinstance(value, Mapping):
        if _LEGACY_ACTIVE[0] and value.get("max_tokens") in _MAX_TOKENS_TYPE_TEXT:
            # declared: the option type of max_tokens is Optional[int] in llm-kit
            value = {**value, "max_tokens": _MAX_TOKENS_TYPE_TEXT[value["max_tokens"]]}
        return ("map", type(value).__name__, {k: norm(v, d) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return ("seq", type(value).__name__, [norm(v, d) for v in value])
    if isinstance(value, (set, frozenset)):
        return ("set", type(value).__name__, sorted(repr(v) for v in value))
    if isinstance(value, Path):
        return ("path", str(value))
    if isinstance(value, _pytypes.ModuleType):
        return ("module", value.__name__.rsplit(".", 1)[-1])
    if hasattr(value, "__dict__") and not callable(value):
        return ("obj", type(value).__name__, norm(dict(vars(value)), d))
    return ("opaque", type(value).__name__)


def outcome(fn, *args, **kwargs):
    try:
        return ("ok", norm(fn(*args, **kwargs)))
    except Exception as exc:  # noqa: BLE001 - parity compares any exception
        return ("raise", norm(exc))


def check(scenario, *args):
    """Run a scenario on both sides; assert identical labelled outcomes."""
    got = scenario(FINAL, *args)
    with legacy_wording():
        want = scenario(LEGACY, *args)
    assert [label for label, _ in got] == [label for label, _ in want]
    for (label, g), (_, w) in zip(got, want):
        assert g == w, f"{label}: final {g!r} != legacy {w!r}"
    return got


def kinds(results) -> set:
    return {r[0] for _, r in results}


# --------------------------------------------------------------------------
# Capability records: legacy records, ported field-for-field to the final
# classes, so both sides are driven with the same advertised data.
# --------------------------------------------------------------------------


def _final_dataclass_registry() -> dict:
    registry = {}
    for mod in (FINAL.capabilities,):
        for name, obj in vars(mod).items():
            if (
                isinstance(obj, type)
                and dataclasses.is_dataclass(obj)
                and obj.__module__ == mod.__name__
            ):
                registry[name] = obj
    return registry


_REGISTRY = _final_dataclass_registry()


def port(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        cls = _REGISTRY[type(value).__name__]
        return cls(
            **{
                f.name: port(getattr(value, f.name))
                for f in dataclasses.fields(value)
                if f.init
            }
        )
    if isinstance(value, Mapping):
        return {k: port(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return tuple(port(v) for v in value)
    if isinstance(value, str):
        # the ported side speaks the final wording, as the legacy side does under
        # ``legacy_wording`` (final_wording.RENAMES)
        return to_final(value)
    return value


_LEGACY_RECORDS = dict(LEGACY.adapter_capabilities.ADAPTER_CAPABILITIES)
_RECORDS = {
    "legacy": _LEGACY_RECORDS,
    "final": {k: port(v) for k, v in _LEGACY_RECORDS.items()},
}


def records(side: _Side) -> dict:
    return _RECORDS[side.label]


def record_variants(side: _Side):
    """Every adapter record plus structural variants for refusal paths."""
    out = []
    for name, rec in records(side).items():
        out.append((name, rec))
        so = rec.structured_output
        for tag, change in (
            ("no-delivery", {"contract_delivery": None}),
            ("prompt-delivery", {"contract_delivery": "prompt"}),
            ("native-delivery", {"contract_delivery": "native"}),
            ("no-policies", {"policies": ()}),
            ("strict-class", {"contract_schema_class": "openai-strict"}),
        ):
            out.append(
                (
                    f"{name}/{tag}",
                    dataclasses.replace(
                        rec, structured_output=dataclasses.replace(so, **change)
                    ),
                )
            )
    return out


# --------------------------------------------------------------------------
# 1. Module surface
# --------------------------------------------------------------------------

# Final-only call-mode vocabulary (declared difference, see _FINAL_ONLY_FIELDS).
_FINAL_ONLY_NAMES = frozenset(
    {"CALL_MODES", "CallModeError", "MODE_COMPLETION", "MODE_WORKSPACE", "require_call_mode"}
)

_SKIP_NAMES = {
    *_FINAL_ONLY_NAMES,
    # Private implementation helpers for the 1.7.1 targeted-floor fix.
    "_disallowed_tool_clauses", "_with_denied_names", "_SUBAGENT_SPAWN",
    "annotations",
    "__builtins__",
    "__cached__",
    "__doc__",
    "__file__",
    "__loader__",
    "__name__",
    "__package__",
    "__spec__",
}
_SKIP_CLASS_ATTRS = {
    "__dict__",
    "__weakref__",
    "__doc__",
    "__module__",
    "__qualname__",
    "__annotations__",
    "__annotate__",
    "__annotate_func__",
    "__annotations_cache__",
    "__firstlineno__",
    "__static_attributes__",
    "__dataclass_fields__",
    "__dataclass_params__",
    "__orig_bases__",
    "__parameters__",
    "__abstractmethods__",
    "__subclasshook__",
    "__protocol_attrs__",
    "__non_callable_proto_members__",
    "_is_protocol",
    "_is_runtime_protocol",
}


# Declared difference: the final Capabilities record carries ``call_modes``; the
# frozen legacy record has none. Shapes, signatures and JSON leave it out here,
# and tests/llm-kit/test_call_modes.py pins the final side.
# LLMResponse also gains ``reported_model`` and the ``estimated_cost_*`` pair in the final library.
_FINAL_ONLY_FIELDS = frozenset({"call_modes", "reported_model", "estimated_cost_usd", "estimated_cost_source", "reasoning_tokens"})
_CALL_MODES_PARAM = ", call_modes: 'Tuple[str, ...]' = ('completion',)"
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


def _without_final_only(document: dict) -> dict:
    return {k: v for k, v in document.items() if k not in _FINAL_ONLY_FIELDS}


def _sig(fn) -> str:
    try:
        # Declared difference: BackendOptions.max_tokens is unset by default (final_wording).
        return _without_final_only_params(str(inspect.signature(fn))).replace(
            "max_tokens: 'int' = 4096", "max_tokens: 'Optional[int]' = None"
        )
    except (TypeError, ValueError):
        return "nosig"


def _is_project(obj) -> bool:
    mod = getattr(obj, "__module__", None)
    return isinstance(mod, str) and mod.startswith(("llm_kit", "llm_scripting_kit"))


def describe(obj):
    if inspect.ismodule(obj):
        return ("module", obj.__name__.rsplit(".", 1)[-1])
    if inspect.isclass(obj):
        if not _is_project(obj):
            return ("external", obj.__module__, obj.__qualname__)
        info = {
            "mro": [b.__qualname__ for b in obj.__mro__],
            "attrs": {
                name: describe_attr(val)
                for name, val in sorted(obj.__dict__.items())
                if name not in _SKIP_CLASS_ATTRS and not name.startswith("_abc")
                and not (dataclasses.is_dataclass(obj) and name in _FINAL_ONLY_FIELDS)
            },
        }
        if dataclasses.is_dataclass(obj):
            params = obj.__dataclass_params__
            info["dataclass"] = {
                "frozen": params.frozen,
                "eq": params.eq,
                "order": params.order,
                "fields": [
                    (
                        f.name,
                        str(f.type),
                        "MISSING" if f.default is dataclasses.MISSING else norm(f.default),
                        None
                        if f.default_factory is dataclasses.MISSING
                        else f.default_factory.__qualname__.rsplit(".", 1)[-1],
                        f.init,
                        f.repr,
                        f.compare,
                        f.hash,
                    )
                    for f in dataclasses.fields(obj)
                    if f.name not in _FINAL_ONLY_FIELDS
                ],
            }
        if obj.__name__ == "BackendOptions":
            # Declared difference (final_wording.max_tokens_default_to_final): max_tokens is
            # Optional[int] = None in llm-kit, int = 4096 in the frozen legacy record.
            info["attrs"]["max_tokens"] = ("val", None)
            info["dataclass"]["fields"] = [
                ("max_tokens", "Optional[int]", None, None, True, True, True, None)
                if field[0] == "max_tokens" else field
                for field in info["dataclass"]["fields"]
            ]
        return ("class", info)
    if inspect.isroutine(obj):
        if not _is_project(obj):
            return ("external", getattr(obj, "__module__", None), obj.__qualname__)
        return ("func", _sig(obj))
    mod = getattr(obj, "__module__", None)
    if isinstance(mod, str) and not mod.startswith(("llm_kit", "llm_scripting_kit")):
        return ("external", mod, getattr(obj, "__qualname__", repr(obj)))
    return ("val", norm(obj))


def describe_attr(val):
    if isinstance(val, tuple) and val and all(isinstance(v, str) for v in val):
        val = tuple(v for v in val if v not in _FINAL_ONLY_FIELDS)  # __match_args__
    if isinstance(val, staticmethod):
        return ("static", _sig(val.__func__))
    if isinstance(val, classmethod):
        return ("classmethod", _sig(val.__func__))
    if isinstance(val, property):
        return ("property", _sig(val.fget) if val.fget else None)
    if inspect.isroutine(val):
        return ("func", _sig(val))
    return ("val", norm(val))


def module_surface(mod) -> dict:
    return {
        name: (
            ("val", norm([n for n in obj if n not in _FINAL_ONLY_NAMES]))
            if name == "__all__"
            else describe(obj)
        )
        for name, obj in sorted(vars(mod).items())
        if name not in _SKIP_NAMES
    }


@pytest.mark.parametrize("name", ALL_MODULES)
def test_module_surface_parity(name):
    final = module_surface(FINAL.modules[name])
    with legacy_wording():
        legacy = module_surface(LEGACY.modules[name])
    assert sorted(final) == sorted(legacy)
    for key in legacy:
        if name in {"capabilities", "requirements"} and key == "DENY_TOOL_NAMES":
            # Published 1.7.2 adds the two remaining native child-agent tools.
            # Pin that sole table difference; every other subject stays equal.
            old_denies = dict(LEGACY.modules[name].DENY_TOOL_NAMES)
            new_denies = dict(FINAL.modules[name].DENY_TOOL_NAMES)
            assert old_denies.pop("subagent-spawn") == ("Task", "Agent")
            assert new_denies.pop("subagent-spawn") == ("Task", "Agent", "Workflow", "SendMessage")
            assert new_denies == old_denies
        elif name == "capabilities" and key == "_TOOL_SUBJECTS":
            old_subjects = dict(LEGACY.capabilities._TOOL_SUBJECTS)
            new_subjects = dict(FINAL.capabilities._TOOL_SUBJECTS)
            assert old_subjects.keys() == new_subjects.keys()
            assert all(type(new_subjects[subject]) is type(old_subjects[subject]) for subject in old_subjects)
            assert old_subjects.pop("subagent-spawn") == frozenset({"task", "agent", "subagent"})
            assert new_subjects.pop("subagent-spawn") == frozenset({"task", "agent", "subagent", "workflow", "sendmessage"})
            assert new_subjects == old_subjects
        else:
            assert final[key] == legacy[key], f"{name}.{key}"
    # Not vacuous: real modules, with real content.
    assert len(legacy) >= 2


# --------------------------------------------------------------------------
# 2. AgentTimeoutError now lives in the neutral halt module
# --------------------------------------------------------------------------


def _strip_pkg(text):
    return text.replace("llm_scripting_kit.completion.", "PKG.").replace("llm_kit.completion.", "PKG.")


def _timeout(side: _Side, message="boom", **kw):
    args = dict(cmd=["claude", "-p"], elapsed_s=7, stdout="out", stderr="err")
    args.update(kw)
    return side.halt.AgentTimeoutError(message, **args)


def test_agent_timeout_error_parity_and_location():
    def scenario(S):
        exc = _timeout(S)
        return [
            ("fields", outcome(lambda: exc)),
            ("is-runtime", ("ok", isinstance(exc, RuntimeError))),
            ("mro", ("ok", [c.__name__ for c in type(exc).__mro__])),
            ("missing-kw", outcome(S.halt.AgentTimeoutError, "m")),
            ("positional", outcome(S.halt.AgentTimeoutError, "m", ["c"], 1, "o", "e")),
            ("raise", outcome(_raiser, _timeout(S, "x y"))),
            ("claude", outcome(S.halt.classify_claude_exception, exc)),
            ("codex", outcome(S.halt.classify_codex_exception, exc)),
            ("opencode", outcome(S.halt.classify_opencode_exception, exc)),
        ]

    results = check(scenario)
    assert kinds(results) == {"ok", "raise"}
    assert FINAL.halt.AgentTimeoutError.__module__ == "llm_kit.completion.halt"


def _raiser(exc):
    raise exc


def test_completion_imports_no_concrete_layer():
    """Importing every completion module loads only completion and schema."""
    code = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(_FINAL_LIB)!r})\n"
        + "".join(f"import llm_kit.completion.{m}\n" for m in ALL_MODULES)
        + "print(json.dumps(sorted(sys.modules)))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    loaded = json.loads(proc.stdout.strip().splitlines()[-1])
    ours = [m for m in loaded if m == "llm_kit" or m.startswith("llm_kit.")]
    for m in ours:
        assert (
            m == "llm_kit"
            or m.startswith("llm_kit.completion")
            or m.startswith("llm_kit.schema")
        ), m
    for forbidden in (
        "llm_scripting_kit",
        "llm_kit._copy",
        "llm_kit.adapters",
        "llm_kit.dispatch",
        "openai",
        "bootstrap_lib",
        "yaml",
        "psutil",
    ):
        assert not any(
            m == forbidden or m.startswith(forbidden + ".") for m in loaded
        ), forbidden
    assert "llm_kit.completion.halt" in loaded


def _filled_records(mod) -> list:
    """Names in ``mod`` bound to a filled Capabilities record (or a table of them)."""
    caps = FINAL.capabilities.Capabilities
    found = []
    for name, value in vars(mod).items():
        if name.startswith("__"):
            continue
        if isinstance(value, caps):
            found.append(name)
        elif isinstance(value, Mapping) and any(isinstance(v, caps) for v in value.values()):
            found.append(name)
    return found


def test_completion_holds_types_not_filled_capability_records():
    """Capability TYPES stay in completion; filled records belong to adapters.

    Every module under llm_kit.completion is imported and scanned, so a filled
    record (or an adapter_capabilities table) moved into the package turns this
    red even when its imports are all allowed by the edge table.
    """
    import pkgutil

    import llm_kit.completion as pkg

    names = sorted(m.name for m in pkgutil.iter_modules(pkg.__path__))
    # invoke_protocol and workspace_manifest (protocol 2 and its filesystem receipt) are final-only too.
    # factory_protocols (I6) holds the injected-factory contracts; it is a final module
    # only, so it joins the expected list here and not in ALL_MODULES, which also feeds
    # the legacy side of the module-by-module comparison.
    assert names == sorted(ALL_MODULES + ("factory_protocols", "invoke_protocol", "workspace_manifest")), names
    for name in names:
        mod = importlib.import_module(f"llm_kit.completion.{name}")
        assert _filled_records(mod) == [], (name, _filled_records(mod))
    # The scan is live: it finds a table of records when one is present.
    assert _filled_records(_Side("probe", {"x": _pytypes.SimpleNamespace(**{"T": {"a": records(FINAL)["openrouter"]}})}).x) == ["T"]


# --------------------------------------------------------------------------
# 3. halt
# --------------------------------------------------------------------------


def _halt_texts() -> list:
    h = LEGACY.halt
    markers = []
    for const in (
        "_RATE_LIMIT_MARKERS",
        "_AUTH_MARKERS",
        "_CODEX_RATE_LIMIT_MARKERS",
        "_CODEX_AUTH_MARKERS",
        "_OPENCODE_AUTH_MARKERS",
        "_QUOTA_MARKERS",
    ):
        markers.extend(getattr(h, const))
    texts = ["", "all is well", "x" * 50]
    for m in markers:
        texts += [m, m.upper(), f"prefix {m} suffix"]
    texts += [
        f"{h._RATE_LIMIT_MARKERS[0]} and {h._AUTH_MARKERS[0]}",
        "status 429 queue is full, retry-after: 3",
        "Error code: 503 server overloaded try again in 500ms",
        "status code 429 try again in 2 seconds",
        "status 503 request timed out",
        "status 429 insufficient_quota billing",
        '{"error": {"code": "request_queue_timeout"}} status 503',
        "request exceeded the timeout",
    ]
    return texts


def _exc_variants(S: _Side, text: str) -> list:
    class Boom(Exception):
        pass

    plain = RuntimeError(text)

    with_status = Boom(text)
    with_status.status_code = 429
    with_status.headers = {"retry-after": "4"}

    resp = _pytypes.SimpleNamespace(status_code=503, headers={"Retry-After": "Wed, 21 Oct"})
    via_response = Boom(text)
    via_response.response = resp

    channels = Boom("opaque failure")
    channels.stderr = text
    channels.stdout = text

    stdout_only = Boom("opaque failure")
    stdout_only.stdout = text
    stdout_only.stderr = ""

    wrapped = RuntimeError("outer")
    wrapped.__cause__ = RuntimeError(text)

    return [
        ("plain", plain),
        ("status", with_status),
        ("response", via_response),
        ("channels", channels),
        ("stdout-only", stdout_only),
        ("wrapped", wrapped),
        ("timeout", _timeout(S, text)),
        ("halt-error", S.halt.HaltError(S.halt.HALT_BACKPRESSURE, text, 2.5)),
    ]


def _install_fake_openai(monkeypatch):
    fake = _pytypes.ModuleType("openai")

    class AuthenticationError(Exception):
        pass

    class RateLimitError(Exception):
        pass

    class APIStatusError(Exception):
        def __init__(self, message, status_code=None):
            super().__init__(message)
            self.status_code = status_code

    fake.AuthenticationError = AuthenticationError
    fake.RateLimitError = RateLimitError
    fake.APIStatusError = APIStatusError
    monkeypatch.setitem(sys.modules, "openai", fake)
    return fake


def _openai_cases(fake) -> list:
    wrapped = RuntimeError("outer")
    wrapped.__cause__ = fake.AuthenticationError("inner")
    return [
        ("auth", fake.AuthenticationError("denied")),
        ("rate", fake.RateLimitError("slow down")),
        ("402", fake.APIStatusError("pay up", 402)),
        ("403", fake.APIStatusError("suspended", 403)),
        ("500", fake.APIStatusError("oops", 500)),
        ("rate-queue", fake.RateLimitError("status 429 queue is full")),
        ("wrapped", wrapped),
    ]


@pytest.mark.parametrize("openai_mode", ["absent", "fake"])
def test_halt_classifiers_parity(monkeypatch, openai_mode):
    fake = None
    if openai_mode == "absent":
        monkeypatch.setitem(sys.modules, "openai", None)
    else:
        fake = _install_fake_openai(monkeypatch)

    def scenario(S):
        h = S.halt
        out = []
        for i, text in enumerate(_halt_texts()):
            out.append((f"text{i}/halt", outcome(h.classify_halt_text, text)))
            out.append((f"text{i}/codex", outcome(h.classify_codex_text, text)))
            for tag, exc in _exc_variants(S, text):
                for fn in (
                    "classify_openai_exception",
                    "classify_claude_exception",
                    "classify_codex_exception",
                    "classify_opencode_exception",
                    "classify_backpressure",
                ):
                    out.append((f"text{i}/{tag}/{fn}", outcome(getattr(h, fn), exc)))
                out.append(
                    (f"text{i}/{tag}/payload", outcome(h.halt_payload, h.HALT_BACKPRESSURE, exc))
                )
        if fake is not None:
            for tag, exc in _openai_cases(fake):
                out.append((f"openai/{tag}", outcome(h.classify_openai_exception, exc)))
        return out

    results = check(scenario)
    seen = {json.dumps(r[1], default=str) for _, r in results if r[0] == "ok"}
    # Not vacuous: every halt kind is produced somewhere.
    flat = json.dumps(sorted(seen))
    for kind in ("auth", "rate_limit", "backpressure", "insufficient_credit"):
        if kind == "insufficient_credit" and fake is None:
            continue
        assert kind in flat, kind


def test_halt_types_parity():
    def scenario(S):
        h = S.halt
        err = h.HaltError(h.HALT_AUTH, "bad key")
        return [
            ("halt-error", outcome(lambda: err)),
            ("halt-error-str", ("ok", str(err))),
            ("halt-error-bare", outcome(lambda: h.HaltError(h.HALT_QUOTA))),
            ("halt-error-str-bare", ("ok", str(h.HaltError(h.HALT_QUOTA)))),
            ("halt-error-bad", outcome(h.HaltError)),
            ("backpressure", outcome(h.Backpressure, 429, 1.5)),
            ("backpressure-frozen", outcome(setattr, h.Backpressure(429), "status", 1)),
            ("payload-plain", outcome(h.halt_payload, h.HALT_AUTH)),
            ("payload-own", outcome(h.halt_payload, h.HALT_BACKPRESSURE, h.HaltError("backpressure", "q", 9.0))),
            ("payload-none", outcome(h.halt_payload, h.HALT_BACKPRESSURE)),
            (
                "consts",
                (
                    "ok",
                    [
                        h.HALT_AUTH,
                        h.HALT_RATE_LIMIT,
                        h.HALT_INSUFFICIENT_CREDIT,
                        h.HALT_QUOTA,
                        h.HALT_BACKPRESSURE,
                    ],
                ),
            ),
        ]

    results = check(scenario)
    assert kinds(results) == {"ok", "raise"}


# --------------------------------------------------------------------------
# 4. prompt_fold and types
# --------------------------------------------------------------------------


def test_prompt_fold_parity():
    def scenario(S):
        return [
            (f"{s!r}/{u!r}/{sep!r}", outcome(S.prompt_fold.fold_prompt, s, u, sep))
            for s in ("", "sys")
            for u in ("", "usr")
            for sep in ("", "\n\n", "---")
        ] + [("bad", outcome(S.prompt_fold.fold_prompt, "a")), ("none", outcome(S.prompt_fold.fold_prompt, None, "u", "-"))]

    results = check(scenario)
    assert kinds(results) == {"ok", "raise"}


def test_types_parity(tmp_path):
    class Fake:
        name = "fake"

        def complete(self, system, user, *, model, options=None):
            return None

        def classify_halt(self, exc):
            return None

    class NotABackend:
        name = "x"

    def scenario(S):
        t = S.types
        resp = t.LLMResponse(text="hi", model="m")
        full = t.LLMResponse(
            text="t",
            model="m",
            reasoning="r",
            finish_reason="stop",
            input_tokens=1,
            output_tokens=2,
            cache_hit_tokens=3,
            total_tokens=4,
            wall_ms=5,
            attempts=2,
            from_cache=True,
            status=t.ERROR,
            error=t.ResponseError("c", "msg"),
            dropped_params=("a",),
            forwarded_params=("b",),
            execution_controls_applied=("c",),
            structured={"k": [1]},
            started_at="s",
            ended_at="e",
            reported_cost_usd=0.5,
            reported_cost_source=t.COST_SOURCE_PROVIDER,
        )
        opts = t.BackendOptions(cwd=tmp_path, extras={"a": 1}, effort="high")
        return [
            ("resp-default", outcome(lambda: resp)),
            ("resp-full", outcome(lambda: full)),
            ("resp-missing", outcome(t.LLMResponse, text="x")),
            ("resp-mutable", outcome(setattr, resp, "text", "changed")),
            ("opts-default", outcome(t.BackendOptions)),
            ("opts-set", outcome(lambda: opts)),
            ("opts-frozen", outcome(setattr, opts, "effort", "low")),
            ("opts-unknown", outcome(t.BackendOptions, nope=1)),
            ("opts-hash", outcome(hash, t.BackendOptions())),
            ("error-json", outcome(t.ResponseError("c", "m").to_json)),
            ("error-frozen", outcome(setattr, t.ResponseError("c"), "code", "d")),
            (
                "empty",
                outcome(
                    lambda: t.EmptyCompletionError(
                        "m", reasoning="r", finish_reason="length", output_tokens=3
                    )
                ),
            ),
            ("empty-default", outcome(lambda: t.EmptyCompletionError("m"))),
            ("empty-isinst", ("ok", issubclass(t.EmptyCompletionError, RuntimeError))),
            ("backend-yes", ("ok", isinstance(Fake(), t.LLMBackend))),
            ("backend-no", ("ok", isinstance(NotABackend(), t.LLMBackend))),
            ("cost-sources", ("ok", list(t.COST_SOURCES))),
            ("states", ("ok", [t.COMPLETED, t.TIMEOUT, t.ERROR])),
        ] + [
            (f"cost-{v!r}", outcome(t.valid_reported_cost, v))
            for v in (None, True, "1", 0, 3, 2.5, -1, -0.0, float("nan"), float("inf"), [1])
        ]

    results = check(scenario)
    assert kinds(results) == {"ok", "raise"}


# --------------------------------------------------------------------------
# 5. capabilities, requirements, results
# --------------------------------------------------------------------------


def test_capability_records_parity():
    def scenario(S):
        c = S.capabilities
        out = []
        for name, rec in records(S).items():
            out.append((f"{name}/json", ("ok", norm(json.dumps(_without_final_only(rec.to_json()))))))
            out.append((f"{name}/dc", outcome(lambda r=rec: r)))
            for subject in (c.FILESYSTEM_WRITE, c.SHELL_EXEC, c.SUBAGENT_SPAWN, "other"):
                out.append((f"{name}/denies/{subject}", outcome(rec.denies, subject)))
            for param in ("max_tokens", "effort", "extras", "nope"):
                out.append((f"{name}/honors/{param}", outcome(rec.honors, param)))
        for disallowed in (None, "", "Bash Edit", "Bash(git *) Edit", "Read", "Write,Task", "x y z", 5):
            out.append((f"subjects/{disallowed!r}", outcome(c.subjects_for_disallowed_tools, disallowed)))
        out.append(("param-min", outcome(lambda: c.ParamCapability("int").to_json())))
        out.append(("param-full", outcome(lambda: c.ParamCapability("str", c.PASSTHROUGH, ("a",), ("d",), "e", "n").to_json())))
        out.append(("caps-min", outcome(lambda: _without_final_only(c.Capabilities("x").to_json()))))
        out.append(("caps-frozen", outcome(setattr, c.Capabilities("x"), "adapter", "y")))
        return out

    results = check(scenario)
    assert len(results) > 40
    assert kinds(results) == {"ok", "raise"}


_REQUIREMENTS = [
    None,
    {},
    ["max_tokens"],
    ["max_tokens", "nope"],
    "effort",
    {"params": ["effort"]},
    {"params": {"effort": {"type": "str"}}},
    {"params": {"nope": False}},
    {"params": {"effort": False}},
    {"honors": ["max_tokens"]},
    {"guarantees": ["filesystem-write"]},
    {"guarantees": {"filesystem-write": True, "shell-exec": False}},
    {"denies": ["shell-exec", "subagent-spawn"]},
    {"guarantees": ["filesystem-write", "shell-exec", "subagent-spawn"]},
    {"structured": {"policies": ["validated-result"]}},
    {"structured_output": {"policies": ["native-required"]}},
    {"structured_output": {"mode": "native"}},
    {"structured_output": {"contract_schema_class": "json-schema-subset"}},
    {"skill_context": {"delivery": "system-message"}},
    {"adapter": "claude-cli"},
    {"system_prompt.mode": "native-role"},
    {"execution_controls": ["text-only-mode"]},
    {"unknown.path": 1},
    {"params": 5},
    5,
    ("a",),
]


def test_match_capabilities_parity():
    def scenario(S):
        out = []
        for name, rec in records(S).items():
            for i, req in enumerate(_REQUIREMENTS):
                out.append((f"{name}/{i}/obj", outcome(S.requirements.match_capabilities, rec, req)))
                out.append((f"{name}/{i}/json", outcome(S.requirements.match_capabilities, rec.to_json(), req)))
                out.append((f"{name}/{i}/names", outcome(S.requirements.required_guarantees, req)))
        return out

    results = check(scenario)
    assert kinds(results) == {"ok", "raise"}
    oks = {json.dumps(r[1]) for _, r in results if r[0] == "ok"}
    assert {"true", "false"} <= oks


@dataclasses.dataclass(frozen=True)
class _ArmableDataclass:
    name: str = "dc"
    text_only: bool = False


class _ArmableMethod:
    name = "method"

    def with_text_only(self):
        return "armed-by-method"


class _Unarmable:
    name = "plain"


def test_arm_call_and_requirements_parity():
    backends = [_ArmableDataclass(), _ArmableDataclass(text_only=True), _ArmableMethod(), _Unarmable()]
    reqs = [
        None,
        {},
        {"guarantees": ["filesystem-write"]},
        {"guarantees": ["shell-exec"]},
        {"guarantees": ["filesystem-write", "shell-exec", "subagent-spawn"]},
        {"guarantees": ["bogus-subject"]},
        {"denies": ["subagent-spawn"]},
    ]

    def scenario(S):
        out = []
        options = [S.types.BackendOptions(), S.types.BackendOptions(disallowed_tools="Bash, Edit")]
        def comparable_call(backend, opt, req, rec):
            armed = S.requirements.arm_call(backend, opt, req, rec)
            raw = armed.options.disallowed_tools
            if name == "opencode-cli" and "subagent-spawn" in S.requirements.required_guarantees(req):
                # The published 1.7.2 shared deny vector also feeds OpenCode.
                # Pin each exact suffix, then compare all remaining fields
                # and tool bytes through the unchanged differential.
                suffix = "Task Agent" + (" Workflow SendMessage" if S.label == "final" else "")
                assert raw.endswith(suffix)
                if S.label == "final":
                    raw = raw.removesuffix(" Workflow SendMessage")
                    armed = dataclasses.replace(armed, options=dataclasses.replace(
                        armed.options, disallowed_tools=raw))
            if raw and "(" not in raw:
                # Deliberate difference: final arming preserves the raw prefix;
                # compare whole-tool meaning here. Scoped bytes are pinned in
                # test_targeted_deny_floor.py, without this normalization.
                armed = dataclasses.replace(armed, options=dataclasses.replace(
                    armed.options, disallowed_tools=" ".join(raw.replace(",", " ").split())))
            return armed
        for name, rec in records(S).items():
            for bi, backend in enumerate(backends):
                for ri, req in enumerate(reqs):
                    tag = f"{name}/b{bi}/r{ri}"
                    out.append((f"{tag}/req", outcome(S.requirements.arm_requirements, backend, req, rec)))
                    for oi, opt in enumerate(options):
                        if name == "claude-cli" and req == {"denies": ["subagent-spawn"]} and not getattr(backend, "text_only", False):
                            # Deliberate 1.7.1 behavior: child-only denial uses
                            # named tools instead of disabling all task tools;
                            # 1.7.2 also denies Workflow and SendMessage.
                            # Broad guarantees and arm_requirements stay in
                            # the unchanged differential above and below.
                            if S.label == "final":
                                for record in (rec, rec.to_json()):
                                    armed = S.requirements.arm_call(backend, opt, req, record)
                                    assert armed.backend is backend
                                    assert armed.controls == ("disallowed-tools",)
                                    prefix = opt.disallowed_tools or ""
                                    assert armed.options.disallowed_tools == prefix + (" " if prefix else "") + "Task Agent Workflow SendMessage"
                            continue
                        out.append((f"{tag}/o{oi}/call", outcome(comparable_call, backend, opt, req, rec)))
                        out.append((f"{tag}/o{oi}/json", outcome(comparable_call, backend, opt, req, rec.to_json())))
        out.append(("no-record", outcome(S.requirements.arm_call, _Unarmable(), S.types.BackendOptions(), {"guarantees": ["shell-exec"]})))
        out.append(("armed-call", outcome(S.requirements.ArmedCall, 1, 2)))
        return out

    results = check(scenario)
    assert kinds(results) == {"ok", "raise"}
    text = json.dumps([r for _, r in results], default=str)
    assert "armed-by-method" in text and "disallowed_tools" in text


def test_results_parity():
    def scenario(S):
        r = S.results
        out = []
        option_sets = [
            {},
            {"effort": "high"},
            {"max_tokens": 10, "temperature": 0.2, "cache_salt": 3},
            {"allowed_tools": "Read", "disallowed_tools": "Bash", "system_prompt_mode": "append"},
            {"extras": {"output_schema": {"a": 1}, "sandbox": "x", "unknown": 1}},
            {"extras": {"zeta": 1, "alpha": 2}, "effort": "low", "client_id": "c"},
            {"user_cache_prefix": "p", "max_retries": 1, "timeout_s": 3.0},
        ]
        for name, rec in records(S).items():
            out.append((f"{name}/fixed", outcome(r.fixed_control_ids, rec)))
            ids = [c.id for c in rec.execution_controls]
            for tag, applied in (("all", ids), ("none", []), ("bad", ids + ["bogus"]), ("gen", (i for i in ids))):
                out.append((f"{name}/check/{tag}", outcome(r.check_applied_controls, rec, applied)))
            for oi, kw in enumerate(option_sets):
                opts = S.types.BackendOptions(**kw)
                out.append((f"{name}/o{oi}/set", outcome(r.caller_set_params, opts)))
                out.append((f"{name}/o{oi}/extras", outcome(r.derive_extras_report, rec, opts)))
                out.append((f"{name}/o{oi}/dropped", outcome(r.derive_dropped_params, rec, opts)))
                out.append((f"{name}/o{oi}/forwarded", outcome(r.derive_forwarded_params, rec, opts)))
        stamp = r.utc_now_iso()
        out.append(("utc-shape", ("ok", [len(stamp), stamp[-1], stamp[4], stamp[10]])))
        return out

    results = check(scenario)
    assert kinds(results) == {"ok", "raise"}


# --------------------------------------------------------------------------
# 6. output contracts
# --------------------------------------------------------------------------

_OBJ_SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "integer"}, "b": {"type": "string"}},
    "required": ["a", "b"],
    "additionalProperties": False,
}
_LOOSE_SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}}
_BAD_SCHEMAS = [
    {"type": "wat"},
    {"type": ["object", "null"]},
    {"anyOf": [{"type": "string"}, {"type": "null"}]},
    {"pattern": "x"},
    {"type": "object", "properties": {"a": {1, 2}}},
    {"type": "object", "properties": {"a": float("nan")}},
    ["not", "a", "mapping"],
    "string",
]
_CONTRACT_ARGS = (
    [("c1", "validated-result", _OBJ_SCHEMA, None)]
    + [("c2", "native-required", _OBJ_SCHEMA, "v2")]
    + [("c3", "validated-result", _LOOSE_SCHEMA, None)]
    + [("c4", "text-only", None, None)]
    + [("c5", "text-only", _OBJ_SCHEMA, None)]
    + [("c6", "validated-result", None, None)]
    + [("", "validated-result", _OBJ_SCHEMA, None)]
    + [("c7", "bogus", _OBJ_SCHEMA, None)]
    + [("c8", "validated-result", _OBJ_SCHEMA, "")]
    + [("c9", "validated-result", _OBJ_SCHEMA, 5)]
    + [(f"bad{i}", "validated-result", s, None) for i, s in enumerate(_BAD_SCHEMAS)]
)
_ANSWERS = [
    '{"a": 1, "b": "x"}',
    '{"a": 1, "b": "x"',
    '```json\n{"a": 1, "b": "x"}\n```',
    'Sure! {"a": 1, "b": "x"} done',
    '{"a": "no", "b": "x"}',
    '{"a": 1}',
    '{"a": 1, "a": 2, "b": "x"}',
    '{"a": NaN, "b": "x"}',
    '{"a": 1 "b": "x"}',
    '[1, 2',
    "",
    "plain prose",
    '{"a": 1, "b": "x"} {"a": 2}',
    5,
    None,
    b"{}",
]


def _contracts(S: _Side):
    out = []
    for args in _CONTRACT_ARGS:
        cid, policy, schema, version = args
        schema = copy.deepcopy(schema) if not isinstance(schema, set) else schema
        out.append((f"{cid}/{policy}/{version}", outcome(S.contract_types.OutputContract, cid, policy, schema, version)))
    return out


def _build_contract(S: _Side, args):
    cid, policy, schema, version = args
    return S.contract_types.OutputContract(cid, policy, copy.deepcopy(schema), version)


def test_contract_types_parity():
    def scenario(S):
        ct = S.contract_types
        out = list(_contracts(S))
        good = [a for a in _CONTRACT_ARGS if a[0] in ("c1", "c2", "c3", "c4")]
        for args in good:
            c = _build_contract(S, args)
            tag = args[0]
            out.append((f"{tag}/dc", outcome(lambda c=c: c)))
            out.append((f"{tag}/json", ("ok", json.dumps(c.to_json()))))
            out.append((f"{tag}/identity", outcome(lambda c=c: c.identity)))
            out.append((f"{tag}/strict", outcome(lambda c=c: (c.strict_violations, c.strict_compatible))))
            out.append((f"{tag}/schema-json", outcome(lambda c=c: c.schema_json)))
            out.append((f"{tag}/canon", outcome(ct.canonical_schema_json, c)))
            out.append((f"{tag}/roundtrip", outcome(lambda c=c: ct.OutputContract.from_json(c.to_json()))))
            out.append((f"{tag}/copy", outcome(copy.copy, c)))
            out.append((f"{tag}/deepcopy", outcome(copy.deepcopy, c)))
            out.append((f"{tag}/pickle", outcome(lambda c=c: pickle.loads(pickle.dumps(c)))))
            out.append((f"{tag}/eq", outcome(lambda c=c: c == _build_contract(S, args))))
            out.append((f"{tag}/hash", outcome(lambda c=c: hash(c) == hash(_build_contract(S, args)))))
            out.append((f"{tag}/frozen", outcome(setattr, c, "id", "z")))
            out.append((f"{tag}/immutable-schema", outcome(lambda c=c: c.schema.__setitem__("x", 1) if c.schema is not None else None)))
        for payload in (None, 5, [], {}, {"id": "a"}, {"id": "a", "policy": "text-only"},
                        {"id": "a", "policy": "text-only", "extra": 1},
                        {"id": "a", "policy": "validated-result", "schema": _OBJ_SCHEMA},
                        {"id": "a", "policy": "validated-result", "schema": _OBJ_SCHEMA, "schema_version": "v", "schema_digest": "bad"}):
            out.append((f"from_json/{payload!r}", outcome(ct.OutputContract.from_json, payload)))
        for schema in [_OBJ_SCHEMA, _LOOSE_SCHEMA, {"type": "array", "items": {"type": "object"}}, {"type": "object", "properties": {"x": {"type": "object"}}, "required": ["x"], "additionalProperties": False}, 5]:
            out.append((f"strict/{schema!r}", outcome(ct.strict_schema_violations, schema)))
        report = ct.ContractReport(
            contract_id="c",
            schema_version="v",
            schema_digest="d",
            policy="validated-result",
            delivery="prompt",
            disposition="valid",
        )
        out.append(("report", outcome(lambda: report)))
        out.append(("report-json", outcome(report.to_json)))
        out.append(("report-bad", outcome(ct.ContractReport, "c", "v", "d", "bogus", "prompt", "valid")))
        out.append(("report-bad-disp", outcome(ct.ContractReport, "c", "v", "d", "text-only", "prompt", "weird")))
        out.append(("consts", ("ok", [list(ct.POLICIES), list(ct.DELIVERIES), list(ct.DISPOSITIONS), ct.SCHEMA_INSTRUCTION_PREFIX])))
        return out

    results = check(scenario)
    assert kinds(results) == {"ok", "raise"}


def test_contract_evaluation_parity():
    def scenario(S):
        c = S.contract
        out = []
        for args in (a for a in _CONTRACT_ARGS if a[0] in ("c1", "c3", "c4")):
            try:
                contract = _build_contract(S, args)
            except ValueError:
                continue
            for i, text in enumerate(_ANSWERS):
                out.append((f"{args[0]}/{i}/eval", outcome(c.evaluate_output, contract, text)))
                out.append((f"{args[0]}/{i}/repair", outcome(c.repair_and_evaluate_output, contract, text)))
            out.append((f"{args[0]}/render", outcome(c.render_schema_instruction, contract)))
            out.append((f"{args[0]}/reqs", outcome(c.contract_requirements, contract)))
        out.append(("reqs-none", outcome(c.contract_requirements, None)))
        out.append(("reqs-bad", outcome(c.contract_requirements, "nope")))
        for i, (a, b) in enumerate([
            (None, None), ({}, {}),
            ({"params": ["a"]}, {"params": ["b"]}),
            ({"params": ["a"]}, {"structured_output": {"policies": ["x"]}}),
            ({"structured": {"policies": ["x"]}}, {"structured_output": {"policies": ["y"]}}),
            ({"params": ["a"]}, ["b"]),
            ("p", "q"), (5, {}),
            ({"guarantees": ["a"]}, {"denies": ["b"]}),
        ]):
            out.append((f"merge/{i}", outcome(c.merge_requirements, a, b)))
        out.append(("consts", ("ok", [c.LEGACY_SCHEMA_EXTRAS, c.VIOLATION_ERROR_CODE])))
        return out

    results = check(scenario)
    assert kinds(results) == {"ok", "raise"}
    text = json.dumps([r for _, r in results], default=str)
    for word in ("schema-mismatch", "unparseable", "valid", "text-only"):
        assert word in text


def test_contract_delivery_plan_parity():
    options_extras = [{}, {"output_schema": {"x": 1}}, {"response_format": 1}, {"other": 1}]

    def scenario(S):
        c = S.contract
        out = []
        contracts = []
        for args in _CONTRACT_ARGS:
            if args[0] in ("c1", "c2", "c3", "c4"):
                contracts.append((args[0], _build_contract(S, args)))
        for name, rec in record_variants(S):
            for cname, contract in contracts:
                for ei, extras in enumerate(options_extras):
                    opts = S.types.BackendOptions(output_contract=contract, extras=extras)
                    tag = f"{name}/{cname}/e{ei}"
                    plan = outcome(c.prepare_contract, rec, opts)
                    out.append((f"{tag}/plan", plan))
                    if plan[0] == "ok" and ei == 0:
                        built = c.prepare_contract(rec, opts)
                        for ai, answer in enumerate(_ANSWERS[:6]):
                            resp = S.types.LLMResponse(text=answer, model="m", input_tokens=3)
                            out.append((f"{tag}/final/{ai}", outcome(c.finalize_contract, built, resp)))
        out.append(("no-contract", outcome(c.prepare_contract, records(S)["openrouter"], S.types.BackendOptions())))
        out.append(("no-options", outcome(c.prepare_contract, records(S)["openrouter"], None)))
        out.append(("bad-type", outcome(c.prepare_contract, records(S)["openrouter"], S.types.BackendOptions(output_contract="nope"))))
        out.append(("no-plan", outcome(c.finalize_contract, None, S.types.LLMResponse(text="t", model="m"))))
        return out

    results = check(scenario)
    assert len(results) > 400
    assert kinds(results) == {"ok", "raise"}
    text = json.dumps([r for _, r in results], default=str)
    for word in ("OutputContractUnsatisfiable", "OutputContractViolation", "native", "prompt"):
        assert word in text


# --------------------------------------------------------------------------
# 7. skill context (the REQUIRED / refusal probe, against fake libraries)
# --------------------------------------------------------------------------

_SCHEMA = LEGACY.skill_context._REQUIRED_REPORT_SCHEMA


def _fake_skill_material(variant: str) -> _pytypes.ModuleType:
    mod = _pytypes.ModuleType("bootstrap_lib.skill_material")

    class SkillMaterialError(Exception):
        pass

    class PyYamlUnavailableError(Exception):
        pass

    class SkillSelection:
        def __init__(self, data):
            self.data = data

        @classmethod
        def from_json(cls, mapping):
            if "refuse" in mapping:
                raise SkillMaterialError("refused selection: " + str(mapping["refuse"]))
            if "yaml" in mapping:
                raise PyYamlUnavailableError("no yaml")
            if "type-error" in mapping:
                raise TypeError("selection exploded")
            return cls(dict(mapping))

    class Report:
        def __init__(self, text):
            self.schema = "other/v9" if variant == "bad-schema" else _SCHEMA
            self.format_version = "1"
            self.digest = "d" * 64
            self.estimated_tokens = 12
            self.token_budget = 100
            self.token_estimate = "chars/4"
            self.skills = [1, 2]
            if variant == "no-budget":
                del self.token_budget

        def to_json(self):
            if variant == "tuple-json":
                return {"skills": (1, 2)}
            if variant == "list-json":
                return [1]
            if variant == "json-type-error":
                raise TypeError("cannot serialize")
            if variant == "float-nan":
                return {"x": float("nan")}
            return {"skills": [{"path": "p"}], "n": 1}

    class Materialized:
        def __init__(self, text):
            self.text = 5 if variant == "text-not-str" else text
            self.report = Report(text)

    def materialize(selection, *, base_dir=None):
        data = getattr(selection, "data", {})
        if "explode" in data:
            raise SkillMaterialError("skill exploded: " + str(base_dir))
        return Materialized("BLOCK " + str(base_dir))

    if variant == "no-materialize":
        materialize = None  # noqa: F811
    if variant == "sig-mismatch":
        def materialize(selection):  # noqa: F811
            return Materialized("x")
    mod.SkillMaterialError = SkillMaterialError
    mod.PyYamlUnavailableError = PyYamlUnavailableError
    mod.SkillSelection = SkillSelection
    mod.SkillMaterialReport = Report
    if materialize is not None:
        mod.materialize = materialize
    mod.SUPPORTED_REPORT_SCHEMAS = (
        [_SCHEMA] if variant == "supported-list"
        else frozenset({"other/v9"}) if variant == "supported-missing"
        else frozenset({_SCHEMA})
    )
    if variant == "no-error-class":
        del mod.SkillMaterialError
    return mod


def _install_fake_lib(monkeypatch, variant: str):
    if variant == "absent":
        monkeypatch.setitem(sys.modules, "bootstrap_lib", None)
        monkeypatch.delitem(sys.modules, "bootstrap_lib.skill_material", raising=False)
        return None
    pkg = _pytypes.ModuleType("bootstrap_lib")
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "bootstrap_lib", pkg)
    if variant == "too-old":
        monkeypatch.setitem(sys.modules, "bootstrap_lib.skill_material", None)
        return None
    mod = _fake_skill_material(variant)
    pkg.skill_material = mod
    monkeypatch.setitem(sys.modules, "bootstrap_lib.skill_material", mod)
    return mod


_VARIANTS = [
    "absent",
    "too-old",
    "good",
    "bad-schema",
    "tuple-json",
    "list-json",
    "json-type-error",
    "float-nan",
    "text-not-str",
    "no-budget",
    "no-materialize",
    "sig-mismatch",
    "supported-list",
    "supported-missing",
    "no-error-class",
]


@pytest.fixture
def legacy_skill_probe_branch(monkeypatch) -> None:
    # These two tests compare the retained legacy bootstrap failure contract.
    # Package identity/availability have standalone library runtime coverage;
    # the real-library probe below also exercises actual package-mode fleet.
    from llm_kit import _runtime
    monkeypatch.setattr(_runtime, "_MODE", "legacy")


@pytest.mark.usefixtures("legacy_skill_probe_branch")
@pytest.mark.parametrize("variant", _VARIANTS)
def test_skill_material_probe_parity(monkeypatch, variant, tmp_path):
    mod = _install_fake_lib(monkeypatch, variant)
    selections = [
        {"skills": []},
        {"refuse": "bad skill"},
        {"yaml": 1},
        {"type-error": 1},
        {"explode": 1},
        "not a selection",
        5,
        None,
    ]
    if mod is not None and hasattr(mod, "SkillSelection"):
        selections.append(mod.SkillSelection({"skills": []}))
        selections.append(mod.SkillSelection({"explode": 1}))

    def scenario(S):
        sc = S.skill_context
        out = [("probe", outcome(sc._skill_material))]
        for i, sel in enumerate(selections):
            out.append((f"sel{i}", outcome(sc.materialize_skill_context, sel)))
            out.append((f"sel{i}/base", outcome(sc.materialize_skill_context, sel, base_dir=tmp_path)))
        return out

    results = check(scenario)
    if variant == "good":
        assert "ok" in kinds(results) and "raise" in kinds(results)
        assert any(r[0] == "ok" and "dc" in json.dumps(r[1], default=str) for _, r in results)
    else:
        assert any(r[0] == "raise" for _, r in results)
    states = json.dumps([r for _, r in results], default=str)
    if variant == "absent":
        assert "'state': 'absent'" in states or '"state": "absent"' in states
    if variant in ("too-old", "bad-schema", "tuple-json", "no-materialize", "sig-mismatch"):
        assert "too-old" in states


def test_skill_material_real_library_parity(monkeypatch):
    from llm_kit import _runtime
    assert _runtime.mode() == "package"
    # Re-probe real availability instead of inheriting an earlier poisoned or
    # successful bootstrap import from another test in this worker.
    monkeypatch.setattr(_runtime, "_FLEET", None)
    """Whatever bootstrap_lib this interpreter has, both sides answer alike."""
    def scenario(S):
        sc = S.skill_context
        return [
            ("probe", outcome(sc._skill_material)),
            ("empty", outcome(sc.materialize_skill_context, {})),
            ("unknown-key", outcome(sc.materialize_skill_context, {"bogus": 1})),
            ("bad-type", outcome(sc.materialize_skill_context, 5)),
        ]

    check(scenario)


def test_skill_context_constants_parity():
    assert FINAL.skill_context.SKILL_MATERIAL_BOOTSTRAP == LEGACY.skill_context.SKILL_MATERIAL_BOOTSTRAP
    assert FINAL.skill_context._REQUIRED_REPORT_SCHEMA == _SCHEMA
    assert FINAL.skill_context._ABSENT_MESSAGE == LEGACY.skill_context._ABSENT_MESSAGE
    assert FINAL.skill_context._TOO_OLD_MESSAGE == LEGACY.skill_context._TOO_OLD_MESSAGE
    assert FINAL.skill_context._NO_PYYAML_MESSAGE == to_final(LEGACY.skill_context._NO_PYYAML_MESSAGE)


def test_skill_context_plan_parity():
    import hashlib

    def scenario(S):
        sct, sc = S.skill_context_types, S.skill_context
        text = "BLOCK"
        digest = hashlib.sha256(text.encode()).hexdigest()

        def report(d=digest, **kw):
            return sct.SkillContextReport(
                resolver=sct.RESOLVER, report_schema=_SCHEMA, format_version="1",
                digest=d, estimated_tokens=2, token_budget=9, token_estimate="chars/4",
                skills=1, provenance={"skills": [{"path": "p"}]}, **kw,
            )

        good = sct.SkillContext(text, report())
        stale = sct.SkillContext(text, report("0" * 64))
        out = [
            ("good", outcome(lambda: good)),
            ("report-json", outcome(report().to_json)),
            ("report-delivered", outcome(lambda: report(adapter="a", delivery="d", emits="e").to_json())),
            ("bad-text", outcome(sct.SkillContext, 5, report())),
            ("bad-report", outcome(sct.SkillContext, "t", {"a": 1})),
            ("support", outcome(lambda: sct.SkillContextSupportError("m", state="s"))),
            ("support-nostate", outcome(sct.SkillContextSupportError, "m")),
            ("unsat", outcome(lambda: sct.SkillContextUnsatisfiable("u"))),
            ("hierarchy", ("ok", [c.__name__ for c in sct.SkillContextSupportError.__mro__])),
            ("consts", ("ok", [sct.RESOLVER, sct.DELIVERY_SYSTEM_MESSAGE])),
            ("req-none", outcome(sc.skill_context_requirements, None)),
            ("req-good", outcome(sc.skill_context_requirements, good)),
            ("req-bad", outcome(sc.skill_context_requirements, "nope")),
            ("compose-none", outcome(sc.compose_system, None, "sys")),
        ]
        for name, rec in records(S).items():
            for tag, ctx in (("good", good), ("stale", stale), ("str", "nope")):
                opts = S.types.BackendOptions.__new__(S.types.BackendOptions)
                object.__setattr__(opts, "skill_context", ctx)
                plan = outcome(sc.prepare_skill_context, rec, opts)
                out.append((f"{name}/{tag}/plan", plan))
                if plan[0] == "ok" and plan[1] is not None:
                    built = sc.prepare_skill_context(rec, opts)
                    out.append((f"{name}/{tag}/delivered", outcome(built.delivered_report)))
                    out.append((f"{name}/{tag}/system", outcome(sc.compose_system, built, "sys")))
                    out.append((f"{name}/{tag}/system-empty", outcome(sc.compose_system, built, "")))
            out.append((f"{name}/none", outcome(sc.prepare_skill_context, rec, S.types.BackendOptions())))
            out.append((f"{name}/no-options", outcome(sc.prepare_skill_context, rec, None)))
        return out

    results = check(scenario)
    assert kinds(results) == {"ok", "raise"}
    text = json.dumps([r for _, r in results], default=str)
    assert "SkillContextUnsatisfiable" in text and "disagree" in text


@pytest.mark.parametrize("variant", ["good", "bad-schema", "tuple-json", "list-json", "json-type-error", "float-nan", "text-not-str", "no-budget"])
def test_skill_context_from_parity(variant):
    mod = _fake_skill_material(variant)

    def scenario(S):
        return [
            ("from", outcome(S.skill_context.skill_context_from, mod.materialize(mod.SkillSelection({}), base_dir=None))),
            ("from-bad", outcome(S.skill_context.skill_context_from, object())),
        ]

    results = check(scenario)
    assert "raise" in kinds(results)


# --------------------------------------------------------------------------
# 8. request protocol
# --------------------------------------------------------------------------

_PAYLOADS = [
    None,
    5,
    [],
    {},
    {"protocol": 2},
    {"protocol": True},
    {"protocol": 1},
    {"protocol": 1, "prompt": "hi"},
    {"protocol": 1, "prompt": "hi", "system": "s", "endpoint": "e", "model": "m", "cheap": True},
    {"protocol": 1, "prompt": 5},
    {"protocol": 1, "cheap": "yes"},
    {"protocol": 1, "bogus": 1},
    {"protocol": 1, "options": 5},
    {"protocol": 1, "options": {}},
    {"protocol": 1, "options": {"max_tokens": 12, "temperature": 0.5, "effort": "high"}},
    {"protocol": 1, "options": {"max_tokens": "12"}},
    {"protocol": 1, "options": {"max_tokens": True}},
    {"protocol": 1, "options": {"temperature": "hot"}},
    {"protocol": 1, "options": {"temperature": None}},
    {"protocol": 1, "options": {"timeout_s": 3}},
    {"protocol": 1, "options": {"cwd": "/tmp/x"}},
    {"protocol": 1, "options": {"cwd": 5}},
    {"protocol": 1, "options": {"log_prefix": "[x]"}},
    {"protocol": 1, "options": {"nope": 1, "log_prefix": "[x]"}},
    {"protocol": 1, "options": {"extras": {"a": 1}}},
    {"protocol": 1, "options": {"extras": [1]}},
    {"protocol": 1, "options": {"cache_salt": 3, "max_retries": 0, "user_cache_prefix": "p"}},
    {"protocol": 1, "options": {"allowed_tools": "Read", "disallowed_tools": "Bash", "system_prompt_mode": "append"}},
    {"protocol": 1, "options": {"output_contract": {"id": "c", "policy": "text-only"}}},
    {"protocol": 1, "options": {"output_contract": {"id": "c", "policy": "validated-result", "schema": _OBJ_SCHEMA}}},
    {"protocol": 1, "options": {"output_contract": {"id": "c", "policy": "bogus"}}},
    {"protocol": 1, "options": {"output_contract": 5}},
    {"protocol": 1, "options": {"skill_context": {"skills": []}}},
    {"protocol": 1, "options": {"skill_context": {"refuse": "x"}}},
    {"protocol": 1, "options": {"skill_context": 5}},
]


@pytest.mark.usefixtures("legacy_skill_probe_branch")
@pytest.mark.parametrize("variant", ["absent", "good"])
def test_request_protocol_parity(monkeypatch, variant):
    _install_fake_lib(monkeypatch, variant)

    def scenario(S):
        rp = S.request_protocol
        out = [
            ("schema", outcome(rp.describe_request_schema)),
            ("envelope", outcome(rp.protocol_error_envelope, "bad")),
            ("version", ("ok", rp.PROTOCOL_VERSION)),
            ("error-type", ("ok", [c.__name__ for c in rp.ProtocolError.__mro__])),
            ("fields", outcome(rp._option_fields)),
            ("hints", outcome(lambda: {k: _strip_pkg(str(v)) for k, v in rp._option_type_hints().items()})),
        ]
        for i, payload in enumerate(_PAYLOADS):
            out.append((f"req{i}", outcome(rp.parse_request, payload)))
            if isinstance(payload, dict) and "options" in payload:
                out.append((f"opt{i}", outcome(rp.parse_options, payload["options"])))
        for i, raw in enumerate((None, 5, [], "x")):
            out.append((f"opt-raw{i}", outcome(rp.parse_options, raw)))
        return out

    results = check(scenario)
    assert kinds(results) == {"ok", "raise"}
    text = json.dumps([r for _, r in results], default=str)
    for word in ("ProtocolError", "CompletionRequest", "BackendOptions"):
        assert word in text
