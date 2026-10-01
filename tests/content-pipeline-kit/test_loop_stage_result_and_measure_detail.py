"""CLF F2/F3: STAGE_FINISHED carries the stage return value; measure_from
attaches consumer detail to Round.detail."""

from content_pipeline.pipeline import convergence_loop as cl
from content_pipeline.pipeline.cell_policy import CellOutcome, measure_from


class _Policy:
    def outcome(self, cell):
        return CellOutcome(cell)


def test_stage_finished_event_carries_stage_return_value():
    events = []

    def grade(store, cycle):
        return None  # mutate in place; result stays None

    def select(store, cycle):
        return {"winner": "a"}  # a non-store return value

    def apply(store, cycle):
        return {"cells": ["locked"]}  # a replacement store

    cl.run(
        {"cells": ["open"]},
        grade=grade,
        select=select,
        apply=apply,
        measure=measure_from(lambda s: s["cells"], _Policy()),
        max_cycles=1,
        observers=[events.append],
    )
    finished = {
        e.stage: e for e in events if e.kind is cl.LoopEventKind.STAGE_FINISHED
    }
    assert finished["grade"].result is None
    assert finished["select"].result == {"winner": "a"}
    assert finished["apply"].result == {"cells": ["locked"]}
    # Only STAGE_FINISHED carries a result; other kinds leave it None.
    assert all(
        e.result is None
        for e in events
        if e.kind is not cl.LoopEventKind.STAGE_FINISHED
    )


def test_measure_from_attaches_detail_of_to_round_detail():
    seen = []

    def detail_of(store):
        seen.append(store)
        return {"n": len(store["cells"])}

    store = {"cells": ["locked", "open"]}
    m = measure_from(lambda s: s["cells"], _Policy(), detail_of=detail_of)
    rnd = m(store)
    assert rnd.detail == {"n": 2}
    assert seen == [store]  # called once, with the store the measure read


def test_measure_from_without_detail_of_leaves_detail_empty():
    m = measure_from(lambda s: s["cells"], _Policy())
    assert m({"cells": ["open"]}).detail == {}
