# skills-kit

Your CLAUDE.md is a codebase. Audit it like one.

## What it does

skills-kit is authoring and auditing tooling for the markdown artifacts
Claude Code actually loads: SKILL.md files and CLAUDE.md files.

- `/md-authoring` guides writing them. It routes to a per-artifact member
  (skill-authoring for SKILL.md, claude-md-authoring for CLAUDE.md), each
  with a per-type contract: what a well-formed artifact of that kind must
  contain, and in what shape.
- `/md-audit` checks what you already have: placement (does this fact
  belong in this file?), cohesion, drift over time, and cross-reference
  integrity (dangling skill references, broken load-graph edges). It
  produces a reviewable set of findings -- FIX / SERIOUS / IMPROVE -- for
  you to accept or decline. It does not auto-rewrite your files.

The underlying methodology is package-design cohesion applied to docs:
CCP (things that change together live together), CRP (if you load a file
you should need all of it), ADP (no dependency cycles). Together they form
a placement algorithm for the question "which file should this fact live
in" -- root CLAUDE.md vs subsystem CLAUDE.md vs a skill reference vs a
project doc.

## How it relates to neighboring tools

- Anthropic's skill-creator generates new skills from a description.
- Deterministic linters check markdown syntax and frontmatter shape.
- skills-kit is the semantic layer above both: it judges placement,
  cohesion, and drift across a whole corpus of md files, and it audits
  files that already exist rather than generating new ones.

## Honest caveat

The audits are opinionated. They check for conventions this plugin
advocates -- typed SKILL.md contracts (a YAML contract block per skill
type), structured CLAUDE.md insight blocks -- which your existing files
almost certainly do not have yet. Expect the first audit of a mature
project to propose adopting structure, not just fixing typos. That is
working as intended, but budget for it.

## Install

```
/plugin marketplace add kitaekatt/plugins-kit
/plugin install skills-kit@plugins-kit
```

The bootstrap plugin (dependency provisioning) is declared as a dependency
and installs automatically.

## Try this first

```
/md-audit
```

Bare invocation shows a menu: audit a SKILL.md, a CLAUDE.md, a project
doc, or cross-references. Point it at your root CLAUDE.md.

## When not to use it

A 30-line CLAUDE.md in a small repo does not need this machinery. The
cohesion framework earns its keep when you have enough md surface --
multiple CLAUDE.md files, a skill corpus, reference docs -- that "where
does this fact live" is a real question with a wrong answer.
