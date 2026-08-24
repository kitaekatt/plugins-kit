# The dialect

The schema language this plugin defines. A consuming project writes a
**profile** in it -- a set of YAML documents declaring that project's own
types -- and the plugin loads, validates and renders against the profile
without learning a single one of the project's nouns.

This document is the specification. It is written for someone authoring a
profile.

## Why the dialect is not JSON Schema

The dialect exists because of three requirements JSON Schema does not serve,
not because YAML is nicer to type.

1. **Every declaration must be authorable and reviewable by a human editing
   YAML, and consumable by an LLM without a translation step.** A profile is
   corpus, on the same footing as the data it describes.
2. **A schema must carry MEANING, not only shape.** A number's unit, a
   field's provenance, why a value is a sentinel -- these are the things real
   corpora record in comments, where nothing can read them.
3. **A corpus written before its schema must be expressible.** The dialect was
   designed against a real body of YAML that predates it and does not bend to
   it, so every construct below exists because the corpus already contains the
   thing it describes. Nothing here is speculative.

## The three document kinds

A profile is made of three document kinds, and the separation is the whole
design. Each fact has exactly one home.

| Kind | Declares | Answers |
|---|---|---|
| `type` | records, fields, constraints | what may be written |
| `view` | labels, order, grouping, formatting | how it is shown |
| `source` | where records of a type live on disk | where it is written |

Splitting `view` from `type` is not tidiness. A corpus that couples them
declares field order in as many places as it has render sites, kept in sync by
hand -- observed three times over for one field set: a template's header
array, that table's cell-emitting sequence, and a detail card's sequence.

Be precise about what the split fixes, because the obvious overstatement is
wrong. A `view` collapses the header/cell pair into ONE declaration, which is
the duplication that was genuinely accidental. It does NOT collapse the table
and the card into one: the card deliberately orders fields differently, and a
deliberate second ordering is a second view, correctly. `covers:` below
constrains the card's field SET against the summary's, never its order.

The cost of the split, stated plainly: a view names fields, so a field rename
now touches a second document. That is the right trade -- a dangling view
reference fails loudly and a rename is already a cross-file migration, while
positional drift between a header array and a render sequence fails silently
and did.

## Document kind: `type`

```yaml
dialect: type/1
id: product
title: Product
identified_by: id
fields:
  id:      { type: id }
  name:    { type: string }
  category:    { type: enum, values_from: category.id }
  tier:  { type: enum, values_from: tier.id }
  labels:    { type: list, of: { type: ref, to: label } }
  price: { type: int, unit: cents }
```

### `value` -- records that are values

A type normally uses `fields:` to declare each record body. A type with
`value:` instead declares that each record IS one value with that field
declaration:

```yaml
dialect: type/1
id: rate_table
fields:
  note: { type: text }
value:
  type: map
  key: { type: enum, values_from: catalog.category_order }
  value: { type: int, unit: cents }
  total: true
```

`value:` takes a complete field declaration. All field types and annotations
in this document can occur inside it.

For a type with `value:`, the roles of the two declarations are exact:

- `value:` declares each record body.
- `fields:` declares the metadata keys of the containing document.

The validator checks each identified record body against `value:`. It checks
the document metadata once against `fields:`, with the same rules as a
`single` source. An unknown metadata key is an error. An absent required
metadata field is an error. A record body of the wrong YAML kind produces the
ordinary wrong-type error, which names the record and the declared kind.

A value-shaped type is legal only with a `keyed_map` source. A profile load
fails for each of these combinations:

- `value:` with `identified_by:`, `variants:`, `extensible:`, or `open:` on
  the same type. The error names the type, `value:`, and the conflicting key.
- A source of the type with a layout other than `keyed_map`. The error names
  the source, its layout, and the type.
- A source of the type with `metadata_keys:`. The error names the source and
  the type. The type's field names already declare the metadata set.

Source and type documents can load in either order. Therefore, the loader
checks source/type combinations during deferred profile resolution.

The document key remains the identity of a value-shaped record. A selector
steps into the declared value after that identity. For example,
`rate_table/basic/standard` selects the `standard` key from the `basic`
record's map value.

The metadata record uses the document identity `@doc`. For example,
`rate_table/@doc/note` selects the `note` metadata field. A metadata field
under a record identity is an unknown name. A selector cannot step into a
list value. The selector for that record denotes the complete list.

### Field types

Scalars: `string`, `int`, `float`, `bool`, `id`, `text`.

`id` and `text` are `string` with a declared job -- `id` is the identity a
`ref` resolves to, `text` is prose meant to be read by a person or an LLM.
They are separate types because the editor and the validator treat them
differently, not because their storage differs.

Compound: `list` (`of:`), `map` (`key:` and `value:`), `ref`, `enum`,
`record` (an inline nested shape).

### `map` keys, and key-dependent values

A map's `key:` must be an `id`, a `ref`, or an `enum`. A `ref` or
`enum` supplies a declared legal set. A bare `id` declares the key shape
without claiming that its member set is known. A raw scalar key is refused,
because it does not even declare that key shape.

Integer keys are therefore permitted exactly when they are declared:

```yaml
negative_fraction:
  type: map
  key:   { type: enum, stored: int, values: { 2: two_stats, 3: three_stats } }
  value: { type: float }
```

The keys stay integers on disk. What changes is that their meaning has a home.

A map whose VALUE shape depends on its KEY uses `shape_from:`:

```yaml
components:
  type: map
  key:   { type: ref, to: component_def }
  value: { shape_from: component_def.fields }
```

This reads: the value's shape is the `fields` map of the `component_def`
record this key names. It is the construct that lets a corpus keep its own
schemas AS DATA -- a set of records declaring field names and types -- and
have the dialect validate against them rather than against a second copy.

`shape_from:` is the one place the dialect lifts data into shape, and it is
deliberately limited to this: a map value, taking the shape from the record
its own key resolves to. It is not a general macro.

**It also needs an adapter, and this is the construct's real cost.** A corpus
that already stores its schemas as data wrote them in ITS OWN type language,
not this one -- observed values include `f32`, `i32`, and a `count: 20`
meaning a fixed-length array, against the dialect's `float`, `int` and
`list`. Without a mapping, `shape_from` only works on a corpus already
written in the dialect, which is the one corpus that would never need it.

So a type declaring `shape_from:` must also declare how to read the foreign
declarations:

```yaml
adapter:
  type_key: type
  types: { f32: float, i32: int, i64: int, bool: bool, string: string }
  cardinality_key: count      # present => a fixed-length list of that type
```

The adapter lives on the type that USES `shape_from`, because it describes
how to read one particular foreign schema -- a fact about that corpus, not
about the dialect.

### `ref` -- typed cross-references

```yaml
billing_period: { type: ref, to: billing_period }
```

Every cross-reference in the corpus this was designed against is a bare
unprefixed string, indistinguishable at the value level from a free-text
field. `ref` is the declaration that makes it distinguishable, and it buys
three things at once: the loader validates the target exists, the editor
offers the legal set instead of a text box, and a rename tool has an
enumerable list of sites to rewrite.

### `enum` -- and where legal values live

```yaml
kind:   { type: enum, values: [record, marker] }
tier: { type: enum, values_from: tier.id }
```

`values:` declares the set inline. `values_from:` declares that another
type's ids ARE the set -- use it whenever a table of those values already
exists, because inlining a copy creates the second source of truth the
dialect exists to remove.

`values_from:` also accepts a path to a LIST-OF-SCALARS field, not only an id
set:

```yaml
belongs: { type: enum, values_from: permission_matrix.categories }
```

Without this, a legal set that a corpus stores as an ordered list of bare
strings -- the same list whose ORDER is load-bearing, so it cannot be
converted into a record table without losing that -- would have no reachable
home.

A third form exists for a value whose STORED form is not its label:

```yaml
shape: { type: enum, stored: int, values: { 0: none, 1: circle, 2: rect } }
```

The value on disk stays the integer; the label is declared rather than
written in a trailing comment. `stored:` may be `int` or `string` and
defaults to `string`.

This form exists because the alternative was to strand data. A corpus can
contain integer-coded enums it does not own -- exported from another tool and
parsed by an engine that expects the integer -- so "write the labels into the
data instead" is not an instruction its authors can carry out. The dialect's
job is to give the legal set a home, and a set that cannot move still needs
one.

What remains unexpressible is a legal-value set with NO declaration at all --
one that lives only in prose or in generator source. That is the refusal, and
it is narrower than it first looks: `values:` accepts the set in whatever form
the data already stores it.

A field that looks boolean but admits a third value (`true`, `false`, or the
literal `TBD`) is an `enum`, not a `bool` with an exception. Say so:

```yaml
hidden: { type: enum, values: [true, false, TBD] }
```

An `enum`'s values are therefore not restricted to strings. That is
deliberate and it settles what a map key may be: see `map` below.

### Field annotations

Any field may carry these. They are not decoration -- each one has a
consumer.

| Key | Means | Read by |
|---|---|---|
| `required` | must be present (default `true`) | validator |
| `unit` | what the number counts (`ticks`, `seconds`, `cents`) | validator, editor, LLM |
| `meaning` | one line on what the value is FOR | editor, LLM |
| `sentinel` | a value with a meaning outside the range (`-1: no limit`) | validator, LLM |
| `derived` | computed, not authored -- how | validator (refuses edits) |
| `provenance` | where the field came from (a port, an upstream field) | human |

`unit`, `sentinel` and `meaning` are the comment-only knowledge the corpus
already carries. Giving them declared homes is most of the dialect's value:
a `-1` meaning "no limit" and a `delay_ticks` that is not
seconds are exactly the facts an agent gets wrong when they live in a
comment.

### `variants` -- discriminated unions

A record whose shape depends on one of its own field values:

```yaml
id: product
identified_by: id
fields:
  category: { type: enum, values_from: category.id }
  # ... fields common to every category
variants:
  on: category
  when:
    subscription:
      billing_period: { type: ref, to: billing_period }
      plan_limits:  { type: map, key: { type: ref, to: measure }, value: { type: float } }
```

`on:` names the discriminator, which must be a field of the record and must
be an `enum`. `when:` maps a discriminator value to the fields that value
adds. A value absent from `when:` adds nothing.

This is required, not a convenience: a corpus in which a product carries
billing fields only when `category: subscription`, and a definition carries
`fields` only when `kind: record`, cannot be validated by a flat record shape without either
marking real fields optional (so a missing one stops being an error) or
rejecting valid data.

### `extensible` -- inheritance with sparse override

```yaml
id: template
identified_by: name
extensible:
  via: extends
  abstract_flag: abstract
```

Declares that a RECORD of this type may name another record of the same type
in its `extends` field and inherit its values, overriding only what it
restates. `abstract_flag` names the field marking a record as a base that is
never instantiated.

`extensible:` DECLARES both fields it names. Do not also list them under
`fields:` -- the `via:` field is a `ref` to this same type and the
`abstract_flag:` field is an optional `bool`, and restating them is the
duplication the dialect refuses elsewhere.

**Required fields are checked AFTER flattening.** A record that inherits a
required field from its parent satisfies it. Checking before the merge would
fail every record that legitimately overrides only what it changes.

**`abstract_flag` does NOT exempt a record from validation**, and a profile
author must not assume it means "never instantiated". That reading is
tempting and it is wrong in practice: a corpus was found in which records
marked abstract are also named directly as spawn targets, so the flag meant
something closer to "generated from a template source" than "not a leaf". An
exemption keyed on it would have silently disabled completeness checking on
live records.

What the flag does is narrower and safer: it is declared so a `view` can hide
those records and an editor can group them. If a type genuinely has
never-instantiated bases that are incomplete ON PURPOSE, express that with
`required: false` on the fields they omit -- a claim about the fields, which
is checkable, rather than a claim about the record, which is not.

### The merge algebra

`extensible:` is useless without stating how parent and child combine, and
the answer is not "the child wins", because the corpus's inherited records
override single entries of a map their parent also populates.

- **Scalars:** the child's value replaces the parent's.
- **Maps:** MERGED BY KEY, recursively. A child that sets one key of a map
  keeps every other key the parent set. This is the case that makes the
  construct work at all.
- **Lists:** the child's list replaces the parent's entirely. A list is a
  value, not a namespace; merging by position is not meaningful and merging
  by identity would require lists to have one.
- **Records (inline nested shapes):** merged field by field, like maps.
- **Deletion:** not expressible. A child cannot remove a key the parent set.
  This is a deliberate limit, not an oversight -- add the construct when a
  corpus needs it, and not before.
- **Chains:** resolved parent-first, deepest ancestor applied first. A cycle
  is a validation error naming every record in it.

Note what this is not: the inheritance is in the DATA, between records. The
dialect declares that the type permits it. Type-level inheritance -- one type
extending another -- is deliberately absent, because nothing in the corpus
needs it and a schema language with two inheritance mechanisms is a schema
language nobody can predict.

### `partial_of` -- named override layers

```yaml
configs:
  type: map
  key:   { type: id }
  value: { partial_of: app }
```

A `partial_of` value is a sparse subset of the named type's fields; each
field present is validated by its declaration on that type, and absent
fields are not merely optional but MEANINGFUL -- they are the ones this layer
does not override.

This is not `extensible:`. That construct is record-to-record inheritance
within one type, via a field naming another record. `partial_of` is a map of
named partial layers embedded in the very record they override, whose members
are not records of anything.

A layer may also carry keys that are not fields of the target type at all --
a value belonging to some ANOTHER record, which a corpus's assembly step
routes onward. Declare each one and where it goes:

```yaml
  value:
    partial_of: app
    routes: { damage_taken_ratio: combat_config.damage_taken_ratio }
```

There is deliberately no blanket "allow anything else" flag. One was drafted
and removed: it would admit a typo and a routed key indistinguishably, which
is precisely the fail-closed posture a corpus adopts a schema to get. If a
routed key is worth having, it is worth naming.

### `open` -- admitting ad hoc fields

```yaml
open:
  prefix: flag_
  type: { type: text }
```

Permits fields the schema does not name, constrained to a prefix. This exists
for a real and slightly uncomfortable case: rows carrying per-record
annotation fields (`flag_pending_review`, `flag_tbd`) that appear on some
records and not others.

It is the dialect's only escape hatch, and it is deliberately narrow -- a
prefix and a type, never a blanket "additional properties allowed". An open
field is reported by the validator as an advisory, because a growing set of
them is the signal that a real field is waiting to be declared.

### Size and cardinality

Any field may carry these. They are separate from `constraints:` below, which
relate one type to another; these bound a single value.

```yaml
xp:          { type: list, of: { type: int }, length: 20 }
short:       { type: text, max_chars: 120 }
word_list:   { type: list, of: { type: ref, to: word }, min_length: 12 }
rates:
  type: map
  key: { type: enum, values_from: catalog.category_order }
  value: { type: int }
  total: true
```

`length` (exact), `min_length` / `max_length` (list or map), `max_chars` /
`min_chars` (string and text), `min` / `max` (numbers).

`total: true` applies to a map whose key has a declared set. The key must be
an inline enum, an enum with `values_from:`, or a `ref`. Every member of that
set must occur in the map. A missing member is an error that names the absent
key, the declared set, the map, and the record.

`total: true` on a map with a bare `id` key is a profile load error. A bare
`id` has no declared set to be total over. Extra map keys remain errors under
the map key's existing value check.

These exist because a real corpus enforces all of them today -- a fixed
20-element per-level array, a prose field with a stated character ceiling, a
vocabulary list that must hold at least enough entries to draw from -- and
enforces them in prose, in generator source, or not at all.

### `constraints` -- obligations between types

```yaml
constraints:
  - kind: covers
    from: measure.id
    to: measure_cost.id
    both_ways: true
    why: "every measure must be priced and every price must name a real measure"
  - kind: matches_files
    ids: template.name
    files: "templates/*.yaml"
    why: "the manifest list and the directory must not drift"
```

Three kinds, each drawn from an obligation the corpus already enforces by
hand or by a build failure:

- `covers` -- every id in one set has a counterpart in another. `both_ways`
  makes it mutual.
- `matches_files` -- a declared id set equals a glob's filenames.
- `unique` -- an id set has no duplicates within a scope.

`ids:`, `from:` and `to:` take anchored paths and may be nested, because the id
set a constraint governs is routinely a list under a nested record --
`ids: app.templates.entities` names the load-order list inside a `templates`
record. The path must END at a set: an `id` field, a list of scalars, or a list
of refs. A path ending at a single scalar is an error, because a constraint over
one value is not a constraint.

`why:` is required on every constraint. A constraint whose reason nobody
wrote down is one nobody can safely remove later.

### `ordered` -- when position is meaning

```yaml
categories:
  type: list
  of: { type: string }
  ordered:
    significance: "index is the bit position in the permission mask"
```

A plain list is a bag the editor may reorder freely. `ordered:` declares that
position carries meaning, which makes reordering a semantic edit -- the
editor warns, and the validator records the order in the anchor hash so a
reorder registers as a change. `significance:` is required, because "this
list is ordered" without saying what the order MEANS tells a later reader
nothing.

## Paths -- how a declaration addresses a value

Several constructs name a value rather than declaring one: `values_from:`,
`shape_from:`, `record_keys_from:`, a constraint's `ids:` / `from:` / `to:`, a
`routes:` target, and a `view` entry's `field:`. They all take a PATH, and
there are exactly two forms.

- An **anchored path** starts at a type: `<type>.<segment>[.<segment>]*` --
  `gear_manifest.slot_order`, `component_def.fields`, `app.templates.entities`.
  The first segment is a type id, which is an anchor rather than a traversal;
  every segment after it walks that type's declarations.
- A **field path** starts at a record whose type the document already named:
  `<segment>[.<segment>]*` -- `weapon_stats.damage` in a view `of: gear`. A
  field path is an anchored path whose anchor `of:` already supplied.

The separator is `.` in both. A field path also appears inside addresses that
spell the type/record part with `/` -- a comment anchor, a lock selector -- and
a language that spells one descent two ways is a language something has to
translate between.

### Walking a path

Each segment after the anchor resolves against the declaration the previous
segment produced. There are exactly three legal steps.

- **Into a `record`** (an inline nested shape): the segment names one of its
  `fields:`. `app.templates.entities` is this step -- a constraint's id set
  living under a nested record.
- **Into a `map`**: the segment is a KEY, and the step produces the map's
  `value:` declaration. `weapon_stats.damage` is this step -- one named key of
  a map field, which is how every weapon row in a real corpus renders.
- **Into a variant-added field**: a first segment may name a field `variants:`
  adds, not only one `fields:` declares. Where the path is written decides
  whether that is in scope: a view entry naming one needs the matching `when:`,
  and is an error without it.

A key step is checked against the map's `key:` declaration. When the key is an
`enum` or a `ref`, the segment must be a member of the declared set, and one
that is not is an error naming the segment, the map, and the set it failed
against. When the key is a bare `id` the set is undeclared, so there is no
membership check to run: the step still resolves the value's shape, and the
validator reports the unchecked key as an advisory -- the same channel an
`open:` field uses, for the same reason. The advisory is the signal to declare
that key set, not a tier of failure.

Depth is unbounded, deliberately. The three steps already bound a path to the
nesting a profile itself declared, which is finite and authored; a numeric
ceiling on top of that is a second rule that can only ever fail an author whose
data is legitimately deep. The deepest path the motivating corpus writes today
is three segments.

An unresolvable segment is a load error naming THAT segment and the declaration
it failed against -- never an empty result, never a skipped check, and never
just the whole path. `app.templates.recipes` reports `recipes` against the
`templates` record, because the useful fact is where the walk stopped.

### Where a path may not go

A path descends ONE type's declarations. Three steps are refused, and they are
the same refusal three times: a path continuing past any of them has reached
into another type, which is a join.

- **Through a `ref`.** `gear.weapon_family.damage` is refused. A `ref` is where
  one type ends and the next begins. This is open question 3 and it stays open:
  a path form that quietly crossed a `ref` would BE the query construct the
  dialect declines to grow by accident.
- **Through a `list`.** A list has no names, so a segment could only be an index
  or a filter -- position addressing, which breaks on exactly the reorder
  `ordered:` exists to make visible, or a predicate, which is a query again. A
  path may END at a list; `values_from:` on a list-of-scalars field is precisely
  that. It may not continue through one.
- **Through a `shape_from:` value.** That shape is read from data at validation
  time, so no segment past it can be resolved when the profile loads. Refusing
  it is what keeps every path checkable at load rather than per record.

### Paths and the constructs they cross

- **`extensible:`** -- a path resolves against the type's DECLARATIONS, which
  inheritance does not change, so one path holds for every record whether the
  value is inherited or restated. This is the same division as required fields
  being checked after flattening: the shape is the type's, the value is the
  flattened record's. The `via:` and `abstract_flag:` fields are addressable at
  depth one, like any other field.
- **`partial_of:`** -- a layer's keys are field names of the target type, not
  paths; a layer overrides a whole field or none of it. A `routes:` TARGET is an
  anchored path and may be nested, because it names a field of the record the
  value is routed onward to. A `routes:` KEY is a key of the layer, and stays
  one segment.
- **`identified_by:` and `variants: on:`** -- one segment each, and that is a
  refusal rather than an omission. An identity is a record's address, and an
  address living inside a map value has no stable form; a discriminator chooses
  the shape of the whole record, so it must be a field the record has before any
  variant applies.
- **`value:`** -- an anchored path still resolves against `fields:`, which are
  the metadata declarations for a value-shaped type. No anchored path enters
  `value:`. A view of the type can name metadata fields, but it cannot name a
  path inside the record value.
- **A source's `path:`, `files:` and `key:`** -- filesystem globs and a
  document's own top-level key. They are not dialect paths, and a `.` in one is
  a dot in a filename.

## Document kind: `view`

```yaml
dialect: view/1
id: product_table
of: product
form: table
fields:
  - { field: name,       label: Name }
  - { field: category,   label: Category }
  - { field: price, label: Price, format: "%.2f" }
  - { field: labels,     label: Labels, link: true }
```

`form:` is `table`, `card`, or `summary`. `fields:` is an ordered list, and
that order IS the render order -- the single declaration the three
hand-synchronized orderings collapse into.

Per-entry keys: `label` (omit to use the field name, which is how a corpus
avoids inventing a second name for a field), `format`, `link` (the value is a
drill-down to the referenced record), `group`, `when` (show only for a
discriminator value).

`field:` takes a field path (see Paths above), so an entry may address a key
inside a map field:

```yaml
  - { field: weapon_stats.damage, label: Damage, when: weapon }
```

This is not an exotic case. A table showing three named keys of a map field
rather than the map itself is how a real corpus's weapon rows render, and
without a path form that table cannot be declared at all.

A view may declare a display value with no backing field:

```yaml
  - { computed: used_by, label: "used by", from: "count(product where measure in measures)" }
```

`computed:` is presentation-only and never writable.

### `covers` -- the checkable superset rule

```yaml
dialect: view/1
id: product_card
of: product
form: card
covers: product_summary
```

`covers:` asserts that this view shows every field the named view shows. The
validator checks it. This exists because the rule "a detail card must show
everything its summary shows" was previously a prose convention, and a prose
convention was violated and only caught by a person noticing.

`covers:` compares field paths by PREFIX: this view shows what the named view
shows if, for each of that view's entries, this one names the same path or an
ANCESTOR of it. A card naming `weapon_stats` covers a table naming
`weapon_stats.damage`, because showing the map shows the key. The converse does
not hold -- naming three keys does not cover the map, which may hold others.

The rule is required, not a convenience. Without it the two views that motivate
`covers:` fail the check they exist to pass, and the check reduces to which of
two equally correct spellings each author happened to pick.

## Document kind: `source`

```yaml
dialect: source/1
of: product
layout: rows
path: content/products.yaml
key: items
generated_by: tools/generate_catalog.py
```

`layout:` is one of:

- `rows` -- a list of records in one file. `key:` names the containing key;
  OMIT it when the document IS the sequence, which is what a generated table
  typically looks like (the file opens with `- id: ...` under a banner
  comment). A `rows` type can omit `identified_by:`. Such rows have no
  identity.
- `file_per_record` -- one record per file matching `path:` as a glob. The
  record's `identified_by:` field remains the identity; the filename must
  AGREE with it, and a disagreement is a validation error naming both. The
  filename is a second expression of the identity, not a second source of it
  -- which is the only arrangement consistent with one fact having one home.
- `keyed_map` -- records as top-level keys of a document. A regular type uses
  `record_keys:` / `record_keys_from:` or `metadata_keys:` to separate records
  from the document's own metadata keys sitting at the same level. A
  value-shaped type uses `record_keys:` or `record_keys_from:` for the record
  keys and uses its `fields:` names for the metadata set. `metadata_keys:` is
  refused for that source. A `keyed_map` record's document key IS its identity.
  `identified_by:` is not required on a regular type and is refused on a
  value-shaped type.
- `single` -- the whole document IS one record of `of:`, with its top-level
  keys as that record's fields. No identity is needed and `identified_by:` is
  not required on the type.

An identity-less row is addressed as `(file, ordinal)`. The ordinal is its
zero-based position in the source file. Every persisted address MUST carry a
content guard. A content-guard mismatch is an error, and the address never
resolves to a different row.

Identity-less rows are allowed only when no construct addresses them by
identity. The profile loader refuses an identity-less type in these cases:

- Any `ref` names the type in `to:`.
- The type declares `extensible:`.
- A `values_from:` or `record_keys_from:` path names the type's id set.

Each refusal names the identity-less type and the declaration that requires
the identity.

When `record_keys:` or `record_keys_from:` supplies a record-key set,
`metadata_keys:` must enumerate every other top-level key for a regular type.
A key in neither set is a load error. The error names the unknown key, the
source document, the declared record-key set, and the declared metadata-key
set. The loader never treats an undeclared key as metadata.

For a value-shaped type, every other top-level key must name a field of that
type. An unknown key is a load error. The error names the unknown key, the
source document, the declared record-key set, and the type's field names.
Thus, the field-name check replaces every case that `metadata_keys:` guarded.
The loader still never treats an undeclared key as metadata.

A field name must not collide with a member of the record-key set. Such a
collision is a load error that names the field, the record-key set, the
source, and the type. One top-level key cannot be both a record and metadata.

`single` is not a degenerate case to be tidied away later. A corpus's most
load-bearing files are routinely one-of-a-kind documents -- a manifest, a
tuning table, a script, an application config -- with heterogeneous top-level
keys and no identity, and none of the other layouts describes them.

`record_keys_from:` declares that a `keyed_map`'s record keys are the id set
of another type, so the two cannot drift as records are added.

`keyed_map` is the awkward one and it is here because the corpus contains it:
a document whose per-group blocks are bare top-level keys interleaved
with metadata keys. Without a declared split, a loader cannot tell a record from a
note.

`generated_by:` marks the source as tool output. The editor refuses to write
there and says which tool owns it. This is the mechanical form of a rule the
corpus states in prose and then breaks -- a generated table living in a
hand-authored directory. The dialect makes the claim checkable rather than
restating the rule.

## What the dialect deliberately cannot express

Each of these is a refusal, not a gap. Each was present in the corpus and is
excluded so that adopting the dialect fixes it rather than preserving it.

- **A legal-value set with no declaration anywhere.** A set living only in
  prose or in generator source is not expressible. Note the narrowness: the
  set does not have to move, only to be declared. An integer-coded enum stays
  integer-coded via `stored: int`.
- **An omitted field meaning "use the default".** The dialect has no
  `default:`. A field is required or optional; an optional field that is
  absent is absent. A value that should be there must be written.
- **Unconstrained scalar map keys.** `key:` must be an `id`, a `ref`, or
  an `enum`, including an integer-valued enum. A bare `id` declares the key
  shape but not its member set. Therefore, it cannot carry `total: true`.
- **A path that crosses a `ref`, a list, or a `shape_from` value.** A path
  descends one type's declarations and stops where that type does. Reaching
  further is a join, which is open question 3, not a path form.
- **Two documents describing the same field.** `view` may not restate a
  field's type, and `type` may not carry a label or an order.

These refusals share a test, and it is worth stating because the first draft
of this document got it wrong: **a refusal may demand a DECLARATION, never a
change to data the project does not own.** A corpus routinely contains
generated files exported from another tool. Telling its authors to rewrite
those is telling them to stop generating them, which is not a schema
decision the dialect gets to make.

## Open questions for the profile author

These are the places the dialect does not yet decide, listed so a profile
author hits them deliberately rather than by surprise.

1. **Derived fields that are stored.** `derived:` marks a field computed and
   not authored. A corpus that STORES the computed value anyway (carrying it
   for a consumer that cannot compute it) needs the validator to either
   recompute and compare, or trust and ignore. The dialect currently says
   nothing; the profile must.
2. **Expression syntax.** `derived:` and `computed:` take a string today.
   Nothing parses it. Giving it a grammar is a real design decision and it is
   not made here -- treat the string as documentation until it is.
3. **A view of ONE type cannot express a join, and real views are joins.**
   An observed table renders rows drawn from three places at once: a record
   table, a matching cost table keyed by the same ids, and a reverse index
   built by scanning a fourth type for references back. `view.of` is unary,
   and `computed:` is an unparsed string, so today the dialect can describe
   that table's COLUMNS but not where its values come from. This is the
   largest known gap. It is listed rather than solved because the fix is a
   query construct, and a schema language that grows a query language by
   accident is how a small format stops being one.
4. **Migration of a corpus that does not match its own stated layout.** The
   dialect describes what IS on disk. When a project's documented target
   layout differs from its actual one, the profile must describe the actual
   one, and the difference is a migration to plan rather than a schema to
   write.
