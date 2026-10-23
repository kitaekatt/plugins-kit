"""Tests for bootstrap_lib.code_review.review_exclusions and its pipeline axis."""

from pathlib import Path

import pytest

from bootstrap_lib.code_review import review_exclusions
from bootstrap_lib.code_review.pipeline import assemble_bundle
from bootstrap_lib.code_review.review_exclusions import (
    ReviewExclusionsError,
    match_exclusion,
    relative_identifier,
    resolve_exclusions,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty fake home, so the developer's real user layer never leaks in."""
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setattr(review_exclusions.Path, "home", classmethod(lambda cls: fake))
    return fake


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _added_section(ident: str, lines: list[str]) -> dict:
    body = "".join(f"+{line}\n" for line in lines)
    return {"identifier": ident, "text": f"<<{ident}>>\n{body}"}


class TestResolve:
    def test_no_layers_means_no_rules(self, tmp_path, home):
        assert resolve_exclusions(tmp_path / "ws") == []

    def test_layers_concatenate_user_first(self, tmp_path, home):
        ws = tmp_path / "ws"
        _write(home / ".claude" / "config" / "review_exclusions.yaml", "exclude: ['a/**']\n")
        _write(ws / ".claude" / "review_exclusions.yaml", "exclude: ['b/**', '!a/keep/**']\n")
        assert resolve_exclusions(ws) == [
            ("user review_exclusions.yaml", "a/**"),
            ("project review_exclusions.yaml", "b/**"),
            ("project review_exclusions.yaml", "!a/keep/**"),
        ]

    def test_empty_file_is_no_rules(self, tmp_path, home):
        ws = tmp_path / "ws"
        _write(ws / ".claude" / "review_exclusions.yaml", "")
        assert resolve_exclusions(ws) == []

    @pytest.mark.parametrize(
        "text, message",
        [
            ("- a\n", "top level must be a mapping"),
            ("paths: ['a']\n", "unknown top-level field"),
            ("exclude: 'a/**'\n", "exclude must be a list"),
            ("exclude: ['']\n", "non-blank glob"),
            ("exclude: [3]\n", "non-blank glob"),
            ("exclude: ['!']\n", "non-blank glob"),
            ("exclude: [\n", "cannot load YAML"),
        ],
    )
    def test_malformed_layer_raises(self, tmp_path, home, text, message):
        ws = tmp_path / "ws"
        _write(ws / ".claude" / "review_exclusions.yaml", text)
        with pytest.raises(ReviewExclusionsError, match=message):
            resolve_exclusions(ws)


class TestMatch:
    RULES = [
        ("project review_exclusions.yaml", ".databench/**"),
        ("project review_exclusions.yaml", "**/*.webp"),
        ("project review_exclusions.yaml", "!docs/keep/**"),
    ]

    def test_directory_glob(self):
        assert (
            match_exclusion(".databench/index-pages/x/decision.yaml", self.RULES)
            == "project review_exclusions.yaml: .databench/**"
        )

    def test_any_depth_glob_matches_root_file(self):
        assert match_exclusion("a.webp", self.RULES) == "project review_exclusions.yaml: **/*.webp"

    def test_negation_beats_positive(self):
        assert match_exclusion("docs/keep/a.webp", self.RULES) is None

    def test_unmatched_path(self):
        assert match_exclusion("src/app.py", self.RULES) is None

    def test_relative_identifier_prefers_local_under_root(self, tmp_path):
        ws = tmp_path / "ws"
        local = ws / "docs" / "a.webp"
        assert relative_identifier("//depot/x/docs/a.webp", str(local), ws) == "docs/a.webp"

    def test_relative_identifier_falls_back_to_identifier(self, tmp_path):
        assert relative_identifier("docs\\a.webp", None, tmp_path) == "docs/a.webp"


class TestPipelineAxis:
    def _bundle(self, tmp_path, ws, **kwargs):
        files = [
            {"identifier": ".databench/page.html", "local": str(ws / ".databench" / "page.html")},
            {"identifier": "src/app.py", "local": str(ws / "src" / "app.py")},
        ]
        return assemble_bundle(
            preamble="",
            sections=[
                _added_section(".databench/page.html", ["<p>x</p>"]),
                _added_section("src/app.py", ["print(1)"]),
            ],
            files=files,
            bundle_dir=tmp_path / "b",
            max_chunk_bytes=1024 * 1024,
            workspace_root=ws,
            **kwargs,
        )

    def test_configured_glob_excludes_file(self, tmp_path, home):
        ws = tmp_path / "ws"
        _write(ws / ".claude" / "review_exclusions.yaml", "exclude: ['.databench/**']\n")
        core = self._bundle(tmp_path, ws)
        [entry] = core["machine_emitted_files"]
        assert entry["identifier"] == ".databench/page.html"
        assert entry["machine_emitted_axis"] == "configured_path"
        assert entry["machine_emitted_signature"] == "project review_exclusions.yaml: .databench/**"
        assert [f["local"] for f in core["changed_files"]] == [str(ws / "src" / "app.py")]
        for chunk in core["diff_chunks"]:
            assert ".databench/page.html" not in chunk["files"]

    def test_configured_glob_outranks_a_claim(self, tmp_path, home):
        ws = tmp_path / "ws"
        _write(ws / ".claude" / "review_exclusions.yaml", "exclude: ['.databench/**']\n")
        core = self._bundle(tmp_path, ws, claim_globs=["**/*.html"])
        assert core["machine_emitted_files"][0]["identifier"] == ".databench/page.html"
        assert core["claimed_files"] == []

    def test_override_reviews_excluded_file(self, tmp_path, home):
        ws = tmp_path / "ws"
        _write(ws / ".claude" / "review_exclusions.yaml", "exclude: ['.databench/**']\n")
        core = self._bundle(tmp_path, ws, review_machine_emitted=True)
        assert "machine_emitted_files" not in core
        assert len(core["changed_files"]) == 2

    def test_malformed_config_stops_the_bundle(self, tmp_path, home):
        ws = tmp_path / "ws"
        _write(ws / ".claude" / "review_exclusions.yaml", "exclude: 'oops'\n")
        with pytest.raises(ReviewExclusionsError):
            self._bundle(tmp_path, ws)
