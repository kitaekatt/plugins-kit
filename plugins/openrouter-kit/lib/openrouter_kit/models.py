"""OpenRouter model registry resolution.

Plugins do not hardcode model slugs. They name a model -- by registry alias, or
by the ``default`` / ``defaultCheap`` selectors -- and this module resolves it to
a concrete OpenRouter slug, reading a layered ``config.yaml`` owned by
openrouter-kit:

    shipped baseline (DEFAULT_MODEL_CONFIG, below)
      -> user    (~/.claude/plugins/data/plugins-kit/openrouter-kit/config.yaml)
        -> project (<project_root>/.local-data/plugins-kit/openrouter-kit/config.yaml)

The file layering + deep-merge is bootstrap's job (bootstrap_lib.config_resolve);
this module owns only the OpenRouter-specific schema (the model registry and the
default/defaultCheap selectors). If bootstrap_lib is unavailable the shipped
baseline is used on its own, so resolution always works.

Any plugin that makes OpenRouter calls reads the SAME owner file, so a single
project override changes the model for all of them at once.
"""

from __future__ import annotations

import copy
import sys
from typing import Optional

# Authoritative shipped baseline. Mirrored by default_config.yaml (which bootstrap
# uses to seed the editable user copy); a test asserts the two stay in sync.
DEFAULT_MODEL_CONFIG = {
    "models": {
        "qwen": {"slug": "qwen/qwen3-32b"},
        "gpt-mini": {"slug": "openai/gpt-4o-mini"},
        "gemini-lite": {"slug": "google/gemini-2.5-flash-lite"},
    },
    "default": "gpt-mini",
    "defaultCheap": "qwen",
}

# The plugin/marketplace identity under which the config.yaml lives.
CONFIG_PLUGIN = "openrouter-kit"
CONFIG_MARKETPLACE = "plugins-kit"
CONFIG_FILE = "config.yaml"


class ModelResolveError(Exception):
    """A model name could not be resolved to an OpenRouter slug."""


def load_model_config(*, project_root: Optional[str] = None) -> dict:
    """Resolve the effective model config: shipped baseline deep-merged with the
    user and (optional) project ``config.yaml`` layers.

    Falls back to the shipped baseline alone if bootstrap_lib is unavailable.
    """
    base = copy.deepcopy(DEFAULT_MODEL_CONFIG)
    try:
        from bootstrap_lib.config_resolve import resolve_config, standard_config_layers
        from bootstrap_lib.manifest_merge import _deep_merge_dicts
    except ImportError:
        print(
            "openrouter-kit: bootstrap_lib unavailable; using the shipped model "
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
    file_cfg = resolve_config(layers)
    return _deep_merge_dicts(base, file_cfg)


def resolve_model(
    name: Optional[str] = None,
    *,
    cheap: bool = False,
    project_root: Optional[str] = None,
    config: Optional[dict] = None,
) -> str:
    """Resolve a model selection to a concrete OpenRouter slug.

    - ``name`` given: a registry alias (looked up in ``models``) or, if it looks
      like a slug (contains ``/``), returned as-is.
    - ``name`` omitted: use the ``defaultCheap`` selector when ``cheap`` is True,
      else ``default`` -- itself a registry alias or a raw slug.

    Pass ``config`` to resolve against an already-loaded config (skips file I/O).
    Raises ModelResolveError if the name/selector cannot be resolved.
    """
    cfg = config if config is not None else load_model_config(project_root=project_root)
    models = cfg.get("models") or {}

    def _slug_for(alias_or_slug: str, what: str) -> str:
        if alias_or_slug in models:
            entry = models[alias_or_slug]
            slug = entry.get("slug") if isinstance(entry, dict) else None
            if not slug:
                raise ModelResolveError(
                    f"model alias '{alias_or_slug}' has no 'slug' in the registry"
                )
            return slug
        if "/" in alias_or_slug:  # a raw OpenRouter slug, used directly
            return alias_or_slug
        raise ModelResolveError(
            f"{what} '{alias_or_slug}' is not a known model alias or an OpenRouter slug"
        )

    if name:
        return _slug_for(name, "model")

    selector = "defaultCheap" if cheap else "default"
    alias = cfg.get(selector)
    if not alias:
        raise ModelResolveError(f"no '{selector}' configured in the OpenRouter model config")
    return _slug_for(alias, selector)
