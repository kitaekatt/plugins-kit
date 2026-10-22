"""The answer store: records, ownership, the choice among records, coverage and modification order
(summary-on-commit-plan.md, D1, D3 and D5).

Layout. A model answer is one committed file, ``<task folder>/answers/<question id>/<host id>.yaml``. Every
machine writes only files named with its own host id that carry its own machine fingerprint (the ownership
check below), so two machines answering one question write different files and merge cleanly. Script answers
are computed on read (``answer_scripts``) and never stored.

Record (``_schema_version`` "1"; every key is required and no other key is allowed)::

    _schema_version: '1'
    host: studio                 # equals the file name
    machine: 3f9a0c1d2e4b5a6c    # machine.fingerprint
    os_instance: macos
    question: orientation
    question_version: 1
    answer: {...}                # matches the question's answer schema
    inputs_fingerprint: <64 hex>
    as_of: {task_commit: <sha>, code_commit: null}
    answerer: {kind: model, entry: qwen38-5090, tier: free, effort: medium,
               attempts: [{entry: qwen38-5090, outcome: completed}]}
    generated_at: '2026-10-21T19:05:12-05:00'   # informational; never used to choose

Choosing among the records of one question (any host). Every machine applies the same rule using nothing
machine-local, no configuration and no clock: ``current`` (fingerprint equals the current one) wins by lowest
host id, then lowest machine fingerprint; with no current record the ``stale`` winner comes from the
ancestry-maximal set of as-of commits (a record whose as-of commit is a proper ancestor of another record's is
dropped), then the same tie-break; a record whose as-of commit the repository does not hold ranks below every
record whose commit it holds. A file that does not parse, fails the record schema or the question's answer
schema, or whose ``host`` differs from its file name is ``invalid``: a note naming the file, never a finding
that gates work; valid records still count.

Nothing here reads ``answers/`` as an input: the readers of ``question_inputs`` do not, so writing an answer
changes no fingerprint. Nothing here commits or pushes; publication belongs to the engine and the publisher.
"""

from __future__ import annotations

import datetime
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from . import answer_scripts
from .answers_types import Answerer, Catalog, Question, RepoLog, ScriptContext
from .machine import HOST_ID_PATTERN, Machine
from .question_inputs import QuestionInputError, inputs_fingerprint, read_inputs
from .questions import ID_RE, validate_value

ANSWERS_DIRNAME = "answers"
SCHEMA_VERSION = "1"
GIT_TIMEOUT_S = 60.0

STATE_CURRENT = "current"
STATE_STALE = "stale"
STATE_MISSING = "missing"
STATE_INVALID = "invalid"
STATE_NOT_APPLICABLE = "not-applicable"
STATES = (STATE_CURRENT, STATE_STALE, STATE_MISSING, STATE_INVALID, STATE_NOT_APPLICABLE)

_RECORD_KEYS = (
    "_schema_version", "host", "machine", "os_instance", "question", "question_version", "answer",
    "inputs_fingerprint", "as_of", "answerer", "generated_at",
)
_AS_OF_KEYS = ("task_commit", "code_commit")
_ANSWERER_KEYS = ("kind", "entry", "tier", "effort", "attempts")
_OS_INSTANCES = ("macos", "linux", "linux-wsl", "windows")
_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_FINGERPRINT_16 = re.compile(r"^[0-9a-f]{16}$")
_FINGERPRINT_64 = re.compile(r"^[0-9a-f]{64}$")


class AnswerStoreError(RuntimeError):
    """The store cannot read or write: the message names the file or command and what to do."""


class AnswerOwnershipError(AnswerStoreError):
    """A write or deletion would touch a file this machine does not own."""


class _RecordError(ValueError):
    """A record file does not meet the record schema; the message is the reason."""


@dataclass(frozen=True)
class AsOf:
    task_commit: str | None  # the task repository's HEAD whose content was read
    code_commit: str | None  # set only by questions that read code inputs

    def as_dict(self) -> dict[str, str | None]:
        return {"task_commit": self.task_commit, "code_commit": self.code_commit}


@dataclass(frozen=True)
class NewAnswer:
    """What a writer supplies; the store adds the schema version, host, machine, OS instance and question."""

    question_version: int
    answer: Mapping[str, Any]
    inputs_fingerprint: str
    as_of: AsOf
    answerer: Answerer
    generated_at: str


@dataclass(frozen=True)
class AnswerRecord:
    """One valid record file as read."""

    path: Path
    host: str
    machine: str
    os_instance: str
    question: str
    question_version: int
    answer: Mapping[str, Any]
    inputs_fingerprint: str
    as_of: AsOf
    answerer: Answerer
    generated_at: str


@dataclass(frozen=True)
class InvalidRecord:
    path: Path
    reason: str

    @property
    def note(self) -> str:
        return f"{self.path}: {self.reason}"


@dataclass(frozen=True)
class AnswerState:
    """What one question says about one task.

    ``answer``, ``answerer``, ``as_of`` and ``fingerprint`` describe the chosen record (or the script answer,
    which has no fingerprint); all are None for ``missing``, ``invalid`` and ``not-applicable``. ``records``
    and ``invalid`` hold every file read, ``notes`` the human-readable problems. ``current_fingerprint`` is the
    fingerprint the inputs have now, whatever the state (None for a script answer or unreadable inputs).
    """

    question: str
    state: str
    answer: Mapping[str, Any] | None = None
    answerer: Answerer | None = None
    as_of: AsOf | None = None
    fingerprint: str | None = None
    host: str | None = None
    machine: str | None = None
    generated_at: str | None = None
    current_fingerprint: str | None = None  # what the inputs hash to now (model questions only)
    records: tuple[AnswerRecord, ...] = ()
    invalid: tuple[InvalidRecord, ...] = ()
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """The shape the export and the list projection carry for one question."""
        return {
            "state": self.state,
            "answer": None if self.answer is None else _thaw(self.answer),
            "answerer": None if self.answerer is None else answerer_dict(self.answerer),
            "as_of": None if self.as_of is None else self.as_of.as_dict(),
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class Coverage:
    catalog_version: int
    applicable: int  # catalog questions that apply to the task's status
    current: int  # of those, the ones whose state is current

    def as_dict(self) -> dict[str, int]:
        return {"catalog_version": self.catalog_version, "applicable": self.applicable, "current": self.current}


@dataclass(frozen=True)
class TaskAnswers:
    states: Mapping[str, AnswerState]  # one per catalog question, in catalog order
    coverage: Coverage
    notes: tuple[str, ...] = ()


# --- small helpers ------------------------------------------------------------


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def answerer_dict(answerer: Answerer) -> dict[str, Any]:
    return {
        "kind": answerer.kind,
        "entry": answerer.entry,
        "name": answerer.name,
        "tier": answerer.tier,
        "effort": answerer.effort,
        "attempts": [{"entry": entry, "outcome": outcome} for entry, outcome in answerer.attempts],
    }


def timestamp(now: datetime.datetime | None = None) -> str:
    """An ISO 8601 stamp with the local offset, to the second (the ``generated_at`` of a new answer)."""
    moment = now if now is not None else datetime.datetime.now().astimezone()
    return moment.replace(microsecond=0).isoformat()


def _git(top: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(top), *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AnswerStoreError(f"git {' '.join(args)} could not run in {top}: {exc}") from exc


def answers_dir(folder: Path, question_id: str) -> Path:
    return Path(folder) / ANSWERS_DIRNAME / question_id


def record_path(folder: Path, question_id: str, host: str) -> Path:
    return answers_dir(folder, question_id) / f"{host}.yaml"


# --- parsing ------------------------------------------------------------------


def _require_str(data: Mapping[str, Any], key: str, where: str, *, nullable: bool = False) -> str | None:
    value = data[key]
    if value is None and nullable:
        return None
    if not (isinstance(value, str) and value):
        raise _RecordError(f"{where}{key} must be {'null or ' if nullable else ''}non-empty text")
    return value


def _exact_keys(data: Any, keys: Sequence[str], where: str) -> Mapping[str, Any]:
    if not isinstance(data, dict):
        raise _RecordError(f"{where or 'the file'} must be a mapping")
    missing = [key for key in keys if key not in data]
    if missing:
        raise _RecordError(f"{where}missing keys {missing}" if where else f"missing keys {missing}")
    unknown = sorted(str(key) for key in set(data) - set(keys))
    if unknown:
        raise _RecordError(f"{where}unknown keys {unknown}" if where else f"unknown keys {unknown}")
    return data


def _parse_as_of(raw: Any) -> AsOf:
    data = _exact_keys(raw, _AS_OF_KEYS, "as_of: ")
    commits = []
    for key in _AS_OF_KEYS:
        value = data[key]
        if value is not None and not (isinstance(value, str) and _SHA.match(value)):
            raise _RecordError(f"as_of: {key} must be null or a full commit sha")
        commits.append(value)
    return AsOf(task_commit=commits[0], code_commit=commits[1])


def _parse_answerer(raw: Any) -> Answerer:
    data = _exact_keys(raw, _ANSWERER_KEYS, "answerer: ")
    if data["kind"] != "model":
        raise _RecordError("answerer: kind must be 'model' (script answers are never stored)")
    entry = _require_str(data, "entry", "answerer: ")
    tier = _require_str(data, "tier", "answerer: ", nullable=True)
    effort = _require_str(data, "effort", "answerer: ", nullable=True)
    attempts = data["attempts"]
    if not isinstance(attempts, list):
        raise _RecordError("answerer: attempts must be a list")
    pairs = []
    for index, attempt in enumerate(attempts):
        where = f"answerer: attempts[{index}]: "
        item = _exact_keys(attempt, ("entry", "outcome"), where)
        pairs.append((_require_str(item, "entry", where), _require_str(item, "outcome", where)))
    return Answerer(kind="model", entry=entry, name=None, tier=tier, effort=effort, attempts=tuple(pairs))


def parse_record(path: Path, text: str) -> AnswerRecord:
    """The record in ``text``; raises ``ValueError`` (a reason) when it does not meet the record schema.

    The file name is the host id: ``host`` must equal it. The question is the name of the file's folder.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _RecordError(f"does not parse: {exc}") from exc
    data = _exact_keys(data, _RECORD_KEYS, "")
    if str(data["_schema_version"]) != SCHEMA_VERSION:
        raise _RecordError(f"_schema_version must be {SCHEMA_VERSION!r}")
    host = _require_str(data, "host", "")
    if not HOST_ID_PATTERN.match(host):
        raise _RecordError(f"host {host!r} is not a host id")
    if host != path.stem:
        raise _RecordError(f"host {host!r} differs from the file name {path.name!r}")
    machine = _require_str(data, "machine", "")
    if not _FINGERPRINT_16.match(machine):
        raise _RecordError("machine must be 16 hex digits")
    os_instance = data["os_instance"]
    if os_instance not in _OS_INSTANCES:
        raise _RecordError(f"os_instance must be one of {list(_OS_INSTANCES)}")
    question = _require_str(data, "question", "")
    if question != path.parent.name:
        raise _RecordError(f"question {question!r} differs from the folder name {path.parent.name!r}")
    version = data["question_version"]
    if not (isinstance(version, int) and not isinstance(version, bool) and version >= 1):
        raise _RecordError("question_version must be a positive integer")
    answer = data["answer"]
    if not isinstance(answer, dict):
        raise _RecordError("answer must be a mapping")
    fingerprint = _require_str(data, "inputs_fingerprint", "")
    if not _FINGERPRINT_64.match(fingerprint):
        raise _RecordError("inputs_fingerprint must be 64 hex digits")
    generated_at = _require_str(data, "generated_at", "")
    try:
        datetime.datetime.fromisoformat(generated_at)
    except ValueError as exc:
        raise _RecordError(f"generated_at is not an ISO 8601 time: {exc}") from exc
    return AnswerRecord(
        path=path, host=host, machine=machine, os_instance=os_instance, question=question,
        question_version=version, answer=answer, inputs_fingerprint=fingerprint,
        as_of=_parse_as_of(data["as_of"]), answerer=_parse_answerer(data["answerer"]), generated_at=generated_at,
    )


def read_records(folder: Path, question_id: str) -> tuple[tuple[AnswerRecord, ...], tuple[InvalidRecord, ...]]:
    """Every ``*.yaml`` file of the question's folder: the valid records, and the files that are not.

    Dot files (a writer's temporary file) and other extensions are not records. Records come back in file-name
    order.
    """
    directory = answers_dir(folder, question_id)
    if not directory.is_dir():
        return (), ()
    records: list[AnswerRecord] = []
    invalid: list[InvalidRecord] = []
    for path in sorted(directory.iterdir()):
        if path.name.startswith(".") or path.suffix != ".yaml":
            continue
        try:
            if not path.is_file():
                raise _RecordError("is not a regular file")
            records.append(parse_record(path, path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError) as exc:
            invalid.append(InvalidRecord(path, f"cannot read: {exc}"))
        except _RecordError as exc:
            invalid.append(InvalidRecord(path, str(exc)))
    return tuple(records), tuple(invalid)


def question_dirs(folder: Path) -> tuple[str, ...]:
    """The question ids that have an answers folder in the task folder, sorted."""
    base = Path(folder) / ANSWERS_DIRNAME
    if not base.is_dir():
        return ()
    return tuple(sorted(entry.name for entry in base.iterdir() if entry.is_dir() and not entry.name.startswith(".")))


# --- ownership, writing and deleting ------------------------------------------


def _machine_of(text: str, where: Path) -> str:
    """The machine fingerprint a record file carries; raises AnswerOwnershipError when the file cannot say."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise AnswerOwnershipError(f"{where}: the file does not parse, so its owner cannot be proven: {exc}") from exc
    machine = data.get("machine") if isinstance(data, dict) else None
    if not (isinstance(machine, str) and _FINGERPRINT_16.match(machine)):
        raise AnswerOwnershipError(f"{where}: the file carries no machine fingerprint, so its owner cannot be proven")
    return machine


def _serialize(machine: Machine, question_id: str, new: NewAnswer) -> str:
    if new.answerer.kind != "model" or not new.answerer.entry:
        raise AnswerStoreError(f"{question_id}: only a model answer is stored (script answers are computed on read)")
    document = {
        "_schema_version": SCHEMA_VERSION,
        "host": machine.host,
        "machine": machine.fingerprint,
        "os_instance": machine.os_instance,
        "question": question_id,
        "question_version": new.question_version,
        "answer": _thaw(new.answer),
        "inputs_fingerprint": new.inputs_fingerprint,
        "as_of": new.as_of.as_dict(),
        "answerer": {
            "kind": "model",
            "entry": new.answerer.entry,
            "tier": new.answerer.tier,
            "effort": new.answerer.effort,
            "attempts": [{"entry": entry, "outcome": outcome} for entry, outcome in new.answerer.attempts],
        },
        "generated_at": new.generated_at,
    }
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=False, default_flow_style=False, width=1_000_000)


def write_answer(folder: Path, question_id: str, machine: Machine, new: NewAnswer) -> Path:
    """Write this machine's record of ``question_id`` for the task in ``folder``; return its path.

    The file is ``answers/<question id>/<host id>.yaml``. A file already there that carries another machine's
    fingerprint (two machines share a host id: set ``sessions.host`` on one) or whose owner cannot be proven
    is refused, naming the file, and left byte-identical. The record is validated by parsing it back before
    anything is written. The write is atomic; nothing is committed.
    """
    if not ID_RE.match(question_id):
        raise AnswerStoreError(f"{question_id!r} is not a question id")
    path = record_path(folder, question_id, machine.host)
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise AnswerOwnershipError(f"{path}: cannot be read, so its owner cannot be proven: {exc}") from exc
        owner = _machine_of(existing, path)
        if owner != machine.fingerprint:
            raise AnswerOwnershipError(
                f"{path} carries machine {owner}, not this machine ({machine.fingerprint}); two machines share the "
                f"host id {machine.host!r}: set sessions.host on one of them. The file was not touched."
            )
    text = _serialize(machine, question_id, new)
    try:
        parse_record(path, text)
    except _RecordError as exc:
        raise AnswerStoreError(f"{path}: the record to write is invalid: {exc}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _preimage(repo_top: Path | None, path: Path) -> str | None:
    """The file's text at HEAD (its committed preimage); None when HEAD does not hold it."""
    if repo_top is None:
        return None
    relative = path.resolve().relative_to(Path(repo_top).resolve()).as_posix()
    proc = _git(repo_top, "cat-file", "blob", f"HEAD:{relative}")
    return proc.stdout if proc.returncode == 0 else None


def delete_answer(
    folder: Path, question_id: str, machine: Machine, repo_top: Path | None, *, host: str | None = None
) -> Path | None:
    """Delete this machine's record file (``host`` defaults to this machine's host id); return its path.

    Ownership is checked on the committed preimage, the content the file had before the deleting commit: the
    blob at HEAD must carry this machine's fingerprint. When the working copy differs from it, that copy must
    carry this machine's fingerprint too. A file HEAD does not hold (never committed) is checked on its working
    content. Another machine's file, or one whose owner cannot be proven, is refused and left in place. Returns
    None when there is no such file. An emptied question folder (and ``answers/``) is removed.
    """
    path = record_path(folder, question_id, host if host is not None else machine.host)
    if not path.exists():
        return None
    try:
        working = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AnswerOwnershipError(f"{path}: cannot be read, so its owner cannot be proven: {exc}") from exc
    preimage = _preimage(repo_top, path)
    for label, text in (("committed preimage", preimage), ("working copy", working)):
        if text is None:
            continue
        owner = _machine_of(text, path)
        if owner != machine.fingerprint:
            raise AnswerOwnershipError(
                f"{path}: the {label} carries machine {owner}, not this machine ({machine.fingerprint}); "
                "another machine's answer is never deleted. The file was not touched."
            )
    path.unlink()
    for directory in (path.parent, path.parent.parent):
        try:
            directory.rmdir()
        except OSError:
            break
    return path


# --- choosing -----------------------------------------------------------------


def _commit_present(top: Path, sha: str | None) -> bool:
    if sha is None:
        return False
    return _git(top, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def _is_ancestor(top: Path, ancestor: str, descendant: str) -> bool:
    proc = _git(top, "merge-base", "--is-ancestor", ancestor, descendant)
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    raise AnswerStoreError(f"git merge-base --is-ancestor failed in {top}: {proc.stderr.strip()}")


def _tie_break(record: AnswerRecord) -> tuple[str, str]:
    return (record.host, record.machine)


def _stale_winner(records: Sequence[AnswerRecord], repo_top: Path | None) -> AnswerRecord:
    """The ancestry-maximal record (lowest host, then machine, among the maximal set)."""
    candidates = list(records)
    if repo_top is not None and len(candidates) > 1:
        present = [record for record in candidates if _commit_present(repo_top, record.as_of.task_commit)]
        if present:
            candidates = [
                record
                for record in present
                if not any(
                    other.as_of.task_commit != record.as_of.task_commit
                    and _is_ancestor(repo_top, record.as_of.task_commit, other.as_of.task_commit)
                    for other in present
                )
            ]
    return min(candidates, key=_tie_break)


def chosen_answer(
    folder: Path, question: Question, current_fp: str, repo_top: Path | None, *, status: str | None = None
) -> AnswerState:
    """The state of a model question for the task in ``folder`` given the current inputs fingerprint.

    ``status`` is the task's classification; one outside ``question.applies_to`` is ``not-applicable``.
    ``repo_top`` is the task repository's top (None outside a repository, where ancestry cannot be asked).
    """
    if status is not None and status not in question.applies_to:
        return AnswerState(question.id, STATE_NOT_APPLICABLE)
    records, invalid = read_records(folder, question.id)
    usable: list[AnswerRecord] = []
    bad = list(invalid)
    for record in records:
        problems = validate_value(question.answer_schema, record.answer)
        if problems:
            bad.append(InvalidRecord(record.path, f"the answer does not match the question's schema: {problems[0]}"))
        else:
            usable.append(record)
    notes = tuple(item.note for item in bad)
    if not usable:
        return AnswerState(
            question.id, STATE_INVALID if bad else STATE_MISSING, current_fingerprint=current_fp,
            invalid=tuple(bad), notes=notes,
        )
    current = [record for record in usable if record.inputs_fingerprint == current_fp]
    if current:
        winner, state = min(current, key=_tie_break), STATE_CURRENT
    else:
        winner, state = _stale_winner(usable, repo_top), STATE_STALE
    return AnswerState(
        question.id, state, answer=winner.answer, answerer=winner.answerer, as_of=winner.as_of,
        fingerprint=winner.inputs_fingerprint, host=winner.host, machine=winner.machine,
        generated_at=winner.generated_at, current_fingerprint=current_fp, records=tuple(usable),
        invalid=tuple(bad), notes=notes,
    )


def question_fingerprint(folder: Path, block: Mapping[str, Any] | None, question: Question) -> str:
    """The current inputs fingerprint of a model question for the task in ``folder``."""
    return inputs_fingerprint(question, read_inputs(folder, block, question))


def answer_state(
    question: Question,
    folder: Path,
    block: Mapping[str, Any] | None,
    status: str,
    *,
    repo_top: Path | None,
    log: RepoLog | None,
    now: datetime.datetime,
) -> AnswerState:
    """One question for one task: its script answers first, then the model records.

    ``log`` is the repository's grouped log (None when there is none), ``now`` the naive local read time.
    """
    folder = Path(folder)
    if status not in question.applies_to:
        return AnswerState(question.id, STATE_NOT_APPLICABLE)
    if question.script is not None:
        context = ScriptContext(folder=folder, repo_top=repo_top if repo_top is not None else folder, log=log, now=now)
        scripted = answer_scripts.answer(question.script, context)
        if scripted is not None:
            return AnswerState(
                question.id, STATE_CURRENT, answer=_thaw(scripted),
                answerer=Answerer(kind="script", entry=None, name=question.script, tier=None, effort=None, attempts=()),
                as_of=AsOf(task_commit=log.head if log is not None else None, code_commit=None),
            )
    if question.prompt is None:
        return AnswerState(question.id, STATE_MISSING)
    try:
        fingerprint = question_fingerprint(folder, block, question)
    except QuestionInputError as exc:
        return AnswerState(question.id, STATE_INVALID, notes=(f"{question.id}: the inputs cannot be read: {exc}",))
    return chosen_answer(folder, question, fingerprint, repo_top, status=status)


def coverage(catalog: Catalog, states: Mapping[str, AnswerState]) -> Coverage:
    """Of the catalog's questions that apply to the task, how many have a current answer."""
    applicable = current = 0
    for question in catalog.questions:
        state = states.get(question.id)
        if state is None or state.state == STATE_NOT_APPLICABLE:
            continue
        applicable += 1
        current += state.state == STATE_CURRENT
    return Coverage(catalog_version=catalog.version, applicable=applicable, current=current)


def task_answers(
    catalog: Catalog,
    folder: Path,
    block: Mapping[str, Any] | None,
    status: str,
    *,
    repo_top: Path | None,
    log: RepoLog | None,
    now: datetime.datetime,
) -> TaskAnswers:
    """Every catalog question's state for the task in ``folder``, its coverage and the notes."""
    states = {
        question.id: answer_state(question, folder, block, status, repo_top=repo_top, log=log, now=now)
        for question in catalog.questions
    }
    notes = tuple(note for state in states.values() for note in state.notes)
    return TaskAnswers(states=states, coverage=coverage(catalog, states), notes=notes)


ORIENTATION_QUESTION = "orientation"


def orientation_headline(states: Mapping[str, AnswerState]) -> tuple[str, str, str | None] | None:
    """(headline, state, generated date) of a usable orientation answer, current or stale; None otherwise.

    The headline is the one-line summary a task listing and the export serve. A date is None when the record's
    ``generated_at`` carries none.
    """
    state = states.get(ORIENTATION_QUESTION)
    if state is None or state.state not in (STATE_CURRENT, STATE_STALE) or state.answer is None:
        return None
    headline = state.answer.get("headline")
    if not (isinstance(headline, str) and headline.strip()):
        return None
    try:
        stamp = datetime.date.fromisoformat((state.generated_at or "")[:10]).isoformat()
    except ValueError:
        stamp = None
    return headline.strip(), state.state, stamp


# --- repository logs ------------------------------------------------------------


class RepoLogs:
    """One repository top per task root and one grouped log per repository, each read once.

    A git failure does not stop a listing or an export: it is recorded in ``notes`` (the caller surfaces them)
    and the folder reads as having no log, so the scripts that need one cannot answer.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tops: dict[Path, Path | None] = {}
        self._logs: dict[Path, RepoLog | None] = {}
        self._notes: list[str] = []

    @property
    def notes(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(dict.fromkeys(self._notes))

    def top_of(self, folder: Path) -> Path | None:
        """The top of the repository holding the task folder (looked up once per containing directory)."""
        key = Path(folder).parent.resolve()
        with self._lock:
            if key not in self._tops:
                try:
                    self._tops[key] = answer_scripts.repo_top_of(key)
                except answer_scripts.AnswerScriptError as exc:
                    self._notes.append(str(exc))
                    self._tops[key] = None
            return self._tops[key]

    def log_for(self, top: Path | None) -> RepoLog | None:
        if top is None:
            return None
        key = Path(top).resolve()
        with self._lock:
            if key not in self._logs:
                try:
                    self._logs[key] = answer_scripts.read_repo_log(top)
                except answer_scripts.AnswerScriptError as exc:
                    self._notes.append(str(exc))
                    self._logs[key] = None
            return self._logs[key]


# --- modification order -------------------------------------------------------


def modification_order(repo_top: Path, folders: Sequence[Path], log: RepoLog) -> list[Path]:
    """``folders`` most recently modified first.

    A folder's rank is the position, in the repository's topological log, of the newest commit touching a path
    in it outside its own ``answers/`` (``RepoLog.rank``); a smaller position is newer. Ties, and folders no
    commit touches, fall back to the folder path (the latter after every ranked folder). The order comes from
    the commit graph, never from a clock or a log.md stamp.
    """
    top = Path(repo_top).resolve()

    def key(folder: Path) -> tuple[bool, int, str]:
        relative = Path(folder).resolve().relative_to(top).as_posix()
        rank = log.rank(relative)
        return (rank is None, rank if rank is not None else 0, relative)

    return sorted((Path(folder) for folder in folders), key=key)
