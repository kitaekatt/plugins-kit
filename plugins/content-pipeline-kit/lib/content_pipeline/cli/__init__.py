"""cli -- CLI scaffold, decomposing the monolithic facade pattern.

Both source systems this plugin unifies grew a single multi-thousand-line
CLI facade over time. This package is the reusable scaffold a thin
per-project CLI wires per-command modules onto instead: ``scaffold`` (arg
dispatch, scope filtering, typo did-you-mean), ``budget`` (budget guard /
hard-stop on 429/401, auth-expiry preflight), ``bulk`` (the two-phase
cache-warm bulk worker), and ``unsupported`` (the sticky unsupported-stub
registry -- exclude an entity forever once flagged, no re-paying the same
LLM call every run).
"""
