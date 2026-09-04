"""Core contract tests for `skills_kit_lib.human_html`.

Covers the package-owned half of the human-html artifact defined by
`plugins/skills-kit/skills/md-domain/references/standards/human-html-standards.md`:
DR-1 (record schema and path mapping), DR-2 (subtree source stamp and dirty
state), DR-4 (instructions survive regeneration), SA-1 (the packaged dark
style asset), PC-1 / RD-2 (the generated-page marker), PC-2 (the navigation
spine), and PC-3 (the announce message).

The lane registration, the check script, and the discover script are separate
units and are not exercised here.
"""

import json
import subprocess

import pytest

from skills_kit_lib import human_html as hh


SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "89abcdef0123456789abcdef0123456789abcdef"


def _page_record(directory="src/example", **overrides):
    data = {
        "schema_version": 1,
        "directory": directory,
        "decision": "page",
        "source_sha": SHA,
        "dirty": False,
        "identity": "The subsystem that validates example inputs.",
        "instructions": "",
        "references": [],
    }
    data.update(overrides)
    return data


def _none_record(directory="src/quiet", **overrides):
    data = {
        "schema_version": 1,
        "directory": directory,
        "decision": "none",
        "source_sha": SHA,
        "dirty": False,
        "identity": "",
        "instructions": "",
        "references": [],
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# DR-1 field validation
# ---------------------------------------------------------------------------

class TestValidateRecord:
    def test_minimal_page_record_validates(self):
        assert hh.validate_record(_page_record())["decision"] == "page"

    def test_minimal_none_record_validates(self):
        assert hh.validate_record(_none_record())["identity"] == ""

    def test_root_directory_value_is_a_dot(self):
        assert hh.validate_record(_page_record(directory="."))["directory"] == "."

    def test_reference_entry_validates(self):
        record = _page_record(
            references=[{"slug": "protocol", "title": "Protocol map", "file": "human.protocol.html"}]
        )
        assert hh.validate_record(record)["references"][0]["slug"] == "protocol"

    @pytest.mark.parametrize("field", hh.RECORD_FIELDS)
    def test_every_field_is_required(self, field):
        record = _page_record()
        del record[field]
        with pytest.raises(hh.RecordValidationError, match=field):
            hh.validate_record(record)

    def test_unknown_field_is_rejected(self):
        with pytest.raises(hh.RecordValidationError, match="budget"):
            hh.validate_record(_page_record(budget=600))

    @pytest.mark.parametrize("value", [0, 2, "1", True, None])
    def test_schema_version_must_be_the_integer_one(self, value):
        with pytest.raises(hh.RecordValidationError, match="schema_version"):
            hh.validate_record(_page_record(schema_version=value))

    @pytest.mark.parametrize("value", ["./src", "src/", "src\\example", 3])
    def test_directory_must_be_normalized_and_a_string(self, value):
        with pytest.raises(hh.RecordValidationError, match="directory"):
            hh.validate_record(_page_record(directory=value))

    def test_directory_must_not_be_absolute(self):
        with pytest.raises(hh.RecordValidationError, match="directory"):
            hh.validate_record(_page_record(directory="/src"))

    @pytest.mark.parametrize("value", ["maybe", "", None, "PAGE"])
    def test_decision_is_page_or_none(self, value):
        with pytest.raises(hh.RecordValidationError, match="decision"):
            hh.validate_record(_page_record(decision=value))

    @pytest.mark.parametrize("value", ["abc", SHA.upper(), SHA[:39], SHA + "0", None])
    def test_source_sha_is_full_lowercase_40_hex(self, value):
        with pytest.raises(hh.RecordValidationError, match="source_sha"):
            hh.validate_record(_page_record(source_sha=value))

    @pytest.mark.parametrize("value", ["true", 1, None])
    def test_dirty_must_be_a_boolean(self, value):
        with pytest.raises(hh.RecordValidationError, match="dirty"):
            hh.validate_record(_page_record(dirty=value))

    def test_identity_must_be_nonempty_for_a_page(self):
        with pytest.raises(hh.RecordValidationError, match="identity"):
            hh.validate_record(_page_record(identity="   "))

    def test_identity_must_be_one_line(self):
        with pytest.raises(hh.RecordValidationError, match="identity"):
            hh.validate_record(_page_record(identity="one\ntwo"))

    def test_identity_may_be_empty_for_none(self):
        assert hh.validate_record(_none_record(identity=""))["identity"] == ""

    def test_instructions_must_be_a_string(self):
        with pytest.raises(hh.RecordValidationError, match="instructions"):
            hh.validate_record(_page_record(instructions=None))

    def test_empty_instructions_is_valid(self):
        assert hh.validate_record(_page_record(instructions=""))["instructions"] == ""

    def test_references_must_be_empty_for_none(self):
        record = _none_record(
            references=[{"slug": "protocol", "title": "T", "file": "human.protocol.html"}]
        )
        with pytest.raises(hh.RecordValidationError, match="references"):
            hh.validate_record(record)

    def test_reference_slug_grammar_is_enforced(self):
        record = _page_record(
            references=[{"slug": "Protocol Map", "title": "T", "file": "human.protocol.html"}]
        )
        with pytest.raises(hh.RecordValidationError, match="slug"):
            hh.validate_record(record)

    def test_reference_file_must_match_its_slug(self):
        record = _page_record(
            references=[{"slug": "protocol", "title": "T", "file": "human-protocol.html"}]
        )
        with pytest.raises(hh.RecordValidationError, match="file"):
            hh.validate_record(record)

    def test_duplicate_reference_slug_is_rejected(self):
        entry = {"slug": "protocol", "title": "T", "file": "human.protocol.html"}
        with pytest.raises(hh.RecordValidationError, match="duplicate"):
            hh.validate_record(_page_record(references=[entry, dict(entry)]))

    def test_reference_filename_helper_matches_rd_1(self):
        assert hh.reference_filename("protocol-map") == "human.protocol-map.html"


# ---------------------------------------------------------------------------
# DR-1 path mapping
# ---------------------------------------------------------------------------

class TestRecordPath:
    def test_root_maps_to_the_bare_record(self, tmp_path):
        assert hh.record_path(tmp_path, ".") == tmp_path / ".databench/human/decision.yaml"

    def test_empty_directory_is_the_root(self, tmp_path):
        assert hh.record_path(tmp_path, "") == hh.record_path(tmp_path, ".")

    def test_nested_directory_maps_under_the_record_root(self, tmp_path):
        expected = tmp_path / ".databench/human/src/example/decision.yaml"
        assert hh.record_path(tmp_path, "src/example") == expected

    def test_backslashes_are_normalized_to_posix(self, tmp_path):
        assert hh.record_path(tmp_path, "src\\example") == hh.record_path(tmp_path, "src/example")

    def test_traversal_above_the_root_is_rejected(self, tmp_path):
        with pytest.raises(hh.RecordValidationError, match="directory"):
            hh.record_path(tmp_path, "../outside")


# ---------------------------------------------------------------------------
# DR-4 instructions survive regeneration
# ---------------------------------------------------------------------------

class TestWriteRecord:
    def test_write_then_load_round_trips(self, tmp_path):
        path = hh.record_path(tmp_path, "src/example")
        hh.write_record(path, _page_record(), preserve_instructions=False)
        assert hh.load_record(path).to_dict() == _page_record()

    def test_record_is_written_in_json_syntax(self, tmp_path):
        path = hh.record_path(tmp_path, "src/example")
        hh.write_record(path, _page_record(), preserve_instructions=False)
        assert json.loads(path.read_text(encoding="utf-8"))["decision"] == "page"

    def test_regeneration_preserves_instructions_byte_identical(self, tmp_path):
        path = hh.record_path(tmp_path, "src/example")
        steering = "Lead with the retry budget; link the protocol reference."
        hh.write_record(
            path, _page_record(instructions=steering), preserve_instructions=False
        )
        before = path.read_bytes()

        regenerated = _page_record(source_sha=OTHER_SHA, identity="A rewritten identity line.")
        written = hh.write_record(path, regenerated)

        assert written.instructions == steering
        assert hh.load_record(path).instructions == steering
        assert path.read_bytes() != before
        assert written.source_sha == OTHER_SHA

    def test_missing_record_yields_empty_instructions(self, tmp_path):
        path = hh.record_path(tmp_path, "src/example")
        assert hh.read_instructions(path) == ""
        assert hh.write_record(path, _page_record(instructions="dropped")).instructions == ""

    def test_instructions_can_be_overridden_explicitly(self, tmp_path):
        path = hh.record_path(tmp_path, "src/example")
        hh.write_record(path, _page_record(instructions="old"), preserve_instructions=False)
        hh.write_record(path, _page_record(instructions="new"), preserve_instructions=False)
        assert hh.load_record(path).instructions == "new"

    def test_load_rejects_a_malformed_record(self, tmp_path):
        path = hh.record_path(tmp_path, "src/example")
        path.parent.mkdir(parents=True)
        path.write_text("decision: page\n", encoding="ascii")
        with pytest.raises(hh.RecordValidationError):
            hh.load_record(path)


# ---------------------------------------------------------------------------
# DR-2 subtree source stamp
# ---------------------------------------------------------------------------

def _git(repo, *args):
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def _head(repo):
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


@pytest.fixture
def stamp_repo(tmp_path):
    """A small git repository with one commit per subtree, newest last.

    Layout after the fixture runs::

        README.md              committed by `root`
        src/y.txt              committed by `src`
        src/deep/x.txt         committed by `deep`
        other/z.txt            committed by `other` (HEAD)

    The returned mapping carries the repository path under `"repo"` and each
    commit sha under its label, so a test can assert which commit a subtree
    stamp resolves to.
    """
    repo = tmp_path / "corpus"
    (repo / "src" / "deep").mkdir(parents=True)
    (repo / "other").mkdir()
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "tests")

    shas = {"repo": repo}
    (repo / "README.md").write_text("root\n", encoding="ascii")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "root")
    shas["root"] = _head(repo)

    (repo / "src" / "y.txt").write_text("y\n", encoding="ascii")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "src")
    shas["src"] = _head(repo)

    (repo / "src" / "deep" / "x.txt").write_text("x\n", encoding="ascii")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "deep")
    shas["deep"] = _head(repo)

    (repo / "other" / "z.txt").write_text("z\n", encoding="ascii")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "other")
    shas["other"] = _head(repo)
    return shas


class TestSourceStamp:
    def test_root_stamp_is_the_newest_commit(self, stamp_repo):
        sha, dirty = hh.source_stamp(stamp_repo["repo"], ".")
        assert sha == stamp_repo["other"]
        assert dirty is False

    def test_subtree_stamp_ignores_unrelated_commits(self, stamp_repo):
        assert hh.source_stamp(stamp_repo["repo"], "src")[0] == stamp_repo["deep"]
        assert hh.source_stamp(stamp_repo["repo"], "src/deep")[0] == stamp_repo["deep"]
        assert hh.source_stamp(stamp_repo["repo"], "other")[0] == stamp_repo["other"]

    def test_generated_output_is_excluded_from_the_stamp(self, stamp_repo):
        repo = stamp_repo["repo"]
        (repo / "src" / "human.html").write_text("<!-- generated -->\n", encoding="ascii")
        (repo / "src" / "human.protocol.html").write_text("<!-- generated -->\n", encoding="ascii")
        databench = repo / "src" / ".databench" / "human"
        databench.mkdir(parents=True)
        (databench / "decision.yaml").write_text("{}\n", encoding="ascii")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "generated output")

        sha, dirty = hh.source_stamp(repo, "src")
        assert sha == stamp_repo["deep"]
        assert dirty is False

    def test_uncommitted_generated_output_does_not_set_dirty(self, stamp_repo):
        repo = stamp_repo["repo"]
        (repo / "src" / "human.html").write_text("<!-- generated -->\n", encoding="ascii")
        assert hh.source_stamp(repo, "src")[1] is False

    def test_modified_tracked_input_sets_dirty(self, stamp_repo):
        repo = stamp_repo["repo"]
        (repo / "src" / "y.txt").write_text("y changed\n", encoding="ascii")
        assert hh.source_stamp(repo, "src") == (stamp_repo["deep"], True)
        assert hh.source_stamp(repo, "other")[1] is False

    def test_untracked_input_sets_dirty(self, stamp_repo):
        repo = stamp_repo["repo"]
        (repo / "src" / "deep" / "new.txt").write_text("new\n", encoding="ascii")
        assert hh.source_stamp(repo, "src")[1] is True
        assert hh.source_stamp(repo, "src/deep")[1] is True

    def test_glob_metacharacters_in_a_directory_name_do_not_defeat_the_excludes(
        self, stamp_repo
    ):
        """A bracketed routing folder such as `app/[slug]` must still exclude its output."""
        repo = stamp_repo["repo"]
        route = repo / "app" / "[slug]"
        route.mkdir(parents=True)
        (route / "page.txt").write_text("page\n", encoding="ascii")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "route")
        route_sha = _head(repo)

        (route / "human.html").write_text("<!-- generated -->\n", encoding="ascii")
        (route / "human.protocol.html").write_text("<!-- generated -->\n", encoding="ascii")
        assert hh.source_stamp(repo, "app/[slug]") == (route_sha, False)

        (route / "page.txt").write_text("page changed\n", encoding="ascii")
        assert hh.source_stamp(repo, "app/[slug]")[1] is True

    def test_directory_with_no_committed_input_raises(self, stamp_repo):
        repo = stamp_repo["repo"]
        (repo / "empty").mkdir()
        with pytest.raises(hh.SourceStampError):
            hh.source_stamp(repo, "empty")


# ---------------------------------------------------------------------------
# SA-1 style asset
# ---------------------------------------------------------------------------

SEED_VALUES = [
    "color-scheme: dark",
    "#080b10",
    "#d7deea",
    "#f3f6fb",
    "#8b98aa",
    "rgba(15,19,27,.88)",
    "#3d4a5e",
    "#5789f4",
    "#7ea3ff",
    "#4f82f7",
    "15px",
    "1.72",
    "Inter, ui-sans-serif, system-ui, sans-serif",
    "JetBrains Mono, ui-monospace, monospace",
    "--hh-space-1: .35rem",
    "--hh-space-5: 2rem",
    '.hh-nav-label',
    '.hh-nav-identity',
    "text-decoration-line: underline",
    "grid-template-columns: minmax(7rem, .22fr) minmax(0, 1fr)",
    "overflow: auto",
]


class TestAsset:
    def test_asset_is_ascii(self):
        hh.asset_css().encode("ascii")

    @pytest.mark.parametrize("value", SEED_VALUES)
    def test_asset_carries_each_seed_value(self, value):
        assert value in hh.asset_css()

    def test_asset_is_stable_across_reads(self):
        assert hh.asset_css() == hh.asset_css()

    def test_asset_defines_no_light_theme_override(self):
        assert "prefers-color-scheme: light" not in hh.asset_css()

    def test_asset_keeps_main_full_width(self):
        main_rule = hh.asset_css().split("main {", 1)[1].split("}", 1)[0]
        assert "width: 100%" in main_rule
        assert "max-width" not in main_rule


# ---------------------------------------------------------------------------
# PC-1 / RD-2 marker
# ---------------------------------------------------------------------------

class TestMarker:
    def test_page_marker_round_trips(self):
        record = hh.Record.from_dict(_page_record())
        parsed = hh.parse_marker(hh.marker(record, hh.KIND_PAGE))
        assert parsed == {
            "generated_by": "md-domain",
            "source_sha": SHA,
            "directory": "src/example",
            "kind": "page",
        }

    def test_reference_marker_round_trips(self):
        record = hh.Record.from_dict(_page_record())
        parsed = hh.parse_marker(hh.marker(record, hh.KIND_REFERENCE, reference="protocol"))
        assert parsed["kind"] == "reference"
        assert parsed["reference"] == "protocol"

    def test_marker_is_one_html_comment_line(self):
        text = hh.marker(_page_record(), hh.KIND_PAGE)
        assert "\n" not in text
        assert text.startswith("<!-- human-html: ") and text.endswith(" -->")

    def test_root_marker_carries_the_dot_directory(self):
        assert hh.parse_marker(hh.marker(_page_record(directory="."), "page"))["directory"] == "."

    def test_reference_marker_requires_a_slug(self):
        with pytest.raises(hh.MarkerError, match="reference"):
            hh.marker(_page_record(), hh.KIND_REFERENCE)

    def test_page_marker_rejects_a_slug(self):
        with pytest.raises(hh.MarkerError, match="reference"):
            hh.marker(_page_record(), hh.KIND_PAGE, reference="protocol")

    def test_unknown_kind_is_rejected(self):
        with pytest.raises(hh.MarkerError, match="kind"):
            hh.marker(_page_record(), "index")

    def test_missing_marker_is_reported(self):
        with pytest.raises(hh.MarkerError, match="no `<!-- human-html"):
            hh.parse_marker("<!doctype html>\n<html lang=\"en\"></html>\n")

    def test_duplicate_marker_is_reported(self):
        text = hh.marker(_page_record(), "page")
        with pytest.raises(hh.MarkerError, match="2 markers"):
            hh.parse_marker(text + "\n" + text)

    def test_marker_below_the_line_budget_is_reported(self):
        text = "\n" * hh.MARKER_MAX_LINE + hh.marker(_page_record(), "page")
        with pytest.raises(hh.MarkerError, match="first 20 lines"):
            hh.parse_marker(text)

    def test_malformed_marker_payload_is_reported(self):
        with pytest.raises(hh.MarkerError, match="valid JSON"):
            hh.parse_marker("<!-- human-html: {not json} -->")

    def test_marker_with_a_bad_sha_is_reported(self):
        text = "<!-- human-html: %s -->" % json.dumps(
            {"generated_by": "md-domain", "source_sha": "abc", "directory": ".", "kind": "page"}
        )
        with pytest.raises(hh.MarkerError, match="source_sha"):
            hh.parse_marker(text)


# ---------------------------------------------------------------------------
# PC-3 announce message
# ---------------------------------------------------------------------------

class TestAnnounceScript:
    def test_announce_constants_match_the_standard(self):
        assert hh.ANNOUNCE_TYPE == "human-html:announce"
        assert hh.ANNOUNCE_VERSION == 1

    def test_page_announce_carries_every_field(self):
        script = hh.announce_script(_page_record(), hh.PAGE_FILENAME, hh.KIND_PAGE)
        assert "if (window.parent !== window) {" in script
        assert "window.parent.postMessage({" in script
        assert '"human-html:announce"' in script
        assert "version: 1," in script
        assert 'directory: "src/example",' in script
        assert 'file: "human.html",' in script
        assert 'kind: "page",' in script
        assert 'source_sha: "%s"' % SHA in script
        assert '}, "*");' in script

    def test_announce_sends_nothing_without_a_parent(self):
        script = hh.announce_script(_page_record(), hh.PAGE_FILENAME, hh.KIND_PAGE)
        assert script.splitlines()[0].startswith("if (window.parent !== window)")

    def test_reference_announce_carries_the_slug(self):
        script = hh.announce_script(
            _page_record(), "human.protocol.html", hh.KIND_REFERENCE, reference="protocol"
        )
        assert 'kind: "reference",' in script
        assert 'reference: "protocol",' in script

    def test_announce_matches_the_marker(self):
        record = _page_record()
        parsed = hh.parse_marker(hh.marker(record, hh.KIND_PAGE))
        script = hh.announce_script(record, hh.PAGE_FILENAME, hh.KIND_PAGE)
        assert 'directory: "%s"' % parsed["directory"] in script
        assert 'source_sha: "%s"' % parsed["source_sha"] in script

    def test_announce_rejects_a_path_for_file(self):
        with pytest.raises(hh.MarkerError, match="file"):
            hh.announce_script(_page_record(), "sub/human.html", hh.KIND_PAGE)


# ---------------------------------------------------------------------------
# PC-2 navigation spine
# ---------------------------------------------------------------------------

@pytest.fixture
def spine():
    """A tree with a `none` gap between the root page and its nearest descendants.

    ``.`` page -- ``a`` none -- ``a/b`` page -- ``a/b/c`` page (shadowed by
    ``a/b``), ``d`` none -- ``d/e`` none, and ``f`` page.
    """
    decisions = {
        ".": "page",
        "a": "none",
        "a/b": "page",
        "a/b/c": "page",
        "d": "none",
        "d/e": "none",
        "f": "page",
    }
    return {
        directory: hh.Record.from_dict(
            {
                "schema_version": 1,
                "directory": directory,
                "decision": decision,
                "source_sha": SHA,
                "dirty": False,
                "identity": "Identity for %s." % directory if decision == "page" else "",
                "instructions": "",
                "references": [],
            }
        )
        for directory, decision in decisions.items()
    }


class TestNavigationTargets:
    def test_root_has_no_up_link(self, spine):
        up, _ = hh.navigation_targets(spine, ".")
        assert up is None

    def test_root_descends_through_the_none_gap(self, spine):
        _, down = hh.navigation_targets(spine, ".")
        assert down == ["a/b", "f"]

    def test_a_page_below_a_page_is_not_a_root_target(self, spine):
        assert "a/b/c" not in hh.navigation_targets(spine, ".")[1]

    def test_nearest_page_ancestor_skips_none_directories(self, spine):
        assert hh.navigation_targets(spine, "a/b")[0] == "."
        assert hh.navigation_targets(spine, "a/b/c")[0] == "a/b"
        assert hh.navigation_targets(spine, "d/e")[0] == "."

    def test_descendant_section_is_empty_at_a_leaf_page(self, spine):
        assert hh.navigation_targets(spine, "f") == (".", [])

    def test_none_directory_still_reports_its_targets(self, spine):
        assert hh.navigation_targets(spine, "a") == (".", ["a/b"])
        assert hh.navigation_targets(spine, "d") == (".", [])

    def test_no_page_ancestor_yields_no_up_link(self, spine):
        rootless = {key: value for key, value in spine.items() if key != "."}
        assert hh.navigation_targets(rootless, "d/e")[0] is None

    def test_plain_mappings_are_accepted(self):
        records = {".": {"decision": "page"}, "x": {"decision": "page"}}
        assert hh.navigation_targets(records, ".") == (None, ["x"])


class TestNavigationLabel:
    def test_root_uses_the_root_label(self):
        assert hh.navigation_label(".") == "Repository root"

    def test_nested_directory_uses_its_final_segment(self):
        assert hh.navigation_label("engine/src/rest") == "rest"

    def test_path_input_is_normalized(self):
        assert hh.navigation_label("engine\\src") == "src"
