"""Library boundary guard: packages/*/src must stand without an AI harness.

The libraries under ``packages/`` are meant to be usable by someone who is not
running a Claude Code plugin install, and they are destined for a separate
repository. This guard enforces that mechanically with an AST scan of every
``.py`` under ``packages/*/src/``:

* no import of ``bootstrap_lib``, of any module resolvable only from
  ``plugins/**``, or of a harness SDK (``import X``, ``from X import``, and
  ``importlib.import_module("X")`` string literals, at any nesting depth);
* no string constant that points into ``~/.claude`` or ``plugins/``.

Every remaining violation is listed in ``IMPORT_ALLOWLIST`` /
``PATH_ALLOWLIST`` as ``{relative_path: [(anchor, reason), ...]}``. The
anchor is text on the violating source line. An entry whose file is gone, or
whose anchor no longer matches a live violation, fails the stale check, so the
lists can only shrink: that list IS the remaining-coupling inventory for the
split into a standalone libraries repository.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PACKAGES = REPO / "packages"
PLUGINS = REPO / "plugins"

# Harness SDKs a harness-neutral library must not import.
HARNESS_MODULES = frozenset({"claude_agent_sdk", "claude_code_sdk", "codex_sdk"})


def _library_roots() -> frozenset[str]:
    """Top-level import names the packages themselves provide."""
    roots: set[str] = set()
    for src in PACKAGES.glob("*/src"):
        roots.update(p.name for p in src.iterdir() if p.is_dir() or p.suffix == ".py")
    return frozenset(r.removesuffix(".py") for r in roots)


def _plugin_only_roots() -> frozenset[str]:
    """Top-level packages that exist only under plugins/** (bootstrap_lib etc.)."""
    roots: set[str] = set()
    for plugin in PLUGINS.iterdir():
        if not plugin.is_dir():
            continue
        for base in (plugin, plugin / "lib", plugin / "src"):
            if not base.is_dir():
                continue
            for pkg in base.iterdir():
                if pkg.is_dir() and (pkg / "__init__.py").is_file():
                    roots.add(pkg.name)
    return frozenset(roots) - _library_roots()


def forbidden_roots() -> frozenset[str]:
    return _plugin_only_roots() | HARNESS_MODULES | {"bootstrap_lib"}


def _source_files() -> list[Path]:
    return sorted(PACKAGES.glob("*/src/**/*.py"))


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    ids.add(id(value))
    return ids


def _is_import_module_call(node: ast.Call) -> bool:
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name == "import_module"


def _is_path_literal(value: str) -> bool:
    return (
        value in (".claude", "plugins")
        or value.startswith("~/.claude")
        or ".claude/" in value
        or "plugins/" in value
    )


def scan_source(text: str, forbidden: frozenset[str]) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Return (import findings, path findings) as (line number, description)."""
    tree = ast.parse(text)
    docs = _docstring_ids(tree)
    imports: list[tuple[int, str]] = []
    paths: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        elif (
            isinstance(node, ast.Call)
            and _is_import_module_call(node)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names = [node.args[0].value]
        for name in names:
            if name.split(".")[0] in forbidden:
                imports.append((node.lineno, name))
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docs
            and _is_path_literal(node.value)
        ):
            paths.append((node.lineno, node.value))
    return imports, paths


# {relative path under packages/: [(anchor text on the violating line, reason)]}
IMPORT_ALLOWLIST: dict[str, list[tuple[str, str]]] = {
    "katsoft-llm-kit/src/llm_kit/_runtime.py": [
        ('import_module("bootstrap_lib")', "fleet-presence probe: absence of bootstrap_lib is the supported standalone state"),
    ],
    "katsoft-llm-kit/src/llm_kit/adapters/backends.py": [
        ("from bootstrap_lib.claudx import nested_claude_args", "claudx nested-claude argv; Claude-plugin specific"),
    ],
    "katsoft-llm-kit/src/llm_kit/completion/skill_context.py": [
        ("import bootstrap_lib  # noqa", "fleet probe before skill_material"),
        ("import bootstrap_lib.skill_material as module", "skill material loader owned by bootstrap"),
    ],
    "katsoft-llm-kit/src/llm_kit/dispatch/declaration.py": [
        ("import bootstrap_lib  # noqa", "fleet probe before execution_event"),
        ('import_module("bootstrap_lib.execution_event")', "execution-event schema probe; too-old bootstrap is reported"),
    ],
    "katsoft-llm-kit/src/llm_kit/dispatch/review_lane.py": [
        ("from bootstrap_lib.code_review.lane_prompts import", "review lane prompts live in bootstrap_lib.code_review"),
        ("from bootstrap_lib.code_review.lane_output import", "review lane output parsing lives in bootstrap_lib.code_review"),
    ],
    "katsoft-llm-kit/src/llm_kit/jobs/model.py": [
        ("import bootstrap_lib  # noqa", "fleet probe before model_declaration"),
        ("from bootstrap_lib import model_declaration", "model declaration validator canonical copy in bootstrap"),
    ],
    "katsoft-llm-kit/src/llm_kit/jobs/run.py": [
        ("from bootstrap_lib.claudx import contract_command", "claudx contract command; Claude-plugin specific"),
    ],
    "katsoft-llm-kit/src/llm_kit/pipeline/execution/drivers/claude_bg.py": [
        ("from bootstrap_lib.claudx import refuse_supervised", "claudx supervision refusal; Claude-plugin specific"),
    ],
    "katsoft-llm-kit/src/llm_kit/registry/declaration_syntax.py": [
        ("import bootstrap_lib  # noqa", "fleet probe before model_declaration"),
        ("from bootstrap_lib import model_declaration", "model declaration validator canonical copy in bootstrap"),
    ],
    "katsoft-llm-kit/src/llm_kit/registry/models.py": [
        ("from bootstrap_lib.config_resolve import", "layered fleet/machine config resolution owned by bootstrap"),
        ("from bootstrap_lib.manifest_merge import deep_merge", "deep merge helper owned by bootstrap"),
    ],
}

_CLAUDE_HOME = "legacy ~/.claude location kept as a fallback until the libraries own a harness-neutral data root"
PATH_ALLOWLIST: dict[str, list[tuple[str, str]]] = {
    "katsoft-llm-kit/src/llm_kit/adapters/usage_sources.py": [
        ('Path.home() / ".claude" / "plugins"', _CLAUDE_HOME + " (usage data dir)"),
    ],
    "katsoft-llm-kit/src/llm_kit/bootstrap_guard.py": [
        ('/ ".claude" / "plugins"', _CLAUDE_HOME + " (bootstrap provisioning probe)"),
    ],
    "katsoft-llm-kit/src/llm_kit/dispatch/usage_budget.py": [
        ('Path.home() / ".claude" / "plugins"', _CLAUDE_HOME + " (usage budget state)"),
    ],
    "katsoft-llm-kit/src/llm_kit/frontdoor/server.py": [
        ('Path.home() / ".claude" / "plugins"', _CLAUDE_HOME + " (access log)"),
    ],
    "katsoft-llm-kit/src/llm_kit/jobs/run.py": [
        ('/ ".claude"', _CLAUDE_HOME + " (job state root)"),
        ('/ "plugins"', _CLAUDE_HOME + " (job state root, plugins segment)"),
    ],
    "katsoft-llm-kit/src/llm_kit/pipeline/execution/drivers/claude_bg.py": [
        ('Path.home() / ".claude" / "projects"', "Claude Code transcript root read by the claude-bg driver"),
        ('Path.home() / ".claude" / "jobs"', "Claude Code background-jobs root read by the claude-bg driver"),
    ],
    "katsoft-llm-kit/src/llm_kit/registry/consent.py": [
        ('Path.home() / ".claude" / "plugins"', "READ-ONLY legacy consent file, used only when the neutral $XDG_CONFIG_HOME/llm-kit/consent.json is absent; consent is never written there"),
    ],
    "katsoft-llm-kit/src/llm_kit/registry/constants.py": [
        ('CLI_COMMAND = "~/.claude/plugins', "display text of the Claude plugin venv launcher shown in agent-facing messages; never opened, read or written"),
        ("CLAUDE_BOOTSTRAP_DATA_ROOT:-$HOME/.claude/plugins/data", "display text of the redirected Claude plugin venv launcher; never opened, read or written"),
        ('Path.home() / ".claude" / "plugins"', "READ-ONLY legacy user key file, used only when neither $LLM_KIT_ENV_FILE nor the neutral $XDG_CONFIG_HOME/llm-kit/.env exists; keys are never written there"),
    ],
    "katsoft-llm-kit/src/llm_kit/registry/model_endpoints.py": [
        ('Path(".claude") / "config"', "READ-ONLY legacy endpoint registry, used only when $MODEL_ENDPOINTS_REGISTRY is unset and the neutral $XDG_CONFIG_HOME/llm-kit/model-endpoints.yaml is absent"),
    ],
    "katsoft-llm-kit/src/llm_kit/registry/models.py": [
        ('Path.home() / ".claude" / "config"', "fleet config layer, read only through bootstrap_lib's layered resolver on a fleet install; a package-only install (no bootstrap_lib) never reads it"),
    ],
}


def _line(text_lines: list[str], lineno: int) -> str:
    return text_lines[lineno - 1]


def _collect() -> tuple[dict[str, list[str]], dict[str, list[str]], list[str]]:
    """Map relative path -> violating source lines, for imports and paths."""
    forbidden = forbidden_roots()
    imports: dict[str, list[str]] = {}
    paths: dict[str, list[str]] = {}
    files: list[str] = []
    for path in _source_files():
        rel = path.relative_to(PACKAGES).as_posix()
        files.append(rel)
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        imp, pth = scan_source(text, forbidden)
        if imp:
            imports[rel] = [_line(lines, n) for n, _ in imp]
        if pth:
            paths[rel] = [_line(lines, n) for n, _ in pth]
    return imports, paths, files


def _unallowed(found: dict[str, list[str]], allow: dict[str, list[tuple[str, str]]]) -> list[str]:
    bad = []
    for rel, lines in sorted(found.items()):
        anchors = [a for a, _ in allow.get(rel, [])]
        for line in lines:
            if not any(a in line for a in anchors):
                bad.append(f"{rel}: {line.strip()}")
    return bad


def _stale(found: dict[str, list[str]], allow: dict[str, list[tuple[str, str]]], files: list[str]) -> list[str]:
    stale = []
    for rel, entries in sorted(allow.items()):
        if rel not in files:
            stale.append(f"{rel}: file no longer exists")
            continue
        for anchor, reason in entries:
            if not reason.strip():
                stale.append(f"{rel}: {anchor!r} has no reason")
            if not any(anchor in line for line in found.get(rel, [])):
                stale.append(f"{rel}: anchor {anchor!r} matches no live violation")
    return stale


def test_no_forbidden_imports() -> None:
    imports, _, _ = _collect()
    bad = _unallowed(imports, IMPORT_ALLOWLIST)
    assert not bad, "library imports a plugin/harness module outside the allowlist:\n" + "\n".join(bad)


def test_no_plugin_or_claude_path_literals() -> None:
    _, paths, _ = _collect()
    bad = _unallowed(paths, PATH_ALLOWLIST)
    assert not bad, "library points into plugins/ or ~/.claude outside the allowlist:\n" + "\n".join(bad)


def test_allowlists_are_not_stale() -> None:
    imports, paths, files = _collect()
    stale = _stale(imports, IMPORT_ALLOWLIST, files) + _stale(paths, PATH_ALLOWLIST, files)
    assert not stale, "stale allowlist entries (delete them):\n" + "\n".join(stale)


def test_forbidden_roots_cover_known_names() -> None:
    roots = forbidden_roots()
    assert "bootstrap_lib" in roots
    assert "llm_kit" not in roots


@pytest.mark.parametrize(
    "source",
    [
        "import bootstrap_lib\n",
        "def f():\n    from bootstrap_lib.x import y\n",
        "import importlib\nimportlib.import_module('bootstrap_lib.z')\n",
        "try:\n    import claude_agent_sdk\nexcept ImportError:\n    pass\n",
    ],
)
def test_scanner_detects_forbidden_imports(source: str) -> None:
    imports, _ = scan_source(source, forbidden_roots())
    assert imports


def test_scanner_detects_path_literals_and_skips_docstrings() -> None:
    _, paths = scan_source('x = "~/.claude/y"\nz = "plugins/a"\n', forbidden_roots())
    assert len(paths) == 2
    _, paths = scan_source('"""doc ~/.claude/y plugins/a"""\n', forbidden_roots())
    assert not paths
