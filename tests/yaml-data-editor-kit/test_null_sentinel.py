"""An explicit `null` is PRESENT, not absent, exactly when the field's
`sentinel:` set declares `null` as a member -- FINDING D-3 / L-16.

A field whose sentinel set does not mention `null` keeps today's behaviour:
an explicit `null` there stays ABSENT, same as a missing key.
"""

from pathlib import Path
from typing import Callable

from yaml_data_editor_kit.schema import (
    errors_only,
    load_profile,
    validate_corpus,
)

Writer = Callable[[str, str], Path]

# A minimal catalogue with:
#  - stat.sentinel_field   -- required, sentinel set CONTAINS null (mapping
#    form, non-empty meaning)
#  - stat.plain_field      -- required, no sentinel at all (control group)
#  - stat.optional_field   -- required: false, no sentinel
#  - stat.other_null_field -- required, sentinel set does NOT contain null
#    (a non-null sentinel exists, so this is the "one field away" near twin)
#  - stat.list_sentinel    -- null sentinel written in LIST form
#    (`sentinel: [null]`), which `_parse_sentinel` gives an EMPTY meaning
#    (`{None: ""}`) -- a fixture that can tell true membership apart from
#    "the meaning string is truthy"
#  - stat.bare_sentinel    -- null sentinel written in BARE-SCALAR form
#    (`sentinel: null`), same empty-meaning shape as list form
#  - box.note              -- nested record field with a null sentinel, to
#    prove the ruling reaches _check_shape at any depth, not just top level.
PROFILE = """
dialect: type/1
id: stat
identified_by: id
fields:
  id:               { type: id }
  sentinel_field:   { type: string, sentinel: { null: "not represented yet" } }
  other_null_field: { type: string, sentinel: { unknown: "value withheld" } }
  plain_field:      { type: string }
  optional_field:   { type: string, required: false }
  multi_sentinel:   { type: string, sentinel: { placeholder: "reserved", null: "not represented yet" } }
  list_sentinel:    { type: string, sentinel: [null] }
  bare_sentinel:    { type: string, sentinel: null }
  box:              { type: record, fields: {
                        width: { type: int },
                        note:  { type: string, sentinel: { null: "no note yet" } }
                      } }
---
dialect: source/1
of: stat
layout: rows
path: content/stats.yaml
"""

GOOD_ROW = """
- id: armor
  sentinel_field: null
  other_null_field: withheld
  plain_field: present
  optional_field: present
  multi_sentinel: null
  list_sentinel: null
  bare_sentinel: null
  box: { width: 2, note: null }
"""


def _setup(write: Writer, rows: str) -> None:
    write("profile/catalog.yaml", PROFILE)
    write("content/stats.yaml", rows)


def _errors(tmp_path: Path, profile_dir: Path, write: Writer, rows: str):
    _setup(write, rows)
    profile = load_profile(profile_dir)
    return errors_only(validate_corpus(profile, tmp_path))


def _for_field(errors, field: str):
    return [p for p in errors if p.field == field]


# -- the core ruling ------------------------------------------------------


def test_null_with_null_sentinel_is_present_not_absent(tmp_path, profile_dir, write) -> None:
    """A field whose sentinel set contains null, written as null, is satisfied."""
    errors = _errors(tmp_path, profile_dir, write, GOOD_ROW)
    assert _for_field(errors, "sentinel_field") == []


def test_null_with_null_sentinel_is_exempt_from_the_type_check(tmp_path, profile_dir, write) -> None:
    """The sentinel null must not also be run through the 'string' type check."""
    errors = _errors(tmp_path, profile_dir, write, GOOD_ROW)
    messages = [p.message for p in _for_field(errors, "sentinel_field")]
    assert not any("declared 'string'" in m for m in messages)
    assert not any("is required but absent" in m for m in messages)


def test_a_well_formed_row_produces_no_errors_at_all(tmp_path, profile_dir, write) -> None:
    """Whole-row sweep: nothing else in this fixture should be flagged either."""
    assert _errors(tmp_path, profile_dir, write, GOOD_ROW) == []


def test_null_is_found_even_when_it_is_not_the_first_sentinel_member(
    tmp_path, profile_dir, write
) -> None:
    """multi_sentinel declares a non-null member FIRST and null SECOND -- the
    membership test must scan the whole set, not just its first entry."""
    errors = _errors(tmp_path, profile_dir, write, GOOD_ROW)
    assert _for_field(errors, "multi_sentinel") == []


def test_null_sentinel_written_as_a_list_is_honoured(tmp_path, profile_dir, write) -> None:
    """`sentinel: [null]` parses to {None: ""} -- an EMPTY meaning string.
    This is the fixture that tells true membership (`None in spec.sentinel`)
    apart from a mutant that checks `spec.sentinel.get(None)` for truthiness:
    an empty-string meaning is falsy, so only real membership passes here."""
    errors = _errors(tmp_path, profile_dir, write, GOOD_ROW)
    assert _for_field(errors, "list_sentinel") == []


def test_null_sentinel_written_as_a_bare_scalar_is_honoured(tmp_path, profile_dir, write) -> None:
    """`sentinel: null` also parses to {None: ""} -- same empty-meaning trap
    as the list form, in the third of the three forms `_parse_sentinel`
    accepts (see L-5 in dialect-gaps.md)."""
    errors = _errors(tmp_path, profile_dir, write, GOOD_ROW)
    assert _for_field(errors, "bare_sentinel") == []


def test_a_list_form_null_sentinel_field_missing_its_key_is_still_absent(
    tmp_path, profile_dir, write
) -> None:
    """Near twin for the list-form fixture: the key itself missing (not
    written null) must still report absent, exactly like the mapping form."""
    rows = GOOD_ROW.replace("  list_sentinel: null\n", "")
    errors = _errors(tmp_path, profile_dir, write, rows)
    problems = _for_field(errors, "list_sentinel")
    assert len(problems) == 1
    assert "is required but absent" in problems[0].message


def test_a_bare_form_null_sentinel_field_missing_its_key_is_still_absent(
    tmp_path, profile_dir, write
) -> None:
    """Near twin for the bare-scalar-form fixture: same, key missing entirely."""
    rows = GOOD_ROW.replace("  bare_sentinel: null\n", "")
    errors = _errors(tmp_path, profile_dir, write, rows)
    problems = _for_field(errors, "bare_sentinel")
    assert len(problems) == 1
    assert "is required but absent" in problems[0].message


# -- the near twins: everything one field away ----------------------------


def test_null_without_any_sentinel_is_still_absent(tmp_path, profile_dir, write) -> None:
    """Control: plain_field has no sentinel at all; null there stays absent."""
    rows = GOOD_ROW.replace("plain_field: present", "plain_field: null")
    errors = _errors(tmp_path, profile_dir, write, rows)
    problems = _for_field(errors, "plain_field")
    assert len(problems) == 1
    assert "is required but absent" in problems[0].message


def test_null_with_a_non_null_sentinel_is_still_absent(tmp_path, profile_dir, write) -> None:
    """Near twin: a sentinel set exists but does NOT contain null -- null there
    is still absent, not silently accepted because *some* sentinel exists."""
    rows = GOOD_ROW.replace("other_null_field: withheld", "other_null_field: null")
    errors = _errors(tmp_path, profile_dir, write, rows)
    problems = _for_field(errors, "other_null_field")
    assert len(problems) == 1
    assert "is required but absent" in problems[0].message


def test_a_genuinely_missing_key_is_still_absent_even_with_a_null_sentinel(
    tmp_path, profile_dir, write
) -> None:
    """Near twin: the KEY ITSELF is missing (not written as null). A sentinel
    that includes null must not paper over a genuinely absent key."""
    rows = GOOD_ROW.replace("  sentinel_field: null\n", "")
    errors = _errors(tmp_path, profile_dir, write, rows)
    problems = _for_field(errors, "sentinel_field")
    assert len(problems) == 1
    assert "is required but absent" in problems[0].message


def test_a_null_sentinel_field_that_holds_a_real_value_is_checked_normally(
    tmp_path, profile_dir, write
) -> None:
    """Near twin: sentinel_field holding an actual string is not exempt from
    anything -- it goes through the ordinary type check like any other value."""
    rows = GOOD_ROW.replace("sentinel_field: null", "sentinel_field: 4")
    errors = _errors(tmp_path, profile_dir, write, rows)
    problems = _for_field(errors, "sentinel_field")
    assert len(problems) == 1
    assert "declared 'string'" in problems[0].message


def test_an_optional_field_with_no_sentinel_written_null_is_not_reported(
    tmp_path, profile_dir, write
) -> None:
    """Near twin: required: false with no sentinel at all -- null there was
    already fine before this ruling, and must stay fine."""
    rows = GOOD_ROW.replace("optional_field: present", "optional_field: null")
    errors = _errors(tmp_path, profile_dir, write, rows)
    assert _for_field(errors, "optional_field") == []


def test_a_nested_record_field_honours_the_same_null_sentinel_ruling(
    tmp_path, profile_dir, write
) -> None:
    """The ruling applies wherever _check_shape runs, including nested records,
    addressed by its dotted path exactly like any other nested field."""
    errors = _errors(tmp_path, profile_dir, write, GOOD_ROW)
    assert _for_field(errors, "box.note") == []


def test_a_nested_record_field_without_the_key_at_all_is_still_absent(
    tmp_path, profile_dir, write
) -> None:
    """Near twin at depth: box.note missing entirely (not written null) inside
    a nested record must still report absent."""
    rows = GOOD_ROW.replace("box: { width: 2, note: null }", "box: { width: 2 }")
    errors = _errors(tmp_path, profile_dir, write, rows)
    problems = _for_field(errors, "box.note")
    assert len(problems) == 1
    assert "is required but absent" in problems[0].message


# -- variant case fields ---------------------------------------------------

# A `variants:` field is added by `fields_for(discriminator)` and then goes
# through the SAME `_check_shape` as any other field -- pin that path
# directly rather than arguing it from the code.
VARIANTS_PROFILE = """
dialect: type/1
id: widget
identified_by: id
fields:
  id:       { type: id }
  category: { type: enum, values: [alpha, beta] }
variants:
  on: category
  when:
    alpha:
      alpha_note: { type: string, sentinel: { null: "not represented in alpha yet" } }
---
dialect: source/1
of: widget
layout: rows
path: content/widgets.yaml
"""


def _variant_errors(tmp_path: Path, profile_dir: Path, write: Writer, rows: str):
    write("profile/catalog.yaml", VARIANTS_PROFILE)
    write("content/widgets.yaml", rows)
    profile = load_profile(profile_dir)
    return errors_only(validate_corpus(profile, tmp_path))


def test_a_null_sentinel_field_added_by_a_variant_case_is_present_not_absent(
    tmp_path, profile_dir, write
) -> None:
    """alpha_note only exists on the 'alpha' variant case, added via `when:`.
    Written null there, with a sentinel set containing null, it must be
    treated as present -- proving the ruling reaches variant-added fields,
    not just a type's own top-level `fields:`."""
    rows = "\n- id: one\n  category: alpha\n  alpha_note: null\n"
    errors = _variant_errors(tmp_path, profile_dir, write, rows)
    assert [p for p in errors if p.field == "alpha_note"] == []


def test_a_null_sentinel_field_added_by_a_variant_case_still_reports_true_absence(
    tmp_path, profile_dir, write
) -> None:
    """Near twin: the 'alpha' record omits alpha_note's key entirely (not
    written null) -- must still report absent, same as the top-level case."""
    rows = "\n- id: one\n  category: alpha\n"
    errors = _variant_errors(tmp_path, profile_dir, write, rows)
    problems = [p for p in errors if p.field == "alpha_note"]
    assert len(problems) == 1
    assert "is required but absent" in problems[0].message


# -- pinning the finding-set SHAPE across multiple records ----------------

# A single-record fixture cannot detect a `[:1]` truncation of the record
# loop, nor of the returned findings list -- either mutation would still
# pass every test above, because every positive case above expects EXACTLY
# ONE (or zero) findings from ONE record. This fixture mirrors the real
# corpus shape the ruling exists for: the SAME field null on one record,
# a real value on another, and genuinely missing on a third, in one file.
MIXED_ROWS = """
- id: armor
  sentinel_field: null
  other_null_field: withheld
  plain_field: present
  optional_field: present
  multi_sentinel: null
  list_sentinel: null
  bare_sentinel: null
  box: { width: 2, note: null }
- id: bravo
  other_null_field: withheld
  plain_field: present
  optional_field: present
  multi_sentinel: null
  list_sentinel: null
  bare_sentinel: null
  box: { width: 2, note: null }
- id: charlie
  sentinel_field: a real value
  other_null_field: withheld
  plain_field: present
  optional_field: present
  multi_sentinel: null
  list_sentinel: null
  bare_sentinel: null
  box: { width: 2, note: null }
"""


def test_the_same_field_mixed_null_real_and_missing_across_three_records(
    tmp_path, profile_dir, write
) -> None:
    """armor: null + null-sentinel -> present, no finding.
    bravo:  key missing entirely -> exactly one 'required but absent' finding.
    charlie: a real value -> no finding, checked as an ordinary string.
    A record-loop or findings-list truncation to the first entry collapses
    this to zero findings; pin the full, correctly-identified shape."""
    errors = _errors(tmp_path, profile_dir, write, MIXED_ROWS)
    problems = _for_field(errors, "sentinel_field")
    assert len(problems) == 1, problems
    assert problems[0].record == "bravo"
    assert "is required but absent" in problems[0].message
    # armor and charlie must NOT appear at all.
    assert {p.record for p in problems} == {"bravo"}


def test_a_second_missing_record_is_not_swallowed_by_the_first_finding(
    tmp_path, profile_dir, write
) -> None:
    """A second record with the SAME field genuinely missing must produce
    its OWN finding, identified by its OWN record label -- not merged into,
    or hidden behind, the first record's finding. This is what a `[:1]`
    truncation of the record loop or the findings list actually collapses."""
    rows = MIXED_ROWS.replace(
        "- id: charlie\n  sentinel_field: a real value\n",
        "- id: charlie\n",
    )
    errors = _errors(tmp_path, profile_dir, write, rows)
    problems = _for_field(errors, "sentinel_field")
    assert len(problems) == 2, problems
    assert {p.record for p in problems} == {"bravo", "charlie"}
    assert all("is required but absent" in p.message for p in problems)
