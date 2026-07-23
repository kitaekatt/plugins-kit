# Porting a Pipeline

> **Status: skeleton -- to be expanded.** This reference is a structural
> outline drawn from the plugin proposal; each section carries only the
> 2-4 sentences the proposal already established. Expand with a worked
> module-by-module port log as real ports happen.

## 1. Freshness first

Port the existing tool's freshness logic onto `content_pipeline.freshness`
before anything else. It is the purely-functional subsystem (no LLM, no
VCS, no I/O side effects to mock), so it validates the entire "extract a
shared seam, port one system, prove equivalence" workflow at the lowest
possible risk before touching LLM-bearing subsystems.

## 2. Pin an equivalence baseline before collapsing a module

Before deleting or collapsing any existing module, run its existing test
suite against the ported code as a pinned equivalence baseline. A green
baseline is the proof the seam is right; a module is not considered ported
until its own tests (not just the new library's tests) pass against the new
implementation.

## 3. Map subsystem by subsystem

Work through the existing tool's modules in dependency order, mapping each
onto the `content_pipeline` sub-package it collapses onto (store, validate,
providers, llm, pipeline, deliver, vcs, audit, cli -- see this domain's
vocabulary in `SKILL.md`). Most modules collapse almost entirely into the
library; what remains in the project-side binding is the genuinely
project-specific residue -- field schemas, prompt content, domain rules.

## 4. Honestly annotate what is retired

For every module that collapses, record explicitly what stays in the
project-side binding versus what disappears entirely into the library. A
module that collapses to "nothing remains" (its generic body was the whole
module) should be deleted outright, not kept as a stub -- honest annotation
means the port log states this plainly rather than leaving dead code as a
hedge.

## 5. Re-run the existing suite as the equivalence oracle

After the full port, re-run the existing project's test suite (not a new
one written against the library) as the final equivalence oracle. The
project's tests encode real behavior the new library implementation must
reproduce; a passing library-only test suite is not sufficient proof the
port preserved behavior.
