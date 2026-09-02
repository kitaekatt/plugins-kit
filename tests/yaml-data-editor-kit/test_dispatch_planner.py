"""Tests for the comment-to-work-unit planner."""

from pathlib import Path
from typing import Callable

from content_pipeline.freshness.hashing import content_hash

from yaml_data_editor_kit.comments import Comment, CommentStore
from yaml_data_editor_kit.dispatch.planner import CommentPlanStore, CommentPlanner
from yaml_data_editor_kit.schema import Corpus, Profile, load_corpus, load_profile

Writer = Callable[[str, str], Path]


def _catalogue(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> tuple[Profile, Corpus]:
    write(
        "profile/catalogue.yaml",
        """
dialect: type/1
id: product
identified_by: id
fields:
  id: { type: id }
  name: { type: string }
  summary: { type: text }
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
""",
    )
    write("content/products.yaml", "- { id: bolt, name: Bolt, summary: fastener }\n")
    write("content/settings.yaml", "theme: plain\n")
    profile = load_profile(profile_dir)
    return profile, load_corpus(profile, tmp_path)


def test_planner_collapses_record_comments_and_keeps_document_comments_separate(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    comments = [
        Comment.create(
            profile,
            corpus,
            id="summary-note",
            anchor="product/bolt/summary",
            text="Make the summary precise.",
            created="2026-08-30",
        ),
        Comment.create(
            profile,
            corpus,
            id="name-note",
            anchor="product/bolt/name",
            text="Use the displayed name.",
            created="2026-08-30",
        ),
        Comment.create(
            profile,
            corpus,
            id="doc-one",
            anchor="settings/@doc",
            text="Use the canonical theme.",
            created="2026-08-30",
        ),
        Comment.create(
            profile,
            corpus,
            id="doc-two",
            anchor="settings/@doc",
            text="Keep this document small.",
            created="2026-08-30",
        ),
    ]

    units = CommentPlanner().units(CommentPlanStore(profile, corpus, comments))

    assert [unit.id for unit in units] == [
        "comment:settings/@doc:doc-one",
        "comment:settings/@doc:doc-two",
        "record:product/bolt",
    ]
    record_unit = units[-1]
    assert record_unit.payload["comments"] == [
        "Use the displayed name.",
        "Make the summary precise.",
    ]
    assert record_unit.payload["anchored_slice"] == {
        "id": "bolt",
        "name": "Bolt",
        "summary": "fastener",
    }


def test_planner_ids_and_content_hashes_are_stable(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    comment = Comment.create(
        profile,
        corpus,
        id="note",
        anchor="product/bolt/name",
        text="Use the name.",
        created="2026-08-30",
    )
    store = CommentPlanStore(profile, corpus, [comment])

    first = CommentPlanner().units(store)
    second = CommentPlanner().units(store)

    assert [unit.id for unit in first] == [unit.id for unit in second]
    payload = first[0].payload
    assert payload["content_hash"] == content_hash(payload["anchored_slice"])
    assert len(payload["content_hash"]) == 16


def test_planner_ignores_resolved_comments(tmp_path: Path, profile_dir: Path, write: Writer) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    comment = Comment.create(
        profile,
        corpus,
        id="done",
        anchor="product/bolt",
        text="Already handled.",
        created="2026-08-30",
    )
    resolved = comment.__class__(
        id=comment.id,
        anchor=comment.anchor,
        text=comment.text,
        state="resolved",
        created=comment.created,
        guard=comment.guard,
        annotations=comment.annotations,
    )

    assert CommentPlanner().units(CommentPlanStore(profile, corpus, [resolved])) == []
