# skills-kit

Your CLAUDE.md is a codebase. Audit it like one.

## What it does

skills-kit is generation and auditing tooling for the markdown a project
accumulates -- the SKILL.md and CLAUDE.md files Claude Code loads, and the
project docs and READMEs it does not. It treats every md file in a repo as
one owned surface with a place each fact belongs, rather than a pile of
files that drift apart independently.

There is one front door, `/md-domain`, and it works on a **verb x artifact**
grammar: pick a verb (`audit`, `author`, `generate`, or `analyze`) and, for
`audit`/`author`, an artifact (`skill`, `claude-md`, `project-doc`, or
`references`; `references` is audit-only).

```
/md-domain audit claude-md ./CLAUDE.md
/md-domain author skill
/md-domain generate claude-md ./some-directory
/md-domain analyze ./some-directory
```

- **audit** checks what you already have: placement (does this fact belong
  in this file?), cohesion, drift over time, and cross-reference integrity
  (dangling skill references, broken load-graph edges). It produces a
  reviewable set of findings -- FIX / SERIOUS / IMPROVE -- for you to accept
  or decline. It does not auto-rewrite your files.
- **author** guides writing an artifact from content you supply: the per-type
  contract for the artifact you are producing, where the content belongs, and
  what shape it takes. "Generate a skill" routes here -- no analysis produces
  coverage for a skill.
- **generate** writes a CLAUDE.md (or the human-html orientation page) from
  analysis-produced coverage, so its claims stay re-checkable against the code
  that produced them. It takes only `claude-md` or `human-html`.
- **analyze** is report-only: it reads one directory's direct code (or, with
  the `human-html` selector, a whole subtree) and reports what it found. It
  never remediates.

Both verbs read the same four standards documents -- one per artifact,
covering SKILL.md, CLAUDE.md, project documents, and cross-references. That
single-source-of-truth arrangement is the point: "what good looks like" is
written down once and read in two directions, so an audit can never enforce
a rule the generation guidance does not teach.

The underlying methodology is package-design cohesion applied to docs:
CCP (things that change together live together), CRP (if you load a file
you should need all of it), ADP (no dependency cycles). Together they form
a placement algorithm for the question "which file should this fact live
in" -- root CLAUDE.md vs subsystem CLAUDE.md vs a skill reference vs a
project doc.

**Why run it across a repo, not one file.** Docs rot by diverging: the root
CLAUDE.md says one thing, a subsystem CLAUDE.md contradicts it, a fact that
belongs in a skill reference is stranded in a README, and a renamed skill
leaves dangling cross-references. Auditing the whole md surface at once is
what surfaces those -- a single-file check cannot see a fact in the wrong
file or a broken edge between two files. It also matters because the code
reviewers you already run (including Claude Code's own /code-review) read
CLAUDE.md to decide what "correct" means; an unaudited CLAUDE.md silently
sets a wrong standard for every review.

This is skills-kit's place in a generation -> auditing -> review path: it
generates and audits the standards, and the `git-kit` and `p4-kit` reviewers
(in this same marketplace) enforce those same CLAUDE.md standards on every
change. Generation and auditing set the standard; review keeps each change
compliant with it.

## Also included

- **knowledge-encoding** -- encode a newly discovered insight into the right
  persistent home.
- **update-documentation** -- an end-of-session pass over what the work
  implies for your docs.
- **materialized-output** -- a design pattern for tools that materialize an
  insight from deep scans over project data.

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
working as intended, but budget for it. Which opinions are enforced is
configurable: individual rules can be turned off and thresholds tuned in a
layered `config.yaml`.

## Install

```
/plugin marketplace add kitaekatt/plugins-kit
/plugin install skills-kit@plugins-kit
```

The bootstrap plugin (dependency provisioning) is declared as a dependency
and installs automatically.

## Try this first

```
/md-domain
```

Bare invocation shows the menu: audit or generate a SKILL.md, a CLAUDE.md, a
project doc, or (audit only) cross-references. Point it at your root
CLAUDE.md.

## When not to use it

A 30-line CLAUDE.md in a small repo does not need this machinery. The
cohesion framework earns its keep when you have enough md surface --
multiple CLAUDE.md files, a skill corpus, reference docs -- that "where
does this fact live" is a real question with a wrong answer.
