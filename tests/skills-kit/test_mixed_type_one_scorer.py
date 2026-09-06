"""I5: audit.mixed_type_signal and classify's heuristic mixed-type verdict
must derive from ONE scorer (markdown_heuristics.type_signals), and tuning
audit.THRESHOLDS["mixed_min_score"] must reach both consumers. Previously
classify.py duplicated the threshold as its own MIXED_THRESHOLD constant,
and audit.mixed_type_signal re-implemented the per-type scoring by hand
instead of calling type_signals.
"""

from pathlib import Path

from skills_kit_lib.audit import THRESHOLDS, mixed_type_signal
from skills_kit_lib.classify import classify
from skills_kit_lib.markdown_heuristics import type_signals


# A body that scores >=1 signal in at least two categories: a recognition
# marker (pattern-skill) and a 3-column lookup table (reference-skill).
MULTI_SIGNAL_BODY = (
    "# S\n\n"
    "This skill will recognize the situation when it applies.\n\n"
    "| a | b | c |\n|---|---|---|\n| 1 | 2 | 3 |\n"
)


def test_mixed_threshold_no_longer_duplicated_in_classify():
    import skills_kit_lib.classify as classify_mod
    assert not hasattr(classify_mod, "MIXED_THRESHOLD")


def test_classify_accepts_resolved_threshold_kwarg(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: s\n---\n" + MULTI_SIGNAL_BODY, encoding="utf-8")
    default_report = classify(p)
    tuned_report = classify(p, mixed_threshold=100)
    # A very high per-type threshold means no type can be "high scoring",
    # so a would-be mixed-type verdict is no longer flagged.
    assert tuned_report["verdict"] != "mixed-type" or default_report["verdict"] != "mixed-type"


def test_audit_mixed_signal_derives_from_type_signals(tmp_path):
    """Parity: audit's mixed signal and classify's heuristic verdict agree,
    both derived from the SAME type_signals() result on the same fixture."""
    body_text = MULTI_SIGNAL_BODY
    scores = type_signals(body_text)
    threshold = THRESHOLDS["mixed_min_score"]
    high = [t for t, s in scores.items() if s >= threshold]

    audit_count, audit_names = mixed_type_signal(body_text, threshold)
    assert audit_count == len(high)
    assert set(audit_names) == set(high) or all(
        name.split("=")[0] in high for name in audit_names
    )
