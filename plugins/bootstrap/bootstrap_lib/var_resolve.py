"""Variable resolution for bootstrap manifest string values.

Expands ${var} references using a variables dict. Unresolved variables
cause the value to be marked as unresolvable (returns None).
"""

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def resolve_vars(value: str, variables: Dict[str, str]) -> Optional[str]:
    """Expand ${var} references in a string value.

    Args:
        value: String potentially containing ${var} references
        variables: Dict of variable name -> value

    Returns:
        Expanded string, or None if any variable is unresolved
    """
    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        if var_name in variables:
            return variables[var_name]
        raise _UnresolvedVar(var_name)

    try:
        return VAR_PATTERN.sub(replacer, value)
    except _UnresolvedVar:
        return None


def build_variables(
    plugin_root: str,
    data_dir: str,
    config: Optional[Dict[str, Any]] = None,
    *,
    project_root: Optional[str] = None,
    marketplace: str = "plugins-kit",
    plugin: Optional[str] = None,
) -> Dict[str, str]:
    """Build the variables dict from static sources and config.

    Static variables:
        plugin_root: Plugin install path
        data_dir: Plugin data directory
        cwd: Canonical project root when supplied, otherwise the process CWD
        plugin_data_dir: Durable project data directory (project sessions only)

    Config-derived variables:
        For each config key whose value looks like a file path,
        adds <key>_dir with the dirname. E.g. uproject=/foo/bar.uproject
        -> uproject_dir=/foo
    """
    reserved = {"plugin_root", "data_dir", "cwd", "plugin_data_dir"}
    variables: Dict[str, str] = {
        "plugin_root": plugin_root,
        "data_dir": data_dir,
        "cwd": str(Path(project_root)) if project_root else os.getcwd(),
    }

    if config:
        for key, val in config.items():
            if key in reserved:
                continue
            if not isinstance(val, str) or not val:
                continue
            variables[key] = val
            # Derive _dir for values that look like file paths
            p = Path(val)
            if p.suffix and len(p.parts) > 1:
                variables[f"{key}_dir"] = str(p.parent)

    if project_root:
        from .config_resolve import resolve_plugin_data_dir

        plugin_name = plugin or Path(data_dir).name
        # A malformed plugin_data_dir override raises ConfigError and the
        # caller reports it. Do NOT swallow it here: dropping the variable
        # would leave ${plugin_data_dir} unexpanded in the manifest and the
        # misconfiguration invisible, which is the failure mode the durable
        # project data pattern exists to prevent.
        variables["plugin_data_dir"] = str(resolve_plugin_data_dir(
            project_root,
            marketplace=marketplace,
            plugin=plugin_name,
            config=config,
        ))

    return variables


class _UnresolvedVar(Exception):
    """Internal sentinel for unresolved variables."""
