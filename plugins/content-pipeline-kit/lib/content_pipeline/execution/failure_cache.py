"""A negative cache of deterministic unit failures. Standard library only.

When a unit fails in a way that would fail again on the same inputs -- the
output cannot be rendered, repair attempts are exhausted -- the consumer
records the failure under its own ATTEMPT KEY: a hash of everything that
decides the attempt (inputs, prompt, model, effort, contract). The next run
looks the key up before spending a call. A hit means "this exact attempt
already failed"; the consumer skips the unit and reports the cached failure.
Any change to the attempt key is a miss, so a changed input, prompt or model
retries on its own.

Entries are keyed by ``(stage, unit_id)``: one entry per unit and stage,
replaced by the next record for that pair and removed by :meth:`clear` (call
it when the unit succeeds). :func:`error_fingerprint` gives a stable digest
of a normalized error, so a reader can tell "the same failure again" from a
new one; bump ``fingerprint_version`` when its normalization changes and
every older entry misses.

Record only deterministic failures. A deadline, an open circuit, a transport
or quota error, or backpressure is transient and must not be cached.

The file is ASCII JSON, schema version 1, replaced atomically. A missing
file is empty. An unreadable or malformed file is read as empty and
:attr:`FailureCache.report` names the problem: a lost negative cache costs
one repeated attempt per unit, not a stopped run. Writes from threads of one
process are serialized; two processes writing one file are
last-writer-wins.

Home: the consumer supplies ``path``; this module derives nothing from its own
location (``__file__``), and the atomic write puts its temporary file in the
target's directory. By the plugin data-home definitions the cache describes
the consuming project and is worth keeping across runs, so it is
PROJECT-DURABLE (``.plugin-data/<plugin>/``); a consumer that treats a lost
cache as acceptable may use the project-ephemeral ``.local-data`` instead.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence, Union

from content_pipeline.execution import _atomic_json

SCHEMA_VERSION = 1
FINGERPRINT_VERSION = 1
_FIELDS = (
    "stage",
    "unit_id",
    "attempt_key",
    "error_fingerprint",
    "fingerprint_version",
    "code",
    "summary",
)
_LOCK = threading.RLock()
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class FailureRecord:
    stage: str
    unit_id: str
    attempt_key: str
    error_fingerprint: str
    fingerprint_version: int
    code: str
    summary: str


def error_fingerprint(code: str, details: Sequence[Mapping[str, object]]) -> str:
    """SHA-256 of a normalized error: its code and its detail atoms.

    Each detail contributes ``code``, ``file`` and ``message``; whitespace
    in ``code`` and ``message`` is collapsed and the atoms are sorted, so
    reordered or rewrapped details give the same fingerprint.
    """
    atoms = [
        {
            "code": _collapse(detail.get("code", "")),
            "file": detail.get("file"),
            "message": _collapse(detail.get("message", "")),
        }
        for detail in details
    ]
    atoms.sort(key=lambda atom: (atom["code"], atom["file"] or "", atom["message"]))
    value = {"code": code, "details": atoms, "fingerprint_version": FINGERPRINT_VERSION}
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _collapse(value: object) -> str:
    return _WHITESPACE_RE.sub(" ", str(value)).strip()


class FailureCache:
    """Read and atomically update a schema-v1 negative failure cache."""

    def __init__(self, path: Union[Path, str]) -> None:
        self.path = Path(path)
        self.report: Optional[str] = None

    def lookup(
        self,
        stage: str,
        unit_id: str,
        attempt_key: str,
        fingerprint_version: int = FINGERPRINT_VERSION,
    ) -> Optional[FailureRecord]:
        """The cached failure for exactly this attempt, or ``None``."""
        with _LOCK:
            value = self._load()["failures"].get(_key(stage, unit_id))
            if value is None or value["attempt_key"] != attempt_key:
                return None
            if value["fingerprint_version"] != fingerprint_version:
                return None
            return FailureRecord(**value)

    def record(self, record: FailureRecord) -> None:
        _validate_record(record)
        with _LOCK:
            payload = self._load()
            payload["failures"][_key(record.stage, record.unit_id)] = asdict(record)
            _atomic_json.write(self.path, payload)

    def clear(self, stage: str, unit_id: str) -> None:
        """Forget the failure for one stage of one unit; other entries stay."""
        with _LOCK:
            payload = self._load()
            if payload["failures"].pop(_key(stage, unit_id), None) is not None:
                _atomic_json.write(self.path, payload)

    def _load(self) -> dict:
        empty = {"schema_version": SCHEMA_VERSION, "failures": {}}
        payload, self.report = _atomic_json.read(
            self.path, empty, _validate_payload, "failure cache"
        )
        return payload


def _key(stage: str, unit_id: str) -> str:
    return stage + "\0" + unit_id


def _validate_record(record: FailureRecord) -> None:
    if not isinstance(record, FailureRecord):
        raise TypeError("record must be a FailureRecord")
    for name in ("stage", "unit_id", "attempt_key", "error_fingerprint", "code", "summary"):
        if not isinstance(getattr(record, name), str):
            raise TypeError("%s must be a string" % name)
    if type(record.fingerprint_version) is not int:
        raise TypeError("fingerprint_version must be an integer")


def _validate_payload(payload: object) -> None:
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("wrong failure cache schema")
    failures = payload.get("failures")
    if not isinstance(failures, dict):
        raise ValueError("invalid failure cache entries")
    for key, value in failures.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            raise ValueError("invalid failure cache entry")
        if set(value) != set(_FIELDS):
            raise ValueError("invalid failure record fields")
        record = FailureRecord(**value)
        _validate_record(record)
        if key != _key(record.stage, record.unit_id):
            raise ValueError("failure cache key does not match its record")


__all__ = [
    "FailureRecord",
    "FailureCache",
    "error_fingerprint",
    "FINGERPRINT_VERSION",
]
