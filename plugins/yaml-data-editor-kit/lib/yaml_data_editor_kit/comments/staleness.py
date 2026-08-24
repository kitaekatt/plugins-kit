'''Classify guarded comment anchors against a loaded corpus.'''

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

from yaml_data_editor_kit.schema import Corpus, Profile

from .address import resolve_anchor
from .errors import EvaluationError
from .hashing import slice_hash
from .store import Comment

OK = 'ok'
MOVED = 'moved'
UNRESOLVABLE = 'unresolvable'


@dataclass(frozen=True)
class AnchorReport:
    '''One comment anchor staleness result.'''

    comment_id: str
    anchor: str
    status: str
    message: str
    current_guard: str | None


def check_anchors(
    profile: Profile,
    corpus: Corpus,
    comments: Iterable[Comment],
) -> list[AnchorReport]:
    '''Classify every comment anchor without changing any stored guard.'''
    reports: list[AnchorReport] = []
    for comment in comments:
        canonical = comment.anchor.canonical()
        try:
            resolved = resolve_anchor(
                comment.anchor,
                profile,
                corpus,
                guard=comment.guard,
            )
        except EvaluationError as exc:
            reports.append(
                AnchorReport(
                    comment_id=comment.id,
                    anchor=canonical,
                    status=UNRESOLVABLE,
                    message=str(exc),
                    current_guard=None,
                )
            )
            continue

        current_guard = slice_hash(resolved.slice_value)
        if current_guard == comment.guard:
            status = OK
            message = ''
        else:
            status = MOVED
            message = (
                'the anchored slice under {!r} changed; re-anchor comment {!r} '
                'to accept the current content'.format(canonical, comment.id)
            )
        reports.append(
            AnchorReport(
                comment_id=comment.id,
                anchor=canonical,
                status=status,
                message=message,
                current_guard=current_guard,
            )
        )
    return reports


def reanchor(comment: Comment, profile: Profile, corpus: Corpus) -> Comment:
    '''Return a copy whose guard explicitly accepts the current anchored slice.'''
    resolved = resolve_anchor(comment.anchor, profile, corpus)
    return replace(comment, guard=slice_hash(resolved.slice_value))
