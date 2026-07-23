"""Tests for content_pipeline.cli.unsupported.

Pins the sticky unsupported-stub registry: mark a unit unsupported, filter it
out of a future work list (no re-pay), clear it when the data is fixed,
round-trip the registry, and the record-embedded stub marker (the source's
brief-embedded unsupported block).
"""

from content_pipeline.cli.unsupported import (
    UnsupportedRegistry,
    default_registry,
    is_unsupported,
    is_unsupported_record,
    mark_unsupported,
    record_reason,
    stub_record,
)


# -- registry -----------------------------------------------------------------

def test_mark_and_filter_excludes_from_future_runs():
    reg = UnsupportedRegistry()
    reg.mark("multi_speaker_conv", "multiple non-player speakers")
    assert reg.is_unsupported("multi_speaker_conv")
    # The no-re-pay gate: a marked unit drops out of the work list.
    remaining = reg.filter(["ok1", "multi_speaker_conv", "ok2"])
    assert remaining == ["ok1", "ok2"]


def test_reason_recorded():
    reg = UnsupportedRegistry()
    reg.mark("c", "no NPC speakers")
    assert reg.reason("c") == "no NPC speakers"
    assert reg.reason("absent") is None


def test_clear_lets_unit_reenter():
    reg = UnsupportedRegistry()
    reg.mark("c", "was broken")
    assert reg.clear("c") is True
    assert reg.is_unsupported("c") is False
    assert reg.filter(["c"]) == ["c"]  # re-enters the work list
    assert reg.clear("c") is False  # already gone


def test_mark_is_idempotent_refreshes_reason():
    reg = UnsupportedRegistry()
    reg.mark("c", "first reason")
    reg.mark("c", "clearer reason")
    assert reg.reason("c") == "clearer reason"


def test_registry_round_trips():
    reg = UnsupportedRegistry()
    reg.mark("c1", "r1")
    reg.mark("c2", "r2")
    doc = reg.to_doc()
    restored = UnsupportedRegistry.from_doc(doc)
    assert restored.is_unsupported("c1")
    assert restored.reason("c2") == "r2"


def test_from_doc_none_is_empty():
    reg = UnsupportedRegistry.from_doc(None)
    assert reg.entries == {}


# -- record-embedded stub -----------------------------------------------------

def test_stub_record_carries_marker():
    stub = stub_record("conv_x", "multi-speaker")
    assert is_unsupported_record(stub)
    assert record_reason(stub) == "multi-speaker"
    assert stub["id"] == "conv_x"


def test_stub_record_preserves_carry_fields():
    base = {"skeleton": "Bear", "csv_file": "bears.csv", "junk": "drop"}
    stub = stub_record(
        "conv_x", "reason", base=base, carry_fields=("skeleton", "csv_file")
    )
    assert stub["skeleton"] == "Bear"
    assert stub["csv_file"] == "bears.csv"
    assert "junk" not in stub


def test_is_unsupported_record_false_for_plain_record():
    assert is_unsupported_record({"id": "x"}) is False
    assert is_unsupported_record(None) is False


# -- process-default (skeleton signature) -------------------------------------

def test_process_default_registry():
    default_registry().entries.clear()
    mark_unsupported("proc_conv", "reason")
    assert is_unsupported("proc_conv") is True
    default_registry().entries.clear()
