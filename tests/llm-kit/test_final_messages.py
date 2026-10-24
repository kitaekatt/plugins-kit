"""The wording of llm-kit's own messages.

Two checks:

* every named wording difference in ``final_wording.RENAMES`` is real: the
  legacy source carries the legacy text and the final source carries the final
  text (and not the legacy one), so a declared rename cannot stand in for an
  unrelated difference;
* no final-package message tells the reader to install, update, enable, run or
  link a legacy plugin. The legacy plugins stay installed during the freeze,
  but a message in llm-kit names llm-kit (or bootstrap) as the thing to install.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import final_wording as fw

REPO = Path(__file__).resolve().parents[2]
LIB = Path(__import__("llm_kit").__file__).resolve().parent

_LEGACY = r"(?:llm-scripting-kit|job-kit|content-pipeline-kit)"

#: What a message that points the reader at a legacy plugin looks like.
LEGACY_INSTALL_PATTERNS = {
    "plugin command": re.compile(rf"plugin\s+(?:install|update|enable|disable|add|remove)\s+`?{_LEGACY}"),
    "marketplace address": re.compile(rf"{_LEGACY}@plugins-kit"),
    "shared-lib owner": re.compile(rf"links\s+{_LEGACY}'s\s+shared\s+libs"),
    "legacy command in backticks": re.compile(rf"`{_LEGACY}[\s`]"),
    "install, enable or update verb": re.compile(
        rf"\b(?:install|enable|update|upgrade|provisioned|bootstrap-provisioned)\s+(?:the\s+)?{_LEGACY}\b"
    ),
    "legacy CLI environment": re.compile(rf"\b{_LEGACY}\s+CLI\s+environment"),
}


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


# --- the named renames -----------------------------------------------------

def test_every_rename_has_a_name_and_no_duplicate_name():
    names = [r.name for r in fw.RENAMES]
    assert len(names) == len(set(names)), "duplicate rename names"
    assert all(n.strip() for n in names)


@pytest.mark.parametrize("rename", fw.RENAMES, ids=[r.name for r in fw.RENAMES])
def test_rename_matches_the_live_sources(rename):
    legacy_src = fw.flat(_read(rename.legacy_file))
    final_src = fw.flat(_read(rename.final_file))
    legacy_frag = rename.legacy_src or rename.legacy
    final_frag = rename.final_src or rename.final
    assert legacy_frag in legacy_src, f"{rename.legacy_file} no longer carries {legacy_frag!r}"
    assert final_frag in final_src, f"{rename.final_file} does not carry {final_frag!r}"
    assert legacy_frag not in final_src, f"{rename.final_file} still carries {legacy_frag!r}"


@pytest.mark.parametrize("name, legacy, final", fw.FLOORS, ids=[f[0] for f in fw.FLOORS])
def test_floor_difference_matches_the_live_constants(name, legacy, final):
    texts = {
        "json_schema subset selector": (
            ("plugins/job-kit/lib/job_kit/interrupts.py", f'_JSON_SCHEMA_LSK_VERSION = "{legacy}"'),
            (str(Path(__import__("llm_kit").__file__).resolve().parent / "jobs/interrupts.py"), f'_JSON_SCHEMA_LSK_VERSION = "{final}"'),
        ),
        "classify_backpressure": (
            ("plugins/content-pipeline-kit/lib/content_pipeline/llm/platform.py",
             f'_BACKPRESSURE_FLOOR = "{legacy}"'),
            (str(Path(__import__("llm_kit").__file__).resolve().parent / "pipeline/runtime/platform.py"), f'_BACKPRESSURE_FLOOR = "{final}"'),
        ),
    }[name]
    for (path, fragment) in texts:
        assert fragment in _read(path), f"{path} does not carry {fragment}"


def test_to_final_rewrites_every_legacy_text_and_is_idempotent():
    for rename in fw.RENAMES:
        assert fw.to_final(rename.legacy) == rename.final, rename.name
        assert fw.to_final(rename.final) == rename.final, rename.name


# --- no legacy install instruction in a final message ----------------------

def _message_constants(path: Path):
    """(line, text) for each string constant that is not a docstring or bare doc."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            yield node.lineno, node.value


def legacy_install_offenders(root: Path = LIB) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts or "_copy" in path.parts:
            continue
        for line, text in _message_constants(path):
            collapsed = " ".join(text.split())
            for kind, pattern in LEGACY_INSTALL_PATTERNS.items():
                if pattern.search(collapsed):
                    found.append(f"{path.relative_to(root).as_posix()}:{line}: {kind}: {collapsed[:100]!r}")
    return found


def test_no_final_message_tells_the_reader_to_use_a_legacy_plugin():
    assert legacy_install_offenders() == []


@pytest.mark.parametrize("kind, sample", [
    ("plugin command", "Run `claude plugin install llm-scripting-kit@plugins-kit`."),
    ("plugin command", "claude plugin update job-kit"),
    ("marketplace address", "content-pipeline-kit@plugins-kit"),
    ("shared-lib owner", "so it links job-kit's shared libs"),
    ("legacy command in backticks", "for example `job-kit resume <run-id>`"),
    ("install, enable or update verb", "update llm-scripting-kit to >= 1.0"),
    ("legacy CLI environment", "Use the bootstrap-provisioned llm-scripting-kit CLI environment"),
])
def test_the_scan_recognizes_each_legacy_instruction_shape(kind, sample):
    assert LEGACY_INSTALL_PATTERNS[kind].search(" ".join(sample.split())), (kind, sample)


def test_the_scan_reads_message_constants_not_docstrings(tmp_path):
    (tmp_path / "m.py").write_text(
        '"""Run `claude plugin install job-kit@plugins-kit` (a docstring)."""\n'
        "MSG = 'run `claude plugin install job-kit@plugins-kit`'\n",
        encoding="utf-8",
    )
    found = legacy_install_offenders(tmp_path)
    assert {f.split(": ")[1] for f in found} >= {"marketplace address", "plugin command"}, found
    assert all(f.startswith("m.py:2:") for f in found), found
