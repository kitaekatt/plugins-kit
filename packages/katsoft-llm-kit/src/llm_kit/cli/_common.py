"""Shared pieces of the scripting groups: exit codes, JSON output, argument helpers.

The scripting groups (``account``, ``completion``, ``models`` and ``local``)
carry the verbs of the legacy llm-scripting-kit CLI, so they keep its exit-code
contract exactly. The values below are that contract; the ``jobs`` group keeps
job-kit's own (a different table) in :mod:`llm_kit.cli.jobs`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from llm_kit.cli._compose import create_backend
from llm_kit.dispatch.declaration import DeclarationSupportError, NoUsableRoutingTarget, describe
from llm_kit.registry.model_endpoints import EndpointRegistryError
from llm_kit.registry.models import EndpointResolveError, ModelResolveError


EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_HALT = 3
EXIT_PROTOCOL = 4
"""The request could not be understood, so no call was attempted.

Separate from EXIT_USAGE (2) on purpose, even though both mean "bad input".
EXIT_USAGE is argparse's territory -- a human typing a command wrong. This code
is for a MACHINE consumer's structured request, and the two want opposite
responses: a person re-reads --help, a program fixes its serializer. Separate
from EXIT_FAILURE (1) for the reason the protocol exists at all: 1 means a call
ran and failed, and retrying it may work; 4 means nothing ran and retrying the
same bytes cannot.
"""


EXIT_INDETERMINATE = 5
"""`probe` could not determine reachability -- the check itself did not run to
a verdict (e.g. an optional dependency such as `bootstrap_lib` was
unavailable), as opposed to running and finding the target down.

Distinct from EXIT_USAGE (2) on a DIFFERENT axis than EXIT_PROTOCOL is from
EXIT_FAILURE: EXIT_USAGE there means the endpoint NAME does not resolve to
anything configured -- a config problem, decided before any check runs.
EXIT_INDETERMINATE means the name resolved fine and a check was attempted, but
that check itself could not complete. A caller gating on `probe`'s exit code
must be able to tell "not configured" (2), "checked, and it is down" (1), and
"could not check" (5) apart, and must not treat 5 as though it were 1 --
skipping a possibly-live endpoint on the strength of an unrun check is exactly
the false negative this code exists to prevent. See DEFAULT_VERIFY_TIMEOUT_S
and reachability.STATUS_UNKNOWN for the full mapping and its rationale.
"""


def _json(value: Any, *, stream: Any = None) -> None:
    print(json.dumps(value, sort_keys=True), file=stream or sys.stdout)


def _add_endpoint_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--endpoint", default=None, help="Configured endpoint name.")


def _add_project_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-root", help="Project root for layered configuration.")


def _add_models_arg(parser: argparse.ArgumentParser) -> None:
    """``--models``: a declaration of registry ids."""
    parser.add_argument(
        "--models",
        action="append",
        default=None,
        help=(
            "Model declaration: registry ids, comma-separated or repeated. The "
            "first usable entry of the pace-ordered list is used."
        ),
    )


def _add_requirements_arg(parser: argparse.ArgumentParser) -> None:
    """``--requirements``: the capability requirement mapping, as in ``describe``."""
    parser.add_argument(
        "--requirements",
        default=None,
        help=(
            "Capability requirement mapping: a JSON file path, or inline JSON "
            "(starting with { or [). Entries that fail it are skipped."
        ),
    )


def _load_requirements(value: Optional[str]) -> Any:
    """The parsed ``--requirements`` value (file path or inline JSON), else None."""
    if value is None:
        return None
    text = value.strip()
    if text[:1] in ("{", "["):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"--requirements is not valid JSON: {exc}") from exc
    return json.loads(Path(value).read_text(encoding="utf-8"))


def _split_ids(values: Optional[list[str]]) -> list[str]:
    """Flatten repeated and comma-separated id arguments, keeping order."""
    ids: list[str] = []
    for value in values or ():
        ids.extend(part.strip() for part in value.split(",") if part.strip())
    return ids


def _declared_models(args: argparse.Namespace) -> Optional[list[str]]:
    """The declaration named by --models, else None."""
    if getattr(args, "models", None):
        return _split_ids(args.models)
    return None


def _backend_factory(name: str, **kwargs: Any) -> Any:
    """``create_backend`` looked up at call time, so the module seam holds."""
    return create_backend(name, **kwargs)


def _first_usable(ids: list[str], project_root: Optional[str], requirements: Any = None) -> str:
    """The default entry of ``describe`` for a process caller; raises the floor."""
    ranking = describe(
        ids, project_root=project_root, caller="process", backend_factory=_backend_factory,
        requirements=requirements,
    )
    assert ranking.default is not None  # describe() raises the floor otherwise
    return ranking.default.id


def guarded(call: Callable[[], int]) -> int:
    """Run one scripting verb under the legacy error-to-exit-code mapping.

    The routing floor is a failure (exit 1: a reset window can clear it);
    configuration, registry, OS and value errors are usage errors (exit 2),
    reported as a JSON ``error`` object on stderr.
    """
    try:
        return call()
    except NoUsableRoutingTarget as floor:
        # The floor is the one selection error: loud, itemised, and a
        # failure rather than a usage error -- a reset window can clear it.
        _json({"error": floor.to_json()}, stream=sys.stderr)
        return EXIT_FAILURE
    except (
        EndpointResolveError, ModelResolveError, EndpointRegistryError, OSError, ValueError,
        DeclarationSupportError,
    ) as exc:
        _json({"error": {"kind": "configuration", "message": str(exc)}}, stream=sys.stderr)
        return EXIT_USAGE
