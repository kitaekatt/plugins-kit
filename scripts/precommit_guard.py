#!/usr/bin/env python3
"""Block project-derived or generated data from staged commits."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# The generated-artifact signature list is SHARED with the code-review pipeline
# (bootstrap_lib.code_review.pipeline excludes such files from reviewer fan-out).
# Both answer "did a tool write this file?", so they answer it from one list --
# a second copy here would drift and let a file be refused at commit time but
# fanned out to reviewers, or the reverse. bootstrap_lib is stdlib-only, which
# this hook requires: it must run on an unprovisioned clone.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "bootstrap"))

from bootstrap_lib.code_review.generated import detect_signature_bytes  # noqa: E402

MAX_STAGED_FILE_BYTES = 1024 * 1024
LOCAL_TERMS_PATH = PurePosixPath(".githooks/project-terms.txt")
OVERRIDE_ENV = "PLUGINS_KIT_ALLOW_PROJECT_DATA"
PATTERN_DOC = PurePosixPath(
    "plugins/bootstrap/skills/bootstrap/references/durable-project-data.md"
)

_COPY_FUNCTIONS = frozenset({"copy", "copy2", "copyfile"})


@dataclass(frozen=True)
class Violation:
    rule: str
    path: str
    detail: str


class GuardInspectionError(RuntimeError):
    """The staged tree could not be inspected safely."""


def _git(repo_root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise GuardInspectionError(detail or f"git {' '.join(args)} failed")
    return result.stdout


def _decode_paths(output: bytes) -> list[str]:
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in output.split(b"\0")
        if item
    ]


def staged_paths(repo_root: Path) -> list[str]:
    return _decode_paths(
        _git(
            repo_root,
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=AM",
            "-z",
        )
    )


def staged_blob(repo_root: Path, repo_path: str) -> bytes:
    return _git(repo_root, "cat-file", "blob", f":{repo_path}")


def staged_blob_size(repo_root: Path, repo_path: str) -> int:
    raw = _git(repo_root, "cat-file", "-s", f":{repo_path}")
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise GuardInspectionError(f"invalid staged size for {repo_path}") from exc


def staged_bootstrap_manifests(repo_root: Path) -> dict[str, bytes]:
    paths = _decode_paths(
        _git(
            repo_root,
            "ls-files",
            "-z",
            "--",
            "bootstrap.json",
            ":(glob)**/bootstrap.json",
        )
    )
    return {path: staged_blob(repo_root, path) for path in paths}


def _manifest_values(data: object, key: str) -> Iterable[object]:
    if isinstance(data, dict):
        for item_key, value in data.items():
            if item_key == key:
                yield value
            yield from _manifest_values(value, key)
    elif isinstance(data, list):
        for value in data:
            yield from _manifest_values(value, key)


def _repo_target(manifest_path: str, declaration: str) -> str | None:
    prefix = "${plugin_root}"
    if declaration != prefix and not declaration.startswith(prefix + "/"):
        return None

    # A plugin manifest's ${plugin_root} is the directory containing that
    # bootstrap.json. In the development repo, map it to that same repo-relative
    # directory, then append the declared suffix.
    plugin_root = PurePosixPath(manifest_path).parent
    suffix = declaration[len(prefix) :].lstrip("/")
    candidate = plugin_root / PurePosixPath(suffix)
    if ".." in candidate.parts:
        raise GuardInspectionError(
            f"write target escapes plugin root in {manifest_path}: {declaration}"
        )
    return candidate.as_posix().removeprefix("./")


def _assignment_map(tree: ast.AST) -> dict[str, ast.expr]:
    assignments: dict[str, ast.expr] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                assignments[node.target.id] = node.value
    return assignments


def _path_parts(
    node: ast.expr,
    assignments: Mapping[str, ast.expr],
    seen: frozenset[str] = frozenset(),
) -> tuple[str, ...] | None:
    if isinstance(node, ast.Attribute):
        if (
            node.attr == "plugin_root"
            and isinstance(node.value, ast.Name)
            and node.value.id == "ctx"
        ):
            return ()
        return None
    if isinstance(node, ast.Name):
        if node.id in seen or node.id not in assignments:
            return None
        return _path_parts(
            assignments[node.id], assignments, seen | frozenset({node.id})
        )
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return (node.value,)
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == "Path" and node.args:
            return _path_parts(node.args[0], assignments, seen)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "joinpath":
            base = _path_parts(node.func.value, assignments, seen)
            additions = [_path_parts(arg, assignments, seen) for arg in node.args]
            if base is None or any(value is None for value in additions):
                return None
            return base + tuple(part for value in additions for part in value or ())
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _path_parts(node.left, assignments, seen)
        right = _path_parts(node.right, assignments, seen)
        if left is None or right is None:
            return None
        return left + right
    return None


def _shutil_aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    modules = {"shutil"}
    functions: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "shutil":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "shutil":
            for alias in node.names:
                if alias.name in _COPY_FUNCTIONS:
                    functions.add(alias.asname or alias.name)
    return modules, functions


def copy_destinations(source: bytes) -> set[str]:
    try:
        tree = ast.parse(source.decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise GuardInspectionError(f"cannot parse custom bootstrap script: {exc}") from exc

    assignments = _assignment_map(tree)
    modules, imported_functions = _shutil_aliases(tree)
    destinations: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        matched = False
        if isinstance(node.func, ast.Attribute):
            matched = (
                node.func.attr in _COPY_FUNCTIONS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in modules
            )
        elif isinstance(node.func, ast.Name):
            matched = node.func.id in imported_functions
        if not matched:
            continue

        destination = node.args[1] if len(node.args) > 1 else None
        for keyword in node.keywords:
            if keyword.arg == "dst":
                destination = keyword.value
        if destination is None:
            continue
        parts = _path_parts(destination, assignments)
        if parts is None:
            continue
        path = PurePosixPath(*parts)
        if ".." in path.parts:
            raise GuardInspectionError("custom bootstrap copy target escapes plugin root")
        destinations.add(path.as_posix().removeprefix("./"))
    return destinations


def manifest_write_targets(
    manifests: Mapping[str, bytes],
    script_loader: Callable[[str], bytes],
) -> set[str]:
    targets: set[str] = set()
    for manifest_path, source in manifests.items():
        try:
            manifest = json.loads(source.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GuardInspectionError(f"cannot parse {manifest_path}: {exc}") from exc

        for value in _manifest_values(manifest, "extract_to"):
            if isinstance(value, str):
                target = _repo_target(manifest_path, value)
                if target:
                    targets.add(target)

        script = manifest.get("script") if isinstance(manifest, dict) else None
        script_path = script.get("path") if isinstance(script, dict) else None
        if not isinstance(script_path, str) or not script_path.endswith(
            "custom_bootstrap.py"
        ):
            continue
        repo_script = (PurePosixPath(manifest_path).parent / script_path).as_posix()
        for destination in copy_destinations(script_loader(repo_script)):
            target = (
                PurePosixPath(manifest_path).parent / PurePosixPath(destination)
            ).as_posix()
            targets.add(target.removeprefix("./"))
    return targets


def load_local_terms(path: Path) -> list[str] | None:
    if not path.is_file():
        return None
    return parse_local_terms(path.read_text(encoding="utf-8"))


def parse_local_terms(content: str) -> list[str]:
    terms: list[str] = []
    for line in content.splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            terms.append(value.casefold())
    return terms


def disabled_terms_message() -> str:
    return f"pre-commit: optional term checks disabled; no {LOCAL_TERMS_PATH} found."


def _is_forbidden_path(path: str, targets: set[str]) -> str | None:
    normalized = PurePosixPath(path).as_posix().removeprefix("./")
    for target in sorted(targets):
        if normalized == target or normalized.startswith(target.rstrip("/") + "/"):
            return target
    return None


def inspect_file(
    path: str,
    size: int,
    content: bytes | None,
    forbidden_targets: set[str],
    terms: Iterable[str],
) -> list[Violation]:
    violations: list[Violation] = []
    if size > MAX_STAGED_FILE_BYTES:
        violations.append(
            Violation(
                "size",
                path,
                f"{size} bytes exceeds {MAX_STAGED_FILE_BYTES} bytes",
            )
        )

    target = _is_forbidden_path(path, forbidden_targets)
    if target:
        violations.append(
            Violation("bootstrap write target", path, f"declared target {target}")
        )

    if content is None:
        return violations
    label = detect_signature_bytes(content)
    if label:
        violations.append(Violation("generated artifact", path, label))

    searchable = path.casefold() + "\n" + content.decode(
        "utf-8", errors="replace"
    ).casefold()
    for term in terms:
        if term in searchable:
            violations.append(Violation("local term", path, "matched local term list"))
            break
    return violations


def refusal_message(violations: Iterable[Violation]) -> str:
    lines = [
        "Refusing this commit: it looks like project-specific data.",
        "plugins-kit is a public repo and we never check in project-specific data --",
        "not generated artifacts, not project names, not internal paths.",
        f"Durable per-project data belongs in that project's own repo; see {PATTERN_DOC}.",
        f"If this is a false positive, retry with {OVERRIDE_ENV}=1.",
        "",
        "Detected:",
    ]
    for violation in violations:
        lines.append(
            f"  - {violation.rule}: {violation.path} ({violation.detail})"
        )
    return "\n".join(lines)


def main() -> int:
    if os.environ.get(OVERRIDE_ENV) == "1":
        print(
            f"pre-commit: project-data guard overridden by {OVERRIDE_ENV}=1",
            flush=True,
        )
        return 0
    try:
        repo_root = Path(
            _git(Path.cwd(), "rev-parse", "--show-toplevel")
            .decode("utf-8")
            .strip()
        )
        term_path = repo_root / Path(LOCAL_TERMS_PATH)
        terms = load_local_terms(term_path)
        if terms is None:
            print(disabled_terms_message(), flush=True)
            terms = []

        manifests = staged_bootstrap_manifests(repo_root)
        targets = manifest_write_targets(
            manifests, lambda path: staged_blob(repo_root, path)
        )
        violations: list[Violation] = []
        for path in staged_paths(repo_root):
            size = staged_blob_size(repo_root, path)
            content = None if size > MAX_STAGED_FILE_BYTES else staged_blob(repo_root, path)
            violations.extend(inspect_file(path, size, content, targets, terms))
    except (GuardInspectionError, OSError) as exc:
        print(f"pre-commit: guard inspection failed; refusing commit: {exc}", file=sys.stderr)
        return 1

    if violations:
        print(refusal_message(violations), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
