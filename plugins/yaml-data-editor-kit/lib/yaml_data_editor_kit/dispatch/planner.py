"""Plan anchored comments as content-pipeline-kit work units.

The planner uses mechanical grouping: comments on one concrete record share a
unit, while document-level and type-level comments each get their own unit.
Agentic grouping is the second pass; the planner does not infer a larger task
from comment wording.
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from content_pipeline.freshness.hashing import content_hash
from content_pipeline.pipeline.workunit import WorkUnit, WorkUnitStrategy

from yaml_data_editor_kit.comments import DOC, Comment, CommentSet, resolve_anchor
from yaml_data_editor_kit.schema import Corpus, Profile

from .request import DispatchSelection


class CommentPlanStore:
    """The profile, corpus, and comments supplied to :class:`CommentPlanner`."""

    def __init__(
        self,
        profile: Profile,
        corpus: Corpus,
        comments: Sequence[Comment] | CommentSet,
        selection: DispatchSelection | None = None,
    ) -> None:
        self.profile = profile
        self.corpus = corpus
        self.comments = comments
        self.selection = selection or DispatchSelection()


class CommentPlanner(WorkUnitStrategy):
    """Turn open comments into stable, mechanically grouped work units.

    The constructor accepts the components directly for callers that already
    loaded them. The same strategy can receive a :class:`CommentPlanStore` or
    a compatible object through ``units(store)``.
    """

    def __init__(
        self,
        profile: Profile | None = None,
        corpus: Corpus | None = None,
        comments: Sequence[Comment] | CommentSet | None = None,
        selection: DispatchSelection | None = None,
    ) -> None:
        self.profile = profile
        self.corpus = corpus
        self.comments = comments
        self.selection = selection

    def units(self, store: Any) -> list[WorkUnit]:
        """Return one unit per anchored record or standalone document comment."""
        profile = self.profile or getattr(store, "profile", None)
        corpus = self.corpus or getattr(store, "corpus", None)
        comments = self.comments
        if comments is None:
            comments = getattr(store, "comments", None)
        if profile is None or corpus is None or comments is None:
            raise ValueError(
                "CommentPlanner requires profile, corpus, and comments"
            )

        if isinstance(comments, CommentSet):
            comments = comments.comments
        selection = self.selection
        if selection is None:
            selection = getattr(store, "selection", None) or DispatchSelection()

        selected = _select_comments(comments, selection)
        grouped: dict[tuple[Any, ...], list[tuple[Comment, Any, Any]]] = {}
        for comment in sorted(selected, key=lambda item: item.id):
            resolved = resolve_anchor(comment.anchor, profile, corpus)
            group_key, unit_anchor = _group_key(comment, resolved)
            grouped.setdefault(group_key, []).append(
                (comment, resolved, unit_anchor)
            )

        planned: list[WorkUnit] = []
        for entries in grouped.values():
            first_comment, first_resolved, unit_anchor = entries[0]
            if _is_record_group(first_comment):
                anchored_slice = first_resolved.record.data
            else:
                anchored_slice = first_resolved.slice_value
            payload = {
                "anchor": unit_anchor,
                "anchored_slice": deepcopy(anchored_slice),
                "comments": [comment.text for comment, _, _ in entries],
                "comment_ids": [comment.id for comment, _, _ in entries],
                "comment_anchors": [
                    comment.anchor.canonical() for comment, _, _ in entries
                ],
                "comment_guards": [comment.guard for comment, _, _ in entries],
                "content_hash": content_hash(anchored_slice),
            }
            planned.append(
                WorkUnit(
                    id=_unit_id(entries[0][0], unit_anchor, _is_record_group(first_comment)),
                    payload=payload,
                )
            )
        return planned


def _select_comments(
    comments: Sequence[Comment], selection: DispatchSelection
) -> list[Comment]:
    ids = set(selection.comment_ids)
    prefix = selection.anchor_prefix
    selected: list[Comment] = []
    for comment in comments:
        if comment.state != "open":
            continue
        if ids and comment.id not in ids:
            continue
        if prefix is not None and not comment.anchor.canonical().startswith(prefix):
            continue
        selected.append(comment)
    return selected


def _is_record_group(comment: Comment) -> bool:
    """Return whether an anchor identifies a concrete record."""
    return comment.anchor.record_seg is not None and comment.anchor.record_seg is not DOC


def _group_key(comment: Comment, resolved: Any) -> tuple[tuple[Any, ...], str]:
    if _is_record_group(comment):
        point = resolved.point
        if point is None:
            raise ValueError(
                "record anchor {!r} did not resolve to a concrete point".format(
                    comment.anchor.canonical()
                )
            )
        anchor = _record_anchor(comment)
        return ("record", point.type_id, point.record), anchor
    anchor = comment.anchor.canonical()
    return ("comment", anchor, comment.id), anchor


def _record_anchor(comment: Comment) -> str:
    selector = comment.anchor
    parts = [str(selector.type_seg)]
    if selector.record_seg is not None:
        record = selector.record_seg
        if isinstance(record, int):
            parts.append("#{}".format(record))
        else:
            parts.append(str(record))
    return "/".join(parts)


def _unit_id(comment: Comment, unit_anchor: str, record_group: bool) -> str:
    if record_group:
        return "record:{}".format(unit_anchor)
    return "comment:{}:{}".format(unit_anchor, comment.id)


__all__ = ["CommentPlanStore", "CommentPlanner"]
