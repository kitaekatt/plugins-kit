"""The optional consent layer for cost classes (registry layer).

Principle: consent governs caller-selected execution policy. The library makes no
LLM calls on its own; whoever uses it makes them. A cost class is INFORMATION that
is always reported (``llm_kit.registry.cost``); this module is a policy a caller
may choose to apply. Nothing here is consulted unless a caller opts in -- protocol 2
request field ``consent``, ``llm-kit models describe --consent``, or the API call
:func:`policy`. A caller may instead build its own layer and ignore this one.

Classes: ``local`` (a transport declared unmetered AND self-owned, or a harness running on
one), ``subscription`` (a harness on a subscription session pool) and ``metered`` (paid or
unverified billing).

Policy
------
One file holds a global policy and optional named scopes::

    {"schema": 2, "local": true, "subscription": true, "metered": false,
     "scopes": {"databench": {"metered": true}}, "updated_at": "..."}

Global classes are a CEILING; a scope may only restrict. An absent scope key
inherits, so ``effective = global AND scope.get(class, true)``. A scope ``on`` that
the global policy blocks is reported as blocked, with the global command that would
lift the block; it never widens the global policy. A global change preserves every
scope entry.

Without a file the policy is ``{local: true, subscription: true, metered: false}``
with provenance ``default``; reading it creates nothing. A file that cannot be read
or is malformed raises :class:`ConsentConfigError` -- when a caller opts in. Nothing
reads the file otherwise.

Location, highest precedence first (:func:`consent_read_path`):

1. ``$LLM_KIT_CONSENT_FILE`` when set: that file alone, read and written.
2. ``$XDG_CONFIG_HOME/llm-kit/consent.json`` (fallback
   ``$HOME/.config/llm-kit/consent.json``) when it exists: the neutral file.
3. the legacy file an earlier release kept under ``~/.claude``
   (:func:`legacy_consent_path`), read only when the neutral file does not exist.
4. none: the default policy.

A write (:func:`save_config`) always targets 1 or 2 and never the legacy file. Paths
are resolved on each call (never at import).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .user_paths import ENV_CONSENT_FILE, neutral_consent_file, override_path

# Cost class names. Spelled here as literals because this module stays
# independent of registry/cost.py (the two are siblings in layer 0).
LOCAL, SUBSCRIPTION, METERED = "local", "subscription", "metered"
CLASSES = (LOCAL, SUBSCRIPTION, METERED)

#: The policy in force when there is no file.
DEFAULT_CLASSES: Mapping[str, bool] = {LOCAL: True, SUBSCRIPTION: True, METERED: False}

SCHEMA = 2

#: ``ClassPolicy.provenance`` values: the layer that decided the effective value.
FROM_DEFAULT = "default"
FROM_GLOBAL = "global"
FROM_SCOPE = "scope"

#: ``Policy.source`` values.
SOURCE_DEFAULT = "default"
SOURCE_FILE = "file"

_FILE_KEYS = ("schema", "updated_at", "scopes") + CLASSES


class ConsentConfigError(ValueError):
    """The consent file cannot be read or is malformed.

    Raised only when a caller opts in to the layer. A :class:`ValueError`, so the
    CLI reports it as a configuration error (exit 2).
    """


def legacy_consent_path() -> Path:
    """``Path.home()/.claude/plugins/data/plugins-kit/llm-kit/consent.json``: the read-only legacy location."""
    return Path.home() / ".claude" / "plugins" / "data" / "plugins-kit" / "llm-kit" / "consent.json"


def default_consent_path(environ: Optional[Mapping[str, str]] = None) -> Path:
    """Where consent is WRITTEN: ``$LLM_KIT_CONSENT_FILE``, else the neutral ``consent.json``."""
    return override_path(ENV_CONSENT_FILE, environ) or neutral_consent_file(environ)


def consent_read_path(environ: Optional[Mapping[str, str]] = None) -> Path:
    """The file :func:`load_config` reads, by the precedence in the module docstring.

    The write target (:func:`default_consent_path`) when it is an explicit
    choice (the override, or any path other than the neutral default) or when
    anything exists at it (an unreadable neutral file is reported, never
    skipped for the legacy one); else the legacy file when it exists; else the
    neutral path, which is absent. Touches nothing.
    """
    primary = default_consent_path() if environ is None else default_consent_path(environ)
    if override_path(ENV_CONSENT_FILE, environ) is not None or primary != neutral_consent_file(environ):
        return primary
    if os.path.lexists(primary):
        return primary
    legacy = legacy_consent_path()
    if os.path.lexists(legacy):
        return legacy
    return primary


def validate_scope_name(scope: Any) -> str:
    """``scope`` as a usable scope name, or :class:`ValueError`: a non-empty string without surrounding space."""
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError(f"a consent scope must be a non-empty string, got {scope!r}")
    if scope != scope.strip():
        raise ValueError(f"a consent scope must not start or end with whitespace, got {scope!r}")
    return scope


def set_command(cost_class: str, *, scope: Optional[str] = None, value: str = "on") -> str:
    """The exact ``llm-kit consent set`` command that sets ``cost_class`` for ``scope`` (global when None)."""
    if cost_class not in CLASSES:
        raise ValueError(f"unknown cost class {cost_class!r}; valid classes: {', '.join(CLASSES)}")
    where = f" --scope {scope}" if scope is not None else ""
    return f"llm-kit consent set{where} --{cost_class} {value}"


# ---------------------------------------------------------------------------
# The stored file
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsentConfig:
    """What the file states, before any scope is applied.

    ``globals_`` holds only the global classes the file states (absent keys fall
    back to :data:`DEFAULT_CLASSES`); ``scopes`` maps a scope name to the classes it
    states. ``source`` is ``default`` (no file) or ``file``.
    """

    globals_: Mapping[str, bool]
    scopes: Mapping[str, Mapping[str, bool]]
    source: str
    path: Path


def _bool_map(data: Any, where: str, path: Path, *, allowed: Tuple[str, ...] = CLASSES) -> Dict[str, bool]:
    result: Dict[str, bool] = {}
    for key, value in data.items():
        if key not in allowed:
            raise ConsentConfigError(
                f"consent file {path}: unknown key {key!r} in {where}; valid keys: {', '.join(allowed)}"
            )
        if type(value) is not bool:
            raise ConsentConfigError(f"consent file {path}: {where} {key!r} must be true or false, got {value!r}")
        result[key] = value
    return result


def load_config(path: Optional[Path] = None) -> ConsentConfig:
    """Read the consent file; a missing file is the default policy and creates nothing.

    Raises :class:`ConsentConfigError` for a file that cannot be read, is not valid
    JSON, or breaks the format (wrong ``schema``, an unknown key, a non-boolean).
    """
    target = Path(path) if path is not None else consent_read_path()
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return ConsentConfig(globals_={}, scopes={}, source=SOURCE_DEFAULT, path=target)
    except OSError as exc:
        raise ConsentConfigError(f"consent file {target} cannot be read ({exc.__class__.__name__}: {exc})") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ConsentConfigError(f"consent file {target} is not valid JSON ({exc.__class__.__name__})") from exc
    if not isinstance(data, dict):
        raise ConsentConfigError(f"consent file {target} is not a JSON object")
    schema = data.get("schema")
    if type(schema) is not int or schema != SCHEMA:
        raise ConsentConfigError(
            f"consent file {target} has schema {schema!r}, not {SCHEMA}; the classes are "
            f"{', '.join(CLASSES)} (the retired usage/spend keys are not read); fix or remove the file, "
            f"then run `{set_command(METERED, value='off')}` to write a new one"
        )
    unknown = sorted(str(k) for k in data if k not in _FILE_KEYS)
    if unknown:
        raise ConsentConfigError(
            f"consent file {target}: unknown key(s) {', '.join(repr(k) for k in unknown)}; "
            f"valid keys: {', '.join(_FILE_KEYS)}"
        )
    globals_ = _bool_map({k: data[k] for k in CLASSES if k in data}, "the global policy", target)
    scopes_raw = data.get("scopes", {})
    if not isinstance(scopes_raw, dict):
        raise ConsentConfigError(f"consent file {target}: 'scopes' must be an object of scope name to classes")
    scopes: Dict[str, Dict[str, bool]] = {}
    for name, classes in scopes_raw.items():
        try:
            validate_scope_name(name)
        except ValueError as exc:
            raise ConsentConfigError(f"consent file {target}: {exc}") from None
        if not isinstance(classes, dict):
            raise ConsentConfigError(f"consent file {target}: scope {name!r} must be an object of classes")
        scopes[name] = _bool_map(classes, f"scope {name!r}", target)
    return ConsentConfig(globals_=globals_, scopes=scopes, source=SOURCE_FILE, path=target)


def save_config(
    *,
    globals_: Mapping[str, bool],
    scopes: Mapping[str, Mapping[str, bool]],
    path: Optional[Path] = None,
) -> Path:
    """Atomically write the consent file (tmp file in the same directory, then replace).

    All three global classes are written. Errors propagate. Returns the path written.
    """
    if set(globals_) != set(CLASSES) or any(type(v) is not bool for v in globals_.values()):
        raise TypeError(f"globals_ must give each of {', '.join(CLASSES)} as a bool")
    clean_scopes: Dict[str, Dict[str, bool]] = {}
    for name, classes in scopes.items():
        validate_scope_name(name)
        if any(k not in CLASSES or type(v) is not bool for k, v in classes.items()):
            raise TypeError(f"scope {name!r} must map cost classes to bool")
        clean_scopes[name] = dict(classes)
    target = Path(path) if path is not None else default_consent_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {"schema": SCHEMA}
    payload.update({cls: globals_[cls] for cls in CLASSES})
    payload["scopes"] = clean_scopes
    payload["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".consent.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return target


def update_config(
    changes: Mapping[str, bool], *, scope: Optional[str] = None, path: Optional[Path] = None
) -> Tuple[Path, ConsentConfig]:
    """Apply ``changes`` (class -> bool) to the global policy, or to ``scope``, and write the file.

    The rest of the file is preserved: a global change keeps every scope entry and a
    scope change keeps the global policy and the other scopes. The write target is
    ``path`` or :func:`default_consent_path`; the content it starts from is what
    :func:`load_config` reads (the legacy file, when that is the only one, is read
    but never written). Raises :class:`ConsentConfigError` when the existing file is
    malformed (it is never overwritten silently).
    """
    unknown = sorted(str(k) for k in changes if k not in CLASSES)
    if unknown:
        raise ValueError(f"unknown cost class {', '.join(repr(u) for u in unknown)}; valid classes: {', '.join(CLASSES)}")
    if scope is not None:
        validate_scope_name(scope)
    target = Path(path) if path is not None else default_consent_path()
    current = load_config(path if path is not None else consent_read_path())
    globals_ = {cls: current.globals_.get(cls, DEFAULT_CLASSES[cls]) for cls in CLASSES}
    scopes = {name: dict(classes) for name, classes in current.scopes.items()}
    if scope is None:
        globals_.update(changes)
    else:
        scopes.setdefault(scope, {}).update(changes)
    written = save_config(globals_=globals_, scopes=scopes, path=target)
    return written, load_config(written)


# ---------------------------------------------------------------------------
# The effective policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassPolicy:
    """The effective state of one class and where it came from.

    ``allowed`` is the effective value. ``provenance`` names the layer that decided
    it: ``default`` (no file, or the file does not state the class), ``global`` or
    ``scope``. ``blocked_by_global`` is true when the scope states ``on`` and the
    global policy says off; the effective value is then off and ``commands`` holds
    the global command that lifts the block. ``commands`` is always the exact
    command(s) that would allow the class (empty when it is allowed).
    """

    cost_class: str
    allowed: bool
    provenance: str
    global_allowed: bool
    scope_allowed: Optional[bool]
    blocked_by_global: bool
    commands: Tuple[str, ...]

    def to_json(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "provenance": self.provenance,
            "global": self.global_allowed,
            "scope": self.scope_allowed,
            "blocked_by_global": self.blocked_by_global,
            "commands": list(self.commands),
        }


@dataclass(frozen=True)
class Policy:
    """The effective policy for one scope (or the global policy when ``scope`` is None)."""

    scope: Optional[str]
    classes: Mapping[str, ClassPolicy]
    source: str
    path: Path

    @property
    def allowed(self) -> frozenset:
        """The effective allowed classes."""
        return frozenset(name for name, state in self.classes.items() if state.allowed)

    def allows(self, cost_class: str) -> bool:
        if cost_class not in self.classes:
            raise ValueError(f"unknown cost class {cost_class!r}; valid classes: {', '.join(CLASSES)}")
        return self.classes[cost_class].allowed

    def refusal(self, cost_class: str) -> str:
        """Why ``cost_class`` is not allowed, naming the scope and the exact command(s) to change it."""
        state = self.classes[cost_class]
        where = f"scope {self.scope!r}" if self.scope is not None else "the global policy"
        if state.provenance == FROM_DEFAULT:
            why = f"no consent file states it, so the built-in default of off applies ({self.path})"
        elif state.provenance == FROM_GLOBAL:
            why = f"the global policy has it off ({self.path})"
        else:
            why = f"{where} has it off ({self.path})"
        return f"the consent layer ({where}) does not allow {cost_class} calls: {why}; {self.remedy(cost_class)}"

    def remedy(self, cost_class: str) -> str:
        """``run `<exact command>` to allow them`` for a class this policy does not allow."""
        state = self.classes[cost_class]
        return f"run {' and '.join('`' + c + '`' for c in state.commands)} to allow them"

    def to_json(self) -> Dict[str, Any]:
        return {
            "scope": self.scope,
            "source": self.source,
            "path": str(self.path),
            "classes": {name: state.to_json() for name, state in self.classes.items()},
        }


def effective(config: ConsentConfig, scope: Optional[str] = None) -> Policy:
    """Apply ``scope`` over the global policy in ``config``: ``global AND scope.get(class, true)``."""
    if scope is not None:
        validate_scope_name(scope)
    stated = config.scopes.get(scope, {}) if scope is not None else {}
    classes: Dict[str, ClassPolicy] = {}
    for cls in CLASSES:
        explicit_global = cls in config.globals_
        global_value = config.globals_[cls] if explicit_global else DEFAULT_CLASSES[cls]
        global_from = FROM_GLOBAL if explicit_global else FROM_DEFAULT
        scope_value = stated.get(cls)
        value = global_value and (scope_value is None or scope_value)
        if not value and scope_value is False:
            provenance = FROM_SCOPE
        elif not value:
            provenance = global_from
        elif scope_value is True:
            provenance = FROM_SCOPE
        else:
            provenance = global_from
        blocked = scope_value is True and not global_value
        commands: list = []
        if not value:
            if not global_value:
                commands.append(set_command(cls))
            if scope_value is False:
                commands.append(set_command(cls, scope=scope))
        classes[cls] = ClassPolicy(
            cost_class=cls,
            allowed=value,
            provenance=provenance,
            global_allowed=global_value,
            scope_allowed=scope_value,
            blocked_by_global=blocked,
            commands=tuple(commands),
        )
    return Policy(scope=scope, classes=classes, source=config.source, path=config.path)


def policy(scope: Optional[str] = None, *, path: Optional[Path] = None) -> Policy:
    """The effective policy for ``scope`` (global when None): the API a caller opts in with.

    Reads the consent file; with none, the default policy (provenance ``default``).
    Raises :class:`ConsentConfigError` for an unreadable or malformed file and
    ``ValueError`` for an empty scope name. Creates nothing.
    """
    if scope is not None:
        validate_scope_name(scope)
    return effective(load_config(path), scope)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def layer_description() -> Dict[str, Any]:
    """The static description of the layer: that it is optional, how a caller opts in, and the commands."""
    return {
        "optional": True,
        "opt_in": {
            "protocol_2_request": {"consent": True, "scoped": {"consent": {"scope": "<name>"}}},
            "describe_flags": ["--consent", "--consent-scope <name>"],
            "api": "llm_kit.registry.consent.policy(scope=None)",
        },
        "commands": {
            "show": "llm-kit consent show [--scope <name>] [--json]",
            "set": "llm-kit consent set [--scope <name>] --local|--subscription|--metered on|off",
        },
    }


def discovery_note(*, policy_in_use: Optional[Policy] = None) -> Dict[str, Any]:
    """One structured note saying the layer is optional and how a caller opts in.

    ``policy_in_use`` is the policy the caller opted in to, or None. Carries the
    commands and field names as data; ``llm-kit consent show`` prints the policy.
    """
    note = layer_description()
    note["in_use"] = policy_in_use is not None
    note["scope"] = policy_in_use.scope if policy_in_use is not None else None
    if policy_in_use is not None:
        note["policy"] = policy_in_use.to_json()
    return note


def discovery_line(note: Mapping[str, Any]) -> str:
    """The text rendering of :func:`discovery_note`: one line."""
    if note.get("in_use"):
        scope = note.get("scope")
        where = f"scope {scope!r}" if scope is not None else "the global policy"
        return f"Consent layer: in use ({where}); `llm-kit consent show` prints the policy."
    return (
        "Consent layer: optional, not in use; opt in with --consent [--consent-scope NAME] "
        "(protocol 2: \"consent\"), configure with `llm-kit consent set`."
    )
