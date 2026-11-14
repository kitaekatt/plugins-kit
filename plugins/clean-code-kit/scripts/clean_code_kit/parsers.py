"""The tree-sitter parser cache both metric tools read: one location, one fetch.

The tools load their parsers through tree-sitter-language-pack, which fetches
one parser bundle for the platform on first use and caches it under
``<cache>/tree-sitter-language-pack/v<pack version>/``. The runner points the
tools at ``cache_dir(<plugin data dir>)`` through ``CACHE_ENV``; the plugin's
custom bootstrap script fills the same directory ahead of the first run. Both
take the location from this module, so they cannot disagree about it.

This file is stdlib-only and has no package-relative imports: the custom
bootstrap script loads it by path inside the bootstrap engine's interpreter,
and runs it as a program under the plugin venv's interpreter to fetch.

Run as a program: ``<plugin venv interpreter> -I parsers.py <cache dir>``
downloads the grammars in ``GRAMMARS`` into ``<cache dir>``; it exits non-zero,
with tree-sitter-language-pack's DownloadError on stderr, when it cannot.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

CACHE_ENV = "TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"
CACHE_DIRNAME = "parsers"
PACK_DIST = "tree_sitter_language_pack"
# The grammars the tools request for the plugin's languages (crapper parses
# the JavaScript extensions as typescript; dryer parses javascript, gdscript and cpp). Fetching any one grammar stores the
# platform's whole parser bundle, which then serves every other grammar offline.
GRAMMARS = ("python", "typescript", "javascript", "gdscript", "cpp")


def cache_dir(data_dir: Path | str) -> Path:
    """The parser cache inside the plugin data directory ``data_dir``."""
    return Path(data_dir) / CACHE_DIRNAME


def version_dir(cache: Path, version: str) -> Path:
    """Where tree-sitter-language-pack ``version`` keeps its files under ``cache``."""
    return cache / "tree-sitter-language-pack" / f"v{version}"


def is_warm(cache: Path, version: str) -> bool:
    """True when ``cache`` holds the release manifest and a parser bundle for ``version``."""
    directory = version_dir(cache, version)
    return (directory / "manifest.json").is_file() and any(
        (directory / "bundles").glob("*.tar.zst"))


def pack_version(venv: Path) -> str | None:
    """The tree-sitter-language-pack version installed in ``venv``, read from its dist-info."""
    for pattern in ("lib/python*/site-packages", "Lib/site-packages"):
        for site in venv.glob(pattern):
            for info in site.glob(f"{PACK_DIST}-*.dist-info"):
                return info.name[len(PACK_DIST) + 1:-len(".dist-info")]
    return None


def _main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: parsers.py <cache dir>", file=sys.stderr)
        return 2
    os.environ[CACHE_ENV] = argv[0]
    import tree_sitter_language_pack

    tree_sitter_language_pack.download(list(GRAMMARS))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
