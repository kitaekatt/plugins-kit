"""Fill the tree-sitter parser cache the code-audit tools read.

The venv phase, which runs before this script, installs dryer, crapper and
tree-sitter-language-pack in the plugin venv. The parsers those tools load are
a separate download (one bundle of about 26 MB per platform) that the pack
otherwise fetches on the first scan. This script fetches it during the
bootstrap pass instead, into the cache directory the runner points the tools
at, so the first audit on a machine needs no network.

The cache location and the warm check come from
``scripts/clean_code_kit/parsers.py``, the module the runner uses, loaded here
by path. The fetch runs that same module as a program under the provisioned
venv's interpreter, because this script runs inside the bootstrap engine's
interpreter, which does not have the pack installed.

Steady state costs no subprocess: the pack version is read from the venv's
dist-info and the cache is checked on disk. A failed fetch is logged and
recorded as a deferred requirement; the runner's first-use fetch remains the
fallback.
"""

import importlib.util
import subprocess
from pathlib import Path
from typing import Any, Optional

REQUIREMENT = "tree_sitter_parsers"
_PARSERS_FILE = Path(__file__).resolve().parent / "scripts" / "clean_code_kit" / "parsers.py"


def _load_parsers():
    spec = importlib.util.spec_from_file_location("_clean_code_kit_parsers", _PARSERS_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


parsers = _load_parsers()


def _venv_python(data_dir: Path) -> Optional[Path]:
    """Return the provisioned venv python, using the engine's candidate order."""
    for candidate in (
        data_dir / ".venv" / "bin" / "python",
        data_dir / ".venv" / "Scripts" / "python.exe",
    ):
        if candidate.is_file():
            return candidate
    return None


def _defer(ctx: Any, command: str, diagnostic: str) -> None:
    """Log the failure and record the fetch as a point-of-need requirement."""
    ctx.log(f"code-audit: parser fetch failed: {diagnostic[-2000:]}")
    add = getattr(ctx, "add_deferred_requirement", None)
    if add is None:
        ctx.log("code-audit: this bootstrap engine cannot record a deferred requirement; "
                "the first audit run fetches the parsers")
        return
    add(
        REQUIREMENT,
        user_msg="clean-code-kit's code audit needs its parsers downloaded (about 26 MB) "
                 "before it can run without network.",
        agent_msg=(
            "The tree-sitter parsers for clean-code-kit's code audit are not cached. "
            "The first audit run downloads them and needs network; to fetch them now, "
            f"run: {command}"
        ),
        satisfied_by=command,
    )


def bootstrap(ctx: Any) -> None:
    """Ensure the parser cache under the plugin data directory is warm."""
    data_dir = Path(ctx.data_dir)
    cache = parsers.cache_dir(data_dir)
    python = _venv_python(data_dir)
    shown_python = python or data_dir / ".venv" / "bin" / "python"
    command = f'"{shown_python}" -I "{_PARSERS_FILE}" "{cache}"'
    if python is None:
        _defer(ctx, command, "provisioned venv python is missing")
        return
    version = parsers.pack_version(python.parents[1])
    if version is None:
        _defer(ctx, command, "tree-sitter-language-pack is not installed in the plugin venv")
        return
    if parsers.is_warm(cache, version):
        # Verbose-only so a healthy bootstrap stays silent.
        ctx.log_ok(f"code-audit: parsers already cached (tree-sitter-language-pack {version})")
        return
    result = subprocess.run(
        [str(python), "-I", str(_PARSERS_FILE), str(cache)],
        capture_output=True, text=True, cwd=str(data_dir),
    )
    if result.returncode != 0:
        _defer(ctx, command, (result.stderr or result.stdout or "").strip())
        return
    if not parsers.is_warm(cache, version):
        _defer(ctx, command, f"the fetch exited 0 but left no parser bundle in "
                             f"{parsers.version_dir(cache, version)}")
        return
    ctx.log(f"code-audit: parsers fetched (tree-sitter-language-pack {version})")
