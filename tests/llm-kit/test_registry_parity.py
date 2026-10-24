"""Differential parity: llm_kit.registry vs the legacy llm_scripting_kit modules.

The legacy side is imported from plugins/llm-scripting-kit/lib with a sys.path
insert scoped to the import. Every case drives the legacy module and the final
package with the same inputs and asserts equal results, equal exception types
(by MRO name) and messages, and equal stderr.

Covered: constants, env_file, api_key, effort (plus the static harness effort
tables that moved out of harness_adapters), model_endpoints, models, and the
slices of usage_budget (conserve) and declaration (declaration_syntax) that the
registry carries. The one deliberate difference is the CLI command, which names
the llm-kit venv and executable (design: "Persisted-state compatibility").
"""

from __future__ import annotations

import copy
import dataclasses
import importlib
import inspect
import sys
import types
from pathlib import Path

import pytest

from final_wording import openrouter_style_to_legacy, to_final

_REPO = Path(__file__).resolve().parents[2]
_LEGACY_LIB = _REPO / "plugins" / "llm-scripting-kit" / "lib"

LEGACY_NAME = "llm-scripting-kit"
FINAL_NAME = "llm-kit"


# --------------------------------------------------------------------------
# Loading both sides
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def L():
    sys.path.insert(0, str(_LEGACY_LIB))
    try:
        names = (
            "constants", "env_file", "api_key", "effort", "model_endpoints",
            "models", "usage_budget", "declaration", "harness_adapters",
        )
        return types.SimpleNamespace(
            **{n: importlib.import_module(f"llm_scripting_kit.{n}") for n in names}
        )
    finally:
        sys.path.remove(str(_LEGACY_LIB))


@pytest.fixture(scope="module")
def F():
    names = (
        "constants", "env_file", "api_key", "effort", "model_endpoints",
        "models", "conserve", "declaration_syntax",
    )
    return types.SimpleNamespace(
        **{n: importlib.import_module(f"llm_kit.registry.{n}") for n in names}
    )


# --------------------------------------------------------------------------
# Outcome normalisation
# --------------------------------------------------------------------------


#: Dataclass fields the final package added after the legacy plugin froze (declared
#: difference, like final_wording): cost-class metadata, consumed only by llm_kit.registry.cost.
FINAL_ONLY_FIELDS = {"EndpointEntry": frozenset({"billing_owner", "endpoint"})}


def _compared_fields(cls):
    skip = FINAL_ONLY_FIELDS.get(cls.__name__, frozenset())
    return [f for f in dataclasses.fields(cls) if f.name not in skip]


def shipped_default_endpoint_to_final(value):
    """``value`` (a config dict) without the legacy shipped ``default_endpoint: openrouter``.

    Declared difference: the final package ships no default model declaration (consent-design
    item 8), so its baseline config has no ``default_endpoint`` key where the legacy one named
    ``openrouter``. Recognised by that exact value next to an ``endpoints`` map, and idempotent,
    so a harness may pass both sides through it. A config's ``default_endpoint`` is also moved
    last: the legacy baseline held the key first, so a layer's value kept that position, and key
    order carries no meaning.
    """
    if not (isinstance(value, dict) and "endpoints" in value and "default_endpoint" in value):
        return value
    kept = {k: v for k, v in value.items() if k != "default_endpoint"}
    if value["default_endpoint"] != "openrouter":
        kept["default_endpoint"] = value["default_endpoint"]
    return kept


def norm(value):
    """Reduce a value to comparable plain data, keeping class names."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            "__type__": type(value).__name__,
            **{f.name: norm(getattr(value, f.name)) for f in _compared_fields(type(value))},
        }
    if isinstance(value, dict):
        value = openrouter_style_to_legacy(value)  # declared difference (final_wording)
        value = shipped_default_endpoint_to_final(value)  # declared difference (no shipped default)
        out = {"__dict__": [(norm(k), norm(v)) for k, v in value.items()]}
        if type(value) is not dict:
            out["__type__"] = type(value).__name__
            out["notes"] = norm(getattr(value, "notes", None))
        return out
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, [norm(v) for v in value])
    if isinstance(value, (set, frozenset)):
        return (type(value).__name__, sorted(norm(v) for v in value))
    if isinstance(value, Path):
        return ("Path", str(value))
    if isinstance(value, str):
        return to_final(value)  # named deliberate wording differences (final_wording.RENAMES)
    return value


def _exc(exc):
    cause = exc.__cause__
    return (
        "raise",
        [c.__name__ for c in type(exc).__mro__],
        to_final(str(exc)),
        None if cause is None else ([c.__name__ for c in type(cause).__mro__], to_final(str(cause))),
    )


def outcome(fn, *args, **kwargs):
    try:
        return ("ok", norm(fn(*args, **kwargs)))
    except Exception as exc:  # noqa: BLE001 - parity compares any exception
        return _exc(exc)


def pair(capsys, legacy_fn, final_fn, *args, **kwargs):
    """Run both sides on deep-copied inputs; assert equal outcome and stderr."""
    capsys.readouterr()
    left = outcome(legacy_fn, *copy.deepcopy(args), **copy.deepcopy(kwargs))
    left_err = capsys.readouterr().err
    right = outcome(final_fn, *copy.deepcopy(args), **copy.deepcopy(kwargs))
    right_err = capsys.readouterr().err
    assert right == left
    assert right_err == left_err
    return left, left_err


def _sig(fn) -> str:
    return (
        str(inspect.signature(fn))
        .replace("llm_scripting_kit.", "X.")
        .replace("llm_kit.registry.", "X.")
    )


def assert_surface(legacy_mod, final_mod, names):
    """Same public surface: dataclass fields, signatures, and constant values."""
    for name in names:
        assert hasattr(legacy_mod, name), f"legacy lacks {name}"
        assert hasattr(final_mod, name), f"final lacks {name}"
        old, new = getattr(legacy_mod, name), getattr(final_mod, name)
        if isinstance(old, type):
            assert isinstance(new, type), name
            assert old.__name__ == new.__name__
            assert [b.__name__ for b in old.__mro__] == [b.__name__ for b in new.__mro__], name
            if dataclasses.is_dataclass(old):
                assert dataclasses.is_dataclass(new), name
                assert [
                    (f.name, str(f.type), repr(f.default), f.default_factory is dataclasses.MISSING)
                    for f in _compared_fields(old)
                ] == [
                    (f.name, str(f.type), repr(f.default), f.default_factory is dataclasses.MISSING)
                    for f in _compared_fields(new)
                ], name
                assert old.__dataclass_params__.frozen == new.__dataclass_params__.frozen
        elif callable(old):
            assert callable(new), name
            assert _sig(old) == _sig(new), name
        else:
            assert norm(old) == norm(new), name


# --------------------------------------------------------------------------
# Environment isolation
# --------------------------------------------------------------------------

_SCRUBBED_ENV = (
    "MODEL_ENDPOINTS_REGISTRY", "OPENROUTER_API_KEY", "CLAUDE_BOOTSTRAP_DATA_ROOT",
    "LOCAL_KEY", "KF_KEY", "OTHER_KEY",
)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    for var in _SCRUBBED_ENV:
        monkeypatch.delenv(var, raising=False)
    return h


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _layers(home: Path, project: Path):
    return {
        "fleet": home / ".claude" / "config" / f"{LEGACY_NAME}.yaml",
        "user": home / ".claude" / "plugins" / "data" / "plugins-kit" / LEGACY_NAME / "config.yaml",
        "project": project / ".local-data" / "plugins-kit" / LEGACY_NAME / "config.yaml",
        "legacy_project": project / ".local-data" / LEGACY_NAME / "config.yaml",
    }


# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------


def test_constants_persisted_paths_are_legacy_byte_for_byte(L, F, tmp_path):
    for name in ("BASE_URL", "API_KEY_ENV", "USER_ENV_FILE"):
        assert norm(getattr(F.constants, name)) == norm(getattr(L.constants, name))
    assert F.constants.USER_ENV_FILE == (
        Path.home() / ".claude" / "plugins" / "data" / "plugins-kit" / LEGACY_NAME / ".env"
    )
    for root in (tmp_path, str(tmp_path), tmp_path / "a b", Path("rel/dir"), "."):
        assert norm(F.constants.project_env_file(root)) == norm(L.constants.project_env_file(root))
        assert norm(F.constants.legacy_project_env_file(root)) == norm(
            L.constants.legacy_project_env_file(root)
        )
        assert norm(F.constants.project_env_files(root)) == norm(L.constants.project_env_files(root))
    assert F.constants.project_env_file(tmp_path).parts[-4:] == (
        ".local-data", "plugins-kit", LEGACY_NAME, ".env"
    )
    assert F.constants.legacy_project_env_file(tmp_path).parts[-3:] == (
        ".local-data", LEGACY_NAME, ".env"
    )


def test_constants_cli_command_names_the_llm_kit_executable(L, F, monkeypatch):
    for name in ("CLI_COMMAND", "REDIRECTED_CLI_COMMAND"):
        legacy_value = getattr(L.constants, name)
        final_value = getattr(F.constants, name)
        assert LEGACY_NAME in legacy_value
        assert final_value == legacy_value.replace(LEGACY_NAME, FINAL_NAME)
    leaf = "Scripts/llm-kit.exe" if sys.platform == "win32" else "bin/llm-kit"
    assert F.constants.CLI_COMMAND == "~/.claude/plugins/data/plugins-kit/llm-kit/.venv/" + leaf
    assert F.constants.REDIRECTED_CLI_COMMAND == (
        '"${CLAUDE_BOOTSTRAP_DATA_ROOT:-$HOME/.claude/plugins/data}"'
        "/plugins-kit/llm-kit/.venv/" + leaf
    )
    monkeypatch.delenv("CLAUDE_BOOTSTRAP_DATA_ROOT", raising=False)
    assert F.constants.cli_command() == F.constants.CLI_COMMAND
    assert L.constants.cli_command() == L.constants.CLI_COMMAND
    monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", "/some/root")
    assert F.constants.cli_command() == F.constants.REDIRECTED_CLI_COMMAND
    assert L.constants.cli_command() == L.constants.REDIRECTED_CLI_COMMAND
    monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", "")
    assert F.constants.cli_command() == F.constants.CLI_COMMAND


# --------------------------------------------------------------------------
# env_file
# --------------------------------------------------------------------------

_ENV_VALUES = [
    {},
    {"A": "plain"},
    {"A": " lead", "B": "trail ", "C": ""},
    {"A": 'has "quote"', "B": "back\\slash", "C": 'both \\" mix', "D": "x=y"},
    {"K1": "v1", "K2": "'single'", "K3": '"double"', "K4": "#hash", "K5": "tab\tvalue"},
    {"UNICODE": "caf" + chr(0xE9), "EMPTY": "", "SPACE": " "},
]

_ENV_TEXTS = [
    "",
    "A=1\nB=2\n",
    "# comment\n\nA = 1 \nexport B=2\nexport\tC=3\nexportD=4\n",
    "A='single'\nB=\"dq \\\"x\\\" \\\\ y\"\nC=\"\"\nD=''\nE='\nF=\"\n",
    "A=1\r\nB=2\r\n",
    "A=b=c\nB==\n",
    "noequals\n",
    "A=1\n  \nbad line\n",
    "A=\"a\\nb\"\nB=\"trailing\\\\\"\n",
    chr(0xFEFF) + "A=1\n",
]


def test_env_file_write_bytes_and_read_round_trip(L, F, tmp_path):
    for i, values in enumerate(_ENV_VALUES):
        old, new = tmp_path / f"old{i}" / ".env", tmp_path / f"new{i}" / ".env"
        L.env_file.write_env_file(old, dict(values))
        F.env_file.write_env_file(new, dict(values))
        assert new.read_bytes() == old.read_bytes()
        assert not list(new.parent.glob("*.tmp"))
        assert outcome(F.env_file.read_env_file, new) == outcome(L.env_file.read_env_file, old)
        # (a value wrapped in single quotes does not round-trip; legacy behavior, carried as is)


def test_env_file_read_parity_on_texts(L, F, tmp_path):
    shapes = set()
    for i, text in enumerate(_ENV_TEXTS):
        path = tmp_path / f"t{i}.env"
        path.write_bytes(text.encode("utf-8"))
        left = outcome(L.env_file.read_env_file, path)
        right = outcome(F.env_file.read_env_file, path)
        shapes.add(left[0])
        assert right == left
    assert shapes == {"ok", "raise"}
    missing = tmp_path / "missing.env"
    assert outcome(F.env_file.read_env_file, missing) == outcome(L.env_file.read_env_file, missing) == (
        "ok", {"__dict__": []}
    )
    assert outcome(F.env_file.read_env_file, str(tmp_path / "t1.env")) == outcome(
        L.env_file.read_env_file, str(tmp_path / "t1.env")
    )


def test_env_file_quoting_helpers(L, F):
    for value in ("", "a", " a", 'a"b', "a\\b", 'a\\"b', "\\\\", '"', "\\"):
        assert F.env_file._needs_quoting(value) == L.env_file._needs_quoting(value)
        assert F.env_file._quote_value(value) == L.env_file._quote_value(value)
        inner = L.env_file._quote_value(value)[1:-1]
        assert F.env_file._unquote_double(inner) == L.env_file._unquote_double(inner)
    for inner in ("\\", "a\\", 'x\\"y', "\\n", "\\\\\\"):
        assert F.env_file._unquote_double(inner) == L.env_file._unquote_double(inner)


# --------------------------------------------------------------------------
# conserve (usage_budget slice)
# --------------------------------------------------------------------------

_CONSERVE_VALUES = [
    None, False, True, 0, 1, "true", [], [1],
    {}, {"pool": "seven_day"}, {"pool": "primary"}, {"pool": " primary "},
    {"pool": "model_scoped", "display_name": "Fable"},
    {"pool": "model_scoped", "display_name": " Fable "},
    {"pool": "model_scoped"}, {"pool": "model_scoped", "display_name": ""},
    {"pool": "model_scoped", "display_name": 5},
    {"pool": "seven_day", "display_name": "x"},
    {"pool": ""}, {"pool": "  "}, {"pool": 5}, {"pool": "five_hour", "extra": 1},
    {"display_name": "x"}, {"pool": "seven_day", "display_name": None},
]


def test_conserve_surface(L, F):
    for name in (
        "STATUS_AVAILABLE", "STATUS_UNDER_QUOTA", "STATUS_OUT_OF_QUOTA", "STATUS_NO_DATA",
        "POOL_SEVEN_DAY", "POOL_MODEL_SCOPED", "POOL_PRIMARY",
        "Budget", "ConserveConfigError", "ConserveSpec", "parse_conserve_usage",
    ):
        assert_surface(L.usage_budget, F.conserve, [name])


def test_parse_conserve_usage_parity(L, F):
    shapes = set()
    for value in _CONSERVE_VALUES:
        for source, entry_id in (("registry 'p'", "e1"), ("layered model config", "")):
            left = outcome(L.usage_budget.parse_conserve_usage, value, source=source, entry_id=entry_id)
            right = outcome(F.conserve.parse_conserve_usage, value, source=source, entry_id=entry_id)
            shapes.add(left[0])
            assert right == left
    assert shapes == {"ok", "raise"}


def test_conserve_spec_and_budget_behaviour(L, F):
    for pool, label in (("seven_day", None), ("model_scoped", "Fable"), ("primary", "")):
        old = L.usage_budget.ConserveSpec(pool, label)
        new = F.conserve.ConserveSpec(pool, label)
        assert norm(new.to_json()) == norm(old.to_json())
        assert norm(new) == norm(old)
        assert hash(new) == hash(F.conserve.ConserveSpec(pool, label))
    for status in ("available", "under-quota", "out-of-quota", "no-data", "other"):
        for extra in ({}, {"remaining": 0.5, "window_remaining": 0.25, "resets_at": 123}):
            old = L.usage_budget.Budget(status, "seven_day", "why", **extra)
            new = F.conserve.Budget(status, "seven_day", "why", **extra)
            assert new.to_json() == old.to_json()
            assert (new.usable, new.deprioritized) == (old.usable, old.deprioritized)
            assert norm(new) == norm(old)
    with pytest.raises(dataclasses.FrozenInstanceError):
        F.conserve.Budget("available", "p", "d").status = "x"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        F.conserve.ConserveSpec("p").pool = "x"  # type: ignore[misc]


# --------------------------------------------------------------------------
# effort
# --------------------------------------------------------------------------

_STYLES = ["top-level", "ninfer", "chat_template_kwargs", "unsupported", None, "bogus", ""]
_EFFORTS = ["low", "medium", "high", "xhigh", "max", "none", "", "weird"]


def test_effort_surface(L, F):
    names = list(L.effort.__all__)
    assert_surface(L.effort, F.effort, names)
    assert set(L.effort.__all__) <= set(F.effort.__all__)
    assert set(F.effort.__all__) - set(L.effort.__all__) == {
        "CODEX_HARNESS", "OPENCODE_HARNESS", "CLAUDE_HARNESS", "CODEX_EFFORT_ORDER",
        "CLAUDE_EFFORT_ORDER", "HarnessAdapterError", "harness_effort_order",
    }
    for name in ("CODEX_HARNESS", "OPENCODE_HARNESS", "CLAUDE_HARNESS",
                 "CODEX_EFFORT_ORDER", "CLAUDE_EFFORT_ORDER"):
        assert getattr(F.effort, name) == getattr(L.harness_adapters, name)
    assert_surface(L.harness_adapters, F.effort, ["harness_effort_order", "HarnessAdapterError"])


def test_effort_remap_delivery_and_menus(L, F):
    for effort in _EFFORTS:
        for style in _STYLES:
            assert F.effort.remap_effort(effort, style) == L.effort.remap_effort(effort, style)
    for style in _STYLES:
        assert F.effort.style_effort_menu(style) == L.effort.style_effort_menu(style)
        for source in ("endpoint", "frontdoor", "routing", "none"):
            old, new = L.effort.EffortDelivery(style, source), F.effort.EffortDelivery(style, source)
            assert (new.deliverable, new.emits, new.to_json()) == (
                old.deliverable, old.emits, old.to_json()
            )
            assert norm(new) == norm(old)
    assert F.effort.EffortDelivery("ninfer").to_json()["remap"] == {"high": "xhigh"}


_BODIES = [
    {}, {"x": 1}, {"reasoning_effort": "high"}, {"reasoning_effort": None},
    {"chat_template_kwargs": {"reasoning_effort": "low", "k": 1}},
    {"chat_template_kwargs": {"reasoning_effort": None}},
    {"reasoning_effort": "high", "chat_template_kwargs": {"reasoning_effort": "low"}},
    {"reasoning_effort": None, "chat_template_kwargs": {"reasoning_effort": "low"}},
    {"chat_template_kwargs": "not-a-dict"}, {"chat_template_kwargs": {"k": 1}},
    {"chat_template_kwargs": {}},
]


def test_effort_body_functions(L, F):
    for body in _BODIES:
        old_body, new_body = copy.deepcopy(body), copy.deepcopy(body)
        old = outcome(L.effort.extract_effort, old_body)
        new = outcome(F.effort.extract_effort, new_body)
        assert new == old and new_body == old_body
        for effort in ("low", "high", "xhigh"):
            for style in _STYLES:
                old_body, new_body = copy.deepcopy(body), copy.deepcopy(body)
                old = outcome(L.effort.place_effort, old_body, effort, style)
                new = outcome(F.effort.place_effort, new_body, effort, style)
                assert new == old and new_body == old_body
        # the caller's nested object is never mutated
        nested = body.get("chat_template_kwargs")
        probe = copy.deepcopy(body)
        F.effort.extract_effort(probe)
        if isinstance(nested, dict):
            assert body["chat_template_kwargs"] is nested
            assert probe["chat_template_kwargs"] is not nested


def test_effort_plan(L, F):
    extras = [None, *_BODIES]
    seen = set()
    for extra in extras:
        for effort in (None, "low", "high"):
            for style in _STYLES:
                before = copy.deepcopy(extra)
                old = outcome(L.effort.plan_effort, extra, effort, style)
                new = outcome(F.effort.plan_effort, extra, effort, style)
                assert new == old
                assert extra == before
                seen.add(old[1]["outcome"])
    assert seen == {
        "translated", "caller-extras", "suppressed", "undeliverable", "unset",
    }
    plan = F.effort.plan_effort({}, "low", "top-level")
    assert plan.effort_delivered is True
    assert F.effort.plan_effort({"reasoning_effort": "x"}, "low", "top-level").effort_delivered is False


def test_harness_effort_order_parity(L, F, capsys):
    for harness in ("claude", "codex", "opencode", "nonesuch", "", "CODEX"):
        left, _ = pair(
            capsys, L.harness_adapters.harness_effort_order, F.effort.harness_effort_order, harness
        )
    assert left[0] == "raise"
    assert left[2] == "unknown harness 'CODEX' (known harnesses: claude, codex, opencode)"
    assert left[1] == ["HarnessAdapterError", "ValueError", "Exception", "BaseException", "object"]
    assert F.effort.harness_effort_order("opencode") == ()


_MENU_REGISTRY = """\
default: local
models:
  local: {base_url: http://h/v1, model: m, effort_style: ninfer}
  top: {base_url: http://h/v1, model: m, effort_style: top-level}
  chat: {base_url: http://h/v1, model: m, effort_style: chat_template_kwargs}
  unsup: {base_url: http://h/v1, model: m, effort_style: unsupported}
  plain: {base_url: http://h/v1, model: m}
  front: {base_url: http://h/v1, model: m, frontdoor: true}
  routed: {base_url: http://h/v1, model: m, routing: {group: g, effort_style: ninfer}}
  cdx: {harness: codex, model: gpt}
  cl: {harness: claude, model: opus}
  oc: {harness: opencode, model: p/m}
  odd: {harness: nonesuch, model: zz}
"""


def test_effort_menu_and_lower_effort_through_the_registry(L, F, home, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(_write(tmp_path / "reg.yaml", _MENU_REGISTRY)))
    shapes = set()
    for endpoint in ("local", "top", "chat", "unsup", "plain", "front", "routed", "cdx", "cl",
                     "oc", "odd", "nope"):
        left, _ = pair(capsys, L.effort.effort_menu, F.effort.effort_menu, endpoint)
        shapes.add(left[0])
        for effort in _EFFORTS:
            pair(capsys, L.effort.lower_effort, F.effort.lower_effort, endpoint, effort)
    assert shapes == {"ok", "raise"}
    assert F.effort.effort_menu("local") == ("none", "low", "medium", "xhigh")
    assert F.effort.lower_effort("local", "high") == "medium"
    assert F.effort.lower_effort("cl", "max") == "xhigh"


# --------------------------------------------------------------------------
# model_endpoints
# --------------------------------------------------------------------------

_REG_FULL = """\
version: 1
default: local
models:
  local:
    base_url: http://localhost:8080/v1
    model: qwen-local
    name: Local Qwen
    context_window: "32768"
    reasoning_effort: high
    key_env: LOCAL_KEY
    key_file: ~/keys/local
    tier: 2
    family: qwen
    conserve_usage: true
    frontdoor: true
    effort_style: ninfer
    billing:
      mode: unmetered
    routing:
      group: g1
      order: 2
      max_parallel: 3
      effort_style: chat_template_kwargs
  routed:
    base_url: http://h/v1
    model: m2
    reasoning_effort: low
    routing: {group: g2, effort_style: top-level}
  chat:
    base_url: http://h/v1
    model: m4
    effort_style: chat_template_kwargs
  unsup:
    base_url: http://h/v1
    model: m5
    effort_style: unsupported
  cdx:
    harness: codex
    model: gpt-x
    effort: high
    tier: 3
    family: openai
    conserve_usage: {pool: primary}
  cl:
    harness: claude
    model: opus
    name: Opus
  mscoped:
    harness: claude
    model: fable
    conserve_usage: {pool: model_scoped, display_name: Fable}
  odd:
    harness: nonesuch
    model: zz
"""

_REG_NOTES = """\
default: ok
models:
  ok: {base_url: http://h/v1, model: m, frontdoor: maybe, billing: {mode: free, extra: 1}, effort_style: weird, routing: {group: " ", order: 0}}
  r1: {base_url: http://h/v1, model: m, routing: bad}
  r2: {base_url: http://h/v1, model: m, routing: {group: g, order: 0, max_parallel: 0, effort_style: bogus, extra: 1}}
  r3: {base_url: http://h/v1, model: m, routing: {group: g, order: true, max_parallel: true}}
  b1: {base_url: http://h/v1, model: m, billing: bad}
  b2: {base_url: http://h/v1, model: m, billing: {}}
  b3: {base_url: http://h/v1, model: m, billing: {mode: provider-reported}}
  b4: {base_url: http://h/v1, model: m, billing: {mode: 5}}
  nm: {base_url: http://h/v1}
  nb: {model: m}
  both: {base_url: http://h/v1, harness: codex, model: m}
  notmap: 5
  conf: {base_url: http://h/v1, model: m, effort_style: top-level, routing: {group: g, effort_style: ninfer}}
  hbad: {harness: codex}
  hname: {harness: codex, model: m, name: ""}
  ints: {base_url: http://h/v1, model: m, context_window: abc}
  intb: {base_url: http://h/v1, model: m, context_window: true}
  intl: {base_url: http://h/v1, model: m, context_window: [1]}
  tname: {base_url: http://h/v1, model: m, name: 5, key_env: 5}
  fd: {base_url: http://h/v1, model: m, frontdoor: 1}
"""

_BASE = "base_url: http://h/v1, model: m"

_REG_CASES = {
    "full": _REG_FULL,
    "notes": _REG_NOTES,
    "empty-file": "",
    "list": "- a\n- b\n",
    "no-models": "default: x\n",
    "models-list": "models: [1]\n",
    "bad-yaml": "models: [unclosed\n",
    "tier-high": f"models:\n  a: {{{_BASE}, tier: 9}}\n",
    "tier-bool": f"models:\n  a: {{{_BASE}, tier: true}}\n",
    "family-empty": f"models:\n  a: {{{_BASE}, family: ''}}\n",
    "harness-tier": "models:\n  h: {harness: codex, model: m, tier: 0}\n",
    "conserve-keys": f"models:\n  a: {{{_BASE}, conserve_usage: {{pool: x, foo: 1}}}}\n",
    "conserve-int": f"models:\n  a: {{{_BASE}, conserve_usage: 5}}\n",
    "conserve-scoped": f"models:\n  a: {{{_BASE}, conserve_usage: {{pool: model_scoped}}}}\n",
    "harness-conserve": "models:\n  h: {harness: codex, model: m, conserve_usage: {pool: ''}}\n",
    "default-missing": f"default: nope\nmodels:\n  a: {{{_BASE}}}\n",
    "default-skipped": f"default: nm\nmodels:\n  nm: {{base_url: http://h/v1}}\n  a: {{{_BASE}}}\n",
    "default-int": f"default: 5\nmodels:\n  5: {{{_BASE}}}\n",
    "default-none": f"models:\n  a: {{{_BASE}}}\n",
    "reasoning-no-style": f"models:\n  a: {{{_BASE}, reasoning_effort: high}}\n",
    "reasoning-unsupported": f"models:\n  a: {{{_BASE}, reasoning_effort: high, effort_style: unsupported}}\n",
    "reasoning-bad-style": f"models:\n  a: {{{_BASE}, reasoning_effort: high, effort_style: bogus}}\n",
    "empty-models": "models: {}\n",
}


def _reg_env(path: Path):
    return {"MODEL_ENDPOINTS_REGISTRY": str(path)}


def test_model_endpoints_surface(L, F):
    names = list(L.model_endpoints.__all__) + [
        "_require_str", "_optional_str", "_optional_int", "_conserve_spec", "_resolve_registry_path",
    ]
    assert_surface(L.model_endpoints, F.model_endpoints, names)
    assert set(L.model_endpoints.__all__) <= set(F.model_endpoints.__all__)
    assert F.model_endpoints.ConserveSpec is F.conserve.ConserveSpec
    assert F.model_endpoints.ConserveConfigError is F.conserve.ConserveConfigError
    assert F.model_endpoints.EndpointMetadataError.__mro__[1] is F.model_endpoints.EndpointRegistryError


def test_load_endpoint_registry_parity(L, F, home, tmp_path):
    shapes = {}
    for label, text in _REG_CASES.items():
        path = _write(tmp_path / f"{label}.yaml", text)
        left = outcome(L.model_endpoints.load_endpoint_registry, _reg_env(path))
        right = outcome(F.model_endpoints.load_endpoint_registry, _reg_env(path))
        assert right == left, label
        shapes[label] = left
    assert {v[0] for v in shapes.values()} == {"ok", "raise"}
    # the full registry loads every entry with its kind and conserve spec
    assert len(shapes["full"][1]["entries"]["__dict__"]) == 8
    assert shapes["notes"][0] == "ok" and shapes["notes"][1]["notes"][1]  # skip notes carried through
    for label in ("tier-high", "tier-bool", "harness-tier", "conserve-keys", "conserve-int",
                  "reasoning-no-style", "default-missing", "default-skipped", "bad-yaml", "list"):
        assert shapes[label][0] == "raise", label
    assert shapes["tier-high"][1][0] == "EndpointMetadataError"


def test_registry_path_resolution_parity(L, F, home, tmp_path, monkeypatch):
    # Declared difference: with no file anywhere llm-kit's convention is the NEUTRAL path
    # (``$XDG_CONFIG_HOME/llm-kit``, fallback ``~/.config/llm-kit``); the frozen legacy one
    # is ``~/.claude/config``. The legacy file stays readable (the legacy loads below pass).
    assert L.model_endpoints.default_registry_path() == home / ".claude" / "config" / "model-endpoints.yaml"
    assert F.model_endpoints.default_registry_path() == home / ".config" / "llm-kit" / "model-endpoints.yaml"
    assert F.model_endpoints.REGISTRY_ENV == L.model_endpoints.REGISTRY_ENV == "MODEL_ENDPOINTS_REGISTRY"
    assert F.model_endpoints.legacy_registry_path() == home / ".claude" / "config" / "model-endpoints.yaml"
    # convention path, file absent: an empty registry
    left = outcome(L.model_endpoints.load_endpoint_registry, {})
    right = outcome(F.model_endpoints.load_endpoint_registry, {})
    assert right == left and left[0] == "ok"
    assert left[1]["path"] is None and left[1]["entries"]["__dict__"] == []
    # explicit override that dangles: loud
    missing = {"MODEL_ENDPOINTS_REGISTRY": str(tmp_path / "gone.yaml")}
    left = outcome(L.model_endpoints.load_endpoint_registry, missing)
    assert outcome(F.model_endpoints.load_endpoint_registry, missing) == left
    assert left[0] == "raise" and "not a readable file" in left[2]
    # blank override falls back to the convention; ~ is expanded
    blank = {"MODEL_ENDPOINTS_REGISTRY": "   "}
    assert outcome(F.model_endpoints.load_endpoint_registry, blank) == outcome(
        L.model_endpoints.load_endpoint_registry, blank
    )
    _write(home / ".claude" / "config" / "model-endpoints.yaml", _REG_FULL)
    for env in ({}, blank, {"MODEL_ENDPOINTS_REGISTRY": "~/.claude/config/model-endpoints.yaml"}):
        left = outcome(L.model_endpoints.load_endpoint_registry, env)
        assert outcome(F.model_endpoints.load_endpoint_registry, env) == left
        assert left[0] == "ok" and len(left[1]["entries"]["__dict__"]) == 8
    # process environment is the default source
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(tmp_path / "gone.yaml"))
    assert outcome(F.model_endpoints.load_endpoint_registry) == outcome(
        L.model_endpoints.load_endpoint_registry
    )
    for env in ({}, blank):
        assert norm(F.model_endpoints._resolve_registry_path(env)) == norm(
            L.model_endpoints._resolve_registry_path(env)
        )


def test_resolve_registry_entry_parity(L, F, home, tmp_path, monkeypatch, capsys):
    path = _write(tmp_path / "full.yaml", _REG_FULL)
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(path))
    results = set()
    for name in (None, "local", "cdx", "cl", "odd", "nope", ""):
        left, _ = pair(capsys, L.model_endpoints.resolve_registry_entry,
                       F.model_endpoints.resolve_registry_entry, name)
        results.add(left[0])
    assert results == {"ok", "raise"}
    # against an already-loaded registry (separate objects per side)
    reg_l = L.model_endpoints.load_endpoint_registry()
    reg_f = F.model_endpoints.load_endpoint_registry()
    for name in (None, "local", "nope"):
        assert outcome(F.model_endpoints.resolve_registry_entry, name, registry=reg_f) == outcome(
            L.model_endpoints.resolve_registry_entry, name, registry=reg_l
        )
    # default naming a harness entry is refused with the shared wording
    for mod in (L.model_endpoints, F.model_endpoints):
        reg = mod.EndpointRegistry(
            default_id="h",
            entries={"h": mod.EndpointEntry(id="h", base_url=None, model="m",
                                            kind="harness", harness="codex")},
        )
        got = outcome(mod.resolve_registry_entry, None, registry=reg)
        assert got[0] == "raise" and "is a harness entry (harness: codex)" in got[2]
        empty = mod.EndpointRegistry()
        assert outcome(mod.resolve_registry_entry, None, registry=empty)[2] == (
            "the model-endpoints registry declares no 'default' entry (known entries: <none>)"
        )
    for harness in ("codex", None, ""):
        assert F.model_endpoints.harness_entry_message("e", harness) == (
            L.model_endpoints.harness_entry_message("e", harness)
        )


_RAW_VALUES = [
    None, True, False, 0, 1, 2, 5, -1, "", " ", "x", " x ", [], {}, {"mode": "unmetered"},
    {"mode": "provider-reported", "z": 1}, {"group": "g"}, {"group": "g", "order": 3},
    {"group": " g ", "order": 0, "max_parallel": 2, "effort_style": "ninfer"},
    {"group": "g", "effort_style": "bogus"}, {"group": "g", "effort_style": None},
    {"group": None}, {"mode": 3}, {"mode": "free"}, "top-level", "ninfer", "unsupported",
    "chat_template_kwargs", "bogus",
]


def test_parse_helpers_parity(L, F):
    lm, fm = L.model_endpoints, F.model_endpoints
    for key in ("routing", "billing", "frontdoor", "effort_style", "tier", "family", "x"):
        for value in _RAW_VALUES:
            raws = [{key: value}, {}] if value is not None else [{key: None}, {}]
            for raw in raws:
                for fn_name in ("parse_routing", "parse_effort_style", "parse_frontdoor",
                                "parse_billing_mode"):
                    notes_l, notes_f = [], []
                    left = outcome(getattr(lm, fn_name), raw, source="src", entry_id="e", notes=notes_l)
                    right = outcome(getattr(fm, fn_name), raw, source="src", entry_id="e", notes=notes_f)
                    assert right == left and notes_f == notes_l, (fn_name, raw)
                assert outcome(fm.parse_classification_fields, raw, source="s", entry_id="e") == outcome(
                    lm.parse_classification_fields, raw, source="s", entry_id="e"
                )
                assert outcome(fm._conserve_spec, raw, source="s", entry_id="e") == outcome(
                    lm._conserve_spec, raw, source="s", entry_id="e"
                )
    for value in _RAW_VALUES:
        for fn_name in ("_require_str", "_optional_str"):
            for err in (None, ValueError):
                kw = {} if err is None else {"error": err}
                assert outcome(getattr(fm, fn_name), value, source="S", entry_id="e", key="k", **kw) == (
                    outcome(getattr(lm, fn_name), value, source="S", entry_id="e", key="k", **kw)
                )
        assert outcome(fm._optional_int, value, path=Path("p.yaml"), entry_id="e", key="k") == outcome(
            lm._optional_int, value, path=Path("p.yaml"), entry_id="e", key="k"
        )
    # the conserve wrapper re-raises as metadata error, chained to the conserve error
    got = outcome(fm._conserve_spec, {"conserve_usage": 5}, source="s", entry_id="e")
    assert got[0] == "raise" and got[1][0] == "EndpointMetadataError" and got[3][0][0] == "ConserveConfigError"


def _entry(mod, **kw):
    kw.setdefault("id", "e")
    kw.setdefault("base_url", "http://h/v1")
    kw.setdefault("model", "m")
    return mod.EndpointEntry(**kw)


def test_effort_resolution_over_entry_grid(L, F):
    lm, fm = L.model_endpoints, F.model_endpoints
    routings = [None, {"group": "g"}, {"group": "g", "effort_style": "ninfer", "effort_style_declared": True},
                {"group": "g", "effort_style": None, "effort_style_declared": True},
                {"group": "g", "effort_style": "top-level", "effort_style_declared": False}]
    count = 0
    for kind in ("transport", "harness"):
        for style in (None, "top-level", "ninfer", "chat_template_kwargs", "unsupported"):
            for declared in (False, True):
                for frontdoor in (False, True):
                    for routing in routings:
                        for reasoning in (None, "high"):
                            kw = dict(kind=kind, effort_style=style, effort_style_declared=declared,
                                      frontdoor=frontdoor, reasoning_effort=reasoning)
                            if kind == "harness":
                                kw.update(base_url=None, harness="codex")
                            entries = []
                            for mod in (lm, fm):
                                r = None if routing is None else mod.RoutingConfig(**routing)
                                entries.append(_entry(mod, routing=r, **kw))
                            e_l, e_f = entries
                            for fn_name in ("resolve_effort_style", "deployment_effort_style"):
                                assert outcome(getattr(fm, fn_name), e_f) == outcome(getattr(lm, fn_name), e_l)
                            assert outcome(fm.effort_notes, e_f, source="S") == outcome(
                                lm.effort_notes, e_l, source="S")
                            assert outcome(
                                fm.effort_delivery, effort_style=style, effort_style_declared=declared,
                                frontdoor=frontdoor, routing=e_f.routing,
                            ) == outcome(
                                lm.effort_delivery, effort_style=style, effort_style_declared=declared,
                                frontdoor=frontdoor, routing=e_l.routing,
                            )
                            count += 1
    assert count == 2 * 5 * 2 * 2 * 5 * 2


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------

_CFG_FLEET = """\
default_endpoint: local
endpoints:
  local:
    base_url: http://localhost:9/v1
    key_env: null
    model: qwen-local
    reasoning_effort: high
    effort_style: top-level
    tier: 2
    family: qwen
    conserve_usage: true
  kf:
    base_url: http://kf/v1
    key_env: KF_KEY
    key_file: "  KEYFILE  "
    models:
      kfm: {slug: vendor/kf-model}
    default: kfm
    defaultCheap: kfm
    account_check: none
  nokeyenv:
    base_url: http://nk/v1
  hh:
    harness: codex
    model: gpt
    effort: high
  bothk:
    base_url: http://b/v1
    harness: codex
    model: m
  nobase:
    key_env: X
  frontd:
    base_url: http://f/v1
    key_env: null
    model: fm
    frontdoor: true
    billing: {mode: unmetered}
  alias:
    base_url: http://a/v1
    key_env: OTHER_KEY
    model: dm
    models: {zz: {slug: z/z}, noslug: {x: 1}, bare: 5}
models:
  extra: {slug: fleet/extra}
default: extra
"""

_CFG_USER = """\
endpoints:
  openrouter:
    account_check: none
  local:
    tier: 3
models:
  usr: {slug: user/model}
"""

_CFG_PROJECT = """\
default_endpoint: [kf, local]
endpoints:
  proj:
    base_url: http://proj/v1
    key_env: PROJ_KEY
    model: pm
models:
  prj: {slug: project/model}
"""

_CFG_LEGACY_PROJECT = """\
models:
  prj: {slug: legacy-project/model}
  legacy: {slug: legacy/model}
"""


def _layer_setup(home, tmp_path, **texts):
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    paths = _layers(home, project)
    for name, text in texts.items():
        _write(paths[name], text)
    return str(project)


def test_models_surface(L, F):
    public = sorted(
        n for n, v in vars(L.models).items()
        if not n.startswith("_")
        and getattr(v, "__module__", None) == L.models.__name__
    ) + [
        "DEFAULT_MODEL_CONFIG", "DEFAULT_ENDPOINT_NAME", "CONFIG_PLUGIN",
        "CONFIG_MARKETPLACE", "CONFIG_FILE", "_registry_entry_ids", "_default_endpoint_value",
        "_config_entry_kind", "_config_model_entry", "_registry_endpoint", "_config_effort_keys",
    ]
    assert len(public) > 15
    assert_surface(L.models, F.models, public)
    assert F.models.CONFIG_PLUGIN == LEGACY_NAME and F.models.CONFIG_MARKETPLACE == "plugins-kit"
    assert F.models.DEFAULT_MODEL_CONFIG is not L.models.DEFAULT_MODEL_CONFIG


def test_fleet_config_path_and_baseline(L, F, home):
    assert norm(F.models.fleet_config_path()) == norm(L.models.fleet_config_path())
    assert F.models.fleet_config_path() == home / ".claude" / "config" / f"{LEGACY_NAME}.yaml"
    assert norm(F.models.DEFAULT_MODEL_CONFIG) == norm(L.models.DEFAULT_MODEL_CONFIG)
    for value in ("claude-opus-5-5", "claude-sonnet-4", "claude-x", "claude", "gpt-5", "", "a-b-c"):
        assert F.models.claude_cli_alias(value) == L.models.claude_cli_alias(value)


def test_load_model_config_layers(L, F, home, tmp_path, capsys):
    project = _layer_setup(
        home, tmp_path, fleet=_CFG_FLEET, user=_CFG_USER, project=_CFG_PROJECT,
        legacy_project=_CFG_LEGACY_PROJECT,
    )
    for kwargs in ({}, {"project_root": project}, {"project_root": str(tmp_path / "nonexistent")}):
        left, err = pair(capsys, L.models.load_model_config, F.models.load_model_config, **kwargs)
        assert left[0] == "ok" and err == ""
    merged = F.models.load_model_config(project_root=project)
    assert merged["models"]["prj"] == {"slug": "project/model"}  # canonical project beats legacy project
    assert merged["models"]["legacy"] == {"slug": "legacy/model"}
    assert merged["endpoints"]["local"]["tier"] == 3  # user beats fleet
    assert merged["default_endpoint"] == ["kf", "local"]
    assert merged["endpoints"]["openrouter"]["base_url"].startswith("https://openrouter.ai")


def test_load_model_config_degrades_on_unreadable_layers(L, F, home, tmp_path, capsys, monkeypatch):
    project = _layer_setup(home, tmp_path, fleet="endpoints: [unclosed\n")
    left, err = pair(capsys, L.models.load_model_config, F.models.load_model_config, project_root=project)
    assert left[0] == "ok" and "cannot read layered config" in err
    assert left[1] == norm(L.models.DEFAULT_MODEL_CONFIG)
    # bootstrap_lib unavailable: the shipped baseline, with the stderr notice
    monkeypatch.setitem(sys.modules, "bootstrap_lib.config_resolve", None)
    left, err = pair(capsys, L.models.load_model_config, F.models.load_model_config)
    assert left[0] == "ok" and "bootstrap_lib unavailable" in err
    assert err.startswith(f"{LEGACY_NAME}: ")


def test_default_endpoint_helpers(L, F, capsys):
    configs = [
        {}, {"default_endpoint": None}, {"default_endpoint": ""}, {"default_endpoint": "kf"},
        {"default_endpoint": ["a", "b"]}, {"default_endpoint": []}, {"default_endpoint": [" ", 3, " x "]},
        {"default_endpoint": ("t",)}, {"default_endpoint": 5}, {"default_endpoint": {"a": 1}},
    ]
    for cfg in configs:
        assert F.models.default_endpoint_name(cfg) == L.models.default_endpoint_name(cfg)
        assert norm(F.models._default_endpoint_value(cfg)) == norm(L.models._default_endpoint_value(cfg))
        if cfg.get("default_endpoint") in (None, "", []):
            # declared difference: no shipped default declaration (see the test below). The
            # legacy reader fell back to openrouter for each of these; the final one reads
            # "nothing declared" as [] and an explicit empty list as an error.
            assert L.models.default_declaration(config=cfg) == ["openrouter"]
            if cfg.get("default_endpoint") == []:
                with pytest.raises(Exception, match="empty list"):
                    F.models.default_declaration(config=cfg)
            else:
                assert F.models.default_declaration(config=cfg) == []
            continue
        left, _ = pair(capsys, L.models.default_declaration, F.models.default_declaration, config=cfg)
        assert left[0] in {"ok", "raise"}


def test_shipped_default_endpoint_is_a_declared_difference(L, F):
    """The final package ships no default declaration; the legacy baseline named openrouter."""
    assert L.models.DEFAULT_MODEL_CONFIG["default_endpoint"] == "openrouter"
    assert "default_endpoint" not in F.models.DEFAULT_MODEL_CONFIG
    assert F.models.default_declaration(config=dict(F.models.DEFAULT_MODEL_CONFIG)) == []
    # recognised only as the exact shipped value next to an endpoints map
    assert shipped_default_endpoint_to_final({"default_endpoint": "openrouter"}) == {"default_endpoint": "openrouter"}
    kept = shipped_default_endpoint_to_final({"default_endpoint": "kf", "endpoints": {}})
    assert kept == {"endpoints": {}, "default_endpoint": "kf"} and list(kept) == ["endpoints", "default_endpoint"]


def test_default_declaration_loads_config_and_honors_patched_probe(L, F, home, tmp_path, capsys, monkeypatch):
    project = _layer_setup(home, tmp_path, fleet=_CFG_FLEET, project=_CFG_PROJECT)
    left, _ = pair(capsys, L.models.default_declaration, F.models.default_declaration, project_root=project)
    assert left[0] == "ok" and left[1][1] == ["kf", "local"]
    # the call-time lookup honors a patch of the syntax seam
    marker = types.SimpleNamespace(parse=lambda value: ("parsed", value))
    monkeypatch.setattr(F.declaration_syntax, "_model_declaration", lambda: marker)
    assert F.models.default_declaration(config={"default_endpoint": "z"}) == ("parsed", "z")


def test_model_declaration_probe_parity(L, F, monkeypatch):
    # Exercise the retained legacy bootstrap compatibility branch explicitly.
    from llm_kit import _runtime
    monkeypatch.setattr(_runtime, "_MODE", "legacy")
    # linked and current: the same validator module object
    assert F.declaration_syntax._model_declaration() is L.declaration._model_declaration()
    assert F.declaration_syntax._MODEL_DECLARATION_BOOTSTRAP == L.declaration._MODEL_DECLARATION_BOOTSTRAP
    assert_surface(L.declaration, F.declaration_syntax, ["DeclarationSupportError"])
    import bootstrap_lib

    # too old: no parse
    monkeypatch.setattr(bootstrap_lib, "model_declaration",
                        types.SimpleNamespace(CORE_IDS=frozenset()), raising=False)
    left = outcome(L.declaration._model_declaration)
    assert outcome(F.declaration_syntax._model_declaration) == left
    assert left[0] == "raise" and left[1][:2] == ["DeclarationSupportError", "ImportError"]
    # too old: CORE_IDS is not a frozenset
    monkeypatch.setattr(bootstrap_lib, "model_declaration",
                        types.SimpleNamespace(parse=lambda v: v, CORE_IDS=["a"]), raising=False)
    left = outcome(L.declaration._model_declaration)
    assert outcome(F.declaration_syntax._model_declaration) == left and left[0] == "raise"
    # acceptable stand-in
    fake = types.SimpleNamespace(parse=lambda v: v, CORE_IDS=frozenset({"a"}))
    monkeypatch.setattr(bootstrap_lib, "model_declaration", fake, raising=False)
    assert F.declaration_syntax._model_declaration() is fake is L.declaration._model_declaration()
    # bootstrap_lib not linked at all
    monkeypatch.setitem(sys.modules, "bootstrap_lib", None)
    left = outcome(L.declaration._model_declaration)
    right = outcome(F.declaration_syntax._model_declaration)
    assert right == left
    assert left[0] == "raise" and "shared_lib_imports" in left[2] and "ImportError" in left[3][0]


_ENDPOINT_NAMES = [
    None, "", "openrouter", "local", "kf", "nokeyenv", "hh", "bothk", "nobase", "frontd",
    "alias", "proj", "unknown", "reg-t", "reg-h", "reg-nokey", "cdx",
]
_REGISTRY_FOR_MODELS = """\
default: reg-t
models:
  reg-t: {base_url: http://reg/v1, model: rm, key_env: REG_KEY, reasoning_effort: low, effort_style: top-level, context_window: 4096, frontdoor: true}
  reg-nokey: {base_url: http://reg2/v1, model: rm2}
  reg-h: {harness: codex, model: gpt, effort: high}
  local: {base_url: http://shadowed/v1, model: shadowed}
"""


def test_resolve_endpoint_and_model_over_config_and_registry(L, F, home, tmp_path, monkeypatch, capsys):
    project = _layer_setup(
        home, tmp_path, fleet=_CFG_FLEET, user=_CFG_USER, project=_CFG_PROJECT,
        legacy_project=_CFG_LEGACY_PROJECT,
    )
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(_write(tmp_path / "reg.yaml", _REGISTRY_FOR_MODELS)))
    shapes = set()
    for name in _ENDPOINT_NAMES:
        for kwargs in ({}, {"project_root": project}):
            left, _ = pair(capsys, L.models.resolve_endpoint, F.models.resolve_endpoint, name, **kwargs)
            shapes.add(left[0])
        cfg = L.models.load_model_config(project_root=project)
        pair(capsys, L.models.resolve_endpoint, F.models.resolve_endpoint, name, config=cfg)
    assert shapes == {"ok", "raise"}
    selections = [None, "", "extra", "usr", "prj", "legacy", "vendor/x", "kfm", "zz", "noslug", "bare",
                  "rm", "z/z", "missing"]
    seen = set()
    for endpoint in (None, "openrouter", "kf", "alias", "local", "reg-t", "frontd", "unknown"):
        for name in selections:
            for cheap in (False, True):
                left, _ = pair(capsys, L.models.resolve_model, F.models.resolve_model, name,
                               cheap=cheap, project_root=project, endpoint=endpoint)
                seen.add(left[0])
    assert seen == {"ok", "raise"}
    for name in selections[1:] + ["gpt-mini", "qwen", "or-qwen", "sol", "fable"]:
        for kwargs in ({}, {"project_root": project}):
            pair(capsys, L.models.is_model_alias, F.models.is_model_alias, name, **kwargs)


def test_resolve_endpoint_over_explicit_config_shapes(L, F, home, capsys):
    """Configs handed in directly reach branches the layered baseline never does."""
    configs = [
        {},
        {"endpoints": {}},
        {"endpoints": "x"},
        {"endpoints": {"openrouter": {}}},
        {"endpoints": {"openrouter": {"key_env": None}}},
        {"endpoints": {"openrouter": {"base_url": "", "key_env": "K"}}},
        {"endpoints": {"openrouter": 5}},
        {"endpoints": {"a": {"base_url": "u", "key_env": "K"}}, "default_endpoint": "a",
         "models": {"m": {"slug": "s/m"}}, "default": "m", "defaultCheap": "m"},
        {"endpoints": {"a": {"base_url": "u", "key_env": "K", "account_check": "x"}}},
        {"endpoints": {"a": {"base_url": "u", "key_env": ""}}},
        {"endpoints": {"a": {"base_url": "u", "key_env": "K", "key_file": "   "}}},
        {"endpoints": {"a": {"base_url": "u", "key_env": "K", "key_file": 5}}},
        {"endpoints": {"a": {"base_url": "u", "key_env": "K", "model": "dm", "models": "notadict"}}},
        {"endpoints": {"a": {"base_url": "u", "key_env": "K", "model": "dm", "default": "x",
                             "defaultCheap": "y"}}},
        {"endpoints": {"a": {"base_url": "u", "key_env": "K", "models": {"q": {"slug": "p/q"}},
                             "default": "q"}}},
        {"endpoints": {"a": {"harness": "codex", "model": "m"}}},
        {"endpoints": {"a": {"harness": "codex"}}},
        {"endpoints": {"a": {"base_url": "u", "harness": "claude", "model": "m"}}},
        {"endpoints": {"a": {"base_url": "u", "key_env": "K", "frontdoor": "x", "billing": {"mode": "bad"},
                             "effort_style": "bogus"}}},
        {"endpoints": {"a": {"base_url": "u", "key_env": "K", "frontdoor": True}}},
        {"endpoints": {"a": {"base_url": "u", "key_env": "K", "effort_style": "ninfer",
                             "frontdoor": True}}},
    ]
    seen = set()
    for cfg in configs:
        for name in (None, "openrouter", "a", "zzz"):
            left, _ = pair(capsys, L.models.resolve_endpoint, F.models.resolve_endpoint, name, config=cfg)
            seen.add(left[0])
            for selector_args in ({}, {"cheap": True}, {"name": "m"}, {"name": "s/m"}, {"name": "q"}):
                pair(capsys, L.models.resolve_model, F.models.resolve_model,
                     selector_args.get("name"), cheap=selector_args.get("cheap", False),
                     config=cfg, endpoint=name)
    assert seen == {"ok", "raise"}
    assert F.models.resolve_endpoint(config={})["account_check"] == "openrouter"
    assert F.models.resolve_endpoint("a", config=configs[8])["account_check"] == "x"
    assert F.models.resolve_endpoint("a", config=configs[7])["account_check"] == "none"
    # a name given as a bare string/odd config shape is classified, never raised
    for name in ("x", "a/b", "", "openrouter"):
        for cfg in configs[:6]:
            pair(capsys, L.models.is_model_alias, F.models.is_model_alias, name, config=cfg)


def test_discover_model_entries_parity(L, F, home, tmp_path, monkeypatch, capsys):
    project = _layer_setup(
        home, tmp_path, fleet=_CFG_FLEET, user=_CFG_USER, project=_CFG_PROJECT,
    )
    reg_path = _write(tmp_path / "reg.yaml", _REGISTRY_FOR_MODELS)
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(reg_path))
    for kwargs in ({}, {"project_root": project}):
        left, _ = pair(capsys, L.models.discover_model_entries, F.models.discover_model_entries, **kwargs)
        assert left[0] == "ok"
    assert left[1]["notes"][1], "skip notes expected"
    # config shapes that exercise the note and error branches
    configs = [
        {"endpoints": "nope"},
        {"endpoints": {"a": 5, "b": {}, "c": {"base_url": "u", "model": 5}}},
        {"endpoints": {"d": {"base_url": "u", "model": "m", "tier": 9}}},
        {"endpoints": {"e": {"harness": "codex"}}},
        {"endpoints": {"f": {"base_url": "u", "model": "m", "frontdoor": "x", "billing": 3,
                             "effort_style": "bogus", "conserve_usage": True}}},
        {"endpoints": {"g": {"base_url": "u", "harness": "claude", "model": "m"}}},
        {"endpoints": {"reg-t": {"base_url": "http://override/v1", "model": "o", "key_env": None}}},
        {"endpoints": {"reg-h": {"base_url": "http://wrapper/v1"}}},
        {},
    ]
    for cfg in configs:
        pair(capsys, L.models.discover_model_entries, F.models.discover_model_entries, config=cfg)
    # an already-loaded registry skips the file read
    reg_l = L.model_endpoints.load_endpoint_registry()
    reg_f = F.model_endpoints.load_endpoint_registry()
    assert outcome(F.models.discover_model_entries, config={}, registry=reg_f) == outcome(
        L.models.discover_model_entries, config={}, registry=reg_l
    )
    # an unreadable registry is swallowed only by the diagnostics helper
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(tmp_path / "gone.yaml"))
    assert F.models._registry_entry_ids() == L.models._registry_entry_ids() == set()
    assert outcome(F.models.discover_model_entries, config={}) == outcome(L.models.discover_model_entries, config={})
    assert outcome(F.models._registry_endpoint, "x") == outcome(L.models._registry_endpoint, "x")


def test_config_entry_helpers_parity(L, F):
    raws = [
        {}, {"base_url": "u"}, {"harness": "codex"}, {"base_url": "u", "harness": "codex"},
        {"base_url": "u", "model": "m", "name": "n", "key_env": "K", "reasoning_effort": "x",
         "tier": 1, "family": "f", "conserve_usage": {"pool": "primary"}, "frontdoor": True,
         "billing": {"mode": "unmetered"}, "effort_style": "ninfer"},
        {"harness": "codex", "model": "m", "effort": "high", "name": "n"},
        {"harness": "codex", "model": ""}, {"base_url": "u", "model": "m", "name": 5},
        {"base_url": "", "model": "m"},
    ]
    for raw in raws:
        assert outcome(F.models._config_entry_kind, "ep", raw) == outcome(L.models._config_entry_kind, "ep", raw)
        notes_l, notes_f = [], []
        assert outcome(F.models._config_model_entry, "ep", raw, notes_f) == outcome(
            L.models._config_model_entry, "ep", raw, notes_l)
        assert notes_f == notes_l
        for kind in ("transport", "harness"):
            assert outcome(F.models._config_required_str, "ep", raw, kind=kind, key="model") == outcome(
                L.models._config_required_str, "ep", raw, kind=kind, key="model")
            assert outcome(F.models._config_optional_str, "ep", raw, kind=kind, key="name") == outcome(
                L.models._config_optional_str, "ep", raw, kind=kind, key="name")
        assert outcome(F.models._config_effort_keys, "ep", raw) == outcome(L.models._config_effort_keys, "ep", raw)
        assert outcome(F.models._config_conserve, "ep", raw) == outcome(L.models._config_conserve, "ep", raw)
        assert outcome(F.models._config_classification, "ep", raw) == outcome(
            L.models._config_classification, "ep", raw)
    disc_l, disc_f = L.models.ModelDiscovery({"a": 1}, notes=["n"]), F.models.ModelDiscovery({"a": 1}, notes=["n"])
    assert norm(disc_f) == norm(disc_l) and disc_f.entries is disc_f and disc_l.entries is disc_l


# --------------------------------------------------------------------------
# api_key
# --------------------------------------------------------------------------


def test_api_key_surface(L, F):
    assert_surface(L.api_key, F.api_key, [
        "KeyLookupResult", "get_api_key", "_resolve_key", "_read_key_file", "_warn_legacy_location",
        "_default_endpoint_key_file",
    ])
    assert F.api_key.API_KEY_ENV == L.api_key.API_KEY_ENV


@pytest.fixture()
def user_env(L, F, home, monkeypatch):
    """Point both api_key modules' user layer at the isolated home."""
    path = home / ".claude" / "plugins" / "data" / "plugins-kit" / LEGACY_NAME / ".env"
    monkeypatch.setattr(L.api_key, "USER_ENV_FILE", path)
    monkeypatch.setattr(F.api_key, "USER_ENV_FILE", path)
    return path


def _get_key(L, F, capsys, *args, **kwargs):
    L.api_key._LEGACY_NOTICE_SEEN.clear()
    F.api_key._LEGACY_NOTICE_SEEN.clear()
    return pair(capsys, L.api_key.get_api_key, F.api_key.get_api_key, *args, **kwargs)


def test_get_api_key_layer_precedence(L, F, home, tmp_path, user_env, monkeypatch, capsys):
    project = Path(_layer_setup(home, tmp_path))
    canonical = F.constants.project_env_file(project)
    legacy = F.constants.legacy_project_env_file(project)
    sources = []

    def step():
        left, err = _get_key(L, F, capsys, project)
        sources.append((left[1]["source"], left[1]["legacy_location"], bool(err)))
        return left

    assert step()[1]["source"] == "missing"
    F.env_file.write_env_file(user_env, {"OPENROUTER_API_KEY": "user-key", "KF_KEY": "kf-user"})
    assert step()[1]["key"] == "user-key"
    F.env_file.write_env_file(legacy, {"OPENROUTER_API_KEY": "legacy-key"})
    left = step()
    assert left[1]["key"] == "legacy-key" and left[1]["legacy_location"] is True
    F.env_file.write_env_file(canonical, {"OPENROUTER_API_KEY": "project-key"})
    assert step()[1]["key"] == "project-key"
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    assert step()[1]["key"] == "env-key"
    assert sources == [
        ("missing", False, False), ("user", False, False), ("project", True, True),
        ("project", False, False), ("env", False, False),
    ]
    # named endpoints resolve their own key_env through the same layers
    monkeypatch.delenv("OPENROUTER_API_KEY")
    _layer_setup(home, tmp_path, fleet=_CFG_FLEET)
    for endpoint in ("kf", "local", "frontd", "alias", "openrouter", "unknown", "nokeyenv", "hh"):
        for root in (project, None, str(project)):
            _get_key(L, F, capsys, root, endpoint=endpoint)
    # the legacy-path notice is printed once per path per process, on stderr, with legacy wording
    L.api_key._LEGACY_NOTICE_SEEN.clear()
    F.api_key._LEGACY_NOTICE_SEEN.clear()
    capsys.readouterr()
    F.env_file.write_env_file(canonical, {})
    F.api_key.get_api_key(project, endpoint="openrouter")
    F.api_key.get_api_key(project, endpoint="openrouter")
    err = capsys.readouterr().err
    assert err.count("API key read from") == 1
    assert err.startswith(f"{LEGACY_NAME}: API key read from {legacy}, a superseded location.")


def test_get_api_key_key_file_layer(L, F, home, tmp_path, user_env, monkeypatch, capsys):
    project = Path(_layer_setup(home, tmp_path, fleet=_CFG_FLEET))
    keyfile = tmp_path / "KEYFILE"
    monkeypatch.chdir(tmp_path)  # the endpoint's key_file is relative: "KEYFILE"
    for content in (b"file-key\n", b"  spaced  \n", b"", b"two\nlines", b"a\rb", b"\xff\xfe\x00bad", b"ok"):
        keyfile.write_bytes(content)
        for endpoint in ("kf",):
            left, _ = _get_key(L, F, capsys, project, endpoint=endpoint)
            assert left[0] == "ok"
        assert outcome(F.api_key._read_key_file, str(keyfile)) == outcome(L.api_key._read_key_file, str(keyfile))
    keyfile.write_bytes(b"file-key\n")
    left, _ = _get_key(L, F, capsys, project, endpoint="kf")
    assert left[1]["source"] == "key_file" and left[1]["key"] == "file-key"
    assert outcome(F.api_key._read_key_file, str(tmp_path / "absent")) == ("ok", None)
    assert outcome(F.api_key._read_key_file, "~/nope") == outcome(L.api_key._read_key_file, "~/nope")
    # a higher layer wins and the key file is never consulted
    F.env_file.write_env_file(user_env, {"KF_KEY": "kf-user"})
    left, _ = _get_key(L, F, capsys, project, endpoint="kf")
    assert left[1]["source"] == "user"


def test_default_endpoint_key_file_and_degraded_resolution(L, F, home, tmp_path, monkeypatch, capsys):
    cfg_default_kf = """\
endpoints:
  openrouter:
    key_file: "~/or.key"
"""
    cfg_other_default = """\
default_endpoint: local
endpoints:
  local: {base_url: "http://l/v1", key_env: null, model: m, key_file: "~/local.key"}
"""
    cfg_broken_default = "default_endpoint: nonexistent\n"
    shapes = set()
    for text in (cfg_default_kf, cfg_other_default, cfg_broken_default, ""):
        project = _layer_setup(home, tmp_path, fleet=text or "models: {}\n")
        for root in (None, Path(project)):
            assert outcome(F.api_key._default_endpoint_key_file, root) == outcome(
                L.api_key._default_endpoint_key_file, root)
        left, _ = _get_key(L, F, capsys, Path(project))
        shapes.add(left[0])
    assert shapes == {"ok", "raise"}  # a keyless default endpoint has no key_env to look up
    # endpoint=None degrades to the hardcoded key env when resolution fails; a named endpoint raises
    _layer_setup(home, tmp_path, fleet=cfg_broken_default)
    project = Path(tmp_path / "proj")
    monkeypatch.setenv("OPENROUTER_API_KEY", "still-works")
    left, _ = _get_key(L, F, capsys, project)
    assert left[1]["key"] == "still-works"
    left, _ = _get_key(L, F, capsys, project, endpoint="nonexistent")
    assert left[0] == "raise"
    # resolve_endpoint failing inside the key-file lookup resolves to no key_file layer
    monkeypatch.setitem(sys.modules, "bootstrap_lib.config_resolve", None)
    assert outcome(F.api_key._default_endpoint_key_file, None) == outcome(
        L.api_key._default_endpoint_key_file, None)
