# Repo-Independent Standards: separating the toolkit from the standards

Status: concept capture, 2026-07-22. This documents a direction
the author has been considering for a while; it exists so the idea has a home
and a shape before any implementation decision.

Implementation status (2026-07-23): the first increment is implemented --
configurable optional rules (disable/tune via `config.yaml`), the layered
user/project standards resolution, and additive per-file-type standards files
(`*-standards.md` carrying a `standards_set:` block). Architectural opinions
stay hard-coded; inoffensive integrity checks stay knob-less. For the
configuration surface (layer model, `config.yaml` format, the rule-id catalog,
thresholds), see
[../skills/md-domain/references/configuring-standards.md](../skills/md-domain/references/configuring-standards.md).

## The observation

As of 2026-07-22 skills-kit is two things fused together:

1. **A toolkit** for authorship, auditing, and review of the markdown artifacts
   Claude loads -- the machinery: audit fan-out, findings taxonomy
   (FIX/SERIOUS/IMPROVE), placement algorithm, remediation-as-reviewable-CL.
2. **A set of opinionated standards** the toolkit currently enforces -- skill
   type contracts, the structured CLAUDE.md insights schema, cohesion
   principles (CCP/CRP/ADP) applied to docs, total-ownership of md files.

These are separable, and separating them is the opportunity. The toolkit is
general; the standards are one (good) opinion. As of 2026-07-22 a user who wants the
toolkit must adopt the opinion wholesale, and a user who has their own
standards has no way to plug them in.

## The vocabulary: authorship / auditing / review

Three verbs, one substrate (standards):

- **Authorship** -- establishing standards that are adhered to when creating
  new documents or code. Includes extracting standards FROM existing files:
  skills-kit can look at what a repo already does and encode the observed
  conventions into CLAUDE.md. Standards are authored from observation, not
  invented in a vacuum.
- **Auditing** -- reviewing a whole file against standards and revising it to
  compliance. (As of 2026-07-22: /md-domain audit.)
- **Review** -- reviewing a CHANGE against standards and revising it to
  compliance. (As of 2026-07-22: git-kit /git-code-review and p4-kit /p4-code-review,
  whose CLAUDE.md-convention-aware reviewer quotes the violated rule
  verbatim.)

The three compose into a loop: authorship extracts and encodes the standards;
auditing brings existing files up to them; review keeps every change compliant
going forward.

## Why repo-independence matters

CLAUDE.md files are innately project-specific. That is correct for
project-specific direction -- but many standards span every project a user
touches (how skills are structured, naming discipline, doc placement, review
etiquette, language conventions). Copying those into every repo's CLAUDE.md is
duplication with drift; leaving them out means the review layer cannot see
them.

skills-kit already proves the pattern works: its skill-standards apply to
skills in ANY project, independent of any CLAUDE.md. The generalization is to
make that a first-class layer rather than a special case.

This is also the main value proposition of git-kit and p4-kit: they support
repo-independent standards for code review. The CLAUDE.md-aware reviewer is
the delivery mechanism; the standards layer is what it delivers. And because
standards-compliant CLAUDE.md files are exactly what convention-following
review tools consume, better standards make EVERY reviewer better -- including
Anthropic's native /code-review, which also reads CLAUDE.md. skills-kit does
not compete with native review; it feeds it.

## What standards-as-a-layer enables (existing proof: domain skills)

Standards on top of Claude's skill system already enable capabilities skills
do not natively have. The domain skill is the working example: a wrapper that

- **encapsulates multiple related skills into one** -- "if you are using any
  of these, you care about all of them." Instead of seven skills bloating
  context permanently, the user expresses interest in a topic once and the
  whole domain becomes available. Context loading on expressed interest,
  not on installation.
- **encodes operational procedures within a specific context** -- the
  domain carries not just member skills but how work is done inside that
  domain.

Nothing in the native skill system provides this encapsulation; the standard
does. That is the existence proof that a standards layer is a capability
layer, not just a style guide.

## The proposed separation

1. **Toolkit** -- skills-kit's machinery, standards-agnostic: given a set of
   standards, author/audit/review any file against them.
2. **Standards sets** -- data, not code. Installed at the USER level (not per
   repo), likely a JSON (or YAML) file; configurable; composable with
   project-level CLAUDE.md direction (project direction wins on conflict,
   or per a declared precedence).
3. **Authoring standards sets is open** -- the format is documented and
   validated, so anyone can write a compliant set. skills-kit's current
   opinions become the first shipped set (the reference implementation),
   not the hard-coded truth.
4. **A marketplace of standards** -- the eventual shape. Initially: host a
   small number of good sets in this repo. If it catches on: a real
   marketplace where standards sets are published, versioned, and installed
   like plugins. Standards become a shareable artifact with an ecosystem,
   the way skills themselves became one.

## Rough implementation surface (sizing, not a plan)

- A standards-set schema + validator (skills-kit already has schema
  machinery to build on).
- A resolution layer: user-level standards file(s) + project CLAUDE.md,
  with declared precedence.
- Refactor the audit/authoring skills to consume resolved standards instead
  of embedding them.
- git-kit/p4-kit conventions reviewer consumes the same resolved layer
  (it already consumes CLAUDE.md; this adds one more source).
- Extraction flow ("author standards from this repo's existing files")
  writes either project CLAUDE.md entries or user-level standards,
  chosen by the user per finding.

This is substantial work. Nothing here commits to it; the point of this
document is that the idea is now written down, in skills-kit, where it
belongs.
