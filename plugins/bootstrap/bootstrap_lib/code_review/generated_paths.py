"""The second generated-artifact axis: paths a plugin DECLARES it writes.

The content axis (``generated.py``) reads banners. It is necessary and it is not
sufficient: a generator is free to emit no banner at all, and a large real stub
that begins directly at its imports carries no marker of any kind. Nothing in
its bytes says "a tool wrote this", so no signature list can ever catch it.

What DOES say so is where the file lives. A file under a project's durable
plugin-data path is plugin-written BY CONSTRUCTION -- that is the definition of
the durable-project-data pattern (see the bootstrap skill's
``references/durable-project-data.md``), not an inference about its content. The
same holds for a path a manifest declares as a write target.

So detection is a UNION: content signature OR declared-generated path. Either
axis alone marks the file generated; neither weakens the other.

Everything here is DERIVED, never hardcoded:

- the durable and ephemeral container layouts come from
  ``config_resolve.resolve_plugin_data_dir`` / ``standard_config_layers``
  themselves, probed with sentinel names, so a layout change in bootstrap
  reaches this module with no edit here;
- a project's ``plugin_data_dir`` relocation is resolved through that same
  resolver, with the project's own config as input;
- manifest-declared write targets are expanded with ``var_resolve.resolve_vars``,
  the expander the engine uses.

Fail-open is the rule throughout: a malformed config, an unreadable manifest, or
a missing PyYAML yields FEWER rules, never an exception. The cost of a missed
rule is a file being reviewed (waste); the cost of a raised exception is a
review that does not run at all.
"""

import json
import os
from pathlib import Path
from typing import Optional

# Sentinel names used only to probe the resolver for its layout. They never
# touch the filesystem.
_PROBE = "_probe_marketplace_"
_PROBE_PLUGIN = "_probe_plugin_"

# Manifest keys whose values are paths a bootstrap phase may write to.
_PATH_KEYS = ("extract_to", "target", "file", "reference")

LABEL_DURABLE = "declared plugin-data path (durable)"
LABEL_RELOCATED = "declared plugin-data path (relocated by project config)"
LABEL_EPHEMERAL = "declared plugin-data path (ephemeral)"
LABEL_MANIFEST = "manifest-declared write target"


def _norm(path: Path) -> str:
    return os.path.normcase(str(path))


def durable_container(workspace_root: Path) -> Optional[Path]:
    """`<workspace>/.plugin-data` -- derived from the resolver, not spelled out.

    Probing with sentinel marketplace/plugin names and stripping the two
    namespace segments yields the container that holds EVERY plugin's durable
    directory, so one prefix rule covers all of them without enumerating any.
    """
    from bootstrap_lib.config_resolve import resolve_plugin_data_dir

    try:
        probe = resolve_plugin_data_dir(
            workspace_root, marketplace=_PROBE, plugin=_PROBE_PLUGIN
        )
    except Exception:
        return None
    if probe.name != _PROBE_PLUGIN or probe.parent.name != _PROBE:
        # The layout changed shape; a guessed answer would be worse than none.
        return None
    return probe.parent.parent


def ephemeral_container(workspace_root: Path) -> Optional[Path]:
    """`<workspace>/.local-data` -- derived from `standard_config_layers`."""
    from bootstrap_lib.config_resolve import standard_config_layers

    try:
        layers = standard_config_layers(
            plugin=_PROBE_PLUGIN, marketplace=_PROBE, project_root=workspace_root
        )
    except Exception:
        return None
    if not layers:
        return None
    project_layer = layers[-1]
    if (
        project_layer.parent.name != _PROBE_PLUGIN
        or project_layer.parent.parent.name != _PROBE
    ):
        return None
    return project_layer.parent.parent.parent


def _relocated_dirs(workspace_root: Path, ephemeral: Optional[Path]) -> list[Path]:
    """Durable dirs a project config moved off the default container.

    Each `<ephemeral>/<marketplace>/<plugin>/config.yaml` is fed to the SAME
    resolver the engine uses, so a `plugin_data_dir:` relocation is followed
    rather than re-implemented. A layer that cannot be read (malformed YAML, no
    PyYAML) is skipped -- the default container rule still covers the common
    case.
    """
    from bootstrap_lib.config_resolve import load_config_layer, resolve_plugin_data_dir

    if ephemeral is None or not ephemeral.is_dir():
        return []
    out: list[Path] = []
    for config_path in sorted(ephemeral.glob("*/*/config.yaml")):
        marketplace = config_path.parent.parent.name
        plugin = config_path.parent.name
        try:
            config = load_config_layer(config_path)
        except Exception:
            continue
        if not config or "plugin_data_dir" not in config:
            continue
        try:
            resolved = resolve_plugin_data_dir(
                workspace_root,
                marketplace=marketplace,
                plugin=plugin,
                config=config,
            )
        except Exception:
            continue
        out.append(resolved)
    return out


def _manifest_targets(workspace_root: Path, durable: Optional[Path]) -> list[Path]:
    """Path-bearing declarations in the project's layered `bootstrap.json`.

    Values are expanded with the engine's own expander, binding only the
    variables that are meaningful inside a workspace (`cwd` / `project_root`,
    and `plugin_data_dir`). A value rooted at `${plugin_root}` or `${data_dir}`
    therefore stays UNRESOLVED and is dropped -- correctly, since those live in
    the plugin cache and the user's data dir, outside the tree under review.
    """
    from bootstrap_lib.var_resolve import resolve_vars

    manifest = workspace_root / ".claude" / "bootstrap.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []

    variables = {
        "cwd": str(workspace_root),
        "project_root": str(workspace_root),
    }
    if durable is not None:
        # The project manifest is not scoped to one plugin, so bind the
        # CONTAINER: any ${plugin_data_dir}-rooted target lands under it, which
        # the durable rule already covers -- this binding just keeps such values
        # resolvable instead of silently dropped.
        variables["plugin_data_dir"] = str(durable)

    out: list[Path] = []
    for value in _walk_path_values(data):
        try:
            expanded = resolve_vars(value, variables)
        except Exception:
            expanded = None
        if not expanded:
            continue
        candidate = Path(expanded)
        if not candidate.is_absolute():
            candidate = workspace_root / candidate
        out.append(candidate)
    return out


def _walk_path_values(data: object) -> list[str]:
    found: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            if key in _PATH_KEYS and isinstance(value, str) and value:
                found.append(value)
            else:
                found.extend(_walk_path_values(value))
    elif isinstance(data, list):
        for item in data:
            found.extend(_walk_path_values(item))
    return found


def declared_generated_rules(
    workspace_root: Optional[Path],
) -> list[tuple[str, Path]]:
    """(label, root) pairs whose subtree a plugin declares that it writes.

    Returns [] for an unknown workspace root: with no project anchor there is no
    declared path to speak of, and the content axis still applies.
    """
    if workspace_root is None:
        return []
    try:
        root = Path(workspace_root).resolve()
    except OSError:
        return []

    rules: list[tuple[str, Path]] = []
    durable = durable_container(root)
    if durable is not None:
        rules.append((LABEL_DURABLE, durable))
    ephemeral = ephemeral_container(root)
    if ephemeral is not None:
        rules.append((LABEL_EPHEMERAL, ephemeral))
    for relocated in _relocated_dirs(root, ephemeral):
        rules.append((LABEL_RELOCATED, relocated))
    for target in _manifest_targets(root, durable):
        rules.append((LABEL_MANIFEST, target))
    return rules


def match_declared_path(
    local: Optional[str], rules: list[tuple[str, Path]]
) -> Optional[str]:
    """Label of the first rule whose subtree contains `local`, else None.

    Compared with `os.path.normcase` so a Windows workspace matches regardless
    of the drive-letter and component casing a VCS front-half emits.
    """
    if not local or not rules:
        return None
    try:
        target = _norm(Path(local).resolve())
    except OSError:
        target = _norm(Path(local))
    for label, root in rules:
        root_norm = _norm(root)
        if target == root_norm or target.startswith(root_norm + os.sep):
            return label
    return None
