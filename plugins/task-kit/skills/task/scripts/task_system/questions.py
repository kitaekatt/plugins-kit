"""The question catalog: loading, layering, retirement and validation (summary-on-commit-plan.md, D1 and D5).

Shipped questions live in ``<skill>/questions/`` (``catalog.yaml`` lists the active ids, one ``<id>.yaml``
each). The fleet layer is a directory of ``<id>.yaml`` files (``~/.claude/config/task-kit/questions``): a file
whose id is shipped replaces the shipped question, a new id adds one, and ``retired: true`` drops the id.
Anything unusable is a ``QuestionError`` naming the file: an unknown key, reader or script, a reader a
prompt cannot use, a bad answer schema, a file name that differs from its id.

Answer schema subset (JSON schema): ``type`` (object, array, string, integer, number, boolean), ``enum``,
``required`` and ``properties`` and ``additionalProperties: false`` (objects; every object declares
``additionalProperties: false``), ``items``, ``minItems`` and ``maxItems`` (arrays), ``minLength`` and
``maxLength`` (strings), ``minimum`` and ``maximum`` (numbers), ``description``. The root is an object.

Grounding paths name string fields whose values must quote the inputs: ``field`` or ``outer.inner``, with
``[]`` after an array field to step into its elements (``decisions[].evidence``). ``validate_value`` and
``grounded_values`` apply the subset and the paths to an answer.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from . import answer_scripts
from .answers_types import Catalog, Question
from .question_inputs import READERS, canonical_json
from .types import HAND_OFF

CATALOG_FILE = "catalog.yaml"
SCHEMA_VERSION = "1"
ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
QUESTION_KEYS = frozenset((
    "_schema_version", "id", "version", "title", "applies_to", "inputs", "max_input_chars", "script",
    "prompt", "answer", "grounding", "models", "effort", "retired",
))
_REQUIRED_KEYS = ("_schema_version", "id", "version", "title", "applies_to", "inputs", "max_input_chars", "answer")
_CATALOG_KEYS = frozenset(("_schema_version", "version", "questions"))
_SCHEMA_TYPES = ("object", "array", "string", "integer", "number", "boolean")
_SCHEMA_KEYS = {
    "object": frozenset(("type", "description", "required", "properties", "additionalProperties")),
    "array": frozenset(("type", "description", "items", "minItems", "maxItems")),
    "string": frozenset(("type", "description", "enum", "minLength", "maxLength")),
    "integer": frozenset(("type", "description", "enum", "minimum", "maximum")),
    "number": frozenset(("type", "description", "enum", "minimum", "maximum")),
    "boolean": frozenset(("type", "description")),
}


class QuestionError(ValueError):
    """A question or catalog file is unusable. The message names the file."""


def default_shipped_dir() -> Path:
    """The questions directory shipped with the skill."""
    return Path(__file__).resolve().parents[2] / "questions"


def default_fleet_dir() -> Path:
    """The fleet layer's questions directory."""
    return Path.home() / ".claude" / "config" / "task-kit" / "questions"


# --- freezing ----------------------------------------------------------------


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def question_digest(definition: Mapping[str, Any]) -> str:
    """sha256 hex of the canonical definition: id, version, inputs, limit, script, prompt, answer schema and
    grounding. Title, applicability, models and effort are not part of it."""
    material = {key: _thaw(definition[key]) for key in (
        "id", "version", "inputs", "max_input_chars", "script", "prompt", "answer", "grounding",
    )}
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


# --- answer schema subset ----------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_schema(schema: Any, where: str = "answer") -> None:
    """Raise ValueError naming the schema path when ``schema`` leaves the supported subset."""
    if not isinstance(schema, dict):
        raise ValueError(f"{where}: a schema must be a mapping")
    kind = schema.get("type")
    if kind not in _SCHEMA_TYPES:
        raise ValueError(f"{where}: type must be one of {list(_SCHEMA_TYPES)}, not {kind!r}")
    unknown = sorted(set(schema) - _SCHEMA_KEYS[kind])
    if unknown:
        raise ValueError(f"{where}: keys {unknown} are outside the supported subset for type {kind}")
    if "enum" in schema and not (isinstance(schema["enum"], list) and schema["enum"]):
        raise ValueError(f"{where}: enum must be a non-empty list")
    for key in ("minItems", "maxItems", "minLength", "maxLength"):
        if key in schema and not (_is_int(schema[key]) and schema[key] >= 0):
            raise ValueError(f"{where}: {key} must be a non-negative integer")
    for key in ("minimum", "maximum"):
        if key in schema and not _is_number(schema[key]):
            raise ValueError(f"{where}: {key} must be a number")
    if kind == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError(f"{where}: properties must be a mapping")
        if schema.get("additionalProperties") is not False:
            raise ValueError(f"{where}: an object schema must declare additionalProperties: false")
        required = schema.get("required", [])
        if not (isinstance(required, list) and all(isinstance(name, str) for name in required)):
            raise ValueError(f"{where}: required must be a list of property names")
        missing = [name for name in required if name not in properties]
        if missing:
            raise ValueError(f"{where}: required names {missing} are not properties")
        for name, sub in properties.items():
            check_schema(sub, f"{where}.{name}")
    if kind == "array":
        if "items" not in schema:
            raise ValueError(f"{where}: an array schema must declare items")
        check_schema(schema["items"], f"{where}[]")


def validate_value(schema: Mapping[str, Any], value: Any, where: str = "answer") -> list[str]:
    """Errors (empty when ``value`` matches ``schema``), each prefixed with the value's path."""
    kind = schema["type"]
    errors: list[str] = []
    if kind == "object":
        if not isinstance(value, dict):
            return [f"{where}: expected an object"]
        properties = schema.get("properties", {})
        errors += [f"{where}: missing required {name!r}" for name in schema.get("required", ()) if name not in value]
        errors += [f"{where}: unexpected key {name!r}" for name in value if name not in properties]
        for name, sub in properties.items():
            if name in value:
                errors += validate_value(sub, value[name], f"{where}.{name}")
        return errors
    if kind == "array":
        if not isinstance(value, list):
            return [f"{where}: expected an array"]
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{where}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{where}: more than {schema['maxItems']} items")
        for index, item in enumerate(value):
            errors += validate_value(schema["items"], item, f"{where}[{index}]")
        return errors
    if kind == "string":
        if not isinstance(value, str):
            return [f"{where}: expected a string"]
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{where}: shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{where}: longer than {schema['maxLength']} characters")
    elif kind == "boolean":
        if not isinstance(value, bool):
            return [f"{where}: expected a boolean"]
    else:
        ok = _is_int(value) if kind == "integer" else _is_number(value)
        if not ok:
            return [f"{where}: expected {'an integer' if kind == 'integer' else 'a number'}"]
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{where}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{where}: above {schema['maximum']}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: not one of {list(schema['enum'])}")
    return errors


def _resolve_grounding(schema: Mapping[str, Any], path: str) -> None:
    """Raise ValueError unless ``path`` reaches a string field of ``schema``."""
    node = schema
    for segment in path.split("."):
        name, stepped = (segment[:-2], True) if segment.endswith("[]") else (segment, False)
        if node.get("type") != "object" or name not in node.get("properties", {}):
            raise ValueError(f"grounding path {path!r}: no field {name!r}")
        node = node["properties"][name]
        if stepped:
            if node.get("type") != "array":
                raise ValueError(f"grounding path {path!r}: {name!r} is not an array")
            node = node["items"]
    if node.get("type") != "string":
        raise ValueError(f"grounding path {path!r} must reach a string field")


def grounded_values(answer: Any, path: str) -> list[str]:
    """The string values a grounding path reaches in ``answer``; an absent optional field contributes none."""
    nodes = [answer]
    for segment in path.split("."):
        name, stepped = (segment[:-2], True) if segment.endswith("[]") else (segment, False)
        reached = [node[name] for node in nodes if isinstance(node, dict) and name in node]
        nodes = [item for node in reached for item in node] if stepped else reached
    return [node for node in nodes if isinstance(node, str)]


# --- loading -----------------------------------------------------------------


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise QuestionError(f"{path}: cannot read: {exc}") from exc


def _check_str_list(path: Path, data: Mapping[str, Any], key: str, *, non_empty: bool) -> tuple[str, ...]:
    value = data[key]
    if not (isinstance(value, list) and all(isinstance(item, str) and item for item in value)):
        raise QuestionError(f"{path}: {key} must be a list of non-empty strings")
    if non_empty and not value:
        raise QuestionError(f"{path}: {key} must not be empty")
    if len(set(value)) != len(value):
        raise QuestionError(f"{path}: {key} lists a name twice")
    return tuple(value)


def _build(path: Path, data: Mapping[str, Any]) -> Question:
    missing = [key for key in _REQUIRED_KEYS if key not in data]
    if missing:
        raise QuestionError(f"{path}: missing keys {missing}")
    question_id = data["id"]
    if not (isinstance(question_id, str) and ID_RE.match(question_id)):
        raise QuestionError(f"{path}: id must match {ID_RE.pattern}")
    if path.stem != question_id:
        raise QuestionError(f"{path}: the file name must be {question_id}.yaml")
    version = data["version"]
    if not (_is_int(version) and version >= 1):
        raise QuestionError(f"{path}: version must be a positive integer")
    title = data["title"]
    if not (isinstance(title, str) and title.strip()):
        raise QuestionError(f"{path}: title must be non-empty")
    applies_to = _check_str_list(path, data, "applies_to", non_empty=True)
    unknown_status = sorted(set(applies_to) - set(HAND_OFF.state_vocabulary))
    if unknown_status:
        raise QuestionError(f"{path}: applies_to names unknown statuses {unknown_status}")
    inputs = _check_str_list(path, data, "inputs", non_empty=True)
    unknown_readers = [name for name in inputs if name not in READERS]
    if unknown_readers:
        raise QuestionError(f"{path}: unknown readers {unknown_readers}; known readers: {sorted(READERS)}")
    script = data.get("script")
    if script is not None and script not in answer_scripts.script_names():
        raise QuestionError(f"{path}: unknown script {script!r}; known scripts: {list(answer_scripts.script_names())}")
    prompt = data.get("prompt")
    if prompt is not None and not (isinstance(prompt, str) and prompt.strip()):
        raise QuestionError(f"{path}: prompt must be null or non-empty text")
    if script is None and prompt is None:
        raise QuestionError(f"{path}: a question needs a script, a prompt, or both")
    script_only = [name for name in inputs if READERS[name].read is None]
    if script_only and prompt is not None:
        raise QuestionError(f"{path}: readers {script_only} are script-only and cannot feed a prompt")
    max_chars = data["max_input_chars"]
    if not (_is_int(max_chars) and max_chars > 0):
        raise QuestionError(f"{path}: max_input_chars must be a positive integer")
    needed = sum(READERS[name].limit or 0 for name in inputs)
    if max_chars < needed:
        raise QuestionError(f"{path}: max_input_chars {max_chars} is below the readers' limits, {needed}")
    schema = data["answer"]
    try:
        check_schema(schema)
        if schema["type"] != "object":
            raise ValueError("answer: the root schema must be an object")
    except ValueError as exc:
        raise QuestionError(f"{path}: {exc}") from exc
    grounding = _check_str_list(path, {"grounding": data.get("grounding", [])}, "grounding", non_empty=False)
    for ground in grounding:
        try:
            _resolve_grounding(schema, ground)
        except ValueError as exc:
            raise QuestionError(f"{path}: {exc}") from exc
    models = data.get("models")
    if models is not None:
        models = _check_str_list(path, data, "models", non_empty=True)
    effort = data.get("effort")
    if effort is not None and not (isinstance(effort, str) and effort):
        raise QuestionError(f"{path}: effort must be null or non-empty text")
    frozen_schema = _freeze(schema)
    return Question(
        id=question_id, version=version, title=title, applies_to=applies_to, inputs=inputs,
        max_input_chars=max_chars, script=script, prompt=prompt, answer_schema=frozen_schema,
        grounding=grounding, models=models, effort=effort, source=path,
        digest=question_digest({
            "id": question_id, "version": version, "inputs": inputs, "max_input_chars": max_chars,
            "script": script, "prompt": prompt, "answer": frozen_schema, "grounding": grounding,
        }),
    )


def load_question(path: Path, *, fleet: bool = False) -> Question | str:
    """The question in ``path``; for a retired fleet file, its id (a ``str``)."""
    path = Path(path)
    data = _read_yaml(path)
    if not isinstance(data, dict):
        raise QuestionError(f"{path}: the file must be a mapping")
    unknown = sorted(str(key) for key in set(data) - QUESTION_KEYS)
    if unknown:
        raise QuestionError(f"{path}: unknown keys {unknown}; known keys: {sorted(QUESTION_KEYS)}")
    if str(data.get("_schema_version")) != SCHEMA_VERSION:
        raise QuestionError(f"{path}: _schema_version must be {SCHEMA_VERSION!r}")
    retired = data.get("retired", False)
    if not isinstance(retired, bool):
        raise QuestionError(f"{path}: retired must be true or false")
    if retired:
        if not fleet:
            raise QuestionError(f"{path}: a shipped question is retired by removing it from {CATALOG_FILE}")
        question_id = data.get("id")
        if not (isinstance(question_id, str) and ID_RE.match(question_id) and path.stem == question_id):
            raise QuestionError(f"{path}: a retired question needs an id equal to the file name")
        return question_id
    return _build(path, data)


def load_catalog(shipped_dir: Path, fleet_dir: Path | None = None) -> Catalog:
    """The active catalog: the shipped questions, then the fleet layer applied over them."""
    shipped_dir = Path(shipped_dir)
    catalog_path = shipped_dir / CATALOG_FILE
    data = _read_yaml(catalog_path)
    if not isinstance(data, dict):
        raise QuestionError(f"{catalog_path}: the file must be a mapping")
    unknown = sorted(str(key) for key in set(data) - _CATALOG_KEYS)
    if unknown:
        raise QuestionError(f"{catalog_path}: unknown keys {unknown}")
    if str(data.get("_schema_version")) != SCHEMA_VERSION:
        raise QuestionError(f"{catalog_path}: _schema_version must be {SCHEMA_VERSION!r}")
    version = data.get("version")
    if not (_is_int(version) and version >= 1):
        raise QuestionError(f"{catalog_path}: version must be a positive integer")
    ids = data.get("questions")
    if not (isinstance(ids, list) and ids and all(isinstance(item, str) for item in ids) and len(set(ids)) == len(ids)):
        raise QuestionError(f"{catalog_path}: questions must be a non-empty list of distinct ids")
    unlisted = sorted(p.stem for p in shipped_dir.glob("*.yaml") if p.name != CATALOG_FILE and p.stem not in ids)
    if unlisted:
        raise QuestionError(f"{catalog_path}: shipped question files {unlisted} are not listed")
    questions: dict[str, Question] = {}
    for question_id in ids:
        file = shipped_dir / f"{question_id}.yaml"
        if not file.is_file():
            raise QuestionError(f"{catalog_path}: lists {question_id!r} but {file} does not exist")
        loaded = load_question(file)
        assert isinstance(loaded, Question)
        questions[question_id] = loaded
    retired: list[str] = []
    if fleet_dir is not None and Path(fleet_dir).is_dir():
        additions: dict[str, Question] = {}
        for file in sorted(Path(fleet_dir).glob("*.yaml")):
            loaded = load_question(file, fleet=True)
            if isinstance(loaded, str):
                questions.pop(loaded, None)
                additions.pop(loaded, None)
                retired.append(loaded)
            elif loaded.id in questions:
                questions[loaded.id] = loaded
            else:
                additions[loaded.id] = loaded
        questions.update(dict(sorted(additions.items())))
    return Catalog(version=version, questions=tuple(questions.values()), retired=tuple(retired))
