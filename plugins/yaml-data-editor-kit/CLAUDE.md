# CLAUDE.md

## What this plugin is

**yaml-data-editor-kit** is a LIBRARY, not a tool. It ships a Python package
(`lib/yaml_data_editor_kit/`) and, later, a web editor surface. It has no
standalone skill or command surface of its own at this stage -- it exists to
be consumed by a project through the packages below.

## The profile boundary

This plugin defines a project-independent YAML schema dialect: a way to
declare types, shapes, and constraints in YAML. It DEPENDS ON no
vocabulary belonging to any consuming project: no project-specific noun
is hardcoded in this plugin's code, and no construct in the schema
grammar is named for, shaped around, or meaningful only to one project's
data. The test is substitution -- rename every noun in a consuming
project's profile and the kit behaves identically.

**Illustrative examples ARE permitted**, in the dialect documentation and
in test fixtures, and are usually drawn from the motivating corpus
because a construct is easier to judge against real data than against
`foo.bar`. An example noun is not a dependency; it is documentation of a
shape. What is forbidden is the kit KNOWING that noun -- branching on it,
defaulting to it, or requiring it to exist.

(Amended 2026-08-23. The rule previously said no project noun could
appear "anywhere in this plugin's code, schema grammar, or examples",
which the dialect spec's own worked examples had never satisfied. It was
restated to what it demonstrably means rather than scrubbing examples
that cost nothing and explain more.)

A consuming project supplies a **profile**: a document written *in* the
dialect this plugin defines, naming that project's own types and fields.
The profile lives in the consuming project, not here. This plugin never
reads, imports, or hardcodes a profile -- it only interprets the dialect a
profile is written in.

## The file seam

The editor (node-side, browser-facing) and the dispatcher (Python, this
package) never call each other directly -- no shared process, no RPC, no
in-memory handoff. They meet only at files on disk -- the corpus, the
comment store, and a dispatch request. This is what "everything is data"
means operationally: it keeps the two sides independently testable and
independently deployable, and keeps the Python side free of any UI
framework dependency.

## The four packages

- `schema/` -- the YAML schema dialect: type and shape declarations a
  profile is written against. Owns dialect parsing and validation; owns
  nothing about any specific profile's content.
- `comments/` -- the anchor and comment model. An ANCHOR is the address a
  comment attaches to -- a document, a record, or a field -- not YAML's own
  `&anchor`/`*alias` syntax. A COMMENT is one anchored unit of intent, and
  it is written to be consumed by an LLM rather than threaded by humans;
  its anchor is what supplies its context, so the author never restates
  what the note is about. This package owns the address grammar, the
  persistence of comment records, and detecting that data moved underneath
  an anchor.
- `dispatch/` -- the planner and the content-pipeline-kit binding. The
  planner reads the UNRESOLVED comments across a corpus and shapes them
  into work units: comment count is not unit count, and not agent count.
  Each unit is then executed inline or delegated to an agent. This is the
  only package that imports content-pipeline-kit -- and `bootstrap.json`
  deliberately declares no dependency edge to it yet, because nothing here
  imports it. The FIRST import of `content_pipeline` must add both
  `shared_lib_imports: ["content_pipeline"]` and a `plugins[]` entry for
  `plugins-kit:content-pipeline-kit` with `install: "auto"`; without them
  the import resolves in development and fails on a consumer's machine.
- `editor/` -- the web surface. Node-side code will live here; today it
  holds only the package placeholder. Communicates with `dispatch/` solely
  through the file seam above.
