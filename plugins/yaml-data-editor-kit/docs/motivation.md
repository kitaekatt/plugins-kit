# Motivation: why this kit exists

Why the yaml-data-editor-kit was built, and what problem shaped its
design. This is background, not a contract -- `dialect.md` is the
normative spec and the code wins over both.

This document is a neutral restatement of the originating specification
(2026-08-21). The verbatim original is retained privately by the author
and is deliberately not reproduced here.

## The problem

AI-assisted development is effectively serial: a person asks, then waits.
Throughput is capped by how fast one agent can be supervised. At the same
time, agents work best against clear specifications, concrete examples,
and real data -- artifacts that are usually scattered across prose
documents, code, and someone's memory.

## The core idea

Make everything data. Domain content, the data model that describes it,
and work specifications all live as YAML. An interface lets a person
create and edit that data starting from nothing. A comment system layered
over the data lets them queue work *in the context of the thing being
commented on*, then dispatch it asynchronously -- so authoring and
implementation stop blocking each other.

## What follows from that

**One format.** Domain data, the schema describing it, and the
specifications for changing it are all YAML. One format means one editor,
one comment system, one diffing story, and one thing an agent has to
understand.

**Comments carry their anchor.** A comment attaches to a specific address
in the data -- a record, a field, a document. It is written primarily for
an agent to consume rather than for human threading. Because the anchor
travels with the comment, the agent inherits the context automatically:
it knows *what* the note is about without anyone restating it. A comment
is a unit of intent -- a defect, a request, a question, a constraint.

**Anchors must survive edits.** Data moves underneath a comment. An
anchor that silently retargets to whatever now occupies its position is
worse than one that fails loudly, so the kit hashes the addressed slice
when the comment is written and reports the anchor as moved when the
content underneath it changes.

**Starting from zero is a requirement, not a convenience.** The interface
must be able to create the schema, then the data, then the work items,
with no pre-existing project and no seed content.

## What the kit is, and is not

The kit is a LIBRARY. It provides the profile/dialect loader, the corpus
model, validation, the address grammar, the comment store, content
hashing and staleness detection. It ships no CLI, skill, or command
surface, and it holds no policy.

Anything that branches on, defaults to, or requires a noun belonging to a
particular project does NOT belong here. The test is substitution: rename
every noun in a consuming project's profile, and the kit must behave
identically. Locks, triage vocabulary, queues and task conventions all
fail that test and live in the consumer.

The planning, dispatch and editor packages are declared but unbuilt. Do
not read them as work in flight; see `architecture.html` for current
status.
