"""Tests for content_pipeline.audit.auditor.

Pins the generalized six-kind FN/FP taxonomy, driven entirely by injected
runtime classifiers
(the SAME policy / marker / store-projection callables the runtime uses -- so a
finding is by construction the runtime's own verdict). Neutral vocabulary: an
entity is an opaque record read through the AuditSpec callables.
"""

from content_pipeline.audit.auditor import (
    POLICY_APPLY,
    POLICY_EXCLUDE,
    POLICY_UNKNOWN,
    AuditSpec,
    Finding,
    FindingKind,
    audit_corpus,
    audit_entity,
    audit_references,
    counts_by_kind,
)


# An entity record: policy verdict, whether a marked machine output exists,
# whether the store has a record + value, and the delivered output value.
def _entity(policy, marked=False, has_record=False, store_value=None, output_value=None):
    return {
        "policy": policy,
        "marked": marked,
        "has_record": has_record,
        "store_value": store_value,
        "output_value": output_value,
    }


SPEC = AuditSpec(
    policy=lambda e: e["policy"],
    output_marked=lambda e: e["marked"],
    store_has_record=lambda e: e["has_record"],
    store_value=lambda e: e["store_value"],
    output_value=lambda e: e["output_value"],
)


def _one(entity):
    findings = audit_entity(entity, "eid", SPEC)
    return findings[0] if findings else None


# -- taxonomy -----------------------------------------------------------------

def test_false_negative_store_has_value_no_output():
    # policy=apply, store has a value, but no machine output was delivered.
    f = _one(_entity(POLICY_APPLY, marked=False, has_record=True, store_value="pick"))
    assert f.kind is FindingKind.FALSE_NEGATIVE


def test_missing_value_no_pick_yet():
    # policy=apply, no output, store record present but carries no value.
    f = _one(_entity(POLICY_APPLY, marked=False, has_record=True, store_value=""))
    assert f.kind is FindingKind.MISSING_VALUE
    assert "no value" in f.detail


def test_missing_value_no_record():
    f = _one(_entity(POLICY_APPLY, marked=False, has_record=False, store_value=None))
    assert f.kind is FindingKind.MISSING_VALUE
    assert "no store record" in f.detail


def test_false_positive_output_on_excluded():
    # policy=exclude, yet a machine-marked output exists.
    f = _one(_entity(POLICY_EXCLUDE, marked=True))
    assert f.kind is FindingKind.FALSE_POSITIVE


def test_orphaned_output_no_backing_record():
    f = _one(_entity(POLICY_APPLY, marked=True, has_record=False))
    assert f.kind is FindingKind.ORPHANED_OUTPUT


def test_store_output_mismatch():
    f = _one(
        _entity(
            POLICY_APPLY,
            marked=True,
            has_record=True,
            store_value="new",
            output_value="stale",
        )
    )
    assert f.kind is FindingKind.STORE_OUTPUT_MISMATCH
    assert f.context == {"output": "stale", "store": "new"}


# -- clean cases (no finding) -------------------------------------------------

def test_marked_output_matching_store_is_clean():
    assert _one(
        _entity(POLICY_APPLY, marked=True, has_record=True, store_value="v", output_value="v")
    ) is None


def test_excluded_with_no_output_is_clean():
    assert _one(_entity(POLICY_EXCLUDE, marked=False)) is None


def test_unknown_policy_has_no_expectation():
    assert _one(_entity(POLICY_UNKNOWN, marked=True)) is None
    assert _one(_entity(POLICY_UNKNOWN, marked=False)) is None


# -- corpus + references ------------------------------------------------------

def test_audit_corpus_concatenates():
    entities = [
        _entity(POLICY_EXCLUDE, marked=True),  # FP
        _entity(POLICY_APPLY, marked=False, has_record=True, store_value="p"),  # FN
    ]
    findings = audit_corpus(entities, SPEC, entity_id=lambda e: e["policy"])
    counts = counts_by_kind(findings)
    assert counts[FindingKind.FALSE_POSITIVE] == 1
    assert counts[FindingKind.FALSE_NEGATIVE] == 1


def test_audit_references_flags_unresolved():
    refs = [{"id": "r1", "ok": True}, {"id": "r2", "ok": False}]
    findings = audit_references(
        refs, ref_id=lambda r: r["id"], resolves=lambda r: r["ok"]
    )
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.STALE_REF
    assert findings[0].entity_id == "r2"


def test_counts_by_kind_has_every_kind():
    counts = counts_by_kind([])
    assert set(counts) == set(FindingKind)
    assert all(v == 0 for v in counts.values())
