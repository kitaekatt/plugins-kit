"""Pluggable work-unit strategy: graph-walk vs. flat-chunk.

A pipeline needs to decide what "one unit of work" is before it can iterate.
A graph-walk strategy treats work units as nodes in a dependency or cadence
graph (structural adjacency matters -- e.g. an animate/hold cadence). A
flat-chunk strategy treats work units as an unordered, independently
processable list. Both strategies expose the same iteration interface so
``single_pass`` and ``convergence_loop`` do not need to know which one a
given pipeline chose.
"""

from typing import Protocol


class WorkUnitStrategy(Protocol):
    def units(self, store):
        ...
