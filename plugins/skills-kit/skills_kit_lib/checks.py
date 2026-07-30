"""Corpus-level audit checks.

These checks operate over the union of (registry, file-system) rather than
a single SKILL.md. The per-SKILL audit lives in audit.py; this module is
for checks that walk the registry against owner docs and cross-source rules.

The owner-doc check is the primary anti-drift guard: every registered schema
declares an owner_doc, and this check asserts each owner doc contains a valid
instance of its schema. If the schema changes incompatibly, the owner doc's
example fails; if the owner doc drifts from the schema, validation flags it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .corpus import parse_skill_md
from .document_walker import collect_yaml_units
from .schema_engine import validate
from .schema_registry import OWNER_DOCS, SCHEMAS_BY_ROOT


@dataclass
class OwnerDocResult:
    root: str
    owner_doc: str
    status: str  # pass | missing-file | missing-instance | invalid-instance
    message: str = ""
    fails: list = None

    def __post_init__(self):
        if self.fails is None:
            self.fails = []


def plugin_root() -> Path:
    """Resolve the plugin-root path (one level above skills_kit_lib/)."""
    return Path(__file__).resolve().parent.parent


def check_schema_owner_docs_validate(root: Path | None = None) -> list[OwnerDocResult]:
    """For each registered schema with an owner_doc, assert the owner doc
    contains a valid instance of the schema's root key.

    Returns one OwnerDocResult per registered schema with an owner_doc.
    """
    root = root or plugin_root()
    results: list[OwnerDocResult] = []

    for unit_root, owner_doc in OWNER_DOCS.items():
        schema = SCHEMAS_BY_ROOT.get(unit_root)
        if schema is None:
            continue
        path = root / owner_doc
        if not path.exists():
            results.append(OwnerDocResult(
                root=unit_root,
                owner_doc=owner_doc,
                status="missing-file",
                message=f"owner_doc path does not exist: {path}",
            ))
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            results.append(OwnerDocResult(
                root=unit_root,
                owner_doc=owner_doc,
                status="missing-file",
                message=f"could not read owner_doc: {e}",
            ))
            continue

        units, _, _ = collect_yaml_units(text)
        instances = [data for (r, data) in units if r == unit_root]
        if not instances:
            results.append(OwnerDocResult(
                root=unit_root,
                owner_doc=owner_doc,
                status="missing-instance",
                message=f"owner_doc contains no `{unit_root}:` block",
            ))
            continue

        # Validate each instance; require all to pass.
        all_fails: list = []
        for i, inst in enumerate(instances):
            fails, _ = validate(inst, schema)
            for path_, msg in fails:
                all_fails.append((f"instance[{i}].{path_}", msg))

        if all_fails:
            results.append(OwnerDocResult(
                root=unit_root,
                owner_doc=owner_doc,
                status="invalid-instance",
                message=f"{len(all_fails)} validation failures across {len(instances)} instance(s)",
                fails=all_fails,
            ))
        else:
            results.append(OwnerDocResult(
                root=unit_root,
                owner_doc=owner_doc,
                status="pass",
                message=f"{len(instances)} instance(s) validate",
            ))

    return results


def render_owner_doc_results(results: list[OwnerDocResult]) -> str:
    """Format owner-doc results as a human-readable text report."""
    lines: list[str] = []
    lines.append("== Schema owner-doc validation ==")
    for r in results:
        suffix = f" -- {r.message}" if r.message else ""
        lines.append(f"  [{r.status}] {r.root} <- {r.owner_doc}{suffix}")
        for path, msg in r.fails:
            lines.append(f"      {path}: {msg}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Domain / capability member-reference resolution
# ---------------------------------------------------------------------------


@dataclass
class MemberRefResult:
    domain: str          # name of the domain- or capability-skill declaring members
    domain_path: str     # plugin-root-relative-ish path to its SKILL.md
    status: str          # pass | unresolved | skipped
    unresolved: list = None  # list of (member_name, ref) that did not resolve
    message: str = ""

    def __post_init__(self):
        if self.unresolved is None:
            self.unresolved = []


def repo_root() -> Path:
    """Resolve the repository root (plugins/skills-kit -> plugins -> repo)."""
    return plugin_root().parent.parent


def _normalize_ref(ref: str) -> str:
    """Strip a leading slash and any plugin qualifier from a member ref.

    `/md-domain` -> `md-domain`
    `skills-kit:md-domain` -> `md-domain`
    `ue-python-api` -> `ue-python-api`
    """
    r = ref.strip()
    if r.startswith("/"):
        r = r[1:]
    if ":" in r:
        r = r.split(":", 1)[1]
    return r


def _known_skill_names(skill_mds: list[Path]) -> set[str]:
    """Build the resolvable name pool from on-disk SKILL.md files: each skill's
    directory name plus its frontmatter `name:` (when present)."""
    names: set[str] = set()
    for p in skill_mds:
        names.add(p.parent.name)
        rec = parse_skill_md(p)
        if rec is not None:
            fm_name = rec.frontmatter.get("name")
            if isinstance(fm_name, str) and fm_name:
                names.add(fm_name)
    return names


def _members_of(body_contract: dict | None) -> list:
    """Extract the member list from a parsed SKILL.md body contract, handling
    both nesting shapes: domain_skill.index.members[] and capability_skill.members[]."""
    if not isinstance(body_contract, dict):
        return []
    ds = body_contract.get("domain_skill")
    if isinstance(ds, dict):
        idx = ds.get("index")
        if isinstance(idx, dict) and isinstance(idx.get("members"), list):
            return idx["members"]
    cs = body_contract.get("capability_skill")
    if isinstance(cs, dict) and isinstance(cs.get("members"), list):
        return cs["members"]
    return []


def check_domain_members_resolve(root: Path | None = None) -> list[MemberRefResult]:
    """For every domain-skill (index.members[]) and capability-skill (members[])
    in the repo, assert each declared member ref/name resolves to a real skill
    on disk.

    Catches mis-pointed or dangling members -- a reorg that re-wires members can
    leave a `ref:` pointing at a skill that was renamed, moved, or never created.
    Resolves against the union of all on-disk skill names (dir name + frontmatter
    name) across every plugin, so same-plugin and (rare) cross-plugin refs both
    resolve. Degrades to `skipped` when a contract cannot be parsed (pyyaml
    absent), consistent with the audit's contract-staged state.
    """
    root = root or repo_root()
    skill_mds = sorted(root.glob("plugins/*/skills/*/SKILL.md"))
    known = _known_skill_names(skill_mds)
    results: list[MemberRefResult] = []

    for p in skill_mds:
        rec = parse_skill_md(p)
        domain_name = p.parent.name
        path_str = str(p).replace("\\", "/")
        if rec is None or rec.body_contract is None:
            # Only report a skip if this skill *looks* like it declares members
            # in raw text; otherwise stay silent (not a member-declaring skill).
            continue
        members = _members_of(rec.body_contract)
        if not members:
            continue

        unresolved: list = []
        for m in members:
            if not isinstance(m, dict):
                continue
            ref = str(m.get("ref") or "")
            name = str(m.get("name") or "")
            candidates = {c for c in (_normalize_ref(ref), _normalize_ref(name)) if c}
            if not (candidates & known):
                unresolved.append((name or "<no-name>", ref or "<no-ref>"))

        if unresolved:
            results.append(MemberRefResult(
                domain=domain_name, domain_path=path_str, status="unresolved",
                unresolved=unresolved,
                message=f"{len(unresolved)} member ref(s) do not resolve to a skill on disk",
            ))
        else:
            results.append(MemberRefResult(
                domain=domain_name, domain_path=path_str, status="pass",
                message=f"{len(members)} member(s) resolve",
            ))

    return results


def render_member_results(results: list[MemberRefResult]) -> str:
    """Format member-resolution results as a human-readable text report."""
    lines: list[str] = []
    lines.append("== Domain/capability member resolution ==")
    if not results:
        lines.append("  (no domain- or capability-skills declare members)")
    for r in results:
        suffix = f" -- {r.message}" if r.message else ""
        lines.append(f"  [{r.status}] {r.domain} ({r.domain_path}){suffix}")
        for name, ref in r.unresolved:
            lines.append(f"      member '{name}' ref '{ref}' -> not found on disk")
    return "\n".join(lines)


def _cli(argv: list[str]) -> int:
    """Run all corpus checks; exit non-zero if any fails. Optional first arg is
    the repo root (defaults to the resolved repo root). Owner-doc paths are
    plugin-root-relative; member resolution scans the whole repo -- each check
    gets the root it expects."""
    repo = Path(argv[0]).resolve() if argv else repo_root()
    plug = repo / "plugins" / "skills-kit"
    owner = check_schema_owner_docs_validate(plug)
    members = check_domain_members_resolve(repo)
    print(render_owner_doc_results(owner))
    print(render_member_results(members))
    bad = [r for r in owner if r.status != "pass"]
    bad += [r for r in members if r.status == "unresolved"]
    print(f"\n=== corpus checks: {len(bad)} failure(s) ===")
    return 1 if bad else 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
