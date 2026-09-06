"""The pass record: everything bootstrap learns, kept whether or not it is shown.

Bootstrap has three audiences with three different appetites. The USER wants a
few short lines. CLAUDE wants remediation directives. A person debugging a
machine six sessions later wants everything -- the check that passed, the
installer's stderr, the exact text each of the other two audiences was handed.

Those appetites used to fight, because ``bootstrap.log`` was simultaneously the
record AND an input to the display: the engine read the log back through a
marker and pasted it into the user-facing output, so anything unfit to display
had to be kept out of the log. Retention was a consequence of presentation, and
every UX decision quietly cost information.

This module is the other side of that split. ``bootstrap_events.jsonl`` is the
COMPLETE record -- every severity including passing checks, every failure dict
verbatim, the rendered payloads sent to Claude and the user, and console passes
that write nothing else. Because it is complete, presentation is free: a label
may be shortened, collated, truncated, or dropped entirely without losing
anything, since the full text is one ``grep`` away. That freedom is the entire
point of the file.

``bootstrap.log`` is unchanged and stays curated -- it is the file humans tail
and the one ``bootstrap_guard`` keys on, and a firehose would ruin both.

Stdlib-only, and best-effort throughout: every public entry point swallows its
own exceptions. A broken recorder must degrade to "no record", never to a failed
bootstrap pass.
"""

import json
import os
import re
import time
import uuid
from datetime import datetime, timezone

EVENTS_FILENAME = "bootstrap_events.jsonl"

#: Rotate at this size, keeping one previous generation (so the on-disk cost is
#: bounded at ~2x). Rotation is an atomic rename, which is safe while another
#: pass holds the file open in append mode: that writer keeps appending to the
#: renamed generation and loses nothing.
MAX_EVENTS_BYTES = 2 * 1024 * 1024

#: Ceiling on any single free-form detail value. A pathological payload (a
#: multi-megabyte installer log) must not be able to blow the rotation budget on
#: its own. This is a BOUND, not a UX truncation -- it is deliberately far
#: larger than anything worth reading in full, and it says so when it bites.
MAX_DETAIL_CHARS = 64 * 1024

#: Keys whose values are masked before anything reaches disk. Bootstrap's
#: condition categories include "user config: API keys", and failure messages
#: embed observed values verbatim (env_var mismatches quote both the current and
#: wanted value), so a more retentive record is also a bigger secret-exposure
#: surface. Masking happens at RECORD time, never at render time: a secret must
#: not exist in the file at all.
_SECRET_KEY_RE = re.compile(r"(?i)(key|token|secret|passw|credential|api[_-]?k)")

#: Secret-shaped substrings inside otherwise-innocent text blobs (subprocess
#: output, command lines). Heuristic by nature -- it catches the common shapes,
#: not every possible one.
_SECRET_TEXT_RES = (
    # Consumes the WHOLE header value, not the first token: an
    # "Authorization: Bearer <token>" masked at \S+ eats "Bearer" and leaves the
    # secret sitting in the clear.
    re.compile(r"(?i)(authorization\s*[:=]\s*)([^\r\n'\"]+)"),
    # NOTE the surrounding [\w.-]* rather than a \b anchor. The real-world shape
    # is an env var -- ANTHROPIC_API_KEY=, GH_TOKEN=, MY_SECRET= -- and \b never
    # matches inside those, because "_" is a word character, so there is no
    # boundary before "API" in "ANTHROPIC_API_KEY". A \b-anchored pattern
    # therefore masks the toy form (api_key=x) and misses every form bootstrap
    # actually handles.
    # The (?<![\w.-]) anchor is a PERFORMANCE guard, not a semantic one: without
    # it the engine retries the greedy [\w.-]* prefix from every offset inside a
    # long word-character run, which is quadratic (a 64KB subprocess blob -- well
    # within MAX_DETAIL_CHARS -- cost ~60s to redact). Anchoring the prefix to a
    # token boundary matches exactly the same strings, because the prefix class
    # IS the boundary class: any start the anchor rejects lies inside a token
    # whose own start already absorbs those characters into [\w.-]*.
    re.compile(r"(?i)(?<![\w.-])([\w.-]*(?:api[_-]?key|access[_-]?token|token|secret"
               r"|password|passwd|credential)[\w.-]*\s*[=:]\s*)(\S+)"),
    re.compile(r"(?i)(--(?:password|token|api-key)[= ])(\S+)"),
)

MASK = "***REDACTED***"


def redact(value, _key=None):
    """Mask secret-shaped data anywhere in ``value`` (recursive, structure-preserving).

    Two rules: a mapping key that LOOKS like a secret has its whole value
    masked, and free text has secret-shaped substrings masked in place. Both are
    heuristics and are documented as such -- they raise the cost of a leak, they
    do not prove its absence.
    """
    if isinstance(value, dict):
        return {k: redact(v, _key=k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, _key=_key) for v in value]
    if isinstance(value, str):
        if _key is not None and _SECRET_KEY_RE.search(str(_key)):
            return MASK
        out = value
        for pattern in _SECRET_TEXT_RES:
            out = pattern.sub(lambda m: m.group(1) + MASK, out)
        if len(out) > MAX_DETAIL_CHARS:
            dropped = len(out) - MAX_DETAIL_CHARS
            out = (out[:MAX_DETAIL_CHARS]
                   + f"\n[record: {dropped} more chars omitted at the "
                     f"{MAX_DETAIL_CHARS}-char per-value bound]")
        return out
    return value


def pass_id(start_time, pid=None):
    """Correlation id for one engine invocation: start time + pid.

    The pid disambiguates the rare case of two passes appending to the same
    file, which the engine-wide lock makes unusual but not impossible (an early
    stand-down records before the lock is resolved).
    """
    stamp = (start_time or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}.{pid if pid is not None else os.getpid()}"


class PassRecorder:
    """Buffers one pass's records and appends them as JSON lines.

    Buffered rather than written per-event so a pass costs two file writes
    instead of hundreds, and so a single ``write()`` in append mode keeps each
    pass's block contiguous even when another pass appends concurrently (the
    same O_APPEND discipline ``log.py`` uses).
    """

    def __init__(self, data_dir, start_time=None, mode="hook", autoflush=True):
        self.data_dir = data_dir
        self._started_at = (start_time or datetime.now(timezone.utc)).timestamp()
        self.pass_id = pass_id(start_time)
        self.mode = mode
        self._buf = []
        self._seq = 0
        self.enabled = True
        # The engine has many early-return paths (unsupported platform, lock
        # stand-down, transient-retry deferral) and a containment wrapper that
        # swallows crashes. Registering the flush at exit means the record
        # survives all of them without threading a try/finally through a
        # 6000-line function -- and a pass that died is precisely the one whose
        # record matters most.
        if autoflush:
            try:
                import atexit
                atexit.register(self.flush)
            except Exception:
                pass

    # -- recording ------------------------------------------------------- #

    def record(self, kind, text="", sev=None, *, plugin=None, section=None,
               display=None, detail=None, failure=None, **extra):
        """Append one record. Never raises; never truncates ``text``.

        ``text`` is the complete line -- the diagnostic, at whatever length it
        needs to be. ``display`` is the optional short form for collated
        surfaces; when absent the renderer derives one. They are stored
        separately on purpose: the short form is a projection, and a projection
        must never overwrite its source.
        """
        if not self.enabled:
            return
        try:
            self._seq += 1
            rec = {
                "pass": self.pass_id,
                "seq": self._seq,
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "mode": self.mode,
                "kind": kind,
            }
            if sev:
                rec["sev"] = sev
            if plugin:
                rec["plugin"] = plugin
            if section:
                rec["section"] = section
            if text:
                rec["text"] = redact(str(text))
            if display:
                rec["display"] = redact(str(display))
            if detail is not None:
                rec["detail"] = redact(detail)
            if failure is not None:
                rec["failure"] = redact(failure)
            for key, value in extra.items():
                if value is not None:
                    rec[key] = redact(value, _key=key)
            self._buf.append(rec)
        except Exception:
            pass

    def record_entry(self, sev, text, **kw):
        """Convenience for the ok/action/quiet/fail entry classes."""
        self.record("check", text, sev=sev, **kw)

    def record_emit(self, channel, response):
        """Record a rendered hook payload verbatim.

        This is the half that reached no log at all before: what Claude was told
        (``additionalContext``) and what the user was shown (``systemMessage``)
        were rendered, emitted, and forgotten. Recording them is what makes an
        aggressively shortened user surface safe -- the exact text both audiences
        received stays reconstructable.
        """
        try:
            payload = response if isinstance(response, dict) else {"raw": str(response)}
            hook = payload.get("hookSpecificOutput") or {}
            self.record(
                "emit", channel=channel,
                system_message=payload.get("systemMessage"),
                additional_context=hook.get("additionalContext"),
                hook_event=hook.get("hookEventName"),
            )
        except Exception:
            pass

    # -- persistence ------------------------------------------------------ #

    def flush(self):
        """Write buffered records and clear the buffer. Never raises."""
        if not self._buf:
            return
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            path = os.path.join(self.data_dir, EVENTS_FILENAME)
            self._rotate_if_needed(path, self._started_at)
            blob = "".join(json.dumps(r, default=str) + "\n" for r in self._buf)
            with open(path, "a", encoding="utf-8") as f:
                f.write(blob)
        except Exception:
            pass
        finally:
            self._buf = []

    def _rotate_if_needed(self, path, started_at=None):
        """Keep one previous generation, by an atomic claim and rename.

        First claim the current file with a unique temporary name. A second
        flusher that observed the same threshold then either sees no current
        file or cannot replace the already-retained generation: the retained
        file's mtime records the winner's rotation. This keeps concurrent
        flushers from deleting the generation the first one retained, without
        introducing a new locking primitive.
        """
        if started_at is None:
            started_at = self._started_at
        try:
            if os.path.getsize(path) < MAX_EVENTS_BYTES:
                return
        except OSError:
            return

        retained = path + ".1"
        temporary = f"{path}.{os.getpid()}.{uuid.uuid4().hex}.rotate"
        try:
            os.replace(path, temporary)
        except OSError:
            return

        try:
            retained_mtime = os.path.getmtime(retained)
        except OSError:
            retained_mtime = None

        if (retained_mtime is not None
                and started_at is not None
                and retained_mtime >= started_at):
            try:
                os.replace(temporary, path)
            except OSError:
                pass
            return

        try:
            # The source file's old mtime would make a concurrent pre-existing
            # flusher look newer than the retained generation. Mark this
            # generation before publishing it so the check above is useful.
            now = time.time()
            os.utime(temporary, (now, now))
            os.replace(temporary, retained)
        except OSError:
            try:
                os.replace(temporary, path)
            except OSError:
                pass


class Entry(str):
    """A log entry carrying its short form and its structured detail.

    A str subclass rather than a wrapper object on purpose: entries are
    concatenated, formatted, written to the log, compared in tests, and copied
    with ``list(...)`` in a dozen places, and all of that keeps working
    unchanged. Only the renderer looks at ``.short`` and only the recorder at
    ``.detail``; everything else sees the full text it always saw.

    Carrying BOTH on the entry is what makes them survive the trip. Phase
    helpers build a plain local list, and the caller extends a RecordingList
    with it -- so an attribute that lived on the list (rather than on the item)
    would be lost in transit, and the recording would happen without it.
    """

    __slots__ = ("short", "detail")

    def __new__(cls, text, short=None, detail=None):
        obj = super().__new__(cls, text)
        obj.short = short
        obj.detail = detail
        return obj


def short_form(item):
    """The authored short label for an entry, or None."""
    return getattr(item, "short", None)


def detail_of(item):
    """The structured detail attached to an entry, or None."""
    return getattr(item, "detail", None)


def reprefix(item, prefix):
    """Prefix an entry's text, preserving its short form and detail.

    ``f"config: {entry}"`` yields a plain ``str`` and silently drops both --
    which is how an authored ``display=`` label in the layered-config and env
    phases went missing while the width test, seeing the kwarg at the call site,
    reported the site as covered.
    """
    short = short_form(item)
    return Entry(f"{prefix}{item}",
                 short=f"{prefix}{short}" if short else None,
                 detail=detail_of(item))


class RecordingList(list):
    """A log-entry list that mirrors every append into the pass record.

    The engine collects entries by appending to plain lists -- from the ctx
    methods, and from dozens of sites that hold a reference and append directly.
    Recording at the LIST rather than at each call site is what makes the record
    complete without touching ~129 callers: there is exactly one place an entry
    can enter, and this is it.

    Everything else about these lists is unchanged, so any code that reads,
    slices, or concatenates them is unaffected.
    """

    __slots__ = ("_recorder", "_sev", "_plugin", "_section")

    def __init__(self, recorder=None, sev="action", plugin=None, section=None,
                 initial=()):
        super().__init__(initial)
        self._recorder = recorder
        self._sev = sev
        self._plugin = plugin
        self._section = section

    def append(self, item):
        super().append(item)
        self._record_item(item)

    def _record_item(self, item):
        if self._recorder is not None:
            # An Entry may already carry a short form and detail from a phase
            # helper that built a plain list; read them off the item so an
            # extend() into this list records everything the entry knows.
            self._recorder.record_entry(
                self._sev, item, plugin=self._plugin, section=self._section,
                display=short_form(item), detail=detail_of(item))

    def extend(self, items):
        for item in items:
            self.append(item)

    def __iadd__(self, items):
        self.extend(items)
        return self

    def insert(self, index, item):
        super().insert(index, item)
        self._record_item(item)

    def __setitem__(self, index, value):
        if isinstance(index, slice):
            replacement = list(value)
            super().__setitem__(index, replacement)
            for item in replacement:
                self._record_item(item)
            return
        super().__setitem__(index, value)
        self._record_item(value)

    def append_rich(self, item, display=None, detail=None):
        """Append with a short display form and/or structured detail.

        The escape hatch for the two things a bare string cannot carry: a label
        authored for a width-constrained surface, and the subprocess output that
        explains the line. The short label rides along on the entry itself (see
        Entry), so it survives the ``list(...)`` copies that build the display
        sections.
        """
        super().append(Entry(item, short=display, detail=detail)
                       if (display or detail) else item)
        if self._recorder is not None:
            self._recorder.record_entry(
                self._sev, item, plugin=self._plugin, section=self._section,
                display=display, detail=detail)


def entry_list(recorder, sev, plugin=None, section=None, initial=()):
    """Build a RecordingList, or a plain list when there is no recorder."""
    if recorder is None:
        return list(initial)
    return RecordingList(recorder, sev, plugin=plugin, section=section,
                         initial=initial)
