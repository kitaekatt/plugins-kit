#!/usr/bin/env python3
"""Disclose presence-kit's waiting rule to an orchestrate invocation, or print nothing.

This is the enabling probe of the skill-embedded enabling contract
(plugins/bootstrap/skills/plugin-dev/references/enabling.md). The orchestrate
SKILL.md runs it once per invocation, after the policy render, and appends any
stdout to the rendered policy. awesome-kit does not depend on presence-kit and
orchestrate is complete without it.

Discovery, in order:

    1. presence-kit@plugins-kit in ~/.claude/plugins/installed_plugins.json
       (the record without projectPath, newest version wins).
    2. Registry v2 keeps that file empty for marketplace installs, so when it
       holds no record: the highest version directory under
       ~/.claude/plugins/cache/plugins-kit/presence-kit/, only when the
       plugin is enabled in settings (enabledPlugins). This mirrors
       merge_cache_fallback in skills/plugin-ecosystem/scripts/generate.py and
       bootstrap_lib.plugin_resolve.discover_cache_plugins; it is
       re-implemented here because this probe is stdlib-only and must run when
       no shared library is linked.

The capability is current enough when the install's references/presence.md
carries the `## Waiting rule` heading. Four outcomes:

    present  1-3 lines on stdout: the capability and a pointer that resolves.
    absent   stdout empty.
    too old  stdout empty (the install lacks presence.md or the heading).
    stale    stdout empty (a record or cache entry whose install path is gone).

Stdout is empty in every state but `present`, and the exit status is 0 in all
four. `--explain` writes the distinct diagnosis to stderr; without it nothing
is written anywhere, so no absence message reaches the skill context. Output
is never cached; every invocation probes afresh.

Usage:
    presence_probe.py [--explain] [--project-root PATH]
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Set

PLUGIN = "presence-kit"
MARKETPLACE = "plugins-kit"
PLUGIN_REF = f"{PLUGIN}@{MARKETPLACE}"
PRESENCE_DOC = Path("references") / "presence.md"
FRONTIER_HEADING = "Waiting rule"
_HEADING_RE = re.compile(r"^## " + re.escape(FRONTIER_HEADING) + r"[ \t]*$", re.MULTILINE)

PRESENT = "present"
ABSENT = "absent"
TOO_OLD = "too old"
STALE = "stale after uninstall"


class Outcome(NamedTuple):
    state: str
    detail: str
    install_path: Optional[Path] = None
    version: str = ""


def _version_key(version: str):
    """Numeric dot-parts compare numerically; other names sort below them."""
    if re.fullmatch(r"\d+(?:\.\d+)*", version):
        return (1, tuple(int(part) for part in version.split(".")), version)
    return (0, (), version)


def _read_json(path: Path) -> Dict[str, Any]:
    """Parse a JSON object file; absent -> {}; unparsable -> raise."""
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be an object, got {type(data).__name__}")
    return data


def _pick_registry_record(entry: object) -> Optional[Dict[str, Any]]:
    """Mirror bootstrap_lib.plugin_resolve.pick_registry_record's policy:
    prefer a record without projectPath, then the newest numeric version."""
    if isinstance(entry, dict):
        return entry
    if not isinstance(entry, list):
        return None
    records = [record for record in entry if isinstance(record, dict)]
    if not records:
        return None
    return max(
        records,
        key=lambda record: (
            0 if record.get("projectPath") else 1,
            _version_key(str(record.get("version", "") or "")),
        ),
    )


def _enabled_refs(claude_home: Path, project_root: Path) -> Set[str]:
    """Refs enabled in user or project settings (enabledPlugins true)."""
    enabled: Set[str] = set()
    for path in (
        claude_home / "settings.json",
        project_root / ".claude" / "settings.json",
        project_root / ".claude" / "settings.local.json",
    ):
        plugins = _read_json(path).get("enabledPlugins", {})
        if not isinstance(plugins, dict):
            raise ValueError(f"{path}: enabledPlugins must be an object")
        for ref, on in plugins.items():
            if on is True:
                enabled.add(ref)
            elif on is False:
                enabled.discard(ref)
    return enabled


def _cache_record(claude_home: Path, project_root: Path) -> Optional[Dict[str, Any]]:
    """Highest cached version of presence-kit, only when the plugin is enabled."""
    if PLUGIN_REF not in _enabled_refs(claude_home, project_root):
        return None
    plugin_dir = claude_home / "plugins" / "cache" / MARKETPLACE / PLUGIN
    if not plugin_dir.is_dir():
        return None
    versions = [d for d in plugin_dir.iterdir() if d.is_dir()]
    if not versions:
        return None
    best = max(versions, key=lambda d: _version_key(d.name))
    return {"installPath": str(best), "version": best.name}


def probe(claude_home: Path, project_root: Path) -> Outcome:
    """Classify presence-kit under `claude_home` into one of the four states."""
    registry = _read_json(claude_home / "plugins" / "installed_plugins.json")
    plugins = registry.get("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("installed_plugins.json: 'plugins' must be an object")
    record = _pick_registry_record(plugins.get(PLUGIN_REF))
    source = "installed_plugins.json"
    if record is None:
        record = _cache_record(claude_home, project_root)
        source = "plugin cache"
    if record is None:
        return Outcome(
            ABSENT,
            f"{PLUGIN_REF} has no installed_plugins.json record and no enabled cache entry",
        )

    version = str(record.get("version", "") or "")
    raw_path = record.get("installPath")
    if not isinstance(raw_path, str) or not raw_path:
        return Outcome(STALE, f"{PLUGIN_REF} record from {source} has no installPath", None, version)
    install = Path(raw_path)
    if not install.is_dir():
        return Outcome(
            STALE,
            f"{PLUGIN_REF} record from {source} names an install path that no longer exists",
            install,
            version,
        )

    doc = install / PRESENCE_DOC
    if not doc.is_file():
        return Outcome(
            TOO_OLD,
            f"{PLUGIN_REF} {version or 'unknown version'} has no {PRESENCE_DOC.as_posix()}; "
            "run: claude plugin update presence-kit@plugins-kit",
            install,
            version,
        )
    if not _HEADING_RE.search(doc.read_text(encoding="utf-8")):
        return Outcome(
            TOO_OLD,
            f"{PLUGIN_REF} {version or 'unknown version'} installed, but "
            f"{PRESENCE_DOC.as_posix()} lacks the '## {FRONTIER_HEADING}' heading; "
            "run: claude plugin update presence-kit@plugins-kit",
            install,
            version,
        )
    return Outcome(PRESENT, f"{PLUGIN_REF} {version or 'unknown version'} from {source}", install, version)


def disclosure(outcome: Outcome) -> List[str]:
    """The capability and a pointer that resolves in the detected install."""
    if outcome.state != PRESENT or outcome.install_path is None:
        return []
    doc = outcome.install_path / PRESENCE_DOC
    return [
        f"presence-kit {outcome.version} is installed: it supplies the waiting rule "
        "and the afk and autonomous modes.",
        f'Waiting on non-agent work: {doc}, section "{FRONTIER_HEADING}".',
        "Seat routing for a load-bearing call stays with orchestrate's consultation policy "
        "in every mode.",
    ]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--explain", action="store_true", help="write the diagnosis to stderr")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="project whose settings decide enablement for the cache fallback (default: cwd)",
    )
    args = parser.parse_args(argv)
    project_root = (args.project_root or Path.cwd()).resolve()
    outcome = probe(Path.home() / ".claude", project_root)
    if args.explain:
        print(f"presence probe: {outcome.state}: {outcome.detail}", file=sys.stderr)
    for line in disclosure(outcome):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
