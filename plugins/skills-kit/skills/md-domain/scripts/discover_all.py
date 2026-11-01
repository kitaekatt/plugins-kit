#!/usr/bin/env python3
"""discover_all.py -- one command that makes md-domain's "audit everything" real.

Runs the four audit lanes' discoverers under ONE pruning policy and emits, per
lane, exactly the Workflow `args` that lane's detect script expects, so the
caller passes `lanes.<lane>` verbatim as `args` of one top-level Workflow call
per lane (never nested; see references/lanes/audit-lane.md).

Usage (from the project to audit):

    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" \\
        "${SKILLS_KIT_ROOT:?requires a bootstrap engine pass; run bootstrap run}/skills/md-domain/scripts/discover_all.py" \\
        [--out FILE] [--max-concurrent N] [--scope SCOPES] [--sources project|all] \\
        [--include-dir NAME ...]

Lanes covered, with the subjects each takes:

    audit_skill        every SKILL.md plus each skill's references/*.md
    audit_claude_md    every CLAUDE.md (AGENTS.md where no CLAUDE.md sits
                       beside it) and CLAUDE.local.md at or below the scan root
    audit_project_doc  project documents (discover_project_doc.py kind
                       `project_doc`)
    audit_references   the cross-reference scan's findings, grouped by file

NOT covered, by decision: `analyze` (the coverage lane) and composition. Both
are report-only over ONE directory and have no whole-repo default (SKILL.md,
"Analyze subject"); "audit everything" is the menu's four audit targets.

Ownership, so no file is audited twice: a doc under a discovered skill's
references/ is owned by audit_skill; CLAUDE.md / AGENTS.md / SKILL.md are owned
by their own lanes (the project-doc lane declines them: audit-lane.md, step
2a). The lane that already holds a path keeps it; the project-doc lane's
duplicate claim is dropped and listed in `deduplicated` (a plain project_doc)
or counted in `ownedByOtherLane` (the other-artifact kinds). A path claimed by
two lanes under no such rule is an error, never a silent pick.

Each lane's file records carry `ancestorClaudeMdPaths` (nearest-ancestor first,
excluding the subject, up to and including the workspace root: the nearest
ancestor holding a `.git` entry, else the scan root) and `standardsPaths`
(resolve_standards.py's authored files for the lane's primitive: skill_md,
reference_doc, claude_md, plain_md). `disabledCriteria`, `laneModels` and the
run-level `fixMode` come from one resolve_standards.py run; a failure there
stops this script. `--max-concurrent N` (a positive integer) becomes
`maxConcurrent` in every lane's args and is absent when not given.

Output is one JSON object: `lanes` (a lane appears only when it has subjects),
`skipped` (each pruned directory, each file no lane would take, each empty
lane, with its reason), `deduplicated`, `counts`, `fixMode`, `notes`,
`projectRoot`, `scanRoot`. The counts
also go to stderr. Exit 0 on success; 1 when standards or the lane models
cannot be resolved or the venv interpreter cannot be located; 2 on a usage
error or an ownership collision.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
from pathlib import Path

_MD_DOMAIN = Path(__file__).resolve().parents[1]
_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
_PLUGIN_SCRIPTS = _PLUGIN_ROOT / "scripts"
_SCRIPTS_DIR = Path(__file__).resolve().parent
for _p in (_PLUGIN_ROOT, _SCRIPTS_DIR, _PLUGIN_SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from skills_kit_lib.dirwalk import is_link_dir  # noqa: E402
from skills_kit_lib.instruction_files import resolve_instruction_file  # noqa: E402

import bootstrap_guard  # noqa: E402  (vendored, stdlib-only)
import discover_claude_md  # noqa: E402
import discover_project_doc  # noqa: E402
import discover_skill  # noqa: E402
import references_audit  # noqa: E402
import resolve_standards  # noqa: E402
import vcs_ignore  # noqa: E402

LANE_SKILL = "audit_skill"
LANE_CLAUDE_MD = "audit_claude_md"
LANE_PROJECT_DOC = "audit_project_doc"
LANE_REFERENCES = "audit_references"
LANES = (LANE_SKILL, LANE_CLAUDE_MD, LANE_PROJECT_DOC, LANE_REFERENCES)

#: The file-type primitives this script resolves standards for; one lane takes
#: two (skill_md for a SKILL.md, reference_doc for a skill reference).
PRIMITIVES = ("skill_md", "reference_doc", "claude_md", "plain_md")

#: The lane_models family each lane's script reads.
LANE_FAMILY = {
    LANE_SKILL: "detect",
    LANE_CLAUDE_MD: "detect",
    LANE_PROJECT_DOC: "detect",
    LANE_REFERENCES: "classify",
}

STANDARDS_DIR = _MD_DOMAIN / "references" / "standards"
SKILL_STANDARDS = STANDARDS_DIR / "skill-standards.md"
CLAUDE_MD_STANDARDS = STANDARDS_DIR / "claude-md-standards.md"
PROJECT_DOC_STANDARDS = STANDARDS_DIR / "project-doc-standards.md"
REFERENCES_STANDARDS = STANDARDS_DIR / "references-standards.md"
REFERENCES_TAXONOMY = _MD_DOMAIN / "references" / "references-finding-taxonomy.md"


class DiscoverAllError(Exception):
    """A condition this script refuses to continue past (exit 1)."""


class OwnershipCollision(DiscoverAllError):
    """One path is claimed by two lanes and no ownership rule decides (exit 2)."""


def _key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _rel(path: Path | str, root: Path) -> str:
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _within(path: Path, root: Path) -> bool:
    return path.parent == root or root in path.parents


def positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(
            f"must be a positive whole number (1 or more), got {raw!r}"
        )
    return value


def workspace_root(scan_root: Path) -> Path:
    """The ancestor chain's stopping point: the nearest ancestor holding a
    `.git` entry, else the scan root (audit-lane.md, Step 1)."""
    return discover_claude_md.find_project_root(scan_root) or scan_root


def ancestor_chain(target: Path, root: Path) -> list[str]:
    """Instruction files above `target`, nearest first, excluding `target`.

    Starts at the target's own directory (so a skill's co-located CLAUDE.md is
    included) and walks up to and including `root`. A directory contributes its
    CLAUDE.md, or its AGENTS.md when it has no CLAUDE.md.
    """
    chain: list[str] = []
    target_key = _key(target)
    directory = target.parent
    while True:
        found = resolve_instruction_file(directory)
        if found is not None and _key(found) != target_key:
            chain.append(str(found))
        if directory == root or directory == directory.parent:
            return chain
        directory = directory.parent


def resolve_run_config(workspace: Path) -> dict:
    """One resolve_standards.py run for all four primitives, parsed.

    The script owns the resolution (layers, validation, the shipped/authored
    split, lane routes); this reuses its output rather than a second copy of
    that logic. Its stderr line passes through unchanged on failure.
    """
    argv = ["--project-root", str(workspace)]
    for primitive in PRIMITIVES:
        argv += ["--primitive", primitive]
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        status = resolve_standards.main(argv)
    if status != 0:
        raise DiscoverAllError(
            f"resolve_standards.py exited {status}; no audit runs on a partial config"
        )
    return json.loads(captured.getvalue())


def _lane_models(config: dict, lane: str) -> dict:
    family = LANE_FAMILY[lane]
    entry = config["lane_models"][family]
    return {"run": entry["run"], "dropped": entry["dropped"]}


def venv_python(override: str | None) -> str:
    if override:
        return override
    found = bootstrap_guard.plugin_venv_python("skills-kit")
    if found is None:
        raise DiscoverAllError(
            "the skills-kit plugin venv interpreter does not exist; "
            + bootstrap_guard.message("skills-kit")
            + " Pass --venv-python to name an interpreter."
        )
    return str(found)


def _clear_caches() -> None:
    vcs_ignore.clear_cache()
    discover_project_doc._WALK_CACHE.clear()


def discover_skill_files(root: Path, include_dirs, skipped_dirs):
    return discover_skill.collect_skill_md(
        root, include_references=True, include_dirs=include_dirs, skipped_out=skipped_dirs
    )


def discover_claude_md_files(root: Path, include_dirs, skipped_dirs, skipped):
    found = discover_claude_md.discover(root, include_dirs=include_dirs, skipped_out=skipped_dirs)
    kept = []
    for path, role in found:
        if _within(path, root):
            kept.append((path, role))
        else:
            skipped.append({
                "path": str(path), "kind": "file",
                "reason": "above the scan root: ancestor context, not a subject "
                          "(run from its own directory to audit it)",
            })
    return kept


def discover_project_doc_records(
    root: Path, include_dirs, skipped_dirs, config_dir: Path | None, skip_plugin_cache: bool
):
    """discover_project_doc.py's main() pipeline, in process."""
    candidates = discover_project_doc.collect_candidates(
        root, include_dirs=include_dirs, skipped_out=skipped_dirs
    )
    anchor = candidates[0].parent if candidates else root
    citer_root = discover_project_doc.find_project_root(anchor) or root
    extra = None
    if not skip_plugin_cache:
        cfg = config_dir if config_dir is not None else discover_project_doc._config_dir()
        extra = list(discover_project_doc.plugin_cache_citer_files(cfg, project_root=citer_root))
    inbound = discover_project_doc.build_inbound_index(
        citer_root, candidates, extra_citer_files=extra, include_dirs=include_dirs
    )
    return [discover_project_doc.describe(p, inbound, citer_root) for p in candidates]


def run_references_scan(
    root: Path, user_dir: Path, plugins_dir: Path, scope: str, sources: str
) -> list[dict]:
    """references_audit.analyze(json_output=True), captured and parsed."""
    scopes = references_audit.parse_scope_arg(scope)
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        references_audit.analyze(
            root / ".claude" / "skills", user_dir, plugins_dir, scopes,
            [], [], [], json_output=True, sources_scope=sources,
        )
    payload = json.loads(captured.getvalue())
    return payload["findings"]


def group_reference_findings(findings: list[dict], skipped: list[dict]) -> list[dict]:
    """The classify lane's `files`: findings grouped by containing file.

    A finding with no file (a shadowed-skill INFO is a pool fact, not a
    file's) cannot be classified per file; it is reported in `skipped`.
    """
    by_file: dict[str, list[dict]] = {}
    for f in findings:
        if not f.get("file"):
            skipped.append({
                "path": f.get("ref") or "", "kind": "reference-finding",
                "reason": f"{f['category_hint']} finding names no file; reported, not classified",
            })
            continue
        by_file.setdefault(f["file"], []).append(
            {"severity": f["severity"], "line": f["line"], "ref": f["ref"] or ""}
        )
    return [
        {"file": file, "findings": sorted(fs, key=lambda x: (x["line"] or 0, x["ref"]))}
        for file, fs in sorted(by_file.items())
    ]


class Claims:
    """Which lane owns which path; a second claim with no rule is an error."""

    def __init__(self) -> None:
        self._owner: dict[str, str] = {}

    def claim(self, lane: str, path: str | Path) -> None:
        k = _key(path)
        held = self._owner.get(k)
        if held is not None and held != lane:
            raise OwnershipCollision(
                f"{path} is claimed by both {held} and {lane}, and no ownership "
                "rule decides between them"
            )
        self._owner[k] = lane

    def owner(self, path: str | Path) -> str | None:
        return self._owner.get(_key(path))


def build(
    root: Path,
    *,
    max_concurrent: int | None = None,
    include_dirs=(),
    scope: str = "skills",
    sources: str = "project",
    user_dir: Path | None = None,
    plugins_dir: Path | None = None,
    config_dir: Path | None = None,
    skip_plugin_cache: bool = False,
    venv_python_override: str | None = None,
) -> dict:
    root = Path(root).resolve()
    _clear_caches()
    home_claude = Path.home() / ".claude"
    user_dir = user_dir if user_dir is not None else home_claude / "skills"
    plugins_dir = plugins_dir if plugins_dir is not None else home_claude / "plugins"
    workspace = workspace_root(root)

    config = resolve_run_config(workspace)
    standards = config["standards"]
    venv = venv_python(venv_python_override)

    skipped: list[dict] = []
    skipped_dirs: list[Path] = []
    deduplicated: list[dict] = []
    owned_elsewhere = 0
    claims = Claims()

    # --- audit_skill -------------------------------------------------------
    skill_files = []
    for path, name, skill_type, kind in discover_skill_files(root, include_dirs, skipped_dirs):
        claims.claim(LANE_SKILL, path)
        record = {"path": str(path), "kind": kind}
        if kind == "skill":
            record["skillType"] = skill_type
        primitive = "skill_md" if kind == "skill" else "reference_doc"
        record["ancestorClaudeMdPaths"] = ancestor_chain(path, workspace)
        record["standardsPaths"] = list(standards[primitive])
        skill_files.append(record)

    # --- audit_claude_md ---------------------------------------------------
    claude_files = []
    for path, role in discover_claude_md_files(root, include_dirs, skipped_dirs, skipped):
        claims.claim(LANE_CLAUDE_MD, path)
        chain = ancestor_chain(path, workspace)
        claude_files.append({
            "path": str(path),
            "role": role,
            "parentPath": chain[0] if role == "child" and chain else None,
            "kind": "claude-md",
            "dimension": "classic" if role == "local" else discover_claude_md.classify_dimension(path),
            "ancestorClaudeMdPaths": chain,
            "standardsPaths": list(standards["claude_md"]),
        })

    # --- audit_project_doc -------------------------------------------------
    doc_files = []
    for rec in discover_project_doc_records(root, include_dirs, skipped_dirs, config_dir, skip_plugin_cache):
        path = Path(rec["path"])
        kind = rec["kind"]
        owner = claims.owner(path)
        if owner is not None:
            # Ownership rule: a skill's reference documents belong to
            # audit_skill and a CLAUDE.md / AGENTS.md / SKILL.md to its own
            # lane (audit-lane.md, step 2a), so the lane that already took the
            # path keeps it. That is expected for the other-artifact kinds, so
            # those are counted; a plain project_doc the skill lane owns (a
            # references/ folder the project-doc walk does not recognise by
            # path) is listed.
            if kind == "project_doc":
                deduplicated.append({
                    "path": _rel(path, root), "keptBy": owner, "droppedFrom": LANE_PROJECT_DOC,
                    "reason": "the path sits under a discovered skill; its owner lane keeps it",
                })
            else:
                owned_elsewhere += 1
            continue
        if kind != "project_doc":
            skipped.append({
                "path": _rel(path, root), "kind": "file",
                "reason": f"{kind} that its own lane did not discover "
                          "(a VCS-ignored directory, a shadowed AGENTS.md, or beyond that lane's depth limit)",
            })
            continue
        claims.claim(LANE_PROJECT_DOC, path)
        doc_files.append({
            "path": rec["path"],
            "kind": kind,
            "lines": rec["lines"],
            "approx_tokens": rec["approx_tokens"],
            "inbound_citations": rec["inbound_citations"],
            "cited_by": rec["cited_by"],
            "ancestorClaudeMdPaths": ancestor_chain(path, workspace),
            "standardsPaths": list(standards["plain_md"]),
        })

    # --- audit_references --------------------------------------------------
    findings = run_references_scan(root, user_dir, plugins_dir, scope, sources)
    ref_files = group_reference_findings(findings, skipped)

    # --- assemble ----------------------------------------------------------
    def common(lane: str, files: list[dict]) -> dict:
        args: dict = {"files": files}
        if lane != LANE_REFERENCES:
            args["disabledCriteria"] = list(config["disabled"])
            args["review"] = False
        args["laneModels"] = _lane_models(config, lane)
        if max_concurrent is not None:
            args["maxConcurrent"] = max_concurrent
        return args

    lanes: dict[str, dict] = {}
    by_lane = {
        LANE_SKILL: skill_files,
        LANE_CLAUDE_MD: claude_files,
        LANE_PROJECT_DOC: doc_files,
        LANE_REFERENCES: ref_files,
    }
    for lane, files in by_lane.items():
        if not files:
            skipped.append({"path": lane, "kind": "lane", "reason": "no subjects found"})
            continue
        args = common(lane, files)
        if lane == LANE_SKILL:
            args["refs"] = {"pluginRoot": str(_PLUGIN_ROOT), "venvPython": venv}
        elif lane == LANE_CLAUDE_MD:
            doc = str(CLAUDE_MD_STANDARDS)
            args["density"] = False
            args["refs"] = {
                "criteria": doc, "codeDirFilter": doc, "densityCriteria": doc,
                "pluginRoot": str(_PLUGIN_ROOT), "venvPython": venv,
            }
        elif lane == LANE_PROJECT_DOC:
            args["refs"] = {"criteria": str(PROJECT_DOC_STANDARDS), "pluginRoot": str(_PLUGIN_ROOT)}
        else:
            args["refs"] = {
                "standardsDoc": str(REFERENCES_STANDARDS),
                "taxonomyDoc": str(REFERENCES_TAXONOMY),
            }
        lanes[lane] = args

    # Every discoverer prunes by the same policy, so one directory is reported
    # by several of them; keep one record per path.
    dir_records = {
        _key(p): {
            "path": _rel(p, root), "kind": "directory",
            "reason": "link" if is_link_dir(p) else "noise-name",
        }
        for p in skipped_dirs
    }
    skipped = sorted(dir_records.values(), key=lambda r: r["path"]) + skipped

    counts = {lane: len(by_lane[lane]) for lane in LANES}
    counts["skipped"] = len(skipped)
    counts["deduplicated"] = len(deduplicated)
    counts["ownedByOtherLane"] = owned_elsewhere
    return {
        "projectRoot": str(workspace),
        "scanRoot": str(root),
        "fixMode": config["audit"]["fix_mode"],
        "notes": list(config["notes"]),
        "lanes": lanes,
        "skipped": skipped,
        "deduplicated": deduplicated,
        "counts": counts,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover every audit lane's subjects under one pruning policy.")
    parser.add_argument("--cwd", default=None, help="scan root (default: the current directory)")
    parser.add_argument("--out", default=None, help="write the JSON here instead of stdout")
    parser.add_argument(
        "--max-concurrent", type=positive_int, default=None, metavar="N",
        help="bound the agents in flight per fan-out; becomes maxConcurrent in every lane's args "
             "and is absent when not given",
    )
    parser.add_argument(
        "--scope", default="skills",
        help="cross-reference scan scope: skills (default), references, md, all; "
             "comma-combinable (references_audit.py --scope)",
    )
    parser.add_argument(
        "--sources", choices=references_audit.SOURCES_CHOICES, default=references_audit.SOURCES_CHOICES[0],
        help="which roots the cross-reference scan reports on (references_audit.py --sources)",
    )
    parser.add_argument(
        "--include-dir", action="append", default=None, metavar="NAME",
        help="directory NAME to walk into although pruned as noise (repeatable); "
             "falls back to MD_DOMAIN_INCLUDE_DIRS",
    )
    parser.add_argument("--user-dir", default=None, help="user skills directory (default: ~/.claude/skills)")
    parser.add_argument("--plugins-dir", default=None, help="plugins directory (default: ~/.claude/plugins)")
    parser.add_argument("--config-dir", default=None, help="harness config dir for the plugin-cache citation scan")
    parser.add_argument("--skip-plugin-cache", action="store_true",
                        help="do not index installed plugin-cache skills as citation sources")
    parser.add_argument("--venv-python", default=None,
                        help="interpreter to name as refs.venvPython (default: the provisioned skills-kit venv)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    root = Path(args.cwd).resolve() if args.cwd else Path.cwd().resolve()
    try:
        result = build(
            root,
            max_concurrent=args.max_concurrent,
            include_dirs=discover_claude_md._resolve_include_dirs(args.include_dir),
            scope=args.scope,
            sources=args.sources,
            user_dir=Path(args.user_dir).expanduser() if args.user_dir else None,
            plugins_dir=Path(args.plugins_dir).expanduser() if args.plugins_dir else None,
            config_dir=Path(args.config_dir).expanduser() if args.config_dir else None,
            skip_plugin_cache=args.skip_plugin_cache,
            venv_python_override=args.venv_python,
        )
    except OwnershipCollision as exc:
        print(f"discover_all: {exc}", file=sys.stderr)
        return 2
    except DiscoverAllError as exc:
        print(f"discover_all: {exc}", file=sys.stderr)
        return 1

    text = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)

    counts = result["counts"]
    print(
        "discover_all: " + " ".join(f"{lane}={counts[lane]}" for lane in LANES)
        + f" skipped={counts['skipped']} deduplicated={counts['deduplicated']}"
        + f" ownedByOtherLane={counts['ownedByOtherLane']} root={result['projectRoot']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    # resolve_standards.main needs the skills-kit plugin venv (pyyaml, and the
    # bootstrap_lib link lane_models imports). The vendored guard is stdlib-only
    # and re-execs under that venv (a no-op when already there) before main().
    bootstrap_guard.reexec_under_plugin_venv("skills-kit")
    raise SystemExit(main())
