"""Tests for the dispatch request file seam."""

from pathlib import Path

import yaml

from yaml_data_editor_kit.dispatch.request import load_request


def test_valid_request_resolves_relative_paths_and_defaults_driver(
    tmp_path: Path, write
) -> None:
    request_path = tmp_path / "requests" / "dispatch.yaml"
    request_path.parent.mkdir()
    request_path.write_text(
        yaml.safe_dump(
            {
                "corpus_path": "../corpus",
                "comment_store_path": "../comments",
                "run_dir": "../runs/one",
                "selection": {"anchor_prefix": "product/"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    loaded = load_request(request_path)

    assert loaded.ok
    assert loaded.request is not None
    assert loaded.request.driver == "inline"
    assert loaded.request.corpus_path == (tmp_path / "corpus").resolve()
    assert loaded.request.comment_store_path == (tmp_path / "comments").resolve()
    assert loaded.request.run_dir == (tmp_path / "runs/one").resolve()
    assert loaded.request.selection.anchor_prefix == "product/"


def test_invalid_request_returns_named_diagnostics(tmp_path: Path) -> None:
    request_path = tmp_path / "dispatch.yaml"
    request_path.write_text(
        """
comment_store_path: comments
run_dir: runs
driver: unknown
selection:
  comment_ids: [one, one]
  anchor_prefix: product
surprise: true
""",
        encoding="utf-8",
    )

    loaded = load_request(request_path)

    assert loaded.request is None
    fields = {diagnostic.field for diagnostic in loaded.diagnostics}
    assert {"corpus_path", "driver", "selection"} <= fields
    messages = "\n".join(item.message for item in loaded.diagnostics)
    assert "unknown top-level key" in messages
    assert "duplicates" in messages
    assert "not both" in messages
