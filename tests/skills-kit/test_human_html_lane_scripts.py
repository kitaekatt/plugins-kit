"""Contract tests for md-domain's two human-html lane scripts.

Covers `scripts/discover_human_html.py` (CK-2) and `scripts/human_html_check.py`
(CK-1) from
`plugins/skills-kit/skills/md-domain/references/standards/human-html-standards.md`.
The package core they consume (`skills_kit_lib.human_html`) is exercised
separately in `test_human_html_core.py`.

Fixtures are real temporary git repositories, because both scripts ask git two
questions no stub answers honestly: which files the repository ignores, and
which commit last touched a subtree (DR-2). The compliant-page builder below is
also load-bearing evidence: if no page can satisfy PC-1 to PC-4, NF-1 and RD-2
at once, the contract is unsatisfiable rather than merely unmet.

The scripts are loaded via importlib under unique module names, matching the
sibling discover_* test files -- the md-domain scripts directory ships several
scripts a bare import would collide on in pytest's module cache.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from skills_kit_lib import human_html as hh


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "skills-kit"
SCRIPTS_DIR = PLUGIN_ROOT / "skills" / "md-domain" / "scripts"


def _load(name: str, path: Path):
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


discover = _load("discover_human_html", SCRIPTS_DIR / "discover_human_html.py")
checker = _load("human_html_check", SCRIPTS_DIR / "human_html_check.py")


# ---------------------------------------------------------------------------
# Repository fixture
# ---------------------------------------------------------------------------

def _git(repo, *args):
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def _commit(repo, message="change"):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


@pytest.fixture
def repo(tmp_path):
    """A small repository with two page branches and one quiet directory.

        README.md
        src/app.py
        src/deep/mod.py
        lib/util.py
        quiet/notes.txt
        vendor/bundle.js        (git-ignored)
    """
    root = tmp_path / "corpus"
    for directory in ("src/deep", "lib", "quiet", "vendor"):
        (root / directory).mkdir(parents=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "tests")

    (root / ".gitignore").write_text("vendor/\n", encoding="ascii")
    (root / "README.md").write_text("root\n", encoding="ascii")
    (root / "src" / "app.py").write_text("app = 1\n", encoding="ascii")
    (root / "src" / "deep" / "mod.py").write_text("mod = 1\n", encoding="ascii")
    (root / "lib" / "util.py").write_text("util = 1\n", encoding="ascii")
    (root / "quiet" / "notes.txt").write_text("notes\n", encoding="ascii")
    (root / "vendor" / "bundle.js").write_text("bundle\n", encoding="ascii")
    _commit(root, "corpus")
    return root


# ---------------------------------------------------------------------------
# Record and page builders
# ---------------------------------------------------------------------------

def make_record(repo, directory, decision="page", identity=None, references=(), instructions=""):
    """Write a fresh DR-1 record for `directory` and return it."""
    sha, dirty = hh.source_stamp(repo, directory)
    normalized = hh.normalize_directory(directory)
    if identity is None:
        identity = "The %s subsystem." % normalized
    data = {
        "schema_version": 1,
        "directory": normalized,
        "decision": decision,
        "source_sha": sha,
        "dirty": dirty,
        "identity": identity if decision == "page" else "",
        "instructions": instructions,
        "references": [
            {"slug": slug, "title": title, "file": hh.reference_filename(slug)}
            for slug, title in references
        ],
    }
    return hh.write_record(
        hh.record_path(repo, normalized), data, preserve_instructions=False
    )


def page_html(
    record,
    filename=hh.PAGE_FILENAME,
    kind=hh.KIND_PAGE,
    slug=None,
    nav_items=(),
    body="<p>Orientation prose for the returning owner.</p>",
    marker_text=None,
    announce_text=None,
    style_text=None,
    metadata=True,
    nav=True,
    nav_markup=None,
):
    """Build a page that satisfies PC-1 to PC-4, NF-1 and RD-2.

    Every override argument exists so a test can break exactly one rule and
    watch the matching FAIL appear.
    """
    marker = hh.marker(record, kind, slug) if marker_text is None else marker_text
    announce = (
        hh.announce_script(record, filename, kind, slug)
        if announce_text is None
        else announce_text
    )
    style = hh.asset_css() if style_text is None else style_text
    head = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="color-scheme" content="dark">',
    ] if metadata else []
    if not nav:
        nav_block = []
    elif nav_markup is not None:
        nav_block = [nav_markup]
    else:
        nav_block = [
            '<nav data-human-html-chrome="nav" aria-label="Orientation pages">',
            "  <ul>",
        ]
        for href, label, identity in nav_items:
            nav_block.extend(
                [
                    "    <li>",
                    '      <a href="%s">' % href,
                    '        <span class="hh-nav-label">%s</span>' % label,
                    '        <span class="hh-nav-identity">%s</span>' % identity,
                    "      </a>",
                    "    </li>",
                ]
            )
        nav_block.extend(["  </ul>", "</nav>"])
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            marker,
        ]
        + head
        + [
            "<title>Orientation</title>",
            "<style data-human-html-style>",
            style,
            "</style>",
            "</head>",
            "<body>",
        ]
        + nav_block
        + [
            "<main>",
            body,
            "</main>",
            "<script>",
            announce,
            "</script>",
            "</body>",
            "</html>",
            "",
        ]
    )


def write_page(repo, directory, record, **kwargs):
    filename = kwargs.get("filename", hh.PAGE_FILENAME)
    base = repo if hh.normalize_directory(directory) == "." else repo / directory
    path = base / filename
    nav_links = kwargs.pop("nav_links", ())
    if "nav_items" not in kwargs:
        items = []
        for href in nav_links:
            target_page = (base / href).resolve()
            target_directory = hh.normalize_directory(
                target_page.parent.relative_to(repo.resolve()).as_posix()
            )
            target_record = hh.load_record(hh.record_path(repo, target_directory))
            items.append(
                (href, hh.navigation_label(target_directory), target_record.identity)
            )
        kwargs["nav_items"] = items
    path.write_text(page_html(record, **kwargs), encoding="ascii")
    return path


def run_check(repo, directory="."):
    return checker.check(repo, directory)


def codes(result, level=None):
    return sorted(
        finding["code"]
        for finding in result["findings"]
        if level is None or finding["level"] == level
    )


# ---------------------------------------------------------------------------
# CK-2 discovery
# ---------------------------------------------------------------------------

class TestDiscoveryWalk:
    def test_every_directory_with_an_input_is_a_subject(self, repo):
        found = {entry["directory"] for entry in discover.scan(repo)["directories"]}
        assert found == {".", "src", "src/deep", "lib", "quiet"}

    def test_ignored_directories_are_not_subjects(self, repo):
        found = {entry["directory"] for entry in discover.scan(repo)["directories"]}
        assert "vendor" not in found

    def test_record_tree_is_not_a_subject(self, repo):
        make_record(repo, "src")
        found = {entry["directory"] for entry in discover.scan(repo)["directories"]}
        assert not any(item.startswith(".databench") for item in found)

    def test_a_generated_only_directory_is_not_a_subject(self, repo):
        (repo / "generated").mkdir()
        (repo / "generated" / hh.PAGE_FILENAME).write_text("<!-- x -->\n", encoding="ascii")
        (repo / "generated" / "human.protocol.html").write_text("<!-- x -->\n", encoding="ascii")
        _commit(repo, "generated only")
        found = {entry["directory"] for entry in discover.scan(repo)["directories"]}
        assert "generated" not in found

    def test_order_is_deepest_first(self, repo):
        order = [entry["directory"] for entry in discover.scan(repo)["directories"]]
        assert order.index("src/deep") < order.index("src")
        assert order[-1] == "."
        depths = [entry["depth"] for entry in discover.scan(repo)["directories"]]
        assert depths == sorted(depths, reverse=True)

    def test_scope_narrows_the_emitted_set_only(self, repo):
        make_record(repo, ".", decision="page")
        result = discover.scan(repo, "src")
        assert [entry["directory"] for entry in result["directories"]] == ["src/deep", "src"]
        src = next(e for e in result["directories"] if e["directory"] == "src")
        assert src["nearest_page_ancestor"] == "."

    def test_root_maps_to_the_root_record_path(self, repo):
        make_record(repo, ".")
        entry = next(
            e for e in discover.scan(repo)["directories"] if e["directory"] == "."
        )
        assert entry["record"]["path"] == ".databench/human/decision.yaml"
        assert entry["record"]["status"] == "fresh"


class TestDiscoveryRecordState:
    def test_missing_record_is_reported(self, repo):
        entry = next(e for e in discover.scan(repo)["directories"] if e["directory"] == "lib")
        assert entry["record"]["status"] == "missing"
        assert entry["record"]["decision"] is None
        assert entry["stale"] is True

    def test_fresh_record_reports_decision_and_identity(self, repo):
        make_record(repo, "src", identity="The application core.")
        entry = next(e for e in discover.scan(repo)["directories"] if e["directory"] == "src")
        assert entry["record"]["status"] == "fresh"
        assert entry["record"]["decision"] == "page"
        assert entry["record"]["identity"] == "The application core."

    def test_a_changed_input_stales_the_record(self, repo):
        make_record(repo, "src/deep")
        (repo / "src" / "deep" / "mod.py").write_text("mod = 2\n", encoding="ascii")
        _commit(repo, "change deep")
        entry = next(
            e for e in discover.scan(repo)["directories"] if e["directory"] == "src/deep"
        )
        assert entry["record"]["status"] == "stale"

    def test_uncommitted_input_reports_dirty(self, repo):
        (repo / "src" / "app.py").write_text("app = 2\n", encoding="ascii")
        entry = next(e for e in discover.scan(repo)["directories"] if e["directory"] == "src")
        assert entry["dirty"] is True

    def test_generated_output_does_not_dirty_the_stamp(self, repo):
        record = make_record(repo, "src")
        write_page(repo, "src", record)
        entry = next(e for e in discover.scan(repo)["directories"] if e["directory"] == "src")
        assert entry["dirty"] is False
        assert entry["record"]["status"] == "fresh"

    def test_invalid_record_is_reported_apart_from_missing(self, repo):
        path = hh.record_path(repo, "lib")
        path.parent.mkdir(parents=True)
        path.write_text('{"decision": "page"}\n', encoding="ascii")
        entry = next(e for e in discover.scan(repo)["directories"] if e["directory"] == "lib")
        assert entry["record"]["status"] == "invalid"
        assert entry["record"]["error"]

    def test_generated_files_are_listed(self, repo):
        record = make_record(repo, "src", references=[("protocol", "Protocol map")])
        write_page(repo, "src", record)
        write_page(
            repo, "src", record,
            filename="human.protocol.html", kind=hh.KIND_REFERENCE, slug="protocol",
            nav_links=[hh.PAGE_FILENAME],
        )
        entry = next(e for e in discover.scan(repo)["directories"] if e["directory"] == "src")
        assert entry["page_file"] == hh.PAGE_FILENAME
        assert entry["reference_files"] == ["human.protocol.html"]


class TestDiscoveryNavigationAndStaleness:
    def test_none_directories_are_traversed_not_targeted(self, repo):
        make_record(repo, ".", decision="page")
        make_record(repo, "src", decision="none")
        make_record(repo, "src/deep", decision="page")
        root = next(e for e in discover.scan(repo)["directories"] if e["directory"] == ".")
        assert root["nearest_page_descendants"] == ["src/deep"]
        deep = next(
            e for e in discover.scan(repo)["directories"] if e["directory"] == "src/deep"
        )
        assert deep["nearest_page_ancestor"] == "."

    def test_multiple_descendant_branches_each_yield_one_target(self, repo):
        make_record(repo, ".", decision="page")
        make_record(repo, "src", decision="none")
        make_record(repo, "src/deep", decision="page")
        make_record(repo, "lib", decision="page")
        make_record(repo, "quiet", decision="none")
        root = next(e for e in discover.scan(repo)["directories"] if e["directory"] == ".")
        assert root["nearest_page_descendants"] == ["lib", "src/deep"]

    def test_a_page_below_a_page_is_not_a_root_target(self, repo):
        make_record(repo, ".", decision="page")
        make_record(repo, "src", decision="page")
        make_record(repo, "src/deep", decision="page")
        root = next(e for e in discover.scan(repo)["directories"] if e["directory"] == ".")
        assert root["nearest_page_descendants"] == ["src"]

    def test_a_stale_child_propagates_to_every_ancestor(self, repo):
        for directory in (".", "src", "src/deep", "lib", "quiet"):
            make_record(repo, directory, decision="none")
        (repo / "src" / "deep" / "mod.py").write_text("mod = 3\n", encoding="ascii")
        _commit(repo, "change deep")
        entries = {e["directory"]: e for e in discover.scan(repo)["directories"]}
        assert entries["src/deep"]["record"]["status"] == "stale"
        assert entries["src"]["stale_child"] is True
        assert entries["."]["stale_child"] is True
        assert entries["lib"]["stale_child"] is False

    def test_a_missing_child_record_also_propagates(self, repo):
        make_record(repo, ".", decision="none")
        make_record(repo, "src", decision="none")
        entries = {e["directory"]: e for e in discover.scan(repo)["directories"]}
        assert entries["src"]["stale_child"] is True
        assert "src/deep" in entries["src"]["stale_children"]


class TestDiscoveryCli:
    def test_cli_emits_json(self, repo):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "discover_human_html.py"), str(repo)],
            capture_output=True, text=True, check=True,
            env={"PYTHONPATH": str(PLUGIN_ROOT), "PATH": "/usr/bin:/bin:/usr/local/bin"},
        )
        payload = json.loads(proc.stdout)
        assert payload["count"] == len(payload["directories"])
        assert payload["scope"] == "."


# ---------------------------------------------------------------------------
# CK-1 clean cases
# ---------------------------------------------------------------------------

class TestCleanCases:
    def test_a_clean_page_passes(self, repo):
        make_record(repo, "src/deep", decision="none")
        record = make_record(repo, "src")
        write_page(repo, "src", record)
        result = run_check(repo, "src")
        assert result["ok"], result["findings"]
        assert result["findings"] == []

    def test_a_clean_none_passes(self, repo):
        make_record(repo, "quiet", decision="none")
        result = run_check(repo, "quiet")
        assert result["ok"], result["findings"]

    def test_the_root_page_maps_to_the_root_record(self, repo):
        for directory in ("src/deep", "src", "lib", "quiet"):
            make_record(repo, directory, decision="none")
        record = make_record(repo, ".")
        write_page(repo, ".", record)
        result = run_check(repo, ".")
        assert result["findings"] == []
        assert "." in result["checked"]

    def test_nested_pages_link_through_a_none_directory(self, repo):
        root = make_record(repo, ".", decision="page")
        make_record(repo, "src", decision="none")
        deep = make_record(repo, "src/deep", decision="page")
        make_record(repo, "lib", decision="none")
        make_record(repo, "quiet", decision="none")
        write_page(repo, ".", root, nav_links=["src/deep/human.html"])
        write_page(repo, "src/deep", deep, nav_links=["../../human.html"])
        result = run_check(repo)
        assert result["ok"], result["findings"]

    def test_a_clean_reference_passes(self, repo):
        record = make_record(repo, "src", references=[("protocol", "Protocol map")])
        write_page(
            repo, "src", record,
            body='<p>See <a href="human.protocol.html">the protocol map</a>.</p>',
        )
        write_page(
            repo, "src", record,
            filename="human.protocol.html", kind=hh.KIND_REFERENCE, slug="protocol",
            nav_links=[hh.PAGE_FILENAME],
        )
        result = run_check(repo, "src")
        assert result["ok"], result["findings"]

    def test_exit_status_is_zero_without_a_fail(self, repo):
        record = make_record(repo, "src")
        write_page(repo, "src", record)
        assert checker.main([str(repo), "src"]) == 0

    def test_exit_status_is_nonzero_with_a_fail(self, repo):
        make_record(repo, "src")
        assert checker.main([str(repo), "src"]) == 1

    def test_json_output_is_machine_readable(self, repo, capsys):
        record = make_record(repo, "src")
        write_page(repo, "src", record)
        checker.main([str(repo), "src", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["fail_count"] == 0
        assert payload["checked"] == ["src"]


# ---------------------------------------------------------------------------
# CK-1 record FAILs
# ---------------------------------------------------------------------------

class TestRecordFailures:
    def test_missing_record_fails(self, repo):
        result = run_check(repo, "lib")
        assert "record-missing" in codes(result, "FAIL")

    def test_invalid_record_fails(self, repo):
        path = hh.record_path(repo, "lib")
        path.parent.mkdir(parents=True)
        path.write_text("not json\n", encoding="ascii")
        assert "record-invalid" in codes(run_check(repo, "lib"), "FAIL")

    def test_a_page_beside_a_none_decision_fails(self, repo):
        record = make_record(repo, "src")
        write_page(repo, "src", record)
        make_record(repo, "src", decision="none")
        assert "page-for-none" in codes(run_check(repo, "src"), "FAIL")

    def test_a_reference_beside_a_none_decision_fails(self, repo):
        record = make_record(repo, "src", references=[("protocol", "Protocol map")])
        write_page(
            repo, "src", record, filename="human.protocol.html",
            kind=hh.KIND_REFERENCE, slug="protocol", nav_links=[hh.PAGE_FILENAME],
        )
        make_record(repo, "src", decision="none")
        assert "page-for-none" in codes(run_check(repo, "src"), "FAIL")

    def test_a_page_decision_with_no_page_fails(self, repo):
        make_record(repo, "src")
        assert "page-missing" in codes(run_check(repo, "src"), "FAIL")

    def test_a_listed_reference_with_no_file_fails(self, repo):
        record = make_record(repo, "src", references=[("protocol", "Protocol map")])
        write_page(repo, "src", record)
        assert "reference-file-missing" in codes(run_check(repo, "src"), "FAIL")

    def test_an_unlisted_reference_file_fails(self, repo):
        record = make_record(repo, "src")
        write_page(repo, "src", record)
        write_page(
            repo, "src", record, filename="human.protocol.html",
            kind=hh.KIND_REFERENCE, slug="protocol", nav_links=[hh.PAGE_FILENAME],
        )
        assert "reference-unlisted" in codes(run_check(repo, "src"), "FAIL")

    def test_a_generated_page_with_no_analysis_input_fails(self, repo):
        """A page in a directory CK-2 omits must not slip through as a clean run.

        The discovery walk excludes a directory holding only generated output,
        which is right for the walk -- no analysis input means no DR-2 stamp and
        no record that could ever describe the page. Without the orphan sweep
        that same exclusion made the checker report "0 FAIL, exit 0" over a
        `human.html` nothing can maintain.
        """
        (repo / "orphan").mkdir()
        (repo / "orphan" / hh.PAGE_FILENAME).write_text("<!-- x -->\n", encoding="ascii")
        result = run_check(repo)
        assert "page-orphaned" in codes(result, "FAIL")
        assert "orphan" in result["checked"]

    def test_an_orphaned_page_fails_when_named_explicitly(self, repo):
        (repo / "orphan").mkdir()
        (repo / "orphan" / hh.PAGE_FILENAME).write_text("<!-- x -->\n", encoding="ascii")
        assert "page-orphaned" in codes(run_check(repo, "orphan"), "FAIL")
        assert checker.main([str(repo), "orphan"]) == 1

    def test_an_orphaned_reference_fails(self, repo):
        (repo / "orphan").mkdir()
        (repo / "orphan" / "human.protocol.html").write_text("<!-- x -->\n", encoding="ascii")
        assert "page-orphaned" in codes(run_check(repo), "FAIL")

    def test_a_directory_that_lost_its_inputs_fails_even_with_a_record(self, repo):
        record = make_record(repo, "quiet", decision="page")
        write_page(repo, "quiet", record)
        (repo / "quiet" / "notes.txt").unlink()
        _commit(repo, "drop the last input")
        result = run_check(repo, "quiet")
        assert "page-orphaned" in codes(result, "FAIL")

    def test_an_orphaned_page_outside_the_scope_is_not_reported(self, repo):
        (repo / "orphan").mkdir()
        (repo / "orphan" / hh.PAGE_FILENAME).write_text("<!-- x -->\n", encoding="ascii")
        make_record(repo, "src/deep", decision="none")
        record = make_record(repo, "src")
        write_page(repo, "src", record)
        assert run_check(repo, "src")["ok"]

    def test_a_reference_the_page_does_not_link_fails(self, repo):
        record = make_record(repo, "src", references=[("protocol", "Protocol map")])
        write_page(repo, "src", record)
        write_page(
            repo, "src", record, filename="human.protocol.html",
            kind=hh.KIND_REFERENCE, slug="protocol", nav_links=[hh.PAGE_FILENAME],
        )
        assert "reference-not-linked" in codes(run_check(repo, "src"), "FAIL")


# ---------------------------------------------------------------------------
# CK-1 page FAILs
# ---------------------------------------------------------------------------

class TestPageFailures:
    def test_a_missing_marker_fails(self, repo):
        record = make_record(repo, "src")
        write_page(repo, "src", record, marker_text="<!-- no marker here -->")
        assert "marker" in codes(run_check(repo, "src"), "FAIL")

    def test_a_duplicate_marker_fails(self, repo):
        record = make_record(repo, "src")
        doubled = hh.marker(record, hh.KIND_PAGE) + "\n" + hh.marker(record, hh.KIND_PAGE)
        write_page(repo, "src", record, marker_text=doubled)
        assert "marker" in codes(run_check(repo, "src"), "FAIL")

    def test_a_malformed_marker_fails(self, repo):
        record = make_record(repo, "src")
        write_page(repo, "src", record, marker_text="<!-- human-html: {not json} -->")
        assert "marker" in codes(run_check(repo, "src"), "FAIL")

    def test_a_marker_inconsistent_with_the_record_fails(self, repo):
        record = make_record(repo, "src")
        stale = record.to_dict()
        stale["source_sha"] = "0" * 40
        write_page(repo, "src", record, marker_text=hh.marker(stale, hh.KIND_PAGE))
        assert "marker-inconsistent" in codes(run_check(repo, "src"), "FAIL")

    def test_a_page_carrying_a_reference_marker_fails(self, repo):
        record = make_record(repo, "src")
        write_page(
            repo, "src", record,
            marker_text=hh.marker(record, hh.KIND_REFERENCE, "protocol"),
        )
        assert "marker-inconsistent" in codes(run_check(repo, "src"), "FAIL")

    def test_missing_metadata_fails(self, repo):
        record = make_record(repo, "src")
        write_page(repo, "src", record, metadata=False)
        assert "metadata" in codes(run_check(repo, "src"), "FAIL")

    def test_a_missing_navigation_region_fails(self, repo):
        record = make_record(repo, "src")
        write_page(repo, "src", record, nav=False)
        assert "navigation" in codes(run_check(repo, "src"), "FAIL")

    def test_navigation_that_disagrees_with_the_spine_fails(self, repo):
        make_record(repo, ".", decision="page")
        record = make_record(repo, "src", decision="page")
        write_page(repo, ".", make_record(repo, ".", decision="page"),
                   nav_links=["src/human.html"])
        write_page(repo, "src", record, nav_links=[])
        assert "navigation-mismatch" in codes(run_check(repo, "src"), "FAIL")

    def test_navigation_links_must_be_list_items(self, repo):
        record = make_record(repo, "src", decision="page")
        root = make_record(repo, ".", decision="page")
        write_page(
            repo,
            ".",
            root,
            nav_markup=(
                '<nav data-human-html-chrome="nav">'
                '<a href="src/human.html">The src subsystem.</a>'
                "</nav>"
            ),
        )
        write_page(repo, "src", record, nav_links=["../human.html"])
        assert "navigation-structure" in codes(run_check(repo, "."), "FAIL")

    def test_navigation_text_must_match_the_label_and_identity(self, repo):
        record = make_record(repo, "src", decision="page")
        root = make_record(repo, ".", decision="page")
        write_page(
            repo,
            ".",
            root,
            nav_items=[("src/human.html", "source", "A different identity.")],
        )
        write_page(repo, "src", record, nav_links=["../human.html"])
        assert "navigation-structure" in codes(run_check(repo, "."), "FAIL")

    def test_each_navigation_list_item_has_exactly_one_link(self, repo):
        src = make_record(repo, "src", decision="page")
        lib = make_record(repo, "lib", decision="page")
        root = make_record(repo, ".", decision="page")
        write_page(
            repo,
            ".",
            root,
            nav_markup=(
                '<nav data-human-html-chrome="nav"><ul><li>'
                '<a href="src/human.html">'
                '<span class="hh-nav-label">src</span>'
                f'<span class="hh-nav-identity">{src.identity}</span></a>'
                '<a href="lib/human.html">'
                '<span class="hh-nav-label">lib</span>'
                f'<span class="hh-nav-identity">{lib.identity}</span></a>'
                "</li><li></li></ul></nav>"
            ),
        )
        write_page(repo, "src", src, nav_links=["../human.html"])
        write_page(repo, "lib", lib, nav_links=["../human.html"])
        assert "navigation-structure" in codes(run_check(repo, "."), "FAIL")

    def test_a_missing_announce_fails(self, repo):
        record = make_record(repo, "src")
        write_page(repo, "src", record, announce_text="// nothing announced")
        assert "announce" in codes(run_check(repo, "src"), "FAIL")

    def test_a_style_that_is_not_the_packaged_asset_fails(self, repo):
        record = make_record(repo, "src")
        write_page(repo, "src", record, style_text="body { color: red; }")
        assert "style" in codes(run_check(repo, "src"), "FAIL")

    def test_a_linked_stylesheet_fails(self, repo):
        record = make_record(repo, "src")
        write_page(
            repo, "src", record,
            body='<link rel="stylesheet" href="human.css">',
        )
        assert "style" in codes(run_check(repo, "src"), "FAIL")

    def test_non_ascii_bytes_fail(self, repo):
        record = make_record(repo, "src")
        path = write_page(repo, "src", record)
        path.write_bytes(path.read_bytes().replace(b"Orientation prose", b"Orientation pros\xc3\xa9"))
        assert "non-ascii" in codes(run_check(repo, "src"), "FAIL")


class TestPortabilityFailures:
    @pytest.mark.parametrize(
        "href",
        [
            "https://example.com/page.html",
            "//example.com/page.html",
            "/absolute/page.html",
            "C:/win/page.html",
            "\\\\host\\share\\page.html",
        ],
    )
    def test_a_nonrelative_href_fails(self, repo, href):
        record = make_record(repo, "src")
        write_page(repo, "src", record, body='<a href="%s">x</a>' % href)
        assert "url-not-relative" in codes(run_check(repo, "src"), "FAIL")

    def test_an_unresolvable_href_fails(self, repo):
        record = make_record(repo, "src")
        write_page(repo, "src", record, body='<a href="missing.html">x</a>')
        assert "url-unresolvable" in codes(run_check(repo, "src"), "FAIL")

    def test_a_missing_same_document_fragment_fails(self, repo):
        record = make_record(repo, "src")
        write_page(repo, "src", record, body='<a href="#nowhere">x</a>')
        assert "url-unresolvable" in codes(run_check(repo, "src"), "FAIL")

    def test_a_present_same_document_fragment_passes(self, repo):
        record = make_record(repo, "src")
        write_page(
            repo, "src", record,
            body='<h2 id="here">Here</h2><a href="#here">x</a>',
        )
        assert run_check(repo, "src")["ok"]

    def test_an_href_escaping_the_repository_fails(self, repo):
        record = make_record(repo, "src")
        write_page(repo, "src", record, body='<a href="../../outside.html">x</a>')
        assert "url-escapes-repository" in codes(run_check(repo, "src"), "FAIL")

    def test_a_disallowed_carrier_fails(self, repo):
        record = make_record(repo, "src")
        write_page(repo, "src", record, body='<object src="app.py"></object>')
        assert "url-carrier" in codes(run_check(repo, "src"), "FAIL")

    def test_an_allowed_carrier_resolving_inside_the_repository_passes(self, repo):
        record = make_record(repo, "src")
        write_page(repo, "src", record, body='<a href="app.py">the module</a>')
        assert run_check(repo, "src")["ok"]

    @pytest.mark.parametrize("api", ["fetch('app.py')", "new XMLHttpRequest()"])
    def test_a_network_api_fails(self, repo, api):
        record = make_record(repo, "src")
        announce = hh.announce_script(record, hh.PAGE_FILENAME, hh.KIND_PAGE)
        write_page(repo, "src", record, announce_text=announce + "\n" + api + ";")
        assert "script-network-api" in codes(run_check(repo, "src"), "FAIL")

    @pytest.mark.parametrize(
        "literal",
        ['"https://example.com/x"', '"//example.com/x"', '"/etc/passwd"',
         '"C:/win/x"', '"www.example.com"'],
    )
    def test_an_absolute_script_literal_fails(self, repo, literal):
        record = make_record(repo, "src")
        announce = hh.announce_script(record, hh.PAGE_FILENAME, hh.KIND_PAGE)
        write_page(
            repo, "src", record,
            announce_text=announce + "\nvar target = " + literal + ";",
        )
        assert "script-absolute-reference" in codes(run_check(repo, "src"), "FAIL")


class TestReferenceFailures:
    def test_a_reference_marker_slug_mismatch_fails(self, repo):
        record = make_record(repo, "src", references=[("protocol", "Protocol map")])
        write_page(
            repo, "src", record,
            body='<a href="human.protocol.html">reference</a>',
        )
        write_page(
            repo, "src", record, filename="human.protocol.html",
            kind=hh.KIND_REFERENCE, slug="protocol", nav_links=[hh.PAGE_FILENAME],
            marker_text=hh.marker(record, hh.KIND_REFERENCE, "other"),
        )
        assert "marker-inconsistent" in codes(run_check(repo, "src"), "FAIL")

    def test_a_reference_without_a_backlink_fails(self, repo):
        record = make_record(repo, "src", references=[("protocol", "Protocol map")])
        write_page(
            repo, "src", record,
            body='<a href="human.protocol.html">reference</a>',
        )
        write_page(
            repo, "src", record, filename="human.protocol.html",
            kind=hh.KIND_REFERENCE, slug="protocol", nav_links=[],
        )
        assert "navigation-mismatch" in codes(run_check(repo, "src"), "FAIL")


# ---------------------------------------------------------------------------
# CK-1 INFO signals
# ---------------------------------------------------------------------------

class TestInfoSignals:
    def test_a_stale_record_is_info_not_fail(self, repo):
        record = make_record(repo, "src")
        write_page(repo, "src", record)
        (repo / "src" / "app.py").write_text("app = 9\n", encoding="ascii")
        _commit(repo, "change app")
        result = run_check(repo, "src")
        assert "STALE" in codes(result, "INFO")
        assert result["fail_count"] == 0
        assert result["ok"]

    def test_stale_child_propagation_is_info_on_the_ancestor(self, repo):
        for directory in (".", "src", "lib", "quiet"):
            make_record(repo, directory, decision="none")
        result = run_check(repo)
        root = [f for f in result["findings"] if f["directory"] == "." and f["code"] == "STALE"]
        assert root, result["findings"]
        assert root[0]["level"] == "INFO"

    def test_a_dirty_record_is_info(self, repo):
        (repo / "src" / "app.py").write_text("app = uncommitted\n", encoding="ascii")
        record = make_record(repo, "src")
        assert record.dirty is True
        write_page(repo, "src", record)
        result = run_check(repo, "src")
        assert "DIRTY" in codes(result, "INFO")
        assert result["fail_count"] == 0

    def test_an_oversized_page_is_info(self, repo):
        record = make_record(repo, "src")
        write_page(repo, "src", record, body="<p>%s</p>" % ("word " * 700))
        result = run_check(repo, "src")
        assert "size" in codes(result, "INFO")
        assert result["fail_count"] == 0

    def test_chrome_and_script_text_is_not_counted(self, repo):
        make_record(repo, "src/deep", decision="none")
        record = make_record(repo, "src")
        write_page(
            repo, "src", record,
            body='<div data-human-html-chrome="footer">%s</div>' % ("word " * 700),
        )
        assert codes(run_check(repo, "src"), "INFO") == []

    def test_the_root_budget_is_larger(self, repo):
        record = make_record(repo, ".")
        write_page(repo, ".", record, body="<p>%s</p>" % ("word " * 700))
        result = run_check(repo, ".")
        assert "size" not in codes(result, "INFO")

    def test_instructions_can_override_the_budget(self, repo):
        record = make_record(repo, "src", instructions="budget: 100")
        write_page(repo, "src", record, body="<p>%s</p>" % ("word " * 200))
        assert "size" in codes(run_check(repo, "src"), "INFO")
