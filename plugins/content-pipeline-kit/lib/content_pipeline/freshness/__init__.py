"""freshness -- two-tier content-hash staleness engine.

The most duplicated subsystem across the two systems this plugin unifies,
and the cleanest (pure, no LLM, no VCS, no I/O side effects to mock) --
which is why it is built and ported first. A two-tier design: a cheap source
hash and a generation hash, cross-referenced against a corpus so a change
anywhere upstream of a generated value is detected without re-deriving that
value. ``classify`` is the single predicate every "needs regen" check and
every coverage-bucket site delegates to, so there is exactly one place that
answers "is this fresh."
"""
