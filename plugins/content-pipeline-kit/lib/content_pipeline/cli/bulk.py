"""Two-phase cache-warm bulk worker.

Runs a bulk operation over many entities in two phases: a cache-warm phase
that issues every request through ``llm.platform``'s content-addressed cache
(so re-running a partially-completed batch never re-pays for already-cached
completions), then an apply phase that only touches entities whose cache
entry is ready. Separating the phases lets a bulk run be interrupted and
resumed without redoing already-warm work.
"""


def run_bulk(entities: list, stage, cache_dir) -> object:
    """Run the two-phase cache-warm bulk worker over a list of entities."""
    raise NotImplementedError
