"""Model registry + named-endpoint resolution.

Plugins do not hardcode model slugs. They name a model -- by registry alias, or
by the ``default`` / ``defaultCheap`` selectors -- and this module resolves it to
a concrete provider slug, reading a layered ``config.yaml``:

    shipped baseline (DEFAULT_MODEL_CONFIG, below)
      -> user    (~/.claude/plugins/data/plugins-kit/llm-scripting-kit/config.yaml)
        -> project, superseded location
           (<project_root>/.local-data/llm-scripting-kit/config.yaml)
          -> project, canonical
             (<project_root>/.local-data/plugins-kit/llm-scripting-kit/config.yaml)

The canonical project layer carries the ``plugins-kit`` marketplace segment,
matching both the user layer and the canonical project API-key path
(``constants.project_env_file``). The marketplace-less location is read at
lower precedence so a file placed there -- the shape the pre-0.6.6 key path
invited -- is not silently ignored.

The file layering + deep-merge is bootstrap's job (bootstrap_lib.config_resolve);
this module owns the schema: the model registry, the default/defaultCheap
selectors, and the named-endpoint map. If bootstrap_lib is unavailable the
shipped baseline is used on its own, so resolution always works.

Named endpoints
---------------
``endpoints:`` maps a name to an OpenAI-compatible endpoint. Each endpoint may
carry its own ``base_url``, ``key_env``, ``models`` / ``default`` /
``defaultCheap`` registry, and an ``account_check`` mode. ``default_endpoint``
names the endpoint used when a caller passes ``endpoint=None`` -- it defaults to
``openrouter``, whose built-in values reproduce today's behavior exactly. An
endpoint that omits ``models`` / ``default`` / ``defaultCheap`` inherits the
top-level ones, so pre-endpoints config.yaml files (top-level registry only)
keep working: their registry drives the default openrouter endpoint.

An endpoint declaring ``key_env: null`` is KEYLESS -- the norm for a locally
hosted OpenAI-compatible server. Omitting ``key_env`` is not the same thing and
still raises.

Endpoints may also come from the model-endpoints registry
(:mod:`llm_scripting_kit.model_endpoints`): a name no config layer declares is
looked up there by entry id. A config-declared endpoint of the same name wins.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Mapping, Optional

from .model_endpoints import (
    HARNESS_KIND,
    TRANSPORT_KIND,
    EndpointEntry,
    EndpointRegistry,
    harness_entry_message,
)

# The name of the endpoint used when a caller does not name one.
DEFAULT_ENDPOINT_NAME = "openrouter"

# Authoritative shipped baseline. Mirrored by default_config.yaml (which bootstrap
# uses to seed the editable user copy); a test asserts the two stay in sync.
#
# The ``openrouter`` endpoint deliberately omits ``models`` / ``default`` /
# ``defaultCheap`` so it inherits the top-level registry below -- that is what
# preserves back-compat for existing top-level-only config.yaml files.
DEFAULT_MODEL_CONFIG = {
    "default_endpoint": "openrouter",
    "endpoints": {
        "openrouter": {
            "base_url": "https://openrouter.ai/api/v1",
            "key_env": "OPENROUTER_API_KEY",
            "account_check": "openrouter",
        },
    },
    "models": {
        "qwen": {"slug": "qwen/qwen3-32b"},
        "gpt-mini": {"slug": "openai/gpt-4o-mini"},
        "gemini-lite": {"slug": "google/gemini-2.5-flash-lite"},
    },
    "default": "gpt-mini",
    "defaultCheap": "qwen",
}

# The plugin/marketplace identity under which the config.yaml lives.
CONFIG_PLUGIN = "llm-scripting-kit"
CONFIG_MARKETPLACE = "plugins-kit"
CONFIG_FILE = "config.yaml"


class ModelResolveError(Exception):
    """A model name could not be resolved to a provider slug."""


class EndpointResolveError(Exception):
    """A named endpoint could not be resolved from the config."""


class ModelDiscovery(dict[str, EndpointEntry]):
    """Merged model entries and non-fatal loader notes.

    The object is a mapping keyed by entry id for callers that only need the
    entries.  ``entries`` and ``notes`` make the two parts explicit for
    discovery consumers that also need to surface skipped declarations.
    """

    def __init__(
        self,
        entries: Optional[Mapping[str, EndpointEntry]] = None,
        *,
        notes: Optional[list[str]] = None,
    ) -> None:
        super().__init__(entries or {})
        self.notes = list(notes or [])

    @property
    def entries(self) -> "ModelDiscovery":
        """Return the id-keyed entries mapping."""
        return self

    @property
    def skipped(self) -> list[str]:
        """Compatibility alias for the non-fatal skip notes."""
        return self.notes


def load_model_config(*, project_root: Optional[str] = None) -> dict:
    """Resolve the effective model config: shipped baseline deep-merged with the
    user and (optional) project ``config.yaml`` layers.

    Falls back to the shipped baseline alone if the layers cannot be read --
    either because bootstrap_lib is unavailable, or because reading them raised
    (most commonly PyYAML missing from whatever interpreter is running us).
    Resolution degrades; it never raises. A caller that hard-fails here would
    take down key management along with model lookup -- which is how a missing
    PyYAML turned every `llm-scripting-kit` subcommand, including `set-key`,
    into a traceback with no way to recover.
    """
    base = copy.deepcopy(DEFAULT_MODEL_CONFIG)
    try:
        from bootstrap_lib.config_resolve import (
            ConfigError,
            resolve_config,
            standard_config_layers,
        )
        from bootstrap_lib.manifest_merge import deep_merge
    except ImportError:
        print(
            "llm-scripting-kit: bootstrap_lib unavailable; using the shipped model "
            "baseline (user/project config.yaml layers ignored)",
            file=sys.stderr,
        )
        return base

    layers = standard_config_layers(
        CONFIG_FILE,
        plugin=CONFIG_PLUGIN,
        marketplace=CONFIG_MARKETPLACE,
        project_root=project_root,
    )
    if project_root is not None:
        # Symmetry with the API key's superseded project location: a config.yaml
        # placed at <project>/.local-data/llm-scripting-kit/ (no marketplace
        # segment, by analogy with the pre-0.6.6 key path) is read too, at LOWER
        # precedence than the canonical marketplace-namespaced layer. Inserted
        # BEFORE the canonical project layer, which standard_config_layers put
        # last.
        layers.insert(-1, Path(project_root) / ".local-data" / CONFIG_PLUGIN / CONFIG_FILE)
    try:
        file_cfg = resolve_config(layers)
    except ConfigError as e:
        print(
            f"llm-scripting-kit: cannot read layered config ({e}); using the shipped "
            "model baseline (user/project config.yaml layers ignored)",
            file=sys.stderr,
        )
        return base
    return deep_merge(base, file_cfg)


def default_endpoint_name(config: dict) -> str:
    """The endpoint used when a caller does not name one."""
    return config.get("default_endpoint") or DEFAULT_ENDPOINT_NAME


def _registry_entry_ids() -> set:
    """Entry ids declared in the model-endpoints registry, for diagnostics.

    Best-effort and never raises: this exists only to make an error message
    complete, so a registry that is absent or unreadable contributes nothing
    rather than replacing the caller's real error with a second one.
    """
    try:
        from .model_endpoints import load_endpoint_registry  # noqa: PLC0415

        return set(load_endpoint_registry().entries)
    except Exception:  # noqa: BLE001 -- a diagnostic must not raise
        return set()


def _config_entry_kind(ep_name: str, ep: Mapping[str, object]) -> Optional[str]:
    """Classify one layered-config endpoint by its mutually exclusive address."""
    has_base_url = "base_url" in ep
    has_harness = "harness" in ep
    if has_base_url and has_harness:
        raise EndpointResolveError(
            f"endpoint '{ep_name}' declares both 'base_url' and 'harness'; "
            "the entry kind is contradictory"
        )
    if has_harness:
        return HARNESS_KIND
    if has_base_url:
        return TRANSPORT_KIND
    return None


def _config_required_str(
    ep_name: str,
    ep: Mapping[str, object],
    *,
    kind: str,
    key: str,
) -> str:
    value = ep.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EndpointResolveError(
            f"endpoint '{ep_name}' is a {kind} entry and has no '{key}' "
            "(a non-empty string is required)"
        )
    return value.strip()


def _config_optional_str(
    ep_name: str,
    ep: Mapping[str, object],
    *,
    kind: str,
    key: str,
) -> Optional[str]:
    value = ep.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise EndpointResolveError(
            f"endpoint '{ep_name}' is a {kind} entry and has a non-string "
            f"'{key}' ({value!r})"
        )
    return value.strip()


def _config_model_entry(
    ep_name: str,
    ep: Mapping[str, object],
) -> Optional[EndpointEntry]:
    """Turn a direct config model declaration into the common entry shape.

    The existing ``openrouter`` config is an endpoint wrapper around the
    top-level model alias map and deliberately has no direct ``model`` field.
    It remains valid for resolution but is not itself a model definition for
    merged discovery, so it returns None here.
    """
    kind = _config_entry_kind(ep_name, ep)
    if kind is None:
        return None

    if kind == HARNESS_KIND:
        return EndpointEntry(
            id=ep_name,
            base_url=None,
            model=_config_required_str(ep_name, ep, kind=kind, key="model"),
            name=_config_optional_str(ep_name, ep, kind=kind, key="name"),
            kind=kind,
            harness=_config_required_str(ep_name, ep, kind=kind, key="harness"),
            effort=_config_optional_str(ep_name, ep, kind=kind, key="effort"),
        )

    if "model" not in ep:
        return None
    return EndpointEntry(
        id=ep_name,
        base_url=_config_required_str(ep_name, ep, kind=kind, key="base_url"),
        model=_config_required_str(ep_name, ep, kind=kind, key="model"),
        name=_config_optional_str(ep_name, ep, kind=kind, key="name"),
        reasoning_effort=_config_optional_str(
            ep_name, ep, kind=kind, key="reasoning_effort"
        ),
        key_env=_config_optional_str(ep_name, ep, kind=kind, key="key_env"),
        kind=kind,
    )


def _registry_endpoint(ep_name: str) -> Optional[dict]:
    """Resolve ``ep_name`` as a model-endpoints registry entry, or None.

    Returns an endpoint dict in the shape ``resolve_endpoint`` returns, with two
    additive keys existing callers ignore: ``request_defaults`` (the entry's
    per-request defaults, e.g. its ``reasoning_effort``) and ``context_window``.

    A registry that exists but cannot be read raises EndpointResolveError with
    the parse detail -- a present-but-broken registry must never read as an
    absent one. No registry at all simply returns None, and the caller's
    unknown-endpoint error stands unchanged.
    """
    from .model_endpoints import (  # noqa: PLC0415 -- avoids an import cycle
        EndpointRegistryError,
        load_endpoint_registry,
    )

    try:
        registry = load_endpoint_registry()
    except EndpointRegistryError as e:
        raise EndpointResolveError(str(e)) from e
    entry = registry.entries.get(ep_name)
    if entry is None:
        return None
    if entry.kind == HARNESS_KIND:
        raise EndpointResolveError(
            harness_entry_message(ep_name, entry.harness)
        )
    if entry.kind != TRANSPORT_KIND or not entry.base_url:
        raise EndpointResolveError(
            f"endpoint '{ep_name}' has an invalid {entry.kind} entry without a "
            "transport base_url"
        )
    return {
        "name": entry.id,
        "base_url": entry.base_url,
        "key_env": entry.key_env,  # None unless the entry declares one
        "models": {entry.id: {"slug": entry.model}},
        "default": entry.id,
        "defaultCheap": entry.id,
        "account_check": "models-probe",
        "request_defaults": (
            {"reasoning_effort": entry.reasoning_effort} if entry.reasoning_effort else {}
        ),
        "context_window": entry.context_window,
    }


def resolve_endpoint(
    name: Optional[str] = None,
    *,
    config: Optional[dict] = None,
    project_root: Optional[str] = None,
) -> dict:
    """Resolve a named endpoint to its effective settings.

    Returns a dict with keys ``name``, ``base_url``, ``key_env``, ``models``,
    ``default``, ``defaultCheap``, and ``account_check``. Fields the endpoint
    omits inherit the top-level ``models`` / ``default`` / ``defaultCheap``, so a
    pre-endpoints config (top-level registry only) resolves the default
    ``openrouter`` endpoint from its constants + that registry.

    ``name`` None means the config's ``default_endpoint``. Raises
    EndpointResolveError for an unknown endpoint or one missing a base_url/key_env.

    An endpoint declaring ``key_env: null`` explicitly resolves KEYLESS
    (``key_env`` is None in the returned dict); an endpoint that merely OMITS
    ``key_env`` still raises, so a dropped line cannot silently disable auth.

    A name that no config layer declares is looked up in the model-endpoints
    registry (:mod:`llm_scripting_kit.model_endpoints`), whose entry ids are
    endpoint names. Such an endpoint resolves keyless unless its entry declares
    a ``key_env``, and carries two additive keys -- ``request_defaults`` and
    ``context_window`` -- that config-declared endpoints do not.
    """
    cfg = config if config is not None else load_model_config(project_root=project_root)
    ep_name = name or default_endpoint_name(cfg)
    endpoints = cfg.get("endpoints") or {}
    if not isinstance(endpoints, dict):
        raise EndpointResolveError("config 'endpoints' is not a mapping")
    ep = endpoints.get(ep_name)
    if ep is None:
        # Back-compat: a config with no endpoints map at all still resolves the
        # built-in default endpoint from constants + the top-level registry.
        if ep_name == DEFAULT_ENDPOINT_NAME:
            ep = {}
        else:
            # Not in any config layer: consult the model-endpoints registry,
            # whose entry ids ARE endpoint names. Checked here -- after the
            # config map -- so a config-declared endpoint of the same name
            # shadows a registry entry (an explicit config edit is a
            # deliberate override).
            injected = _registry_endpoint(ep_name)
            if injected is not None:
                return injected
            # Name BOTH namespaces. An endpoint name may come from the config
            # map or from the model-endpoints registry -- the lookup above just
            # tried both -- so listing only the config's is misleading in the
            # exact case the reader is in: a typo'd registry entry id was told
            # the known set is "openrouter", pointing the fix at the wrong file.
            # Same config-vs-registry confusion that caused a live defect in
            # content-pipeline-kit's model-endpoint backend.
            known = ", ".join(sorted(set(endpoints) | _registry_entry_ids()))
            known = known or "<none>"
            raise EndpointResolveError(
                f"unknown endpoint '{ep_name}' (known: {known})"
            )
    if not isinstance(ep, dict):
        raise EndpointResolveError(f"endpoint '{ep_name}' is not a mapping")

    entry_kind = _config_entry_kind(ep_name, ep)
    if entry_kind == HARNESS_KIND:
        harness = _config_required_str(ep_name, ep, kind=entry_kind, key="harness")
        _config_required_str(ep_name, ep, kind=entry_kind, key="model")
        raise EndpointResolveError(harness_entry_message(ep_name, harness))

    is_default = ep_name == DEFAULT_ENDPOINT_NAME
    base_url = ep.get("base_url") or (
        DEFAULT_MODEL_CONFIG["endpoints"]["openrouter"]["base_url"] if is_default else None
    )
    if not base_url:
        raise EndpointResolveError(f"endpoint '{ep_name}' has no 'base_url'")

    # Keyless endpoints are first-class, but only when declared DELIBERATELY:
    # an explicit `key_env: null` resolves keyless, while an OMITTED key_env
    # still raises exactly as before. The asymmetry is typo protection -- a
    # dropped or misspelled key_env line must not silently become "no auth".
    if "key_env" in ep and ep["key_env"] is None:
        key_env = None
    else:
        key_env = ep.get("key_env") or (
            DEFAULT_MODEL_CONFIG["endpoints"]["openrouter"]["key_env"] if is_default else None
        )
        if not key_env:
            raise EndpointResolveError(
                f"endpoint '{ep_name}' has no 'key_env' "
                f"(declare `key_env: null` explicitly for a keyless endpoint)"
            )

    # A DIRECT model entry (the config-store counterpart to a registry entry)
    # supplies its own one-model alias map and its own selectors. All three
    # decisions below hang off this single predicate on purpose: gating the map
    # on one condition and the selectors on another lets an endpoint carrying
    # BOTH `model` and a nested `models:` alias map resolve with selectors
    # naming an id that is absent from the map it actually chose, which
    # resolve_model() can only report as an unknown model.
    is_direct_model = "model" in ep and not isinstance(ep.get("models"), dict)
    if is_direct_model:
        models = {ep_name: {"slug": ep["model"]}}
    else:
        models = ep["models"] if isinstance(ep.get("models"), dict) else (cfg.get("models") or {})
    default_sel = ep.get("default") or cfg.get("default")
    if is_direct_model and not ep.get("default"):
        default_sel = ep_name
    default_cheap_sel = ep.get("defaultCheap") or cfg.get("defaultCheap")
    if is_direct_model and not ep.get("defaultCheap"):
        default_cheap_sel = ep_name
    account_check = ep.get("account_check")
    if account_check is None:
        account_check = "openrouter" if is_default else "none"

    return {
        "name": ep_name,
        "base_url": base_url,
        "key_env": key_env,
        "models": models,
        "default": default_sel,
        "defaultCheap": default_cheap_sel,
        "account_check": account_check,
    }


def discover_model_entries(
    *,
    config: Optional[dict] = None,
    project_root: Optional[str] = None,
    registry: Optional[EndpointRegistry] = None,
) -> ModelDiscovery:
    """Return model entries from layered config plus the user registry.

    Entries declared by the layered config are applied after registry entries,
    preserving the resolution rule that an explicit config entry shadows the
    same id from the model-endpoints registry.  The legacy endpoint wrapper
    form (for example the built-in ``openrouter`` record with a nested model
    alias map) remains available to ``resolve_endpoint`` but is not a direct
    model definition and is therefore omitted here.

    Registry skip notes are carried through unchanged.  Invalid or unknown
    direct config declarations are also recorded as notes because discovery is
    an inspection API and must not make a skipped entry look absent without an
    explanation.
    """
    cfg = config if config is not None else load_model_config(project_root=project_root)
    if registry is None:
        from .model_endpoints import load_endpoint_registry  # noqa: PLC0415

        registry = load_endpoint_registry()

    entries = dict(registry.entries)
    notes = list(getattr(registry, "notes", []))
    endpoints = cfg.get("endpoints") or {}
    if not isinstance(endpoints, dict):
        notes.append("layered config 'endpoints' skipped; it is not a mapping")
        return ModelDiscovery(entries, notes=notes)

    config_ids: set[str] = set()
    config_entries: dict[str, EndpointEntry] = {}
    for entry_id, raw in endpoints.items():
        key = str(entry_id)
        if not isinstance(raw, dict):
            notes.append(f"layered config endpoint '{key}' skipped; it is not a mapping")
            continue
        try:
            kind = _config_entry_kind(key, raw)
            if kind is None:
                notes.append(
                    f"layered config endpoint '{key}' skipped; unknown kind "
                    "(neither 'base_url' nor 'harness' is present)"
                )
                continue
            config_ids.add(key)
            entry = _config_model_entry(key, raw)
        except EndpointResolveError as exc:
            notes.append(f"layered config endpoint '{key}' skipped: {exc}")
            continue
        if entry is not None:
            config_entries[key] = entry

    # A recognized config endpoint remains the deliberate shadow even when it
    # is an old-style wrapper with no direct model or a malformed declaration;
    # silently falling back to a same-id registry entry would defeat the
    # config-vs-registry override rule.
    for key in config_ids:
        entries.pop(key, None)
    entries.update(config_entries)
    return ModelDiscovery(entries, notes=notes)


def resolve_model(
    name: Optional[str] = None,
    *,
    cheap: bool = False,
    project_root: Optional[str] = None,
    config: Optional[dict] = None,
    endpoint: Optional[str] = None,
) -> str:
    """Resolve a model selection to a concrete provider slug.

    - ``name`` given: a registry alias (looked up in the endpoint's ``models``)
      or, if it looks like a slug (contains ``/``), returned as-is.
    - ``name`` omitted: use the ``defaultCheap`` selector when ``cheap`` is True,
      else ``default`` -- itself a registry alias or a raw slug.

    ``endpoint`` None uses the config's default endpoint (``openrouter``); all
    existing no-arg / endpoint-less calls resolve exactly as before. Pass
    ``config`` to resolve against an already-loaded config (skips file I/O).
    Raises ModelResolveError if the name/selector cannot be resolved.
    """
    cfg = config if config is not None else load_model_config(project_root=project_root)
    ep = resolve_endpoint(endpoint, config=cfg)
    models = ep["models"] or {}

    def _slug_for(alias_or_slug: str, what: str) -> str:
        if alias_or_slug in models:
            entry = models[alias_or_slug]
            slug = entry.get("slug") if isinstance(entry, dict) else None
            if not slug:
                raise ModelResolveError(
                    f"model alias '{alias_or_slug}' has no 'slug' in the registry"
                )
            return slug
        if "/" in alias_or_slug:  # a raw provider slug, used directly
            return alias_or_slug
        raise ModelResolveError(
            f"{what} '{alias_or_slug}' is not a known model alias or a provider slug"
        )

    if name:
        return _slug_for(name, "model")

    selector = "defaultCheap" if cheap else "default"
    alias = ep.get(selector)
    if not alias:
        raise ModelResolveError(
            f"no '{selector}' configured for endpoint '{ep['name']}'"
        )
    return _slug_for(alias, selector)
