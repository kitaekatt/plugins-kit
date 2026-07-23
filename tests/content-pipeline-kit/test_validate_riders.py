"""Tests for content_pipeline.validate.riders.

Port-equivalence baseline: these cases translate the deterministic
fact-rider behaviors pinned by localization ``qa_riders`` into the plugin's
neutral vocabulary -- riders MAPPED from a single validation run's rejection
kinds (never re-deriving the check), pure fact-function riders (a width),
and attachment/caching on the candidate for downstream reuse. No loc concepts
appear: rejections carry a neutral ``kind``; the width function is a stand-in
for any deterministic metric.
"""

from content_pipeline.store.candidate import Candidate
from content_pipeline.validate.contract import Rejection, Severity
from content_pipeline.validate.riders import (
    attach_riders,
    cached_riders,
    compute_riders,
    facts_from_rejections,
    rider_from_kind,
)


# -- riders mapped from validator output --------------------------------------

def test_rider_from_kind_ok_when_absent():
    assert rider_from_kind([], "markup") == {"ok": True, "detail": ""}


def test_rider_from_kind_reports_first_match():
    rejections = [Rejection(kind="markup", detail="dropped {x}")]
    assert rider_from_kind(rejections, "markup") == {"ok": False, "detail": "dropped {x}"}


def test_facts_from_rejections_one_per_kind():
    rejections = [
        Rejection(kind="markup", detail="dropped"),
        Rejection(kind="english_leak", detail="leak of 'foo'"),
    ]
    facts = facts_from_rejections(rejections, ["markup", "english_leak", "missing"])
    assert facts["markup"]["ok"] is False
    assert facts["english_leak"]["ok"] is False
    assert facts["missing"]["ok"] is True  # absent kind -> ok


def test_riders_do_not_fork_the_check():
    # The rider reads the verdict the validator produced; it never re-derives.
    # Same rejection list -> same rider block, deterministically.
    rejections = [Rejection(kind="rule", severity=Severity.SOFT, detail="R1")]
    a = facts_from_rejections(rejections, ["rule"])
    b = facts_from_rejections(rejections, ["rule"])
    assert a == b


# -- pure fact-function riders ------------------------------------------------

def _width(value, context):
    # A stand-in for any deterministic metric (e.g. CJK-aware display width).
    return sum(2 if ord(c) > 0x2E80 else 1 for c in value)


def test_compute_riders_runs_pure_functions():
    riders = compute_riders("abc", None, {"width": _width})
    assert riders == {"width": 3}


# -- attach / cache -----------------------------------------------------------

def test_attach_riders_to_dict_candidate():
    candidate = {"id": "c0", "value": "x"}
    out = attach_riders(candidate, {"width": {"ok": True}})
    assert out["riders"] == {"width": {"ok": True}}
    assert candidate.get("riders") is None  # non-mutating


def test_attach_riders_merges_existing():
    candidate = {"id": "c0", "riders": {"a": 1}}
    out = attach_riders(candidate, {"b": 2})
    assert out["riders"] == {"a": 1, "b": 2}


def test_attach_riders_to_dataclass_candidate():
    candidate = Candidate(id="c0", value="x")
    out = attach_riders(candidate, {"width": {"ok": True}})
    assert out.riders == {"width": {"ok": True}}
    assert candidate.riders is None  # non-mutating (frozen replace)


def test_cached_riders_reads_back():
    candidate = attach_riders({"id": "c0"}, {"w": 5})
    assert cached_riders(candidate) == {"w": 5}
    assert cached_riders({"id": "c1"}) == {}
