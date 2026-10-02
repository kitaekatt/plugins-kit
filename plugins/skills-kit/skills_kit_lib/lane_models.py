"""lane_models -- resolve the layered md-domain `lane_models` config slot.

The slot names the model and effort each md-domain lane family runs under, as
an ordered list of ``{id, effort}`` entries (preference order)::

    lane_models:
      detect:    [{id: sonnet, effort: low}]
      audit_job: [{id: luna, effort: high}, {id: sonnet, effort: low}]

Layers, lowest first. They are the same config files standards_resolve reads,
found through ``standards_resolve.layer_paths`` and loaded with its loader:

    shipped  skills_kit_lib/defaults/lane_models.yaml   (must exist)
    user     <user_dir>/skills-kit/config.yaml, then config.local.yaml
    project  <project>/.claude/skills-kit/config.yaml, then config.local.yaml

``<user_dir>`` is ``<home>/.claude`` when ``home`` is given, else
``$CLAUDE_CONFIG_DIR``, else ``~/.claude``. A layer without a ``lane_models``
key contributes nothing. A later layer wins per family key, and its list
REPLACES the lower layer's list wholesale.

Grammar is review_profiles' entry grammar: ``EFFORT_LEVELS`` is imported from
``bootstrap_lib.code_review.review_profiles`` and the ordered ids pass through
``bootstrap_lib.model_declaration.parse``.

Errors (``LaneModelsError``; CLI exit 2): an unknown family key, a value that
is not a list, an entry that is not a mapping, an unknown entry field, a
missing id, an effort outside ``EFFORT_LEVELS``, a blank or duplicate id, an
empty list, a missing or incomplete shipped file.

Findings (``IncompleteLaneModelsError``; CLI exit 1): an entry that states no
effort. Findings are collected across ALL layers, including layers a higher
layer overrides, and raised together after every layer validated.

``agent_route`` splits an agent-only family's entries into the ids Workflow
``agent()`` / the Agent tool can run (``CORE_IDS``) and the ids they cannot,
which are dropped and reported. ``audit_job`` never goes through it, because
its route runs through job-kit and llm-scripting-kit.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from bootstrap_lib import model_declaration
from bootstrap_lib.code_review.review_profiles import EFFORT_LEVELS

from . import standards_resolve

#: Families whose lanes run on Workflow agent() or the Agent tool only.
AGENT_ONLY_FAMILIES = ("detect", "classify", "coverage", "generate", "remediate")

#: Every family the slot carries.
FAMILIES = AGENT_ONLY_FAMILIES + ("audit_job",)

SLOT_KEY = "lane_models"
ENTRY_FIELDS = frozenset({"id", "effort"})
DEFAULTS_PATH = Path(__file__).resolve().parent / "defaults" / "lane_models.yaml"

#: The reason agent_route records for every dropped entry.
DROP_REASON = "not runnable by agent()/Agent"


class LaneModelsError(ValueError):
    """A lane_models layer is missing, unreadable, or malformed."""


class IncompleteLaneModelsError(LaneModelsError):
    """Every layer is well formed, but one or more entries state no effort.

    ``findings`` lists every gap across every layer, one string each.
    """

    def __init__(self, findings: list[str]) -> None:
        self.findings = list(findings)
        super().__init__(
            f"{len(self.findings)} incomplete lane_models entr"
            f"{'y' if len(self.findings) == 1 else 'ies'}:\n  "
            + "\n  ".join(self.findings)
        )


class NoRunnableLaneModelError(LaneModelsError):
    """agent_route dropped every entry of a family, so nothing can run it."""


def _fail(source: str, location: str, message: str) -> None:
    raise LaneModelsError(f"{source}: {location}: {message}")


def layer_files(
    project_root: Path | None = None, home: Path | None = None
) -> list[tuple[str, Path]]:
    """Return ``[(layer, path), ...]`` lowest first. Pure: reads no file."""
    user_dir = None if home is None else Path(home).expanduser() / ".claude"
    config_files, _dirs = standards_resolve.layer_paths(
        None if project_root is None else Path(project_root).expanduser(),
        user_dir=user_dir,
    )
    # layer_paths lists the two user files, then (with a project root) the two
    # project files.
    layers = [("shipped", DEFAULTS_PATH)]
    layers += [("user", path) for path in config_files[:2]]
    layers += [("project", path) for path in config_files[2:]]
    return layers


def _load(path: Path, source: str) -> dict:
    """Load one layer through standards_resolve's loader; wrap its error."""
    if not standards_resolve.HAVE_YAML:
        raise LaneModelsError(
            f"{source}: pyyaml is not importable by this interpreter "
            f"({sys.executable}); run under the skills-kit plugin venv"
        )
    try:
        return standards_resolve._load_config_file(path)
    except standards_resolve.StandardsConfigError as exc:
        raise LaneModelsError(str(exc)) from exc


def _validate_family(value: Any, source: str, location: str) -> list[str]:
    """Validate one family's entry list; return its effort findings."""
    if isinstance(value, dict):
        _fail(source, location,
              "must be a list of {id, effort} entries, got a mapping; write "
              "`[{id: <model>, effort: <level>}]` -- a list replaces the lower "
              "layer's list wholesale, a mapping has no such meaning")
    if not isinstance(value, list):
        _fail(source, location,
              "must be a list of {id: <model>, effort: <level>} entries, got "
              f"{type(value).__name__}")
    findings: list[str] = []
    ids: list[Any] = []
    for index, entry in enumerate(value):
        where = f"{location}[{index}]"
        if isinstance(entry, str):
            # The id-only shape: a gap in the entry, reported with the others.
            findings.append(
                f"{source}: {where} ({entry.strip()}): states no effort; write "
                f"[{{id: {entry.strip()}, effort: <level>}}]"
            )
            ids.append(entry)
            continue
        if not isinstance(entry, dict):
            _fail(source, where,
                  "must be an {id: <model>, effort: <level>} entry, got "
                  f"{type(entry).__name__}")
        unknown = [k for k in entry if k not in ENTRY_FIELDS]
        if unknown:
            _fail(source, where,
                  f"unknown field(s): {', '.join(repr(k) for k in unknown)}; "
                  f"known fields: effort, id")
        if "id" not in entry:
            _fail(source, where, "required field missing: id")
        if "effort" in entry:
            effort = entry["effort"]
            if effort not in EFFORT_LEVELS:
                levels = ", ".join(repr(level) for level in EFFORT_LEVELS)
                _fail(source, f"{where}.effort",
                      f"unknown effort {effort!r}; known levels: {levels}")
        else:
            label = entry["id"].strip() if isinstance(entry["id"], str) else entry["id"]
            findings.append(f"{source}: {where} ({label}): missing effort")
        ids.append(entry["id"])
    try:
        model_declaration.parse(ids)
    except model_declaration.DeclarationError as exc:
        where = location if exc.index is None else f"{location}[{exc.index}]"
        _fail(source, where, str(exc))
    return findings


def _validate_layer(data: dict, source: str) -> tuple[dict[str, list], list[str]]:
    """Return (the layer's family -> entries, its findings); raise on errors."""
    if SLOT_KEY not in data:
        return {}, []
    slot = data[SLOT_KEY]
    if not isinstance(slot, dict):
        _fail(source, SLOT_KEY,
              f"must be a mapping of family -> entry list, got {type(slot).__name__}")
    findings: list[str] = []
    for family, value in slot.items():
        if family not in FAMILIES:
            _fail(source, f"{SLOT_KEY}.{family}",
                  f"unknown family {family!r}; known families: {', '.join(FAMILIES)}")
        findings.extend(_validate_family(value, source, f"{SLOT_KEY}.{family}"))
    return dict(slot), findings


def _normalize(entries: list) -> list[dict]:
    return [{"id": e["id"].strip(), "effort": e["effort"]} for e in entries]


def load_lane_models(
    project_root: Path | None = None, home: Path | None = None
) -> dict[str, list[dict]]:
    """Resolve every layer and return ``{family: [{id, effort}, ...]}``.

    Families appear in ``FAMILIES`` order. Raises ``LaneModelsError`` on a
    malformed or missing layer and ``IncompleteLaneModelsError`` (carrying
    ``.findings``) when any layer holds an entry without an effort.
    """
    if not DEFAULTS_PATH.is_file():
        raise LaneModelsError(f"shipped lane_models file missing: {DEFAULTS_PATH}")
    resolved: dict[str, list] = {}
    findings: list[str] = []
    for layer, path in layer_files(project_root, home):
        source = f"{layer} {path}"
        families, layer_findings = _validate_layer(_load(path, source), source)
        if layer == "shipped":
            missing = [f for f in FAMILIES if f not in families]
            if missing:
                raise LaneModelsError(
                    f"{source}: shipped {SLOT_KEY} must declare every family; "
                    f"missing: {', '.join(missing)}"
                )
        findings.extend(layer_findings)
        resolved.update(families)
    if findings:
        raise IncompleteLaneModelsError(findings)
    return {family: _normalize(resolved[family]) for family in FAMILIES}


def agent_route(entries: list[dict]) -> dict[str, list[dict]]:
    """Split ``entries`` into the ids agent()/Agent can run and the dropped rest.

    Returns ``{"run": [{id, effort}, ...], "dropped": [{id, effort, reason}, ...]}``,
    each in declaration order. A drop is not an error. Raises
    ``NoRunnableLaneModelError`` when no entry is runnable.
    """
    run: list[dict] = []
    dropped: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict) or "id" not in entry or "effort" not in entry:
            raise LaneModelsError(f"agent_route: not an {{id, effort}} entry: {entry!r}")
        if model_declaration.is_core_id(entry["id"]):
            run.append({"id": entry["id"].strip(), "effort": entry["effort"]})
        else:
            dropped.append({"id": entry["id"], "effort": entry["effort"], "reason": DROP_REASON})
    if not run:
        names = ", ".join(f"{d['id']} ({d['effort']})" for d in dropped) or "none"
        raise NoRunnableLaneModelError(
            f"no declared entry is runnable by agent()/Agent (core ids: "
            f"{', '.join(sorted(model_declaration.CORE_IDS))}); dropped: {names}"
        )
    return {"run": run, "dropped": dropped}


def agent_routes(resolved: dict[str, list[dict]]) -> dict[str, dict[str, list[dict]]]:
    """The resolve_standards JSON block: ``{family: {declared, run, dropped}}``.

    Covers the agent-only families only. Raises ``NoRunnableLaneModelError``,
    naming the family, when one of them has nothing runnable.
    """
    block: dict[str, dict[str, list[dict]]] = {}
    for family in AGENT_ONLY_FAMILIES:
        declared = [dict(e) for e in resolved[family]]
        try:
            route = agent_route(declared)
        except NoRunnableLaneModelError as exc:
            raise NoRunnableLaneModelError(f"{SLOT_KEY}.{family}: {exc}") from exc
        block[family] = {"declared": declared, **route}
    return block


def render_yaml(resolved: dict[str, list[dict]]) -> str:
    """Return the resolved slot as YAML, one flow-style entry per line."""
    import yaml

    return yaml.safe_dump(
        {SLOT_KEY: resolved}, sort_keys=False, default_flow_style=None
    )


def main(argv: list[str] | None = None) -> int:
    """CLI: exit 0 complete, 1 findings (stderr), 2 malformed (stderr)."""
    parser = argparse.ArgumentParser(
        prog="skills_kit_tool.py lane-models",
        description="Resolve the layered md-domain lane_models slot.",
    )
    parser.add_argument("--check", action="store_true",
                        help="Validate every layer and print no slot. Exit 0 "
                             "complete, 1 findings, 2 malformed.")
    parser.add_argument("--project-root", default=str(Path.cwd()),
                        help="Project root for the project layer (default: cwd).")
    parser.add_argument("--home",
                        help="Home root for the user layer (<home>/.claude); "
                             "default $CLAUDE_CONFIG_DIR, else ~/.claude.")
    args = parser.parse_args(argv)
    project_root = Path(args.project_root).expanduser().resolve()
    home = Path(args.home).expanduser().resolve() if args.home else None
    try:
        resolved = load_lane_models(project_root, home)
    except IncompleteLaneModelsError as exc:
        print(f"lane_models: {exc}", file=sys.stderr)
        return 1
    except LaneModelsError as exc:
        print(f"lane_models: {exc}", file=sys.stderr)
        return 2
    if args.check:
        files = [str(p) for _layer, p in layer_files(project_root, home) if p.is_file()]
        print(f"lane_models: complete ({len(files)} layer file(s): {', '.join(files)})")
        return 0
    sys.stdout.write(render_yaml(resolved))
    return 0


if __name__ == "__main__":
    sys.exit(main())
