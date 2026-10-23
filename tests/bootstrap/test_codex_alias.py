"""bootstrap_lib.codex is an exact alias of llm_kit.adapters.codex_command.

Command construction has one owner, the katsoft-llm-kit package. The alias
copies nothing and keeps no behavior; where the distribution is absent, or is a
release before the module existed, importing the alias refuses with the remedy
named, and it never runs a stale builder. Each case runs in a fresh interpreter
so import state cannot leak between them.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO / "plugins" / "bootstrap"
KIT_SRC = REPO / "packages" / "katsoft-llm-kit" / "src"
ALIAS_SOURCE = BOOTSTRAP / "bootstrap_lib" / "codex.py"
TARGET = "llm_kit.adapters.codex_command"


def run(body: str, *paths: Path) -> dict:
    """Run ``body`` in an isolated interpreter; it assigns ``result`` (JSON-able)."""
    script = (
        "import json, sys\n"
        f"sys.path[:0] = {[str(p) for p in paths]!r}\n"
        + textwrap.dedent(body)
        + "\nprint(json.dumps({'ok': result}))\n"
    )
    proc = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])["ok"]


def make_llm_kit(root: Path, *, codex_command: str | None) -> Path:
    """A minimal llm_kit tree; ``codex_command`` is the module's source, or None for a 1.2 shape."""
    package = root / "llm_kit"
    (package / "adapters").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "adapters" / "__init__.py").write_text("", encoding="utf-8")
    if codex_command is not None:
        (package / "adapters" / "codex_command.py").write_text(codex_command, encoding="utf-8")
    return root


def test_the_alias_is_the_canonical_module_itself():
    result = run("""
        import bootstrap_lib.codex as alias
        import llm_kit.adapters.codex_command as canonical
        from bootstrap_lib import codex as from_package
        result = {
            "same": alias is canonical,
            "from_package": from_package is canonical,
            "private": alias._reject_cmd_metacharacters is canonical._reject_cmd_metacharacters,
            "file": canonical.__file__,
        }
    """, KIT_SRC, BOOTSTRAP)
    assert result["same"] and result["from_package"] and result["private"]
    assert Path(result["file"]) == KIT_SRC / "llm_kit" / "adapters" / "codex_command.py"


def test_the_alias_module_copies_nothing():
    """No definition lives in bootstrap_lib/codex.py: only the alias call."""
    tree = ast.parse(ALIAS_SOURCE.read_text(encoding="utf-8"))
    defined = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))]
    assert defined == []
    assert "build_codex_exec_argv" not in ALIAS_SOURCE.read_text(encoding="utf-8")


def test_an_llm_kit_without_the_module_is_refused_as_too_old(tmp_path):
    """The 1.2 shape: llm_kit imports, the module does not exist."""
    tree = make_llm_kit(tmp_path / "old", codex_command=None)
    result = run("""
        try:
            import bootstrap_lib.codex
        except ModuleNotFoundError as exc:
            result = {"name": exc.name, "message": str(exc),
                      "left_in_sys_modules": "bootstrap_lib.codex" in sys.modules}
        else:
            result = {"imported": True}
    """, tree, BOOTSTRAP)
    assert result["name"] == TARGET
    assert "predates" in result["message"] and "katsoft-llm-kit>=1.3,<2" in result["message"]
    assert result["left_in_sys_modules"] is False


def test_an_absent_llm_kit_is_refused_as_missing(tmp_path):
    result = run("""
        class Absent:
            def find_spec(self, name, path=None, target=None):
                if name == "llm_kit":
                    raise ModuleNotFoundError("No module named 'llm_kit'", name="llm_kit")
        sys.meta_path.insert(0, Absent())
        try:
            import bootstrap_lib.codex
        except ModuleNotFoundError as exc:
            result = {"name": exc.name, "message": str(exc)}
        else:
            result = {"imported": True}
    """, BOOTSTRAP)
    assert result["name"] == "llm_kit"
    assert "not installed" in result["message"] and "katsoft-llm-kit>=1.3,<2" in result["message"]
    assert "predates" not in result["message"]


def test_any_other_import_failure_propagates_unchanged(tmp_path):
    """A broken install must not read as an absent or old one."""
    tree = make_llm_kit(tmp_path / "broken", codex_command="import a_dependency_that_is_not_there_xyz\n")
    result = run("""
        try:
            import bootstrap_lib.codex
        except ModuleNotFoundError as exc:
            result = {"name": exc.name, "message": str(exc)}
        else:
            result = {"imported": True}
    """, tree, BOOTSTRAP)
    assert result["name"] == "a_dependency_that_is_not_there_xyz"
    assert "katsoft-llm-kit" not in result["message"]


def test_the_alias_is_not_bootstraps_own_dependency():
    """Importing bootstrap_lib itself reaches neither the alias nor llm_kit."""
    result = run("""
        import bootstrap_lib
        result = sorted(n for n in sys.modules if n == "llm_kit" or n.startswith("llm_kit.")
                        or n in ("bootstrap_lib.codex", "bootstrap_lib._llm_kit_alias"))
    """, BOOTSTRAP)
    assert result == []
