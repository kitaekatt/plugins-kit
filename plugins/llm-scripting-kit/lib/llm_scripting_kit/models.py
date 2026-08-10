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
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Optional

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
    """
    cfg = config if config is not None else load_model_config(project_root=project_root)
    ep_name = name or default_endpoint_name(cfg)
    endpoints = cfg.get("endpoints") or {}
    ep = endpoints.get(ep_name)
    if ep is None:
        # Back-compat: a config with no endpoints map at all still resolves the
        # built-in default endpoint from constants + the top-level registry.
        if ep_name == DEFAULT_ENDPOINT_NAME:
            ep = {}
        else:
            known = ", ".join(sorted(endpoints)) or "<none>"
            raise EndpointResolveError(
                f"unknown endpoint '{ep_name}' (known: {known})"
            )
    if not isinstance(ep, dict):
        raise EndpointResolveError(f"endpoint '{ep_name}' is not a mapping")

    is_default = ep_name == DEFAULT_ENDPOINT_NAME
    base_url = ep.get("base_url") or (
        DEFAULT_MODEL_CONFIG["endpoints"]["openrouter"]["base_url"] if is_default else None
    )
    key_env = ep.get("key_env") or (
        DEFAULT_MODEL_CONFIG["endpoints"]["openrouter"]["key_env"] if is_default else None
    )
    if not base_url:
        raise EndpointResolveError(f"endpoint '{ep_name}' has no 'base_url'")
    if not key_env:
        raise EndpointResolveError(f"endpoint '{ep_name}' has no 'key_env'")

    models = ep["models"] if isinstance(ep.get("models"), dict) else (cfg.get("models") or {})
    default_sel = ep.get("default") or cfg.get("default")
    default_cheap_sel = ep.get("defaultCheap") or cfg.get("defaultCheap")
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
