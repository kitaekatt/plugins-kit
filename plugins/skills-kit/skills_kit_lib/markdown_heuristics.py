"""Structural-shape detectors for SKILL.md body text.

Stdlib-only heuristic detectors. Used by classify.py to score a SKILL.md
against the canonical skill types, and by audit.py to flag mixed-type drift
in skills that haven't adopted the YAML-contract layer.
"""

import re
from dataclasses import dataclass, field

from .schema_registry import SKILL_TYPE_ROOTS


FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
FIELD_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_-]*)\s*:\s*(.+?)\s*$", re.MULTILINE)

# Canonical dashed type names, derived from the registry's skill-type roots
# (single source of truth; do not restate the type list here). An ORDERED
# tuple, not a set: SKILL_TYPE_ROOTS is itself a deterministic
# registration-order tuple, and type_signals' scores dict below inherits
# CANONICAL_TYPES' iteration order -- a set here made classify's stable sort
# (and its "top types tie" reason) vary with PYTHONHASHSEED across
# interpreter starts (I2).
CANONICAL_TYPES = tuple(root.replace("_", "-") for root in SKILL_TYPE_ROOTS)


@dataclass
class Frontmatter:
    raw: str
    fields: dict = field(default_factory=dict)


@dataclass
class Body:
    text: str
    lines: int
    tokens_approx: int


def parse_frontmatter(content: str, mode: str = "light"):
    """Parse YAML frontmatter into a Frontmatter record. The ONE frontmatter
    parser for the plugin (consumers must not re-implement it).

    mode="light" (default): stdlib regex field extraction -- flat `key: value`
        pairs, quote-stripped string values. Always available.
    mode="full": pyyaml parse of the frontmatter block -- typed values (bools,
        lists, nested maps). Degrades to EMPTY fields when pyyaml is
        unavailable or the YAML is invalid/non-dict (the contract-staged
        degradation corpus discovery relies on).

    Returns None when the document has no leading --- block.
    """
    m = FRONTMATTER_RE.match(content)
    if not m:
        return None
    raw = m.group(1)
    fm = Frontmatter(raw=raw)
    if mode == "full":
        try:
            import yaml as _pyyaml  # guarded; pyyaml is optional
            parsed = _pyyaml.safe_load(raw)
            if isinstance(parsed, dict):
                fm.fields = parsed
        except Exception:
            pass
        return fm
    for name, val in FIELD_RE.findall(raw):
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        fm.fields[name] = val
    return fm


def parse_body(content: str) -> Body:
    m = FRONTMATTER_RE.match(content)
    body_text = content[m.end():] if m else content
    lines = body_text.splitlines()
    words = body_text.split()
    tokens_approx = int(len(words) * 1.3)
    return Body(text=body_text, lines=len(lines), tokens_approx=tokens_approx)


def strip_code_fences(body_text: str) -> str:
    """Return body with fenced code blocks (```...```) removed.

    Skill bodies are written for Claude. Structured data inside fenced blocks
    (yaml, json, python, etc.) is reference content for machine comprehension,
    not narrative or procedure. Type-signal heuristics should operate on the
    narrative body without code-block contamination.
    """
    return re.sub(r"```.*?```", "", body_text, flags=re.DOTALL)


def has_yaml_block(body_text: str) -> bool:
    """Detect a fenced YAML block. Strong reference-content signal."""
    return bool(re.search(r"^```ya?ml\s*$", body_text, re.MULTILINE))


def has_heading(body_text: str, *names: str) -> bool:
    pattern = r"^#{1,6}\s+(?:" + "|".join(re.escape(n) for n in names) + r")\b"
    return bool(re.search(pattern, body_text, re.MULTILINE | re.IGNORECASE))


def count_ordered_steps(body_text: str) -> int:
    return len(re.findall(r"^\s*\d+\.\s+\S", body_text, re.MULTILINE))


def has_tickbox_list(body_text: str) -> bool:
    return bool(re.search(r"^\s*-\s*\[\s?\]", body_text, re.MULTILINE))


def has_step_tracker_invocation(body_text: str) -> bool:
    """Detect an explicit step-tracker invocation in the procedure body.

    Per Dec-8: the workflow-checklist conditional row is satisfied by EITHER a
    paste-able `- [ ]` checklist OR an explicit step-tracker invocation.
    """
    if re.search(r"\b(TaskCreate|TaskWrite|TodoWrite)\b", body_text):
        return True
    if re.search(
        r"\b(track\s+(?:the\s+)?steps?\s+(?:in|with)|step\s+tracker|track\s+progress\s+in|scratch\s+file\s+for\s+steps?)\b",
        body_text,
        re.IGNORECASE,
    ):
        return True
    return False


def has_excuse_reality_table(body_text: str) -> bool:
    """Detect an excuse -> reality rule/counter structure.

    Matches a table or record SHAPE, not vocabulary: a markdown table row
    carrying both an `excuse` and a `reality` column, or a record-style pair
    of `excuse:` / `reality:` fields. A skill merely *discussing*
    rationalization in prose must not trip this discipline-content signal.
    """
    for line in body_text.splitlines():
        if "|" not in line:
            continue
        if re.search(r"\|\s*excuses?\s*\|", line, re.IGNORECASE) and re.search(
            r"\|\s*reality\s*\|", line, re.IGNORECASE
        ):
            return True
    if re.search(r"^\s*[-*]?\s*\*{0,2}excuse\*{0,2}\s*:", body_text, re.IGNORECASE | re.MULTILINE) and re.search(
        r"^\s*[-*]?\s*\*{0,2}reality\*{0,2}\s*:", body_text, re.IGNORECASE | re.MULTILINE
    ):
        return True
    return False


def has_red_green_refactor(body_text: str) -> bool:
    return bool(re.search(r"\bRED\s*[-/>]+\s*GREEN\s*[-/>]+\s*REFACTOR\b", body_text, re.IGNORECASE))


def has_red_flags_list(body_text: str) -> bool:
    return has_heading(body_text, "Red flags", "Red Flags")


def has_conditional_loading(body_text: str) -> bool:
    return has_heading(body_text, "Conditional Loading", "Conditional loading")


def has_companion_declaration(body_text: str) -> bool:
    if re.search(
        r"^#{1,6}\s+(?:Companion declaration|Companion Declaration|Companions?)\b",
        body_text,
        re.MULTILINE,
    ):
        return True
    if re.search(r"\bno sibling\s+domains?\b", body_text, re.IGNORECASE):
        return True
    if re.search(r"\bcompanion\s+domains?\b", body_text, re.IGNORECASE):
        return True
    return False


def has_recognition_marker(body_text: str) -> bool:
    return bool(re.search(r"\b(recogn(?:ize|ition)|applies\s+when)\b", body_text, re.IGNORECASE))


def has_counter_example(body_text: str) -> bool:
    return bool(re.search(r"\bcounter[- ]example|\bdo\s+NOT\s+apply", body_text, re.IGNORECASE))


def has_lookup_table(body_text: str) -> bool:
    """Detect a markdown table with at least 3 columns (suggests reference-style lookup)."""
    return bool(re.search(r"^\|.+\|.+\|.+\|$", body_text, re.MULTILINE))


def is_user_only(fm) -> bool:
    """User-only attribute: frontmatter sets disable-model-invocation: true."""
    if fm is None:
        return False
    val = fm.fields.get("disable-model-invocation")
    return val is not None and str(val).lower() == "true"


def high_scoring_types(scores: dict, threshold: int) -> list:
    """The canonical types whose type_signals() score is at or above
    `threshold`, in CANONICAL_TYPES order. The ONE per-type gate shared by
    classify.py's heuristic-fallback verdict and audit.py's legacy mixed-type
    signal (I5) -- do not re-implement this comparison at either call site."""
    return [t for t in CANONICAL_TYPES if scores.get(t, 0) >= threshold]


def type_signals(body_text: str, fm=None) -> dict:
    """Score each canonical skill type based on structural markers in the body.

    Returns a dict mapping canonical type names to integer scores.
    """
    narrative = strip_code_fences(body_text)
    # Every canonical type gets a key (some callers, e.g. high_scoring_types,
    # iterate CANONICAL_TYPES order and expect the full set present) --
    # "audit-skill" is deliberately never incremented below (I6): its
    # identity lives entirely in the structured criteria / taxonomy /
    # procedures / remediations YAML contract (skill-standards.md's
    # audit-skill contract), not in any narrative-only marker this
    # legacy-fallback heuristic set could reliably score without guessing.
    scores = {t: 0 for t in CANONICAL_TYPES}

    # discipline signals (narrative-only)
    if has_excuse_reality_table(narrative):
        scores["discipline-skill"] += 2
    if has_red_green_refactor(narrative):
        scores["discipline-skill"] += 2
    if has_red_flags_list(narrative):
        scores["discipline-skill"] += 1

    # pattern signals (narrative-only)
    if has_recognition_marker(narrative):
        scores["pattern-skill"] += 1
    if has_counter_example(narrative):
        scores["pattern-skill"] += 2

    # technique signals (narrative-only; ignore numbered lines inside code)
    steps = count_ordered_steps(narrative)
    if steps >= 1:
        scores["technique-skill"] += 1
    if steps > 3:
        scores["technique-skill"] += 1
    if has_tickbox_list(narrative):
        scores["technique-skill"] += 1
    if is_user_only(fm):
        scores["technique-skill"] += 3

    # reference signals
    if has_lookup_table(narrative):
        scores["reference-skill"] += 1
    if has_yaml_block(body_text):
        scores["reference-skill"] += 1
    if has_heading(narrative, "Gotcha", "Gotchas", "Known gotchas"):
        scores["reference-skill"] += 1
    if has_heading(narrative, "Example", "Examples"):
        scores["reference-skill"] += 1

    # domain signals
    if has_conditional_loading(narrative):
        scores["domain-skill"] += 2
    if has_companion_declaration(narrative):
        scores["domain-skill"] += 2

    # capability signals
    if re.search(r"\bwraps?\s+(?:the\s+)?(?:\w+\s+){0,3}(?:tool|server|api|service|ide|framework|cli)\b",
                 narrative, re.IGNORECASE):
        scores["capability-skill"] += 2
    if re.search(r"^#{1,6}\s+(?:Capabilit(?:y|ies)|External\s+capability)\b",
                 narrative, re.MULTILINE | re.IGNORECASE):
        scores["capability-skill"] += 1

    return scores
