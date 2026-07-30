# Authoring a standards file

A standards file is a markdown document carrying one fenced `standards_set:`
YAML block. The prose is for a human browsing the standards; the block is the
machine-validated contract skills-kit's own audits consume. One file governs
exactly one file-type primitive -- the optional, tunable opinions skills-kit
applies when auditing that kind of file.

Standards files are the authored surface of skills-kit's configurable
standards: architectural opinions stay hard-coded and are never expressed here;
mechanical integrity checks carry no knob. What lives in a standards file is the
optional layer -- opinions a project may keep, disable, or tune.

## Where standards files live

Standards files sit in a skills-kit config layer directory (mirroring
`bootstrap.json` layering), one file per file type. The filename is a
discoverability convention; the block's `applies_to:` key is authoritative.

| Filename | `applies_to` primitive |
|---|---|
| `SKILL-standards.md` | `skill_md` |
| `CLAUDE-md-standards.md` | `claude_md` |
| `reference-standards.md` | `reference_doc` |
| `doc-standards.md` | `plain_md` |

The primitive ids are the file-type sub-kinds registered in
`plugins/skills-kit/skills/md-domain/references/audit-framework.yaml`. If the
filename and `applies_to` disagree, `applies_to` wins; keep them aligned so the
file is discoverable by name.

## The `standards_set` block

Top-level fields:

- **`identity`** (required) -- one sentence stating what this set governs and
  for which file type. Read aloud, it answers "which opinions does this file
  carry, and over what."
- **`applies_to`** (required) -- the file-type primitive id this set governs.
  Authoritative over the filename convention.
- **`criteria`** (required, at least one) -- the list of standards. Each entry
  is one checkable opinion.

Each criterion carries:

- **`id`** (required) -- a stable kebab-case identifier. It is the config knob a
  user disables (`rules: {<id>: off}`) and the key an audit finding quotes, so a
  reader can go from finding to config line. Keep it stable once shipped.
- **`statement`** (required) -- the standard, stated as a single checkable
  proposition. One criterion, one proposition.
- **`severity`** (required) -- one of `fail`, `info`, `judgment` (see below).
- **`keywords`** (required, at least three) -- a chat-term routing cluster, as
  on every load-bearing record in the framework.
- **`example`** (optional) -- illustrative exemplar or before/after for the
  statement.
- **`enforcement`** (optional) -- `mechanical` or `judgment`; default
  `judgment` when absent (see below).

## Severity semantics

`severity` declares how a violation is dispositioned:

- **`fail`** -- a violation blocks. The finding is a hard failure the author
  must resolve (or disable the criterion) before the gate passes.
- **`info`** -- surfaced but non-blocking. The finding is reported for the
  author's awareness; it does not fail the audit.
- **`judgment`** -- the agent decides per instance. The finding is raised for a
  human-or-agent call rather than mechanically dispositioned.

## Enforcement

`enforcement` declares how a criterion is evaluated:

- **`judgment`** (default) -- the detect lane evaluates the criterion from its
  `statement` text. No code is involved; the standard is enforced entirely from
  what it says.
- **`mechanical`** -- a registered evaluator, keyed by the criterion `id`,
  performs the check (for example a character count or a regex). The standards
  file remains the single source of truth for the statement, id, and default;
  the code is only the evaluator.

Omit `enforcement` and the criterion is a judgment criterion.

## Verbatim-quote enforcement posture

A standards criterion is enforced by quoting it verbatim, never by paraphrase.
When the detect lane raises a finding against a standards criterion, it emits
the criterion's exact `statement` text together with the source path of the
standards file that declared it. An agent does not infer a rule the standards
file does not state, and does not restate a criterion in its own words. This
mirrors the ancestor-CLAUDE.md convention enforcement already in the audit
lanes: the standard, its wording, and its provenance travel together so the
author can trace every finding back to the exact line that produced it.

## Complete example

A minimal, valid standards file governing `skill_md`:

```yaml
standards_set:
  identity: Optional description-hygiene standards for SKILL.md files.
  applies_to: skill_md
  criteria:
    - id: desc-160-char
      statement: The description frontmatter field is at most 160 characters.
      severity: fail
      keywords: [description length, 160 char, hygiene]
      enforcement: mechanical
      example: "A 240-char description is truncated in the skill picker; tighten to <=160."
    - id: desc-directive-form
      statement: The description opens with a directive verb naming when to use the skill.
      severity: info
      keywords: [description form, directive verb, when to use]
      enforcement: judgment
```
