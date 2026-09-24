"""Model declarations in a workflow: parsing, and agent-step routing.

A ``model:`` field is a model DECLARATION: a registry id, or a list of them,
naming which model(s) may do the step (the format is specified in bootstrap's
plugin-dev skill, ``references/model-declaration.md``). The loader validates
its SHAPE with ``bootstrap_lib.model_declaration``; which id can route is
decided here, at compile time, per node kind.

An agent step routes through the Workflow tool's ``agent()``, which the
harness drives, so only the Claude core ids (``fable``, ``opus``, ``sonnet``,
``haiku``) are routable for it. The first core id in declaration order is
compiled; every other id is skipped silently -- no notice, and nothing about
it in the emitted script. A declaration with no core id at all is the floor:
a compile error that itemises every declared id. workflow-kit imports no
llm-scripting-kit here, because Claude routing belongs to the harness.

An openrouter node passes its declaration to the node runner unchanged, as
the comma carrier; llm-scripting-kit decides there (``scripts/openrouter_run.py``).

``bootstrap_lib`` is a REQUIRED shared lib of workflow-kit: its bootstrap.json
links it, and bootstrap is a declared dependency of every plugin. An absent
copy and a copy predating ``model_declaration`` are diagnosed apart, because
the repairs differ.
"""

from __future__ import annotations

from typing import Any, Optional

from .errors import WorkflowError

#: The bootstrap release that shipped ``bootstrap_lib.model_declaration``.
MODEL_DECLARATION_BOOTSTRAP = "0.129.0"

#: The node executor's model (W3, W4): a one-entry declaration. The agent
#: definition's frontmatter and the preamble's ``agent()`` options can carry
#: only a scalar, so each writes this declaration's single id; a test pins
#: that both agree with it.
EXECUTOR_MODELS = ("haiku",)


def _model_declaration() -> Any:
    """Return ``bootstrap_lib.model_declaration``, probed for the symbols used."""
    try:
        import bootstrap_lib  # noqa: F401, PLC0415
    except ImportError as exc:
        raise WorkflowError(
            "workflow-kit validates model declarations with bootstrap_lib, which is "
            "not linked into this environment: the plugins-kit:bootstrap plugin has "
            "not provisioned workflow-kit here. Install or enable bootstrap "
            "(`claude plugin install bootstrap@plugins-kit`), start a new session, "
            "and run this with workflow-kit's venv python."
        ) from exc
    try:
        from bootstrap_lib import model_declaration  # noqa: PLC0415
    except ImportError:
        model_declaration = None
    if (
        model_declaration is None
        or not callable(getattr(model_declaration, "parse", None))
        or not isinstance(getattr(model_declaration, "CORE_IDS", None), frozenset)
    ):
        raise WorkflowError(
            "the linked bootstrap_lib predates model_declaration.parse/CORE_IDS; "
            f"workflow-kit needs bootstrap >= {MODEL_DECLARATION_BOOTSTRAP}. Run "
            "`claude plugin update bootstrap@plugins-kit` and start a new session."
        )
    return model_declaration


def parse_declaration(value: Any, key: str, where: str) -> Optional[tuple]:
    """Structurally validate an optional ``model:`` value; None when absent."""
    if value is None:
        return None
    module = _model_declaration()
    try:
        return tuple(module.parse(value))
    except module.DeclarationError as exc:
        raise WorkflowError(f"{where}: {key!r} is not a valid model declaration: {exc}") from exc


def agent_model(declared: Optional[tuple], where: str) -> Optional[str]:
    """The id an agent step compiles to, or None when the step declares none.

    Skipping is silent; a declaration with no routable id raises the floor.
    """
    if declared is None:
        return None
    core = _model_declaration().CORE_IDS
    for entry in declared:
        if entry in core:
            return entry
    items = "\n".join(
        f"  {entry}: unroutable here (an agent step routes only the Claude core ids "
        f"{', '.join(sorted(core))}; not a core id, or not a registry id at all)"
        for entry in declared
    )
    raise WorkflowError(
        f"{where}: no usable routing target in [{', '.join(declared)}] for an agent step:\n{items}"
    )


__all__ = [
    "EXECUTOR_MODELS",
    "MODEL_DECLARATION_BOOTSTRAP",
    "agent_model",
    "parse_declaration",
]
