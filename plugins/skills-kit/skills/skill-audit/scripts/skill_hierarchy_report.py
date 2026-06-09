#!/usr/bin/env python3
"""skill_hierarchy_report.py -- emit an HTML hierarchy report of every SKILL.md
discoverable under user, project, and installed-plugin roots.

Preferred entry point:

    /skill-audit hierarchy

This script is the backend for that command and also remains directly
runnable for dev iteration or scripting. The `render_html(corpus)` function
is imported by `report.py` when the `hierarchy` subcommand is selected.

Hierarchy:

    All (N)
      |- User skills (N)         -- ~/.claude/skills/
      |- Project skills (N)      -- <project>/.claude/skills/
      `- Plugins (N plugins)     -- enumerated from installed_plugins.json
            |- <marketplace-1>
            |    |- <marketplace-1>:<plugin-a> (N)
            |    `- <marketplace-1>:<plugin-b> (N)
            `- <marketplace-2>
                 `- ...

Sections are HTML <details>/<summary>, so the report is interactive without
any JavaScript. Expanding a plugin (or User/Project) section reveals a table
of every skill in that scope. Columns are the union of every frontmatter key
seen across the section's skills, with `name` first and `description` last;
table width is intentionally unconstrained (assumes an ultra-wide monitor).

Standalone usage:
    python skill_hierarchy_report.py [--project-root PATH] [--out PATH]
                                     [--installed-plugins PATH]
                                     [--user-skills PATH]

All flags are optional. Defaults assume the standard Claude Code install
layout. When run without --out the report goes to:
    <project-root>/tmp/skill-hierarchy.html

The resolved output path is always echoed to stdout.

Discovery is delegated to the plugin-level `skills_kit_lib.corpus` module so
this script and the markdown roster (also under /skill-audit) enumerate the
same corpus.
"""

import argparse
import html
import json
import os
import sys
from collections import OrderedDict
from pathlib import Path

from skills_kit_lib.corpus import (
    PluginEntry,
    SkillCorpus,
    SkillRecord,
    discover_corpus,
)


HOME = Path(os.path.expanduser("~"))
DEFAULT_USER_SKILLS = HOME / ".claude" / "skills"
DEFAULT_INSTALLED_PLUGINS_JSON = HOME / ".claude" / "plugins" / "installed_plugins.json"


# ----------------------------------------------------------------------------
# HTML rendering
# ----------------------------------------------------------------------------


# Hover-tooltip content for each canonical skill-type, authored from
# skill-authoring/scripts/schemas.py (the source of truth on required fields)
# and framework.md (the source of truth on purpose/audit/prohibits).
SKILL_TYPE_TOOLTIPS: dict[str, dict] = {
    "reference-skill": {
        "purpose": "Collects facts, conventions, or syntax the agent retrieves on demand. Optimized for lookup, not procedure.",
        "audit": "Drop a fresh agent into a topic -- does it find and apply the right fact?",
        "prohibits": "adversarial pressure testing; rule + counter pairs; workflow checklists",
        "frontmatter_required": "name, description",
        "contract_required": (
            "reference_skill.identity (one sentence); "
            "reference_skill.scope.covers (list); reference_skill.scope.excludes (list); "
            "reference_skill.facts (>=1 record with id + summary + keywords (>=3) + detail; "
            "at least one fact carries gotchas and at least one carries example)"
        ),
    },
    "pattern-skill": {
        "purpose": "Names a reusable design pattern with recognition criteria and counter-examples. Teaches when to apply AND when not to.",
        "audit": "Does the agent recognize when to apply and when not? Counter-examples must be exercised.",
        "prohibits": "utility bundle; workflow checklist; rule + counter pairs",
        "frontmatter_required": "name, description",
        "contract_required": (
            "pattern_skill.identity; pattern_skill.scope.covers; pattern_skill.scope.excludes; "
            "pattern_skill.patterns (>=1 record with id + name + keywords (>=3) + problem + "
            "mechanic + why + apply_when (>=1 signal/example) + do_not_apply_when (>=1 signal/counter_example) + "
            "examples (>=1 title/before/after))"
        ),
    },
    "technique-skill": {
        "purpose": "Procedural how-to. Ordered steps to accomplish a task; user-only slash-commands also live here.",
        "audit": "Can the agent apply the method to a novel scenario? Try variation and missing-information tests.",
        "prohibits": "adversarial pressure testing (that belongs to discipline-skill)",
        "frontmatter_required": "name, description",
        "contract_required": (
            "technique_skill.identity; technique_skill.scope.covers/excludes; "
            "technique_skill.techniques (>=1 record with id + name + keywords (>=3) + goal + "
            "steps (>=1 ordered step, each with n + action) + gotchas (>=1))"
        ),
    },
    "capability-skill": {
        "purpose": "Wraps an external capability (tool / MCP server / API / service / IDE / framework / harness). Conceptually IS-A technique-skill with extra structure for the external thing.",
        "audit": "Does the capability surface enumerate the operations a user might invoke? Does the layering manifest match actual content allocation (CLAUDE.md / SKILL.md / references)?",
        "prohibits": "adversarial pressure testing; rule + counter pairs; techniques: at root (capabilities: subsumes it); index: at root (members + Conditional Loading is the canonical shape)",
        "frontmatter_required": "name, description",
        "contract_required": (
            "capability_skill.identity; capability_skill.scope.covers/excludes; "
            "external_capability (kind + name + description); "
            "layering (claude_md + skill_md + references lists; skill_md non-empty); "
            "capabilities (>=1 record with id + keywords (>=3) + user_objective + operation); "
            "gotchas (>=1, capability-skill-level)"
        ),
    },
    "discipline-skill": {
        "purpose": "Enforces a rule under pressure. Pairs each rule with named rationalization counters so the rule survives time + sunk-cost + fatigue.",
        "audit": "Does the rule hold under combined pressures? Run an adversarial subagent.",
        "prohibits": "hedging language in rule statements (\"should\", \"might\", \"try to\", \"consider\", \"usually\", \"prefer\")",
        "frontmatter_required": "name, description",
        "contract_required": (
            "discipline_skill.identity; discipline_skill.scope.covers/excludes; "
            "target (type + ref); "
            "rules (>=1 record with id + keywords + statement (non-hedging) + why + "
            "counters (>=1 excuse/reality/observed_in) + red_flags (>=1)); "
            "pressure_test (baseline + green + refactor list (>=1 loophole/closed_by))"
        ),
    },
    "domain-skill": {
        "purpose": "Container for a knowledge area. Aggregates references and member skills with vocabulary, capability surface, and a conditional-loading index.",
        "audit": "Does a fresh agent operate fluently in vocabulary and conventions, find the right member skill on a trigger, and recognize the boundary with declared companions? Is the index complete relative to members on disk?",
        "prohibits": "monolithic prose content (meaty workflows belong in member skills); index without orientation",
        "frontmatter_required": "name, description",
        "contract_required": (
            "domain_skill.identity; "
            "companions.siblings (list; or note explicitly \"no siblings\"); "
            "scope.covers/excludes; "
            "orientation.summary (one substantive section beyond the index); "
            "index.references (>=1 record with id + path + keywords + summary)"
        ),
    },
}


def render_skill_type_tooltip_html(skill_type: str) -> str:
    info = SKILL_TYPE_TOOLTIPS.get(skill_type)
    if not info:
        return ""
    rows = [
        ("Purpose", info["purpose"]),
        ("Audit criterion", info["audit"]),
        ("Prohibits", info["prohibits"]),
        ("Required frontmatter", info["frontmatter_required"]),
        ("Required contract fields", info["contract_required"]),
    ]
    body = "".join(
        f'<div class="tooltip-row"><span class="tooltip-label">{html.escape(label)}:</span> '
        f'<span class="tooltip-value">{html.escape(value)}</span></div>'
        for label, value in rows
    )
    title = f'<div class="tooltip-title">{html.escape(skill_type)}</div>'
    return f'<span class="skill-type-tooltip" role="tooltip">{title}{body}</span>'


CSS = """
:root {
  color-scheme: light dark;
  --fg: #1c1c1c;
  --bg: #fafafa;
  --muted: #6a6a6a;
  --accent: #1f6feb;
  --border: #d0d0d0;
  --table-stripe: #f0f0f0;
  --code-bg: #eef2f5;
}
@media (prefers-color-scheme: dark) {
  :root {
    --fg: #e6e6e6;
    --bg: #1c1c1f;
    --muted: #9a9a9a;
    --accent: #4ea1ff;
    --border: #3a3a3f;
    --table-stripe: #25252a;
    --code-bg: #2a2a30;
  }
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  background: var(--bg);
  color: var(--fg);
  margin: 0;
  padding: 24px 32px;
  line-height: 1.45;
}
h1 { margin-top: 0; }
p.meta { color: var(--muted); margin-top: 4px; }
details { margin: 6px 0; }
details > summary {
  cursor: pointer;
  user-select: none;
  padding: 6px 10px;
  border-radius: 4px;
  font-weight: 600;
}
details > summary:hover { background: var(--table-stripe); }
details.level-all > summary { font-size: 1.15rem; }
details.level-group > summary { font-size: 1.05rem; }
details.level-marketplace > summary { font-size: 1.0rem; font-weight: 600; }
details.level-plugin > summary { font-weight: 500; }
details > div.body {
  padding-left: 18px;
  border-left: 2px solid var(--border);
  margin-left: 6px;
}
.count {
  color: var(--muted);
  font-weight: 400;
  margin-left: 6px;
  font-variant-numeric: tabular-nums;
}
.scope-name { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.empty {
  color: var(--muted);
  font-style: italic;
  padding: 6px 0;
}
table.skills {
  border-collapse: collapse;
  margin: 10px 0 18px 0;
  font-size: 0.9rem;
  /* Intentionally no max-width: assume ultra-wide viewing. */
}
table.skills th, table.skills td {
  border: 1px solid var(--border);
  padding: 6px 10px;
  text-align: left;
  vertical-align: top;
  white-space: pre-wrap;
}
table.skills th {
  background: var(--table-stripe);
  position: sticky;
  top: 0;
  font-weight: 600;
}
table.skills td.skill-name {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  white-space: nowrap;
}
table.skills td.desc { min-width: 320px; }
table.skills td.token-count {
  text-align: right;
  font-variant-numeric: tabular-nums;
  color: var(--muted);
}
table.skills tr:nth-child(even) td { background: var(--table-stripe); }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.path { color: var(--muted); font-size: 0.85rem; }

/* skill-type cell tooltip */
.skill-type-cell {
  position: relative;
  cursor: help;
  border-bottom: 1px dotted var(--muted);
}
.skill-type-cell .skill-type-tooltip {
  display: none;
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  z-index: 50;
  background: var(--bg);
  color: var(--fg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 12px 14px;
  min-width: 520px;
  max-width: 760px;
  font-size: 0.85rem;
  font-weight: normal;
  line-height: 1.45;
  white-space: normal;
  box-shadow: 0 6px 20px rgba(0,0,0,0.25);
  text-align: left;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
.skill-type-cell:hover .skill-type-tooltip,
.skill-type-cell:focus .skill-type-tooltip,
.skill-type-cell:focus-within .skill-type-tooltip {
  display: block;
}
.skill-type-tooltip .tooltip-title {
  font-weight: 600;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  margin-bottom: 6px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--border);
  color: var(--accent);
}
.skill-type-tooltip .tooltip-row { margin: 4px 0; }
.skill-type-tooltip .tooltip-label {
  color: var(--muted);
  font-weight: 600;
  margin-right: 4px;
}
.skill-type-tooltip .tooltip-value { color: var(--fg); }
"""


def _stringify(value: object) -> str:
    """Coerce a frontmatter value to a display string.

    PyYAML returns native types (bool / int / list / dict). The HTML cell
    needs a string; this function gives a consistent rendering across types.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return ", ".join(_stringify(v) for v in value)
    if isinstance(value, dict):
        try:
            return json.dumps(value, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _esc(value: object) -> str:
    return html.escape(_stringify(value), quote=False)


def union_columns(skills: list[dict]) -> list[str]:
    """Return the ordered union of frontmatter keys present in `skills`.

    `name` first, `description` last; everything else alphabetical between.
    """
    keys = set()
    for s in skills:
        keys.update(s["fm"].keys())
    middle = sorted(keys - {"name", "description"})
    columns = ["name"]
    columns.extend(middle)
    columns.append("description")
    return columns


def render_skill_table(skills: list[dict]) -> str:
    if not skills:
        return '<p class="empty">No skills.</p>'
    columns = union_columns(skills)
    headers_html = "".join(f"<th>{_esc(c)}</th>" for c in columns)

    rows_html_parts = []
    for s in sorted(skills, key=lambda r: r["display_name"].lower()):
        fm = s["fm"]
        cells = []
        for col in columns:
            if col == "name":
                cells.append(f'<td class="skill-name">{_esc(s["display_name"])}</td>')
            elif col == "description":
                cells.append(f'<td class="desc">{_esc(fm.get(col, ""))}</td>')
            elif col == "skill-type":
                value = fm.get(col, "")
                tooltip_html = render_skill_type_tooltip_html(_stringify(value))
                if tooltip_html:
                    cells.append(
                        f'<td><span class="skill-type-cell" tabindex="0">'
                        f"{_esc(value)}{tooltip_html}</span></td>"
                    )
                else:
                    cells.append(f"<td>{_esc(value)}</td>")
            else:
                cells.append(f"<td>{_esc(fm.get(col, ''))}</td>")
        rows_html_parts.append(f"<tr>{''.join(cells)}</tr>")
    rows_html = "\n".join(rows_html_parts)
    return (
        '<table class="skills">\n'
        f"<thead><tr>{headers_html}</tr></thead>\n"
        f"<tbody>\n{rows_html}\n</tbody>\n"
        "</table>"
    )


def _to_view_dict(rec: SkillRecord, qualifier: str | None) -> dict:
    """Adapt a corpus SkillRecord to the renderer's per-skill view dict."""
    display = f"{qualifier}:{rec.skill_name}" if qualifier else rec.skill_name
    return {
        "display_name": display,
        "skill_name": rec.skill_name,
        "path": rec.path,
        "fm": rec.frontmatter,
    }


def _group_plugins_by_marketplace(
    plugins: list[PluginEntry],
) -> "OrderedDict[str, list[PluginEntry]]":
    """Return marketplace -> [PluginEntry, ...], sorted by marketplace then plugin name.

    Plugins with no skills are dropped; marketplaces with no skill-bearing
    plugins are dropped.
    """
    bucket: dict[str, list[PluginEntry]] = {}
    for p in plugins:
        if not p.skills:
            continue
        bucket.setdefault(p.marketplace, []).append(p)
    ordered: OrderedDict[str, list[PluginEntry]] = OrderedDict()
    for mkt in sorted(bucket.keys(), key=str.lower):
        ordered[mkt] = sorted(bucket[mkt], key=lambda p: p.name.lower())
    return ordered


def render_html(corpus: SkillCorpus) -> str:
    user_views = [_to_view_dict(r, None) for r in corpus.user]
    project_views = [_to_view_dict(r, None) for r in corpus.project]
    marketplaces = _group_plugins_by_marketplace(corpus.plugins)

    plugin_skill_count = sum(
        len(p.skills) for plugins in marketplaces.values() for p in plugins
    )
    plugin_count = sum(len(plugins) for plugins in marketplaces.values())
    total = len(user_views) + len(project_views) + plugin_skill_count

    parts: list[str] = []
    parts.append("<!doctype html>")
    parts.append('<html lang="en"><head><meta charset="utf-8">')
    parts.append("<title>Skill Hierarchy Report</title>")
    parts.append(f"<style>{CSS}</style>")
    parts.append("</head><body>")
    parts.append("<h1>Skill Hierarchy Report</h1>")
    parts.append(
        '<p class="meta">'
        "Generated by <code>skill_hierarchy_report.py</code>. "
        "Sections collapse and expand. Tables are not width-limited."
        "</p>"
    )

    # All
    parts.append(
        '<details class="level-all" open><summary>All <span class="count">'
        f"({total} skills)</span></summary>"
    )
    parts.append('<div class="body">')

    # User skills
    parts.append(
        '<details class="level-group"><summary>User skills '
        f'<span class="count">({len(user_views)} skills)</span></summary>'
    )
    parts.append('<div class="body">')
    parts.append(f'<p class="path">Source: <code>{_esc(corpus.user_skills_root)}</code></p>')
    parts.append(render_skill_table(user_views))
    parts.append("</div></details>")

    # Project skills
    parts.append(
        '<details class="level-group"><summary>Project skills '
        f'<span class="count">({len(project_views)} skills)</span></summary>'
    )
    parts.append('<div class="body">')
    if corpus.project_skills_root is not None:
        parts.append(
            f'<p class="path">Source: <code>{_esc(corpus.project_skills_root)}</code></p>'
        )
    parts.append(render_skill_table(project_views))
    parts.append("</div></details>")

    # Plugins -- grouped by marketplace
    parts.append(
        '<details class="level-group"><summary>Plugins '
        f'<span class="count">({plugin_skill_count} skills)</span></summary>'
    )
    parts.append('<div class="body">')
    if not marketplaces:
        parts.append('<p class="empty">No installed plugins with skills.</p>')
    for marketplace, plugin_list in marketplaces.items():
        mkt_skill_count = sum(len(p.skills) for p in plugin_list)
        parts.append(
            '<details class="level-marketplace"><summary>'
            f'<span class="scope-name">{_esc(marketplace)}</span> '
            f'<span class="count">({mkt_skill_count} skills)</span>'
            "</summary>"
        )
        parts.append('<div class="body">')
        for plugin in plugin_list:
            qualified = f"{plugin.marketplace}:{plugin.name}"
            views = [_to_view_dict(r, qualified) for r in plugin.skills]
            parts.append(
                '<details class="level-plugin"><summary>'
                f'<span class="scope-name">{_esc(qualified)}</span> '
                f'<span class="count">({len(views)} skills)</span>'
                "</summary>"
            )
            parts.append('<div class="body">')
            parts.append(render_skill_table(views))
            parts.append("</div></details>")
        parts.append("</div></details>")
    parts.append("</div></details>")

    parts.append("</div></details>")
    parts.append("</body></html>")
    return "\n".join(parts)


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------


def _force_utf8_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass


def main(argv: list[str]) -> int:
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Project root (the dir containing .claude/skills/). Defaults to CWD.",
    )
    parser.add_argument(
        "--user-skills",
        type=Path,
        default=DEFAULT_USER_SKILLS,
        help=f"User skills dir (default: {DEFAULT_USER_SKILLS}).",
    )
    parser.add_argument(
        "--installed-plugins",
        type=Path,
        default=DEFAULT_INSTALLED_PLUGINS_JSON,
        help=f"installed_plugins.json (default: {DEFAULT_INSTALLED_PLUGINS_JSON}).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output HTML path (default: <project-root>/tmp/skill-hierarchy.html).",
    )
    args = parser.parse_args(argv)

    project_root = (args.project_root or Path.cwd()).resolve()
    out_path = (args.out or (project_root / "tmp" / "skill-hierarchy.html")).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    corpus = discover_corpus(
        project_root=project_root,
        user_skills_root=args.user_skills,
        installed_plugins_json=args.installed_plugins,
    )
    html_text = render_html(corpus)
    out_path.write_text(html_text, encoding="utf-8")

    marketplaces = _group_plugins_by_marketplace(corpus.plugins)
    plugin_count = sum(len(plugins) for plugins in marketplaces.values())
    plugin_skill_count = sum(
        len(p.skills) for plugins in marketplaces.values() for p in plugins
    )
    print(f"Wrote {out_path}")
    print(f"  user skills:    {len(corpus.user)}")
    print(f"  project skills: {len(corpus.project)}")
    print(f"  marketplaces:   {len(marketplaces)}")
    print(f"  plugins:        {plugin_count} ({plugin_skill_count} skills)")
    print(f"  total:          {corpus.total_skills}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
