"""content_pipeline -- library for LLM-in-the-loop batch content pipelines.

This package provides generic machinery shared by LLM-in-the-loop content
systems, with zero project-specific knowledge: an attributed canonical store,
a two-tier content-hash freshness engine, a one-rule-many-
call-sites validator contract, a tiered context-provider registry, an LLM
platform binding (transport / cache / cost / budget / convergence over an
OpenAI-compatible endpoint, reusing llm-scripting-kit for key + model + client),
stage orchestration for both a single-pass and a convergence-loop pipeline
shape, two delivery modes (in-place mutation and append-only projection), a
VCS seam (git-default, Perforce ships in p4-kit), a human-in-the-loop
round-trip abstraction, and an audit framework that reuses the runtime's own
classifiers.

Deliberately re-exports NOTHING here -- submodule imports only
(``from content_pipeline.freshness import classify``, not
``from content_pipeline import classify``). This keeps the import graph a
strict DAG: importing ``freshness`` never drags in ``llm``, importing
``store`` never drags in ``vcs``. See each subpackage's own ``__init__.py``
for its scope (REP: independently reusable; CCP: single-owner modules for
things that change together; CRP: opt-in components -- guards, convergence,
round-trip, VCS -- never reached from the core import path).
"""
