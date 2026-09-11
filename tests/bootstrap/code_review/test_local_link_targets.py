from __future__ import annotations

import sys
from pathlib import Path


BOOTSTRAP = Path(__file__).parents[3] / "plugins" / "bootstrap"
sys.path.insert(0, str(BOOTSTRAP))

from bootstrap_lib.code_review.mechanical import MechanicalSnapshot  # noqa: E402
from bootstrap_lib.code_review.mechanical_repository import (  # noqa: E402
    FrozenRepositoryView,
    ReadResult,
    StatResult,
)
from bootstrap_lib.code_review.mechanical_repository.local_link_targets import (  # noqa: E402
    CHECK,
    _markdown_anchors,
)


def _snapshot(text: str, *added: int, file: str = "docs/guide.md") -> MechanicalSnapshot:
    return MechanicalSnapshot(
        file=file,
        diff_section_text="diff",
        pre_image_text="",
        added_lines=tuple((line, "") for line in added),
        changed_lines=(),
        post_image_text=text,
    )


def _view(
    stats: dict[str, StatResult], reads: dict[str, ReadResult] | None = None
) -> FrozenRepositoryView:
    reads = reads or {}
    return FrozenRepositoryView(
        stats,
        reads,
        frozenset(stats),
        frozenset(reads),
    )


def test_collects_parser_decoded_and_normalized_targets() -> None:
    source = _snapshot(
        "[one](../a%23b.md?view=1#part%20one) and [two](x&amp;y.md)\n",
        1,
    )

    request = CHECK.collect({source.file: source})[source.file]

    assert request.diagnostic is None
    assert [(item.target, item.need_content) for item in request.requests] == [
        ("a#b.md", True),
        ("docs/x&y.md", False),
    ]


def test_missing_target_is_attributed_only_to_added_introduction_line() -> None:
    source = _snapshot("[old](old.md)\n\n[new](gone.md)\n", 3)
    requests = CHECK.collect({source.file: source})
    view = _view({"docs/gone.md": StatResult("missing")})

    outcome = CHECK.evaluate({source.file: source}, view, requests)[source.file]

    assert outcome.ran is True
    assert outcome.findings == (
        {
            "check": "local_link_targets",
            "line": 3,
            "detail": "'docs/gone.md': target does not exist in review snapshot",
        },
    )


def test_reference_definition_and_use_are_separate_introduction_sites() -> None:
    source = _snapshot(
        "[new][guide]\n\n[guide]: ../README.md#install\n",
        1,
        3,
    )
    requests = CHECK.collect({source.file: source})
    view = _view(
        {"README.md": StatResult("file", size=9)},
        {"README.md": ReadResult("file", b"# Other\n")},
    )

    outcome = CHECK.evaluate({source.file: source}, view, requests)[source.file]

    assert outcome.ran is True
    assert [finding["line"] for finding in outcome.findings] == [1, 3]
    assert all("anchor does not exist" in finding["detail"] for finding in outcome.findings)


def test_unused_added_reference_definition_is_covered_without_a_query() -> None:
    source = _snapshot("[unused]: missing.md\n", 1)

    requests = CHECK.collect({source.file: source})
    outcome = CHECK.evaluate({source.file: source}, _view({}), requests)[source.file]

    assert requests[source.file].requests == ()
    assert outcome == type(outcome)(True)


def test_reference_definitions_are_matched_by_label_identity() -> None:
    source = _snapshot(
        "[full][used] [collapsed][] [shortcut]\n\n"
        "[used]: same.md\n"
        "[collapsed]: collapsed.md\n"
        "[shortcut]: shortcut.md\n"
        "[unused]: same.md\n",
        1,
        3,
        4,
        5,
        6,
    )

    request = CHECK.collect({source.file: source})[source.file]

    assert request.diagnostic is None
    assert [item.target for item in request.requests] == [
        "docs/same.md",
        "docs/collapsed.md",
        "docs/shortcut.md",
    ]


def test_multi_line_local_link_explicitly_defers_the_source() -> None:
    source = _snapshot("[guide](\n../README.md\n)\n", 1)

    request = CHECK.collect({source.file: source})[source.file]

    assert request.requests == ()
    assert request.diagnostic == "local link in a multi-line Markdown inline block is unsupported"


def test_missing_post_image_does_not_claim_precondition_coverage() -> None:
    source = _snapshot("[guide](README.md)\n", 1)
    source = MechanicalSnapshot(
        source.file,
        source.diff_section_text,
        source.pre_image_text,
        source.added_lines,
        source.changed_lines,
        None,
    )

    assert CHECK.collect({source.file: source}) == {}


def test_directory_and_non_markdown_fragment_are_deliberately_out_of_scope() -> None:
    source = _snapshot(
        "[explicit-dir](../assets/) [ambiguous](../assets) [code](thing.py#symbol)\n",
        1,
    )
    requests = CHECK.collect({source.file: source})
    assert [item.target for item in requests[source.file].requests] == ["assets"]

    outcome = CHECK.evaluate(
        {source.file: source},
        _view({"assets": StatResult("directory")}),
        requests,
    )[source.file]

    assert outcome.ran is True
    assert outcome.findings == ()


def test_fragment_only_target_uses_source_post_image() -> None:
    source = _snapshot("# Setup\n\n[go](#setup)\n", 3)
    requests = CHECK.collect({source.file: source})
    view = _view(
        {source.file: StatResult("file", size=len(source.post_image_text or ""))},
        {source.file: ReadResult("file", (source.post_image_text or "").encode())},
    )

    outcome = CHECK.evaluate({source.file: source}, view, requests)[source.file]

    assert outcome.ran is True
    assert outcome.findings == ()


def test_mechanical_slug_v1_and_explicit_html_anchors() -> None:
    anchors = _markdown_anchors(
        (
            "# Hello, *WORLD* ![Sun](sun.png) `Code` <b>x</b>\n"
            "# Hello world Sun Code x\n"
            "# Hello world Sun Code x-1\n"
            "# Hi ![foo `bar`](x.png)\n"
            "<span ID=Exact></span><a name='named'></a>\n"
        ).encode()
    )

    assert anchors == {
        "hello-world-sun-code-x",
        "hello-world-sun-code-x-1",
        "hello-world-sun-code-x-1-1",
        "hi-foo-bar",
        "Exact",
        "named",
    }


def test_html_anchor_entities_decode_once_and_matching_is_case_sensitive() -> None:
    anchors = _markdown_anchors(b'<span ID="A&amp;amp;B"></span>')
    assert anchors == {"A&amp;B"}

    source = _snapshot('[go](target.md#A%26amp%3BB)\n', 1)
    requests = CHECK.collect({source.file: source})
    outcome = CHECK.evaluate(
        {source.file: source},
        _view(
            {"docs/target.md": StatResult("file", 30)},
            {"docs/target.md": ReadResult("file", b'<span id="A&amp;amp;B"></span>')},
        ),
        requests,
    )[source.file]
    assert outcome.ran is True
    assert outcome.findings == ()


def test_repeated_identical_links_on_one_line_collapse_one_finding() -> None:
    source = _snapshot("[a](gone.md) [b](gone.md)\n", 1)
    requests = CHECK.collect({source.file: source})

    outcome = CHECK.evaluate(
        {source.file: source},
        _view({"docs/gone.md": StatResult("missing")}),
        requests,
    )[source.file]

    assert len(outcome.findings) == 1


def test_snapshot_error_makes_source_uncovered_without_partial_findings() -> None:
    source = _snapshot("[missing](gone.md) [error](bad.md)\n", 1)
    requests = CHECK.collect({source.file: source})
    view = _view(
        {
            "docs/gone.md": StatResult("missing"),
            "docs/bad.md": StatResult("error", diagnostic="reader failed"),
        }
    )

    outcome = CHECK.evaluate({source.file: source}, view, requests)[source.file]

    assert outcome.ran is False
    assert outcome.findings == ()
    assert outcome.diagnostic == "reader failed"


def test_decoded_query_nul_and_backslash_defer_transactionally() -> None:
    for query in ("%00", "%5C"):
        source = _snapshot(f"[valid](valid.md) [invalid](bad.md?x={query})\n", 1)

        request = CHECK.collect({source.file: source})[source.file]

        assert request.requests == ()
        assert request.diagnostic is not None
        assert "unsupported local link destination" in request.diagnostic
