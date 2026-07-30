# Query-tool facade pattern

A document that catalogs entities the agent and user reference repeatedly -- a gazetteer or inventory -- benefits from a fast lookup tool wrapping the catalog. The query-tool facade pattern names the convention: a single CLI with multiple lookup modes, structured YAML output on every path, and did-you-mean records on misses.

Use when a document has a catalog that fits any of:

- An entity index referenced by name or ID frequently (a directory of people, items, characters, rooms, configurations, accounts).
- A flat list of records routinely searched by substring or partial spelling.
- A dataset where exact-match lookups would otherwise force the agent to grep the codebase or read raw files.

Do not use when the catalog is small enough that the agent loads it whole into context, or when lookups are rare enough that bespoke greps are cheaper than maintaining a dedicated tool.

## YAML output -- the discipline

Every query result is YAML structured data. Not raw text, not JSON, not markdown tables. The agent reads the YAML as data; the agent can render it to the user as-is, or extract fields for downstream use. Either way the contract is stable: every query mode emits YAML.

The shape of a hit:

```yaml
status: found
<entity-fields>: ...
```

The shape of a miss:

```yaml
status: not_found
query: <the queried value>
did_you_mean:
  - <suggestion-1>
  - <suggestion-2>
  - ...
```

The structure carries an assertion the prose could not: every result has a `status:` field. The agent can branch on `status:` without parsing free-form text.

## Tool surface convention

A single facade script with multiple lookup modes wraps the catalog. The script auto-indexes the underlying data on load (or maintains a companion index file) so lookups are fast.

```
catalog_lookup.py <mode> <args...>
```

Modes typically include:

- `<entity-type> <query>` -- exact match by ID or name (e.g. `catalog_lookup.py user alice42`)
- `<entity-type> <category-name>` -- exact match by category attribute
- `list [--substring <fragment>]` -- enumerate entries; `--substring` filters

The exact mode names are domain-specific. The convention is the shape: a single script with multiple modes, structured arguments, deterministic indexing.

## Did-you-mean on miss

When a lookup misses, the script returns a `did_you_mean:` list with 5-10 close suggestions ranked by edit distance, substring overlap, or some other proximity heuristic. The script does NOT silently resolve misspellings -- it surfaces the suggestions and lets the agent or user pick.

This rule pays off in two ways:

- The agent learns the canonical spelling without prompting, because the next attempt uses a value from the suggestions list.
- The user sees their typo as a typo rather than as a silent wrong-match.

A miss with did-you-mean:

```yaml
status: not_found
query: alic42
did_you_mean:
  - alice42
  - alex42
  - alicia42
```

When no close matches exist, return an empty list explicitly:

```yaml
status: not_found
query: zzz999
did_you_mean: []
```

An empty list is informative; absence of the key is ambiguous.

## Spelling-discovery discipline

The agent's discipline when interacting with the catalog:

1. If unsure of the canonical spelling of an entity name, run `list --substring <fragment>` first to enumerate matches.
2. Accept `did_you_mean:` suggestions from a missed lookup before attempting any grep, file scan, or codebase search for the spelling.
3. Do NOT hardcode spelling assumptions when invoking the catalog tool. The catalog is the source of truth; spelling-from-memory is a failure mode.

This discipline matters because the agent's training-data-style spelling guesses (which often disagree with project-specific conventions) silently corrupt downstream operations when they pass to a tool that does not validate. The query tool's strict-match-then-suggest contract makes the spelling failure visible.

## Worked example: user-directory query tool

A document declares a `user_lookup.py` query tool wrapping a user catalog.

Tool surface:

```
user_lookup.py user <id-or-name>           # exact match by user ID or display name
user_lookup.py team <team-id>              # exact match by team
user_lookup.py role <role-name>            # exact match by role
user_lookup.py list [--substring <frag>]   # enumerate; substring filter
```

Hit case:

```yaml
# user_lookup.py user alice42
status: found
user_id: alice42
display_name: Alice Example
team: platform
role: senior_engineer
active: true
```

Miss case with did-you-mean:

```yaml
# user_lookup.py user alic42
status: not_found
query: alic42
did_you_mean:
  - alice42
  - alic-three
```

Spelling-discovery flow: the agent needs to look up "the user named ali something" but cannot remember the canonical id. Rather than grep, the agent runs `list --substring ali` and accepts a result from the enumerated set.

## Worked example: config-entry query tool

A document that wraps a project's configuration files exposes a `config_lookup.py` query tool.

Tool surface:

```
config_lookup.py entry <key>                # lookup a config entry by key
config_lookup.py section <section-name>     # list all entries in a section
config_lookup.py list [--substring <frag>]  # enumerate keys
```

The tool auto-indexes the configuration tree at startup; lookups are O(1) on a hash. Misses surface did_you_mean for typo recovery.

## Audit hooks

A document that claims this pattern can be mechanically checked against:

- The query-tool script exists at the documented path and is executable from the project root.
- The script has a stable mode-and-args interface; mode names are documented in the source document.
- All output is YAML on both hit and miss paths.
- Misses produce a `did_you_mean:` list (or an explicit `did_you_mean: []` when no near matches exist).
- The source document declares the spelling-discovery discipline so the agent's behavior is documented.

The pattern composes naturally with sub-area config (when the catalog spans sub-areas with per-sub-area state vocabulary) and with the actions pattern (when a multi-step recipe captures fields from a query result for use in later steps).
