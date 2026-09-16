"""Pure lint/hint helpers for bare Python in bootstrap manifest commands.

Stdlib and sibling imports only. The interpreter names, the /bootstrap fact
id, and the reference document come from :mod:`interpreter_env`, so every
message names exactly what the docs and the engine export.

Three pure functions (plus :func:`lint_manifest_hits`, the structured form of
:func:`lint_manifest` the engine uses to author a short display label):

- :func:`lint_command` -- does one opaque shell command string invoke a bare
  Python interpreter (``python``, ``python3``, ``py``, ``pythonw``, a
  versioned ``python3.NN``, or ``uv run ... python`` without ``--project``)
  in a command-word position? A command that names any absolute path or any
  ``$VAR`` is never linted: its author chose where things live, and nothing
  nags a manifest that chose its own interpreter.
- :func:`lint_manifest` -- walks every command-bearing field of a parsed
  bootstrap.json manifest and returns one lint message per bare-Python hit.
- :func:`python_hint_for_output` -- does captured command output look like a
  "python not found" failure, worth hinting the manifest author toward
  ``BOOTSTRAP_PYTHON``?

The engine decides severity by boundary: a shipped plugin manifest's hit is a
displayed action entry, a layered manifest's is a log-only entry, and the
failure hint is appended wherever a command already failed.
"""

import re
from typing import NamedTuple

from .interpreter_env import (
    CALL_SITE_EXPR,
    ENGINE_VAR,
    FACT_ID,
    MIN_VERSION_HINT,
    PLUGIN_CALL_SITE_EXPR,
    PROJECT_VAR,
    REFERENCE_DOC,
    REFERENCE_HEADING,
)

# Both copy-paste forms carry MIN_VERSION_HINT through the ":?" expansion.
if not (MIN_VERSION_HINT in PLUGIN_CALL_SITE_EXPR and ENGINE_VAR in PLUGIN_CALL_SITE_EXPR
        and MIN_VERSION_HINT in CALL_SITE_EXPR and PROJECT_VAR in CALL_SITE_EXPR):
    raise ImportError("interpreter_env call-site expressions lost their names or hint")

_SEE_ALSO = f"see /bootstrap fact {FACT_ID} and {REFERENCE_DOC}"

_HINT_MESSAGE = (
    f"hint: python was not found -- invoke Python as {PLUGIN_CALL_SITE_EXPR} "
    f"(project code: {CALL_SITE_EXPR}) rather than bare python/python3; "
    f'{_SEE_ALSO} ("{REFERENCE_HEADING}")'
)

# ---------------------------------------------------------------------------
# lint_command
# ---------------------------------------------------------------------------

_OPERATOR_CHARS = set(";&|({!")

_LEADING_WORDS = {
    "if", "then", "do", "else", "elif", "exec", "env", "sudo", "nohup",
    "time", "timeout", "xargs", "command",
}

_BAD_EXACT = {"python": "python", "python3": "python3", "py": "py", "pythonw": "pythonw"}
_PY3_VERSIONED_RE = re.compile(r"^python3\.\d+$")
_VAR_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_NUMERIC_ARG_RE = re.compile(r"^\d+[A-Za-z]*$")
# A variable reference ($NAME or ${...}); "$(" is command substitution, not a
# variable, and does not exempt a command.
_VAR_REF_RE = re.compile(r"\$(?:[A-Za-z_]|\{)")
# A word that is an absolute path: POSIX (/x), home-anchored (~/x), or a
# Windows drive path (C:/x, C:\x), optionally opened by a quote.
_ABS_PATH_WORD_RE = re.compile(r"""^["']?(?:/[^/\s]|~/|[A-Za-z]:[\\/])""")
# A redirection word (">", "2>", ">>", "<"): the path after it says where
# output goes (/dev/null above all), not a location the command works with.
_REDIRECT_WORD_RE = re.compile(r"^\d*(?:>>?|<)$")


def _tokenize(text: str) -> list[tuple[str, str]]:
    """Split a shell command string into ``("word" | "op", value)`` tokens.

    Operator characters (``; & | ( \\` { !``) always split, quoted, or not;
    a quoted run (single or double) is kept as one atomic word token so
    internal whitespace/operators never split it; an unquoted ``#`` at a
    word boundary starts a comment that runs to the next newline; a literal
    newline is additionally emitted as its own ``"\\n"`` operator token so a
    line start is recognized as a command-word position like any other
    operator boundary.
    """
    tokens: list[tuple[str, str]] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    i = 0
    n = len(text)

    def flush() -> None:
        if buf:
            tokens.append(("word", "".join(buf)))
            buf.clear()

    while i < n:
        c = text[i]
        if in_single:
            buf.append(c)
            if c == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            buf.append(c)
            if c == '"':
                in_double = False
            i += 1
            continue
        if c == "'":
            in_single = True
            buf.append(c)
            i += 1
            continue
        if c == '"':
            in_double = True
            buf.append(c)
            i += 1
            continue
        if c == "#" and not buf:
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if c == "\n":
            flush()
            tokens.append(("op", "\n"))
            i += 1
            continue
        if c in (" ", "\t", "\r"):
            flush()
            i += 1
            continue
        if c in _OPERATOR_CHARS:
            flush()
            tokens.append(("op", c))
            i += 1
            continue
        buf.append(c)
        i += 1
    flush()
    return tokens


def _is_skippable_arg(word: str) -> bool:
    """A flag, ``VAR=val`` assignment, or bare numeric/duration argument --
    the tokens a leading word like ``env`` or ``timeout`` takes before its
    real command word."""
    return bool(
        word.startswith("-")
        or _VAR_ASSIGN_RE.match(word)
        or _NUMERIC_ARG_RE.match(word)
    )


def _resolve_command_word(tokens: list[tuple[str, str]], pos: int) -> int:
    """Walk past a chain of leading words (``if``, ``env FOO=1``, ``timeout
    5``, ...) to the index of the actual command word they introduce."""
    n = len(tokens)
    while pos < n and tokens[pos][0] == "word" and tokens[pos][1] in _LEADING_WORDS:
        pos += 1
        while pos < n and tokens[pos][0] == "word" and _is_skippable_arg(tokens[pos][1]):
            pos += 1
    return pos


def _check_uv_run(tokens: list[tuple[str, str]], pos: int) -> str | None:
    """``tokens[pos]`` is the word "uv" immediately followed by "run" --
    scan forward for a bare ``python`` target, honoring ``--project`` as an
    escape."""
    n = len(tokens)
    i = pos + 2
    saw_project = False
    while i < n and tokens[i][0] == "word":
        word = tokens[i][1]
        if word == "--project" or word.startswith("--project="):
            saw_project = True
        if word == "python":
            return None if saw_project else "uv run python"
        i += 1
    return None


def _names_own_location(tokens: list[tuple[str, str]]) -> bool:
    """Whether a command names any ``$VAR`` or any absolute path.

    Such a command is never linted (see the module docstring). A redirection
    target (``> /dev/null``) does not count: it says where output goes, and
    exempting it would exempt the most common bare-python check of all.
    ``2>/dev/null`` is one word that does not start with a path, so it never
    counts either.
    """
    previous = ""
    for kind, value in tokens:
        if kind != "word":
            previous = ""
            continue
        if _VAR_REF_RE.search(value):
            return True
        redirected = bool(_REDIRECT_WORD_RE.match(previous))
        if not redirected and _ABS_PATH_WORD_RE.match(value):
            return True
        previous = value
    return False


def lint_command(text: str) -> str | None:
    """Return the offending token if ``text`` invokes a bare Python
    interpreter in a command-word position, else ``None``.

    Offending tokens: ``"python"``, ``"python3"``, ``"py"``, ``"pythonw"``,
    ``"python3.NN"`` (any versioned ``python3.<digits>``), or
    ``"uv run python"`` (any ``uv run [flags] python`` without
    ``--project``).

    A command word is examined only at a command-word position: the start
    of ``text``, immediately after one of the operator characters
    ``; && || | & ( \\` $( { !`` or a newline, or immediately after (a
    chain of) the leading words ``if then do else elif exec env sudo nohup
    time timeout xargs command`` (including any ``VAR=val`` arguments to
    ``env`` or a bare duration argument to ``timeout``/``time``). This
    positional discipline is what keeps a path (``/usr/bin/python3``), a
    longer identifier (``mypython3``, ``pythonic``), an argument
    (``ls ~/.../python-standalone``), a quoted expansion
    (``"${BOOTSTRAP_PYTHON:?...}"``), and text after an unquoted ``#``
    comment from ever being examined.

    A command that names any ``$VAR`` or any absolute path (a redirection
    target aside) is never linted at all.
    """
    tokens = _tokenize(text)
    if _names_own_location(tokens):
        return None
    n = len(tokens)

    def is_cmd_start(idx: int) -> bool:
        if idx == 0:
            return True
        return tokens[idx - 1][0] == "op"

    for idx, (kind, _val) in enumerate(tokens):
        if kind != "word" or not is_cmd_start(idx):
            continue
        pos = _resolve_command_word(tokens, idx)
        if pos >= n or tokens[pos][0] != "word":
            continue
        word = tokens[pos][1]
        if word == "uv" and pos + 1 < n and tokens[pos + 1] == ("word", "run"):
            hit = _check_uv_run(tokens, pos)
            if hit:
                return hit
            continue
        if word in _BAD_EXACT:
            return _BAD_EXACT[word]
        if _PY3_VERSIONED_RE.match(word):
            return "python3.NN"
    return None


# ---------------------------------------------------------------------------
# lint_manifest
# ---------------------------------------------------------------------------

class LintHit(NamedTuple):
    """One bare-Python hit: where it is, what it calls, and the full message."""

    entry: str
    field: str
    token: str
    message: str

    @property
    def display(self) -> str:
        """The short label the engine shows in a collated display line."""
        return f"{self.entry}.{self.field}: bare {self.token} (fact {FACT_ID})"


def _lint_message(source: str, entry_name: str, field_path: str, token: str) -> str:
    reason = "calls uv run python" if token == "uv run python" else f"calls bare {token}"
    return (
        f"python: {source} {entry_name}.{field_path} {reason}; the multi-platform "
        f"default is {PLUGIN_CALL_SITE_EXPR} (project code: {CALL_SITE_EXPR}) -- "
        f"{_SEE_ALSO}"
    )


def _lint_field(hits: list, source: str, entry_name, field_path: str, value) -> None:
    if not isinstance(value, str):
        return
    token = lint_command(value)
    if token:
        hits.append(LintHit(str(entry_name), field_path, token,
                            _lint_message(source, entry_name, field_path, token)))


def lint_manifest_hits(manifest: dict, *, source: str) -> list:
    """:func:`lint_manifest` as structured :class:`LintHit` records."""
    hits: list = []
    if not isinstance(manifest, dict):
        return hits

    tools = manifest.get("tools")
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name", "<unnamed>")

        _lint_field(hits, source, name, "check", tool.get("check"))

        install = tool.get("install")
        if isinstance(install, str):
            _lint_field(hits, source, name, "install", install)
        elif isinstance(install, dict):
            for os_key, os_val in install.items():
                if isinstance(os_val, str):
                    _lint_field(hits, source, name, f"install.{os_key}", os_val)
                elif isinstance(os_val, dict):
                    for field in ("command", "check"):
                        _lint_field(
                            hits, source, name, f"install.{os_key}.{field}",
                            os_val.get(field),
                        )

    entries = manifest.get("env_checks")
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "<unnamed>")
        _lint_field(hits, source, name, "check", entry.get("check"))
        _lint_field(hits, source, name, "fix", entry.get("fix"))

    return hits


def lint_manifest(manifest: dict, *, source: str) -> list[str]:
    """Walk a parsed bootstrap.json manifest for bare-Python command hits.

    Walks ``tools[].check``; ``tools[].install`` as a bare string, as a
    per-OS dict of strings, and as a per-OS dict of objects (keys
    ``command`` and ``check``) -- for EVERY OS key the manifest declares,
    regardless of the current host; and ``env_checks[].check`` /
    ``env_checks[].fix``. Returns one message per hit, in manifest order.
    """
    return [hit.message for hit in lint_manifest_hits(manifest, source=source)]


# ---------------------------------------------------------------------------
# python_hint_for_output
# ---------------------------------------------------------------------------

_HINT_SUBSTRINGS = (
    "python3: command not found",
    "python: command not found",
    "python3: not found",
    "'python' is not recognized",
    "'python3' is not recognized",
)

_BARE_PYTHON_TOKEN_RE = re.compile(r"(?<![\w.-])python3?(?![\w.-])")


def python_hint_for_output(text: str) -> str | None:
    """Return the interpreter hint if ``text`` looks like a "python not
    found" failure (a shell "command not found", a Windows
    "is not recognized", the Microsoft Store stub, or a "not found in PATH"
    line naming a bare python/python3 token), else ``None``.

    Does not match an unrelated "command not found" for some other tool
    (e.g. ``git: command not found``).
    """
    if any(sub in text for sub in _HINT_SUBSTRINGS):
        return _HINT_MESSAGE
    if "Python was not found" in text and "Microsoft Store" in text:
        return _HINT_MESSAGE
    if "not found in PATH" in text and _BARE_PYTHON_TOKEN_RE.search(text):
        return _HINT_MESSAGE
    return None
