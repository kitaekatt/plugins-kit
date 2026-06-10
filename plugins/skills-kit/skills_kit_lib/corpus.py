"""SKILL.md corpus discovery across user/project/plugin tiers.

Single source of truth for "what skills exist in this session's universe?",
consumed by skill-audit's report.py and skill_hierarchy_report.py.

The corpus has three tiers:
    - User skills    ~/.claude/skills/**/SKILL.md
    - Project skills <project_root>/.claude/skills/**/SKILL.md
    - Plugin skills  per ~/.claude/plugins/installed_plugins.json,
                     one entry per active install
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml as _pyyaml
    HAVE_YAML = True
except ImportError:
    _pyyaml = None
    HAVE_YAML = False

from .document_walker import collect_yaml_units
from .markdown_heuristics import FRONTMATTER_RE, parse_frontmatter
from .schema_registry import SKILL_TYPE_ROOTS


# Contract roots for body-type detection: alias of the registry's skill-type
# roots (single source of truth lives in schema_registry).
CONTRACT_ROOTS = SKILL_TYPE_ROOTS


@dataclass
class SkillRecord:
    """One SKILL.md, parsed."""

    path: Path
    skill_name: str
    frontmatter: dict = field(default_factory=dict)
    body_contract: dict | None = None


@dataclass
class PluginEntry:
    name: str
    marketplace: str
    version: str
    install_path: Path
    skills: list[SkillRecord] = field(default_factory=list)


@dataclass
class SkillCorpus:
    user: list[SkillRecord]
    project: list[SkillRecord]
    plugins: list[PluginEntry]

    user_skills_root: Path
    project_skills_root: Path | None
    installed_plugins_json: Path

    @property
    def total_skills(self) -> int:
        return (
            len(self.user)
            + len(self.project)
            + sum(len(p.skills) for p in self.plugins)
        )


def parse_skill_md(path: Path) -> SkillRecord | None:
    """Read and parse one SKILL.md. Returns None on read failure.

    Body-contract extraction goes through document_walker (the canonical
    fenced-yaml walker): ALL ```yaml/```yml blocks are walked, and the
    contract is the first unit carrying a skill-type root -- so a document
    whose first fence is an example no longer shadows the real contract.
    Falls back to the first recognized (portable-unit) block when no
    skill-type root is present anywhere.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    fm: dict = {}
    body_text = text
    m = FRONTMATTER_RE.match(text)
    if m:
        body_text = text[m.end():]
        if HAVE_YAML:
            rec = parse_frontmatter(text, mode="full")
            if rec is not None:
                fm = rec.fields

    body_contract: dict | None = None
    if HAVE_YAML:
        units, _ = collect_yaml_units(body_text)
        for root, data in units:
            if root in CONTRACT_ROOTS:
                body_contract = data
                break
        if body_contract is None and units:
            body_contract = units[0][1]

    return SkillRecord(
        path=path,
        skill_name=path.parent.name,
        frontmatter=fm,
        body_contract=body_contract,
    )


def detect_skill_type(record: SkillRecord) -> tuple[str, str]:
    """Return (skill_type, variant).

    skill_type: a canonical skill-type slug (`reference-skill`, etc.), or
                `(unknown)` when neither frontmatter nor body contract declares one.
    variant:    `user-only` / `auto` for technique-skill, else empty string.
    """
    fm = record.frontmatter or {}
    body = record.body_contract or {}

    declared = str(fm.get("skill-type") or "").strip().lower()

    body_type = ""
    if isinstance(body, dict):
        for root in CONTRACT_ROOTS:
            if root in body:
                body_type = root.replace("_", "-")
                break

    skill_type = declared or body_type or "(unknown)"

    variant = ""
    if skill_type == "technique-skill":
        block = body.get("technique_skill") if isinstance(body, dict) else None
        trigger = ""
        if isinstance(block, dict):
            trigger = str(block.get("trigger_model") or "").strip().lower()
        variant = "user-only" if trigger == "user-only" else "auto"

    return skill_type, variant


def _find_skill_mds(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    flat = sorted(root.glob("*/SKILL.md"))
    if flat:
        return flat
    return sorted(root.rglob("SKILL.md"))


def _records_from(paths: list[Path]) -> list[SkillRecord]:
    out: list[SkillRecord] = []
    for p in paths:
        rec = parse_skill_md(p)
        if rec is not None:
            out.append(rec)
    return out


def discover_corpus(
    home: Path | None = None,
    project_root: Path | None = None,
    user_skills_root: Path | None = None,
    installed_plugins_json: Path | None = None,
) -> SkillCorpus:
    """Walk all three tiers and return a single SkillCorpus."""
    home = home or Path.home()
    user_skills_root = user_skills_root or (home / ".claude" / "skills")
    installed_plugins_json = (
        installed_plugins_json or home / ".claude" / "plugins" / "installed_plugins.json"
    )

    project_skills_root: Path | None = None
    project: list[SkillRecord] = []
    if project_root is not None:
        project_skills_root = project_root / ".claude" / "skills"
        project = _records_from(_find_skill_mds(project_skills_root))

    user = _records_from(_find_skill_mds(user_skills_root))

    plugins: list[PluginEntry] = []
    if installed_plugins_json.is_file():
        try:
            data = json.loads(installed_plugins_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        for key, installs in (data.get("plugins") or {}).items():
            if not installs:
                continue
            if "@" in key:
                plugin_name, marketplace = key.split("@", 1)
            else:
                plugin_name, marketplace = key, "(unknown)"
            for install in installs:
                install_path = Path(install.get("installPath", ""))
                version = str(install.get("version", ""))
                skills_root = install_path / "skills"
                skills = _records_from(_find_skill_mds(skills_root))
                plugins.append(
                    PluginEntry(
                        name=plugin_name,
                        marketplace=marketplace,
                        version=version,
                        install_path=install_path,
                        skills=skills,
                    )
                )

    return SkillCorpus(
        user=user,
        project=project,
        plugins=plugins,
        user_skills_root=user_skills_root,
        project_skills_root=project_skills_root,
        installed_plugins_json=installed_plugins_json,
    )
