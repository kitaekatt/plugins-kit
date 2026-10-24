"""``codex exec`` completion transport.

The third backend behind the shared :class:`~.types.LLMBackend` protocol,
alongside :class:`~.backends.OpenRouterBackend` and
:class:`~.backends.ClaudeCliBackend`. Auth and billing come from the Codex
CLI's own login, so a run is subscription-billed rather than metered per call
and no cost is produced.

Three structural differences from the claude transport are the whole reason
this is a separate module rather than another class in ``backends.py``:

* **The result arrives in a FILE, not on stdout.** ``codex exec -o <FILE>``
  writes the final message there; stdout carries progress chatter. So the
  runner's ``(stdout, stderr, returncode)`` triple does not carry the answer --
  :meth:`CodexCliBackend.complete` allocates a temp file, passes it to the
  builder, and reads it afterwards. A zero exit with a missing or empty file is
  the observable form of a silently-denied run, so it raises rather than
  returning "".
* **Usage rides on stdout as JSON events.** Every run passes ``--json``, so
  stdout is a JSONL event stream whose ``turn.completed`` event carries the
  per-category token counts (see :func:`parse_codex_usage_events`). The answer
  still comes from the ``-o`` file, never from the stream.
* **There is no separate system-prompt channel.** ``system`` and ``user`` are
  composed into the one stdin prompt (see :func:`compose_prompt`).
* **Every path must be absolute.** VERIFIED upstream: a relative ``-C``
  combined with ``--add-dir`` silently voids the entire writable-root set, so
  every write fails while the process still exits 0. ``build_codex_exec_argv``
  raises on a relative path, and this backend never supplies one -- an absent
  ``options.cwd`` resolves to ``Path.cwd().resolve()``.

Argv is built EXCLUSIVELY by
:func:`llm_kit.adapters.codex_command.build_codex_exec_argv`, the single source
of truth for codex detection and command construction. No flag is
hand-assembled here; a new codex knob is added there and forwarded from
``options.extras``.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict, Mapping, Optional

from llm_kit.registry import conserve
from llm_kit.registry.effort import CODEX_EFFORT_ORDER, CODEX_HARNESS
from llm_kit.registry.model_profiles import check_effort
from . import codex_command, usage_sources
from llm_kit.completion import halt
from .claude_runner import run_cli_streaming
from .capabilities import (
    _CODEX_TEXT_ONLY_CATALOG_DROP,
    _CODEX_TEXT_ONLY_CATALOG_SET,
    _CODEX_TEXT_ONLY_CONFIG,
    CODEX_CAPABILITIES,
)
from llm_kit.completion.capabilities import Capabilities
from llm_kit.completion.contract import finalize_contract, prepare_contract
from llm_kit.completion.skill_context import prepare_skill_context
from llm_kit.completion.skill_context_types import SkillContextUnsatisfiable
from llm_kit.completion.prompt_fold import fold_prompt
from llm_kit.completion.results import (
    check_applied_controls,
    derive_dropped_params,
    derive_forwarded_params,
    utc_now_iso,
)
from llm_kit.completion.types import BackendOptions, LLMResponse


#: The spec a non-zero-exit quota re-read is evaluated against. Deliberately
#: the harness-neutral default, not an entry's own ``conserve_usage`` --
#: nothing this far down the completion seam carries the registry entry a
#: model id resolved from (that wiring is out of this step's scope; see
#: docs/planning/quota-resilient-dispatch/declaration-format-design.md,
#: Decision 6). ``read_codex_pool`` remaps ``seven_day`` to codex's own
#: ``primary`` window, so this is the same read an opted-in entry would get.
_QUOTA_PROBE_SPEC = conserve.ConserveSpec(pool=conserve.POOL_SEVEN_DAY)


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
        halt_kind: Optional[str] = None,
        resets_at: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        # Authoritative pre-classification for a non-zero exit whose halt
        # kind was determined by re-reading the session rollout rather than
        # by matching this exception's own channels -- see
        # CodexCliBackend._quota_probe. None (the default) means the raise
        # site did no such probe or the probe found no quota evidence; the
        # ordinary text classifiers in `halt` still apply in that case.
        self.halt_kind = halt_kind
        self.resets_at = resets_at


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
    "native_subagents",
    "output_schema",
)


@dataclass(frozen=True)
class CodexUsage:
    """Token counts from the ``turn.completed`` events of a ``codex exec --json`` run.

    Measured on codex-cli 0.164.0 (the event shape is in
    ``tests/llm-kit/fixtures/codex_json``), and cross-checked against the
    session rollout's own ``token_count`` record:

    - ``input_tokens`` INCLUDES ``cached_input_tokens`` (the cached part is a
      subset of the input), the same convention as the OpenAI-compatible
      transport, which is what ``platform.py``'s cost arithmetic expects.
    - ``output_tokens`` INCLUDES ``reasoning_output_tokens`` (the rollout's
      ``total_tokens`` is input plus output with reasoning not added again).
    """

    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int

    @property
    def blended_total_tokens(self) -> int:
        """Codex's own "tokens used" figure: uncached input plus output.

        Equal to the number the default (non-``--json``) path prints on stderr:
        a measured pair, input 20428 / cached 11520 / output 5, printed
        ``tokens used 8,913`` = (20428 - 11520) + 5.
        """
        return max(0, self.input_tokens - self.cached_input_tokens) + self.output_tokens


def _usage_count(usage: Mapping, key: str, *, required: bool) -> int:
    value = usage.get(key)
    if value is None and not required:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(
            "codex turn.completed usage field %r is %r, expected a "
            "non-negative integer" % (key, value)
        )
    return value


def parse_codex_usage_events(stdout: str) -> Optional[CodexUsage]:
    """Sum the usage of every ``turn.completed`` event in a ``--json`` stream.

    Lines that are not JSON objects are skipped (a stream cut off mid-line, or
    stray text); events of any other ``type`` are ignored. Returns None when no
    ``turn.completed`` event carries a ``usage`` object -- no figure, which the
    caller must not read as zero tokens.

    ``input_tokens`` and ``output_tokens`` are required in a usage object and a
    malformed value raises ValueError: a usage shape this module has not seen
    must not be reported as a number it made up. ``cached_input_tokens`` and
    ``reasoning_output_tokens`` default to 0 when absent or null, since a CLI
    that predates a category legitimately lacks it.
    """
    if not stdout:
        return None
    found = False
    input_tokens = cached = output_tokens = reasoning = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, Mapping) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, Mapping):
            continue
        found = True
        input_tokens += _usage_count(usage, "input_tokens", required=True)
        output_tokens += _usage_count(usage, "output_tokens", required=True)
        cached += _usage_count(usage, "cached_input_tokens", required=False)
        reasoning += _usage_count(usage, "reasoning_output_tokens", required=False)
    if not found:
        return None
    return CodexUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning,
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
        text_only: Run with no file-writing, shell or subagent tool:
            ``--ignore-user-config``, ``-s read-only``, no network flag, the
            ``-c`` pairs in ``_CODEX_TEXT_ONLY_CONFIG``, and a one-model
            ``model_catalog_json`` built from ``codex debug models`` (one
            extra runner call) with the model's code-mode, multi-agent,
            apply_patch, tool_search and node-REPL settings removed. See
            ``adapter_capabilities`` for the measurement. Not a caller flag:
            :func:`~.requirements.arm_call` sets it. ``extras.sandbox`` other
            than ``read-only``, a true ``extras.network``, or no model id is
            refused before dispatch in this mode.

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
    - ``output_contract`` -> native delivery. A schema contract's
      :func:`~.contract_types.canonical_schema_json` is written to a temp file
      passed as ``--output-schema`` (deleted in the same ``finally`` as the
      result file), and the answer is judged by
      :func:`~.contract.finalize_contract`; ``text-only`` emits nothing. A
      contract sent beside ``extras.output_schema`` is refused before
      dispatch. Codex applies OpenAI strict-mode rules to that schema (every
      object needs ``additionalProperties: false`` and every property listed
      in ``required``; a schema that breaks them fails the run with a
      non-zero exit, observed live on codex-cli 0.161.0-alpha.2), so the
      record advertises ``contract_schema_class: openai-strict`` and
      ``prepare_contract`` refuses any other schema before a file is written
      or the runner called, naming the offending schema pointers.
    - ``temperature`` / ``max_tokens`` / ``allowed_tools`` /
      ``disallowed_tools`` / ``system_prompt_mode`` / ``cache_salt`` /
      ``user_cache_prefix`` -- codex exposes no such knobs. Accepted for
      protocol compatibility and dropped, exactly as ClaudeCliBackend does --
      and reported in ``dropped_params``, not discarded in silence.

    Usage/cost: every run passes ``--json`` and the ``turn.completed`` events
    are read by :func:`parse_codex_usage_events`. ``input_tokens`` is codex's
    input count (cached part included), ``cache_hit_tokens`` its cached input,
    ``output_tokens`` its output count (reasoning included),
    ``reasoning_tokens`` the reasoning share of that output and
    ``total_tokens`` codex's own blended figure (uncached input plus output,
    the number "tokens used" shows). Nothing is estimated: when the stream carries no usage
    event the counts stay 0, and ``total_tokens`` falls back to the stderr
    "tokens used" figure (:func:`parse_codex_token_total`), which a ``--json``
    run does not print, so 0 in practice.
    """

    default_timeout_s: float = 900.0
    argv_prefix: Optional[tuple] = None
    runner: Callable[..., "tuple[str, str, int]"] = run_cli_streaming
    # Test seam for the quota re-read (_quota_probe). None uses
    # usage_sources.CODEX_SESSIONS_DIR, the real machine-wide rollout
    # directory.
    sessions_dir: Optional[Path] = None
    text_only: bool = False
    name: str = field(default="codex-cli", init=False)
    capabilities: ClassVar[Capabilities] = CODEX_CAPABILITIES

    def with_text_only(self) -> "CodexCliBackend":
        """This backend in text-only mode (``requirements.arm_call`` calls it)."""
        return self if self.text_only else dataclasses.replace(self, text_only=True)

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
        native_subagents = (opts.extras or {}).get("native_subagents")
        if native_subagents is not None and native_subagents != "deny":
            raise ValueError("extras.native_subagents supports only 'deny'; nothing was dispatched")
        # Before any temp file, argv or runner call: a contract this record
        # does not list (or one sent beside extras.output_schema) is refused
        # here, with nothing spawned.
        plan = prepare_contract(self.capabilities, opts)
        # Skill context is refused here, before any temp file, argv or runner call:
        # this record carries no skill_context block (the harness loads skills
        # itself), so prepare_skill_context raises for any skill context.
        if prepare_skill_context(self.capabilities, opts) is not None:
            raise SkillContextUnsatisfiable(
                f"{self.name} has no skill-context delivery path; nothing was dispatched"
            )
        if self.text_only:
            self._refuse_text_only_conflicts(opts.extras)
            if not model:
                raise ValueError(
                    f"{self.name} text-only mode patches the model's catalog "
                    "entry, so it needs a model id; nothing was dispatched"
                )
        # The effort is held to the exact model's measured menu here, before any
        # temp file, argv or runner call -- the same validator CodexAdapter
        # uses, so neither Codex path can bypass it. With no model id the
        # harness-wide menu stands.
        check_effort(
            opts.effort,
            route=CODEX_HARNESS,
            model=model or None,
            fallback=CODEX_EFFORT_ORDER,
            subject=self.name,
        )
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
        schema_path: Optional[Path] = None
        catalog_path: Optional[Path] = None

        try:
            if self.text_only:
                catalog_path = self._write_text_only_catalog(
                    model, root, log_prefix=opts.log_prefix, timeout_s=timeout_s
                )
            extras = dict(opts.extras or {})
            codex_config = extras.pop("__llm_kit_codex_config", None)
            image_paths = extras.pop("__llm_kit_image_paths", ())
            if plan is not None and plan.schema_json is not None:
                # Native contract delivery: the canonical schema document goes
                # to --output-schema through the same builder key the legacy
                # extras path uses. prepare_contract already refused a
                # contract sent beside extras.output_schema, so nothing here
                # overrides a caller's own schema file.
                schema_path = self._write_schema_file(plan.schema_json)
                extras = {**extras, "output_schema": str(schema_path)}
            argv = self._build_argv(
                root=root, model=model, effort=opts.effort,
                output_file=output_path,
                extras={
                    **extras,
                    **({"__llm_kit_codex_config": codex_config} if codex_config else {}),
                    **({"__llm_kit_image_paths": image_paths} if image_paths else {}),
                },
                catalog_path=catalog_path,
            )
            response_options = dataclasses.replace(opts, extras=extras)
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
                quota = self._quota_probe()
                if quota is not None and quota.status == conserve.STATUS_OUT_OF_QUOTA:
                    raise CodexRunError(
                        f"codex exec failed (exit {returncode})",
                        stdout=stdout,
                        stderr=stderr,
                        returncode=returncode,
                        halt_kind=halt.HALT_QUOTA,
                        resets_at=quota.resets_at,
                    )
                raise CodexRunError(
                    f"codex exec failed (exit {returncode})",
                    stdout=stdout,
                    stderr=stderr,
                    returncode=returncode,
                )

            text = self._read_output(output_path, stdout=stdout, stderr=stderr)
        finally:
            for path in (output_path, schema_path, catalog_path):
                if path is None:
                    continue
                try:
                    path.unlink()
                except OSError:
                    pass

        usage = parse_codex_usage_events(stdout)
        if usage is not None:
            input_tokens = usage.input_tokens
            output_tokens = usage.output_tokens
            cache_hit_tokens = usage.cached_input_tokens
            total_tokens = usage.blended_total_tokens
            reasoning_tokens = usage.reasoning_output_tokens
        else:
            input_tokens = output_tokens = cache_hit_tokens = reasoning_tokens = 0
            total_tokens = parse_codex_token_total(stderr) or 0

        response = LLMResponse(
            text=text,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_hit_tokens=cache_hit_tokens,
            total_tokens=total_tokens,
            reasoning_tokens=reasoning_tokens,
            wall_ms=wall_ms,
            attempts=1,
            from_cache=False,
            dropped_params=derive_dropped_params(self.capabilities, response_options),
            forwarded_params=derive_forwarded_params(self.capabilities, response_options),
            execution_controls_applied=self._applied_controls(argv),
            # Under a contract, structured is set only by finalize_contract,
            # and only to a validated object; the legacy bare parse is for
            # the extras.output_schema path alone.
            structured=None if plan is not None else self._parse_structured(text, response_options.extras),
            started_at=started_at,
            ended_at=utc_now_iso(),
        )
        # No contract: returned unchanged. Otherwise the answer is judged
        # against the contract, and a violation raises with this response.
        return finalize_contract(plan, response)

    @staticmethod
    def _write_schema_file(schema_json: str) -> Path:
        """Write the native schema document to a fresh temp file, exact bytes.

        ``schema_json`` is :func:`~.contract_types.canonical_schema_json`
        (ASCII by construction), written as those bytes and nothing else so
        what codex reads is exactly what the contract fingerprints. The
        caller deletes the file in its ``finally``.
        """
        handle, raw_path = tempfile.mkstemp(prefix="codex_schema_", suffix=".json")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(schema_json.encode("ascii"))
        except BaseException:
            # Not yet handed to the caller's finally, so remove it here.
            Path(raw_path).unlink(missing_ok=True)
            raise
        return Path(raw_path)

    def _applied_controls(self, argv: "list") -> "tuple[str, ...]":
        """Which advertised controls THIS argv actually carries.

        Read off the built argv rather than re-derived from the options,
        deliberately: argv construction is delegated to
        ``llm_kit.adapters.codex_command``, so an option value is only a request for a flag and
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
        if "sandbox_workspace_write.network_access=false" in joined:
            applied.append("network-disable")
        if all(value in joined for value in ("agents.enabled=false", "features.multi_agent=false", "features.multi_agent_v2=false")):
            applied.append("native-subagents-deny")
        if 'windows.sandbox="unelevated"' in joined:
            applied.append("windows-sandbox-mode")
        if "--skip-git-repo-check" in joined:
            applied.append("skip-git-repo-check")
        pairs = {
            str(argv[i + 1]) for i in range(len(argv) - 1) if str(argv[i]) == "-c"
        }
        if (
            "--ignore-user-config" in [str(a) for a in argv]
            and all(item in pairs for item in _CODEX_TEXT_ONLY_CONFIG)
            and any(pair.startswith("model_catalog_json=") for pair in pairs)
        ):
            applied.append("text-only-mode")
        return check_applied_controls(self.capabilities, applied)

    def _write_text_only_catalog(
        self, model: str, root: Path, *, log_prefix: str, timeout_s: float
    ) -> Path:
        """Write ``model``'s live catalog entry, patched for text-only mode.

        The catalog, not the config, decides whether a model gets code mode,
        multi-agent tools, apply_patch, tool_search and the node REPL, so the
        entry codex would use is read with ``codex debug models`` and rewritten
        with ``_CODEX_TEXT_ONLY_CATALOG_SET`` / ``_CODEX_TEXT_ONLY_CATALOG_DROP``.
        A failed read, unparseable output, or a model the catalog does not
        list raises: without the patch the mode would not deliver what it
        advertises. The caller deletes the file in its ``finally``.
        """
        prefix = self.argv_prefix
        if prefix is None:
            prefix = codex_command.resolve_codex_cli()
            if prefix is None:
                raise RuntimeError(
                    f"`{codex_command.configured_codex_executable()}` is not on PATH, so text-only mode "
                    "cannot read the model catalog"
                )
        stdout, stderr, returncode = self.runner(
            [*[str(part) for part in prefix], "debug", "models"],
            "",
            root,
            log_prefix=log_prefix,
            timeout_s=timeout_s,
            label="codex debug models",
            hard_stop_markers=(),
        )
        if returncode != 0:
            raise CodexRunError(
                f"codex debug models failed (exit {returncode}); text-only mode "
                "needs the model catalog",
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
            )
        try:
            catalog = json.loads(stdout)
        except ValueError as exc:
            raise CodexRunError(
                "codex debug models printed no JSON catalog", stdout=stdout, stderr=stderr,
                returncode=returncode,
            ) from exc
        models = catalog.get("models") if isinstance(catalog, Mapping) else None
        entry = next(
            (m for m in models or () if isinstance(m, Mapping) and m.get("slug") == model),
            None,
        )
        if entry is None:
            raise CodexRunError(
                f"codex model catalog has no entry for {model!r}, so text-only "
                "mode cannot remove its catalog tools; nothing was dispatched",
                stdout=stdout, stderr=stderr, returncode=returncode,
            )
        patched = {
            key: value for key, value in entry.items()
            if key not in _CODEX_TEXT_ONLY_CATALOG_DROP
        }
        patched.update(_CODEX_TEXT_ONLY_CATALOG_SET)
        handle, raw_path = tempfile.mkstemp(prefix="codex_catalog_", suffix=".json")
        try:
            with os.fdopen(handle, "w", encoding="ascii") as stream:
                json.dump({"models": [patched]}, stream, ensure_ascii=True)
        except BaseException:
            Path(raw_path).unlink(missing_ok=True)
            raise
        path = Path(raw_path)
        if "'" in str(path):
            path.unlink(missing_ok=True)
            raise ValueError(
                f"temp catalog path {path} contains a single quote, which a TOML "
                "literal string cannot carry"
            )
        return path

    def _refuse_text_only_conflicts(self, extras: Any) -> None:
        """Refuse an extras value text-only mode would have to override."""
        mapping = extras if isinstance(extras, Mapping) else {}
        sandbox = mapping.get("sandbox")
        if sandbox is not None and sandbox != "read-only":
            raise ValueError(
                f"{self.name} text-only mode runs -s read-only, so "
                f"extras.sandbox={sandbox!r} cannot be honored; nothing was dispatched"
            )
        if mapping.get("network"):
            raise ValueError(
                f"{self.name} text-only mode runs with network off, so "
                "extras.network=true cannot be honored; nothing was dispatched"
            )

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
        """Report the raise site's own verdict first, then fall back to text.

        A :class:`CodexRunError` raised by :meth:`complete` after a quota
        re-read carries an authoritative ``halt_kind`` (see
        :data:`~.halt.HALT_QUOTA`'s docstring) -- that determination already
        read the session rollout, which this method has no access to, so it
        is reported verbatim rather than re-derived from exception text.
        Every other exception (no ``halt_kind`` attribute, or ``None``) falls
        through to the ordinary text-based classifier unchanged.
        """
        kind = getattr(exc, "halt_kind", None)
        if kind is not None:
            return kind
        return halt.classify_codex_exception(exc)

    def _quota_probe(self) -> Optional["conserve.Budget"]:
        """Re-read the codex session rollout for a quota-exhaustion verdict.

        Called only on a non-zero exit -- a healthy call's rollout has
        nothing to probe, and probing it anyway would cost a filesystem scan
        on every successful dispatch. Fails open (returns ``None``) on any
        read error: a broken probe must never mask or replace the real
        dispatch failure being reported.
        """
        try:
            return usage_sources.read_codex_pool(
                _QUOTA_PROBE_SPEC, sessions_dir=self.sessions_dir
            )
        except Exception:
            return None

    # -- internals ---------------------------------------------------------

    def _build_argv(
        self,
        *,
        root: Path,
        model: str,
        effort: Optional[str],
        output_file: Path,
        extras: Any,
        catalog_path: Optional[Path] = None,
    ) -> list:
        """Delegate argv construction to ``llm_kit.adapters.codex_command``."""
        kwargs: Dict[str, Any] = {
            "root": root,
            "output_file": output_file,
            "argv_prefix": self.argv_prefix,
            # Usage events; the answer still comes from the -o file.
            "json_events": True,
        }
        if model:
            kwargs["model"] = model
        if effort is not None:
            kwargs["effort"] = effort
        mapping = extras if isinstance(extras, dict) else dict(extras or {})
        codex_config = mapping.get("__llm_kit_codex_config")
        image_paths = mapping.get("__llm_kit_image_paths", ())
        if codex_config:
            kwargs["codex_config"] = dict(codex_config)
        if image_paths:
            kwargs["image_paths"] = image_paths
        for key in CODEX_EXTRA_KEYS:
            if key in mapping and mapping[key] is not None:
                kwargs[key] = mapping[key]
        if self.text_only:
            # complete() already refused a conflicting sandbox or network.
            kwargs["sandbox"] = "read-only"
            kwargs["network"] = False
            kwargs["ignore_user_config"] = True
            # A TOML literal string: no escape processing, so a Windows path's
            # backslashes survive (_write_text_only_catalog refused a quote).
            kwargs["extra_config"] = (
                *_CODEX_TEXT_ONLY_CONFIG,
                f"model_catalog_json='{catalog_path}'",
            )
        return codex_command.build_codex_exec_argv(**kwargs)

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
    "CodexUsage",
    "compose_prompt",
    "parse_codex_token_total",
    "parse_codex_usage_events",
]
