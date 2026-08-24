# Dialect gaps found by writing the first profile

Source: the `pd-profile` unit, 2026-08-21. 45 profile documents, 1244 lines,
written against the real corpus. Every finding below is evidenced by a corpus
file the profile could not express.

**Status: NOT YET APPLIED to `docs/dialect.md`.** The loader was being
implemented against the current spec when these arrived; revising the spec
mid-implementation is how a loader ends up built against a superseded
contract. Apply after the loader lands, then re-check the loader against the
revision.

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
2. **Anything that must reach ACROSS a type.** A key set that is the union of
   two sets; a foreign field that is really a reference; a view column drawn
   from a second type; a reverse index. The dialect is strong within one type
   and weak between types.

## Findings, by severity

| id | What breaks | Corpus evidence | Minimal fix |
|---|---|---|---|
| D-9 | `keyed_map` requires `of:` to name a TYPE, but the values are a map of numbers or a list of refs | `prices.yaml`, `stat-pools.yaml` (both inexpressible), `weapon-stats.yaml` (half) | let `keyed_map` take `value:` as an alternative to `of:`; let `of:` name the type whose fields are the metadata keys |
| D-12 | `shape_from` cannot enrich an INDIVIDUAL foreign field, so a declared `string` that is really a ref, and an `i32` that is really an enum, stay dumb | all 35 `entities/*.yaml`, `app.yaml` `spawn_list` | `shape_overrides:` on the `shape_from` declaration |
| D-5 | no way to say a map is TOTAL over its key enum | `manifest.yaml` `counts`; `prices.yaml` states a missing pair is a BUILD FAILURE | `total: true` on a map field |
| D-1 | `values_from:` takes ONE path, but a real key set is a UNION of two | `gear.weapon_stats` keys = `stat.id` + `weapon_stat_table.derived_fields` | `values_from:` accepts a list of paths |
| D-16 | `view` `field:` has no PATH form, so it cannot address a map key -- which also breaks `covers:` (card names the map, table names three paths) | `templates/items.yaml` `headers_weapon_extra` | a path form for `field:`, plus how a path relates to its parent under `covers:` |
| D-2 | a record set partitioned across files by a filename segment; `rows` takes one literal path | `names-{armor,helm,accessory,charm}.yaml` | `path:` with a `{segment}` placeholder + `partition_by:` |
| D-3 | a MEANINGFUL explicit `null` (present-and-null is an assertion; absent is silence) | `stats.yaml` `sashimi_field: null` on 11 of 30 | permit `null` as a sentinel key, or `nullable: true` |
| D-7 | two sources claiming different regions of ONE file; `single`'s wording reads as a conflict | `manifest.yaml`, claimed by `gear_manifest` (single) and `weapon_family` (rows) | one sentence: `single` covers the keys no other source claims |
| D-6 | `rows` with no identity column; `identified_by:` is required | `open-decisions.yaml` is a `{what, where}` table | permit omission for `rows`, row index as the anchor address |
| D-8 | no home for an open question ABOUT a declaration (`meaning:` says what a value is for, nothing says what is unresolved) | `points.yaml`, eight owner TBD directives as trailing comments | an `open_question:` field annotation |
| D-10 | no `disjoint` constraint kind; `covers` asserts inclusion only | `weapon-stats.yaml` `fixed_stat_meanings` | a fourth constraint kind |
| D-14 | no foreign-scope `ref`; the value degrades to bare string | `app.yaml` `imports.*.components` | `scope:` / `external: true` on `ref` |
| D-15 | `values_from:` cannot SUBTRACT, so four values are inlined two keys below the list they duplicate | `manifest.name_template` = slots MINUS one | an `except:` on `values_from` |
| D-0 | a view of ONE type cannot express a JOIN -- confirmed structural, three instances over three type pairs | `templates/stats.yaml` (11 columns from 3 sources incl. a reverse index over 112 records), `tag_table`, `mechanic_card` | none proposed; the fix is a query construct |

Also noted, minor: `constraints.ids:` is documented as `<type>.<field>` but real
paths go two deep, and nested paths are used in `values_from:` too. The spec
is silent on whether they are legal. Say.

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


# Spec defects found by implementing the loader

Source: the `dialect-loader` unit, 2026-08-21. 19 findings. The unit's
premise -- "the spec is complete enough to implement without a design
decision of my own" -- was REFUTED. It did not halt, because none of the
findings blocked a defensible implementation, but every resolution below is
the LOADER's choice and not the spec's, which means the spec and the code can
now disagree without either being wrong.

## Two genuine contradictions, not silences

**L-1. `variants: { on: ... }` is unloadable as written.** `on` is a YAML 1.1
boolean, so PyYAML resolves a bare `on:` key to `True`. The spec's own
example therefore does not parse into the shape the spec describes. The
loader accepts both spellings; the SPEC must either say so or rename the key.
Renaming is cleaner -- a schema language whose own example is a parser gotcha
is teaching the gotcha. `discriminator:` or `switch:` would not have this
problem. VERIFIED directly, not taken from the report.

**L-2. `stored:` defaults to `string`, yet `values: [true, false, TBD]` is
endorsed.** Under a literal reading `true` is not a string, so the spec's own
tri-state example contradicts its own default. The loader scoped `stored:` to
the MAPPING form only, leaving the list form as literal membership. The spec
must settle it.

## Silences the loader resolved on its own authority

Each of these is now encoded in code and nowhere in the spec. Left alone,
they become folklore.

| id | The silence | What the loader chose |
|---|---|---|
| L-3 | `unique` has no declared keys | `ids: <type>.<field>` plus an uninterpreted `scope:` -- so `scope` is NOT enforced |
| L-4 | what `matches_files` compares | `Path.stem`, both directions reported |
| L-5 | `sentinel:` has no stated form | accepts mapping, list or bare scalar; exempt from `min`/`max` only |
| L-6 | is a `derived` field required? | never demanded, never compared (a literal reading fails every record) |
| L-7 | `required` for fields from `shape_from` | defaults true -- so a genuinely optional foreign field cannot be expressed |
| L-8 | `shape_from` outside a map value | a load-time error |
| L-9 | restating `via`/`abstract_flag` under `fields:` | an error (the spec says "do not", with no consequence) |
| L-10 | `keyed_map` identity vs a body id | document key wins; NO cross-check (unlike `file_per_record`, where the spec is explicit) |
| L-11 | duplicate identities | not an implicit error, since `unique` exists -- which makes `ref` resolution silently ambiguous |
| L-12 | `covers` and `computed:` entries | compares `field:` entries only |
| L-13 | must both `covers` views share `of:`? | not checked |
| L-14 | which layouts glob | `file_per_record` only |
| L-15 | `float` accepting an `int` | yes; `bool` is never an `int` |
| L-16 | an explicit `null` | treated as ABSENT -- note this COLLIDES with D-3, where a meaningful null is real data |
| L-17 | dangling `extensible` parent | reported once, by the generic `ref` check |
| L-18 | `generated_by:` enforcement | parsed and carried, not enforced (the spec assigns it to the editor) |
| L-19 | the advisory channel | `severity="advisory"`, with `errors_only()` as the fatal subset |

**L-16 and D-3 are in direct conflict** and should be resolved together: the
loader treats an explicit `null` as absent, while the corpus uses an explicit
`null` as a meaningful assertion on 11 of 30 records. Whichever way it goes,
one of the two has to change.
