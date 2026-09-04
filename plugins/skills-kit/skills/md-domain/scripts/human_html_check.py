#!/usr/bin/env python3
"""human_html_check.py -- the CK-1 contract checker for generated human HTML.

Usage:
    python human_html_check.py <repository-root>
    python human_html_check.py <repository-root> <directory>
    python human_html_check.py <repository-root> [<directory>] --json

Judges what a generation run produced against `human-html-standards.md`: the
decision record (DR-1, DR-2), the page identity, navigation, announce snippet
and inline style (PC-1 to PC-4), the portability prohibitions (PC-6, NF-1), the
reference contract (RD-1, RD-2), and the size signal (SZ-1).

TWO LEVELS, AND THE SPLIT IS THE POINT.

  * `FAIL` is a broken contract: output that is unportable, inconsistent with
    its record, or missing where the record says it must exist. A run with at
    least one FAIL exits nonzero.
  * `INFO` is a signal for a human to act on: `STALE` (the recorded source stamp
    is not the one DR-2 recomputes, including stale-child propagation under
    TS-2), `DIRTY` (the record itself says no commit identifies the judged
    content), and a visible-word count above its SZ-1 budget. None of these
    makes the exit status nonzero, because each is resolved by rerunning the
    lane or by editing prose, not by fixing a defect in the output.

WHAT IS CHECKED. A directory is checked when it carries a decision record, or
when generated output sits in it, or when the caller names it explicitly. A
plain directory that has none of those is not a subject: reporting "missing
record" for every directory in a repository would bury the findings that matter
under a list of directories nobody has analyzed yet.

EVERY GENERATED FILE IS ACCOUNTED FOR, including one the CK-2 walk does not
reach. A directory holding no analysis input is not a discovery subject, so a
`human.html` left in one is invisible to the subject loop above -- and a check
that silently passes over a generated page is the one outcome this script exists
to prevent. `orphaned_output` sweeps for exactly those files and FAILs them.

Imports: the Python standard library, `skills_kit_lib.human_html`, and the
sibling `discover_human_html.py` (CK-1, CK-2). The sibling is itself
stdlib-plus-package-only, so the no-provisioning property holds transitively --
and sharing its walk is what stops the checker from disagreeing with the
generator about navigation targets or ordering.

One prohibition in PC-6 is deliberately NOT machine-checked: hand-written HTML
content. Authorship is not observable in the bytes. It is held instead by the
PC-1 marker plus `replace-generated` regeneration, which overwrite any hand
edit rather than detecting it.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from skills_kit_lib import human_html as hh  # noqa: E402

import discover_human_html as discover  # noqa: E402


FAIL = "FAIL"
INFO = "INFO"

# SZ-1 budgets: six minutes at the repository root, three minutes elsewhere, at
# 200 visible words per minute.
ROOT_WORD_BUDGET = 1200
WORD_BUDGET = 600
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*")
# SZ-1 lets a record's `instructions` override its page budget.
_BUDGET_OVERRIDE_RE = re.compile(r"\bbudget\s*[:=]\s*(\d{2,5})\b", re.IGNORECASE)

# PC-6 / NF-1 prohibited forms.
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_NETWORK_API_RE = re.compile(r"\b(?:fetch|XMLHttpRequest)\b")
_STRING_LITERAL_RE = re.compile(r"\"([^\"\\\n]*)\"|'([^'\\\n]*)'|`([^`\\]*)`")
# A hostname heuristic, deliberately narrow: a `www.` prefix or a bare label
# followed by one of the TLDs an external asset realistically uses. A broad
# "looks like a dotted name" rule flags ordinary filenames such as `a.b.html`.
_HOSTNAME_RE = re.compile(
    r"(?:^|[^\w.-])(?:www\.[\w-]+"
    r"|[\w-]+\.(?:com|net|org|io|dev|ai|co|edu|gov|app|sh|me|cdn|cloud)(?:$|[/:?#]))",
    re.IGNORECASE,
)
_ID_ATTR_RE = re.compile(r"""\bid\s*=\s*["']([^"']+)["']""")

# Elements NF-1 allows to carry a cross-file read.
_CARRIERS = {"a": "href", "iframe": "src", "script": "src", "img": "src"}

_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

CHROME_ATTR = "data-human-html-chrome"
CHROME_NAV = "nav"
STYLE_ATTR = "data-human-html-style"


@dataclass
class Finding:
    """One check result. `level` is FAIL or INFO; `code` is stable for tests."""

    level: str
    code: str
    directory: str
    message: str
    file: str | None = None

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "code": self.code,
            "directory": self.directory,
            "file": self.file,
            "message": self.message,
        }


@dataclass
class ParsedNavLink:
    """One PC-2 navigation link and its two required text levels."""

    href: str
    in_list_item: bool
    label_parts: list[str] = field(default_factory=list)
    identity_parts: list[str] = field(default_factory=list)
    label_nodes: int = 0
    identity_nodes: int = 0

    @property
    def label(self) -> str:
        return " ".join("".join(self.label_parts).split())

    @property
    def identity(self) -> str:
        return " ".join("".join(self.identity_parts).split())


@dataclass
class ParsedPage:
    """The subset of one HTML file's structure the contract is judged against."""

    doctype: str | None = None
    html_attrs: dict = field(default_factory=dict)
    metas: list = field(default_factory=list)
    ids: set = field(default_factory=set)
    urls: list = field(default_factory=list)      # (tag, attr, value)
    scripts: list = field(default_factory=list)   # inline script text
    styles: list = field(default_factory=list)    # (attrs, text)
    nav_regions: int = 0
    nav_links: list = field(default_factory=list)
    nav_lists: int = 0
    nav_list_items: int = 0
    nav_item_link_counts: list[int] = field(default_factory=list)
    nav_items: list[ParsedNavLink] = field(default_factory=list)
    visible_words: int = 0


class _PageParser(HTMLParser):
    """Collect the structural facts CK-1 judges, in one pass.

    Two stacks matter. `_open` tracks non-void elements so a `data-human-html-chrome`
    subtree can be excluded from the SZ-1 word count (SZ-1 excludes chrome, and
    `script`, `style` and `template` besides). `_chrome_depth` counts how deep
    inside such a subtree the parser sits at any moment, so nesting cannot leak
    chrome text into the count.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.page = ParsedPage()
        self._open: list[str] = []
        self._chrome_depth = 0
        self._nav_depth = 0
        self._nav_list_depth = 0
        self._nav_item_stack: list[int] = []
        self._nav_anchor: ParsedNavLink | None = None
        self._nav_text_role: str | None = None
        self._skip_depth = 0
        self._capture: str | None = None
        self._buffer: list[str] = []
        self._capture_attrs: dict = {}

    # -- structure ---------------------------------------------------------
    def handle_decl(self, decl: str) -> None:
        if decl.lower().startswith("doctype"):
            self.page.doctype = decl

    def handle_starttag(self, tag: str, attrs) -> None:
        mapping = {name: (value if value is not None else "") for name, value in attrs}
        if "id" in mapping:
            self.page.ids.add(mapping["id"])
        if tag == "html":
            self.page.html_attrs = mapping
        if tag == "meta":
            self.page.metas.append(mapping)
        carrier = _CARRIERS.get(tag)
        for attr in ("href", "src"):
            if attr in mapping:
                self.page.urls.append((tag, attr, mapping[attr]))
                if self._nav_depth and tag == "a" and carrier == attr:
                    self.page.nav_links.append(mapping[attr])

        if self._nav_depth and tag == "ul":
            self.page.nav_lists += 1
            self._nav_list_depth += 1
        if self._nav_depth and tag == "li":
            self.page.nav_list_items += 1
            self.page.nav_item_link_counts.append(0)
            self._nav_item_stack.append(len(self.page.nav_item_link_counts) - 1)
        if self._nav_depth and tag == "a":
            in_list_item = bool(self._nav_list_depth and self._nav_item_stack)
            if in_list_item:
                item_index = self._nav_item_stack[-1]
                self.page.nav_item_link_counts[item_index] += 1
            self._nav_anchor = ParsedNavLink(
                href=mapping.get("href", ""),
                in_list_item=in_list_item,
            )
            self.page.nav_items.append(self._nav_anchor)
        if self._nav_anchor is not None and tag == "span":
            classes = set(mapping.get("class", "").split())
            if "hh-nav-label" in classes:
                self._nav_anchor.label_nodes += 1
                self._nav_text_role = "label"
            elif "hh-nav-identity" in classes:
                self._nav_anchor.identity_nodes += 1
                self._nav_text_role = "identity"

        is_chrome = CHROME_ATTR in mapping
        is_nav = is_chrome and mapping[CHROME_ATTR] == CHROME_NAV
        if is_nav:
            self.page.nav_regions += 1

        if tag in _VOID_TAGS:
            return

        self._open.append(tag)
        if is_chrome or self._chrome_depth:
            self._chrome_depth += 1
        if is_nav or self._nav_depth:
            self._nav_depth += 1
        if tag in ("script", "style", "template") or self._skip_depth:
            self._skip_depth += 1
        if tag in ("script", "style") and self._capture is None:
            self._capture = tag
            self._capture_attrs = mapping
            self._buffer = []

    def handle_startendtag(self, tag: str, attrs) -> None:
        mapping = {name: (value if value is not None else "") for name, value in attrs}
        if "id" in mapping:
            self.page.ids.add(mapping["id"])
        if tag == "meta":
            self.page.metas.append(mapping)
        if CHROME_ATTR in mapping and mapping[CHROME_ATTR] == CHROME_NAV:
            self.page.nav_regions += 1
        for attr in ("href", "src"):
            if attr in mapping:
                self.page.urls.append((tag, attr, mapping[attr]))

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS or tag not in self._open:
            return
        if self._nav_anchor is not None and tag == "span":
            self._nav_text_role = None
        if self._nav_anchor is not None and tag == "a":
            self._nav_anchor = None
            self._nav_text_role = None
        if self._nav_depth and tag == "li" and self._nav_item_stack:
            self._nav_item_stack.pop()
        if self._nav_depth and tag == "ul" and self._nav_list_depth:
            self._nav_list_depth -= 1
        while self._open:
            open_tag = self._open.pop()
            if self._chrome_depth:
                self._chrome_depth -= 1
            if self._nav_depth:
                self._nav_depth -= 1
            if self._skip_depth:
                self._skip_depth -= 1
            if open_tag == self._capture:
                text = "".join(self._buffer)
                if open_tag == "script":
                    if "src" not in self._capture_attrs:
                        self.page.scripts.append(text)
                else:
                    self.page.styles.append((self._capture_attrs, text))
                self._capture = None
                self._capture_attrs = {}
                self._buffer = []
            if open_tag == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._buffer.append(data)
            return
        if self._nav_anchor is not None and self._nav_text_role == "label":
            self._nav_anchor.label_parts.append(data)
        elif self._nav_anchor is not None and self._nav_text_role == "identity":
            self._nav_anchor.identity_parts.append(data)
        if self._skip_depth or self._chrome_depth:
            return
        self.page.visible_words += len(_WORD_RE.findall(data))


def parse_page(text: str) -> ParsedPage:
    parser = _PageParser()
    parser.feed(text)
    parser.close()
    return parser.page


# ---------------------------------------------------------------------------
# URL and script checks (PC-6, NF-1)
# ---------------------------------------------------------------------------

def _ids_of(path: Path) -> set:
    try:
        return set(_ID_ATTR_RE.findall(path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return set()


def check_urls(
    repo_root: Path,
    file_path: Path,
    page: ParsedPage,
    directory: str,
    add,
) -> None:
    """Apply NF-1 and the PC-6 URL prohibitions to every href and src."""
    rel = file_path.relative_to(repo_root).as_posix()
    for tag, attr, value in page.urls:
        where = "%s[%s]=%r" % (tag, attr, value)
        raw = value.strip()
        if not raw:
            add(FAIL, "url-unresolvable", directory, "%s: empty %s" % (where, attr), rel)
            continue
        if raw.startswith("//"):
            add(FAIL, "url-not-relative", directory,
                "%s: a protocol-relative URL is prohibited (NF-1)" % where, rel)
            continue
        if _SCHEME_RE.match(raw):
            add(FAIL, "url-not-relative", directory,
                "%s: a URL scheme is prohibited (PC-6)" % where, rel)
            continue
        if raw.startswith("/"):
            add(FAIL, "url-not-relative", directory,
                "%s: an absolute path is prohibited (PC-6)" % where, rel)
            continue
        if _DRIVE_RE.match(raw) or raw.startswith("\\\\"):
            add(FAIL, "url-not-relative", directory,
                "%s: a drive or UNC path is prohibited (PC-6)" % where, rel)
            continue
        if tag in _CARRIERS and _CARRIERS[tag] != attr:
            add(FAIL, "url-carrier", directory,
                "%s: NF-1 allows %s only on %s" % (where, attr, _CARRIERS[tag]), rel)
            continue
        if tag not in _CARRIERS:
            add(FAIL, "url-carrier", directory,
                "%s: NF-1 allows a cross-file read only on a[href], iframe[src], "
                "script[src] and img[src]" % where, rel)
            continue

        target, _, fragment = raw.partition("#")
        if not target:
            if fragment and fragment not in page.ids:
                add(FAIL, "url-unresolvable", directory,
                    "%s: same-document fragment #%s has no matching id" % (where, fragment), rel)
            continue
        resolved = (file_path.parent / target).resolve()
        try:
            resolved.relative_to(repo_root)
        except ValueError:
            add(FAIL, "url-escapes-repository", directory,
                "%s: resolves outside the repository (%s)" % (where, resolved), rel)
            continue
        if not resolved.exists():
            add(FAIL, "url-unresolvable", directory,
                "%s: does not resolve from %s" % (where, rel), rel)
            continue
        if fragment and resolved.is_file() and resolved.suffix == ".html":
            if fragment not in _ids_of(resolved):
                add(FAIL, "url-unresolvable", directory,
                    "%s: fragment #%s has no matching id in %s"
                    % (where, fragment, resolved.name), rel)


def check_scripts(page: ParsedPage, directory: str, rel: str, add) -> None:
    """Apply the PC-6 and NF-1 prohibitions to inline script content."""
    for text in page.scripts:
        found = _NETWORK_API_RE.search(text)
        if found:
            add(FAIL, "script-network-api", directory,
                "inline script uses %s, which NF-1 prohibits" % found.group(0), rel)
        for match in _STRING_LITERAL_RE.finditer(text):
            literal = next(group for group in match.groups() if group is not None)
            value = literal.strip()
            if not value:
                continue
            if "://" in value or value.startswith("//"):
                add(FAIL, "script-absolute-reference", directory,
                    "inline script literal %r carries a URL scheme or protocol-relative "
                    "URL" % literal, rel)
            elif value.startswith("/"):
                add(FAIL, "script-absolute-reference", directory,
                    "inline script literal %r is an absolute path" % literal, rel)
            elif _DRIVE_RE.match(value) or value.startswith("\\\\"):
                add(FAIL, "script-absolute-reference", directory,
                    "inline script literal %r is a drive or UNC path" % literal, rel)
            elif _HOSTNAME_RE.search(value):
                add(FAIL, "script-absolute-reference", directory,
                    "inline script literal %r names a host" % literal, rel)


# ---------------------------------------------------------------------------
# One HTML file
# ---------------------------------------------------------------------------

def _relative_link(from_directory: str, to_directory: str) -> str:
    """The relative href from one directory's page to another directory's page."""
    from_parts = [] if from_directory == hh.ROOT_DIRECTORY else from_directory.split("/")
    to_parts = [] if to_directory == hh.ROOT_DIRECTORY else to_directory.split("/")
    common = 0
    while common < min(len(from_parts), len(to_parts)) and from_parts[common] == to_parts[common]:
        common += 1
    steps = [".."] * (len(from_parts) - common) + to_parts[common:]
    return "/".join(steps + [hh.PAGE_FILENAME])


def check_html_file(
    repo_root: Path,
    directory: str,
    record: hh.Record,
    file_path: Path,
    kind: str,
    slug: str | None,
    expected_nav: list[tuple[str, str, str]],
    add,
) -> None:
    """Check one generated file against PC-1 to PC-4, PC-6, NF-1, RD-2 and SZ-1."""
    rel = file_path.relative_to(repo_root).as_posix()
    raw = file_path.read_bytes()
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        add(FAIL, "non-ascii", directory,
            "contains non-ASCII bytes at offset %d (PC-6)" % exc.start, rel)
        text = raw.decode("utf-8", errors="replace")

    # PC-1 / RD-2 marker.
    try:
        payload = hh.parse_marker(text)
    except hh.MarkerError as exc:
        add(FAIL, "marker", directory, str(exc), rel)
        payload = None
    if payload is not None:
        if payload["source_sha"] != record.source_sha:
            add(FAIL, "marker-inconsistent", directory,
                "marker source_sha %s does not match the record's %s"
                % (payload["source_sha"], record.source_sha), rel)
        if payload["directory"] != record.directory:
            add(FAIL, "marker-inconsistent", directory,
                "marker directory %r does not match the record's %r"
                % (payload["directory"], record.directory), rel)
        if payload["kind"] != kind:
            add(FAIL, "marker-inconsistent", directory,
                "marker kind %r does not match the file (expected %r)"
                % (payload["kind"], kind), rel)
        elif kind == hh.KIND_REFERENCE and payload.get("reference") != slug:
            add(FAIL, "marker-inconsistent", directory,
                "marker reference %r does not match the filename slug %r"
                % (payload.get("reference"), slug), rel)

    page = parse_page(text)

    # PC-1 metadata.
    if page.doctype is None or page.doctype.strip().lower() != "doctype html":
        add(FAIL, "metadata", directory, "missing `<!doctype html>` (PC-1)", rel)
    if page.html_attrs.get("lang") != "en":
        add(FAIL, "metadata", directory, 'missing `<html lang="en">` (PC-1)', rel)
    if not any(meta.get("charset", "").lower() == "utf-8" for meta in page.metas):
        add(FAIL, "metadata", directory, "missing a UTF-8 charset meta (PC-1)", rel)
    if not any(meta.get("name") == "viewport" for meta in page.metas):
        add(FAIL, "metadata", directory, "missing a responsive viewport meta (PC-1)", rel)
    if not any(
        meta.get("name") == "color-scheme" and meta.get("content") == "dark"
        for meta in page.metas
    ):
        add(FAIL, "metadata", directory,
            'missing `<meta name="color-scheme" content="dark">` (PC-1)', rel)

    # PC-4 inline style.
    styled = [text_ for attrs, text_ in page.styles if STYLE_ATTR in attrs]
    if not styled:
        add(FAIL, "style", directory,
            "missing the `<style %s>` element carrying the SA-1 asset (PC-4)" % STYLE_ATTR, rel)
    elif len(styled) > 1:
        add(FAIL, "style", directory,
            "%d `<style %s>` elements found, exactly one is allowed (PC-4)"
            % (len(styled), STYLE_ATTR), rel)
    elif styled[0].strip() != hh.asset_css().strip():
        add(FAIL, "style", directory,
            "the inline style is not the SA-1 packaged asset (PC-4)", rel)
    if any(
        tag == "link" and attr == "href"
        for tag, attr, _value in page.urls
    ):
        add(FAIL, "style", directory,
            "a linked stylesheet is prohibited; PC-4 requires the asset inline", rel)

    # PC-3 announce.
    expected_announce = hh.announce_script(record, file_path.name, kind, slug)
    if not any(expected_announce in script for script in page.scripts):
        add(FAIL, "announce", directory,
            "missing the PC-3 announce snippet for %s" % file_path.name, rel)

    # PC-2 / RD-2 navigation.
    if page.nav_regions != 1:
        add(FAIL, "navigation", directory,
            '%d regions marked `%s="%s"` found, exactly one is required'
            % (page.nav_regions, CHROME_ATTR, CHROME_NAV), rel)
    else:
        found = [link.strip() for link in page.nav_links]
        wanted = [href for href, _label, _identity in expected_nav]
        if sorted(found) != sorted(wanted):
            add(FAIL, "navigation-mismatch", directory,
                "navigation links %s do not match the computed spine %s"
                % (sorted(found), sorted(wanted)), rel)
        if (
            page.nav_lists != 1
            or page.nav_list_items != len(page.nav_items)
            or any(count != 1 for count in page.nav_item_link_counts)
            or any(not item.in_list_item for item in page.nav_items)
        ):
            add(FAIL, "navigation-structure", directory,
                "PC-2 requires one list with one navigation link per list item", rel)
        expected_by_href = {
            href: (label, identity) for href, label, identity in expected_nav
        }
        for item in page.nav_items:
            expected = expected_by_href.get(item.href.strip())
            if expected is None:
                continue
            label, identity = expected
            if (
                item.label_nodes != 1
                or item.identity_nodes != 1
                or item.label != label
                or item.identity != identity
            ):
                add(FAIL, "navigation-structure", directory,
                    "navigation link %r must contain label %r and the target identity %r"
                    % (item.href, label, identity), rel)

    check_urls(repo_root, file_path, page, directory, add)
    check_scripts(page, directory, rel, add)

    # SZ-1 size signal.
    budget = ROOT_WORD_BUDGET if (
        kind == hh.KIND_PAGE and directory == hh.ROOT_DIRECTORY
    ) else WORD_BUDGET
    override = _BUDGET_OVERRIDE_RE.search(record.instructions or "")
    if override:
        budget = int(override.group(1))
    if page.visible_words > budget:
        add(INFO, "size", directory,
            "%d visible words exceeds the %d-word budget (SZ-1)"
            % (page.visible_words, budget), rel)


# ---------------------------------------------------------------------------
# One directory
# ---------------------------------------------------------------------------

def check_directory(repo_root: Path, entry: dict, add) -> None:
    """Check one discovered directory: its record, then every generated file."""
    directory = entry["directory"]
    status = entry["record"]["status"]
    page_file = entry["page_file"]
    reference_files = entry["reference_files"]

    if status == discover.RECORD_STATUS_MISSING:
        add(FAIL, "record-missing", directory,
            "no decision record at %s (DR-1)" % entry["record"]["path"])
        return
    if status == discover.RECORD_STATUS_INVALID:
        add(FAIL, "record-invalid", directory,
            "%s: %s" % (entry["record"]["path"], entry["record"]["error"]))
        return

    record = hh.load_record(repo_root / entry["record"]["path"])

    if status == discover.RECORD_STATUS_STALE:
        add(INFO, "STALE", directory,
            "recorded source_sha %s but DR-2 recomputes %s"
            % (record.source_sha, entry["source_sha"]))
    elif entry["stale_child"]:
        add(INFO, "STALE", directory,
            "a descendant is stale or missing, which propagates under TS-2: %s"
            % ", ".join(entry["stale_children"]))
    if record.dirty:
        add(INFO, "DIRTY", directory,
            "the record carries dirty: true, so no commit identifies the judged content (DR-2)")

    if record.decision == hh.DECISION_NONE:
        for name in ([page_file] if page_file else []) + reference_files:
            add(FAIL, "page-for-none", directory,
                "%s exists but the decision is `none` (CK-1)" % name, _rel(directory, name))
        return

    if not page_file:
        add(FAIL, "page-missing", directory,
            "the decision is `page` but %s does not exist (PC-1)" % hh.PAGE_FILENAME)
        return

    listed = {ref.file: ref for ref in record.references}
    for name in sorted(set(listed) - set(reference_files)):
        add(FAIL, "reference-file-missing", directory,
            "the record lists %s but the file does not exist (RD-1)" % name)
    for name in sorted(set(reference_files) - set(listed)):
        add(FAIL, "reference-unlisted", directory,
            "%s exists but the record does not list it (RD-1)" % name, _rel(directory, name))

    up = entry["nearest_page_ancestor"]
    down = entry["nearest_page_descendants"]
    targets = ([up] if up else []) + down
    expected_nav = [
        (
            _relative_link(directory, target),
            hh.navigation_label(target),
            hh.load_record(hh.record_path(repo_root, target)).identity,
        )
        for target in targets
    ]

    page_path = repo_root / _rel(directory, page_file)
    check_html_file(
        repo_root, directory, record, page_path, hh.KIND_PAGE, None, expected_nav, add,
    )

    page_text = page_path.read_text(encoding="utf-8", errors="replace")
    page_links = {value.strip() for _tag, attr, value in parse_page(page_text).urls if attr == "href"}
    for name in sorted(set(listed) & set(reference_files)):
        reference = listed[name]
        if name not in page_links:
            add(FAIL, "reference-not-linked", directory,
                "%s does not link its reference %s (RD-1)" % (hh.PAGE_FILENAME, name),
                _rel(directory, hh.PAGE_FILENAME))
        check_html_file(
            repo_root, directory, record, repo_root / _rel(directory, name),
            hh.KIND_REFERENCE,
            reference.slug,
            [(hh.PAGE_FILENAME, hh.navigation_label(directory), record.identity)],
            add,
        )


def _rel(directory: str, name: str) -> str:
    return name if directory == hh.ROOT_DIRECTORY else "%s/%s" % (directory, name)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def orphaned_output(repo_root: Path) -> dict:
    """Return generated files sitting in directories discovery does not emit.

    CK-2 excludes a directory that holds only generated output, which is correct
    for the walk -- there is no analysis input there, so no DR-2 stamp and no
    decision record can ever describe it. It leaves a hole in CK-1 that has to be
    closed HERE rather than by widening the walk: a `human.html` whose directory
    lost its last input, or one written into a directory that never had one, is
    invisible to the subject loop and would otherwise be reported as a clean run.

    That page is not merely unchecked -- it is unmaintainable by construction. It
    can never go fresh, so it is a FAIL rather than a `STALE` signal, and the
    remedy is to delete it or to look at why its inputs are gone.
    """
    files = discover.repository_files(repo_root)
    subjects = set(discover.subject_directories(files))
    orphans: dict[str, list[str]] = {}
    record_root = hh.RECORD_ROOT.split("/")[0]
    for rel in files:
        parts = rel.split("/")
        if record_root in parts or not discover.is_generated_name(parts[-1]):
            continue
        directory = "/".join(parts[:-1]) or hh.ROOT_DIRECTORY
        if directory in subjects:
            continue
        orphans.setdefault(directory, []).append(parts[-1])
    return {directory: sorted(names) for directory, names in orphans.items()}


def check(repo_root: str | Path, directory: str | Path = hh.ROOT_DIRECTORY) -> dict:
    """Run every CK-1 check and return the machine-readable result."""
    root_path = Path(repo_root).resolve()
    scope = hh.normalize_directory(directory)
    result = discover.scan(root_path, scope)

    findings: list[Finding] = []

    def add(level, code, dir_, message, file=None):
        findings.append(Finding(level, code, dir_, message, file))

    explicit = scope != hh.ROOT_DIRECTORY
    checked = []
    for entry in result["directories"]:
        subject = (
            entry["record"]["status"] != discover.RECORD_STATUS_MISSING
            or entry["page_file"]
            or entry["reference_files"]
            or (explicit and entry["directory"] == scope)
        )
        if not subject:
            continue
        checked.append(entry["directory"])
        check_directory(root_path, entry, add)

    for orphan, names in sorted(orphaned_output(root_path).items()):
        if not discover.in_scope(orphan, scope):
            continue
        checked.append(orphan)
        has_record = hh.record_path(root_path, orphan).is_file()
        for name in names:
            add(FAIL, "page-orphaned", orphan,
                "%s exists in a directory with no analysis input, so %s (CK-2 omits "
                "the directory, and DR-2 can compute no source stamp for it) -- delete "
                "the file, or restore the inputs the page describes"
                % (name,
                   "its decision record cannot be kept fresh" if has_record
                   else "no decision record can describe it"),
                _rel(orphan, name))

    fails = [f for f in findings if f.level == FAIL]
    return {
        "repo_root": str(root_path),
        "scope": scope,
        "checked": checked,
        "findings": [f.to_dict() for f in findings],
        "fail_count": len(fails),
        "info_count": len(findings) - len(fails),
        "ok": not fails,
    }


def render(result: dict) -> str:
    lines = []
    for finding in result["findings"]:
        location = finding["file"] or finding["directory"]
        lines.append(
            "%s %s %s: %s" % (finding["level"], finding["code"], location, finding["message"])
        )
    lines.append(
        "checked %d director%s: %d FAIL, %d INFO"
        % (
            len(result["checked"]),
            "y" if len(result["checked"]) == 1 else "ies",
            result["fail_count"],
            result["info_count"],
        )
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check generated human HTML against its contract (CK-1).",
    )
    parser.add_argument("repo_root", help="repository root to check")
    parser.add_argument(
        "directory",
        nargs="?",
        default=hh.ROOT_DIRECTORY,
        help="optional repository-relative directory to check instead of the whole tree",
    )
    parser.add_argument("--json", action="store_true", help="emit the result as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = check(args.repo_root, args.directory)
    except (discover.DiscoveryError, hh.HumanHtmlError) as exc:
        print("human_html_check: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=True) if args.json else render(result))
    return 1 if result["fail_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
