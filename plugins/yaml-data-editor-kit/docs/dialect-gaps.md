# Dialect gaps found by writing the first profile

Source: the `pd-profile` unit, 2026-08-21. 45 profile documents, 1244 lines,
written against the real corpus. Every finding below is evidenced by a corpus
file the profile could not express.

**Status: each row below carries its own marker -- APPLIED, RESOLVED, or
UNAPPLIED -- and that marker, not this paragraph, is the authority on
whether `docs/dialect.md` reflects it.** The loader was being implemented
against the current spec when these arrived; revising the spec
mid-implementation is how a loader ends up built against a superseded
contract, so most findings waited for the loader to land, and several
landed piecemeal as the loader was built out (D-6, D-9, D-16, L-18, L-19)
rather than as one pass. D-3 and L-16 (marked RESOLVED) were applied
together, most recently -- they have no single-line citation because the
ruling spans the whole "Field annotations" section of `docs/dialect.md`
(the `required`, `sentinel` prose starting there), not one sentence.

Every other row was checked against `docs/dialect.md` by grep, most recently
on 2026-08-26, and carries either a line citation or "no hit" -- treat an
individual row's marker as current, re-grep before trusting an old citation,
and do not treat this file as spec on its own even where a row is marked
APPLIED: `docs/dialect.md` is the spec, this file is the record of how it
got there.

**The APPLIED/UNAPPLIED standard, stated once so rows are marked
consistently:** a row is APPLIED only when `docs/dialect.md` states the
actual behaviour or consequence the row describes, not merely a prose
admonition that gestures at the same restriction. "Do not restate `via:`
under `fields:`" and "`shape_from:` is deliberately limited to a map value"
are both admonitions, not statements of what happens on violation (a load
error, in both cases) -- so both L-8 and L-9 are marked UNAPPLIED under this
standard, even though each already has SOME prose nearby.

## The headline

The hypothesis "the dialect can express this corpus" is **refuted in part**.
About 85% is expressible, and several constructs land exactly on real data --
`hidden: enum [true, false, TBD]`, `open: {prefix: flag_}`, integer-keyed
enums, `partial_of` + `routes`, `shape_from` + adapter, `ordered` on a
bit-position list. The failures are not scattered. They cluster in two
places, and both are worth stating as the actual shape of the problem:

1. **Documents that are a MAP, not a record.** Every layout assumes the thing
   on disk is a record or a sequence of records. A file that is a map from an
   id to a non-record value -- a map of numbers, a list of refs -- has no
   home.

   **SUPERSEDED.** This is no longer true. `value:` (dialect.md:72-125, D-9
   row below, marked APPLIED) declares exactly this shape: a type whose
   records ARE values rather than field-bearing bodies, loaded via a
   `keyed_map` source.
2. **Anything that must reach ACROSS a type.** A key set that is the union of
   two sets; a foreign field that is really a reference; a view column drawn
   from a second type; a reverse index. The dialect is strong within one type
   and weak between types.

## Findings, by severity

| id | What breaks | Corpus evidence | Minimal fix |
|---|---|---|---|
| D-9 | **APPLIED** (dialect.md:72, `value:` -- records that are values) -- `keyed_map` requires `of:` to name a TYPE, but the values are a map of numbers or a list of refs | `prices.yaml`, `stat-pools.yaml` (both inexpressible), `weapon-stats.yaml` (half) | let `keyed_map` take `value:` as an alternative to `of:`; let `of:` name the type whose fields are the metadata keys |
| D-12 | **UNAPPLIED** (no hit for `shape_overrides`) -- `shape_from` cannot enrich an INDIVIDUAL foreign field, so a declared `string` that is really a ref, and an `i32` that is really an enum, stay dumb | all 35 `entities/*.yaml`, `app.yaml` `spawn_list` | `shape_overrides:` on the `shape_from` declaration |
| D-5 | **APPLIED** (dialect.md:458-477, `total: true`) -- no way to say a map is TOTAL over its key enum | `manifest.yaml` `counts`; `prices.yaml` states a missing pair is a BUILD FAILURE | `total: true` on a map field |
| D-1 | **UNAPPLIED** (dialect.md:221 added a single-path LIST-OF-SCALARS form, a different capability; a UNION of two paths, the fix this row actually proposes, has no hit) -- `values_from:` takes ONE path, but a real key set is a UNION of two | `gear.weapon_stats` keys = `stat.id` + `weapon_stat_table.derived_fields` | `values_from:` accepts a list of paths |
| D-16 | **APPLIED** (dialect.md:665, `field:` takes a path, `{ field: weapon_stats.damage, ... }`; dialect.md:699, `covers:` compares field paths by PREFIX) -- `view` `field:` has no PATH form, so it cannot address a map key -- which also breaks `covers:` (card names the map, table names three paths) | `templates/items.yaml` `headers_weapon_extra` | a path form for `field:`, plus how a path relates to its parent under `covers:` |
| D-2 | **UNAPPLIED** (no hit for `partition_by` or a `{segment}` placeholder) -- a record set partitioned across files by a filename segment; `rows` takes one literal path | `names-{armor,helm,accessory,charm}.yaml` | `path:` with a `{segment}` placeholder + `partition_by:` |
| D-3 | **RESOLVED** -- a MEANINGFUL explicit `null` (present-and-null is an assertion; absent is silence) | `stats.yaml` `sashimi_field: null` on 11 of 30 | `sentinel:` may declare `null` as a member; a field whose sentinel set contains `null` treats an explicit `null` as PRESENT, carrying that sentinel's meaning (resolved together with L-16) |
| D-7 | **UNAPPLIED** (no hit for a `single`-covers-the-unclaimed-keys statement) -- two sources claiming different regions of ONE file; `single`'s wording reads as a conflict | `manifest.yaml`, claimed by `gear_manifest` (single) and `weapon_family` (rows) | one sentence: `single` covers the keys no other source claims |
| D-6 | **APPLIED** (dialect.md:744, identity-less `(file, ordinal)` rows) -- `rows` with no identity column; `identified_by:` is required | `open-decisions.yaml` is a `{what, where}` table | permit omission for `rows`, row index as the anchor address |
| D-8 | **UNAPPLIED** (no hit for `open_question:`) -- no home for an open question ABOUT a declaration (`meaning:` says what a value is for, nothing says what is unresolved) | `points.yaml`, eight owner TBD directives as trailing comments | an `open_question:` field annotation |
| D-10 | **UNAPPLIED** (no hit for a `disjoint` constraint kind; still three kinds at dialect.md:509-512) -- no `disjoint` constraint kind; `covers` asserts inclusion only | `weapon-stats.yaml` `fixed_stat_meanings` | a fourth constraint kind |
| D-14 | **UNAPPLIED** (no hit for `scope:` / `external:` on `ref`) -- no foreign-scope `ref`; the value degrades to bare string | `app.yaml` `imports.*.components` | `scope:` / `external: true` on `ref` |
| D-15 | **UNAPPLIED** (no hit for an `except:` on `values_from:`) -- `values_from:` cannot SUBTRACT, so four values are inlined two keys below the list they duplicate | `manifest.name_template` = slots MINUS one | an `except:` on `values_from` |
| D-0 | **UNAPPLIED, no construct proposed** (carried forward as Open question 3, dialect.md:835) -- a view of ONE type cannot express a JOIN -- confirmed structural, three instances over three type pairs | `templates/stats.yaml` (11 columns from 3 sources incl. a reverse index over 112 records), `tag_table`, `mechanic_card` | none proposed; the fix is a query construct |

Also noted, minor: `constraints.ids:` is documented as `<type>.<field>` but real
paths go two deep, and nested paths are used in `values_from:` too. The spec
is silent on whether they are legal. Say.

**SUPERSEDED.** The spec is no longer silent: dialect.md:510 states `ids:`,
`from:` and `to:` "take anchored paths and may be nested," with the worked
example `ids: app.templates.entities`.

## What this confirms

- **`generated_by:` earns its keep immediately.** Declaring it on the gear
  source makes the corpus's own misplacement -- a generated table in the
  hand-authored directory -- CHECKABLE rather than a prose rule that is
  already being broken. The check fires. That is the correct outcome.
- **The `abstract_flag` warning was right, empirically.** 33 of 35 entity
  templates are marked abstract, and three of those are named directly in the
  spawn list. Reading the flag as "never instantiated" would have silently
  disabled completeness checking on live records.
- **The profile could not live in the plugin.** Every type document names a
  project noun and several constraints name project file paths and generator
  names. The boundary is load-bearing, not stylistic.

## Untested, and it matters

Nothing loads or validates this profile -- no loader existed when it was
written. Every claim about what validates is a READING of the spec, not a
run. Specifically unverified: whether the merge algebra produces the right
flattened records for the 24 templates using `extends` (checked by hand on
one pair only), and whether `covers:` between the gear card and table passes
-- predicted to FAIL, for the reason in D-16.

Running the loader against this profile is the next real test, and it is
expected to produce a second finding list.

**SUPERSEDED.** The loader now exists and runs against this profile
routinely (the real-corpus gate in `devolver/`). The `covers:` prediction was
WRONG: D-16 resolved with `covers:`'s PREFIX rule (dialect.md:695-699), and
`gear_card`'s `covers: gear_table` passes -- the gate reports no covers
finding for that pair. Whether the `extends` merge algebra is correct for
all 24 templates has not been separately re-audited by this note; the gate
passing is evidence for it but was not re-verified template-by-template here.


# Spec defects found by implementing the loader

Source: the `dialect-loader` unit, 2026-08-21. 19 findings. The unit's
premise -- "the spec is complete enough to implement without a design
decision of my own" -- was REFUTED. It did not halt, because none of the
findings blocked a defensible implementation, but every resolution below is
the LOADER's choice and not the spec's, which means the spec and the code can
now disagree without either being wrong.

## Two genuine contradictions, not silences

**L-1. UNAPPLIED (no hit for the YAML-1.1 `on:` gotcha or the accept-both-spellings clarification). `variants: { on: ... }` is unloadable as written.** `on` is a YAML 1.1
boolean, so PyYAML resolves a bare `on:` key to `True`. The spec's own
example therefore does not parse into the shape the spec describes. The
loader accepts both spellings; the SPEC must either say so or rename the key.
Renaming is cleaner -- a schema language whose own example is a parser gotcha
is teaching the gotcha. `discriminator:` or `switch:` would not have this
problem. VERIFIED directly, not taken from the report.

**L-2. UNAPPLIED for the `stored:`-scoping half (no hit for whether `stored:` is scoped to the mapping form only). dialect.md:262 settles the OTHER half -- "an `enum`'s values are therefore not restricted to strings" -- so the tri-state example is no longer unremarked, only the scoping question is open. `stored:` defaults to `string`, yet `values: [true, false, TBD]` is
endorsed.** Under a literal reading `true` is not a string, so the spec's own
tri-state example contradicts its own default. The loader scoped `stored:` to
the MAPPING form only, leaving the list form as literal membership. The spec
must settle it.

## Silences the loader resolved on its own authority

Each of these is now encoded in code and nowhere in the spec. Left alone,
they become folklore.

| id | The silence | What the loader chose |
|---|---|---|
| L-3 | **UNAPPLIED** (dialect.md:512 names `unique` but no declared `scope:` keys) -- `unique` has no declared keys | `ids: <type>.<field>` plus an uninterpreted `scope:` -- so `scope` is NOT enforced |
| L-4 | **UNAPPLIED** (dialect.md:511 names `matches_files` with no comparison detail) -- what `matches_files` compares | `Path.stem`, both directions reported |
| L-5 | **APPLIED, and it is the correct row: do not edit it** (dialect.md:285-289, the three-forms sentence added for D-3/L-16) -- `sentinel:` has no stated form | accepts mapping, list or bare scalar; exempt from `min`/`max` only |
| L-6 | **UNAPPLIED** (dialect.md:276 says 'not authored' but never states the required-check is skipped) -- is a `derived` field required? | never demanded, never compared (a literal reading fails every record) |
| L-7 | **UNAPPLIED** (no hit for `required:` defaulting on a `shape_from` field) -- `required` for fields from `shape_from` | defaults true -- so a genuinely optional foreign field cannot be expressed |
| L-8 | **UNAPPLIED** (dialect.md:171-172 states the restriction -- 'deliberately limited to this: a map value... not a general macro' -- but not the enforced consequence, a load-time error; see the L-8/L-9 standard in the header) -- `shape_from` outside a map value | a load-time error |
| L-9 | **UNAPPLIED** (dialect.md:357 still only says 'do not', no stated consequence) -- restating `via`/`abstract_flag` under `fields:` | an error (the spec says "do not", with no consequence) |
| L-10 | **UNAPPLIED** (dialect.md:737 states the document key IS the identity but never addresses a disagreeing body id) -- `keyed_map` identity vs a body id | document key wins; NO cross-check (unlike `file_per_record`, where the spec is explicit) |
| L-11 | **UNAPPLIED** (no hit for duplicate-identity / ambiguous-ref clarification) -- duplicate identities | not an implicit error, since `unique` exists -- which makes `ref` resolution silently ambiguous |
| L-12 | **UNAPPLIED** (dialect.md:684 `covers:` section never scopes itself to `field:` entries over `computed:`) -- `covers` and `computed:` entries | compares `field:` entries only |
| L-13 | **UNAPPLIED** (no hit for a shared-`of:` requirement between covering views) -- must both `covers` views share `of:`? | not checked |
| L-14 | **UNAPPLIED** (dialect.md:727 documents `file_per_record`'s glob; other layouts' non-globbing is never stated) -- which layouts glob | `file_per_record` only |
| L-15 | **UNAPPLIED** (no hit for float/int or bool/int coercion rules) -- `float` accepting an `int` | yes; `bool` is never an `int` |
| L-16 | **RESOLVED** -- an explicit `null` | treated as ABSENT, except on a field whose `sentinel:` set contains `null`, where it is PRESENT and carries that sentinel's meaning; every other field keeps the old ABSENT behaviour unchanged (resolved together with D-3) |
| L-17 | **UNAPPLIED** (no hit for a dangling-`extensible`-parent clarification) -- dangling `extensible` parent | reported once, by the generic `ref` check |
| L-18 | **APPLIED** (dialect.md:788, 'The editor refuses to write there') -- `generated_by:` enforcement | parsed and carried, not enforced (the spec assigns it to the editor) |
| L-19 | **APPLIED** (dialect.md:455, 582, the advisory channel is documented) -- the advisory channel | `severity="advisory"`, with `errors_only()` as the fatal subset |

**L-16 and D-3 were in direct conflict** and have been resolved together:
the loader treated an explicit `null` as absent, while the corpus uses an
explicit `null` as a meaningful assertion on 11 of 30 records.

**Resolved.** `sentinel:` may now declare `null` as a member. The type-check
exemption that lets a null-sentinel field hold `null` is pre-existing and
applies to FIELD SLOTS -- a field of a mapping, an ORDINARY map entry value,
a `partial_of` layer value, or the `open:` ad hoc branch -- not to a `null`
written as a list item or a map key, where the ordinary type check still
fires, and not to a `shape_from:`-shaped map entry value either, which is
dispatched to shape resolution ahead of the null guard and so fails as a
wrong-type record rather than being read as absent; this exemption is
unrelated to `sentinel:`. `sentinel:` itself still grants exemption from
`min`/`max` only, exactly as L-5 above states, and that is unchanged. What `sentinel:` now also controls is the required-field
check: when a field's sentinel set contains `null`, an explicit `null` in
the corpus is PRESENT -- it satisfies `required` and carries the declared
sentinel meaning. When a field's sentinel set does not contain `null`, an
explicit `null` stays ABSENT exactly as before -- nothing else in any corpus
changes behaviour. Marking the field `required: false` was considered and
refused: that is pure silencing, and the corpus needs to distinguish "the
value is missing" from "the value is asserted to be null with this
meaning." See `docs/dialect.md`, "Field annotations".
