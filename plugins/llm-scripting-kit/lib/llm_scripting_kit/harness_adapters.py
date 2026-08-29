"""Command-line adapters for harness-backed :class:`EndpointEntry` values.

The registry entry owns the harness name, model id, and optional default
effort. This module owns the small amount of per-harness command grammar
needed to dispatch one non-interactive run. It deliberately does not own a
subprocess runner or a backend: callers can pipe :attr:`HarnessInvocation.stdin`
to the returned argv using whichever run policy they need.

Both CLIs consume the brief on standard input, so the prompt source is not
encoded as an argv argument. ``build_argv`` accepts the source so it can check
the same dispatch contract as ``build_invocation``; ``prompt_stdin`` reads an
absolute prompt file when a caller wants the ready-to-pipe invocation object.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import ClassVar, Optional, Sequence, Union

from .model_endpoints import HARNESS_KIND, EndpointEntry

PathLike = Union[str, os.PathLike[str]]
EffortMenu = Optional[frozenset[str]]

CODEX_HARNESS = "codex"
OPENCODE_HARNESS = "opencode"
OPENCODE_EXECUTABLE = "opencode"
OPENCODE_AGENT = "build"

# `xhigh` is intentionally present even though some Codex help output omits
# it. The accepted menu is a runtime contract, not a transcription of one
# version's help text, so an undocumented but accepted level must survive.
CODEX_EFFORT_MENU = frozenset(("low", "medium", "high", "xhigh", "max"))

# OpenCode resolves variants from each provider's user-owned configuration.
# None is the explicit "no exhaustive local menu" signal; non-empty values
# are passed through after basic type validation.
OPENCODE_EFFORT_MENU: EffortMenu = None

KNOWN_HARNESSES = (CODEX_HARNESS, OPENCODE_HARNESS)


class HarnessAdapterError(ValueError):
    """A harness entry or dispatch argument cannot be honored by an adapter."""


@dataclass(frozen=True)
class HarnessInvocation:
    """An argv plus the stdin payload for one harness dispatch."""

    argv: tuple[str, ...]
    stdin: str


def _absolute_path(value: PathLike, parameter: str) -> str:
    """Return a path string, refusing relative paths at the adapter boundary.

    The Codex builder repeats this check for its own path arguments. OpenCode
    has no shared builder, so this module must enforce the same caller-facing
    promise itself rather than letting a relative ``--dir`` look intentional.
    """
    try:
        text = os.fspath(value)
    except TypeError as exc:
        raise HarnessAdapterError(
            f"{parameter} must be an absolute path (got {value!r})"
        ) from exc
    if isinstance(text, bytes):
        text = os.fsdecode(text)
    if not os.path.isabs(text):
        raise HarnessAdapterError(
            f"{parameter} must be an absolute path (got {text!r})"
        )
    return text


def _prompt_file(prompt_file: Optional[PathLike]) -> Optional[str]:
    """Validate the stdin file input, if the caller passed one."""
    if prompt_file is None:
        return None
    return _absolute_path(prompt_file, "prompt_file")


class HarnessAdapter:
    """Common validation and stdin handling for one harness command adapter."""

    name: ClassVar[str]
    effort_menu: ClassVar[EffortMenu]

    def accepted_efforts(self) -> EffortMenu:
        """Return the accepted menu, or None for a provider-defined menu."""
        return self.effort_menu

    def is_available(self) -> bool:
        """Return whether this adapter's CLI can be resolved on this machine."""
        raise NotImplementedError

    def prompt_stdin(
        self,
        *,
        prompt: Optional[str] = None,
        prompt_file: Optional[PathLike] = None,
    ) -> str:
        """Return the text to pipe to the harness's stdin.

        The CLI contracts use stdin for the brief, not a prompt-file flag. A
        file therefore has to be read by the caller or here; accepting a path
        without ever consuming it would make a seemingly valid dispatch feed
        no brief at all.
        """
        path = _prompt_file(prompt_file)
        if prompt is not None and path is not None:
            raise HarnessAdapterError(
                "pass only one of prompt and prompt_file"
            )
        if prompt is not None:
            if not isinstance(prompt, str):
                raise HarnessAdapterError("prompt must be text")
            return prompt
        if path is None:
            raise HarnessAdapterError(
                "one of prompt or prompt_file is required"
            )
        with open(path, "r", encoding="utf-8") as stream:
            return stream.read()

    def build_invocation(
        self,
        entry: EndpointEntry,
        cwd: PathLike,
        *,
        prompt: Optional[str] = None,
        prompt_file: Optional[PathLike] = None,
        output_file: Optional[PathLike] = None,
        effort: Optional[str] = None,
        add_dirs: Sequence[PathLike] = (),
    ) -> HarnessInvocation:
        """Build argv and the stdin payload for one ready-to-run dispatch."""
        stdin = self.prompt_stdin(
            prompt=prompt, prompt_file=prompt_file
        )
        argv = self.build_argv(
            entry,
            cwd,
            prompt=prompt,
            prompt_file=prompt_file,
            output_file=output_file,
            effort=effort,
            add_dirs=add_dirs,
        )
        return HarnessInvocation(argv=tuple(argv), stdin=stdin)

    def _check_entry(self, entry: EndpointEntry) -> None:
        if entry.kind != HARNESS_KIND:
            raise HarnessAdapterError(
                f"entry '{entry.id}' is not a harness entry (kind: {entry.kind})"
            )
        if entry.harness != self.name:
            raise HarnessAdapterError(
                f"entry '{entry.id}' names harness {entry.harness!r}, not {self.name!r}"
            )
        if not isinstance(entry.model, str) or not entry.model:
            raise HarnessAdapterError(
                f"entry '{entry.id}' has no non-empty model for {self.name}"
            )

    def _effective_effort(
        self, entry: EndpointEntry, override: Optional[str]
    ) -> Optional[str]:
        value = override if override is not None else entry.effort
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise HarnessAdapterError(
                f"{self.name} effort must be a non-empty string (got {value!r})"
            )
        if self.effort_menu is not None and value not in self.effort_menu:
            accepted = "|".join(sorted(self.effort_menu))
            raise HarnessAdapterError(
                f"{self.name} does not accept effort {value!r}; "
                f"accepted efforts: {accepted}"
            )
        return value

    def _validate_prompt_args(
        self,
        *,
        prompt: Optional[str],
        prompt_file: Optional[PathLike],
    ) -> None:
        """Check stdin-source arguments without reading a file during argv build."""
        path = _prompt_file(prompt_file)
        if prompt is not None and path is not None:
            raise HarnessAdapterError(
                "pass only one of prompt and prompt_file"
            )
        if prompt is not None and not isinstance(prompt, str):
            raise HarnessAdapterError("prompt must be text")


@dataclass(frozen=True)
class CodexAdapter(HarnessAdapter):
    """Build the sanctioned Codex argv through ``bootstrap_lib.codex``.

    ``codex exec`` consumes its prompt from stdin (the shared builder emits a
    final ``-``) and can write its answer to ``output_file``. The adapter does
    not copy any Codex flags here: ``build_codex_exec_argv`` remains the sole
    owner of that command line, including Windows launcher handling.
    """

    argv_prefix: Optional[Sequence[str]] = None

    name: ClassVar[str] = CODEX_HARNESS
    effort_menu: ClassVar[EffortMenu] = CODEX_EFFORT_MENU

    def build_argv(
        self,
        entry: EndpointEntry,
        cwd: PathLike,
        *,
        prompt: Optional[str] = None,
        prompt_file: Optional[PathLike] = None,
        output_file: Optional[PathLike] = None,
        effort: Optional[str] = None,
        add_dirs: Sequence[PathLike] = (),
    ) -> list[str]:
        """Build one Codex argv, leaving the brief on stdin.

        ``add_dirs`` is forwarded rather than dropped because codex documents a
        write that fails SILENTLY without it: under ``-s workspace-write`` the
        session scratchpad is outside the writable root, so a unit told to
        write there exits 0 having written nothing. An adapter that cannot
        express the flag would render a command missing it.
        """
        self._check_entry(entry)
        self._validate_prompt_args(
            prompt=prompt, prompt_file=prompt_file
        )
        effective_effort = self._effective_effort(entry, effort)
        _absolute_path(cwd, "cwd")
        if output_file is not None:
            _absolute_path(output_file, "output_file")

        # This import must stay inside dispatch. Consumers that only link this
        # package are allowed to import its adapter types without provisioning
        # bootstrap_lib; the missing shared library should fail only here.
        from bootstrap_lib.codex import build_codex_exec_argv  # noqa: PLC0415

        kwargs = {"root": cwd, "model": entry.model}
        if effective_effort is not None:
            kwargs["effort"] = effective_effort
        if output_file is not None:
            kwargs["output_file"] = output_file
        if add_dirs:
            kwargs["add_dirs"] = [
                _absolute_path(d, "add_dirs") for d in add_dirs
            ]
        if self.argv_prefix is not None:
            kwargs["argv_prefix"] = self.argv_prefix
        return build_codex_exec_argv(**kwargs)

    def is_available(self) -> bool:
        """Use bootstrap's existing Codex presence detector, without a model run."""
        from bootstrap_lib.codex import detect_codex  # noqa: PLC0415

        return bool(detect_codex().available)


def resolve_opencode_cli() -> Optional[tuple[str, ...]]:
    """Resolve the OpenCode launcher, including Windows batch shims.

    ``shutil.which`` is intentional: on Windows it honors PATHEXT, and a
    resolved ``.cmd`` or ``.bat`` must go through ``cmd /c`` because it is not
    an executable image. This is a path lookup only; it never starts OpenCode
    and never checks a provider or model server.
    """
    resolved = shutil.which(OPENCODE_EXECUTABLE)
    if resolved is None:
        return None
    if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        return ("cmd", "/c", resolved)
    return (resolved,)


def detect_opencode() -> bool:
    """Return whether the OpenCode executable resolves on PATH."""
    return resolve_opencode_cli() is not None


@dataclass(frozen=True)
class OpencodeAdapter(HarnessAdapter):
    """Build the OpenCode one-shot command.

    The command is ``opencode run --pure --dir DIR -m MODEL --agent build
    [--variant EFFORT] --auto`` and the brief is piped on stdin. ``--auto`` is REQUIRED for a
    non-interactive run; it also BYPASSES PERMISSIONS, so it is a security
    decision rather than a convenience flag.

    OpenCode's ``--dir`` sets the working directory for the command but does
    NOT confine writes. An absolute path elsewhere in a repository can still
    be written, as verified against the live CLI. Callers must provide their
    own filesystem policy; this adapter does not treat ``--dir`` as a sandbox.

    OpenCode has no ``-o`` result-file flag and its ``--format json`` output is
    an NDJSON event stream, not a result object. The answer therefore remains
    on stdout, and a supplied ``output_file`` is rejected instead of dropped.

    ``--variant`` values come from the user's provider configuration. The
    adapter reports ``effort_menu is None`` and passes every non-empty string
    through; there is no exhaustive menu this package can honestly validate.
    """

    argv_prefix: Optional[Sequence[str]] = None

    name: ClassVar[str] = OPENCODE_HARNESS
    effort_menu: ClassVar[EffortMenu] = OPENCODE_EFFORT_MENU

    def build_argv(
        self,
        entry: EndpointEntry,
        cwd: PathLike,
        *,
        prompt: Optional[str] = None,
        prompt_file: Optional[PathLike] = None,
        output_file: Optional[PathLike] = None,
        effort: Optional[str] = None,
        add_dirs: Sequence[PathLike] = (),
    ) -> list[str]:
        """Build one OpenCode argv, leaving the brief on stdin."""
        self._check_entry(entry)
        if add_dirs:
            # Refused rather than ignored: opencode has no writable-root set to
            # extend -- `--dir` confines nothing in the first place -- so a
            # caller passing add_dirs has a confinement belief this adapter
            # cannot satisfy and should not appear to.
            raise HarnessAdapterError(
                "opencode has no add-dir flag; `--dir` does not confine writes, "
                "so there is no writable-root set to extend"
            )
        self._validate_prompt_args(
            prompt=prompt, prompt_file=prompt_file
        )
        effective_effort = self._effective_effort(entry, effort)
        cwd_text = _absolute_path(cwd, "cwd")
        if output_file is not None:
            # Rejected outright rather than path-checked first: opencode has no
            # result-file flag at all, so a relative path must not be told it is
            # relative when the real answer is that the parameter does not apply.
            raise HarnessAdapterError(
                "opencode has no output-file flag; its answer is emitted on stdout"
            )

        prefix = self.argv_prefix
        if prefix is None:
            prefix = resolve_opencode_cli()
        if prefix is None:
            raise HarnessAdapterError(
                "opencode is not on PATH, so no opencode command can be built"
            )

        argv = [str(part) for part in prefix]
        argv += [
            "run", "--pure", "--dir", cwd_text, "-m", entry.model,
            "--agent", OPENCODE_AGENT,
        ]
        if effective_effort is not None:
            argv += ["--variant", effective_effort]
        # --auto is required for a background/non-interactive invocation and
        # deliberately carries the permission bypass documented above.
        argv.append("--auto")
        return argv

    def is_available(self) -> bool:
        """Resolve OpenCode on PATH without probing a server or running a model."""
        return detect_opencode()


def resolve_harness_adapter(entry: EndpointEntry) -> HarnessAdapter:
    """Return the adapter named by a harness entry, or fail loudly.

    The entry model is intentionally consumed rather than reconstructed here:
    a transport entry cannot accidentally acquire a harness adapter, and an
    unknown harness name names both the bad value and the complete known set.
    """
    if entry.kind != HARNESS_KIND:
        raise HarnessAdapterError(
            f"entry '{entry.id}' is not a harness entry (kind: {entry.kind})"
        )
    harness = entry.harness
    adapter_type = {
        CODEX_HARNESS: CodexAdapter,
        OPENCODE_HARNESS: OpencodeAdapter,
    }.get(harness)
    if adapter_type is None:
        shown = harness if harness is not None else "<none>"
        known = ", ".join(KNOWN_HARNESSES)
        raise HarnessAdapterError(
            f"unknown harness {shown!r} for entry '{entry.id}' "
            f"(known harnesses: {known})"
        )
    return adapter_type()


__all__ = [
    "CODEX_EFFORT_MENU",
    "CODEX_HARNESS",
    "EffortMenu",
    "HarnessAdapter",
    "HarnessAdapterError",
    "HarnessInvocation",
    "KNOWN_HARNESSES",
    "OPENCODE_EFFORT_MENU",
    "OPENCODE_AGENT",
    "OPENCODE_EXECUTABLE",
    "OPENCODE_HARNESS",
    "CodexAdapter",
    "OpencodeAdapter",
    "detect_opencode",
    "resolve_harness_adapter",
    "resolve_opencode_cli",
]
