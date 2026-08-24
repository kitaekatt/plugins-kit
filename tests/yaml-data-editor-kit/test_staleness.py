'''Guard comparison, stale-anchor classification, and explicit re-anchoring.'''

from pathlib import Path
from typing import Callable

import pytest

from yaml_data_editor_kit.comments import (
    MOVED,
    OK,
    UNRESOLVABLE,
    Comment,
    EvaluationError,
    check_anchors,
    reanchor,
    resolve_anchor,
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
    write('content/settings.yaml', 'theme: plain\n')
    write('content/decisions.yaml', '- { what: first }\n- { what: second }\n')
    profile = load_profile(profile_dir)
    return profile, load_corpus(profile, tmp_path)


def _comment(
    profile: Profile, corpus: Corpus, comment_id: str, anchor: str
) -> Comment:
    return Comment.create(
        profile,
        corpus,
        id=comment_id,
        anchor=anchor,
        text='Review this slice.',
        created='2026-08-24',
    )


def test_unchanged_and_out_of_slice_edits_are_ok(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    comment = _comment(profile, corpus, 'summary', 'product/bolt/summary')

    unchanged = check_anchors(profile, corpus, [comment])[0]
    product = corpus.find('product', 'bolt')
    assert product is not None
    product.data['labels'].append('hardware')
    outside = check_anchors(profile, corpus, [comment])[0]

    assert unchanged.status == OK and unchanged.message == ''
    assert outside.status == OK and outside.current_guard == comment.guard


def test_in_slice_identity_edit_is_moved_without_mutating_the_guard(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    comment = _comment(profile, corpus, 'summary', 'product/bolt/summary')
    product = corpus.find('product', 'bolt')
    assert product is not None
    product.data['summary'] = 'changed'

    report = check_anchors(profile, corpus, [comment])[0]

    assert report.status == MOVED
    assert report.current_guard is not None
    assert report.current_guard != comment.guard
    assert 're-anchor' in report.message


def test_deleted_record_is_unresolvable_with_the_evaluation_message(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    comment = _comment(profile, corpus, 'summary', 'product/bolt/summary')
    product = corpus.find('product', 'bolt')
    assert product is not None
    corpus.records.remove(product)

    report = check_anchors(profile, corpus, [comment])[0]

    assert report.status == UNRESOLVABLE
    assert report.current_guard is None
    assert 'no record' in report.message and 'bolt' in report.message


def test_inserted_row_above_never_retargets_a_guarded_index(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    comment = _comment(profile, corpus, 'row', 'decision/#1')
    write(
        'content/decisions.yaml',
        '- { what: inserted }\n- { what: first }\n- { what: second }\n',
    )
    changed = load_corpus(profile, tmp_path)

    report = check_anchors(profile, changed, [comment])[0]

    assert report.status == UNRESOLVABLE
    assert report.current_guard is None
    assert 'changed or moved' in report.message
    assert 'never resolves to a different row' in report.message
    with pytest.raises(EvaluationError) as exc_info:
        resolve_anchor(comment.anchor, profile, changed, guard=comment.guard)
    assert 'never resolves to a different row' in str(exc_info.value)


def test_reanchor_is_the_explicit_guard_update_and_restores_ok(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    comment = _comment(profile, corpus, 'summary', 'product/bolt/summary')
    product = corpus.find('product', 'bolt')
    assert product is not None
    product.data['summary'] = 'changed'

    refreshed = reanchor(comment, profile, corpus)

    assert refreshed is not comment
    assert refreshed.guard != comment.guard
    assert comment.guard != check_anchors(profile, corpus, [comment])[0].current_guard
    assert check_anchors(profile, corpus, [refreshed])[0].status == OK


def test_every_anchor_kind_fits_the_three_state_model(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    anchors = {
        'type': 'product',
        'identity': 'product/bolt',
        'identity-field': 'product/bolt/summary',
        'document': 'settings/@doc',
        'document-field': 'settings/@doc/theme',
        'row': 'decision/#1',
        'row-field': 'decision/#1/what',
    }
    comments = [
        _comment(profile, corpus, comment_id, anchor)
        for comment_id, anchor in anchors.items()
    ]

    assert {report.status for report in check_anchors(profile, corpus, comments)} == {
        OK
    }

    product = corpus.find('product', 'bolt')
    assert product is not None
    product.data['summary'] = 'changed'
    settings = next(record for record in corpus.records if record.type_id == 'settings')
    settings.data['theme'] = 'contrast'
    decision = next(
        record
        for record in corpus.records
        if record.type_id == 'decision' and record.ordinal == 1
    )
    decision.data['what'] = 'changed'

    statuses = {
        report.comment_id: report.status
        for report in check_anchors(profile, corpus, comments)
    }
    assert statuses == {
        'type': MOVED,
        'identity': MOVED,
        'identity-field': MOVED,
        'document': MOVED,
        'document-field': MOVED,
        'row': UNRESOLVABLE,
        'row-field': UNRESOLVABLE,
    }
