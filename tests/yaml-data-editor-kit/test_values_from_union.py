'''``values_from:`` as a LIST of paths: the legal set is the UNION of what
they resolve to (dialect gap D-1).

A single string still RESOLVES bit-for-bit as it always has -- that premise
is exercised throughout ``test_fields.py``, ``test_nested_paths.py`` and
``test_address_fields.py``, which this module does not repeat (and pinned
here too, in ``test_a_one_element_list_behaves_exactly_like_the_scalar_form``).
One thing does change for the scalar form as of 0.10.0: a legal-set LISTING
in a message is now capped at 12 members everywhere, including a single-path
``values_from:`` whose set exceeds that -- previously uncapped at exactly one
of the three sites that render one. That is pinned in
``test_a_wide_single_path_legal_set_is_truncated_in_the_message`` below, not
silently assumed unchanged.

This module is about the LIST form: multiple member paths, duplicates across
them collapsing to one legal value, an empty member set being fine while an
empty path LIST is not, a repeated path being refused, a cycle through one
member of a list still being named correctly (and the OTHER member not being
blamed for it), the corpus-load-time map-key check (not just the validator's
per-record check) seeing every member, the per-path-tuple value-set cache not
colliding two different unions that happen to share a first member, and a
validator rejection actually naming the union's member paths rather than the
note silently going missing.
'''

from pathlib import Path
from typing import Callable

import pytest

from yaml_data_editor_kit.comments import EvaluationError, Point, evaluate, parse_selector
from yaml_data_editor_kit.schema import (
    ADVISORY,
    Corpus,
    Diagnostic,
    ProfileError,
    Profile,
    errors_only,
    load_corpus,
    load_profile,
    validate_corpus,
)

Writer = Callable[[str, str], Path]

# `ingredient.id`, `batch.extra` and `tag.id` are three member paths with
# several members each, so a union/first/last/intersect mutant all disagree
# with the correct answer on some value -- a two-member, two-path fixture
# cannot tell those apart (see the module docstring in the report).
UNION_PROFILE = '''
dialect: type/1
id: ingredient
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: ingredient
layout: rows
path: content/ingredients.yaml
---
dialect: type/1
id: batch
identified_by: id
fields:
  id: { type: id }
  extra:
    type: list
    of: { type: string }
---
dialect: source/1
of: batch
layout: rows
path: content/batches.yaml
---
dialect: type/1
id: tag
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: tag
layout: rows
path: content/tags.yaml
---
dialect: type/1
id: dish
identified_by: id
fields:
  id: { type: id }
  ratios:
    type: map
    key:
      type: enum
      values_from: [ingredient.id, batch.extra, tag.id]
    value: { type: float }
---
dialect: source/1
of: dish
layout: rows
path: content/dishes.yaml
'''


def _write_union_corpus(write: Writer, dish_rows: str) -> None:
    write('profile/types.yaml', UNION_PROFILE)
    # ingredient.id: salt, sugar, flour
    write('content/ingredients.yaml', '- { id: salt }\n- { id: sugar }\n- { id: flour }\n')
    # batch.extra: pepper (sweet-only), paprika + salt (savory) -- salt is
    # also an ingredient id, so it is the "in both" near-twin.
    write(
        'content/batches.yaml',
        '- { id: sweet, extra: [pepper] }\n'
        '- { id: savory, extra: [paprika, salt] }\n',
    )
    # tag.id: vegan, gluten_free
    write('content/tags.yaml', '- { id: vegan }\n- { id: gluten_free }\n')
    write('content/dishes.yaml', dish_rows)


def _load(tmp_path: Path, profile_dir: Path, write: Writer, dish_rows: str) -> Profile:
    _write_union_corpus(write, dish_rows)
    return load_profile(profile_dir)


def _corpus(tmp_path: Path, profile: Profile) -> Corpus:
    return load_corpus(profile, tmp_path)


# --------------------------------------------------------------------------
# the union itself: near-twin coverage (first-only, second-only, both, and
# neither), across three member paths.
# --------------------------------------------------------------------------


def test_a_value_named_by_only_the_first_path_is_legal(tmp_path, profile_dir, write) -> None:
    profile = _load(tmp_path, profile_dir, write, '- { id: d1, ratios: { sugar: 0.5 } }\n')
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_a_value_named_by_only_the_second_path_is_legal(tmp_path, profile_dir, write) -> None:
    profile = _load(tmp_path, profile_dir, write, '- { id: d1, ratios: { pepper: 0.5 } }\n')
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_a_value_named_by_only_the_third_path_is_legal(tmp_path, profile_dir, write) -> None:
    profile = _load(tmp_path, profile_dir, write, '- { id: d1, ratios: { vegan: 0.5 } }\n')
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_a_value_named_by_two_paths_is_legal_and_reported_once(
    tmp_path, profile_dir, write
) -> None:
    # 'salt' is both an ingredient id and a batch.extra member.
    profile = _load(tmp_path, profile_dir, write, '- { id: d1, ratios: { salt: 0.5 } }\n')
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_a_value_named_by_no_path_is_rejected(tmp_path, profile_dir, write) -> None:
    profile = _load(
        tmp_path, profile_dir, write, '- { id: d1, ratios: { charcoal: 0.5 } }\n'
    )
    problems = [
        p for p in errors_only(validate_corpus(profile, tmp_path)) if p.field == 'ratios.charcoal'
    ]
    assert len(problems) == 1
    message = problems[0].message
    assert 'not one of the declared values' in message
    # the message names WHICH paths the set came from -- this is
    # validate.py's own rejection (reached via _check_key -> _check_enum,
    # not corpus.py's check_path_key_steps), so it separately pins that
    # validate._union_note is not silently dropped.
    assert 'union of' in message
    assert "'ingredient.id'" in message
    assert "'batch.extra'" in message
    assert "'tag.id'" in message
    # every member value is listed, from every path -- proof this is a real
    # union and not just the first or last path's set.
    for member in ('salt', 'sugar', 'flour', 'pepper', 'paprika', 'vegan', 'gluten_free'):
        assert member in message


def test_all_three_named_paths_are_legal_together(tmp_path, profile_dir, write) -> None:
    rows = (
        '- id: d1\n'
        '  ratios: { salt: 0.2, sugar: 0.2, pepper: 0.2, vegan: 0.2, flour: 0.2 }\n'
    )
    profile = _load(tmp_path, profile_dir, write, rows)
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_an_empty_member_path_contributes_nothing_but_is_not_an_error(
    tmp_path, profile_dir, write
) -> None:
    # 'tag' has a source but zero records -- an EMPTY member path, distinct
    # from an empty PATH LIST (a load error, tested below). The other two
    # members still union normally; a value only 'tag.id' would have
    # supplied is correctly rejected, proving the empty member contributed
    # nothing rather than the whole union silently degrading to "anything
    # goes".
    write('profile/types.yaml', UNION_PROFILE)
    write('content/ingredients.yaml', '- { id: salt }\n- { id: sugar }\n- { id: flour }\n')
    write(
        'content/batches.yaml',
        '- { id: sweet, extra: [pepper] }\n- { id: savory, extra: [paprika, salt] }\n',
    )
    write('content/tags.yaml', '[]\n')  # tag.id resolves to the empty set
    write('content/dishes.yaml', '- { id: d1, ratios: { salt: 0.2, pepper: 0.2 } }\n')
    profile = load_profile(profile_dir)
    assert errors_only(validate_corpus(profile, tmp_path)) == []

    # 'vegan' would only ever come from the now-empty tag.id member.
    write('content/dishes.yaml', '- { id: d1, ratios: { vegan: 0.2 } }\n')
    profile = load_profile(profile_dir)
    problems = [
        p for p in errors_only(validate_corpus(profile, tmp_path)) if p.field == 'ratios.vegan'
    ]
    assert len(problems) == 1
    assert 'not one of the declared values' in problems[0].message


# --------------------------------------------------------------------------
# scalar-compatibility edges: one-element list is identical to a bare string.
# --------------------------------------------------------------------------


def test_a_one_element_list_behaves_exactly_like_the_scalar_form(
    tmp_path, profile_dir, write
) -> None:
    scalar_profile = UNION_PROFILE.replace(
        'values_from: [ingredient.id, batch.extra, tag.id]',
        'values_from: ingredient.id',
    )
    list_profile = UNION_PROFILE.replace(
        'values_from: [ingredient.id, batch.extra, tag.id]',
        'values_from: [ingredient.id]',
    )
    dish_rows = '- { id: d1, ratios: { sugar: 0.5 } }\n'
    bad_rows = '- { id: d1, ratios: { charcoal: 0.5 } }\n'

    def _run(text: str, rows: str) -> list[Diagnostic]:
        write('profile/types.yaml', text)
        write('content/ingredients.yaml', '- { id: salt }\n- { id: sugar }\n- { id: flour }\n')
        write('content/batches.yaml', '- { id: sweet, extra: [pepper] }\n')
        write('content/tags.yaml', '- { id: vegan }\n')
        write('content/dishes.yaml', rows)
        profile = load_profile(profile_dir)
        return validate_corpus(profile, tmp_path)

    good_scalar = [d.message for d in errors_only(_run(scalar_profile, dish_rows))]
    good_list = [d.message for d in errors_only(_run(list_profile, dish_rows))]
    assert good_scalar == good_list == []

    bad_scalar = [d.message for d in errors_only(_run(scalar_profile, bad_rows))]
    bad_list = [d.message for d in errors_only(_run(list_profile, bad_rows))]
    assert bad_scalar == bad_list
    assert len(bad_scalar) == 1
    assert 'not one of the declared values' in bad_scalar[0]
    # the scalar form's message never names a union -- one path is not one.
    assert 'union of' not in bad_scalar[0]


# --------------------------------------------------------------------------
# the >12-value cap on a legal-set LISTING now applies at every site that
# renders one -- including corpus.py's check_path_key_steps, which was the
# one uncapped site before this fix (see the module docstring and the
# amended dialect.md prose). This is a SCALAR (single-path) values_from --
# the cap is not a union-only behaviour.
# --------------------------------------------------------------------------

WIDE_SCALAR_PROFILE = '''
dialect: type/1
id: wide
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: wide
layout: rows
path: content/wide.yaml
---
dialect: type/1
id: box
identified_by: id
fields:
  id: { type: id }
  slots:
    type: map
    key: { type: enum, values_from: wide.id }
    value: { type: float }
---
dialect: source/1
of: box
layout: rows
path: content/boxes.yaml
---
dialect: view/1
id: box_table
of: box
form: table
fields:
  - { field: slots.nonexistent_key }
'''


def test_a_wide_single_path_legal_set_is_truncated_in_the_message(
    tmp_path, profile_dir, write
) -> None:
    write('profile/wide.yaml', WIDE_SCALAR_PROFILE)
    wide_ids = ['w{0}'.format(i) for i in range(1, 16)]  # 15 members, > the cap of 12
    write('content/wide.yaml', '\n'.join('- {{ id: {0} }}'.format(i) for i in wide_ids) + '\n')
    write('content/boxes.yaml', '- { id: b1, slots: {} }\n')
    profile = load_profile(profile_dir)
    corpus = load_corpus(profile, tmp_path)

    problems = [d for d in corpus.diagnostics if d.field == 'slots.nonexistent_key']
    assert len(problems) == 1
    message = problems[0].message
    assert '...' in message
    for member in wide_ids[:12]:
        assert member in message
    for member in wide_ids[12:]:
        assert member not in message
    # the scalar form never names a union.
    assert 'union of' not in message


# --------------------------------------------------------------------------
# load-time refusals: empty list, repeated path.
# --------------------------------------------------------------------------


def test_an_empty_values_from_list_is_a_load_error(tmp_path, profile_dir, write) -> None:
    write(
        'profile/types.yaml',
        UNION_PROFILE.replace('values_from: [ingredient.id, batch.extra, tag.id]', 'values_from: []'),
    )
    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)
    assert 'names no paths' in str(caught.value)


def test_a_repeated_path_in_a_values_from_list_is_a_load_error(
    tmp_path, profile_dir, write
) -> None:
    write(
        'profile/types.yaml',
        UNION_PROFILE.replace(
            'values_from: [ingredient.id, batch.extra, tag.id]',
            'values_from: [ingredient.id, ingredient.id]',
        ),
    )
    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)
    message = str(caught.value)
    assert 'lists path' in message
    assert "'ingredient.id'" in message
    assert 'more than once' in message


def test_record_keys_from_refuses_a_list_with_a_clear_message(
    tmp_path, profile_dir, write
) -> None:
    # record_keys_from: is deliberately NOT extended to the list/union form
    # (see docs/dialect.md and docs/dialect-gaps.md D-1). Now that authors
    # have seen '[a.id, b.id]' syntax taught for values_from:, a list here
    # must be refused with a message naming the mistake -- not fall through
    # to str([...]) and produce an unresolvable path built from Python's own
    # list repr.
    write(
        'profile/types.yaml',
        '''
dialect: type/1
id: ingredient
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: ingredient
layout: rows
path: content/ingredients.yaml
---
dialect: type/1
id: tag
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: tag
layout: rows
path: content/tags.yaml
---
dialect: type/1
id: dish
fields:
  name: { type: string }
---
dialect: source/1
of: dish
layout: keyed_map
path: content/dishes.yaml
record_keys_from: [ingredient.id, tag.id]
''',
    )
    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)
    message = str(caught.value)
    assert 'takes a single path, not a list' in message
    assert 'values_from' in message
    # the OLD failure mode this guards against: str([...]) leaking into the
    # path-shape error instead of a message naming the actual mistake.
    assert '[' not in message


# --------------------------------------------------------------------------
# corpus.py's own check_path_key_steps (a VIEW naming a map key statically,
# checked once per declared path use -- not validate.py's per-record check)
# must see every member, not just the first. This is the site
# yaml-data-editor-kit fixed in corpus.resolve_value_set; it has its own
# call site in check_path_key_steps, so it needs its own pin.
# --------------------------------------------------------------------------

UNION_VIEW_PROFILE = UNION_PROFILE + '''
---
dialect: view/1
id: dish_table
of: dish
form: table
fields:
  - { field: ratios.pepper }
'''


def test_check_path_key_steps_sees_a_non_first_member_key(
    tmp_path, profile_dir, write
) -> None:
    # 'pepper' is a batch.extra member ONLY -- absent from ingredient.id,
    # the FIRST path in the list. A resolver that only consulted the first
    # member would wrongly refuse it here.
    write('profile/types.yaml', UNION_VIEW_PROFILE)
    write('content/ingredients.yaml', '- { id: salt }\n- { id: sugar }\n- { id: flour }\n')
    write('content/batches.yaml', '- { id: sweet, extra: [pepper] }\n')
    write('content/tags.yaml', '- { id: vegan }\n')
    write('content/dishes.yaml', '- { id: d1, ratios: { pepper: 0.5 } }\n')
    profile = load_profile(profile_dir)
    corpus = load_corpus(profile, tmp_path)

    problems = [d for d in corpus.diagnostics if 'pepper' in d.message]
    assert problems == []


# --------------------------------------------------------------------------
# the value-set cache (validate.Validator._value_sets) is keyed by the FULL
# values_from tuple -- two distinct unions that happen to share a first
# member path must not collide and share each other's legal set.
# --------------------------------------------------------------------------

TWO_UNIONS_SHARING_A_FIRST_PATH = '''
dialect: type/1
id: ingredient
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: ingredient
layout: rows
path: content/ingredients.yaml
---
dialect: type/1
id: batch
identified_by: id
fields:
  id: { type: id }
  extra: { type: list, of: { type: string } }
---
dialect: source/1
of: batch
layout: rows
path: content/batches.yaml
---
dialect: type/1
id: tag
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: tag
layout: rows
path: content/tags.yaml
---
dialect: type/1
id: dish
identified_by: id
fields:
  id: { type: id }
  savoury_pick: { type: enum, values_from: [ingredient.id, batch.extra] }
  tagged_pick:  { type: enum, values_from: [ingredient.id, tag.id] }
---
dialect: source/1
of: dish
layout: rows
path: content/dishes.yaml
'''


def test_two_unions_sharing_a_first_path_do_not_collide_in_the_cache(
    tmp_path, profile_dir, write
) -> None:
    write('profile/types.yaml', TWO_UNIONS_SHARING_A_FIRST_PATH)
    write('content/ingredients.yaml', '- { id: salt }\n')
    write('content/batches.yaml', '- { id: sweet, extra: [pepper] }\n')
    write('content/tags.yaml', '- { id: vegan }\n')
    # 'pepper' is legal for savoury_pick (via batch.extra) but NOT for
    # tagged_pick (whose second member is tag.id, not batch.extra). A cache
    # keyed on the shared first element 'ingredient.id' would wrongly reuse
    # one field's resolved set for the other and let this through, or wrongly
    # reject 'vegan' below.
    write(
        'content/dishes.yaml',
        '- { id: d1, savoury_pick: pepper, tagged_pick: vegan }\n',
    )
    profile = load_profile(profile_dir)
    assert errors_only(validate_corpus(profile, tmp_path)) == []

    write(
        'content/dishes.yaml',
        '- { id: d1, savoury_pick: pepper, tagged_pick: pepper }\n',
    )
    profile = load_profile(profile_dir)
    problems = [
        p for p in errors_only(validate_corpus(profile, tmp_path)) if p.field == 'tagged_pick'
    ]
    assert len(problems) == 1
    assert 'not one of the declared values' in problems[0].message


# --------------------------------------------------------------------------
# cycle detection: one member of a list cycles, the other does not -- the
# report must name the CYCLING member, not an arbitrary one.
# --------------------------------------------------------------------------

CYCLIC_LIST_PROFILE = '''
dialect: type/1
id: t
identified_by: id
fields:
  id: { type: id }
  safe:
    type: list
    of: { type: string }
  m:
    type: map
    key: { type: enum, values_from: [t.safe, t.m.a] }
    value: { type: string }
---
dialect: source/1
of: t
layout: rows
path: content/t.yaml
---
dialect: view/1
id: v
of: t
form: table
fields:
  - { field: m.a }
'''


def test_a_cycle_through_one_list_member_names_that_member(
    tmp_path, profile_dir, write
) -> None:
    write('profile/t.yaml', CYCLIC_LIST_PROFILE)
    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)
    message = str(caught.value)
    assert "cyclic 'values_from:'" in message
    # the message template is `resolving path '{path}' steps through map
    # '{map}'` -- assert against that RENDERED text directly (not a
    # brittle substring split) so a mutant that reports an arbitrary
    # member's walk instead of the one that actually cycles is caught: it
    # would flip which of these two holds.
    assert "resolving path 't.m.a'" in message
    assert "resolving path 't.safe'" not in message
    assert "map 't.m'" in message


# --------------------------------------------------------------------------
# address.py: the same union governs comment-address map-key evaluation.
# --------------------------------------------------------------------------


def _addressing_corpus(tmp_path: Path, profile_dir: Path, write: Writer) -> tuple[Profile, Corpus]:
    profile = _load(
        tmp_path,
        profile_dir,
        write,
        '- { id: d1, ratios: { salt: 0.2, pepper: 0.3, vegan: 0.5 } }\n',
    )
    return profile, _corpus(tmp_path, profile)


def test_address_accepts_a_key_from_any_member_path(tmp_path, profile_dir, write) -> None:
    profile, corpus = _addressing_corpus(tmp_path, profile_dir, write)
    for key in ('salt', 'pepper', 'vegan'):
        result = evaluate(parse_selector('dish/d1/ratios.' + key), profile, corpus)
        assert result.points == frozenset({Point('dish', 'd1', ('ratios', key))})


def test_address_rejects_a_key_from_no_member_path(tmp_path, profile_dir, write) -> None:
    profile, corpus = _addressing_corpus(tmp_path, profile_dir, write)
    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector('dish/d1/ratios.charcoal'), profile, corpus)
    assert 'declared' in str(exc_info.value)


def test_address_rejects_a_predicate_value_outside_every_member_path(
    tmp_path, profile_dir, write
) -> None:
    # A predicate over an enum whose values_from is a union goes through the
    # same '_check_declared_membership' path as a map key -- pin it too.
    predicate_profile = UNION_PROFILE.replace(
        'dialect: type/1\nid: dish',
        'dialect: type/1\nid: dish',
    ) + '''
---
dialect: type/1
id: pick
identified_by: id
fields:
  id: { type: id }
  choice: { type: enum, values_from: [ingredient.id, batch.extra, tag.id] }
---
dialect: source/1
of: pick
layout: rows
path: content/picks.yaml
'''
    write('profile/types.yaml', predicate_profile)
    write('content/ingredients.yaml', '- { id: salt }\n- { id: sugar }\n- { id: flour }\n')
    write('content/batches.yaml', '- { id: sweet, extra: [pepper] }\n')
    write('content/tags.yaml', '- { id: vegan }\n')
    write('content/dishes.yaml', '- { id: d1, ratios: { salt: 0.5 } }\n')
    write('content/picks.yaml', '- { id: p1, choice: pepper }\n- { id: p2, choice: vegan }\n')
    profile = load_profile(profile_dir)
    corpus = load_corpus(profile, tmp_path)

    matched = evaluate(parse_selector("pick/[choice=pepper]"), profile, corpus)
    assert matched.matched_records == 1

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector('pick/[choice=charcoal]'), profile, corpus)
    assert 'outside the declared' in str(exc_info.value)


# --------------------------------------------------------------------------
# the profile-load-time 'total: true' membership check also unions -- and
# must not double-report the 'salt' value that two paths both name.
# --------------------------------------------------------------------------


def test_total_true_reports_each_missing_union_member_once(
    tmp_path, profile_dir, write
) -> None:
    total_profile = UNION_PROFILE.replace(
        'value: { type: float }',
        'value: { type: float }\n    total: true',
    )
    write('profile/types.yaml', total_profile)
    write('content/ingredients.yaml', '- { id: salt }\n- { id: sugar }\n')
    write('content/batches.yaml', '- { id: savory, extra: [salt] }\n')  # salt again
    write('content/tags.yaml', '- { id: vegan }\n')
    write('content/dishes.yaml', '- { id: d1, ratios: {} }\n')
    profile = load_profile(profile_dir)
    problems = errors_only(validate_corpus(profile, tmp_path))
    missing_salt = [p for p in problems if "key 'salt'" in p.message]
    assert len(missing_salt) == 1, missing_salt
