"""``codex exec`` completion transport.

The third backend behind the shared :class:`~.types.LLMBackend` protocol,
alongside :class:`~.backends.OpenRouterBackend` and
:class:`~.backends.ClaudeCliBackend`. Auth and billing come from the Codex
CLI's own login, so a run is subscription-billed rather than metered per call
and no usage envelope is produced.

Three structural differences from the claude transport are the whole reason
this is a separate module rather than another class in ``backends.py``:

* **The result arrives in a FILE, not on stdout.** ``codex exec -o <FILE>``
  writes the final message there; stdout carries progress chatter. So the
  runner's ``(stdout, stderr, returncode)`` triple does not carry the answer --
  :meth:`CodexCliBackend.complete` allocates a temp file, passes it to the
  builder, and reads it afterwards. A zero exit with a missing or empty file is
  the observable form of a silently-denied run, so it raises rather than
  returning "".
* **There is no separate system-prompt channel.** ``system`` and ``user`` are
  composed into the one stdin prompt (see :func:`compose_prompt`).
* **Every path must be absolute.** VERIFIED upstream: a relative ``-C``
  combined with ``--add-dir`` silently voids the entire writable-root set, so
  every write fails while the process still exits 0. ``build_codex_exec_argv``
  raises on a relative path, and this backend never supplies one -- an absent
  ``options.cwd`` resolves to ``Path.cwd().resolve()``.

Argv is built EXCLUSIVELY by :func:`bootstrap_lib.codex.build_codex_exec_argv`,
the single source of truth for codex detection and command construction. No
flag is hand-assembled here; a new codex knob is added there and forwarded from
``options.extras``.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict, Mapping, Optional

from . import halt
from .claude_runner import run_cli_streaming
from .adapter_capabilities import CODEX_CAPABILITIES
from .capabilities import Capabilities
from .prompt_fold import fold_prompt
from .results import (
    check_applied_controls,
    derive_dropped_params,
    derive_forwarded_params,
    utc_now_iso,
)
from .types import BackendOptions, LLMResponse


class CodexRunError(RuntimeError):
    """A codex run that failed or produced no answer.

    The transcript rides on ATTRIBUTES, never in the message, and this is
    load-bearing rather than tidiness. ``halt`` classifies an exception by
    substring-matching ``str(exc)``, and codex writes model-authored text to
    BOTH stdout and stderr -- so interpolating either channel into the message
    lets a healthy run that merely discusses "rate limit" or "unauthorized"
    classify as a persistent halt and abort an entire bulk run. The message is
    therefore restricted to text this module wrote itself.
    """

    def __init__(
        self,
        message: str,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


#: Separator between the system and user halves of the composed stdin prompt.
PROMPT_SEPARATOR = "\n\n---\n\n"

#: ``options.extras`` keys this backend forwards to ``build_codex_exec_argv``.
#: Anything else in ``extras`` reaches no argv element (it belongs to another
#: consumer) and is reported per key in ``dropped_params`` as ``extras.<key>``.
#: This tuple and the ``extras.*`` params in the advertisement are the same
#: fact stated twice -- one drives argv, the other drives the report -- so a
#: key added here without an advertisement record would be applied yet
#: reported as dropped. ``test_codex_extra_keys_match_the_advertisement`` in
#: tests/llm-scripting-kit/test_completion_capabilities.py pins them together.
CODEX_EXTRA_KEYS = (
    "scratch_dir",
    "add_dirs",
    "sandbox",
    "network",
    "output_schema",
)


#: Matches codex's "tokens used" label, wherever it sits in a noisy line.
_TOKENS_USED_RE = re.compile(r"tokens\s+used", re.IGNORECASE)

#: The first digit run after the label -- ``,`` tolerated as a thousands
#: separator, everything else (units, punctuation) left for the caller.
_NUMBER_RE = re.compile(r"\d[\d,]*")


def parse_codex_token_total(text: str) -> Optional[int]:
    """Extract codex's total-token figure from a stderr transcript.

    Codex's default (non-``--json``) path prints exactly one usage number --
    a running TOTAL, with no input/output split and no cache figure -- on its
    own pair of stderr lines::

        tokens used
        14,214

    Observed live: the label and the number can also land on ONE line (a
    log-prefix echo or a differently-buffered CLI collapses the pair), so both
    shapes are handled:

    - same line: the first digit run anywhere after the label on that line.
    - two lines: the first digit run on the next NON-BLANK line, when the
      label's own line has nothing after it.

    A thousands separator (``14,214``) is stripped before parsing. Surrounding
    noise -- a timestamp, a log-prefix tag, extra whitespace -- is tolerated
    because the label is matched with a bare substring search, not an anchored
    pattern.

    Returns ``None``, never raises, when the label is absent, or present with
    no digits reachable on its own line or the very next non-blank one. A
    "not found" and a "malformed" input are deliberately indistinguishable to
    the caller: both mean "no total to report", and returning 0 in either case
    would read as codex having reported a literal zero-token call.
    """
    if not text:
        return None
    lines = text.splitlines()
    for i, line in enumerate(lines):
        label = _TOKENS_USED_RE.search(line)
        if label is None:
            continue
        number = _NUMBER_RE.search(line[label.end():])
        if number is None:
            for candidate in lines[i + 1 :]:
                stripped = candidate.strip()
                if not stripped:
                    continue
                number = _NUMBER_RE.search(stripped)
                break
        if number is None:
            continue
        try:
            return int(number.group(0).replace(",", ""))
        except ValueError:
            continue
    return None


def compose_prompt(system: str, user: str) -> str:
    """Fold a system and a user prompt into codex's single stdin channel.

    ``codex exec`` takes one prompt and exposes no system-prompt flag, so the
    two halves are concatenated system-first, separated by
    :data:`PROMPT_SEPARATOR` (a blank line, a ``---`` rule, a blank line). An
    empty ``system`` yields the user prompt verbatim, so a caller that already
    folded its own instructions gets no stray leading separator. The folding
    itself is shared with the opencode sibling -- see :mod:`.prompt_fold`.
    """
    return fold_prompt(system, user, PROMPT_SEPARATOR)


@dataclass
class CodexCliBackend:
    """``codex exec`` completion transport over :func:`.run_cli_streaming`.

    Attributes:
        default_timeout_s: Per-call watchdog when the caller's options carry no
            ``timeout_s``. 900s, matching :class:`~.backends.ClaudeCliBackend`
            -- generous for a large completion, tight enough that a silent
            CLI-layer backoff cannot stall a bulk run.
        argv_prefix: Explicit launcher argv (the test seam). ``None`` lets the
            builder resolve ``codex`` via ``shutil.which`` at call time, which
            is also what wraps a Windows ``codex.cmd`` in ``cmd /c``.
        runner: The subprocess runner -- test seam; production is
            :func:`.claude_runner.run_cli_streaming`.

    Options mapping:

    - ``model`` -> ``-m``. Fully qualified ids only; the bare codenames are not
      dispatchable.
    - ``effort`` -> ``-c model_reasoning_effort=...``.
    - ``timeout_s`` -> the runner's per-call watchdog.
    - ``cwd`` -> ``-C``. MUST be absolute; defaults to ``Path.cwd().resolve()``.
    - ``extras`` -> the sanctioned place for codex-specific knobs. Recognised
      keys are :data:`CODEX_EXTRA_KEYS`: ``scratch_dir`` (extra writable dir,
      typically the session scratchpad under TEMP), ``add_dirs`` (further
      writable roots), ``sandbox`` (``-s`` value), ``network`` (bool, network
      access inside the sandbox), ``output_schema`` (JSON-schema file). Each is
      optional and forwarded to the builder only when present, so the builder's
      defaults (``-s workspace-write``, network on) stand otherwise. Any OTHER
      extras key reaches nothing and comes back in ``dropped_params`` as
      ``extras.<key>`` -- per key, because half of this map IS read and the
      bare field name would have no single answer here.
    - ``temperature`` / ``max_tokens`` / ``allowed_tools`` /
      ``disallowed_tools`` / ``system_prompt_mode`` / ``cache_salt`` /
      ``user_cache_prefix`` -- codex exposes no such knobs. Accepted for
      protocol compatibility and dropped, exactly as ClaudeCliBackend does --
      and reported in ``dropped_params``, not discarded in silence.

    Usage/cost: codex emits no PER-DIRECTION usage envelope on the default
    (non-``--json``) path, so ``input_tokens`` / ``output_tokens`` /
    ``cache_hit_tokens`` are always 0 -- they are NOT estimated, since a
    fabricated split is worse than an honest zero in an audit record. Codex
    does print a running TOTAL on stderr ("tokens used" / a number), which
    :func:`parse_codex_token_total` extracts into ``LLMResponse.total_tokens``
    when present, 0 when the line is absent or unparseable.
    """

    default_timeout_s: float = 900.0
    argv_prefix: Optional[tuple] = None
    runner: Callable[..., "tuple[str, str, int]"] = run_cli_streaming
    name: str = field(default="codex-cli", init=False)
    capabilities: ClassVar[Capabilities] = CODEX_CAPABILITIES

    def complete(
        self,
        system: str,
        user: str,
        *,
        model: str,
        options: Optional[BackendOptions] = None,
    ) -> LLMResponse:
        """Run one ``codex exec`` and normalize the result.

        Allocates a temp output file, dispatches, then reads it back -- the
        file is removed in a ``finally`` so a raising path leaves nothing
        behind.
        """
        opts = options or BackendOptions()
        timeout_s = (
            opts.timeout_s if opts.timeout_s is not None else self.default_timeout_s
        )
        root = opts.cwd if opts.cwd is not None else Path.cwd()
        # resolve() BEFORE the builder sees it: an absent cwd must never be the
        # source of the relative -C that voids the writable-root set. A cwd the
        # caller DID supply is passed through as given, so the builder's
        # ValueError still names a caller's relative path rather than being
        # papered over here.
        if opts.cwd is None:
            root = root.resolve()

        handle, raw_output_path = tempfile.mkstemp(
            prefix="codex_out_", suffix=".txt"
        )
        os.close(handle)
        output_path = Path(raw_output_path)

        try:
            argv = self._build_argv(
                root=root, model=model, effort=opts.effort,
                output_file=output_path, extras=opts.extras,
            )
            prompt = compose_prompt(system, user)

            started_at = utc_now_iso()
            start = time.monotonic()
            stdout, stderr, returncode = self.runner(
                argv,
                prompt,
                root,
                log_prefix=opts.log_prefix,
                timeout_s=timeout_s,
                label="codex exec",
                # No live stderr kill. The runner's default marker set is
                # claude's vocabulary, which codex does not speak, and the
                # codex markers in `halt` are inferred rather than observed --
                # a false positive there would kill a HEALTHY run the moment
                # the model's own output echoed the phrase. A real cap still
                # surfaces: codex exits nonzero and the raise below carries
                # stderr, which classify_halt then reads.
                hard_stop_markers=(),
            )
            wall_ms = int((time.monotonic() - start) * 1000)

            if returncode != 0:
                raise CodexRunError(
                    f"codex exec failed (exit {returncode})",
                    stdout=stdout,
                    stderr=stderr,
                    returncode=returncode,
                )

            text = self._read_output(output_path, stdout=stdout, stderr=stderr)
        finally:
            try:
                output_path.unlink()
            except OSError:
                pass

        total_tokens = parse_codex_token_total(stderr)

        return LLMResponse(
            text=text,
            model=model,
            input_tokens=0,
            output_tokens=0,
            cache_hit_tokens=0,
            total_tokens=total_tokens or 0,
            wall_ms=wall_ms,
            attempts=1,
            from_cache=False,
            dropped_params=derive_dropped_params(self.capabilities, opts),
            forwarded_params=derive_forwarded_params(self.capabilities, opts),
            execution_controls_applied=self._applied_controls(argv),
            structured=self._parse_structured(text, opts.extras),
            started_at=started_at,
            ended_at=utc_now_iso(),
        )

    def _applied_controls(self, argv: "list") -> "tuple[str, ...]":
        """Which advertised controls THIS argv actually carries.

        Read off the built argv rather than re-derived from the options,
        deliberately: argv construction is delegated to ``bootstrap_lib.codex``
        in another plugin, so an option value is only a request for a flag and
        this adapter is not the code that decides whether one is emitted. The
        argv is the emission, and the advertisement's rule is that a control is
        advertised only where something is emitted -- so reading the emission is
        the only report that cannot drift from the builder.
        """
        joined = " ".join(str(a) for a in argv)
        applied = []
        if "-s" in [str(a) for a in argv]:
            applied.append("sandbox-mode")
        if "sandbox_workspace_write.network_access=true" in joined:
            applied.append("network-enable")
        if 'windows.sandbox="unelevated"' in joined:
            applied.append("windows-sandbox-mode")
        if "--skip-git-repo-check" in joined:
            applied.append("skip-git-repo-check")
        return check_applied_controls(self.capabilities, applied)

    @staticmethod
    def _parse_structured(text: str, extras: Any) -> Optional[Any]:
        """Parse the result as JSON, but only when a caller schema was sent.

        ``--output-schema`` is a first-class CLI schema control, so a result
        produced under one is schema-backed output and the contract can carry it
        in ``structured``. Two restraints keep the claim honest:

        - Absent ``extras.output_schema`` nothing is parsed, even when the text
          happens to be valid JSON. A model that returned JSON unbidden was not
          honoring a schema, and presenting it as though it were would be the
          overclaim this contract exists to prevent.
        - A parse failure yields ``None`` rather than raising. The call
          succeeded and ``text`` still carries the result verbatim; the honest
          report is that no structured output is available, not that the call
          failed.
        """
        if not isinstance(extras, Mapping) or not extras.get("output_schema"):
            return None
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return None

    def classify_halt(self, exc: BaseException) -> Optional[str]:
        return halt.classify_codex_exception(exc)

    # -- internals ---------------------------------------------------------

    def _build_argv(
        self,
        *,
        root: Path,
        model: str,
        effort: Optional[str],
        output_file: Path,
        extras: Any,
    ) -> list:
        """Delegate argv construction to the shared bootstrap_lib builder.

        Imported inside the call rather than at module import so this module
        stays importable (for its types and :func:`compose_prompt`) on a machine
        whose venv has not yet been provisioned with the ``bootstrap_lib``
        shared lib -- the failure then lands on the call that needs it, with the
        import error intact, instead of poisoning ``llm_scripting_kit``'s whole
        package import.
        """
        from bootstrap_lib.codex import build_codex_exec_argv  # noqa: PLC0415

        kwargs: Dict[str, Any] = {
            "root": root,
            "output_file": output_file,
            "argv_prefix": self.argv_prefix,
        }
        if model:
            kwargs["model"] = model
        if effort is not None:
            kwargs["effort"] = effort
        mapping = extras if isinstance(extras, dict) else dict(extras or {})
        for key in CODEX_EXTRA_KEYS:
            if key in mapping and mapping[key] is not None:
                kwargs[key] = mapping[key]
        return build_codex_exec_argv(**kwargs)

    @staticmethod
    def _read_output(path: Path, *, stdout: str, stderr: str) -> str:
        """Read the ``-o`` file, raising when codex wrote nothing to it.

        A zero exit with no output file (or an empty one) is how a silently
        denied run presents: codex declines, reports success, and the caller
        would otherwise record an empty completion as a legitimate answer. The
        transcript rides on the raised error's attributes, not its message --
        see :class:`CodexRunError` for why that separation matters.
        """
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise CodexRunError(
                f"codex exec exited 0 but wrote no output file ({path}): "
                f"{type(exc).__name__}",
                stdout=stdout,
                stderr=stderr,
                returncode=0,
            ) from exc
        if not text.strip():
            raise CodexRunError(
                f"codex exec exited 0 but its output file ({path}) is empty -- "
                f"the run was most likely denied without failing",
                stdout=stdout,
                stderr=stderr,
                returncode=0,
            )
        return text


__all__ = [
    "CODEX_EXTRA_KEYS",
    "PROMPT_SEPARATOR",
    "CodexCliBackend",
    "CodexRunError",
    "compose_prompt",
    "parse_codex_token_total",
]
