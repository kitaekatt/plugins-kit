# Technical English

How to write a document a person reads to find their way around a codebase.
Loaded alongside the artifact's own contract, which decides WHAT the document
contains. This decides how the prose reads.

## Who is reading

The owner of this code, returning after weeks away. They know the domain, so
ECS, WASM, schema, worker, runtime and their kin need no explanation. They do
not remember this project's coined phrases, its abbreviations, or the shape of
the machinery that generated the document. Prefer plain words for those.

A newcomer should be able to follow the document. Efficient re-entry for the
owner comes first.

## The vocabulary in your instructions is not vocabulary for the page

Your instructions describe the job using their own machinery: what a document
owns, the territory it covers, its responsibility boundary, its ownership
surface. Those words tell YOU which directories are yours. They tell the reader
nothing about the software, and a page that says "the owned territory contains
the ECS simulation" has published its own scaffolding.

Write what the software does instead: defines, contains, implements, starts,
loads, generates, documents, validates, renders, configures, calls, stores,
reads, writes.

- Write: `systems.c` implements the native simulation.
- Not:   `systems.c` owns the native simulation behavior.

`owns` is fine where ownership is real behavior -- a subsystem that owns a lock,
a process that owns a socket. The rule is about describing documents, not
describing programs.

## Open with a model, then the taxonomy

The first paragraph says what this area is and why the reader came. It is not
the place for the inventory, the exclusions, or the routing choice -- those land
after the reader has something to attach them to.

- Write: This is the main working repository for the game. It contains the
  runtime, game content, client, tools and documentation.
- Not:   This territory owns the current ECS simulation and its runtime
  boundaries.

When the reader must choose between destinations, explain the distinction in
ordinary words before naming the categories.

## Rank the destinations

Say where to start and why. One primary entry point per section when one
exists; secondary files only when they help the reader choose what to open
next. Where there is no single best start, say what separates the alternatives.
Label what is generated rather than authored, and what is planned rather than
current -- those are the distinctions an owner forgets first.

A "Where to start" section stops at two sentences: the entry point and why, or
two alternatives and what separates them. The overrun is a second destination
carrying a rationale of its own; let the file listing carry that one. Count the
sentences before you move on.

Link what the reader is likely to open. A sentence whose content is mostly
filenames has ranked nothing.

- Write: Start with `systems.c` for simulation behavior. Use
  `system-ordering.md` when you need execution order.
- Not:   an unranked list giving both files equal weight.

## Unpack noun stacks into verbs

When a phrase makes the reader work out how three nouns relate, use a verb.

- Write: The client reads the generated game data and renders it.
- Not:   the generated-game-data client presentation path

Repeat the same term for the same thing. Varying it for style costs the reader
a comparison every time.

## Routing is the job, not a lapse in style

A page that points somewhere is doing its work. Say where to start, what each
destination is for, when to choose one over another, and what is primary. Keep
explanation in service of those decisions rather than expanding into an essay.

## Fix density by cutting, not by adding

Dense prose is too many ideas in too little room. The repair order is: cut what
does not change a navigation decision, separate the ideas that were fused, make
the priority explicit, and only then add a sentence of explanation. Length is
the last resort, never the first. A short document with a clear hierarchy beats
a longer one carrying the same compression.

## Before you finish

Read the document as the owner, and answer four questions from it alone: What
is this area for? What do I open first? Where do I go for my kind of question?
What can I ignore for now? An answer you cannot find is the revision.
