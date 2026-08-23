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

### Field types

Scalars: `string`, `int`, `float`, `bool`, `id`, `text`.

`id` and `text` are `string` with a declared job -- `id` is the identity a
`ref` resolves to, `text` is prose meant to be read by a person or an LLM.
They are separate types because the editor and the validator treat them
differently, not because their storage differs.

Compound: `list` (`of:`), `map` (`key:` and `value:`), `ref`, `enum`,
`record` (an inline nested shape).

### `map` keys, and key-dependent values

A map's `key:` must be an `id`, a `ref`, or an `enum` -- something with a
declared legal set. A raw scalar key is refused, because a map keyed by
undeclared values is a record whose fields nobody wrote down.

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
```

`length` (exact), `min_length` / `max_length` (list or map), `max_chars` /
`min_chars` (string and text), `min` / `max` (numbers).

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
  comment).
- `file_per_record` -- one record per file matching `path:` as a glob. The
  record's `identified_by:` field remains the identity; the filename must
  AGREE with it, and a disagreement is a validation error naming both. The
  filename is a second expression of the identity, not a second source of it
  -- which is the only arrangement consistent with one fact having one home.
- `keyed_map` -- records as top-level keys of a document, which requires
  `record_keys:` / `record_keys_from:` or `metadata_keys:` to separate records
  from the document's own metadata keys sitting at the same level.
- `single` -- the whole document IS one record of `of:`, with its top-level
  keys as that record's fields. No identity is needed and `identified_by:` is
  not required on the type.

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
- **Undeclared map keys.** `key:` must be an `id`, a `ref`, or an `enum` --
  including an integer-valued enum. What is refused is a key set nobody
  declared, not a key that happens to be a number.
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
