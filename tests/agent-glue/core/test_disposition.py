from agent_glue_lib.core import Accepted, AcceptedWithAudit, Rejected


def test_accepted_carries_value():
    d = Accepted[int](value=7)
    assert d.kind == "accepted"
    assert d.value == 7


def test_accepted_with_audit_requires_reason():
    d = AcceptedWithAudit[str](value="ok", audit_reason="suspicious-input")
    assert d.kind == "accepted_with_audit"
    assert d.audit_metadata == {}


def test_rejected_carries_reason_and_metadata():
    d = Rejected(reason="schema-violation", metadata={"path": "out.x"})
    assert d.kind == "rejected"
    assert d.metadata == {"path": "out.x"}
