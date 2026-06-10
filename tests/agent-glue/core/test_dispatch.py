import pytest

from agent_glue_lib.core import (
    Accepted,
    AcceptedWithAudit,
    NoHandlerForVariant,
    Rejected,
    dispatch,
)


def test_dispatch_routes_to_matching_handler():
    accepted = Accepted[int](value=5)
    handlers = {
        "accepted": lambda v: ("accept", v.value),
        "rejected": lambda v: ("reject", v.reason),
    }
    assert dispatch(accepted, handlers) == ("accept", 5)


def test_dispatch_handles_rejected():
    rejected = Rejected(reason="bad")
    handlers = {
        "accepted": lambda v: "ok",
        "rejected": lambda v: ("reject", v.reason),
    }
    assert dispatch(rejected, handlers) == ("reject", "bad")


def test_dispatch_handles_accepted_with_audit():
    audited = AcceptedWithAudit[str](value="x", audit_reason="r")
    handlers = {
        "accepted_with_audit": lambda v: ("audit", v.audit_reason),
    }
    assert dispatch(audited, handlers) == ("audit", "r")


def test_dispatch_no_handler_raises_typed_error():
    handlers = {"accepted": lambda v: None}
    with pytest.raises(NoHandlerForVariant) as exc:
        dispatch(Rejected(reason="nope"), handlers)
    assert exc.value.discriminator == "kind"
    assert exc.value.value == "rejected"


def test_dispatch_default_handler_used_on_no_match():
    handlers = {"accepted": lambda v: "ok"}
    out = dispatch(Rejected(reason="x"), handlers, default=lambda v: ("default", v.reason))
    assert out == ("default", "x")


def test_dispatch_works_on_dict_variants():
    handlers = {"foo": lambda v: v["x"]}
    assert dispatch({"kind": "foo", "x": 42}, handlers) == 42


def test_dispatch_custom_discriminator():
    handlers = {"a": lambda v: 1, "b": lambda v: 2}
    assert dispatch({"variant": "b", "val": "y"}, handlers, discriminator="variant") == 2
