"""audit -- run deterministic contract checks against a SKILL.md.

Usage:
    python -m skills_kit_lib.audit <path-to-SKILL.md>
    python -m skills_kit_lib.audit <path-to-SKILL.md> --json

Emits a per-row verdict: pass / fail / judgment-required / n/a. Rows flagged
judgment-required are not deterministic at this level; the agent runs them
by hand against the contract in md-domain's skill-domain/framework.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import warnings
from urllib.parse import unquote
from dataclasses import dataclass, asdict
from functools import cached_property
from pathlib import Path
from typing import Any, Callable, Sequence

from .document_walker import (
    extract_claude_md_unit,
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
    high_scoring_types,
    has_recognition_marker,
    has_red_flags_list,
    has_red_green_refactor,
    has_step_tracker_invocation,
    has_tickbox_list,
    parse_body,
    parse_frontmatter,
    strip_code_fences,
    type_signals,
)
from .instruction_files import is_active_instruction_file
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

# Centralized threshold literals for the rules audit.py hard-codes. Each is
# referenced by lookup below (and overlaid by resolved standards config) rather
# than being hard-coded at the check site. A threshold a SHIPPED STANDARDS
# CRITERION consumes is not listed here: its default lives in that criterion's
# `threshold:` declaration (skills_kit_lib/defaults/standards/), and
# standards_resolve reports it as ResolvedStandards.threshold_defaults.
#: The default `import_max_lines` carried while the key is deprecated. The
#: shipped claude-md-import-size criterion owns the live value; a test pins this
#: constant equal to that criterion's declared default.
_DEPRECATED_IMPORT_MAX_LINES = 50

#: First and last skills-kit versions of the deprecation window for the names
#: that left this module (docs/reference/deprecation-policy.md).
_DEPRECATED_SINCE = "0.97.0"
_REMOVABLE_IN = "0.99.0"


def _deprecation_message(old: str, replacement: str) -> str:
    return (
        f"skills_kit_lib.audit.{old} is deprecated since skills-kit {_DEPRECATED_SINCE} and "
        f"will be removed in {_REMOVABLE_IN}; use {replacement}"
    )


class _BuiltinThresholds(dict):
    """The built-in thresholds. `import_max_lines` left the mapping when the
    shipped claude-md-import-size criterion took ownership of it; a lookup of
    the old key still answers, with a DeprecationWarning, for the policy window.
    Only `thresholds["import_max_lines"]` is served: membership, iteration and
    `.get` see the real keys."""

    def __missing__(self, key):
        if key == "import_max_lines":
            warnings.warn(
                _deprecation_message(
                    'THRESHOLDS["import_max_lines"]',
                    "the threshold declared by the shipped claude-md-import-size criterion "
                    "(resolve_shipped().threshold_defaults)",
                ),
                DeprecationWarning,
                stacklevel=2,
            )
            return _DEPRECATED_IMPORT_MAX_LINES
        raise KeyError(key)


THRESHOLDS = _BuiltinThresholds({
    "name_max_chars": 64,
    "body_max_lines": 500,
    "body_max_tokens": 3000,
    "mixed_min_score": 2,
    "wrapper_max_lines": 40,
})


@dataclass
class CheckResult:
    row: str
    verdict: str
    note: str = ""
    rule: str = ""


@dataclass(frozen=True)
class SkillContext:
    """The parsed SKILL.md a mechanical skill_md evaluator reads.

    yaml_root is the contract root the audit resolved for the document (None
    when no contract block was recognized); it is set only on the context the
    yaml_contract-section evaluators receive, because it is a result of the
    contract check and does not exist when the universal rows are evaluated.
    yaml_units is every recognized (root, block data) unit in the body, parsed
    on first use and shared by the evaluators that read it; it is empty
    without pyyaml.

    contract_staged is True when the document carries a YAML contract block
    (parsed, or detected by regex on a bare interpreter); it is set only on the
    context the type_specific-section evaluators receive, whose `body` is the
    NARRATIVE body (code fences stripped), because the legacy per-type
    heuristics must not count markers inside fenced examples.
    """

    fm: Frontmatter
    body: Body
    skill_dir: Path
    yaml_root: str | None = None
    contract_staged: bool = False

    @cached_property
    def yaml_units(self) -> list[tuple[str, dict]]:
        units, _, _ = collect_yaml_units(self.body.text)
        return units


@dataclass(frozen=True)
class ClaudeMdContext:
    """The parsed CLAUDE.md / AGENTS.md a mechanical claude_md evaluator reads.

    claude_md is the inner mapping of the document's `claude_md:` contract
    block, or None when the file carries no such block (or the block is not a
    mapping). path anchors relative `@import` targets; content is the file as
    read; body is parse_body(content).
    """

    path: Path
    content: str
    body: Body
    claude_md: dict | None


#: What a mechanical evaluator receives: the context of the document type its
#: criterion's `applies_to` names.
AuditContext = SkillContext | ClaudeMdContext


@dataclass(frozen=True)
class MechanicalEvaluator:
    """The code half of one `enforcement: mechanical` standards criterion.

    The criterion (id, statement, severity, threshold default) lives in a
    standards file; this record carries only what code must supply:

    - applies_to: the file-type primitive whose audit runs the evaluator; it
      also names the context type the callables receive (skill_md ->
      SkillContext, claude_md -> ClaudeMdContext).
    - threshold: the threshold name the criterion OWNS, or None. The criterion
      must declare exactly this name with a default; standards_resolve rejects
      any other declaration.
    - deferred_row: the row label emitted when pyyaml is unavailable and the
      criterion cannot be read. It names the threshold, never a default.
    - applies: context -> whether the criterion has a row for this document at
      all. It is checked in both resolved and deferred modes and reads no
      threshold.
    - evaluate: (context, thresholds) -> (row label, outcome, note). The
      outcome is True (pass), False (the criterion is violated; the row's
      verdict follows the criterion's severity: fail -> fail, otherwise
      judgment-required) or None (not applicable: the row's verdict is n/a).
      None is for a criterion that applies to the document but has nothing to
      check on it, so its row still appears; a criterion with no row at all
      uses `applies`. The outcome may also be the string JUDGMENT: the
      mechanical test found what it looks for but a judgment remains for the
      agent, so the row is judgment-required whatever the criterion's severity
      (a miss still follows the severity). The thresholds mapping holds only
      `threshold` and `reads_builtin_thresholds`, so an evaluator cannot read a
      limit its record does not name.
    - reads_builtin_thresholds: built-in thresholds (THRESHOLDS) the evaluator
      reads without owning. A criterion never declares one of these; a user
      tunes it through `thresholds:` and every reader sees the same value.
    - section: the report list the row joins ("universal", "yaml_contract" or
      "type_specific"). A claude_md audit appends yaml_contract rows after the
      contract rows. A skill_md audit emits them after the portable-unit rows
      and before the asset-dependency rows, with a SkillContext that carries
      yaml_root; its universal rows are evaluated inside check_universal. The
      type_specific rows are the legacy per-skill-type heuristics: a skill_md
      audit evaluates them last, with a SkillContext whose body is the
      narrative body and which carries contract_staged, and each evaluator's
      `applies` gate holds only for a document with no staged contract whose
      declared skill-type is the one the criterion covers.
    - bare_severity: the severity applied when the shipped standards file
      cannot be read (no pyyaml), instead of a deferred row. Set only for a
      criterion whose row a bare interpreter has always reported with its real
      verdict; a test pins it equal to the severity the shipped file declares,
      so the file stays the source and this is the bare-interpreter copy of it.
    """

    applies_to: str
    threshold: str | None
    deferred_row: str
    applies: Callable[[Any], bool]
    evaluate: Callable[[Any, dict[str, int]], tuple[str, bool | str | None, str]]
    reads_builtin_thresholds: tuple[str, ...] = ()
    section: str = "universal"
    bare_severity: str | None = None

    def __post_init__(self) -> None:
        unknown = [n for n in self.reads_builtin_thresholds if n not in THRESHOLDS]
        if unknown:
            raise ValueError(
                f"reads_builtin_thresholds names {unknown}, which are not built-in "
                f"thresholds (audit.THRESHOLDS: {sorted(THRESHOLDS)})"
            )

    def threshold_view(self, th: dict[str, int]) -> dict[str, int]:
        """The slice of the effective thresholds this evaluator may read."""
        names = (*self.reads_builtin_thresholds, *((self.threshold,) if self.threshold else ()))
        return {name: th[name] for name in names}


def _has_description(ctx: SkillContext) -> bool:
    return "description" in ctx.fm.fields


def _eval_desc_length(ctx: SkillContext, th: dict[str, int]) -> tuple[str, bool, str]:
    desc = ctx.fm.fields["description"]
    limit = th["desc_max_chars"]
    return f"description <= {limit} chars", len(desc) <= limit, f"len={len(desc)}"


def _eval_desc_directive(ctx: SkillContext, th: dict[str, int]) -> tuple[str, bool, str]:
    desc_lc = ctx.fm.fields["description"].lower().lstrip()
    directive = desc_lc.startswith("use when") or desc_lc.startswith("invoke when")
    note = "" if directive else "description should open with 'Use when...' or 'Invoke when...'"
    return "directive form ('Use when...' / 'Invoke when...')", directive, note


def _eval_desc_exclusion(ctx: SkillContext, th: dict[str, int]) -> tuple[str, bool, str]:
    desc = ctx.fm.fields["description"]
    excl = bool(re.search(r"\bdo not use\b|\bdon'?t use\b", desc, re.IGNORECASE))
    note = "" if excl else "no 'do not use' phrase in description"
    return "exclusion clause (Do NOT use for...)", excl, note


def _lacks_skill_type(ctx: SkillContext) -> bool:
    return "skill-type" not in ctx.fm.fields


def _has_skill_type(ctx: SkillContext) -> bool:
    return "skill-type" in ctx.fm.fields


def _eval_skill_type_tag(ctx: SkillContext, th: dict[str, int]) -> tuple[str, bool, str]:
    # Applies only while the tag is absent, so this row is never a pass.
    return "skill-type advisory tag", False, "no skill-type frontmatter; agent infers type from content"


def _eval_skill_type_valid(ctx: SkillContext, th: dict[str, int]) -> tuple[str, bool, str]:
    val = ctx.fm.fields["skill-type"]
    if val in CANONICAL_TYPES:
        return "skill-type value valid", True, val
    return "skill-type value valid", False, f"got '{val}', expected one of {sorted(CANONICAL_TYPES)}"


def _eval_line_count(ctx: SkillContext, th: dict[str, int]) -> tuple[str, bool, str]:
    # An informational count row: it always passes and carries the count.
    return "SKILL.md line count", True, str(ctx.body.lines)


def _eval_token_count(ctx: SkillContext, th: dict[str, int]) -> tuple[str, bool, str]:
    return "SKILL.md token count (approx)", True, str(ctx.body.tokens_approx)


def _eval_progressive_disclosure(ctx: SkillContext, th: dict[str, int]) -> tuple[str, bool | None, str]:
    """Size is a SIGNAL, not a verdict (framework Dec-11): a split is REQUIRED
    only if a CRP-passing decomposition exists -- sections serve different
    reading tasks. The mechanical check cannot evaluate CRP, so a violation
    must not FAIL: the criterion's severity is judgment, and the agent runs
    the CRP test before any split. A stub-plus-always-co-loaded-reference is a
    tool-call doubling, not a context-efficiency win."""
    label = "progressive disclosure (conditional)"
    body = ctx.body
    counts = f"lines={body.lines}, tokens~{body.tokens_approx}"
    if not (body.lines > th["body_max_lines"] or body.tokens_approx > th["body_max_tokens"]):
        return label, None, counts
    if (ctx.skill_dir / "references").exists():
        return label, True, f"body large ({counts}); references/ exists"
    return (
        label,
        False,
        f"body large ({counts}) and no references/; "
        "run the CRP test (do sections serve different reading tasks?) before splitting -- "
        "size is a signal, not a verdict (Dec-11)",
    )


def _eval_refs_one_hop_deep(ctx: SkillContext, th: dict[str, int]) -> tuple[str, bool | None, str]:
    label = "references one-hop-deep (ADP)"
    refs_dir = ctx.skill_dir / "references"
    if not refs_dir.exists():
        return label, None, "no references/ directory"
    # A nested file is one hop when SKILL.md's index.references[] declares
    # its exact path or its containing directory; only undeclared nested
    # files fail (an unindexed nested file is invisible to the load graph).
    declared = {
        norm
        for _, v, is_path in _structured_index_paths(ctx.body.text)
        if is_path
        for norm in [v.strip().replace("\\", "/").removeprefix("./").rstrip("/")]
        if norm.startswith("references/")
    }
    nested = [
        p for p in refs_dir.glob("*/*.md")
        if p.relative_to(ctx.skill_dir).as_posix() not in declared
        and p.parent.relative_to(ctx.skill_dir).as_posix() not in declared
    ]
    if nested:
        rel = [str(p.relative_to(ctx.skill_dir)) for p in nested]
        return (
            label,
            False,
            f"nested and not declared in SKILL.md index.references[] (add the file's path or its directory as an entry): {rel}",
        )
    return label, True, ""


def _is_reference_skill(ctx: SkillContext) -> bool:
    """Gated on the YAML ROOT when a contract block was recognized, NOT on the
    frontmatter skill-type: the frontmatter tag is advisory and judgment-only,
    so a `reference_skill:` document with no (or a disagreeing) tag must not
    skip the facts floor (I8). The tag is the fallback gate only for a
    document that carries no recognized root at all."""
    if ctx.yaml_root is not None:
        return ctx.yaml_root == "reference_skill"
    return ctx.fm.fields.get("skill-type") == "reference-skill"


def _declared_facts(ctx: SkillContext) -> list[dict]:
    """Every fact across the union of its sources: nested in a `reference_skill:`
    unit's `facts` and in a top-level `facts:` unit. The facts criteria apply
    over the union, not per source."""
    all_facts: list[dict] = []
    for unit_root, block_data in ctx.yaml_units:
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
    return all_facts


def _facts_floor_applies(ctx: SkillContext) -> bool:
    # Without pyyaml the units cannot be parsed, so there is no row (not a
    # deferred one): the contract-staged state skips these floors.
    return HAVE_YAML and _is_reference_skill(ctx)


def _facts_declared_applies(ctx: SkillContext) -> bool:
    # With no fact declared, facts-floor reports the gap once and the two
    # per-fact criteria have nothing to read.
    return _facts_floor_applies(ctx) and bool(_declared_facts(ctx))


def _eval_facts_floor(ctx: SkillContext, th: dict[str, int]) -> tuple[str, bool, str]:
    label = "yaml: facts present (cross-source)"
    facts = _declared_facts(ctx)
    if not facts:
        return (
            label,
            False,
            "reference-skill requires at least one fact (nested in reference_skill: or as a top-level facts: unit, or both)",
        )
    return label, True, f"{len(facts)} facts across all sources"


def _eval_facts_gotcha(ctx: SkillContext, th: dict[str, int]) -> tuple[str, bool, str]:
    ok = any(f.get("gotchas") for f in _declared_facts(ctx))
    return (
        "yaml: >=1 fact carries gotchas (cross-source)",
        ok,
        "" if ok else "no fact carries a gotchas list across any source",
    )


def _eval_facts_example(ctx: SkillContext, th: dict[str, int]) -> tuple[str, bool, str]:
    ok = any(f.get("example") for f in _declared_facts(ctx))
    return (
        "yaml: >=1 fact carries example (cross-source)",
        ok,
        "" if ok else "no fact carries an example block across any source",
    )


def _technique_unit(ctx: SkillContext) -> dict | None:
    """The inner mapping of the document's first `technique_skill:` unit whose
    value is a mapping, or None."""
    for unit_root, block_data in ctx.yaml_units:
        if unit_root != "technique_skill":
            continue
        inner = block_data.get("technique_skill", {})
        if isinstance(inner, dict):
            return inner
    return None


def _caution_floor_applies(ctx: SkillContext) -> bool:
    return HAVE_YAML and _technique_unit(ctx) is not None


def _eval_caution_floor(ctx: SkillContext, th: dict[str, int]) -> tuple[str, bool, str]:
    """The technique-skill caution-surface OR-rule. Per-technique gotchas are
    optional at the schema level; the document-level floor is >=1 gotcha across
    techniques OR >=1 anti_patterns record. The two are alternate containers
    for the same caution surface -- duplicating one caution across both is an
    anti-pattern (the reader pays twice)."""
    inner = _technique_unit(ctx)
    techniques = inner.get("techniques", [])
    has_gotcha = isinstance(techniques, list) and any(
        isinstance(t, dict) and t.get("gotchas") for t in techniques)
    anti_patterns = inner.get("anti_patterns")
    has_anti = isinstance(anti_patterns, list) and len(anti_patterns) >= 1
    ok = has_gotcha or has_anti
    return (
        "yaml: >=1 gotcha OR >=1 anti_pattern record (caution-surface OR-rule)",
        ok,
        "" if ok else "no technique carries a gotchas list and no anti_patterns record exists",
    )


def _legacy_applies(skill_type: str) -> Callable[[SkillContext], bool]:
    """The scope gate of one legacy per-type heuristic: it holds only for a
    document with NO staged YAML contract (a contract block, parsed or merely
    detected, supersedes the heuristics) whose declared skill-type is
    `skill_type`. It is a predicate, not data in the standards file, for the
    same reason the facts floors gate on the contract root: the scope is part of
    what the evaluator checks, and the criterion's statement names the type."""

    def applies(ctx: SkillContext) -> bool:
        return not ctx.contract_staged and ctx.fm.fields.get("skill-type") == skill_type

    return applies


def _marker_present(
    label: str, test: Callable[[str], bool], miss_note: str
) -> Callable[[SkillContext, dict[str, int]], tuple[str, bool, str]]:
    """Evaluator for a legacy row that passes when its marker IS in the
    narrative body and carries `miss_note` when it is not."""

    def evaluate(ctx: SkillContext, th: dict[str, int]) -> tuple[str, bool, str]:
        ok = test(ctx.body.text)
        return label, ok, "" if ok else miss_note

    return evaluate


def _marker_absent(
    label: str, test: Callable[[str], bool], hit_note: str
) -> Callable[[SkillContext, dict[str, int]], tuple[str, bool, str]]:
    """Evaluator for a legacy "prohibited:" row that passes when its marker is
    NOT in the narrative body and carries `hit_note` when it is."""

    def evaluate(ctx: SkillContext, th: dict[str, int]) -> tuple[str, bool, str]:
        hit = test(ctx.body.text)
        return label, not hit, hit_note if hit else ""

    return evaluate


def has_identity_sentence(body_text: str) -> bool:
    after_h1 = re.split(r"^#\s+\S.+$", body_text, maxsplit=1, flags=re.MULTILINE)
    if len(after_h1) < 2:
        return False
    rest = after_h1[1].lstrip()
    first_para = rest.split("\n\n", 1)[0].strip()
    return bool(first_para) and "." in first_para and len(first_para) < 600


def _discipline_content(text: str) -> bool:
    return has_red_green_refactor(text) or has_excuse_reality_table(text)


def _eval_pattern_bundle(ctx: SkillContext, th: dict[str, int]) -> tuple[str, bool, str]:
    present = (ctx.skill_dir / "scripts").exists() or (ctx.skill_dir / "bin").exists()
    return "prohibited: utility bundle", not present, "scripts/ or bin/ present" if present else ""


def _eval_ordered_steps(ctx: SkillContext, th: dict[str, int]) -> tuple[str, bool, str]:
    # User-only technique-skills (disable-model-invocation: true) are NOT
    # exempt: the technique-skill schema requires steps regardless of
    # trigger_model (Dec-2), and this legacy path mirrors it.
    step_count = count_ordered_steps(ctx.body.text)
    return "ordered-step body", step_count >= 1, f"{step_count} ordered-step entries detected"


_WRAPPER_CITATION = re.compile(
    r"(?:\$\{CLAUDE_SKILL_DIR\}/|(?<![/:\w.-]))references/([A-Za-z0-9_\-]+\.md)"
)


def _eval_wrapper_procedure_cited(ctx: SkillContext, th: dict[str, int]) -> tuple[str, bool, str]:
    """A technique-wrapper stub cites at least one references/ document, and every
    one it cites exists: the stub's only job is to point at the procedure, so a
    stub that points at nothing, or at a missing file, has no technique."""
    label = "wrapper cites its procedure document"
    cited = sorted(set(_WRAPPER_CITATION.findall(ctx.body.text)))
    if not cited:
        return label, False, "no references/<file>.md citation in the body"
    missing = [name for name in cited if not (ctx.skill_dir / "references" / name).is_file()]
    if missing:
        return label, False, f"cited but missing: {missing}"
    return label, True, f"cites {len(cited)} reference file(s)"


def _eval_wrapper_max_lines(ctx: SkillContext, th: dict[str, int]) -> tuple[str, bool, str]:
    """A technique-wrapper stub stays within wrapper_max_lines lines: a longer body
    has inlined content or steps the procedure document should carry, and a
    skill that fires repeatedly pays that body on every firing."""
    limit = th["wrapper_max_lines"]
    lines = ctx.body.lines
    return f"wrapper stub within {limit} lines", lines <= limit, f"{lines} lines"


def _eval_step_tracking(ctx: SkillContext, th: dict[str, int]) -> tuple[str, bool | None, str]:
    label = "explicit step-tracking (conditional, IF >3 steps): checklist OR tracker invocation"
    step_count = count_ordered_steps(ctx.body.text)
    if step_count <= 3:
        return label, None, f"only {step_count} steps"
    has_checklist = has_tickbox_list(ctx.body.text)
    has_tracker = has_step_tracker_invocation(ctx.body.text)
    if has_checklist or has_tracker:
        via = "tickbox checklist" if has_checklist else "step-tracker invocation"
        return label, True, f"{step_count} steps; satisfied via {via}"
    return (
        label,
        False,
        f"{step_count} steps; neither tickbox checklist nor step-tracker invocation present",
    )


def _eval_pressure_test(ctx: SkillContext, th: dict[str, int]) -> tuple[str, bool | str, str]:
    """A discipline-skill must apply adversarial pressure testing to its own
    rules. Markers present is not a pass: whether the testing was actually
    applied to this skill's rules is for the agent, so that outcome is JUDGMENT
    whatever the severity; no markers is the miss."""
    label = "adversarial pressure testing applied"
    if has_red_green_refactor(ctx.body.text):
        return (
            label,
            JUDGMENT,
            "RED/GREEN/REFACTOR markers present; agent must verify pressure testing was applied to this skill's own rules",
        )
    return label, False, "no RED/GREEN/REFACTOR markers"


def _h2_count(text: str) -> int:
    return len(re.findall(r"^##\s+\S", text, re.MULTILINE))


def _eval_domain_orientation(ctx: SkillContext, th: dict[str, int]) -> tuple[str, bool, str]:
    h2_count = _h2_count(ctx.body.text)
    return "orientation content (>=1 H2 beyond index)", h2_count >= 2, f"{h2_count} H2 sections"


def _eval_domain_index_only(ctx: SkillContext, th: dict[str, int]) -> tuple[str, bool, str]:
    """An index with no orientation content: Conditional Loading is the only H2."""
    index_only = _h2_count(ctx.body.text) == 1 and has_conditional_loading(ctx.body.text)
    return (
        "prohibited: index without orientation",
        not index_only,
        "only Conditional Loading H2; no orientation content" if index_only else "",
    )


_REFERENCE_SKILL_APPLIES = _legacy_applies("reference-skill")
_PATTERN_SKILL_APPLIES = _legacy_applies("pattern-skill")
_TECHNIQUE_SKILL_APPLIES = _legacy_applies("technique-skill")
_DISCIPLINE_SKILL_APPLIES = _legacy_applies("discipline-skill")
_DOMAIN_SKILL_APPLIES = _legacy_applies("domain-skill")
_TECHNIQUE_WRAPPER_APPLIES = _legacy_applies("technique-wrapper")


def _has_claude_md_block(ctx: ClaudeMdContext) -> bool:
    return ctx.claude_md is not None


def _always(ctx: Any) -> bool:
    return True


def _eval_import_size(ctx: ClaudeMdContext, th: dict[str, int]) -> tuple[str, bool, str]:
    limit = th["import_max_lines"]
    outside, _unclosed = _split_fences(ctx.content)
    over: list[str] = []
    for n, target, path, missing in _iter_imports(outside, ctx.path.parent):
        # A missing import is reported under claude-md-links-resolve.
        if missing or path is None or not path.is_file():
            continue
        try:
            count = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
        if count > limit:
            over.append(f"line {n}: @{target} has {count} lines")
    return f"@imports <= {limit} lines", not over, "; ".join(over)


def _eval_size_signal(ctx: ClaudeMdContext, th: dict[str, int]) -> tuple[str, bool, str]:
    body = ctx.body
    too_big = body.lines > th["body_max_lines"] or body.tokens_approx > th["body_max_tokens"]
    counts = f"lines={body.lines}, tokens~{body.tokens_approx}"
    if too_big:
        return (
            "body size signal",
            False,
            f"{counts}; run the size-signal judgment (split only when sections serve different reading tasks)",
        )
    return "body size signal", True, counts


def _eval_record_floor(ctx: ClaudeMdContext, th: dict[str, int]) -> tuple[str, bool, str]:
    """Document-level floor for claude_md blocks: at least one record across
    the insights/conventions union. The schema no longer requires insights
    specifically -- a conventions-only CLAUDE.md is valid -- but an empty
    block (neither insights nor conventions) fails."""
    label = "yaml: >=1 record across insights/conventions (union floor)"
    insights = ctx.claude_md.get("insights")
    conventions = ctx.claude_md.get("conventions")
    n_insights = len(insights) if isinstance(insights, list) else 0
    n_conventions = len(conventions) if isinstance(conventions, list) else 0
    if n_insights + n_conventions >= 1:
        return label, True, f"{n_insights} insight(s), {n_conventions} convention(s)"
    return label, False, "claude_md block carries neither insights nor conventions; the block must be non-empty"


#: criterion id -> its evaluator. The registry of every id an
#: `enforcement: mechanical` criterion may carry: standards_resolve rejects a
#: mechanical criterion whose id is not a key here. Declaration order is the
#: deferred-row order when pyyaml is unavailable; with pyyaml the rows follow
#: the standards file's criterion order.
MECHANICAL_EVALUATORS: dict[str, MechanicalEvaluator] = {
    "desc-160-char": MechanicalEvaluator(
        "skill_md", "desc_max_chars",
        "description <= desc_max_chars chars (deferred)", _has_description, _eval_desc_length,
    ),
    "desc-directive-form": MechanicalEvaluator(
        "skill_md", None,
        "directive form ('Use when...' / 'Invoke when...') (deferred)", _has_description, _eval_desc_directive,
    ),
    "desc-exclusion-clause": MechanicalEvaluator(
        "skill_md", None,
        "exclusion clause (Do NOT use for...) (deferred)", _has_description, _eval_desc_exclusion,
    ),
    "skill-type-tag": MechanicalEvaluator(
        "skill_md", None,
        "skill-type advisory tag (deferred)", _lacks_skill_type, _eval_skill_type_tag,
    ),
    "skill-type-valid": MechanicalEvaluator(
        "skill_md", None,
        "skill-type value valid (deferred)", _has_skill_type, _eval_skill_type_valid,
    ),
    "body-line-count": MechanicalEvaluator(
        "skill_md", None,
        "SKILL.md line count (deferred)", _always, _eval_line_count,
    ),
    "body-token-count": MechanicalEvaluator(
        "skill_md", None,
        "SKILL.md token count (approx) (deferred)", _always, _eval_token_count,
    ),
    "body-size-signal": MechanicalEvaluator(
        "skill_md", None,
        "progressive disclosure (conditional) (deferred)", _always, _eval_progressive_disclosure,
        reads_builtin_thresholds=("body_max_lines", "body_max_tokens"),
    ),
    "refs-one-hop-deep": MechanicalEvaluator(
        "skill_md", None,
        "references one-hop-deep (ADP) (deferred)", _always, _eval_refs_one_hop_deep,
    ),
    "facts-floor": MechanicalEvaluator(
        "skill_md", None,
        "yaml: facts present (cross-source) (deferred)", _facts_floor_applies, _eval_facts_floor,
        section="yaml_contract",
    ),
    "facts-gotcha": MechanicalEvaluator(
        "skill_md", None,
        "yaml: >=1 fact carries gotchas (cross-source) (deferred)", _facts_declared_applies, _eval_facts_gotcha,
        section="yaml_contract",
    ),
    "facts-example": MechanicalEvaluator(
        "skill_md", None,
        "yaml: >=1 fact carries example (cross-source) (deferred)", _facts_declared_applies, _eval_facts_example,
        section="yaml_contract",
    ),
    "caution-floor": MechanicalEvaluator(
        "skill_md", None,
        "yaml: >=1 gotcha OR >=1 anti_pattern record (caution-surface OR-rule) (deferred)",
        _caution_floor_applies, _eval_caution_floor,
        section="yaml_contract",
    ),
    "ref-example-block": MechanicalEvaluator(
        "skill_md", None,
        ">=1 example block (deferred)", _REFERENCE_SKILL_APPLIES,
        _marker_present(
            ">=1 example block",
            lambda text: has_heading(text, "Example", "Examples"),
            "no 'Example' heading detected",
        ),
        section="type_specific", bare_severity="judgment",
    ),
    "ref-gotcha-block": MechanicalEvaluator(
        "skill_md", None,
        ">=1 gotcha block (deferred)", _REFERENCE_SKILL_APPLIES,
        _marker_present(
            ">=1 gotcha block",
            lambda text: has_heading(text, "Gotcha", "Gotchas", "Known gotchas"),
            "no 'Gotcha' heading detected",
        ),
        section="type_specific", bare_severity="judgment",
    ),
    "ref-prohibited-discipline": MechanicalEvaluator(
        "skill_md", None,
        "prohibited: discipline content (rule+counter, RED/GREEN/REFACTOR) (deferred)", _REFERENCE_SKILL_APPLIES,
        _marker_absent(
            "prohibited: discipline content (rule+counter, RED/GREEN/REFACTOR)",
            _discipline_content,
            "discipline markers detected",
        ),
        section="type_specific", bare_severity="fail",
    ),
    "ref-prohibited-checklist": MechanicalEvaluator(
        "skill_md", None,
        "prohibited: workflow checklist (deferred)", _REFERENCE_SKILL_APPLIES,
        _marker_absent("prohibited: workflow checklist", has_tickbox_list, "tickbox list present"),
        section="type_specific", bare_severity="fail",
    ),
    "pattern-recognition-block": MechanicalEvaluator(
        "skill_md", None,
        "recognition criteria block (deferred)", _PATTERN_SKILL_APPLIES,
        _marker_present(
            "recognition criteria block",
            has_recognition_marker,
            "no 'recognize/recognition/applies when' marker",
        ),
        section="type_specific", bare_severity="judgment",
    ),
    "pattern-counter-example": MechanicalEvaluator(
        "skill_md", None,
        "counter-example(s) block (deferred)", _PATTERN_SKILL_APPLIES,
        _marker_present(
            "counter-example(s) block",
            has_counter_example,
            "no 'counter-example' or 'do NOT apply' marker",
        ),
        section="type_specific", bare_severity="judgment",
    ),
    "pattern-prohibited-bundle": MechanicalEvaluator(
        "skill_md", None,
        "prohibited: utility bundle (deferred)", _PATTERN_SKILL_APPLIES, _eval_pattern_bundle,
        section="type_specific", bare_severity="fail",
    ),
    "pattern-prohibited-checklist": MechanicalEvaluator(
        "skill_md", None,
        "prohibited: workflow checklist (deferred)", _PATTERN_SKILL_APPLIES,
        _marker_absent("prohibited: workflow checklist", has_tickbox_list, ""),
        section="type_specific", bare_severity="fail",
    ),
    "pattern-prohibited-rule-counter": MechanicalEvaluator(
        "skill_md", None,
        "prohibited: rule + counter pairs (deferred)", _PATTERN_SKILL_APPLIES,
        _marker_absent(
            "prohibited: rule + counter pairs",
            has_excuse_reality_table,
            "rationalization/excuse->reality detected",
        ),
        section="type_specific", bare_severity="fail",
    ),
    "technique-ordered-steps": MechanicalEvaluator(
        "skill_md", None,
        "ordered-step body (deferred)", _TECHNIQUE_SKILL_APPLIES, _eval_ordered_steps,
        section="type_specific", bare_severity="fail",
    ),
    "step-tracking": MechanicalEvaluator(
        "skill_md", None,
        "explicit step-tracking (conditional, IF >3 steps): checklist OR tracker invocation (deferred)",
        _TECHNIQUE_SKILL_APPLIES, _eval_step_tracking,
        section="type_specific", bare_severity="fail",
    ),
    "technique-prohibited-pressure-test": MechanicalEvaluator(
        "skill_md", None,
        "prohibited: adversarial pressure testing (deferred)", _TECHNIQUE_SKILL_APPLIES,
        _marker_absent(
            "prohibited: adversarial pressure testing",
            has_red_green_refactor,
            "RED/GREEN/REFACTOR markers present",
        ),
        section="type_specific", bare_severity="fail",
    ),
    "wrapper-ordered-step": MechanicalEvaluator(
        "skill_md", None,
        "ordered-step body (deferred)", _TECHNIQUE_WRAPPER_APPLIES, _eval_ordered_steps,
        section="type_specific", bare_severity="fail",
    ),
    "wrapper-procedure-cited": MechanicalEvaluator(
        "skill_md", None,
        "wrapper cites its procedure document (deferred)", _TECHNIQUE_WRAPPER_APPLIES,
        _eval_wrapper_procedure_cited,
        section="type_specific", bare_severity="fail",
    ),
    "wrapper-max-lines": MechanicalEvaluator(
        "skill_md", None,
        "wrapper stub within wrapper_max_lines lines (deferred)", _TECHNIQUE_WRAPPER_APPLIES,
        _eval_wrapper_max_lines,
        section="type_specific", bare_severity="fail",
        reads_builtin_thresholds=("wrapper_max_lines",),
    ),
    "discipline-rule-counter": MechanicalEvaluator(
        "skill_md", None,
        ">=1 rule + counter pair (deferred)", _DISCIPLINE_SKILL_APPLIES,
        _marker_present(
            ">=1 rule + counter pair",
            has_excuse_reality_table,
            "no rule+counter / rationalization markers",
        ),
        section="type_specific", bare_severity="fail",
    ),
    "discipline-red-flags": MechanicalEvaluator(
        "skill_md", None,
        "red flags list (deferred)", _DISCIPLINE_SKILL_APPLIES,
        _marker_present("red flags list", has_red_flags_list, "no 'Red flags' heading"),
        section="type_specific", bare_severity="fail",
    ),
    "discipline-pressure-test": MechanicalEvaluator(
        "skill_md", None,
        "adversarial pressure testing applied (deferred)", _DISCIPLINE_SKILL_APPLIES, _eval_pressure_test,
        section="type_specific", bare_severity="fail",
    ),
    "domain-identity-sentence": MechanicalEvaluator(
        "skill_md", None,
        "identity sentence (deferred)", _DOMAIN_SKILL_APPLIES,
        _marker_present(
            "identity sentence",
            has_identity_sentence,
            "no clear single-sentence identity after H1",
        ),
        section="type_specific", bare_severity="judgment",
    ),
    "domain-companion-declaration": MechanicalEvaluator(
        "skill_md", None,
        "companion declaration (deferred)", _DOMAIN_SKILL_APPLIES,
        _marker_present(
            "companion declaration",
            has_companion_declaration,
            "no 'Companion declaration' heading or 'no sibling' / 'companion domains' phrase",
        ),
        section="type_specific", bare_severity="fail",
    ),
    "domain-orientation": MechanicalEvaluator(
        "skill_md", None,
        "orientation content (>=1 H2 beyond index) (deferred)", _DOMAIN_SKILL_APPLIES, _eval_domain_orientation,
        section="type_specific", bare_severity="fail",
    ),
    "domain-reference-index": MechanicalEvaluator(
        "skill_md", None,
        "reference index (Conditional Loading) (deferred)", _DOMAIN_SKILL_APPLIES,
        _marker_present("reference index (Conditional Loading)", has_conditional_loading, ""),
        section="type_specific", bare_severity="fail",
    ),
    "domain-prohibited-index-only": MechanicalEvaluator(
        "skill_md", None,
        "prohibited: index without orientation (deferred)", _DOMAIN_SKILL_APPLIES, _eval_domain_index_only,
        section="type_specific", bare_severity="fail",
    ),
    "claude-md-import-size": MechanicalEvaluator(
        "claude_md", "import_max_lines",
        "@imports <= import_max_lines lines (deferred)", _always, _eval_import_size,
    ),
    "claude-md-size-signal": MechanicalEvaluator(
        "claude_md", None,
        "body size signal (deferred)", _always, _eval_size_signal,
        reads_builtin_thresholds=("body_max_lines", "body_max_tokens"),
    ),
    "claude-md-record-floor": MechanicalEvaluator(
        "claude_md", None,
        "yaml: >=1 record across insights/conventions (union floor) (deferred)",
        _has_claude_md_block, _eval_record_floor,
        section="yaml_contract",
    ),
}


@dataclass(frozen=True)
class MechanicalCriterion:
    """One mechanical standards criterion an audit evaluates.

    severity is the criterion's declared severity (fail | info | judgment), or
    None when the criterion could not be read (no pyyaml): the row is then a
    deferred judgment-required row.
    """

    id: str
    severity: str | None


def _criterion_rows(
    ctx: AuditContext,
    criteria: Sequence[MechanicalCriterion],
    th: dict[str, int],
    section: str | None = None,
) -> list[tuple[str, CheckResult]]:
    """Evaluate criteria against ctx; return (report section, row) pairs in order.

    section, when given, restricts the run to criteria whose evaluator joins
    that report section; the other criteria are skipped, not evaluated.
    """
    rows: list[tuple[str, CheckResult]] = []
    for crit in criteria:
        evaluator = MECHANICAL_EVALUATORS[crit.id]
        if section is not None and evaluator.section != section:
            continue
        if not evaluator.applies(ctx):
            continue
        severity = crit.severity if crit.severity is not None else evaluator.bare_severity
        if severity is None:
            rows.append((evaluator.section, CheckResult(
                evaluator.deferred_row,
                JUDGMENT,
                "pyyaml unavailable: the shipped standards file cannot be read, so this "
                "criterion's severity and threshold are unresolved; run under the "
                "skills-kit plugin venv to evaluate it",
                rule=crit.id,
            )))
            continue
        label, passed, note = evaluator.evaluate(ctx, evaluator.threshold_view(th))
        if passed is None:
            verdict = NA
        elif isinstance(passed, str):
            if passed != JUDGMENT:
                raise ValueError(f"evaluator '{crit.id}' returned outcome {passed!r}; a string outcome must be JUDGMENT")
            verdict = JUDGMENT
        elif passed:
            verdict = PASS
        elif severity == "fail":
            verdict = FAIL
        else:
            verdict = JUDGMENT
        rows.append((evaluator.section, CheckResult(label, verdict, note, rule=crit.id)))
    return rows


def check_universal(
    fm: Frontmatter | None,
    body: Body,
    skill_dir: Path,
    thresholds: dict[str, int] | None = None,
    *,
    skill_criteria: Sequence[MechanicalCriterion],
) -> list[CheckResult]:
    """The universal SKILL.md rows.

    skill_criteria is required: the mechanical skill_md standards criteria to
    evaluate right after the description row (present or absent), in order
    (see mechanical_criteria("skill_md", ...)); each evaluator's `applies` gate decides whether it
    has a row for this document. Only criteria whose evaluator joins the
    "universal" section run here; the yaml_contract ones run in audit(). An
    empty sequence evaluates none -- a caller passes it explicitly, never by
    omission.
    """
    th = thresholds if thresholds is not None else THRESHOLDS
    out: list[CheckResult] = []
    if fm is None:
        out.append(CheckResult("frontmatter present", FAIL, "no leading --- block", rule="frontmatter-present"))
        return out
    out.append(CheckResult("frontmatter present", PASS, rule="frontmatter-present"))
    ctx = SkillContext(fm, body, skill_dir)

    if "name" not in fm.fields:
        out.append(CheckResult("frontmatter.name present", FAIL, rule="name-present"))
    else:
        name = fm.fields["name"]
        out.append(CheckResult("frontmatter.name present", PASS, rule="name-present"))
        out.append(CheckResult(
            f"name <= {th['name_max_chars']} chars",
            PASS if len(name) <= th["name_max_chars"] else FAIL,
            f"len={len(name)}",
            rule="name-length",
        ))
        out.append(CheckResult(
            "name charset (lowercase/digits/hyphens)",
            PASS if re.fullmatch(r"[a-z0-9-]+", name) else FAIL,
            name,
            rule="name-charset",
        ))
        out.append(CheckResult("name not reserved", PASS if name not in RESERVED_NAMES else FAIL, name, rule="name-reserved"))

    if "description" not in fm.fields:
        out.append(CheckResult("frontmatter.description present", FAIL, rule="desc-present"))
    else:
        out.append(CheckResult("frontmatter.description present", PASS, rule="desc-present"))
    out.extend(row for _section, row in _criterion_rows(ctx, skill_criteria, th, section="universal"))

    # Match local references/X.md citations only; exclude cross-plugin refs.
    cited = set(re.findall(r"(?<![/:])references/([a-zA-Z0-9_\-]+\.md)", body.text))
    if not cited:
        out.append(CheckResult("references cited in body all exist", NA, "no references cited in body", rule="refs-cited-exist"))
    else:
        missing = [name for name in cited if not (skill_dir / "references" / name).exists()]
        if missing:
            out.append(CheckResult("references cited in body all exist", FAIL, f"missing: {missing}", rule="refs-cited-exist"))
        else:
            out.append(CheckResult("references cited in body all exist", PASS, f"checked {len(cited)} references", rule="refs-cited-exist"))

    return out


def mixed_type_signal(body_text: str, threshold: int | None = None) -> tuple[int, list[str]]:
    """Detect cross-type signals via the ONE shared scorer,
    markdown_heuristics.type_signals (I5) -- do not re-implement per-type
    detection here; extend type_signals instead (SSOT convention in
    skills_kit_lib/CLAUDE.md).

    `threshold` is the per-type high-score gate (defaults to
    THRESHOLDS["mixed_min_score"]); this is the SAME rule classify.py's
    heuristic-fallback path applies via markdown_heuristics.high_scoring_types,
    so tuning mixed_min_score reaches both consumers identically. Returns the
    count of canonical types scoring at or above the gate, and
    "type=score" labels for each.
    """
    th = threshold if threshold is not None else THRESHOLDS["mixed_min_score"]
    scores = type_signals(body_text)
    active = high_scoring_types(scores, th)
    return len(active), [f"{t}={scores[t]}" for t in active]


def check_yaml_contract(yaml_data: dict) -> tuple[list[CheckResult], str | None]:
    """Validate the YAML block against the appropriate schema."""
    results: list[CheckResult] = []

    roots_present = detect_mixed_type_yaml(yaml_data)
    if len(roots_present) > 1:
        results.append(CheckResult(
            "yaml: single root key",
            FAIL,
            f"multiple type roots present (mixed-type drift): {roots_present}",
            rule="mixed-type",
        ))

    root_key, schema = resolve_schema(yaml_data)
    if schema is None:
        results.append(CheckResult("yaml: recognized root key", FAIL, "no canonical-type root key found", rule="yaml-contract"))
        return results, None

    results.append(CheckResult(f"yaml: root key '{root_key}'", PASS, rule="yaml-contract"))

    fails, _checked = validate(yaml_data, schema)
    if not fails:
        results.append(CheckResult("yaml: schema validation", PASS, "all required keys present, all rules satisfied", rule="yaml-contract"))
    else:
        for path, msg in fails:
            results.append(CheckResult(f"yaml: {path}", FAIL, msg, rule="yaml-contract"))

    return results, root_key


def check_portable_units(body_text: str) -> list[CheckResult]:
    """Validate every top-level portable typed unit in the document body."""
    results: list[CheckResult] = []
    if not HAVE_YAML:
        return results
    units, _, _ = collect_yaml_units(body_text)
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
                rule="yaml-contract",
            ))
        else:
            for path, msg in fails:
                results.append(CheckResult(f"yaml: {path}", FAIL, msg, rule="yaml-contract"))
    return results


_PROJECT_MARKERS = (".git", ".hg", ".svn", ".p4config.txt")


def _find_project_root(start: Path) -> Path | None:
    """Nearest ancestor of `start` (inclusive) holding a VCS/project marker."""
    current = start if start.is_dir() else start.parent
    while True:
        if any((current / marker).exists() for marker in _PROJECT_MARKERS):
            return current
        if current == current.parent:
            return None
        current = current.parent


def _declared_asset_paths(body_text: str) -> list[tuple[str, str]]:
    """Collect (row_label, path) pairs from asset_dependencies declarations
    (top-level portable unit OR nested inside a skill-type unit) and from
    domain_skill tools[].tests entries."""
    out: list[tuple[str, str]] = []
    units, _, _ = collect_yaml_units(body_text)
    seen_ids = set()

    def _add_deps(deps, origin: str):
        if not isinstance(deps, list):
            return
        for i, d in enumerate(deps):
            if isinstance(d, dict) and isinstance(d.get("path"), str):
                key = (origin, i, d["path"])
                if key not in seen_ids:
                    seen_ids.add(key)
                    out.append((f"asset_dependencies[{i}].path", d["path"]))

    for unit_root, block_data in units:
        if unit_root == "asset_dependencies":
            _add_deps(block_data.get("asset_dependencies"), "top")
        elif unit_root in SKILL_TYPE_ROOTS:
            inner = block_data.get(unit_root)
            if isinstance(inner, dict):
                _add_deps(inner.get("asset_dependencies"), unit_root)
                tools = inner.get("tools")
                if isinstance(tools, list):
                    for i, t in enumerate(tools):
                        if isinstance(t, dict) and isinstance(t.get("tests"), str):
                            out.append((f"tools[{i}].tests", t["tests"]))
    return out


def check_asset_dependencies_resolve(body_text: str, skill_dir: Path) -> list[CheckResult]:
    """Resolve every declared runtime asset dependency (and tools[].tests path)
    against the skill dir, then the nearest project root. A declared path that
    resolves against neither is a broken asset edge: the consumer would break
    silently at runtime (md-domain's cohesion-principles.md runtime_asset_dependencies_declared)."""
    if not HAVE_YAML:
        return []
    declared = _declared_asset_paths(body_text)
    if not declared:
        return []

    project_root = _find_project_root(skill_dir)
    results: list[CheckResult] = []
    missing = 0
    for row_label, raw in declared:
        rel = raw.replace("${CLAUDE_PLUGIN_ROOT}/", "").lstrip("/")
        candidates = [skill_dir / rel]
        if project_root is not None:
            candidates.append(project_root / rel)
        if any(c.exists() for c in candidates):
            continue
        missing += 1
        results.append(CheckResult(
            f"yaml: {row_label}",
            FAIL,
            f"declared asset does not resolve against skill dir or project root: {raw}",
            rule="asset-paths-resolve",
        ))
    if not missing:
        results.append(CheckResult(
            "yaml: asset dependencies resolve",
            PASS,
            f"{len(declared)} declared path(s) resolve",
            rule="asset-paths-resolve",
        ))
    return results


_MEMBER_DIR_EXCLUDES = {"__pycache__", "node_modules", "venv"}
_MEMBER_FILE_EXCLUDE_SUFFIXES = (".pyc", ".pyo")


def _is_excluded_member_dir(d: Path) -> bool:
    """Directories that are never knowledge surfaces: hidden (VCS / venv /
    tool state), interpreter or build artifacts."""
    return (
        d.name.startswith(".")
        or d.name in _MEMBER_DIR_EXCLUDES
        or d.name.endswith(".egg-info")
    )


def _dir_has_content(d: Path) -> bool:
    """True when the directory holds at least one real file after exclusions
    (an empty dir, or one holding only __pycache__ / *.pyc, is not a member)."""
    for p in d.rglob("*"):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(d).parts
        if any(part.startswith(".") or part in _MEMBER_DIR_EXCLUDES
               or part.endswith(".egg-info") for part in rel_parts[:-1]):
            continue
        name = rel_parts[-1]
        if name.startswith(".") or name.endswith(_MEMBER_FILE_EXCLUDE_SUFFIXES):
            continue
        return True
    return False


def _mentions_file(text: str, f: Path) -> bool:
    """A load-graph edge to a file: its basename cited anywhere (path or bare),
    or the wiki-link form [[stem]]. The lookbehind stops a basename matching
    inside a longer one (state.md inside blind-state.md)."""
    if re.search(r"(?<![A-Za-z0-9_\-])" + re.escape(f.name), text):
        return True
    return f"[[{f.stem}]]" in text


def _mentions_dir(text: str, name: str) -> bool:
    """A load-graph edge to a directory: its name cited path-style (tests/,
    tests/conftest.py) or as the exact value of a structured ref (ref: tests)."""
    if re.search(r"(?<![A-Za-z0-9_\-])" + re.escape(name) + r"/", text):
        return True
    return bool(re.search(r":\s*" + re.escape(name) + r"\s*$", text, re.MULTILINE))


def _structured_index_paths(body_text: str) -> list[tuple[str, str, bool]]:
    """Collect (row_label, value, is_path) triples from the structured index
    surfaces of a skill-type unit: index.references[].path (always a path),
    index.members[].ref and a top-level members[].ref (a path OR a sibling
    skill / slash-command name -- union-domain skills index other skills).
    (tools[].tests and asset_dependencies[].path are resolved by
    check_asset_dependencies_resolve.)"""
    out: list[tuple[str, str, bool]] = []
    if not HAVE_YAML:
        return out
    units, _, _ = collect_yaml_units(body_text)
    for unit_root, block_data in units:
        if unit_root not in SKILL_TYPE_ROOTS:
            continue
        inner = block_data.get(unit_root)
        if not isinstance(inner, dict):
            continue
        index = inner.get("index")
        if isinstance(index, dict):
            refs = index.get("references")
            if isinstance(refs, list):
                for i, r in enumerate(refs):
                    if isinstance(r, dict) and isinstance(r.get("path"), str):
                        out.append((f"index.references[{i}].path", r["path"], True))
            members = index.get("members")
            if isinstance(members, list):
                for i, m in enumerate(members):
                    if isinstance(m, dict) and isinstance(m.get("ref"), str):
                        out.append((f"index.members[{i}].ref", m["ref"], False))
        members = inner.get("members")
        if isinstance(members, list):
            for i, m in enumerate(members):
                if isinstance(m, dict) and isinstance(m.get("ref"), str):
                    out.append((f"members[{i}].ref", m["ref"], False))
    return out


# Covered by the universal "references cited in body all exist" check (the
# index path appears literally in the body text and matches its regex); the
# structured resolution below skips these to avoid double-reporting.
_BODY_CITATION_COVERED = re.compile(r"references/[a-zA-Z0-9_\-]+\.md$")


def check_references_reachable_from_skill_md(body_text: str, skill_dir: Path) -> list[CheckResult]:
    """Rule references_reachable_from_skill_md (audit-framework registry,
    skill_md_audit / skill composition): every member of the skill composition
    is reachable from SKILL.md, and every structured index edge resolves.

    Three detections, all mechanical:
    - Orphaned reference: a file under references/ reachable from SKILL.md by
      NO path -- FAIL for .md (the routable knowledge surface whose designed
      inbound edge is SKILL.md), JUDGMENT for other files (may be
      script-consumed data whose declaration gap is asset_dependencies' rule).
    - Two-hop-only reference: an .md under references/ cited only from another
      reference doc, never from SKILL.md itself -- JUDGMENT (the index cannot
      route to it; the agent decides whether to add the direct edge).
    - Unlinked member directory: a content-bearing subdirectory of the skill
      (tests/, scripts/, templates/, ...) with no edge from SKILL.md at all --
      JUDGMENT (may be an internal helper dir, e.g. a lib/ only imported by
      scripts; the agent decides).
    Plus: a structured index/members path that does not resolve on disk (FAIL,
    dangling edge), skipping shapes the body-citation check already covers.
    """
    results: list[CheckResult] = []

    # -- dangling structured index edges ------------------------------------
    project_root = _find_project_root(skill_dir)
    for row_label, raw, is_path in _structured_index_paths(body_text):
        if _BODY_CITATION_COVERED.search(raw):
            continue
        if not is_path:
            # A members ref is a path only when it is slash-qualified and
            # relative. A leading slash is a slash-command reference; a bare
            # name is ambiguous (sibling skill name vs directory) -- both are
            # skipped here rather than risk a false dangling-edge FAIL
            # (skill-name resolution belongs to md-domain's references audit lane).
            if raw.startswith("/") or "/" not in raw:
                continue
        rel = raw.replace("${CLAUDE_PLUGIN_ROOT}/", "").lstrip("/").rstrip("/")
        candidates = [skill_dir / rel]
        if project_root is not None:
            candidates.append(project_root / rel)
        if not any(c.exists() for c in candidates):
            results.append(CheckResult(
                f"load-graph: {row_label}",
                FAIL,
                f"index entry points at a path that does not exist: {raw}",
                rule="refs-reachable",
            ))

    # -- reachability of references/ files ----------------------------------
    refs_dir = skill_dir / "references"
    ref_files = []
    if refs_dir.is_dir():
        ref_files = sorted(
            p for p in refs_dir.iterdir()
            if p.is_file() and not p.name.startswith(".")
            and not p.name.endswith(_MEMBER_FILE_EXCLUDE_SUFFIXES)
        )

    direct = {p for p in ref_files if _mentions_file(body_text, p)}
    # Transitive closure over reachable reference docs (only .md docs carry
    # onward citations; a directly-cited doc may route to a sibling one hop).
    reachable = set(direct)
    frontier = [p for p in direct if p.suffix == ".md"]
    doc_text: dict[Path, str] = {}
    citer_of: dict[Path, Path] = {}
    while frontier:
        doc = frontier.pop()
        if doc not in doc_text:
            try:
                doc_text[doc] = doc.read_text(encoding="utf-8")
            except OSError:
                doc_text[doc] = ""
        text = doc_text[doc]
        for p in ref_files:
            if p in reachable:
                continue
            if _mentions_file(text, p):
                reachable.add(p)
                citer_of[p] = doc
                if p.suffix == ".md":
                    frontier.append(p)

    for p in ref_files:
        rel = f"references/{p.name}"
        if p in direct:
            continue
        if p in reachable:
            if p.suffix == ".md":
                citer = citer_of.get(p)
                via = f"references/{citer.name}" if citer else "a sibling reference"
                results.append(CheckResult(
                    f"load-graph: {rel}",
                    JUDGMENT,
                    f"not cited or indexed from SKILL.md; reachable only via {via} "
                    "(two hops -- the index cannot route to it). Add a direct edge "
                    "or confirm the routing is intentional.",
                    rule="refs-reachable",
                ))
            continue
        results.append(CheckResult(
            f"load-graph: {rel}",
            FAIL if p.suffix == ".md" else JUDGMENT,
            "orphaned reference: no edge from SKILL.md or any reachable reference "
            "points at this file. Add an index entry / citation, or delete it.",
            rule="refs-reachable",
        ))

    # -- member directories with no SKILL.md edge ---------------------------
    all_docs_text = body_text + "".join(doc_text.get(p, "") for p in reachable
                                        if p.suffix == ".md")
    member_dirs = sorted(
        d for d in skill_dir.iterdir()
        if d.is_dir() and d.name != "references" and not _is_excluded_member_dir(d)
        and _dir_has_content(d)
    )
    unlinked = []
    for d in member_dirs:
        if _mentions_dir(body_text, d.name):
            continue
        note_suffix = ""
        if _mentions_dir(all_docs_text, d.name):
            note_suffix = " (it is mentioned from a reference doc -- two hops)"
        unlinked.append((d, note_suffix))
    for d, note_suffix in unlinked:
        results.append(CheckResult(
            f"load-graph: {d.name}/",
            JUDGMENT,
            f"skill member directory has no edge from SKILL.md{note_suffix}. "
            "An agent cannot discover it; index it (index.members / tools[].tests "
            "/ a citation) or confirm it is an internal helper.",
            rule="refs-reachable",
        ))

    if (ref_files or member_dirs) and not results:
        results.append(CheckResult(
            "load-graph: members reachable from SKILL.md",
            PASS,
            f"{len(ref_files)} reference file(s) reachable "
            f"({len(direct)} direct), {len(member_dirs)} member dir(s) linked",
            rule="refs-reachable",
        ))
    return results


def check_cross_block_drift(body_text: str) -> CheckResult | None:
    """Cross-block mixed-type drift detection."""
    if not HAVE_YAML:
        return None
    units, _, _ = collect_yaml_units(body_text)
    skill_type_roots = sorted({root for (root, _) in units if root in SKILL_TYPE_ROOTS})
    if len(skill_type_roots) > 1:
        return CheckResult(
            "yaml: single skill-type root (cross-block)",
            FAIL,
            f"multiple skill-type roots across blocks (mixed-type drift): {skill_type_roots}",
            rule="cross-block-drift",
        )
    return None


_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_LINK_RE = re.compile(r"\]\(\s*<?([^)>\s]+)>?(?:\s+[^)]*)?\)")
_LINK_DEF_RE = re.compile(r"^ {0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)")
_IMPORT_RE = re.compile(r"^@(\S+)")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _split_fences(content: str) -> tuple[list[tuple[int, str]], bool]:
    """Return ([(line_number, line)] outside code fences, fence_left_open).

    CommonMark fence rule: a closing fence uses the opening character, is at
    least as long, and carries no info string. Text after an unclosed opener
    counts as inside the fence.
    """
    outside: list[tuple[int, str]] = []
    open_char = ""
    open_len = 0
    for n, line in enumerate(content.splitlines(), 1):
        m = _FENCE_RE.match(line)
        if open_char:
            if (m and m.group(1)[0] == open_char and len(m.group(1)) >= open_len
                    and not m.group(2).strip()):
                open_char, open_len = "", 0
            continue
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            open_char, open_len = m.group(1)[0], len(m.group(1))
            continue
        outside.append((n, line))
    return outside, bool(open_char)


def _link_target_path(target: str, base: Path) -> Path | None:
    """The on-disk path a relative link target names, or None when the target
    is not checkable (URL/scheme, fragment-only, absolute)."""
    if not target or target.startswith(("#", "/", "\\")) or _SCHEME_RE.match(target):
        return None
    path_part = unquote(re.split(r"[#?]", target, maxsplit=1)[0])
    if not path_part:
        return None
    return base / path_part


def _iter_imports(
    outside: list[tuple[int, str]], base: Path
) -> list[tuple[int, str, Path | None, bool]]:
    """The `@path` imports among the lines outside code fences.

    Returns (line number, target as written, resolved path, missing) for each
    line that opens with `@` followed by a token containing `.` or `/`. A
    relative target that does not exist is reported with missing=True; a
    `~` or absolute target is resolved but never checked for existence.
    """
    found: list[tuple[int, str, Path | None, bool]] = []
    for n, line in outside:
        m = _IMPORT_RE.match(line)
        if not m or not re.search(r"[./]", m.group(1)):
            continue
        t = m.group(1)
        if t.startswith("~"):
            p: Path | None = Path(t).expanduser()
        elif t.startswith("/"):
            p = Path(t)
        else:
            p = _link_target_path(t, base)
            if p is not None and not p.exists():
                found.append((n, t, p, True))
                continue
        found.append((n, t, p, False))
    return found


def check_claude_md_defaults(
    path: Path,
    content: str,
    body: Body | None = None,
    th: dict[str, int] | None = None,
) -> list[CheckResult]:
    """The inoffensive rules for every CLAUDE.md / AGENTS.md, with or without a
    claude_md: block: links resolve, fences closed. Independent of the
    directory dimension classifier and of sibling files; reads only the file
    itself and its link/import targets. The optional rules (import size, size
    signal, record floor) are shipped standards criteria, evaluated by
    MECHANICAL_EVALUATORS.

    `body` and `th` are the deprecated arguments of the former call shape
    `check_claude_md_defaults(path, content, body, th)`. Passing them emits a
    DeprecationWarning and the call also returns the two rows that shape used
    to append (import size, then body size signal), computed by the shipped
    evaluators with `th`. Pass both or neither."""
    if (body is None) != (th is None):
        raise TypeError("check_claude_md_defaults takes body and th together (deprecated arguments) or neither")
    legacy_shape = body is not None
    if legacy_shape:
        warnings.warn(
            _deprecation_message(
                "check_claude_md_defaults(body, th)",
                "check_claude_md_defaults(path, content) for the link and fence rows, and "
                "audit_claude_md for the import-size and size-signal criteria",
            ),
            DeprecationWarning,
            stacklevel=2,
        )
    base = path.parent
    outside, unclosed = _split_fences(content)

    bad_links: list[str] = []
    for n, line in outside:
        scan = _INLINE_CODE_RE.sub("", line)
        targets = _LINK_RE.findall(scan)
        d = _LINK_DEF_RE.match(scan)
        if d:
            targets.append(d.group(1))
        for t in targets:
            p = _link_target_path(t, base)
            if p is not None and not os.path.exists(p):
                bad_links.append(f"line {n}: {t}")

    for n, t, _p, missing in _iter_imports(outside, base):
        if missing:
            bad_links.append(f"line {n}: @{t} (missing import)")

    out: list[CheckResult] = []
    out.append(CheckResult(
        "relative markdown links resolve",
        FAIL if bad_links else PASS,
        "; ".join(bad_links[:10]) + (f"; +{len(bad_links) - 10} more" if len(bad_links) > 10 else ""),
        rule="claude-md-links-resolve",
    ))
    out.append(CheckResult(
        "code fences closed",
        FAIL if unclosed else PASS,
        "a ``` or ~~~ fence is opened and never closed" if unclosed else "",
        rule="claude-md-fences-closed",
    ))
    if legacy_shape:
        legacy_th = {"import_max_lines": _DEPRECATED_IMPORT_MAX_LINES, **th}
        out.extend(row for _section, row in _criterion_rows(
            ClaudeMdContext(path, content, body, None),
            [MechanicalCriterion("claude-md-import-size", "fail"),
             MechanicalCriterion("claude-md-size-signal", "judgment")],
            legacy_th,
        ))
    return out


def audit_claude_md(
    claude_md_path: Path,
    content: str,
    resolved: "ResolvedStandards | None" = None,
) -> dict[str, Any]:
    """Audit an instruction file (CLAUDE.md / AGENTS.md).

    Default rules (universal rows) run on every file. The claude_md: block is
    optional: when present it is schema-validated; when absent the contract row
    is n/a and the file is validated against the defaults alone.
    """
    criteria, criterion_defaults = mechanical_criteria("claude_md", resolved)
    th = {**THRESHOLDS, **criterion_defaults, **(resolved.thresholds if resolved else {})}
    body = parse_body(content)
    universal = check_claude_md_defaults(claude_md_path, content)
    yaml_data, yaml_err, detected_root = extract_claude_md_unit(body.text)
    yaml_results: list[CheckResult] = []
    yaml_root: str | None = None

    if yaml_data is not None:
        if "claude_md" not in yaml_data:
            # Only other contract roots were found: no claude_md contract.
            roots = [k for k in yaml_data if k in SCHEMAS_BY_ROOT]
            yaml_results.append(CheckResult(
                "yaml: claude_md contract block",
                NA,
                "no claude_md: contract block; file validated against default rules",
                rule="yaml-contract",
            ))
            yaml_results.append(CheckResult(
                "yaml: non-claude_md contract root",
                JUDGMENT,
                f"non-claude_md contract root '{roots[0] if roots else '?'}' in an "
                "instruction file (standards section 6.2) -- example or misplaced?",
                rule="yaml-contract",
            ))
        else:
            yaml_results, yaml_root = check_yaml_contract(yaml_data)
    elif yaml_err == "no-yaml-parser":
        yaml_results.append(CheckResult(
            f"yaml: contract block detected (root='{detected_root}')",
            JUDGMENT,
            "pyyaml not installed; YAML contract validation unavailable.",
            rule="yaml-contract",
        ))
    elif yaml_err.startswith("yaml-parse-error"):
        yaml_results.append(CheckResult(
            f"yaml: claude_md contract block (root='{detected_root}')",
            FAIL,
            "fenced yaml block found but failed to parse -- "
            + yaml_err.split(": ", 1)[1],
            rule="yaml-contract",
        ))
    else:
        yaml_results.append(CheckResult(
            "yaml: claude_md contract block",
            NA,
            "no claude_md: contract block; file validated against default rules",
            rule="yaml-contract",
        ))

    inner = yaml_data.get("claude_md") if yaml_data is not None else None
    ctx = ClaudeMdContext(claude_md_path, content, body, inner if isinstance(inner, dict) else None)
    for section, row in _criterion_rows(ctx, criteria, th):
        (yaml_results if section == "yaml_contract" else universal).append(row)

    disabled = resolved.disabled_rules if resolved else set()
    universal_rows = [asdict(r) for r in universal]
    yaml_rows = [asdict(r) for r in yaml_results]
    if disabled:
        universal_rows = [r for r in universal_rows if r["rule"] not in disabled]
        yaml_rows = [r for r in yaml_rows if r["rule"] not in disabled]

    return {
        "path": str(claude_md_path),
        "kind": "claude_md",
        "declared_type": None,
        "yaml_root": yaml_root,
        "universal": universal_rows,
        "yaml_contract": yaml_rows,
        "type_specific": [],
        "mixed_type": asdict(CheckResult("mixed-type signal (n/a for CLAUDE.md)", NA, rule="mixed-type")),
    }


def mechanical_criteria(
    applies_to: str,
    resolved: "ResolvedStandards | None",
) -> tuple[list[MechanicalCriterion], dict[str, int]]:
    """The mechanical criteria of one file-type primitive to evaluate, and their
    threshold defaults. applies_to is "skill_md" or "claude_md".

    - resolved given: its standards files for that primitive, in layer then
      file order, keeping each `enforcement: mechanical` criterion
      (standards_resolve has already rejected any without a registered
      evaluator).
    - resolved None, pyyaml available: the shipped layer alone
      (standards_resolve.resolve_shipped), so a plain audit stays deterministic
      and ignores user and project layers.
    - resolved None, no pyyaml: the shipped files cannot be parsed. Their
      presence is still verified, and each registered evaluator for the
      primitive yields a deferred row; no criterion default is assumed.

    A missing or invalid shipped standards file raises StandardsConfigError.
    Returns (criteria, criterion-declared threshold defaults).
    """
    from . import standards_resolve

    if resolved is None:
        if not standards_resolve.HAVE_YAML:
            standards_resolve.require_shipped_standards()
            deferred = [
                MechanicalCriterion(rid, None)
                for rid, ev in MECHANICAL_EVALUATORS.items()
                if ev.applies_to == applies_to
            ]
            return deferred, {}
        resolved = standards_resolve.resolve_shipped()
    criteria = [
        MechanicalCriterion(c["id"], c["severity"])
        for sf in resolved.standards_by_primitive.get(applies_to, [])
        for c in sf.criteria
        if c.get("enforcement") == "mechanical"
    ]
    return criteria, dict(resolved.threshold_defaults)


def audit(skill_md_path: Path, resolved: "ResolvedStandards | None" = None) -> dict[str, Any]:
    if not skill_md_path.exists():
        return {"error": f"file not found: {skill_md_path}"}
    content = skill_md_path.read_text(encoding="utf-8")

    if skill_md_path.name.lower() == "claude.md":
        return audit_claude_md(skill_md_path, content, resolved)
    if skill_md_path.name.lower() == "agents.md":
        # An AGENTS.md is an instruction file only when its directory has no
        # CLAUDE.md; a shadowed one is not audited.
        if is_active_instruction_file(skill_md_path):
            return audit_claude_md(skill_md_path, content, resolved)
        return {"error": f"not audited: {skill_md_path} is shadowed by a CLAUDE.md in the same directory"}

    skill_dir = skill_md_path.parent

    # Resolved after the CLAUDE.md / AGENTS.md dispatch above, which resolves
    # its own claude_md criteria.
    skill_criteria, criterion_defaults = mechanical_criteria("skill_md", resolved)

    # Effective thresholds: audit defaults, then criterion-declared defaults,
    # overlaid by any resolved overrides. The module-level THRESHOLDS dict is
    # never mutated (this run only).
    th = {**THRESHOLDS, **criterion_defaults, **(resolved.thresholds if resolved else {})}

    # mode="full" (real YAML parse) resolves folded/multi-line description
    # blocks and quoted values correctly; mode="light" (same-line regex) is
    # kept as the fallback for a bare interpreter without pyyaml, preserving
    # the documented contract-staged/legacy-fallback degradation (I1).
    fm = parse_frontmatter(content, mode="full" if HAVE_YAML else "light")
    body = parse_body(content)

    universal = check_universal(fm, body, skill_dir, th, skill_criteria=skill_criteria)
    universal.extend(check_references_reachable_from_skill_md(body.text, skill_dir))
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
            rule="yaml-contract",
        ))
    elif yaml_err == "no-yaml-parser-no-block":
        yaml_results.append(CheckResult(
            "yaml: parser available + contract block",
            JUDGMENT,
            "pyyaml not installed AND no yaml contract block found; falling back to legacy markdown heuristics",
            rule="yaml-contract",
        ))
    elif yaml_err.startswith("yaml-parse-error"):
        yaml_root = detected_root
        yaml_results.append(CheckResult(
            f"yaml: contract block (root='{detected_root}')",
            FAIL,
            "fenced yaml block found but failed to parse -- "
            + yaml_err.split(": ", 1)[1],
            rule="yaml-contract",
        ))
    else:
        yaml_results.append(CheckResult(
            "yaml: contract block",
            JUDGMENT,
            "no fenced yaml contract block with a recognized root key; falling back to legacy markdown heuristics",
            rule="yaml-contract",
        ))

    yaml_results.extend(check_portable_units(body.text))
    # The yaml_contract-section criteria (facts and caution floors) read the
    # parsed units and the resolved root, so they run once both exist.
    contract_ctx = SkillContext(
        fm if fm is not None else Frontmatter(raw="", fields={}), body, skill_dir, yaml_root)
    yaml_results.extend(
        row for _section, row in _criterion_rows(contract_ctx, skill_criteria, th, section="yaml_contract"))
    yaml_results.extend(check_asset_dependencies_resolve(body.text, skill_dir))

    cross_block_drift = check_cross_block_drift(body.text)
    if cross_block_drift is not None:
        yaml_results.append(cross_block_drift)

    # Legacy heuristics run on the NARRATIVE body -- code fences stripped, like
    # mixed_type_signal (strip_code_fences_before_heuristics insight): numbered
    # lists / markers inside fenced examples must not count. Each evaluator's
    # `applies` gate holds only with no staged contract and a matching declared
    # skill-type, so the section is empty for every other document.
    narrative_body = Body(
        text=strip_code_fences(body.text),
        lines=body.lines,
        tokens_approx=body.tokens_approx,
    )
    type_ctx = SkillContext(contract_ctx.fm, narrative_body, skill_dir, yaml_root, contract_staged)
    type_specific: list[CheckResult] = [
        row for _section, row in _criterion_rows(type_ctx, skill_criteria, th, section="type_specific")
    ]

    if not contract_staged:
        # threshold=th["mixed_min_score"] gates each TYPE's score (I5, the
        # same per-type gate classify.py applies via high_scoring_types);
        # "mixed" itself means 2+ such types contend, same as classify's
        # heuristic-fallback verdict.
        score, signals = mixed_type_signal(body.text, th["mixed_min_score"])
        if score >= 2:
            mixed = CheckResult(
                "mixed-type signal (legacy heuristic)",
                JUDGMENT,
                f"score={score}: {signals}",
                rule="mixed-type",
            )
        else:
            mixed = CheckResult("mixed-type signal (legacy heuristic)", PASS, f"score={score}", rule="mixed-type")
    elif yaml_data is not None:
        roots_present = detect_mixed_type_yaml(yaml_data)
        if len(roots_present) > 1:
            mixed = CheckResult(
                "mixed-type signal (deterministic)",
                FAIL,
                f"multiple root keys present: {roots_present}",
                rule="mixed-type",
            )
        else:
            mixed = CheckResult("mixed-type signal (deterministic)", PASS, f"single root: {roots_present[0] if roots_present else 'none'}", rule="mixed-type")
    else:
        mixed = CheckResult(
            "mixed-type signal (deferred)",
            JUDGMENT,
            f"contract block detected (root='{detected_root}') but pyyaml unavailable; cannot determine deterministically",
            rule="mixed-type",
        )

    disabled = resolved.disabled_rules if resolved else set()
    universal_rows = [asdict(r) for r in universal]
    yaml_rows = [asdict(r) for r in yaml_results]
    type_rows = [asdict(r) for r in type_specific]
    if disabled:
        universal_rows = [r for r in universal_rows if r["rule"] not in disabled]
        yaml_rows = [r for r in yaml_rows if r["rule"] not in disabled]
        type_rows = [r for r in type_rows if r["rule"] not in disabled]

    return {
        "path": str(skill_md_path),
        "declared_type": declared_type,
        "yaml_root": yaml_root,
        "universal": universal_rows,
        "yaml_contract": yaml_rows,
        "type_specific": type_rows,
        "mixed_type": asdict(mixed),
    }


# ---------------------------------------------------------------------------
# Deprecated names. Each keeps its former call signature and returns what the
# former function returned, by delegating to the shipped criteria through the
# current evaluator path. They emit DeprecationWarning for the policy window
# (docs/reference/deprecation-policy.md) and are removed in _REMOVABLE_IN;
# contract/public-surface.json records the dates.
# ---------------------------------------------------------------------------

#: The legacy per-skill-type criteria a former check_<type>_skill function ran,
#: in the order it emitted their rows.
_LEGACY_SKILL_RULES: dict[str, tuple[str, ...]] = {
    "reference-skill": (
        "ref-example-block", "ref-gotcha-block", "ref-prohibited-discipline", "ref-prohibited-checklist",
    ),
    "pattern-skill": (
        "pattern-recognition-block", "pattern-counter-example", "pattern-prohibited-bundle",
        "pattern-prohibited-checklist", "pattern-prohibited-rule-counter",
    ),
    "technique-skill": (
        "technique-ordered-steps", "step-tracking", "technique-prohibited-pressure-test",
    ),
    "technique-wrapper": (
        "wrapper-ordered-step", "wrapper-procedure-cited", "wrapper-max-lines",
    ),
    "discipline-skill": (
        "discipline-rule-counter", "discipline-red-flags", "discipline-pressure-test",
    ),
    "domain-skill": (
        "domain-identity-sentence", "domain-companion-declaration", "domain-orientation",
        "domain-reference-index", "domain-prohibited-index-only",
    ),
}


def _legacy_skill_rows(skill_type: str, body: Body, skill_dir: Path) -> list[CheckResult]:
    """The rows of the legacy criteria of `skill_type` for `body`. A criterion
    with no severity takes its evaluator's bare_severity, which a test pins
    equal to the severity the shipped SKILL-legacy-standards.md declares, so
    the verdicts do not depend on pyyaml or on a user's standards layers."""
    ctx = SkillContext(Frontmatter(raw="", fields={"skill-type": skill_type}), body, skill_dir)
    criteria = [MechanicalCriterion(rule_id, None) for rule_id in _LEGACY_SKILL_RULES[skill_type]]
    return [row for _section, row in _criterion_rows(ctx, criteria, THRESHOLDS)]


def check_reference_skill(body: Body, skill_dir: Path) -> list[CheckResult]:
    """Deprecated: the ref-* criteria of the shipped SKILL-legacy-standards.md,
    evaluated by audit() for a document declaring skill-type reference-skill."""
    warnings.warn(
        _deprecation_message("check_reference_skill", "audit() (the ref-* criteria in type_specific)"),
        DeprecationWarning,
        stacklevel=2,
    )
    return _legacy_skill_rows("reference-skill", body, skill_dir)


def check_pattern_skill(body: Body, skill_dir: Path) -> list[CheckResult]:
    """Deprecated: the pattern-* criteria of the shipped SKILL-legacy-standards.md."""
    warnings.warn(
        _deprecation_message("check_pattern_skill", "audit() (the pattern-* criteria in type_specific)"),
        DeprecationWarning,
        stacklevel=2,
    )
    return _legacy_skill_rows("pattern-skill", body, skill_dir)


def check_technique_skill(body: Body, skill_dir: Path, fm: Frontmatter | None = None) -> list[CheckResult]:
    """Deprecated: the technique-* and step-tracking criteria of the shipped
    SKILL-legacy-standards.md. `fm` is accepted and never read, as before."""
    warnings.warn(
        _deprecation_message(
            "check_technique_skill",
            "audit() (the technique-* and step-tracking criteria in type_specific)",
        ),
        DeprecationWarning,
        stacklevel=2,
    )
    return _legacy_skill_rows("technique-skill", body, skill_dir)


def check_discipline_skill(body: Body, skill_dir: Path) -> list[CheckResult]:
    """Deprecated: the discipline-* criteria of the shipped SKILL-legacy-standards.md."""
    warnings.warn(
        _deprecation_message("check_discipline_skill", "audit() (the discipline-* criteria in type_specific)"),
        DeprecationWarning,
        stacklevel=2,
    )
    return _legacy_skill_rows("discipline-skill", body, skill_dir)


def check_domain_skill(body: Body, skill_dir: Path) -> list[CheckResult]:
    """Deprecated: the domain-* criteria of the shipped SKILL-legacy-standards.md."""
    warnings.warn(
        _deprecation_message("check_domain_skill", "audit() (the domain-* criteria in type_specific)"),
        DeprecationWarning,
        stacklevel=2,
    )
    return _legacy_skill_rows("domain-skill", body, skill_dir)


def _bare_body(text: str) -> Body:
    return Body(text=text, lines=len(text.splitlines()), tokens_approx=int(len(text.split()) * 1.3))


def check_facts_cross_rules(
    body_text: str,
    declared_type: str | None,
    yaml_root: str | None = None,
) -> list[CheckResult]:
    """Deprecated: the facts-floor, facts-gotcha and facts-example criteria,
    evaluated by audit() in the yaml_contract section. Gated as before: on the
    YAML root when one is given, otherwise on `declared_type`; empty without
    pyyaml."""
    warnings.warn(
        _deprecation_message(
            "check_facts_cross_rules", "audit() (the facts-floor, facts-gotcha and facts-example criteria)"
        ),
        DeprecationWarning,
        stacklevel=2,
    )
    fields = {"skill-type": declared_type} if declared_type is not None else {}
    ctx = SkillContext(Frontmatter(raw="", fields=fields), _bare_body(body_text), Path("."), yaml_root)
    criteria = [MechanicalCriterion(rule_id, "fail") for rule_id in ("facts-floor", "facts-gotcha", "facts-example")]
    return [row for _section, row in _criterion_rows(ctx, criteria, THRESHOLDS)]


def check_technique_caution_cross_rule(body_text: str) -> list[CheckResult]:
    """Deprecated: the caution-floor criterion, evaluated by audit() in the
    yaml_contract section. Empty without pyyaml or without a technique_skill unit."""
    warnings.warn(
        _deprecation_message("check_technique_caution_cross_rule", "audit() (the caution-floor criterion)"),
        DeprecationWarning,
        stacklevel=2,
    )
    ctx = SkillContext(Frontmatter(raw="", fields={}), _bare_body(body_text), Path("."))
    return [row for _section, row in _criterion_rows(ctx, [MechanicalCriterion("caution-floor", "fail")], THRESHOLDS)]


def check_claude_md_record_floor(yaml_data: dict) -> CheckResult | None:
    """Deprecated: the claude-md-record-floor criterion, evaluated by
    audit_claude_md() in the yaml_contract section. None when `yaml_data` has no
    claude_md mapping."""
    warnings.warn(
        _deprecation_message(
            "check_claude_md_record_floor", "audit_claude_md() (the claude-md-record-floor criterion)"
        ),
        DeprecationWarning,
        stacklevel=2,
    )
    inner = yaml_data.get("claude_md")
    ctx = ClaudeMdContext(Path("."), "", _bare_body(""), inner if isinstance(inner, dict) else None)
    rows = _criterion_rows(ctx, [MechanicalCriterion("claude-md-record-floor", "fail")], THRESHOLDS)
    return rows[0][1] if rows else None


def skill_md_criteria(
    resolved: "ResolvedStandards | None",
) -> tuple[list[MechanicalCriterion], dict[str, int]]:
    """Deprecated: mechanical_criteria("skill_md", resolved)."""
    warnings.warn(
        _deprecation_message("skill_md_criteria", 'mechanical_criteria("skill_md", resolved)'),
        DeprecationWarning,
        stacklevel=2,
    )
    return mechanical_criteria("skill_md", resolved)


def __getattr__(name: str) -> Any:
    """Serve the deprecated module-level names that are not defined here, so
    that an access warns: SkillCriterion (now MechanicalCriterion) and
    TYPE_RUNNERS (the former skill-type -> check function table)."""
    if name == "SkillCriterion":
        warnings.warn(_deprecation_message("SkillCriterion", "MechanicalCriterion"), DeprecationWarning, stacklevel=2)
        return MechanicalCriterion
    if name == "TYPE_RUNNERS":
        warnings.warn(
            _deprecation_message("TYPE_RUNNERS", "audit() (the criteria in the type_specific section)"),
            DeprecationWarning,
            stacklevel=2,
        )
        return {
            "reference-skill": check_reference_skill,
            "pattern-skill": check_pattern_skill,
            "discipline-skill": check_discipline_skill,
            "domain-skill": check_domain_skill,
        }
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
        description="Audit a SKILL.md against the md-domain framework.",
    )
    parser.add_argument("path", help="Path to SKILL.md")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of formatted text")
    parser.add_argument(
        "--config",
        action="store_true",
        help="Resolve and apply the layered skills-kit standards config "
             "(disabled rules + threshold overrides). Off by default so existing "
             "invocations stay deterministic.",
    )
    args = parser.parse_args(argv)

    resolved = None
    if args.config:
        from . import standards_resolve
        project_root = _find_project_root(Path(args.path).resolve())
        try:
            resolved = standards_resolve.resolve(project_root)
        except standards_resolve.StandardsConfigError as exc:
            # --config asked for the layered config. A layer that cannot be
            # read -- malformed, or no pyyaml on this interpreter -- must stop
            # the run, not fall back to an audit that ignored it.
            print(f"audit --config: {exc}", file=sys.stderr)
            return 1

    from . import standards_resolve

    try:
        report = audit(Path(args.path), resolved)
    except standards_resolve.StandardsConfigError as exc:
        # A missing or invalid shipped standards layer stops the run: an audit
        # that skipped those criteria would read exactly like one that passed.
        print(f"audit: {exc}", file=sys.stderr)
        return 1
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
