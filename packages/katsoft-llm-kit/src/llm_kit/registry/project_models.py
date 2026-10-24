"""The project model file: one tracked catalogue plus named selections.

A project keeps one YAML file (default ``.llm-kit/models.yaml``, tracked in the
project's own repository):

    version: 1
    catalog:                     # ordered; the order is the project's order
      - id: luna                 # opaque, unique, chosen by the project
        label: Luna              # presentation text
        endpoint: luna           # a registry endpoint (credentials, adapter)
        model: gpt-6-luna        # the exact wire model for that endpoint
        efforts: [low, high]     # optional; a menu-ordered subset of the library menu
                                 # (efforts: [] declares NO effort for the row)
        max_output_tokens: 65536 # optional; positive integer, the output cap this model's
                                 # calls carry when the call names none (see below)
        request_defaults:        # optional; only ``extras`` (a JSON mapping)
          extras: {provider: {sort: latency}}
    selections:                  # opaque consumer keys -> {model, effort}
      draft.generate.primary: {model: luna, effort: high}

The file holds no keys, key files, base URLs, consent or vendor capability
claims: endpoints, credentials and billing resolve through the endpoint
registry, and the effort menu is the library's, measured per exact model
(``model_profiles``). A project may narrow a menu and never widen it; a harness
model the library has not measured has no menu, so its row declares no
``efforts`` and its selections carry a null effort. ``efforts: []`` declares that a
row has no effort dial at all (any model; the shipped profile of a model the vendor
lists without one has the same empty menu): every selection of it carries a null
effort and a request for it refuses any effort. ``null`` effort means "omit"; the string ``none`` is an explicit
disable value accepted only where the library menu lists it. Nothing here
translates ``off`` or any other word.

``max_output_tokens`` is the project's configured output cap for that model. A
protocol 2 call to a transport (OpenAI-compatible) row uses it when the request
names no ``max_tokens``; an explicit request ``max_tokens`` always wins, and the
row's ``request_defaults.extras.max_tokens`` is the next fallback. A row that
declares neither sends no cap to openrouter.ai, so the provider's own limit for the
model applies. The claude and codex harnesses expose no output-cap control: a harness
row may declare the field, and it is not applied (the catalogue says so in its notes).
Nothing clamps the value to a model limit; the library ships no per-model limit table.

This module is harness-independent: standard library plus PyYAML (the ``config``
extra), no ``bootstrap_lib``, no plugin path, no home-directory convention of
its own. Parsing, strict validation, canonical rendering, hashing and the
atomic save all live here; the CLI (``llm-kit models project``) is a thin shell.

Canonical form (what ``write`` stores and ``render_yaml`` returns): UTF-8, LF,
two-space indentation, one final newline, catalogue order preserved, row fields
in the fixed order id/label/endpoint/model/efforts/max_output_tokens/request_defaults, every
free-form mapping (``selections``, ``extras``) sorted by key. Comments are not
round-tripped. The digest of a file is the SHA-256 of its bytes as stored.

A second, personal layer sits beside the file: ``<file-stem>.local.yaml``
(default ``.llm-kit/models.local.yaml``), ``{version: 1, selections: {...}}``,
meant to be untracked. Its entries override the tracked selections per key; see
"The personal layer" below. The library never edits ``.gitignore``.

``write`` is optimistic: ``if_match`` compares the digest and the replacement
follows, with no lock between the two. A concurrent writer in that window wins
silently; callers that need more serialize their own writes.
"""
from __future__ import annotations

import functools
import hashlib
import json
import math
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Optional

from .effort import EFFORT_KEY, TEMPLATE_KEY, style_effort_menu
from .model_profiles import (
    MENU_ENDPOINT_STYLE,
    MENU_EXACT_MODEL,
    MENU_NONE,
    exact_effort_menu,
    route_of_base_url,
)

SCHEMA = "llm-kit.project-models/v1"
FILE_VERSION = 1
DEFAULT_FILE = ".llm-kit/models.yaml"

# ProjectModelsError.code values.
CODE_INVALID = "invalid"
CODE_STALE = "stale"
CODE_PATH = "path"
CODE_SYMLINK = "symlink"
CODE_MISSING = "missing"
CODE_DEPENDENCY = "dependency"

# The personal layer: ``<file-stem>.local.yaml`` beside the project file.
LOCAL_SUFFIX = ".local.yaml"
LAYER_TRACKED = "tracked"
LAYER_LOCAL = "local"
SOURCE_DEFAULT = "default"
SOURCE_OVERRIDE = "override"

_TOP_KEYS = ("version", "catalog", "selections")
_LOCAL_TOP_KEYS = ("version", "selections")
_ROW_KEYS = ("id", "label", "endpoint", "model", "efforts", "max_output_tokens", "request_defaults")
_ROW_REQUIRED = ("id", "label", "endpoint", "model")
_SELECTION_KEYS = ("model", "effort")
_DEFAULTS_KEYS = ("extras",)
# Extras a project default must not carry: the effort belongs to ``efforts`` and
# selections (so the menu check cannot be bypassed), and these name what the
# call itself owns.
_RESERVED_EXTRAS = frozenset({"model", "messages", "stream", "api_key", EFFORT_KEY})
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ProjectModelsError(ValueError):
    """The project model file, a document for it, or a path to it was refused.

    ``code`` is one of ``invalid`` (the document or an argument is wrong),
    ``stale`` (``if_match`` does not match the stored file), ``path`` (the file
    escapes the project root), ``symlink`` (a path component is a symlink),
    ``missing`` (the file or root does not exist), ``dependency`` (PyYAML is
    not installed).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def to_json(self) -> dict[str, str]:
        return {"kind": "project-models", "code": self.code, "message": str(self)}


def _invalid(where: str, message: str) -> ProjectModelsError:
    return ProjectModelsError(CODE_INVALID, f"{where}: {message}" if where else message)


# ---------------------------------------------------------------------------
# Endpoints and the effort menu
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EndpointInfo:
    """What validation needs to know about one registry endpoint.

    ``harness`` is set for a harness entry (``codex``, ``claude``, ...);
    ``effort_style`` for a transport or wrapper endpoint that resolves one.
    ``route`` keys the measured exact-model profiles: the harness name for a
    harness entry, ``openrouter`` for a transport whose base URL is
    openrouter.ai, None for any other endpoint.
    """

    name: str
    kind: str
    harness: Optional[str] = None
    effort_style: Optional[str] = None
    route: Optional[str] = None


EffortMenuResolver = Callable[[EndpointInfo, str], "tuple[str, ...]"]


def library_effort_menu_with_source(
    endpoint: EndpointInfo, model: str
) -> "tuple[tuple[str, ...], str]":
    """``(menu, source)``: the ordered effort menu the library accepts, and where it came from.

    EXACT-MODEL FIRST. A model with a measured profile
    (:mod:`llm_kit.registry.model_profiles`) takes that menu, for a harness
    entry and for an openrouter.ai transport alike (``library-model``). A harness
    model with no profile has NO menu: the library does not invent one
    (``none``). A transport that resolves an effort style and has no profile
    takes that style's wire menu (``library-endpoint-style``); a transport with
    neither delivers no effort (``none``). An empty menu means a row may declare
    no ``efforts`` and a selection must carry a null effort.
    """
    exact = exact_effort_menu(endpoint.route, model)
    if exact is not None:
        return exact, MENU_EXACT_MODEL
    if endpoint.harness is not None:
        return (), MENU_NONE
    menu = style_effort_menu(endpoint.effort_style)
    return menu, (MENU_ENDPOINT_STYLE if menu else MENU_NONE)


def library_effort_menu(endpoint: EndpointInfo, model: str) -> "tuple[str, ...]":
    """The ordered effort menu the library accepts for ``endpoint`` and ``model``.

    Validation's default ``menu_for`` and the seam a test may replace; see
    :func:`library_effort_menu_with_source` for the rule.
    """
    return library_effort_menu_with_source(endpoint, model)[0]


def _route_of(base_url: Optional[str]) -> Optional[str]:
    """``openrouter`` when ``base_url`` is on openrouter.ai, else None."""
    return route_of_base_url(base_url)


class _Endpoints:
    """Registry lookups for one validation pass, loaded on first use."""

    def __init__(self, project_root: Optional[str]) -> None:
        self._project_root = project_root
        self._loaded: Optional["tuple[dict, Any]"] = None
        self._seen: dict[str, Optional[EndpointInfo]] = {}

    def _load(self) -> "tuple[dict, Any]":
        if self._loaded is None:
            from .models import discover_model_entries, load_model_config  # noqa: PLC0415

            config = load_model_config(project_root=self._project_root)
            discovery = discover_model_entries(config=config, project_root=self._project_root)
            self._loaded = (config, discovery)
        return self._loaded

    def info(self, name: str) -> Optional[EndpointInfo]:
        if name not in self._seen:
            self._seen[name] = self._resolve(name)
        return self._seen[name]

    def _resolve(self, name: str) -> Optional[EndpointInfo]:
        from .model_endpoints import HARNESS_KIND, resolve_effort_style  # noqa: PLC0415

        config, discovery = self._load()
        entry = discovery.get(name)
        if entry is not None:
            if entry.kind == HARNESS_KIND:
                return EndpointInfo(name, HARNESS_KIND, harness=entry.harness, route=entry.harness)
            return EndpointInfo(
                name,
                entry.kind,
                effort_style=resolve_effort_style(entry).style,
                route=_route_of(entry.base_url),
            )
        endpoints = config.get("endpoints") or {}
        if isinstance(endpoints, Mapping) and name in endpoints:
            return self._wrapper(name, config)
        return None

    def _wrapper(self, name: str, config: dict) -> EndpointInfo:
        """A legacy wrapper endpoint (the shipped ``openrouter`` record): its resolved base URL and style."""
        from .models import EndpointResolveError, resolve_endpoint  # noqa: PLC0415

        try:
            resolved = resolve_endpoint(name, config=config, project_root=self._project_root)
        except EndpointResolveError:
            return EndpointInfo(name, "wrapper")
        return EndpointInfo(
            name,
            "wrapper",
            effort_style=resolved.get("effort_style"),
            route=_route_of(resolved.get("base_url")),
        )

    def entry(self, name: str) -> Any:
        """The discovered registry entry for ``name``, or None (a wrapper endpoint has none)."""
        return self._load()[1].get(name)

    def notes(self) -> "list[str]":
        """The registry's non-fatal loader notes (skipped declarations)."""
        return list(self._load()[1].notes)

    def entries(self) -> "Mapping[str, Any]":
        """Every discovered registry entry by id (the cost classifier's tier lookup)."""
        return self._load()[1].entries

    def known(self) -> list[str]:
        config, discovery = self._load()
        names = set(discovery)
        endpoints = config.get("endpoints") or {}
        if isinstance(endpoints, Mapping):
            names.update(str(n) for n in endpoints)
        return sorted(names)


# ---------------------------------------------------------------------------
# Strict parsing
# ---------------------------------------------------------------------------


def _json_pairs(pairs: list) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise _invalid("", f"duplicate key {key!r} in JSON input")
        out[key] = value
    return out


def _json_constant(token: str) -> Any:
    raise _invalid("", f"JSON input holds {token}, which is not a JSON value")


def parse_json_document(text: str) -> Any:
    """Parse JSON input strictly: no duplicate keys, no NaN or Infinity."""
    try:
        return json.loads(text, object_pairs_hook=_json_pairs, parse_constant=_json_constant)
    except json.JSONDecodeError as exc:
        raise _invalid("", f"input is not valid JSON: {exc}") from None


def _import_yaml() -> Any:
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        raise ProjectModelsError(
            CODE_DEPENDENCY,
            "PyYAML is required for the project model file; "
            "install katsoft-llm-kit[config]",
        ) from None
    return yaml


@functools.lru_cache(maxsize=1)
def _yaml_types() -> "tuple[type, type, type]":
    """``(Loader, Dumper, FlowList)``: a duplicate-key-rejecting loader and the canonical dumper."""
    yaml = _import_yaml()

    class Loader(yaml.SafeLoader):
        def construct_mapping(self, node: Any, deep: bool = False) -> dict:
            mapping = super().construct_mapping(node, deep=deep)
            if len(mapping) != len(node.value):
                raise yaml.constructor.ConstructorError(
                    None, None, "duplicate mapping key", node.start_mark
                )
            return mapping

    class FlowList(list):
        """A list rendered in flow style (``[a, b]``)."""

    class Dumper(yaml.SafeDumper):
        def increase_indent(self, flow: bool = False, indentless: bool = False) -> Any:
            return super().increase_indent(flow, False)

        def ignore_aliases(self, data: Any) -> bool:
            return True

    Dumper.add_representer(
        FlowList,
        lambda dumper, value: dumper.represent_sequence(
            "tag:yaml.org,2002:seq", list(value), flow_style=True
        ),
    )
    return Loader, Dumper, FlowList


def parse_yaml_document(data: bytes) -> Any:
    """Parse the stored file's bytes: one UTF-8 YAML document, duplicate keys refused."""
    yaml = _import_yaml()
    loader = _yaml_types()[0]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _invalid("", f"the file is not UTF-8: {exc}") from None
    try:
        document = yaml.load(text, Loader=loader)  # noqa: S506 -- a SafeLoader subclass
    except yaml.YAMLError as exc:
        raise _invalid("", f"the file is not valid YAML: {exc}") from None
    if document is None:
        raise _invalid("", "the file is empty")
    return document


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise _invalid(where, f"must be a string, not {type(value).__name__}")
    if not value or value != value.strip():
        raise _invalid(where, "must be non-empty with no leading or trailing whitespace")
    return value


def _mapping(value: Any, where: str, allowed: "tuple[str, ...]") -> Mapping:
    if not isinstance(value, Mapping):
        raise _invalid(where, f"must be a mapping, not {type(value).__name__}")
    for key in value:
        if not isinstance(key, str):
            raise _invalid(where, f"key {key!r} must be a string")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise _invalid(where, f"unknown key(s) {unknown}; allowed: {list(allowed)}")
    return value


def _json_value(value: Any, where: str) -> Any:
    """A deep, key-sorted copy of a JSON value; anything else is refused."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _invalid(where, "must be a finite number")
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(item, f"{where}[{i}]") for i, item in enumerate(value)]
    if isinstance(value, Mapping):
        out = {}
        for key in sorted(value, key=lambda k: (not isinstance(k, str), str(k))):
            if not isinstance(key, str):
                raise _invalid(where, f"key {key!r} must be a string")
            out[key] = _json_value(value[key], f"{where}.{key}")
        return out
    raise _invalid(where, f"must be JSON data, not {type(value).__name__}")


def _request_defaults(value: Any, where: str) -> dict:
    defaults = _mapping(value, where, _DEFAULTS_KEYS)
    out: dict = {}
    if "extras" in defaults:
        extras = defaults["extras"]
        if not isinstance(extras, Mapping):
            raise _invalid(f"{where}.extras", "must be a mapping")
        copy = _json_value(extras, f"{where}.extras")
        for key in copy:
            if key in _RESERVED_EXTRAS:
                raise _invalid(
                    f"{where}.extras", f"{key!r} is reserved; set effort through efforts and selections"
                )
        nested = copy.get(TEMPLATE_KEY)
        if isinstance(nested, Mapping) and EFFORT_KEY in nested:
            raise _invalid(
                f"{where}.extras.{TEMPLATE_KEY}",
                f"{EFFORT_KEY!r} is reserved; set effort through efforts and selections",
            )
        out["extras"] = copy
    return out


def _max_output_tokens(value: Any, where: str, row_id: str, model: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _invalid(
            where,
            f"must be a positive integer, not {value!r} (row {row_id!r}, model {model!r})",
        )
    return value


def _efforts(value: Any, where: str, menu: "tuple[str, ...]", endpoint: str, model: str) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise _invalid(where, f"must be a list, not {value!r} (an empty list declares no effort)")
    out: list[str] = []
    last = -1
    for i, item in enumerate(value):
        item_where = f"{where}[{i}]"
        if not isinstance(item, str) or not item:
            raise _invalid(
                item_where,
                f"must be a non-empty string, not {item!r} (YAML reads on/off/yes/no as booleans; quote the word)",
            )
        if not menu:
            raise _invalid(
                item_where,
                f"the library knows no effort menu for endpoint {endpoint!r} model {model!r}; omit efforts",
            )
        if item not in menu:
            raise _invalid(item_where, f"{item!r} is not in the library menu {list(menu)}")
        index = menu.index(item)
        if index <= last:
            raise _invalid(
                item_where, f"{item!r} repeats or breaks the library order {list(menu)}"
            )
        last = index
        out.append(item)
    return out


def _validate_catalog(
    catalog_in: Any, endpoints: "_Endpoints", menu_of: EffortMenuResolver
) -> "tuple[list[dict], dict[str, tuple[str, ...]], dict[str, Optional[list[str]]]]":
    """``(rows, menus, efforts_of)``: the normalized catalogue and each row's library menu and declared efforts."""
    if not isinstance(catalog_in, (list, tuple)):
        raise _invalid("catalog", f"must be a list, not {type(catalog_in).__name__}")
    rows: list[dict] = []
    menus: dict[str, "tuple[str, ...]"] = {}
    efforts_of: dict[str, Optional[list[str]]] = {}
    for i, raw in enumerate(catalog_in):
        where = f"catalog[{i}]"
        row = _mapping(raw, where, _ROW_KEYS)
        for key in _ROW_REQUIRED:
            if key not in row:
                raise _invalid(where, f"{key!r} is required")
        row_id = _text(row["id"], f"{where}.id")
        if row_id in menus:
            raise _invalid(f"{where}.id", f"duplicate id {row_id!r}")
        label = _text(row["label"], f"{where}.label")
        endpoint = _text(row["endpoint"], f"{where}.endpoint")
        model = _text(row["model"], f"{where}.model")
        info = endpoints.info(endpoint)
        if info is None:
            raise _invalid(
                f"{where}.endpoint",
                f"{endpoint!r} resolves to no registry endpoint (known: {', '.join(endpoints.known()) or '<none>'})",
            )
        menu = tuple(menu_of(info, model))
        out: dict = {"id": row_id, "label": label, "endpoint": endpoint, "model": model}
        efforts: Optional[list[str]] = None
        if "efforts" in row:
            efforts = _efforts(row["efforts"], f"{where}.efforts", menu, endpoint, model)
            out["efforts"] = efforts
        if "max_output_tokens" in row:
            out["max_output_tokens"] = _max_output_tokens(
                row["max_output_tokens"], f"{where}.max_output_tokens", row_id, model
            )
        if "request_defaults" in row:
            out["request_defaults"] = _request_defaults(
                row["request_defaults"], f"{where}.request_defaults"
            )
        menus[row_id] = menu
        efforts_of[row_id] = efforts
        rows.append(out)
    return rows, menus, efforts_of


def _validate_selections(
    selections_in: Any,
    menus: "Mapping[str, tuple[str, ...]]",
    efforts_of: "Mapping[str, Optional[list[str]]]",
) -> "dict[str, dict]":
    """The key-sorted normalized selections; each must name a catalog id and an accepted effort."""
    if not isinstance(selections_in, Mapping):
        raise _invalid("selections", f"must be a mapping, not {type(selections_in).__name__}")
    selections: dict[str, dict] = {}
    for key in selections_in:
        where = f"selections[{key!r}]" if isinstance(key, str) else "selections"
        if not isinstance(key, str):
            raise _invalid(where, f"key {key!r} must be a string")
        _text(key, f"selections key {key!r}")
        picked = _mapping(selections_in[key], where, _SELECTION_KEYS)
        if "model" not in picked:
            raise _invalid(where, "'model' is required")
        target = _text(picked["model"], f"{where}.model")
        if target not in menus:
            raise _invalid(f"{where}.model", f"{target!r} is not a catalog id")
        effort = picked.get("effort")
        if effort is not None:
            if not isinstance(effort, str) or not effort:
                raise _invalid(
                    f"{where}.effort",
                    f"must be a non-empty string or null, not {effort!r} "
                    "(YAML reads on/off/yes/no as booleans; quote the word)",
                )
            allowed = efforts_of[target] if efforts_of[target] is not None else list(menus[target])
            if effort not in allowed:
                raise _invalid(
                    f"{where}.effort",
                    f"{effort!r} is not accepted for {target!r} (accepted: {list(allowed)})",
                )
        selections[key] = {"model": target, "effort": effort}
    return {key: selections[key] for key in sorted(selections)}


def validate_document(
    data: Any,
    *,
    project_root: Optional[str] = None,
    menu_for: Optional[EffortMenuResolver] = None,
) -> dict:
    """Validate ``data`` and return its normalized, canonical-order copy.

    Raises :class:`ProjectModelsError` (``invalid``) naming the first problem,
    with a path such as ``catalog[1].efforts[0]``. Nothing is written.
    ``menu_for`` replaces :func:`library_effort_menu` (the exact-model seam).
    """
    _import_yaml()  # the registry and the canonical form both need it: name the extra first
    menu_of = menu_for or library_effort_menu
    top = _mapping(data, "document", _TOP_KEYS)
    version = top.get("version")
    if "version" not in top:
        raise _invalid("version", "is required")
    if not isinstance(version, int) or isinstance(version, bool) or version != FILE_VERSION:
        raise _invalid("version", f"must be {FILE_VERSION}, not {version!r}")

    rows, menus, efforts_of = _validate_catalog(top.get("catalog", []), _Endpoints(project_root), menu_of)
    selections = _validate_selections(top.get("selections", {}), menus, efforts_of)
    return {
        "version": FILE_VERSION,
        "catalog": rows,
        "selections": selections,
    }


# ---------------------------------------------------------------------------
# Canonical rendering and hashing
# ---------------------------------------------------------------------------


def render_yaml(document: Mapping) -> bytes:
    """The canonical bytes of an already-validated document."""
    yaml = _import_yaml()
    _loader, dumper, flow_list = _yaml_types()
    rows = []
    for row in document["catalog"]:
        shown = dict(row)
        if "efforts" in shown:
            shown["efforts"] = flow_list(shown["efforts"])
        rows.append(shown)
    shown_document = {
        "version": document["version"],
        "catalog": rows,
        "selections": document["selections"],
    }
    text = yaml.dump(
        shown_document,
        Dumper=dumper,
        sort_keys=False,
        default_flow_style=False,
        indent=2,
        allow_unicode=True,
        width=1_000_000,
    )
    return text.encode("utf-8")


def digest(data: bytes) -> str:
    """The SHA-256 hex digest the ``if_match`` guard compares."""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# The file: confinement, read, atomic write
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectFile:
    """A confined project file: the resolved root, the absolute path, the root-relative POSIX name."""

    root: Path
    path: Path
    relative: str


def resolve_project_file(
    project_root: Optional[str] = None, file: Optional[str] = None
) -> ProjectFile:
    """Resolve ``file`` (default :data:`DEFAULT_FILE`) beneath ``project_root`` (default cwd).

    Refuses a root that is not a directory, a name that leaves the root
    (``..`` or an absolute path outside it), and any symlink among the
    components beneath the root, the file itself included. Touches nothing.
    """
    try:
        root = Path(project_root if project_root else os.getcwd()).resolve(strict=True)
    except OSError as exc:
        raise ProjectModelsError(CODE_MISSING, f"project root does not exist: {exc.filename or project_root}") from None
    if not root.is_dir():
        raise ProjectModelsError(CODE_MISSING, f"project root is not a directory: {root}")
    raw = DEFAULT_FILE if file is None else file
    if not raw or "\0" in raw:
        raise _invalid("--file", "must be a non-empty path")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    lexical = Path(os.path.normpath(candidate))
    if not lexical.is_relative_to(root) and Path(raw).is_absolute():
        # An absolute name spelled through a symlinked prefix of the root
        # (macOS /tmp, a mounted home): compare through its parent's real path.
        lexical = Path(os.path.realpath(lexical.parent)) / lexical.name
    if not lexical.is_relative_to(root):
        raise ProjectModelsError(CODE_PATH, f"{raw!r} resolves outside the project root {root}")
    relative = lexical.relative_to(root)
    if relative == Path("."):
        raise ProjectModelsError(CODE_PATH, f"{raw!r} names the project root, not a file")
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ProjectModelsError(
                CODE_SYMLINK, f"{cursor} is a symlink; the project model file must not be reached through one"
            )
    return ProjectFile(root=root, path=lexical, relative=relative.as_posix())


def _read_existing(project_file: ProjectFile) -> Optional[bytes]:
    """The stored bytes, or None when the file is absent; a non-regular file is refused."""
    try:
        info = project_file.path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise _invalid(project_file.relative, "exists and is not a regular file")
    return project_file.path.read_bytes()


@dataclass(frozen=True)
class ProjectModels:
    """The stored file, validated: its normalized document and the stored bytes' digest."""

    path: str
    sha256: str
    document: dict

    def to_json(self) -> dict:
        return {"schema": SCHEMA, "path": self.path, "sha256": self.sha256, "document": self.document}


@dataclass(frozen=True)
class Validation:
    """A document that passed validation, and the digest ``write`` would store."""

    path: str
    canonical_sha256: str
    catalog_rows: int
    selections: int

    def to_json(self) -> dict:
        return {
            "schema": SCHEMA,
            "valid": True,
            "path": self.path,
            "canonical_sha256": self.canonical_sha256,
            "catalog_rows": self.catalog_rows,
            "selections": self.selections,
        }


@dataclass(frozen=True)
class WriteResult:
    """A completed write: the confined name, the stored digest and the counts."""

    path: str
    sha256: str
    changed: bool
    catalog_rows: int
    selections: int

    def to_json(self) -> dict:
        return {
            "schema": SCHEMA,
            "path": self.path,
            "sha256": self.sha256,
            "changed": self.changed,
            "catalog_rows": self.catalog_rows,
            "selections": self.selections,
        }


def _read_tracked(
    project_file: ProjectFile, menu_for: Optional[EffortMenuResolver] = None
) -> ProjectModels:
    data = _read_existing(project_file)
    if data is None:
        raise ProjectModelsError(CODE_MISSING, f"{project_file.relative} does not exist under {project_file.root}")
    document = validate_document(
        parse_yaml_document(data), project_root=str(project_file.root), menu_for=menu_for
    )
    return ProjectModels(project_file.relative, digest(data), document)


def read_project_models(
    *,
    project_root: Optional[str] = None,
    file: Optional[str] = None,
    menu_for: Optional[EffortMenuResolver] = None,
) -> ProjectModels:
    """Read, strictly parse and validate the stored file.

    Raises ``missing`` when it does not exist and ``invalid`` when it fails
    validation; a file that cannot be shown is never silently repaired. The
    personal layer is not read here; see :func:`read_resolved_models`.
    """
    return _read_tracked(resolve_project_file(project_root, file), menu_for)


def validate_project_models(
    document: Any,
    *,
    project_root: Optional[str] = None,
    file: Optional[str] = None,
    menu_for: Optional[EffortMenuResolver] = None,
) -> Validation:
    """Validate ``document`` and confine ``file``; nothing is read or written."""
    project_file = resolve_project_file(project_root, file)
    normalized = validate_document(document, project_root=str(project_file.root), menu_for=menu_for)
    return Validation(
        project_file.relative,
        digest(render_yaml(normalized)),
        len(normalized["catalog"]),
        len(normalized["selections"]),
    )


def _fsync_directory(path: Path) -> None:
    if os.name == "posix":
        directory = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _atomic_write(path: Path, data: bytes, mode: Optional[int]) -> None:
    """Write ``data`` to a same-directory temporary file, fsync it, and replace ``path``.

    A created file takes the mode the umask leaves; a replaced file keeps its mode.
    """
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise
    _fsync_directory(path.parent)


def _check_if_match(project_file: ProjectFile, existing: Optional[bytes], if_match: Optional[str]) -> None:
    """Refuse a write whose ``if_match`` digest does not match the stored bytes."""
    if if_match is None:
        return
    if not _SHA256.fullmatch(if_match):
        raise _invalid("if_match", "must be a lowercase 64-character SHA-256 hex digest")
    if existing is None:
        raise ProjectModelsError(
            CODE_STALE, f"{project_file.relative} does not exist, so it cannot match {if_match}"
        )
    if digest(existing) != if_match:
        raise ProjectModelsError(
            CODE_STALE, f"{project_file.relative} changed: stored digest is {digest(existing)}, not {if_match}"
        )


def write_project_models(
    document: Any,
    *,
    project_root: Optional[str] = None,
    file: Optional[str] = None,
    if_match: Optional[str] = None,
    menu_for: Optional[EffortMenuResolver] = None,
) -> WriteResult:
    """Validate ``document`` and store its canonical form atomically.

    Every refusal (path, symlink, validation, stale digest) happens before the
    file or its directory is touched. ``if_match`` is the digest a prior
    ``read_project_models`` returned; the file must exist and still hash to it.
    Without ``if_match`` the file is created or overwritten. A document whose
    canonical bytes equal the stored bytes is not rewritten (``changed`` False).
    """
    project_file = resolve_project_file(project_root, file)
    normalized = validate_document(document, project_root=str(project_file.root), menu_for=menu_for)
    data = render_yaml(normalized)
    existing = _read_existing(project_file)
    _check_if_match(project_file, existing, if_match)
    result = WriteResult(
        project_file.relative, digest(data), existing != data,
        len(normalized["catalog"]), len(normalized["selections"]),
    )
    if existing == data:
        return result
    mode: Optional[int] = None
    if existing is None:
        project_file.path.parent.mkdir(parents=True, exist_ok=True)
    else:
        mode = stat.S_IMODE(project_file.path.lstat().st_mode)
    _atomic_write(project_file.path, data, mode)
    return result


# ---------------------------------------------------------------------------
# The personal layer: overrides beside the tracked file
# ---------------------------------------------------------------------------
#
# ``<file-stem>.local.yaml`` (default ``.llm-kit/models.local.yaml``) holds
#
#     version: 1
#     selections:
#       draft.generate.primary: {model: luna, effort: low}
#
# and is meant to be untracked. This library never edits ``.gitignore``; that
# is the consumer's call. Each key in the local file wins over the same key in
# the tracked file's ``selections``; a key only the local file names is an
# override with no default. The local file is validated exactly as the tracked
# one is, against the TRACKED catalogue (it carries none of its own), and every
# error names the local file. An override equal to the tracked default is not
# stored: a write drops it, and a file left with no override is deleted.


def local_file_name(relative: str) -> str:
    """The root-relative POSIX name of the personal layer for the tracked file ``relative``."""
    path = PurePosixPath(relative)
    return path.with_name(path.stem + LOCAL_SUFFIX).as_posix()


def resolve_local_file(project_file: ProjectFile) -> ProjectFile:
    """The confined personal-layer file beside ``project_file`` (same confinement rules). Touches nothing."""
    return resolve_project_file(str(project_file.root), local_file_name(project_file.relative))


def _name_local(local_file: ProjectFile, exc: ProjectModelsError) -> ProjectModelsError:
    if exc.code != CODE_INVALID:
        return exc
    return ProjectModelsError(exc.code, f"{local_file.relative}: {exc}")


def validate_local_document(
    data: Any,
    tracked: ProjectModels,
    local_file: ProjectFile,
    *,
    menu_for: Optional[EffortMenuResolver] = None,
) -> dict:
    """Validate a personal-layer document against ``tracked``'s catalogue; return its normalized copy.

    Same model and effort checks as the tracked file's selections. A refusal
    is a ``ProjectModelsError`` (``invalid``) whose message starts with the
    local file's name.
    """
    try:
        _import_yaml()
        top = _mapping(data, "document", _LOCAL_TOP_KEYS)
        if "version" not in top:
            raise _invalid("version", "is required")
        version = top["version"]
        if not isinstance(version, int) or isinstance(version, bool) or version != FILE_VERSION:
            raise _invalid("version", f"must be {FILE_VERSION}, not {version!r}")
        _rows, menus, efforts_of = _validate_catalog(
            tracked.document["catalog"], _Endpoints(str(local_file.root)), menu_for or library_effort_menu
        )
        selections = _validate_selections(top.get("selections", {}), menus, efforts_of)
    except ProjectModelsError as exc:
        raise _name_local(local_file, exc) from None
    return {"version": FILE_VERSION, "selections": selections}


def render_local_yaml(document: Mapping) -> bytes:
    """The canonical bytes of an already-validated personal-layer document."""
    yaml = _import_yaml()
    dumper = _yaml_types()[1]
    text = yaml.dump(
        {"version": document["version"], "selections": document["selections"]},
        Dumper=dumper,
        sort_keys=False,
        default_flow_style=False,
        indent=2,
        allow_unicode=True,
        width=1_000_000,
    )
    return text.encode("utf-8")


def resolve_selections(
    defaults: Mapping[str, Mapping], overrides: Mapping[str, Mapping]
) -> "dict[str, dict]":
    """Per selection key: ``{"effective", "default", "source"}``, sorted by key.

    ``effective`` is the override when the local layer names the key with a
    value that differs from the default, else the default; ``default`` is the
    tracked selection, or ``None`` for a key only the local layer names;
    ``source`` is ``override`` when the effective value comes from the local
    layer, else ``default``. An entry equal to the default is no override.
    """
    out: dict[str, dict] = {}
    for key in sorted(set(defaults) | set(overrides)):
        default = dict(defaults[key]) if key in defaults else None
        local = dict(overrides[key]) if key in overrides else None
        if local is not None and local != default:
            out[key] = {"effective": local, "default": default, "source": SOURCE_OVERRIDE}
        else:
            out[key] = {"effective": default, "default": default, "source": SOURCE_DEFAULT}
    return out


@dataclass(frozen=True)
class LocalOverrides:
    """The personal layer as stored: its name, whether it exists, its digest and its overrides."""

    path: str
    present: bool
    sha256: Optional[str]
    selections: "dict[str, dict]"

    def to_json(self) -> dict:
        return {
            "path": self.path,
            "present": self.present,
            "sha256": self.sha256,
            "selections": self.selections,
        }


@dataclass(frozen=True)
class ResolvedModels:
    """The tracked file, the personal layer over it, and the per-selection resolution."""

    project: ProjectModels
    local: LocalOverrides
    selections: "dict[str, dict]"

    def to_json(self) -> dict:
        out = self.project.to_json()
        out["local"] = self.local.to_json()
        out["resolved_selections"] = self.selections
        return out


def _read_local(
    tracked: ProjectModels, local_file: ProjectFile, menu_for: Optional[EffortMenuResolver]
) -> LocalOverrides:
    data = _read_existing(local_file)
    if data is None:
        return LocalOverrides(local_file.relative, False, None, {})
    try:
        parsed = parse_yaml_document(data)
    except ProjectModelsError as exc:
        raise _name_local(local_file, exc) from None
    document = validate_local_document(parsed, tracked, local_file, menu_for=menu_for)
    return LocalOverrides(local_file.relative, True, digest(data), document["selections"])


def read_local_overrides(
    tracked: ProjectModels,
    *,
    project_root: Optional[str] = None,
    file: Optional[str] = None,
    menu_for: Optional[EffortMenuResolver] = None,
) -> LocalOverrides:
    """Read and validate the personal layer for ``tracked``; an absent file is no overrides."""
    local_file = resolve_local_file(resolve_project_file(project_root, file))
    return _read_local(tracked, local_file, menu_for)


def resolve_models(tracked: ProjectModels, local: LocalOverrides) -> ResolvedModels:
    """Layer ``local`` over ``tracked``."""
    return ResolvedModels(
        tracked, local, resolve_selections(tracked.document["selections"], local.selections)
    )


def read_resolved_models(
    *,
    project_root: Optional[str] = None,
    file: Optional[str] = None,
    menu_for: Optional[EffortMenuResolver] = None,
) -> ResolvedModels:
    """Read the tracked file and the personal layer over it, both validated."""
    project_file = resolve_project_file(project_root, file)
    tracked = _read_tracked(project_file, menu_for)
    local = _read_local(tracked, resolve_local_file(project_file), menu_for)
    return resolve_models(tracked, local)


@dataclass(frozen=True)
class LocalValidation:
    """A personal-layer document that passed validation, and the digest ``write`` would store.

    ``canonical_sha256`` is ``None`` when nothing would be stored (no override
    survives), in which case a write deletes the file.
    """

    path: str
    canonical_sha256: Optional[str]
    selections: int
    dropped_defaults: "list[str]"

    def to_json(self) -> dict:
        return {
            "schema": SCHEMA,
            "layer": LAYER_LOCAL,
            "valid": True,
            "path": self.path,
            "canonical_sha256": self.canonical_sha256,
            "selections": self.selections,
            "dropped_defaults": self.dropped_defaults,
        }


@dataclass(frozen=True)
class LocalWriteResult:
    """A completed personal-layer write or clear.

    ``sha256`` is the stored digest, ``None`` when no file is stored;
    ``deleted`` is true when the write removed the file because no override
    remained; ``dropped_defaults`` lists keys not stored because they equal the
    tracked default.
    """

    path: str
    sha256: Optional[str]
    changed: bool
    deleted: bool
    selections: int
    dropped_defaults: "list[str]"

    def to_json(self) -> dict:
        return {
            "schema": SCHEMA,
            "layer": LAYER_LOCAL,
            "path": self.path,
            "sha256": self.sha256,
            "changed": self.changed,
            "deleted": self.deleted,
            "selections": self.selections,
            "dropped_defaults": self.dropped_defaults,
        }


def _tidy_local(
    document: Any,
    project_root: Optional[str],
    file: Optional[str],
    menu_for: Optional[EffortMenuResolver],
) -> "tuple[ProjectFile, dict, dict, list[str]]":
    """``(local_file, kept, normalized, dropped)``: validated, with default-equal entries removed."""
    project_file = resolve_project_file(project_root, file)
    tracked = _read_tracked(project_file, menu_for)
    local_file = resolve_local_file(project_file)
    normalized = validate_local_document(document, tracked, local_file, menu_for=menu_for)
    defaults = tracked.document["selections"]
    kept = {k: v for k, v in normalized["selections"].items() if defaults.get(k) != v}
    dropped = sorted(set(normalized["selections"]) - set(kept))
    return local_file, {"version": FILE_VERSION, "selections": kept}, normalized, dropped


def validate_local_overrides(
    document: Any,
    *,
    project_root: Optional[str] = None,
    file: Optional[str] = None,
    menu_for: Optional[EffortMenuResolver] = None,
) -> LocalValidation:
    """Validate a personal-layer ``document`` against the tracked file; nothing is written."""
    local_file, kept, _normalized, dropped = _tidy_local(document, project_root, file, menu_for)
    stored = digest(render_local_yaml(kept)) if kept["selections"] else None
    return LocalValidation(local_file.relative, stored, len(kept["selections"]), dropped)


def write_local_overrides(
    document: Any,
    *,
    project_root: Optional[str] = None,
    file: Optional[str] = None,
    if_match: Optional[str] = None,
    menu_for: Optional[EffortMenuResolver] = None,
) -> LocalWriteResult:
    """Validate ``document`` and store the personal layer atomically, replacing it whole.

    Entries equal to the tracked default are dropped; when no override
    remains, the file is deleted (or stays absent). ``if_match`` is the
    ``sha256`` of the stored personal file, as for the tracked file. Every
    refusal happens before the file or its directory is touched.
    """
    local_file, kept, _normalized, dropped = _tidy_local(document, project_root, file, menu_for)
    existing = _read_existing(local_file)
    _check_if_match(local_file, existing, if_match)
    count = len(kept["selections"])
    if not count:
        if existing is None:
            return LocalWriteResult(local_file.relative, None, False, False, 0, dropped)
        local_file.path.unlink()
        _fsync_directory(local_file.path.parent)
        return LocalWriteResult(local_file.relative, None, True, True, 0, dropped)
    data = render_local_yaml(kept)
    result = LocalWriteResult(local_file.relative, digest(data), existing != data, False, count, dropped)
    if existing == data:
        return result
    mode: Optional[int] = None
    if existing is None:
        local_file.path.parent.mkdir(parents=True, exist_ok=True)
    else:
        mode = stat.S_IMODE(local_file.path.lstat().st_mode)
    _atomic_write(local_file.path, data, mode)
    return result


def set_local_override(
    key: str,
    model: str,
    effort: Optional[str] = None,
    *,
    project_root: Optional[str] = None,
    file: Optional[str] = None,
    if_match: Optional[str] = None,
    menu_for: Optional[EffortMenuResolver] = None,
) -> LocalWriteResult:
    """Set one override (``effort`` None omits the effort); equal to the default, it clears the key."""
    project_file = resolve_project_file(project_root, file)
    tracked = _read_tracked(project_file, menu_for)
    current = _read_local(tracked, resolve_local_file(project_file), menu_for).selections
    current[key] = {"model": model, "effort": effort}
    return write_local_overrides(
        {"version": FILE_VERSION, "selections": current},
        project_root=project_root, file=file, if_match=if_match, menu_for=menu_for,
    )


def clear_local_override(
    key: str,
    *,
    project_root: Optional[str] = None,
    file: Optional[str] = None,
    if_match: Optional[str] = None,
    menu_for: Optional[EffortMenuResolver] = None,
) -> LocalWriteResult:
    """Remove one override; the file is deleted when none remains.

    The stored file is read structurally, not validated whole, so a stale
    override (for example one naming a model the tracked catalogue dropped) can
    be cleared; the entries that remain are still validated before anything is
    written. A key the file does not hold changes nothing.
    """
    project_file = resolve_project_file(project_root, file)
    tracked = _read_tracked(project_file, menu_for)
    local_file = resolve_local_file(project_file)
    data = _read_existing(local_file)
    if data is None:
        _check_if_match(local_file, None, if_match)
        return LocalWriteResult(local_file.relative, None, False, False, 0, [])
    try:
        parsed = parse_yaml_document(data)
        if not isinstance(parsed, Mapping) or not isinstance(parsed.get("selections", {}), Mapping):
            raise _invalid("document", "must be a mapping with a selections mapping")
    except ProjectModelsError as exc:
        raise _name_local(local_file, exc) from None
    selections = dict(parsed.get("selections", {}))
    if key not in selections:
        _check_if_match(local_file, data, if_match)
        return LocalWriteResult(
            local_file.relative, digest(data), False, False, len(selections), []
        )
    del selections[key]
    return write_local_overrides(
        {"version": FILE_VERSION, "selections": selections},
        project_root=project_root, file=file, if_match=if_match, menu_for=menu_for,
    )
