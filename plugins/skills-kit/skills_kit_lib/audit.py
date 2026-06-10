"""audit -- run deterministic contract checks against a SKILL.md.

Usage:
    python -m skills_kit_lib.audit <path-to-SKILL.md>
    python -m skills_kit_lib.audit <path-to-SKILL.md> --json

Emits a per-row verdict: pass / fail / judgment-required / n/a. Rows flagged
judgment-required are not deterministic at this level; the agent runs them
by hand against the contract in skill-authoring's framework.md.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from .document_walker import (
    HAVE_YAML,
    collect_yaml_units,
    extract_skill_type_unit,
)
from .markdown_heuristics import (
    Body,
    Frontmatter,
    CANONICAL_TYPES,
    count_ordered_steps,
    has_companion_declaration,
    has_conditional_loading,
    has_counter_example,
    has_excuse_reality_table,
    has_heading,
    has_lookup_table,
    has_recognition_marker,
    has_red_flags_list,
    has_red_green_refactor,
    has_step_tracker_invocation,
    has_tickbox_list,
    has_yaml_block,
    is_user_only,
    parse_body,
    parse_frontmatter,
    strip_code_fences,
)
from .schema_engine import validate
from .schema_registry import (
    PORTABLE_UNIT_ROOTS,
    SCHEMAS_BY_ROOT,
    SKILL_TYPE_ROOTS,
    detect_mixed_type_yaml,
    resolve_schema,
)


RESERVED_NAMES = {"anthropic", "claude"}

PASS = "pass"
FAIL = "fail"
JUDGMENT = "judgment-required"
NA = "n/a"


@dataclass
class CheckResult:
    row: str
    verdict: str
    note: str = ""


def has_identity_sentence(body_text: str) -> bool:
    after_h1 = re.split(r"^#\s+\S.+$", body_text, maxsplit=1, flags=re.MULTILINE)
    if len(after_h1) < 2:
        return False
    rest = after_h1[1].lstrip()
    first_para = rest.split("\n\n", 1)[0].strip()
    return bool(first_para) and "." in first_para and len(first_para) < 600


def check_universal(fm: Frontmatter | None, body: Body, skill_dir: Path) -> list[CheckResult]:
    out: list[CheckResult] = []
    if fm is None:
        out.append(CheckResult("frontmatter present", FAIL, "no leading --- block"))
        return out
    out.append(CheckResult("frontmatter present", PASS))

    if "name" not in fm.fields:
        out.append(CheckResult("frontmatter.name present", FAIL))
    else:
        name = fm.fields["name"]
        out.append(CheckResult("frontmatter.name present", PASS))
        out.append(CheckResult("name <= 64 chars", PASS if len(name) <= 64 else FAIL, f"len={len(name)}"))
        out.append(CheckResult(
            "name charset (lowercase/digits/hyphens)",
            PASS if re.fullmatch(r"[a-z0-9-]+", name) else FAIL,
            name,
        ))
        out.append(CheckResult("name not reserved", PASS if name not in RESERVED_NAMES else FAIL, name))

    if "description" not in fm.fields:
        out.append(CheckResult("frontmatter.description present", FAIL))
    else:
        desc = fm.fields["description"]
        out.append(CheckResult("frontmatter.description present", PASS))
        out.append(CheckResult(
            "description <= 160 chars",
            PASS if len(desc) <= 160 else FAIL,
            f"len={len(desc)}",
        ))
        desc_lc = desc.lower().lstrip()
        directive = desc_lc.startswith("use when") or desc_lc.startswith("invoke when")
        out.append(CheckResult(
            "directive form ('Use when...' / 'Invoke when...')",
            PASS if directive else FAIL,
            "description should open with 'Use when...' or 'Invoke when...'" if not directive else "",
        ))
        excl = bool(re.search(r"\bdo not use\b|\bdon'?t use\b", desc, re.IGNORECASE))
        out.append(CheckResult(
            "exclusion clause (Do NOT use for...)",
            PASS if excl else FAIL,
            "no 'do not use' phrase in description" if not excl else "",
        ))

    if "skill-type" not in fm.fields:
        out.append(CheckResult(
            "skill-type advisory tag",
            JUDGMENT,
            "no skill-type frontmatter; agent infers type from content",
        ))
    else:
        val = fm.fields["skill-type"]
        if val in CANONICAL_TYPES:
            out.append(CheckResult("skill-type value valid", PASS, val))
        else:
            out.append(CheckResult(
                "skill-type value valid",
                FAIL,
                f"got '{val}', expected one of {sorted(CANONICAL_TYPES)}",
            ))

    out.append(CheckResult("SKILL.md line count", PASS, str(body.lines)))
    out.append(CheckResult("SKILL.md token count (approx)", PASS, str(body.tokens_approx)))

    has_references = (skill_dir / "references").exists()
    body_too_big = body.lines > 500 or body.tokens_approx > 3000
    if not body_too_big:
        out.append(CheckResult(
            "progressive disclosure (conditional)",
            NA,
            f"lines={body.lines}, tokens~{body.tokens_approx}",
        ))
    elif has_references:
        out.append(CheckResult(
            "progressive disclosure (conditional)",
            PASS,
            f"body large (lines={body.lines}, tokens~{body.tokens_approx}); references/ exists",
        ))
    else:
        # Size is a SIGNAL, not a verdict (framework Dec-11): a split is REQUIRED
        # only if a CRP-passing decomposition exists -- sections serve different
        # reading tasks. The mechanical check cannot evaluate CRP, so it must not
        # FAIL. It emits judgment-required: the agent runs the CRP test before any
        # split. A stub-plus-always-co-loaded-reference is a tool-call doubling,
        # not a context-efficiency win.
        out.append(CheckResult(
            "progressive disclosure (conditional)",
            JUDGMENT,
            f"body large (lines={body.lines}, tokens~{body.tokens_approx}) and no references/; "
            "run the CRP test (do sections serve different reading tasks?) before splitting -- "
            "size is a signal, not a verdict (Dec-11)",
        ))

    refs_dir = skill_dir / "references"
    if not refs_dir.exists():
        out.append(CheckResult("references one-hop-deep (ADP)", NA, "no references/ directory"))
    else:
        nested = list(refs_dir.glob("*/*.md"))
        if nested:
            rel = [str(p.relative_to(skill_dir)) for p in nested]
            out.append(CheckResult("references one-hop-deep (ADP)", FAIL, f"nested: {rel}"))
        else:
            out.append(CheckResult("references one-hop-deep (ADP)", PASS))

    # Match local references/X.md citations only; exclude cross-plugin refs.
    cited = set(re.findall(r"(?<![/:])references/([a-zA-Z0-9_\-]+\.md)", body.text))
    if not cited:
        out.append(CheckResult("references cited in body all exist", NA, "no references cited in body"))
    else:
        missing = [name for name in cited if not (skill_dir / "references" / name).exists()]
        if missing:
            out.append(CheckResult("references cited in body all exist", FAIL, f"missing: {missing}"))
        else:
            out.append(CheckResult("references cited in body all exist", PASS, f"checked {len(cited)} references"))

    return out


def check_reference_skill(body: Body, skill_dir: Path) -> list[CheckResult]:
    out: list[CheckResult] = []
    out.append(CheckResult(
        ">=1 example block",
        PASS if has_heading(body.text, "Example", "Examples") else JUDGMENT,
        "no 'Example' heading detected" if not has_heading(body.text, "Example", "Examples") else "",
    ))
    out.append(CheckResult(
        ">=1 gotcha block",
        PASS if has_heading(body.text, "Gotcha", "Gotchas", "Known gotchas") else JUDGMENT,
        "no 'Gotcha' heading detected" if not has_heading(body.text, "Gotcha", "Gotchas", "Known gotchas") else "",
    ))
    discipline_hit = has_red_green_refactor(body.text) or has_excuse_reality_table(body.text)
    out.append(CheckResult(
        "prohibited: discipline content (rule+counter, RED/GREEN/REFACTOR)",
        FAIL if discipline_hit else PASS,
        "discipline markers detected" if discipline_hit else "",
    ))
    out.append(CheckResult(
        "prohibited: workflow checklist",
        FAIL if has_tickbox_list(body.text) else PASS,
        "tickbox list present" if has_tickbox_list(body.text) else "",
    ))
    return out


def check_pattern_skill(body: Body, skill_dir: Path) -> list[CheckResult]:
    out: list[CheckResult] = []
    out.append(CheckResult(
        "recognition criteria block",
        PASS if has_recognition_marker(body.text) else JUDGMENT,
        "no 'recognize/recognition/applies when' marker" if not has_recognition_marker(body.text) else "",
    ))
    out.append(CheckResult(
        "counter-example(s) block",
        PASS if has_counter_example(body.text) else JUDGMENT,
        "no 'counter-example' or 'do NOT apply' marker" if not has_counter_example(body.text) else "",
    ))
    bundle_present = (skill_dir / "scripts").exists() or (skill_dir / "bin").exists()
    out.append(CheckResult(
        "prohibited: utility bundle",
        FAIL if bundle_present else PASS,
        "scripts/ or bin/ present" if bundle_present else "",
    ))
    out.append(CheckResult(
        "prohibited: workflow checklist",
        FAIL if has_tickbox_list(body.text) else PASS,
    ))
    out.append(CheckResult(
        "prohibited: rule + counter pairs",
        FAIL if has_excuse_reality_table(body.text) else PASS,
        "rationalization/excuse->reality detected" if has_excuse_reality_table(body.text) else "",
    ))
    return out


def check_technique_skill(body: Body, skill_dir: Path, fm: Frontmatter | None) -> list[CheckResult]:
    out: list[CheckResult] = []
    step_count = count_ordered_steps(body.text)
    out.append(CheckResult(
        "ordered-step body",
        PASS if step_count >= 1 else FAIL,
        f"{step_count} ordered-step entries detected",
    ))
    if step_count > 3:
        has_checklist = has_tickbox_list(body.text)
        has_tracker = has_step_tracker_invocation(body.text)
        signal_present = has_checklist or has_tracker
        if signal_present:
            via = "tickbox checklist" if has_checklist else "step-tracker invocation"
            note = f"{step_count} steps; satisfied via {via}"
        else:
            note = (f"{step_count} steps; neither tickbox checklist nor "
                    f"step-tracker invocation present")
        out.append(CheckResult(
            "explicit step-tracking (conditional, IF >3 steps): checklist OR tracker invocation",
            PASS if signal_present else FAIL,
            note,
        ))
    else:
        out.append(CheckResult(
            "explicit step-tracking (conditional, IF >3 steps): checklist OR tracker invocation",
            NA,
            f"only {step_count} steps",
        ))
    out.append(CheckResult(
        "prohibited: adversarial pressure testing",
        FAIL if has_red_green_refactor(body.text) else PASS,
        "RED/GREEN/REFACTOR markers present" if has_red_green_refactor(body.text) else "",
    ))
    return out


def check_discipline_skill(body: Body, skill_dir: Path) -> list[CheckResult]:
    out: list[CheckResult] = []
    out.append(CheckResult(
        ">=1 rule + counter pair",
        PASS if has_excuse_reality_table(body.text) else FAIL,
        "no rule+counter / rationalization markers" if not has_excuse_reality_table(body.text) else "",
    ))
    out.append(CheckResult(
        "red flags list",
        PASS if has_red_flags_list(body.text) else FAIL,
        "no 'Red flags' heading" if not has_red_flags_list(body.text) else "",
    ))
    if has_red_green_refactor(body.text):
        out.append(CheckResult(
            "adversarial pressure testing applied",
            JUDGMENT,
            "RED/GREEN/REFACTOR markers present; agent must verify pressure testing was applied to this skill's own rules",
        ))
    else:
        out.append(CheckResult(
            "adversarial pressure testing applied",
            FAIL,
            "no RED/GREEN/REFACTOR markers",
        ))
    return out


def check_domain_skill(body: Body, skill_dir: Path) -> list[CheckResult]:
    out: list[CheckResult] = []
    out.append(CheckResult(
        "identity sentence",
        PASS if has_identity_sentence(body.text) else JUDGMENT,
        "no clear single-sentence identity after H1" if not has_identity_sentence(body.text) else "",
    ))
    out.append(CheckResult(
        "companion declaration",
        PASS if has_companion_declaration(body.text) else FAIL,
        "no 'Companion declaration' heading or 'no sibling' / 'companion domains' phrase" if not has_companion_declaration(body.text) else "",
    ))
    h2_count = len(re.findall(r"^##\s+\S", body.text, re.MULTILINE))
    out.append(CheckResult(
        "orientation content (>=1 H2 beyond index)",
        PASS if h2_count >= 2 else FAIL,
        f"{h2_count} H2 sections",
    ))
    out.append(CheckResult(
        "reference index (Conditional Loading)",
        PASS if has_conditional_loading(body.text) else FAIL,
    ))
    if h2_count == 1 and has_conditional_loading(body.text):
        out.append(CheckResult(
            "prohibited: index without orientation",
            FAIL,
            "only Conditional Loading H2; no orientation content",
        ))
    else:
        out.append(CheckResult(
            "prohibited: index without orientation",
            PASS,
        ))
    return out


def mixed_type_signal(body_text: str) -> tuple[int, list[str]]:
    """Detect cross-type signals on the narrative body (code fences stripped)."""
    narrative = strip_code_fences(body_text)
    signals: list[str] = []
    if has_excuse_reality_table(narrative) or has_red_green_refactor(narrative):
        signals.append("discipline-content (rule+counter or RED/GREEN/REFACTOR)")
    if has_recognition_marker(narrative) or has_counter_example(narrative):
        signals.append("pattern-content (recognition / counter-example)")
    if count_ordered_steps(narrative) >= 1:
        signals.append("technique-content (ordered steps)")
    if has_lookup_table(narrative) or has_yaml_block(body_text):
        signals.append("reference-content (lookup tables / YAML blocks)")
    if has_conditional_loading(narrative):
        signals.append("domain-content (Conditional Loading index)")
    return len(signals), signals


TYPE_RUNNERS = {
    "reference-skill": check_reference_skill,
    "pattern-skill": check_pattern_skill,
    "technique-skill": check_technique_skill,
    "discipline-skill": check_discipline_skill,
    "domain-skill": check_domain_skill,
}


def check_yaml_contract(yaml_data: dict) -> tuple[list[CheckResult], str | None]:
    """Validate the YAML block against the appropriate schema."""
    results: list[CheckResult] = []

    roots_present = detect_mixed_type_yaml(yaml_data)
    if len(roots_present) > 1:
        results.append(CheckResult(
            "yaml: single root key",
            FAIL,
            f"multiple type roots present (mixed-type drift): {roots_present}",
        ))

    root_key, schema = resolve_schema(yaml_data)
    if schema is None:
        results.append(CheckResult("yaml: recognized root key", FAIL, "no canonical-type root key found"))
        return results, None

    results.append(CheckResult(f"yaml: root key '{root_key}'", PASS))

    fails, _checked = validate(yaml_data, schema)
    if not fails:
        results.append(CheckResult("yaml: schema validation", PASS, "all required keys present, all rules satisfied"))
    else:
        for path, msg in fails:
            results.append(CheckResult(f"yaml: {path}", FAIL, msg))

    return results, root_key


def check_portable_units(body_text: str) -> list[CheckResult]:
    """Validate every top-level portable typed unit in the document body."""
    results: list[CheckResult] = []
    if not HAVE_YAML:
        return results
    units, _ = collect_yaml_units(body_text)
    for unit_root, block_data in units:
        if unit_root not in PORTABLE_UNIT_ROOTS:
            continue
        schema = SCHEMAS_BY_ROOT.get(unit_root)
        if schema is None:
            continue
        fails, _checked = validate(block_data, schema)
        if not fails:
            results.append(CheckResult(
                f"yaml: portable unit '{unit_root}'",
                PASS,
                "all required keys present, all rules satisfied",
            ))
        else:
            for path, msg in fails:
                results.append(CheckResult(f"yaml: {path}", FAIL, msg))
    return results


def check_facts_cross_rules(body_text: str, declared_type: str | None) -> list[CheckResult]:
    """Enforce document-level facts cross-rules across the union of all sources.

    Facts can live nested in `reference_skill.facts` OR as a top-level `facts:`
    portable unit, OR both. The cross-rules ("at least one fact carries gotchas",
    "at least one fact carries example", "at least one fact exists") apply over
    the union, not per-source. Only applies to reference-skill documents.
    """
    if declared_type != "reference-skill":
        return []
    if not HAVE_YAML:
        return []

    all_facts: list[dict] = []
    units, _ = collect_yaml_units(body_text)
    for unit_root, block_data in units:
        if unit_root == "reference_skill":
            inner = block_data.get("reference_skill", {})
            if isinstance(inner, dict):
                inner_facts = inner.get("facts", [])
                if isinstance(inner_facts, list):
                    all_facts.extend(f for f in inner_facts if isinstance(f, dict))
        elif unit_root == "facts":
            top_facts = block_data.get("facts", [])
            if isinstance(top_facts, list):
                all_facts.extend(f for f in top_facts if isinstance(f, dict))

    results: list[CheckResult] = []
    if not all_facts:
        results.append(CheckResult(
            "yaml: facts present (cross-source)",
            FAIL,
            "reference-skill requires at least one fact (nested in reference_skill: or as a top-level facts: unit, or both)",
        ))
        return results

    results.append(CheckResult(
        "yaml: facts present (cross-source)",
        PASS,
        f"{len(all_facts)} facts across all sources",
    ))
    has_gotcha = any(f.get("gotchas") for f in all_facts)
    results.append(CheckResult(
        "yaml: >=1 fact carries gotchas (cross-source)",
        PASS if has_gotcha else FAIL,
        "" if has_gotcha else "no fact carries a gotchas list across any source",
    ))
    has_example = any(f.get("example") for f in all_facts)
    results.append(CheckResult(
        "yaml: >=1 fact carries example (cross-source)",
        PASS if has_example else FAIL,
        "" if has_example else "no fact carries an example block across any source",
    ))
    return results


def check_cross_block_drift(body_text: str) -> CheckResult | None:
    """Cross-block mixed-type drift detection."""
    if not HAVE_YAML:
        return None
    units, _ = collect_yaml_units(body_text)
    skill_type_roots = sorted({root for (root, _) in units if root in SKILL_TYPE_ROOTS})
    if len(skill_type_roots) > 1:
        return CheckResult(
            "yaml: single skill-type root (cross-block)",
            FAIL,
            f"multiple skill-type roots across blocks (mixed-type drift): {skill_type_roots}",
        )
    return None


def audit_claude_md(claude_md_path: Path, content: str) -> dict[str, Any]:
    """Audit a CLAUDE.md insight file."""
    body = parse_body(content)
    yaml_data, yaml_err, detected_root = extract_skill_type_unit(body.text)
    yaml_results: list[CheckResult] = []
    yaml_root: str | None = None

    if yaml_data is not None:
        if "claude_md" not in yaml_data:
            roots = list(yaml_data.keys()) if isinstance(yaml_data, dict) else []
            yaml_results.append(CheckResult(
                "yaml: claude_md root key",
                FAIL,
                f"CLAUDE.md must carry a claude_md: YAML block; found roots {roots}",
            ))
        else:
            yaml_results, yaml_root = check_yaml_contract(yaml_data)
    elif yaml_err == "no-yaml-parser":
        yaml_results.append(CheckResult(
            f"yaml: contract block detected (root='{detected_root}')",
            JUDGMENT,
            "pyyaml not installed; YAML contract validation unavailable.",
        ))
    else:
        yaml_results.append(CheckResult(
            "yaml: claude_md contract block",
            FAIL,
            "no fenced yaml block with a claude_md root key found",
        ))

    return {
        "path": str(claude_md_path),
        "kind": "claude_md",
        "declared_type": None,
        "yaml_root": yaml_root,
        "universal": [],
        "yaml_contract": [asdict(r) for r in yaml_results],
        "type_specific": [],
        "mixed_type": asdict(CheckResult("mixed-type signal (n/a for CLAUDE.md)", NA)),
    }


def audit(skill_md_path: Path) -> dict[str, Any]:
    if not skill_md_path.exists():
        return {"error": f"file not found: {skill_md_path}"}
    content = skill_md_path.read_text(encoding="utf-8")

    if skill_md_path.name.lower() == "claude.md":
        return audit_claude_md(skill_md_path, content)

    skill_dir = skill_md_path.parent

    fm = parse_frontmatter(content)
    body = parse_body(content)

    universal = check_universal(fm, body, skill_dir)
    declared_type = fm.fields.get("skill-type") if fm else None

    yaml_data, yaml_err, detected_root = extract_skill_type_unit(body.text)
    yaml_results: list[CheckResult] = []
    yaml_root: str | None = None
    contract_staged = yaml_data is not None or detected_root is not None

    if yaml_data is not None:
        yaml_results, yaml_root = check_yaml_contract(yaml_data)
    elif yaml_err == "no-yaml-parser":
        yaml_root = detected_root
        yaml_results.append(CheckResult(
            f"yaml: contract block detected (root='{detected_root}')",
            JUDGMENT,
            "pyyaml not installed; YAML contract validation unavailable. Skill is staged for YAML validation; install pyyaml to validate.",
        ))
    elif yaml_err == "no-yaml-parser-no-block":
        yaml_results.append(CheckResult(
            "yaml: parser available + contract block",
            JUDGMENT,
            "pyyaml not installed AND no yaml contract block found; falling back to legacy markdown heuristics",
        ))
    else:
        yaml_results.append(CheckResult(
            "yaml: contract block",
            JUDGMENT,
            "no fenced yaml contract block with a recognized root key; falling back to legacy markdown heuristics",
        ))

    yaml_results.extend(check_portable_units(body.text))
    yaml_results.extend(check_facts_cross_rules(body.text, declared_type))

    cross_block_drift = check_cross_block_drift(body.text)
    if cross_block_drift is not None:
        yaml_results.append(cross_block_drift)

    type_specific: list[CheckResult] = []
    if not contract_staged and declared_type in TYPE_RUNNERS:
        # Legacy heuristics run on the NARRATIVE body -- code fences stripped,
        # like mixed_type_signal (strip_code_fences_before_heuristics insight):
        # numbered lists / markers inside fenced examples must not count.
        narrative_body = Body(
            text=strip_code_fences(body.text),
            lines=body.lines,
            tokens_approx=body.tokens_approx,
        )
        if declared_type == "technique-skill":
            type_specific = check_technique_skill(narrative_body, skill_dir, fm)
        else:
            type_specific = TYPE_RUNNERS[declared_type](narrative_body, skill_dir)

    if not contract_staged:
        score, signals = mixed_type_signal(body.text)
        if score >= 2:
            mixed = CheckResult(
                "mixed-type signal (legacy heuristic)",
                JUDGMENT,
                f"score={score}: {signals}",
            )
        else:
            mixed = CheckResult("mixed-type signal (legacy heuristic)", PASS, f"score={score}")
    elif yaml_data is not None:
        roots_present = detect_mixed_type_yaml(yaml_data)
        if len(roots_present) > 1:
            mixed = CheckResult(
                "mixed-type signal (deterministic)",
                FAIL,
                f"multiple root keys present: {roots_present}",
            )
        else:
            mixed = CheckResult("mixed-type signal (deterministic)", PASS, f"single root: {roots_present[0] if roots_present else 'none'}")
    else:
        mixed = CheckResult(
            "mixed-type signal (deferred)",
            JUDGMENT,
            f"contract block detected (root='{detected_root}') but pyyaml unavailable; cannot determine deterministically",
        )

    return {
        "path": str(skill_md_path),
        "declared_type": declared_type,
        "yaml_root": yaml_root,
        "universal": [asdict(r) for r in universal],
        "yaml_contract": [asdict(r) for r in yaml_results],
        "type_specific": [asdict(r) for r in type_specific],
        "mixed_type": asdict(mixed),
    }


def render_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"audit: {report['path']}")
    lines.append(f"declared_type: {report['declared_type']}")
    if report.get("yaml_root"):
        lines.append(f"yaml_root: {report['yaml_root']}")
    lines.append("")
    lines.append("== Universal ==")
    for r in report["universal"]:
        suffix = f" -- {r['note']}" if r["note"] else ""
        lines.append(f"  [{r['verdict']}] {r['row']}{suffix}")
    lines.append("")
    lines.append("== YAML contract ==")
    for r in report.get("yaml_contract", []):
        suffix = f" -- {r['note']}" if r["note"] else ""
        lines.append(f"  [{r['verdict']}] {r['row']}{suffix}")
    if report["type_specific"]:
        lines.append("")
        lines.append(f"== Type-specific (legacy fallback, {report['declared_type']}) ==")
        for r in report["type_specific"]:
            suffix = f" -- {r['note']}" if r["note"] else ""
            lines.append(f"  [{r['verdict']}] {r['row']}{suffix}")
    lines.append("")
    lines.append("== Mixed-type ==")
    mt = report["mixed_type"]
    suffix = f" -- {mt['note']}" if mt["note"] else ""
    lines.append(f"  [{mt['verdict']}] {mt['row']}{suffix}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit a SKILL.md against the skill-authoring framework.",
    )
    parser.add_argument("path", help="Path to SKILL.md")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of formatted text")
    args = parser.parse_args(argv)

    report = audit(Path(args.path))
    if "error" in report:
        print(report["error"], file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
