"""The task-export fixture world and the golden normalization (tests/task-kit/fixtures/task_export/).

The fixture is one parent directory holding two checkouts of one origin,
``alpha`` and ``alpha-two``, each a git repository whose ``dev/tasks`` is
ignored (so validation sees a git-ignored task root: no uncommitted-folder
warning). ``alpha/dev/tasks/.sessions/othermac.yaml`` is another machine's
working-session file.

Goldens are the export's documents with every path- or version-dependent
value replaced by a placeholder (:func:`normalize`), serialized with
:func:`golden_text`. ``TASK_EXPORT_REGENERATE=1`` rewrites them; review the
diff before committing one.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from task_system import answer_store, export, question_inputs, questions
from task_system.answers_types import Answerer
from task_system.discovery import read_task_block
from task_system.machine import Machine

FIXTURE = Path(__file__).with_name("fixtures") / "task_export"
GOLDEN = FIXTURE / "golden"
CHECKOUTS = ("alpha", "alpha-two")
ORIGIN = "https://example.invalid/acme/alpha.git"
NOW = datetime(2026, 10, 22, 15, 0, 0, tzinfo=timezone.utc)
LOCAL_NOW = datetime(2026, 10, 22, 15, 0, 0)  # naive local time the script answers read
MACHINE = Machine(host="thismac", fingerprint="fedcba9876543210", os_instance="macos")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def make_world(tmp_path: Path) -> Path:
    """Copy the fixture checkouts under ``tmp_path/parent`` as git repositories; return the parent."""
    parent = tmp_path / "parent"
    parent.mkdir()
    for name in CHECKOUTS:
        root = parent / name
        shutil.copytree(FIXTURE / name, root)
        _git(root, "init", "-q", "-b", "main")
        _git(root, "config", "user.email", "test@example.invalid")
        _git(root, "config", "user.name", "Test")
        _git(root, "config", "commit.gpgsign", "false")
        _git(root, "remote", "add", "origin", ORIGIN)
        (root / "README.md").write_text("fixture\n", encoding="utf-8")
        (root / ".gitignore").write_text("dev/tasks/\n", encoding="utf-8")
        _git(root, "add", "README.md", ".gitignore")
        _git(root, "commit", "-q", "-m", "init")
    write_current_answer(parent / "alpha" / "dev" / "tasks" / "wait-on-review")
    return parent


def shipped_catalog():
    """The shipped questions only, so a fleet layer on the machine running the tests cannot reach a golden."""
    return questions.load_catalog(questions.default_shipped_dir())


def write_current_answer(folder: Path) -> None:
    """This machine's orientation answer for ``folder``, current for the folder's present inputs."""
    question = shipped_catalog().get("orientation")
    fingerprint = answer_store.question_fingerprint(folder, read_task_block(folder), question)
    answer_store.write_answer(
        folder, "orientation", MACHINE,
        answer_store.NewAnswer(
            question_version=question.version,
            answer={"headline": "Wait for the build-export review", "state": "Blocked on build-export",
                    "next": "Start when build-export closes"},
            inputs_fingerprint=fingerprint,
            as_of=answer_store.AsOf(task_commit="3" * 40, code_commit=None),
            answerer=Answerer(kind="model", entry="example-endpoint", name=None, tier="free", effort="medium",
                              attempts=(("example-endpoint", "completed"),)),
            generated_at="2026-10-22T09:00:00-05:00",
        ),
    )


def inject() -> dict:
    """What a test passes to ``export_cli.document`` so the document is deterministic."""
    return {
        "resolve_machine": lambda: MACHINE, "now": lambda: NOW, "catalog": shipped_catalog(),
        "local_now": lambda: LOCAL_NOW,
    }


def normalize(doc: dict, parent: Path) -> dict:
    """The document with the parent path, checkout ids, version and the payload revision as placeholders."""
    replacements = {}
    for name in CHECKOUTS:
        replacements[export.checkout_id(parent / name)] = f"<checkout:{name}>"
    real_parent = os.path.realpath(parent)
    version = doc.get("task_kit_version")
    if isinstance(version, str) and version:
        replacements[f"({version})"] = "(<version>)"

    def fix(value):
        if isinstance(value, dict):
            return {key: fix(item) for key, item in value.items()}
        if isinstance(value, list):
            return [fix(item) for item in value]
        if isinstance(value, str):
            for old, new in replacements.items():
                value = value.replace(old, new)
            return value.replace(real_parent, "<parent>").replace(str(parent), "<parent>")
        return value

    fixed = fix(doc)
    fixed["task_kit_version"] = "<version>"
    for task in (fixed.get("payload") or {}).get("tasks", []) if isinstance(fixed.get("payload"), dict) else []:
        for entry in task.get("answers", {}).values():
            if entry["fingerprint"] is not None:
                entry["fingerprint"] = "<fingerprint>"  # moves with the question text; question tests own it
            if entry["as_of"] is not None and entry["as_of"]["task_commit"] is not None and entry["answerer"]["kind"] == "script":
                entry["as_of"]["task_commit"] = "<head>"  # a fixture repository's HEAD is not reproducible
    payload = fixed.get("payload")
    if isinstance(payload, dict) and "revision" in payload and "generated_at" in payload:
        payload["revision"] = "<revision>"
    return fixed


def golden_text(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=True, sort_keys=True, indent=2) + "\n"


def check_golden(name: str, doc: dict, parent: Path) -> None:
    path = GOLDEN / f"{name}.json"
    text = golden_text(normalize(doc, parent))
    if os.environ.get("TASK_EXPORT_REGENERATE") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    assert text == path.read_text(encoding="utf-8"), f"{path} differs from the export's document"


# -- a JSON Schema subset validator (the keywords task-export.schema.json uses) --------------


SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "task-kit" / "skills" / "task" / "references" / "task-export.schema.json"
)
_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool, "null": type(None),
}


def _type_ok(value, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, _TYPES[name])


def schema_errors(value, schema: dict, root: dict, where: str = "$") -> list[str]:
    """Violations of ``schema`` (type, const, required, properties, additionalProperties, items, minimum, $ref, oneOf)."""
    known = {"$schema", "$id", "title", "description", "$defs", "type", "const", "required", "properties",
             "additionalProperties", "items", "minimum", "$ref", "oneOf"}
    unknown = set(schema) - known
    if unknown:
        raise AssertionError(f"the test validator does not implement {sorted(unknown)} at {where}")
    if "$ref" in schema:
        name = schema["$ref"].removeprefix("#/$defs/")
        return schema_errors(value, root["$defs"][name], root, where)
    if "oneOf" in schema:
        matches = [option for option in schema["oneOf"] if not schema_errors(value, option, root, where)]
        return [] if len(matches) == 1 else [f"{where}: matches {len(matches)} of oneOf"]
    errors: list[str] = []
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(value, name) for name in names):
            return [f"{where}: {type(value).__name__} is not {names}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{where}: {value!r} != {schema['const']!r}")
    if "minimum" in schema and value < schema["minimum"]:
        errors.append(f"{where}: {value} < {schema['minimum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{where}: missing {key!r}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            errors.extend(f"{where}: unexpected {key!r}" for key in value if key not in properties)
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            for key, item in value.items():
                if key not in properties:
                    errors.extend(schema_errors(item, extra, root, f"{where}.{key}"))
        for key, sub in properties.items():
            if key in value:
                errors.extend(schema_errors(value[key], sub, root, f"{where}.{key}"))
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(schema_errors(item, schema["items"], root, f"{where}[{index}]"))
    return errors


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
