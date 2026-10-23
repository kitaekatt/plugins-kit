"""Error vocabulary shared by the swapper service and its process layer.

``swapper`` (the service) and ``_swapper_process`` (the process primitive)
both raise these. They live in this leaf so the process layer imports no
service module; ``swapper`` re-exports every name, so
``swapper.ProcessNotFound`` is this very class. ``exit_code`` is the CLI
contract, so the facade maps without logic.
"""

from __future__ import annotations

EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INDETERMINATE = 5


class SwapperError(Exception):
    """Base error. Operation failure unless a subclass says otherwise."""

    exit_code = EXIT_FAILURE


class ProcessNotFound(SwapperError):
    """A PID does not exist, or was reused (different create_time)."""


class InspectionIndeterminate(SwapperError):
    """The process table could not be read well enough to decide."""

    exit_code = EXIT_INDETERMINATE
