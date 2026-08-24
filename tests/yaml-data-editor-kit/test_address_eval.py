'''Core address evaluation for types, records, documents, and row ordinals.'''

from pathlib import Path
from typing import Callable

import pytest

from yaml_data_editor_kit.comments import (
    DOC,
    EvaluationError,
    Point,
    evaluate,
    parse_selector,
)
from yaml_data_editor_kit.schema import Corpus, Profile, load_corpus, load_profile

Writer = Callable[[str, str], Path]


def _catalogue(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> tuple[Profile, Corpus]:
    write(
        'profile/catalogue.yaml',
        '''
dialect: type/1
id: product
identified_by: id
fields:
  id: { type: id }
  name: { type: string }
---
dialect: source/1
of: product
layout: rows
path: content/products.yaml
---
dialect: type/1
id: settings
fields:
  theme: { type: string }
---
dialect: source/1
of: settings
layout: single
path: content/settings.yaml
---
dialect: type/1
id: rate_table
fields:
  note: { type: text }
value:
  type: map
  key: { type: enum, values: [standard] }
  value: { type: int }
---
dialect: source/1
of: rate_table
layout: keyed_map
path: content/rates.yaml
record_keys: [basic, plus]
---
dialect: type/1
id: decision
fields:
  what: { type: text }
---
dialect: source/1
of: decision
layout: rows
path: content/decisions.yaml
''',
    )
    write(
        'content/products.yaml',
        '- { id: bolt, name: Bolt }\n- { id: nut, name: Nut }\n',
    )
    write('content/settings.yaml', 'theme: plain\n')
    write(
        'content/rates.yaml',
        'note: reviewed\nbasic: { standard: 10 }\nplus: { standard: 20 }\n',
    )
    write('content/decisions.yaml', '- { what: first }\n- { what: second }\n')
    profile = load_profile(profile_dir)
    return profile, load_corpus(profile, tmp_path)


def test_whole_type_includes_value_records_and_the_document_record(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)

    result = evaluate(parse_selector('rate_table'), profile, corpus)

    assert result.points == frozenset(
        {
            Point('rate_table', 'basic'),
            Point('rate_table', 'plus'),
            Point('rate_table', DOC),
        }
    )
    assert result.matched_records == 3


def test_identity_matches_exactly(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)

    result = evaluate(parse_selector('product/bolt'), profile, corpus)

    assert result.points == frozenset({Point('product', 'bolt')})
    assert result.matched_records == 1


def test_unknown_type_names_the_selector_and_legal_types(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector('missing/bolt'), profile, corpus)

    message = str(exc_info.value)
    assert 'missing/bolt' in message
    assert 'missing' in message
    assert 'product' in message and 'settings' in message


def test_unknown_identity_is_an_error_not_an_empty_result(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector('product/washer'), profile, corpus)

    assert 'no record' in str(exc_info.value)
    assert 'washer' in str(exc_info.value)


def test_duplicate_identity_names_both_files(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    write(
        'profile/products.yaml',
        '''
dialect: type/1
id: product
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: product
layout: rows
path: content/a.yaml
---
dialect: source/1
of: product
layout: rows
path: content/b.yaml
''',
    )
    write('content/a.yaml', '- { id: bolt }\n')
    write('content/b.yaml', '- { id: bolt }\n')
    profile = load_profile(profile_dir)
    corpus = load_corpus(profile, tmp_path)

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector('product/bolt'), profile, corpus)

    message = str(exc_info.value)
    assert 'duplicate identities cannot be addressed' in message
    assert 'content/a.yaml' in message and 'content/b.yaml' in message


def test_document_segment_resolves_single_and_value_shaped_metadata(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)

    assert evaluate(parse_selector('settings/@doc'), profile, corpus).points == frozenset(
        {Point('settings', DOC)}
    )
    assert evaluate(parse_selector('rate_table/@doc'), profile, corpus).points == frozenset(
        {Point('rate_table', DOC)}
    )


def test_document_segment_is_refused_on_plain_rows(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector('product/@doc'), profile, corpus)

    assert 'has no document metadata record' in str(exc_info.value)
    assert '@doc' in str(exc_info.value)


def test_row_index_uses_the_file_and_on_disk_ordinal(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)

    result = evaluate(parse_selector('decision/#1'), profile, corpus)

    assert result.points == frozenset(
        {Point('decision', ('content/decisions.yaml', 1))}
    )


def test_row_index_is_refused_on_an_identified_type(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector('product/#1'), profile, corpus)

    message = str(exc_info.value)
    assert 'identified by' in message
    assert '#INDEX' in message


def test_row_index_out_of_range_names_the_file_and_row_count(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector('decision/#7'), profile, corpus)

    message = str(exc_info.value)
    assert 'content/decisions.yaml has 2 rows' in message
    assert '#7' in message


def test_row_index_is_ambiguous_across_multiple_files(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    write(
        'profile/decisions.yaml',
        '''
dialect: type/1
id: decision
fields:
  what: { type: text }
---
dialect: source/1
of: decision
layout: rows
path: content/a.yaml
---
dialect: source/1
of: decision
layout: rows
path: content/b.yaml
''',
    )
    write('content/a.yaml', '- { what: first }\n')
    write('content/b.yaml', '- { what: second }\n')
    profile = load_profile(profile_dir)
    corpus = load_corpus(profile, tmp_path)

    with pytest.raises(EvaluationError) as exc_info:
        evaluate(parse_selector('decision/#0'), profile, corpus)

    message = str(exc_info.value)
    assert 'live in 2 files' in message
    assert 'ambiguous' in message
