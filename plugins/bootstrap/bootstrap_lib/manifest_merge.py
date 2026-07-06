"""Deep-merge utilities for bootstrap and env manifest dicts."""


# Sections whose entries are objects keyed by an identity field.
# Maps section name → identity key used for deduplication.
# A value of None means the section uses a composite key (see _COMPOSITE_FNS).
_IDENTITY_KEYS = {
    "plugins": "ref",
    "marketplaces": "name",
    "tools": "name",
    "env_vars": "name",
    "fonts": "name",
    "json_entries": "file",
    "ini_settings": None,  # composite key: file + section
    "pypi_packages": "package",
    "shared_libs": "name",
}

# Sections that are plain string lists (unioned, deduplicated, order preserved).
_STRING_LIST_KEYS = {"path_entries", "shared_lib_imports"}

# env.json identity keys (bootstrap-env-refactor spec 4.1: same merge
# discipline as bootstrap.json, identity-keyed sections). `machines` is a
# plain dict keyed by hostname, so the generic dict deep-merge covers it.
_ENV_IDENTITY_KEYS = {
    "symlinks": "name",
    "shell_rc": "name",
    "macos_defaults": None,  # composite key: domain + key
    "macos_hotkeys": "id",
    "login_items": "name",
    "env_checks": "name",
}

_ENV_STRING_LIST_KEYS = frozenset()


def _ini_key(entry):
    """Composite identity key for ini_settings entries."""
    return (entry.get("file", ""), entry.get("section", ""))


def _defaults_key(entry):
    """Composite identity key for macos_defaults entries."""
    return (entry.get("domain", ""), entry.get("key", ""))


# Section name → composite key fn, for _IDENTITY_KEYS/_ENV_IDENTITY_KEYS
# entries whose value is None.
_COMPOSITE_FNS = {
    "ini_settings": _ini_key,
    "macos_defaults": _defaults_key,
}


def _merge_arrays(base_list, override_list, identity_key=None, composite_fn=None):
    """Union two lists of dicts by identity key.

    Entries with the same identity are merged (override wins for conflicting
    fields). New entries from override are appended.

    Args:
        base_list: Lower-priority entries.
        override_list: Higher-priority entries.
        identity_key: String key used to identify entries (e.g. "name", "ref").
        composite_fn: Callable returning a hashable key from an entry dict.
                       Used when identity requires multiple fields (ini_settings).
                       Mutually exclusive with identity_key.
    """
    if not base_list:
        return list(override_list) if override_list else []
    if not override_list:
        return list(base_list)

    key_fn = composite_fn if composite_fn else (lambda e: e.get(identity_key))

    # Build ordered index of base entries. Same-identity entries are
    # deep-merged (not shallow-updated) so users can override a single
    # nested key (e.g. tools[name=jq].download[macos-arm64].url) without
    # blowing away sibling keys. Override wins for scalar conflicts;
    # nested dicts merge recursively.
    merged = []  # list of (key, dict) tuples to preserve order
    index = {}   # key -> position in `merged`

    def _upsert(entry):
        k = key_fn(entry)
        if k in index:
            pos = index[k]
            merged[pos] = (k, _deep_merge_dicts(merged[pos][1], entry))
        else:
            index[k] = len(merged)
            merged.append((k, dict(entry)))

    for entry in base_list:
        _upsert(entry)
    for entry in override_list:
        _upsert(entry)

    return [v for _, v in merged]


def _deep_merge_dicts(base, override):
    """Recursively merge two dicts. Override wins for scalar conflicts."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge_dicts(result[key], val)
        else:
            result[key] = val
    return result


def deep_merge(base, override):
    """Recursively merge two plain dicts; override wins for scalar conflicts.

    The supported public entry point for generic dict deep-merging (other
    plugins import this — e.g. openrouter-kit's model-config layering). For
    bootstrap *manifest* dicts use merge_manifests, which adds the
    identity-keyed array semantics.
    """
    return _deep_merge_dicts(base, override)


def merge_manifests(base, override):
    """Deep-merge two bootstrap manifest dicts. Override wins for conflicts.

    Arrays of objects with identity keys (ref, name) are unioned —
    entries with the same identity are merged, new entries are appended.
    path_entries are unioned as simple string lists (deduplicated).
    Objects are deep-merged. Scalars from override win.

    Note: an explicit JSON ``null`` is treated as ABSENT, not as a value —
    a higher-priority layer cannot null-out a key declared by a lower layer
    (it can only override it with a non-null value).

    Args:
        base: Lower-priority manifest dict.
        override: Higher-priority manifest dict.

    Returns:
        New merged dict (inputs are not mutated).
    """
    return _merge_layers(base, override, _IDENTITY_KEYS, _STRING_LIST_KEYS)


def merge_env_manifests(base, override):
    """Deep-merge two env.json manifest dicts. Override wins for conflicts.

    Same merge discipline as :func:`merge_manifests` (identity-keyed array
    union, dict deep-merge, null-is-absent), with env.json's own identity
    keys (``_ENV_IDENTITY_KEYS``). The ``machines`` registry is a plain dict
    keyed by hostname, so per-machine fields deep-merge across layers.
    """
    return _merge_layers(base, override, _ENV_IDENTITY_KEYS, _ENV_STRING_LIST_KEYS)


def _merge_layers(base, override, identity_keys, string_list_keys):
    """Shared layer-merge machinery behind merge_manifests/merge_env_manifests."""
    if not base:
        return dict(override) if override else {}
    if not override:
        return dict(base)

    result = {}
    all_keys = set(base) | set(override)

    for key in all_keys:
        base_val = base.get(key)
        over_val = override.get(key)

        # Only in one side
        if base_val is None:
            result[key] = over_val
            continue
        if over_val is None:
            result[key] = base_val
            continue

        # Plain string-list sections: union (deduplicated, order preserved)
        if key in string_list_keys:
            seen = set()
            merged = []
            for item in (base_val or []) + (over_val or []):
                if item not in seen:
                    seen.add(item)
                    merged.append(item)
            result[key] = merged
            continue

        # Identity-keyed array sections
        if key in identity_keys:
            id_key = identity_keys[key]
            if id_key is None:
                result[key] = _merge_arrays(base_val, over_val, composite_fn=_COMPOSITE_FNS[key])
            else:
                result[key] = _merge_arrays(base_val, over_val, identity_key=id_key)
            continue

        # Both are dicts — deep merge
        if isinstance(base_val, dict) and isinstance(over_val, dict):
            result[key] = _deep_merge_dicts(base_val, over_val)
            continue

        # Both are lists but not a known section — concatenate
        if isinstance(base_val, list) and isinstance(over_val, list):
            result[key] = base_val + over_val
            continue

        # Scalar or type mismatch — override wins
        result[key] = over_val

    return result
