'''Comment creation and file-per-comment persistence.'''

from pathlib import Path
from typing import Callable

import pytest
import yaml

from yaml_data_editor_kit.comments import (
    Comment,
    CommentStore,
    parse_anchor,
    resolve_anchor,
    slice_hash,
)
from yaml_data_editor_kit.schema import Corpus, Profile, load_corpus, load_profile

Writer = Callable[[str, str], Path]

_VALID_RAW = {
    'id': 'note',
    'anchor': 'product/bolt/summary',
    'text': 'Clarify this field.',
    'state': 'open',
    'created': '2026-08-24',
    'guard': 'sha256:' + ('0' * 64),
}


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
  summary: { type: text }
  labels:
    type: list
    of: { type: string }
---
dialect: source/1
of: product
layout: rows
path: content/products.yaml
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
        '- { id: bolt, summary: threaded fastener, labels: [metal] }\n',
    )
    write('content/decisions.yaml', '- { what: first }\n- { what: second }\n')
    profile = load_profile(profile_dir)
    return profile, load_corpus(profile, tmp_path)


def _comment(profile: Profile, corpus: Corpus, **overrides: object) -> Comment:
    values = {
        'id': 'note',
        'anchor': 'product/bolt/summary',
        'text': 'Clarify this field.',
        'created': '2026-08-24',
        'annotations': None,
    }
    values.update(overrides)
    return Comment.create(profile, corpus, **values)


def test_create_computes_the_anchored_slice_guard(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)

    comment = _comment(profile, corpus)

    resolved = resolve_anchor(comment.anchor, profile, corpus)
    assert comment.state == 'open'
    assert comment.anchor == parse_anchor('product/bolt/summary')
    assert comment.guard == slice_hash(resolved.slice_value)


def test_store_round_trip_preserves_comment_and_opaque_annotations(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    annotations = {
        'consumer': {
            'state': 'parked',
            'tags': ['small', {'verdict': False}],
            7: {'arbitrary': None},
        }
    }
    comment = _comment(profile, corpus, annotations=annotations)
    store = CommentStore.init(tmp_path / 'comments')

    path = store.write(comment)
    loaded = store.load()

    assert path == tmp_path / 'comments' / 'note.yaml'
    assert loaded.diagnostics == []
    assert loaded.comments == [comment]
    assert loaded.comments[0].annotations == annotations
    persisted = yaml.safe_load(path.read_text(encoding='utf-8'))
    assert persisted['annotations'] == annotations


def test_resolve_writes_only_the_kit_resolved_state(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    comment = _comment(
        profile,
        corpus,
        annotations={'consumer': {'state': 'converted'}},
    )
    store = CommentStore.init(tmp_path / 'comments')
    store.write(comment)

    resolved = store.resolve(comment)
    loaded = store.load().comments[0]

    assert comment.state == 'open'
    assert resolved.state == 'resolved'
    assert loaded.state == 'resolved'
    assert loaded.guard == comment.guard
    assert loaded.annotations == comment.annotations


@pytest.mark.parametrize('missing', sorted(_VALID_RAW))
def test_each_required_field_omission_is_a_named_diagnostic(
    tmp_path: Path, write: Writer, missing: str
) -> None:
    root = tmp_path / 'comments'
    raw = dict(_VALID_RAW)
    del raw[missing]
    write('comments/note.yaml', yaml.safe_dump(raw, sort_keys=False))

    loaded = CommentStore(root).load()

    assert loaded.comments == []
    matching = [item for item in loaded.diagnostics if item.field == missing]
    assert len(matching) == 1
    assert 'note.yaml' in str(matching[0])
    assert 'missing required field' in matching[0].message


def test_filename_and_id_disagreement_is_a_named_diagnostic(
    tmp_path: Path, write: Writer
) -> None:
    raw = dict(_VALID_RAW, id='different')
    write('comments/note.yaml', yaml.safe_dump(raw, sort_keys=False))

    loaded = CommentStore(tmp_path / 'comments').load()

    assert loaded.comments == []
    assert len(loaded.diagnostics) == 1
    message = str(loaded.diagnostics[0])
    assert 'note.yaml' in message
    assert 'different' in message
    assert 'does not match' in message


def test_unknown_top_level_key_fails_with_the_legal_set(
    tmp_path: Path, write: Writer
) -> None:
    raw = dict(_VALID_RAW, surprise=True)
    write('comments/note.yaml', yaml.safe_dump(raw, sort_keys=False))

    loaded = CommentStore(tmp_path / 'comments').load()

    assert loaded.comments == []
    assert len(loaded.diagnostics) == 1
    message = str(loaded.diagnostics[0])
    assert 'surprise' in message
    assert 'annotations' in message and 'guard' in message


def test_non_mapping_annotations_fail_without_reading_their_contents(
    tmp_path: Path, write: Writer
) -> None:
    raw = dict(_VALID_RAW, annotations=['consumer-owned'])
    write('comments/note.yaml', yaml.safe_dump(raw, sort_keys=False))

    loaded = CommentStore(tmp_path / 'comments').load()

    assert loaded.comments == []
    assert len(loaded.diagnostics) == 1
    assert loaded.diagnostics[0].field == 'annotations'
    assert 'mapping' in loaded.diagnostics[0].message


def test_only_open_and_resolved_are_kit_states(
    tmp_path: Path, write: Writer
) -> None:
    raw = dict(_VALID_RAW, state='parked')
    write('comments/note.yaml', yaml.safe_dump(raw, sort_keys=False))

    loaded = CommentStore(tmp_path / 'comments').load()

    assert loaded.comments == []
    assert len(loaded.diagnostics) == 1
    assert 'open or resolved' in loaded.diagnostics[0].message


def test_corrupt_yaml_fails_loudly_while_other_files_are_reported(
    tmp_path: Path, write: Writer
) -> None:
    write('comments/broken.yaml', 'id: [unterminated\n')
    write('comments/note.yaml', yaml.safe_dump(_VALID_RAW, sort_keys=False))

    loaded = CommentStore(tmp_path / 'comments').load()

    assert [comment.id for comment in loaded.comments] == ['note']
    assert len(loaded.diagnostics) == 1
    assert 'broken.yaml' in str(loaded.diagnostics[0])
    assert 'cannot read comment YAML' in loaded.diagnostics[0].message


def test_atomic_write_leaves_no_temporary_file(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    store = CommentStore.init(tmp_path / 'comments')

    store.write(_comment(profile, corpus))

    assert sorted(path.name for path in store.root.iterdir()) == ['note.yaml']


def test_write_refuses_existing_file_with_a_different_declared_id(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    store = CommentStore.init(tmp_path / 'comments')
    write(
        'comments/note.yaml',
        yaml.safe_dump(dict(_VALID_RAW, id='different'), sort_keys=False),
    )

    with pytest.raises(ValueError) as exc_info:
        store.write(_comment(profile, corpus))

    message = str(exc_info.value)
    assert 'note.yaml' in message
    assert 'different' in message and 'note' in message


def test_missing_store_root_is_an_error_not_an_empty_set(tmp_path: Path) -> None:
    root = tmp_path / 'misspelled'

    with pytest.raises(FileNotFoundError) as exc_info:
        CommentStore(root).load()

    assert str(root) in str(exc_info.value)


def test_init_refuses_an_existing_non_store_directory(
    tmp_path: Path, write: Writer
) -> None:
    write('comments/unrelated.txt', 'not a comment')

    with pytest.raises(ValueError) as exc_info:
        CommentStore.init(tmp_path / 'comments')

    assert 'unrelated.txt' in str(exc_info.value)


def test_store_load_does_not_require_anchor_to_still_evaluate(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    store = CommentStore.init(tmp_path / 'comments')
    store.write(_comment(profile, corpus))
    product = corpus.find('product', 'bolt')
    assert product is not None
    corpus.records.remove(product)

    loaded = store.load()

    assert loaded.diagnostics == []
    assert [comment.id for comment in loaded.comments] == ['note']


def test_0120_record_without_kind_loads_as_instruction(
    tmp_path: Path, write: Writer
) -> None:
    write('comments/note.yaml', yaml.safe_dump(_VALID_RAW, sort_keys=False))

    loaded = CommentStore(tmp_path / 'comments').load()

    assert loaded.diagnostics == []
    assert loaded.comments[0].kind == 'instruction'
    assert loaded.comments[0].ruling is None


def test_instruction_round_trip_writes_explicit_kind(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    store = CommentStore.init(tmp_path / 'comments')

    store.write(_comment(profile, corpus))

    persisted = yaml.safe_load((store.root / 'note.yaml').read_text(encoding='utf-8'))
    assert persisted['kind'] == 'instruction'
    assert 'ruling' not in persisted


def test_create_question_computes_guard_and_open_shape(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    question = Comment.create_question(
        profile,
        corpus,
        id='question-1',
        anchor='product/bolt',
        text='Which value is authoritative?',
        created='2026-09-01',
        annotations={'source': {'run': 'run-1'}},
    )

    resolved = resolve_anchor(question.anchor, profile, corpus)
    assert question.kind == 'question'
    assert question.state == 'open'
    assert question.ruling is None
    assert question.guard == slice_hash(resolved.slice_value)


def test_rule_resolves_question_and_preserves_prompt_guard_and_annotations(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    question = Comment.create_question(
        profile,
        corpus,
        id='question-1',
        anchor='product/bolt',
        text='Which value is authoritative?',
        created='2026-09-01',
        annotations={'source': {'run': 'run-1'}},
    )
    store = CommentStore.init(tmp_path / 'comments')
    store.write(question)

    resolved = store.rule(question, 'Use the displayed value.')
    loaded = store.load().comments[0]

    assert resolved.state == 'resolved'
    assert resolved.text == question.text
    assert resolved.anchor == question.anchor
    assert resolved.guard == question.guard
    assert resolved.created == question.created
    assert resolved.annotations == question.annotations
    assert resolved.ruling == 'Use the displayed value.'
    assert loaded == resolved


def test_resolve_rejects_question_and_names_rule(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    question = Comment.create_question(
        profile,
        corpus,
        id='question-1',
        anchor='product/bolt',
        text='Which value is authoritative?',
        created='2026-09-01',
    )

    with pytest.raises(ValueError, match=r'rule\(\)'):
        CommentStore.init(tmp_path / 'comments').resolve(question)


def test_unknown_kind_is_a_named_diagnostic(tmp_path: Path, write: Writer) -> None:
    write('comments/note.yaml', yaml.safe_dump(dict(_VALID_RAW, kind='other')))

    loaded = CommentStore(tmp_path / 'comments').load()

    assert loaded.comments == []
    assert any(item.field == 'kind' and 'kind' in item.message for item in loaded.diagnostics)


def test_whitespace_only_question_text_is_a_named_diagnostic(
    tmp_path: Path, write: Writer
) -> None:
    write(
        'comments/note.yaml',
        yaml.safe_dump(dict(_VALID_RAW, kind='question', text='   ', ruling=None)),
    )

    loaded = CommentStore(tmp_path / 'comments').load()

    assert loaded.comments == []
    assert any(
        item.field == 'text' and 'non-whitespace' in item.message
        for item in loaded.diagnostics
    )


def test_instruction_with_ruling_is_a_named_diagnostic(
    tmp_path: Path, write: Writer
) -> None:
    write(
        'comments/note.yaml',
        yaml.safe_dump(dict(_VALID_RAW, ruling='Not allowed')),
    )

    loaded = CommentStore(tmp_path / 'comments').load()

    assert loaded.comments == []
    assert any(item.field == 'ruling' and 'instruction' in item.message for item in loaded.diagnostics)


def test_open_question_with_ruling_is_a_named_diagnostic(
    tmp_path: Path, write: Writer
) -> None:
    raw = dict(_VALID_RAW, kind='question', ruling='Already decided')
    write('comments/note.yaml', yaml.safe_dump(raw))

    loaded = CommentStore(tmp_path / 'comments').load()

    assert loaded.comments == []
    assert any(item.field == 'ruling' and 'open' in item.message for item in loaded.diagnostics)


def test_resolved_question_without_ruling_is_a_named_diagnostic(
    tmp_path: Path, write: Writer
) -> None:
    raw = dict(_VALID_RAW, kind='question', state='resolved', ruling=None)
    write('comments/note.yaml', yaml.safe_dump(raw))

    loaded = CommentStore(tmp_path / 'comments').load()

    assert loaded.comments == []
    assert any(
        item.field == 'ruling' and 'non-empty' in item.message
        for item in loaded.diagnostics
    )
