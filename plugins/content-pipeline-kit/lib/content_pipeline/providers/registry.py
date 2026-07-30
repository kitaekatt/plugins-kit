"""Tiered ``name -> (callable, tier)`` context-provider registry.

Generalizes loc's ``providers/_framework.py`` to a domain-free registry. A
*provider* is a small callable that produces one piece of context a prompt
assembles from; registering it under a name lets ``assembly`` reference it by
label rather than importing the function directly, which is what lets
``assembly`` be the single owner of how those labels compose.

Two tiers, matching the source split:

- :data:`SOURCE_TIER` (``"source"``) -- unit-agnostic: the same value no
  matter which generation unit is running (a glossary slice, config metadata).
- :data:`GENERATION_TIER` (``"generation"``) -- parameterized per variant
  (per-language, per-target): the output depends on the generation parameters,
  which are forwarded as extra positional args at invocation.

The registry is duck-typed (providers take/return ``Any``; ``run_tier`` asserts
each returns a ``dict``) and deterministically ordered (sorted by name), so two
build sites assembling the same tier get byte-stable output. This module is
stdlib-only -- providers may import nothing beyond stdlib and
``freshness.hashing``, and this registry imports neither ``store`` nor ``vcs``.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

Provider = Callable[..., dict]

SOURCE_TIER = "source"
GENERATION_TIER = "generation"
_VALID_TIERS: Tuple[str, ...] = (SOURCE_TIER, GENERATION_TIER)


class ProviderError(Exception):
    """Base class for provider registry / invocation errors."""


class UnknownProviderError(ProviderError):
    """Raised when a lookup names an unregistered provider."""


class ProviderAlreadyRegisteredError(ProviderError):
    """Raised when :func:`register` is called twice with the same name and
    ``replace=False``."""


# name -> (callable, tier)
_REGISTRY: Dict[str, Tuple[Provider, str]] = {}


def register(
    name: str,
    fn: Provider,
    *,
    tier: str,
    replace: bool = False,
) -> None:
    """Register ``fn`` under ``name`` in ``tier``.

    ``tier`` must be :data:`SOURCE_TIER` or :data:`GENERATION_TIER`. Raises
    :class:`ProviderAlreadyRegisteredError` when ``name`` is already registered
    and ``replace`` is False; ``replace=True`` overwrites.
    """
    if not isinstance(name, str) or not name:
        raise ProviderError(f"provider name must be a non-empty string, got {name!r}")
    if not callable(fn):
        raise ProviderError(
            f"provider {name!r} must be callable, got {type(fn).__name__}"
        )
    if tier not in _VALID_TIERS:
        raise ProviderError(
            f"provider {name!r} has invalid tier {tier!r}; expected one of {_VALID_TIERS}"
        )
    if name in _REGISTRY and not replace:
        raise ProviderAlreadyRegisteredError(f"provider {name!r} is already registered")
    _REGISTRY[name] = (fn, tier)


def provider(name: str, *, tier: str, replace: bool = False) -> Callable[[Provider], Provider]:
    """Decorator form of :func:`register` for self-registration at import.

    ``@provider("glossary", tier=SOURCE_TIER)`` registers the decorated
    callable and returns it unchanged, so the function stays directly callable.
    """

    def _decorate(fn: Provider) -> Provider:
        register(name, fn, tier=tier, replace=replace)
        return fn

    return _decorate


def resolve(name: str) -> Provider:
    """Return the callable registered under ``name`` (raises if unknown)."""
    try:
        return _REGISTRY[name][0]
    except KeyError:
        raise UnknownProviderError(f"no provider registered under {name!r}") from None


def get_tier(name: str) -> str:
    """Return the tier registered under ``name`` (raises if unknown)."""
    try:
        return _REGISTRY[name][1]
    except KeyError:
        raise UnknownProviderError(f"no provider registered under {name!r}") from None


def registered_names(*, tier: str | None = None) -> Tuple[str, ...]:
    """Return registered provider names, sorted, optionally filtered by tier.

    A snapshot: mutating the registry afterwards does not affect a returned
    tuple, and the caller cannot mutate the registry through it.
    """
    if tier is None:
        return tuple(sorted(_REGISTRY))
    return tuple(sorted(n for n, (_, t) in _REGISTRY.items() if t == tier))


def tiers() -> Dict[str, str]:
    """Return a fresh ``{name: tier}`` snapshot, sorted by name."""
    return {name: _REGISTRY[name][1] for name in sorted(_REGISTRY)}


def unregister(name: str) -> None:
    """Remove a registration (raises if unknown). Mainly for test teardown."""
    try:
        del _REGISTRY[name]
    except KeyError:
        raise UnknownProviderError(f"no provider registered under {name!r}") from None


def clear() -> None:
    """Remove every registration. Test-teardown helper."""
    _REGISTRY.clear()


def invoke(name: str, *args: Any, expect_tier: Optional[str] = None) -> dict:
    """Look up ``name`` and call it with ``*args``; assert a ``dict`` result.

    Source-tier providers are typically called ``invoke(name, source, item)``;
    generation-tier providers ``invoke(name, source, item, variant)`` -- the
    registry does not enforce arity, it forwards whatever the caller passes.

    ``expect_tier`` opts into a tier check the variadic signature otherwise
    cannot make. A system with per-tier entry points (``invoke_source`` /
    ``invoke_generation``) gets a mismatch -- wiring a source provider into a
    generation call site -- rejected by the signature itself; collapsing those
    into one variadic ``invoke`` gives that up, and the failure it used to
    catch degrades into a confusing arity or KeyError deep inside the
    provider. Passing ``expect_tier`` restores the check at the call site,
    which is the only place that knows which tier it meant.

    Optional by design: the generic path stays tier-agnostic, and a caller
    that has no per-tier intent should omit it rather than assert a tier it
    does not care about.
    """
    if expect_tier is not None:
        if expect_tier not in _VALID_TIERS:
            raise ProviderError(
                f"invalid expect_tier {expect_tier!r}; expected one of {_VALID_TIERS}"
            )
        actual = get_tier(name)
        if actual != expect_tier:
            raise ProviderError(
                f"provider {name!r} is registered as tier {actual!r}, "
                f"but the call site expected {expect_tier!r}"
            )
    fn = resolve(name)
    result = fn(*args)
    if not isinstance(result, dict):
        raise ProviderError(
            f"provider {name!r} must return a dict, got {type(result).__name__}"
        )
    return result


def run_tier(tier: str, *args: Any) -> Dict[str, dict]:
    """Invoke every provider of ``tier`` with ``*args``; assemble a brief map.

    Returns an ordered ``{provider_name: output}`` mapping (deterministic --
    providers run in sorted-name order) -- the brief slice a prompt assembler
    consumes. Every provider of the tier receives the same ``*args`` (the unit
    context for source tier; the unit context plus variant for generation
    tier). Raises :class:`ProviderError` if any provider returns a non-dict.
    """
    if tier not in _VALID_TIERS:
        raise ProviderError(f"invalid tier {tier!r}; expected one of {_VALID_TIERS}")
    out: Dict[str, dict] = {}
    for name in registered_names(tier=tier):
        out[name] = invoke(name, *args)
    return out


__all__ = [
    "Provider",
    "SOURCE_TIER",
    "GENERATION_TIER",
    "ProviderError",
    "UnknownProviderError",
    "ProviderAlreadyRegisteredError",
    "register",
    "provider",
    "resolve",
    "get_tier",
    "registered_names",
    "tiers",
    "unregister",
    "clear",
    "invoke",
    "run_tier",
]
