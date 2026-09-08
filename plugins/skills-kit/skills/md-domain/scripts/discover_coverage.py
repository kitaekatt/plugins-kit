#!/usr/bin/env python3
"""discover_coverage.py -- enumerate coverage subjects: (one directory's own code
files, its ambient CLAUDE.md chain).

Usage:
    python discover_coverage.py <directory>
    python discover_coverage.py <directory> --json
    python discover_coverage.py --diff [<git-range>]

The coverage verb's subject is ONE DIRECTORY'S OWN DIRECT CODE FILES plus the
CLAUDE.md files that actually LOAD for it -- never a subtree, and never a
markdown file. This script is the mechanical half: it resolves the subject set
and applies the structural exclusions, and it decides nothing about what the
code means. No whole-repo default: a directory must be named, or --diff must
derive the roots from changed files.

Three properties are load-bearing and easy to get wrong:

  * THE CODE-FILE SET IS NON-RECURSIVE. A child directory is its OWN subject,
    not part of this one. Writing a CLAUDE.md for D requires no look into D's
    subdirectories' code: the composition step reads D's direct code PLUS every
    child CLAUDE.md, so the facts of a descendant reach D through that
    descendant's finished document rather than by being re-derived here. A
    recursive subject made every ancestor re-report its descendants' facts
    against copies of themselves (`<dir>` with 4 direct files reported 125), and
    that is the defect this shape exists to remove.

  * The ambient chain INCLUDES a CLAUDE.md at the directory itself. The document
    lanes' resolver starts at the target's PARENT, because the target is the
    CLAUDE.md. Here the target is a directory, and a CLAUDE.md sitting in it is
    the most ambient file there is. The chain still walks UPWARD -- only the
    code-file set stopped recursing.

  * The upward walk stops at the nearest .git. A nested repository's ambient
    chain is its own, never the outer repository's -- so a vendored or submodule
    directory does not silently inherit ancestors that never load for it.

Exclusions are STRUCTURAL only and are applied before any file is read: what the
PROJECT'S VCS IGNORES (git or p4 ignore rules, and nothing at all when the tree
is under neither -- see vcs_ignore.py), vendored and third-party directories
(by case-insensitive NAME, and by the CONTENT signal of a directory whose direct
code files are mostly minified/bundled web assets), generated directories, and
nested repositories. Deciding that a fact is "already
covered by an ambient claim that resolves" is NOT an exclusion -- establishing it
requires reading the ambient document and usually the source it anchors to, so it
belongs to the assessment step, not here.

Every exclusion is RECORDED and reported, never silently applied. A NAME rule
prunes at directory level, so first-party build glue parked under a
vendored-named parent goes with it and its children are never enumerated -- a
known, accepted false-positive class, described at VENDOR_DIR_NAMES.

This module also hosts `walk_tree`, a RECURSIVE tree-descent primitive used by
discover_composition.py. It is never used to build a coverage subject --
`walk_directory` above stays the non-recursive unit this module's own subject
is defined over. `walk_tree` lives here rather than in its consumer because it
descends using the same structural
exclusions this module owns (`_skip_reason`, `NOISE_DIR_NAMES`,
`SKIP_NESTED_REPO`, `MAX_DEPTH`), the same precedent `MAX_DEPTH` already set
before this move: one home for a shared constant or primitive, so the verbs
that use it cannot disagree about what it means.

A file whose extension is recognized as neither code, doc, nor a common asset
type is also RECORDED, never dropped with no trace: it is counted (aggregated
by extension) into `unknownExtensions`. Silently dropping such a file made a
whole subtree of an unrecognized language read as an EMPTY, well-formed
subject -- the strongest verdict, over a subtree that was never read at all.

Stdlib-only.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

# The shared extension set and project-root walk live alongside the CLAUDE.md
# discover script; import rather than copy so the two cannot drift.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from discover_claude_md import CODE_DATA_EXT, _MD_LIKE  # noqa: E402
# The ambient chain stops at the PROJECT root, which is not the same question as
# the claude-md audit lane's git boundary: a Perforce workspace has no .git
# anywhere, so a git-only walk would report "no project" for every directory in
# it and truncate the chain to the subject's own CLAUDE.md (or to nothing).
from project_root import find_project_root  # noqa: E402
from vcs_ignore import detect_vcs, ignored_paths  # noqa: E402

# Directory basenames that are vendored / third-party or build output. Matched
# against a single path component, CASE-INSENSITIVELY (see `_skip_reason`), so
# one lowercase entry covers every casing convention a project might use:
# `third_party`, `ThirdParty` (the Unreal convention), and `THIRDPARTY` are one
# rule, not three. Listing both `build` and `Build` here used to be the
# hand-rolled version of that fold; a case-sensitive list silently missed every
# casing nobody had thought to enumerate, and ~342 vendored `ThirdParty`
# directories arrived as real coverage subjects because of it. `build` and
# `target` are ambiguous (a real source dir may be named either), which is
# exactly why a skip is reported rather than assumed correct -- the user can see
# it and re-scope. `node_modules` and `bower_components` are package-manager
# INSTALL ROOTS: nothing hand-authored ever lives in one, so they carry no
# false-positive risk at all.
#
# KNOWN FALSE-POSITIVE CLASS, accepted deliberately: a path-segment name rule
# cannot tell a vendored library from FIRST-PARTY BUILD GLUE parked beside it
# under the same vendored-named parent. The prune is at DIRECTORY level, so the
# children of a matched directory are never enumerated and never reported
# individually. The shape to recognize is a directory whose only direct files are
# team-authored build scripts (`Foo.Build.cs`, provisioning scripts) sitting
# under `ThirdParty/`, while the upstream source is a SIBLING subtree the same
# rule excludes correctly. Reinstating such a directory is the consuming repo's
# call -- name it explicitly, or move it out from under the vendored parent; this
# module carries no first-party override. Pinned by
# test_thirdparty_prune_also_drops_first_party_build_glue.
VENDOR_DIR_NAMES = {
    "node_modules", "bower_components", "vendor", "third_party", "thirdparty",
    "pods", "target", "dist", "build", "out",
}

# Directory basenames whose contents are generated. An existing modality already
# covers generated content, so it is not this verb's subject. Matched
# case-insensitively, like VENDOR_DIR_NAMES -- `Generated/` is at least as
# common a convention as `generated/`.
GENERATED_DIR_NAMES = {"generated", "__generated__", "gen", "autogen"}

# Lowercased lookups for the two name sets above. Derived once rather than
# lowered per candidate, and kept private so the published constants stay
# readable in their conventional casing.
_VENDOR_DIR_NAMES_LOWER = {name.lower() for name in VENDOR_DIR_NAMES}
_GENERATED_DIR_NAMES_LOWER = {name.lower() for name in GENERATED_DIR_NAMES}

# Filename suffixes that mark a MINIFIED or BUNDLED web asset -- build output of
# some other project, checked in as-is. Matched case-insensitively against a
# file NAME, not an extension, because the signal is the `.min`/`.bundle`
# infix rather than `.js` itself.
MINIFIED_BUNDLE_SUFFIXES = (
    ".min.js", ".min.mjs", ".min.css", "-min.js", "-min.css",
    ".bundle.js", ".bundle.css",
)

# Noise directories: pruned and counted, but not itemized in the report. These
# are never anybody's source of insight and listing them would bury the skips
# that matter.
NOISE_DIR_NAMES = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", "Intermediate", "Saved", "Binaries",
    "DerivedDataCache", ".idea", ".vs",
}

# Extensions that are never code: images, fonts, audio, video, archives,
# compiled binaries/lockfiles, and engine asset containers. Excluded from
# `unknownExtensions` so that signal stays readable -- a 105-PNG directory
# should not drown out the one `.gd` file CODE_DATA_EXT is missing.
#
# A MISSING entry here is still the SAFE DIRECTION: it can only cause noise,
# never a missed subject, because a real code extension is never added here.
# But THE COST OF THAT NOISE SCALES WITH THE CONSUMING PROJECT, and on an engine
# project it is not cosmetic. A directory whose files are all unrecognized
# reports codeFiles: [] with a non-empty unknownExtensions, which the coverage
# lane must treat as a discovery FAILURE rather than as an empty subject -- so
# every one of them becomes a subject an agent is spent on. Measured on a real
# Unreal project: `.uasset` alone (30,593 files) manufactured 1,318 empty
# subjects out of 3,071 -- roughly half the corpus, each a full agent reading
# binary it cannot parse. ONE missing binary extension bought four figures of
# phantom subjects. Adding an entry here is cheap; leaving one out is not.
#
# Add only formats that are genuinely UNREADABLE. Verify with `od -c` before
# assuming: `.afb` and `.collection` look binary from the name and are plain
# text, so they belong in unknownExtensions where a human can see them.
ASSET_BINARY_EXT = {
    # images
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp",
    ".tiff", ".tga", ".psd", ".heic", ".avif",
    # fonts
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # audio
    ".wav", ".mp3", ".ogg", ".flac", ".aac", ".m4a",
    # video
    ".mp4", ".mov", ".avi", ".webm", ".mkv",
    # archives
    ".zip", ".tar", ".gz", ".7z", ".rar", ".bz2", ".xz",
    # compiled binaries / build byproducts / lockfiles
    ".exe", ".dll", ".so", ".dylib", ".pdb", ".pyc", ".pyd", ".class",
    ".o", ".obj", ".a", ".lib", ".lock",
    # Unreal Engine asset containers: serialized binary, not a source language.
    # `.uasset`/`.umap` are the editor's on-disk assets; the rest are cooked or
    # packaged output. `.uplugin`/`.uproject` are deliberately NOT here -- they
    # are JSON descriptors and live in CODE_DATA_EXT instead.
    ".uasset", ".umap", ".upk", ".uexp", ".ubulk", ".ucas", ".utoc", ".pak",
    # Compiled localization output. `.locres` is the BINARY artifact; its plain
    # text SOURCES (`.po`, `.xlf`) stay readable and must never be added here --
    # a localization pipeline works on exactly those two.
    ".locres",
    # Other binary containers, each verified by header on a real project:
    # `.scmap` is a FlatBuffers setpiece companion (a designer skill parses it
    # through a dedicated reader, which is what structured binary requires),
    # `.bk2` is Bink video (magic "KB2n"), `.vox` is MagicaVoxel (magic
    # "VOX "), `.hxd` is a binary font-editor working file, `.pkf` is an audio
    # peak/waveform cache (magic "k$!").
    ".scmap", ".bk2", ".vox", ".hxd", ".pkf",
    # DCC formats, PROMOTED out of the unverified inventory below because they
    # are present on a real project and were checked there: `.fbx` opens
    # "Kaydara FBX Binary", `.blend` opens "BLENDER-v293". The rest of the DCC
    # list stays unverified. NOTE the FBX ASCII variant uses the same extension
    # and IS text; absorbing it is the accepted cost, since an FBX of either
    # flavour is a mesh export rather than a coverage subject.
    ".fbx", ".blend",
}

# REPORTED BUT UNVERIFIED on a real project of that type -- deliberately NOT in
# the set above. A wrong entry here is a MISSED SUBJECT, the unsafe direction, so
# these are recorded for the next maintainer who has such a repo to check them
# against, rather than guessed at on their behalf. Apply the `od -c` rule before
# promoting any of them.
#   Unity:    .unity .prefab .asset .mat .anim .controller .meta -- text under
#             "Force Text" serialization and binary otherwise, so the project's
#             mode decides and this one genuinely cannot be settled from here.
#   Godot:    .res .scn .import .ctex
#   DCC / 3D: .max .ma .mb .dds .exr .hdr. (`.fbx` and `.blend` were on this
#             list and have been PROMOTED into the set above, having been
#             verified on a real project.) Note `.obj` is already in the set as
#             a compiled OBJECT file; on a 3D repo that entry is accidentally
#             right for the wrong reason.
#   Documents and packages: .pdf .docx .xlsx .pptx .sqlite .db3 .parquet .jar
#             .whl .apk .aab .ipa .nupkg .msi .dmg .iso
#
# PROPOSAL, not implemented -- recorded so it is discoverable rather than
# rediscovered. A BINARY SNIFF (a NUL byte in the first 8 KB) as a fallback
# before a file is counted into `unknownExtensions` would subsume this entire
# constant: it catches every container above with no list at all, and it
# correctly leaves `.afb` and `.collection` visible, both of which look binary
# from the name and are plain text. What keeps it out of a constant edit is that
# it changes the DISCOVERY-FAILURE path -- the one mechanism here whose misfires
# are expensive in BOTH directions. Absorb a real source language and a whole
# subtree reads as an empty, well-formed subject (the strongest verdict, over
# something never read); fail to absorb and phantom subjects multiply. It also
# reads bytes, which this module otherwise never does. It deserves its own
# cross-check rather than riding along with a list edit.

# Extensionless files that are never code, matched on NAME. `Path(".gitignore")`
# and `Path("LICENSE")` both have an empty suffix, so without this every repo
# convention file would land in `unknownExtensions` under "" -- and a
# genuinely code-free directory holding only a LICENSE and a .gitignore would
# be reported as a discovery FAILURE rather than as the empty subject it is.
# Extensionless files NOT listed here (Makefile, Dockerfile, an extensionless
# shell script) stay counted: those plausibly are code, and the safe direction
# is to surface them.
EXTENSIONLESS_NON_CODE = {
    "license", "licence", "notice", "copying", "authors", "contributors",
    "changelog", "codeowners", "version", "readme", "patents", "owners",
}

# Depth ceiling for the RECURSIVE walk (`walk_tree`, below). `walk_directory`
# above is one level deep and never consults it; discover_composition.py
# imports `walk_tree` from here so the two callers cannot disagree about how
# deep a tree is read.
MAX_DEPTH = 12

# Skip reasons, reported verbatim.
SKIP_VENDORED = "vendored"
SKIP_VENDORED_BUNDLE = "vendored-bundle"
SKIP_GENERATED = "generated"
SKIP_SYMLINK_OUT = "symlink-outside-subtree"
SKIP_NESTED_REPO = "nested-repo"
SKIP_IGNORED = "vcs-ignored"


def is_code_file(path: Path) -> bool:
    """A file counts as code when its extension is in the shared code/data set."""
    return path.suffix.lower() in CODE_DATA_EXT


def ambient_chain(directory: Path) -> list[Path]:
    """Return the CLAUDE.md files ambient for `directory`, root-most first.

    Walks from `directory` itself upward to the PROJECT root, collecting
    CLAUDE.md at each level. The walk stops at the nearest project marker
    (.git/.hg/.svn/.p4config.txt -- see project_root.py) -- a nested project's
    chain is its own, and a Perforce workspace resolves like a git one. Returns
    an empty list when nothing covers the directory at all, which is the case the
    coverage verb most exists to surface.
    """
    project_root = find_project_root(directory)
    chain: list[Path] = []
    current = directory
    while True:
        candidate = current / "CLAUDE.md"
        if candidate.is_file():
            chain.append(candidate)
        if project_root is not None and current == project_root:
            break
        if current == current.parent:
            break
        # Outside any project, do not climb past the named directory's own tree.
        if project_root is None:
            break
        current = current.parent
    chain.reverse()
    return chain


def _is_minified_bundle(name: str) -> bool:
    """True when a FILE NAME looks like a minified or bundled web asset."""
    return name.lower().endswith(MINIFIED_BUNDLE_SUFFIXES)


def _vendored_bundle_dir(path: Path) -> bool:
    """True when `path`'s DIRECT code files are dominated by minified bundles.

    The name rules above cannot see a vendored web bundle dropped into a
    directory called `libraries`, `static`, or `js`: the name carries no
    signal and the broad name (`lib`, `libs`) is exactly the one that would
    also eat first-party source. So this rule asks for POSITIVE EVIDENCE
    instead -- a payload nobody hand-writes.

    "Dominated" means at least one minified/bundled file AND at least half of
    the directory's direct code files. The majority test is what keeps the rule
    off first-party code: a source directory that merely VENDORS one
    `jquery.min.js` alongside its own modules stays a subject, because its own
    modules still outnumber the bundle. Only a directory that is essentially
    nothing but other people's build output trips it.

    Directory listing only -- no file is read, no subprocess is run.
    """
    try:
        entries = list(path.iterdir())
    except OSError:
        return False
    code = [e for e in entries if e.is_file() and is_code_file(e)]
    if not code:
        return False
    minified = [e for e in code if _is_minified_bundle(e.name)]
    return bool(minified) and len(minified) * 2 >= len(code)


def is_nested_repo(path: Path) -> bool:
    """True when `path` carries its own `.git` -- a separate repository whose
    ambient chain is its own, never the outer repository's. One home for this
    test so `walk_directory`, `walk_tree`, and any other consumer (e.g.
    coverage_subjects.py's `--tree` walk) cannot disagree about what counts as
    a nested repository.
    """
    return (path / ".git").exists()


def _skip_reason(dir_name: str, path: Path | None = None) -> str | None:
    """Return the structural exclusion `dir_name` matches, or None.

    Name matching is CASE-INSENSITIVE: `ThirdParty` and `third_party` are the
    same rule. `path`, when given, additionally enables the content-based
    vendored-bundle rule; callers that only hold a path COMPONENT (diff_roots
    walks `parts`) pass the name alone and get the name rules only.
    """
    lowered = dir_name.lower()
    if lowered in _VENDOR_DIR_NAMES_LOWER:
        return SKIP_VENDORED
    if lowered in _GENERATED_DIR_NAMES_LOWER:
        return SKIP_GENERATED
    if path is not None and _vendored_bundle_dir(path):
        return SKIP_VENDORED_BUNDLE
    return None


def _child_disposition(entry: Path, ignored: set[Path]) -> str | None:
    """Classify one child directory entry for reporting: `"noise"` (a
    NOISE_DIR_NAMES member or a dot-directory other than `.claude` -- pruned
    and counted, never itemized), one of the `SKIP_*` reason strings, or None
    for an ordinary directory nobody needs telling about.

    The order is `walk_directory`'s original, cheapest-first order: the
    noise/dot-dir name check runs BEFORE `_skip_reason`, which is the only one
    of these checks that can read a directory's contents
    (`_vendored_bundle_dir`'s directory iteration). `walk_tree` used to run
    `_skip_reason` before its own dot-dir check, paying that content-read cost
    on every dot-directory (`.github` and friends) and -- for an entry
    matching more than one rule -- reporting a different disposition than
    `walk_directory` for the identical entry. Both callers are wired through
    this one function so they cannot drift apart again.
    """
    name = entry.name
    if name in NOISE_DIR_NAMES or (name.startswith(".") and name != ".claude"):
        return "noise"
    reason = _skip_reason(name, entry)
    if reason is None and is_nested_repo(entry):
        reason = SKIP_NESTED_REPO
    if reason is None and entry in ignored:
        reason = SKIP_IGNORED
    return reason


def walk_directory(
    root: Path, ignored: set[Path] | None = None
) -> tuple[list[Path], list[dict], int, dict[str, int]]:
    """Collect the code files DIRECTLY in `root`, recording every exclusion.

    NON-RECURSIVE, and that is the whole point (see the module docstring): a
    child directory is its own subject and contributes nothing here. Child
    directories are still INSPECTED, one level, so the report can say why one of
    them is not a subject anybody should assess -- a `node_modules`, a
    `generated`, a submodule, an ignored `tmp`. None of that can change
    `code_files` any more; it is reporting, not filtering.

    Returns (code_files, skipped, noise_pruned, unknown_extensions).
    `skipped` entries are {"path": <str>, "reason": <str>} for the exclusions a
    user should see; `noise_pruned` counts the ones deliberately not itemized.
    `unknown_extensions` is {<ext>: <count>} for every file whose extension is
    in neither CODE_DATA_EXT, `_MD_LIKE` (already accounted for as docs), nor
    ASSET_BINARY_EXT (never code) -- aggregated by extension, never itemized
    per file, so a directory of 105 PNGs does not become 105 report lines. A
    file counted here was NOT read as code and NOT recognized as a doc or
    asset: the directory was not fully accounted for, and that must never be
    mistaken for "nothing else is here" (see the coverage lane's refusal rule).

    `ignored` is the pre-computed set of entries the project's VCS ignores, as
    returned by vcs_ignore.ignored_paths -- passed in rather than queried per
    entry so one directory costs one subprocess. None means "no ignore
    information", which excludes nothing.

    Side-effect free: it stats and lists one directory, and reads nothing.
    """
    code_files: list[Path] = []
    skipped: list[dict] = []
    noise_pruned = 0
    unknown_extensions: dict[str, int] = {}
    ignored = ignored or set()
    root = root.resolve()

    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return code_files, skipped, noise_pruned, unknown_extensions

    for entry in entries:
        name = entry.name
        if entry.is_dir():
            # A child directory is a SEPARATE subject. Nothing under it is
            # collected here, so this only decides whether the child is worth
            # telling the user about.
            disposition = _child_disposition(entry, ignored)
            if disposition == "noise":
                noise_pruned += 1
            elif disposition is not None:
                skipped.append({"path": str(entry), "reason": disposition})
            continue
        if not entry.is_file():
            continue
        if is_code_file(entry):
            # An ignored file is out regardless of what is tracked: the question
            # is whether the project's ignore RULES cover it.
            if entry in ignored:
                skipped.append({"path": str(entry), "reason": SKIP_IGNORED})
            else:
                code_files.append(entry)
            continue
        ext = entry.suffix.lower()
        if ext in _MD_LIKE or ext in ASSET_BINARY_EXT:
            continue
        # A dotfile (.gitignore, .editorconfig) has an empty suffix and is
        # configuration, not an unrecognized language. So does a convention file
        # like LICENSE. Neither means the directory went unread, so neither may
        # trip the lane's discovery-failure rule.
        if not ext:
            if name.startswith(".") or name.lower() in EXTENSIONLESS_NON_CODE:
                continue
        if entry in ignored:
            # Unrecognized AND ignored: accounted for, so it must not trip the
            # lane's discovery-failure rule. Recorded rather than dropped.
            skipped.append({"path": str(entry), "reason": SKIP_IGNORED})
            continue
        unknown_extensions[ext] = unknown_extensions.get(ext, 0) + 1

    code_files.sort(key=str)
    return code_files, skipped, noise_pruned, unknown_extensions


def walk_tree(root: Path) -> tuple[list[Path], list[Path], list[dict], int]:
    """Return (leaves, claude_md_paths, skipped, noise_pruned) under `root`.

    RECURSIVE, unlike `walk_directory` above -- and that is deliberate rather
    than a relapse into the retired recursive-subject model. This primitive
    answers a different question than a coverage subject does: "does at least
    one code file exist at or beneath this directory", which the composition
    subject set in `discover_composition.py` needs and which is not itself a
    coverage subject. A
    coverage subject stays exactly `walk_directory`'s non-recursive unit; this
    function is never used to build one -- see the module docstring's first
    bullet.

    A LEAF is a directory directly holding at least one code file. CLAUDE.md
    files are collected at every level, whether or not that level is a leaf --
    a directory of pure documentation is not a leaf but its document still
    governs the tree.

    The project's VCS ignore rules are applied DURING the descent, one
    `ignored_paths` query per visited directory (covering that directory's own
    entries), so an ignored subtree is pruned before it is entered rather than
    filtered out of an already-complete walk. Reused without this, a code file
    inside an ignored directory would pull that directory's ancestors into
    scope -- exactly the case a composition subject must never see. `root`'s
    own VCS status is not queried here; an explicitly named root is the
    caller's concern (see `root_exclusion`), the same split `walk_directory`
    already makes.

    Side-effect free: it stats and lists directories, queries VCS ignore
    status, and reads nothing else.
    """
    root = root.resolve()
    vcs_kind = detect_vcs(root)
    leaves: list[Path] = []
    claude_mds: list[Path] = []
    skipped: list[dict] = []
    noise_pruned = 0
    visited: set[Path] = {root}

    def descend(directory: Path, depth: int) -> None:
        nonlocal noise_pruned
        if depth > MAX_DEPTH:
            skipped.append({"path": str(directory), "reason": "depth-limit"})
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name)
        except OSError:
            return

        ignored = ignored_paths(entries, root=root, vcs=vcs_kind) if entries else set()

        has_code = False
        for entry in entries:
            name = entry.name
            if entry.is_dir():
                disposition = _child_disposition(entry, ignored)
                if disposition == "noise":
                    noise_pruned += 1
                    continue
                if disposition is not None:
                    skipped.append({"path": str(entry), "reason": disposition})
                    continue
                try:
                    target = entry.resolve()
                except (OSError, RuntimeError):
                    skipped.append({"path": str(entry), "reason": SKIP_SYMLINK_OUT})
                    continue
                if not _is_within(target, root):
                    skipped.append({"path": str(entry), "reason": SKIP_SYMLINK_OUT})
                    continue
                if target in visited:
                    continue
                visited.add(target)
                descend(entry, depth + 1)
            elif entry.is_file():
                if name == "CLAUDE.md":
                    claude_mds.append(entry)
                elif is_code_file(entry):
                    if entry in ignored:
                        skipped.append({"path": str(entry), "reason": SKIP_IGNORED})
                    else:
                        has_code = True

        if has_code:
            leaves.append(directory)

    descend(root, 0)
    leaves.sort(key=str)
    claude_mds.sort(key=str)
    return leaves, claude_mds, skipped, noise_pruned


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def root_exclusion(root: Path) -> str | None:
    """Return the exclusion reason the ROOT itself matches, or None.

    walk_directory tests the entries it lists, never the root it was handed --
    so a root that is itself vendored or generated would be read with no
    exclusion recorded. Naming such a directory explicitly is honoured (the user
    asked for it), but it is always REPORTED; a root derived from a diff is
    filtered before it gets here.

    STRUCTURAL ONLY: it matches the root's own name against the vendored and
    generated name rules and inspects its direct entries for the
    vendored-bundle rule. It still runs no subprocess and touches no VCS -- the
    ignored-root case is decided in build_subject, where an ignore query is
    already being spent on the directory's entries.
    """
    reason = _skip_reason(root.name, root)
    if reason is not None:
        return reason
    return None


def build_subject(root: Path) -> dict:
    """Assemble one coverage subject: one directory's own code, plus its chain."""
    root = root.resolve()
    # One ignore query per subject, covering the root and every direct entry.
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        entries = []
    ignored = ignored_paths([root, *entries], root=root)

    root_ignored = root in ignored
    if root_ignored:
        # The user named an ignored directory. Honour it WHOLESALE and report it
        # once at the root, rather than reporting every entry ignored by the same
        # rule -- filtering the entries here would hand back an empty subject and
        # silently defeat the explicit request.
        ignored = set()

    code_files, skipped, noise_pruned, unknown_extensions = walk_directory(
        root, ignored=ignored
    )
    chain = ambient_chain(root)
    exclusion = root_exclusion(root)
    if exclusion is None and root_ignored:
        exclusion = SKIP_IGNORED
    return {
        "root": str(root),
        "rootExclusion": exclusion,
        "codeFiles": [str(p) for p in code_files],
        "ambientClaudeMdPaths": [str(p) for p in chain],
        "skipped": skipped,
        "noisePruned": noise_pruned,
        "unknownExtensions": unknown_extensions,
    }


def diff_roots(repo: Path, git_range: str | None) -> tuple[list[Path], list[str]]:
    """Derive subject roots from changed files. Returns (roots, notes).

    Read-only: runs `git diff --name-only` plus one ignore query. One subject per
    distinct directory DIRECTLY holding a changed code file -- the same
    non-recursive unit a named directory resolves to, so a changed file maps to
    its own directory's subject and never to an ancestor's. Two changed files in
    the same directory therefore collapse to one subject, and a changed file in a
    child directory produces a subject for the CHILD, not for its parent.
    """
    # `git diff --name-only` prints paths relative to the WORKTREE ROOT, not to
    # -C's directory, so names must be joined to the toplevel. Joining them to a
    # subdirectory silently yields nonexistent paths and an empty subject set.
    toplevel = _git_toplevel(repo)
    if toplevel is None:
        return [], ["not a git worktree; --diff needs one"]

    # -c core.quotePath=false: git C-quotes non-ASCII names by default
    # ("caf\303\251.c"), which would not resolve on disk and would be dropped.
    # --end-of-options: a range is user input and must never be read as a flag;
    # without it a value like --output=<path> makes this read-only probe write.
    cmd = [
        "git", "-c", "core.quotePath=false", "-C", str(toplevel),
        "diff", "--name-only", "--end-of-options",
    ]
    if git_range:
        cmd.append(git_range)
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], [f"git diff failed: {exc}"]
    if completed.returncode != 0:
        return [], [f"git diff failed: {completed.stderr.strip()}"]

    dirs: dict[Path, None] = {}
    notes: list[str] = []
    excluded = 0
    for line in completed.stdout.splitlines():
        rel = line.strip()
        if not rel:
            continue
        path = toplevel / rel
        if not is_code_file(path):
            continue
        parent = path.parent
        if not parent.is_dir():
            continue
        # A diff-derived root gets the structural exclusions applied to it. An
        # explicitly named root is honoured-and-reported instead; nobody asked
        # for this one by name.
        # Ancestors are tested by NAME (only a component is in hand); the
        # parent itself also gets the content-based vendored-bundle rule, so a
        # changed file inside a checked-in bundle directory is excluded here for
        # the same reason walk_directory would have skipped it.
        rel_parts = parent.relative_to(toplevel).parts
        if any(_skip_reason(part) for part in rel_parts) or _skip_reason(
            parent.name, parent
        ):
            excluded += 1
            continue
        dirs.setdefault(parent.resolve(), None)
    if excluded:
        notes.append(f"{excluded} changed file(s) skipped: vendored or generated path")

    # A diff-derived root also gets the VCS exclusion applied to it, for the same
    # reason: nobody named it. `git diff` reports tracked files, and a tracked
    # file inside an ignored directory is exactly the case `--no-index` exists to
    # catch -- the rules cover it, so it is not a subject.
    candidates = list(dirs)
    if candidates:
        ignored = ignored_paths(candidates, root=toplevel, vcs="git")
        if ignored:
            notes.append(
                f"{len(ignored)} subject root(s) skipped: covered by the "
                f"project's VCS ignore rules"
            )
            candidates = [d for d in candidates if d not in ignored]
    return candidates, notes


def _git_toplevel(start: Path) -> Path | None:
    """Return the worktree root for `start`, or None when it is not in one."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    out = completed.stdout.strip()
    return Path(out) if out else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory", nargs="?", default=None,
        help="the code subtree to assess; required unless --diff is given",
    )
    parser.add_argument(
        "--diff", nargs="?", const="", default=None, metavar="RANGE",
        help="derive subject roots from changed files instead of a named directory",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--cwd", default=None, help="override cwd (for testing)")
    args = parser.parse_args()

    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd().resolve()
    notes: list[str] = []

    if args.diff is not None:
        roots, notes = diff_roots(cwd, args.diff or None)
    elif args.directory:
        target = (cwd / args.directory).resolve()
        if not target.is_dir():
            print(f"error: not a directory: {target}", file=sys.stderr)
            return 2
        roots = [target]
    else:
        # No whole-repo default, deliberately -- an unbounded default is how this
        # becomes expensive and non-idempotent.
        print(
            "error: name a directory to assess, or pass --diff. There is no "
            "whole-repo default.",
            file=sys.stderr,
        )
        return 2

    subjects = [build_subject(root) for root in roots]
    payload = {"subjects": subjects, "notes": notes}

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    if not subjects:
        print("No coverage subjects resolved.")
        for note in notes:
            print(f"  note: {note}")
        return 0

    for subject in subjects:
        root = subject["root"]
        chain = subject["ambientClaudeMdPaths"]
        print(f"subject: {root}")
        if subject["rootExclusion"]:
            print(
                f"  NOTE: this root is itself {subject['rootExclusion']}; "
                f"assessing it because you named it explicitly"
            )
        print(
            f"  code files: {len(subject['codeFiles'])} "
            f"(this directory only; each child directory is its own subject)"
        )
        if chain:
            print(f"  ambient CLAUDE.md chain ({len(chain)}, root-most first):")
            for path in chain:
                print(f"    - {path}")
        else:
            print("  ambient CLAUDE.md chain: NONE -- no CLAUDE.md loads for this subtree")
        if subject["skipped"]:
            print(f"  skipped ({len(subject['skipped'])}):")
            for entry in subject["skipped"]:
                print(f"    - [{entry['reason']}] {entry['path']}")
        if subject["unknownExtensions"]:
            total_unknown = sum(subject["unknownExtensions"].values())
            print(
                f"  unknown extensions ({total_unknown} file(s), not code, "
                f"not docs, not a recognized asset type):"
            )
            for ext, count in sorted(subject["unknownExtensions"].items()):
                print(f"    - {ext or '(no extension)'}: {count}")
        if subject["noisePruned"]:
            print(f"  noise directories pruned: {subject['noisePruned']}")
        print()

    for note in notes:
        print(f"note: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
