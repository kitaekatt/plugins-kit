# CLAUDE-potential-defects.md -- known defects deliberately deferred

This file's role is broader than any one admission route into it. Its purpose:

**Record known defects and gotchas that are severe enough that they should be
addressed, but that the project is not addressing right now.** A
`CLAUDE-potential-defects.md` is the durable home for that residue -- real,
severe, and knowingly unfixed -- wherever noticing it would otherwise mean
either fixing it on the spot (scope creep) or losing it (silent drop).

The coverage lane's `hazard-durability` rejection (a candidate fact that
describes a defect's transient state, and so cannot become ambient CLAUDE.md
prose -- see `standards/coverage-standards.md`) is one supported admission
route among several. A defect noticed during triage, surfaced and set aside
during a code review, or hit in passing during ordinary work -- and in every
case judged real, severe, and not worth fixing right now -- is admitted the
same way. Every route records where the entry came from (`source` below); none
of them makes the entry a finding, and none of them is a licence to go hunting
for more. Why the pattern's scope reaches beyond the coverage lane, and what
the schema looked like before it did: `references/provenance/standards-decisions.md`
(`potential_defects_broadened_v1_to_v2`).

## Placement rule

A `CLAUDE-potential-defects.md` is created in the **most relevant directory
associated with each defect** -- normally the directory of the anchor file
itself, or the narrowest directory that contains every anchor an entry names.
**A sidecar is never written without a co-located `CLAUDE.md`, in the same
turn.** A `CLAUDE.md` must already exist, or be created in that same write, in
that same directory, and it carries a one-line pointer to the sidecar file and
no entry content (see "Referenced, never ambient" below) -- the pointer is
required, never optional, and a directory whose own content would otherwise
earn no CLAUDE.md still gets a pointer-only one when it holds a sidecar. An
admission route that cannot arrange this (no way to create or edit the
same-directory CLAUDE.md) does not admit the entry. Different defects in
different directories get different sidecar files; do not centralize them.

## What still holds

These properties hold for every admission route, and are what keeps any of
them from turning this file into a defect-hunting pass or a second CLAUDE.md:

- **`observed` is separated from `suspected`.** The observation is cheap to
  state truly; the inference is where confident falsehood enters. Keeping them
  apart keeps an entry honest rather than merely fast.
- **Referenced, never ambient.** The CLAUDE.md in the same directory carries a
  one-line pointer and no entry content. The sidecar is not a composition
  input for CLAUDE.md generation or any other ambient-document process, so a
  defect claim recorded here can never hoist upward and become ambient
  guidance.
- **No file when there are no entries.** An empty file would read as a clean
  bill of health nothing established.
- **An absent file means "nothing was recorded", never "nothing is wrong
  here."** Do not read the absence of a `CLAUDE-potential-defects.md` as a
  clean bill of health for the directory.
- **The YAML format below**, schema version `"2"`.

Every field below is current state. For what schema version `"1"` looked like
and why it changed, see `references/provenance/standards-decisions.md`
(`potential_defects_broadened_v1_to_v2`) -- this document does not carry that
history.

## Format

A short prose header stating what the file is, then one YAML block. The two
entries below are both ILLUSTRATIVE, not a live defect register: `pd-1` is a
real observation carried forward from where this pattern was first used, in a
different project than this repository -- kept for its shape, not as a claim
about anything in plugins-kit. `pd-2` is invented outright, anchored on an
obviously fictitious path, specifically so this document is never mistaken for
an actual record of a plugins-kit defect.

```yaml
potential_defects:
  _schema_version: "2"
  entries:
    - id: pd-1
      anchor: docs/parity_ref/capture_web.mjs:3
      source: coverage-lane:hazard-durability
      verified: false
      observed: >-
        Imports playwright-core. It appears in neither package.json nor
        package-lock.json, and is absent from node_modules. The file's own
        header asserts it is "already in node_modules transitively".
      suspected: >-
        The script cannot run as checked in, and the documented remedy
        (npx playwright install chromium) fetches the browser binary rather
        than the package.
      checked: >-
        Nothing beyond the four reads above. Not executed.
      why_deferred: >-
        hazard-durability -- transient defect state; written as ambient prose
        it would fossilize into a false instruction once fixed.
    - id: pd-2
      anchor: example/fetch-report.py:40
      source: review
      verified: false
      observed: >-
        INVENTED FOR ILLUSTRATION. The retry loop catches OSError and retries
        unconditionally, with no cap on attempt count and no backoff.
      suspected: >-
        A persistent failure (bad credentials, unreachable host) spins forever
        instead of surfacing an error.
      checked: >-
        Read only; not exercised against a failing endpoint.
      why_deferred: >-
        Surfaced during review of an unrelated change; fixing the retry
        policy is out of scope for that change and is not blocking it.
```

`id` is stable within the file (`pd-1`, `pd-2`, ...). `observed` states only
what was seen and must be true as written. `suspected` carries every
inference, and is where a wrong entry is expected to be wrong. `checked`
records what was actually done, and "nothing beyond the read above" is a
complete and honest answer. `source` names the process the entry came from --
deliberately open free text, since the set of processes that can responsibly
defer a real defect is open-ended; a closed enum would either reject a
legitimate route or invite a false fit into the nearest existing tag.
`verified` is `false` at creation, always -- flipping it is the code-audit
capability's job (`capability-boundaries.md`), never the reporter's. `source`
and `why_deferred` together answer "where did this come from, and why are we
not fixing it now" -- `source` is a process, `why_deferred` is a reason, and
they do not restate each other.

## Where each route's rules live

This document is the schema and placement SSOT; it does not restate a route's
own admission criteria. Read the relevant place for those:

- **coverage-lane:hazard-durability** -- the admission criterion itself:
  `standards/coverage-standards.md`. The generation lane's write mechanics for
  this route (when it writes the file, what it must not touch):
  `lanes/generation-lane.md`.
- **Any route** -- who is accountable for VERIFYING an entry once it exists,
  and the capability that does not exist yet to do that at scale:
  `capability-boundaries.md` ("Code audit").

## The field-name split across the coverage-lane boundary

The coverage lane's structured agent-result schema
(`workflow/claude-md-generate.js`) carries `whyNotAmbient` in camelCase,
matching its neighbours `writtenFalseReason` and `candidatesRead` in that
schema's convention. The emitted FILE carries `why_deferred` in snake_case,
matching every other YAML block in this skill. The writing agent translates
one to the other, and also supplies the constant `source` and `verified`
values for that route (`coverage-lane:hazard-durability` and `false`) that the
agent-result schema does not carry per entry. Do not "fix" any side to match
another without changing all of them together.
