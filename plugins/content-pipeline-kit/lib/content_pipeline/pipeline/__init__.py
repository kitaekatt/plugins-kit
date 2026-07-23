"""pipeline -- stage orchestration for both pipeline shapes.

``stage`` declares the Stage protocol every orchestration shape composes:
a pure function over the store. ``single_pass`` is the regenerate-on-stale
shape (two-phase generate / apply) -- the shape a single-pass consumer
ports onto first. ``convergence_loop`` is the fill -> grade -> select ->
apply cycle, driven to a verdict by ``llm.convergence`` -- built as a seam
in the core phases but implemented last, in the pre-port phase, so it is
never speculative. ``workunit`` is the pluggable work-unit strategy: a
graph-walk (structural cadence) or a flat-chunk split, selected per
pipeline.
"""
