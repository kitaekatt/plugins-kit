"""Per-machine consent for cost classes (registry layer).

The consent file records which non-free cost classes (usage, spend) the user
allows callers to use. Free is always allowed. Paths are resolved on each call
(never at import).

Location, highest precedence first (:func:`consent_read_path`):

1. ``$LLM_KIT_CONSENT_FILE`` when set: that file alone, read and written.
2. ``$XDG_CONFIG_HOME/llm-kit/consent.json`` (fallback
   ``$HOME/.config/llm-kit/consent.json``) when it exists: the neutral file.
3. the legacy file an earlier release kept under ``~/.claude``
   (:func:`legacy_consent_path`), read only when the neutral file does not exist.
4. none: free-only.

A write (:func:`save_consent`) always targets 1 or 2 and never the legacy file,
so the first ``llm-kit consent set`` on a machine that still holds a legacy file
carries its values into the neutral file, which then shadows the legacy one. A
read creates nothing.

File format: ``{"schema": 1, "usage": bool, "spend": bool, "updated_at": iso}``.
Only real booleans are accepted. An absent, unreadable or malformed file means
free-only, with ``Consent.note`` explaining why. Callers that opt in must print
``Consent.note`` themselves; the dispatch floor does not carry it.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from .user_paths import ENV_CONSENT_FILE, neutral_consent_file, override_path

# Cost class names. Spelled here as literals because this module stays
# independent of registry/cost.py (the two are siblings in layer 0).
_FREE, _USAGE, _SPEND = "free", "usage", "spend"

SCHEMA = 1
REMEDY = "run `llm-kit consent set --usage on` to allow usage-billed models"


def legacy_consent_path() -> Path:
    """``Path.home()/.claude/plugins/data/plugins-kit/llm-kit/consent.json``: the read-only legacy location."""
    return Path.home() / ".claude" / "plugins" / "data" / "plugins-kit" / "llm-kit" / "consent.json"


def default_consent_path(environ: Optional[Mapping[str, str]] = None) -> Path:
    """Where consent is WRITTEN: ``$LLM_KIT_CONSENT_FILE``, else the neutral ``consent.json``."""
    return override_path(ENV_CONSENT_FILE, environ) or neutral_consent_file(environ)


def consent_read_path(environ: Optional[Mapping[str, str]] = None) -> Path:
    """The file :func:`load_consent` reads, by the precedence in the module docstring.

    The write target (:func:`default_consent_path`) when it is an explicit
    choice (the override, or any path other than the neutral default) or when
    anything exists at it (an unreadable neutral file is reported, never
    skipped for the legacy one); else the legacy file when it exists; else the
    neutral path, which is absent. Touches nothing.
    """
    primary = default_consent_path() if environ is None else default_consent_path(environ)
    if override_path(ENV_CONSENT_FILE, environ) is not None or primary != neutral_consent_file(environ):
        return primary
    if os.path.lexists(primary):
        return primary
    legacy = legacy_consent_path()
    if os.path.lexists(legacy):
        return legacy
    return primary


@dataclass(frozen=True)
class Consent:
    """The loaded consent. ``source`` is ``file``, ``absent`` or ``unreadable``."""

    usage: bool
    spend: bool
    source: str
    path: Path
    note: str

    @property
    def allowed(self) -> frozenset:
        """The allowed cost classes: always ``free``, plus ``usage``/``spend`` when consented."""
        classes = {_FREE}
        if self.usage:
            classes.add(_USAGE)
        if self.spend:
            classes.add(_SPEND)
        return frozenset(classes)


def _free_only(source: str, path: Path, why: str) -> Consent:
    note = f"consent: {why} ({path}); only free models are allowed; {REMEDY}"
    return Consent(usage=False, spend=False, source=source, path=path, note=note)


def load_consent(path: Optional[Path] = None) -> Consent:
    """Load consent; catches OSError, decode and schema errors -> free-only plus a note."""
    target = Path(path) if path is not None else consent_read_path()
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return _free_only("absent", target, "no consent file")
    except OSError as exc:
        return _free_only("unreadable", target, f"consent file cannot be read ({exc.__class__.__name__})")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return _free_only("unreadable", target, f"consent file is not valid JSON ({exc.__class__.__name__})")
    if not isinstance(data, dict):
        return _free_only("unreadable", target, "consent file is not a JSON object")
    schema = data.get("schema")
    if type(schema) is not int or schema != SCHEMA:
        return _free_only("unreadable", target, f"consent file schema is not {SCHEMA}")
    usage, spend = data.get("usage"), data.get("spend")
    if type(usage) is not bool or type(spend) is not bool:
        return _free_only("unreadable", target, "consent file 'usage' and 'spend' must be true or false")
    return Consent(usage=usage, spend=spend, source="file", path=target, note="")


def save_consent(*, usage: bool, spend: bool, path: Optional[Path] = None) -> Path:
    """Atomically write the consent file (tmp file in the same directory, then replace).

    Errors propagate. Returns the path written.
    """
    if type(usage) is not bool or type(spend) is not bool:
        raise TypeError("usage and spend must be bool")
    target = Path(path) if path is not None else default_consent_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": SCHEMA,
        "usage": usage,
        "spend": spend,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".consent.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return target
