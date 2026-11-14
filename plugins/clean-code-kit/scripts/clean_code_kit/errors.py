"""The two failure classes the command line maps to exit codes."""

from __future__ import annotations

EXIT_TOOL_FAILED = 1
EXIT_REFUSED = 2


class Refusal(Exception):
    """A usage, config or environment problem. Exit 2; the message names the file or path at fault."""


class ToolFailure(Exception):
    """A metric tool failed. Exit 1; the message carries the tool's stderr verbatim."""
