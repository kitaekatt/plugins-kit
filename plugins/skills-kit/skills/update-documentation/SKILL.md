---
_schema_version: 1
name: update-documentation
author: christina
description: Use when ending a session to review work and recommend doc updates to CLAUDE.md, skills, and reference docs. Do NOT use for new skills or one-off notes.
activation_contexts:
  - "update documentation based on session"
  - "end of session documentation review"
  - "what documentation should I update"
  - "review session for documentation gaps"
skill-type: technique-skill
---

## Skill Purpose

Evaluate the current session's work and recommend documentation updates that will make future sessions more efficient. This skill translates session experience into persistent improvements across CLAUDE.md files, skills, and reference documents.

Run at end of session before committing/pushing, when there is meaningful work to capture.

## When to Use

- Scenario: End of session with code changes, new scripts, or modified interfaces
- Scenario: Session involved errors that cost significant time to recover from
- Scenario: Exploration was required to locate information that could be documented
- Scenario: User asks to review what documentation should change after a task

## Contract

The YAML block below is the load-bearing contract; the prose around it is orientation.

```yaml
technique_skill:
  _schema_version: "1"
  identity: The end-of-session procedure for auditing the session's work, filtering the documentable events, classifying each into a documentation target, and presenting a severity-ranked recommendation table for user approval.
  scope:
    covers:
      - reviewing session work for documentation candidates
      - recommending updates to CLAUDE.md files, skills, and reference docs
      - making approved documentation changes
    excludes:
      - creating new skills (hand off to md-domain's author skill lane)
      - deep placement design across the load graph (hand off to md-domain's plugins/skills-kit/skills/md-domain/references/cohesion-principles.md / knowledge-encoding)
      - memory system updates (separate from documentation)
  techniques:
    - id: session_doc_review
      name: End-of-session documentation review
      keywords: [end of session, documentation review, what should I document, session audit, doc update recommendations, capture learnings]
      goal: Turn one session's work into a small set of approved, high-value documentation updates.
      preconditions:
        - "A session with meaningful work to capture -- code changes, recovered errors, or expensive exploration."
      steps:
        - n: 1
          action: "Session audit. Review the session for three categories of documentable events. CODE CHANGES (new or modified interfaces, scripts, libraries, configurations): check `git diff --stat` and `git log --oneline` for this session's commits; look for new files, renamed files, changed APIs, new scripts, modified configs. RECOVERED ERRORS (tool errors, incorrect actions, misunderstood requests): review the conversation for corrections, retries, and wrong assumptions, focusing on errors caused by missing context rather than typos or transient failures. INEFFICIENT OPERATIONS (expensive exploration to find information): review the conversation for repeated searches and multi-step lookups for simple facts -- e.g. grepping across many files to find something that could be documented with a path."
          tool: "Bash (git diff --stat, git log --oneline)"
          expected: "A raw candidate list spanning the three categories."
        - n: 2
          action: "Filter. Apply each filter below to every candidate and drop the items that fail."
          filters:
            - filter: Recurrence
              keep_if: Likely to happen again in future sessions
            - filter: Consequence
              keep_if: Cost more than 2-3 tool calls to resolve
            - filter: Not already documented
              keep_if: Information isn't already in CLAUDE.md, skills, or docs
            - filter: Not derivable
              keep_if: "Can't be found in 1-2 obvious tool calls (e.g. `git log`, reading a file header)"
          expected: "A surviving candidate list; every dropped item failed a named filter."
        - n: 3
          action: "Classify. Place each surviving candidate into exactly one documentation target, then apply the classification rules to break ties. Size guidelines are NOT restated here -- md-domain's standards docs are the SSOT."
          targets:
            - target: "Root `~/.claude/CLAUDE.md`"
              when_appropriate: Universally needed for navigation or avoiding common mistakes
              size_guideline: "size thresholds: see md-domain references/standards/claude-md-standards (hygiene thresholds; signals, not verdicts)"
            - target: "Directory `CLAUDE.md`"
              when_appropriate: Specific to files in that directory/subdirectory
              size_guideline: "size thresholds: see md-domain references/standards/claude-md-standards (hygiene thresholds; signals, not verdicts)"
            - target: "Skill `SKILL.md`"
              when_appropriate: Within an existing skill's domain
              size_guideline: "size thresholds: see md-domain references/standards/skill-standards (hygiene thresholds; signals, not verdicts)"
            - target: Skill reference doc
              when_appropriate: Sub-domain detail not always needed
              size_guideline: No hard limit; loaded conditionally
          classification_rules:
            - "If it helps locate information quickly across the project -> root CLAUDE.md"
            - "If it helps understand files in a specific directory -> directory CLAUDE.md"
            - "If it's domain expertise relevant when a skill is active -> skill or skill reference"
            - "If a skill isn't required in the error situation, don't add error avoidance to that skill"
          expected: "Every surviving candidate is bound to exactly one target."
        - n: 4
          action: "Evaluate integration. For each candidate, check the current state of the target document: (1) read the target document; (2) assess its current size against md-domain's standards thresholds; (3) determine whether the addition is worth the context cost; (4) if the target is at or near a hygiene threshold, consider whether existing content can be condensed or whether a different target is better. Size is a CRP-evaluation signal, not a verdict -- run the CRP test before proposing a split."
          tool: "Read (the target document)"
          expected: "A keep / drop / retarget decision per candidate, informed by the target's current state."
        - n: 5
          action: "Present recommendations. Output the table of recommended changes with a severity per row, then WAIT for user approval before making any changes. The user may accept all, select specific items, or modify recommendations."
          output_template: |
            | # | Target file | Change type | Summary | Severity |
            |---|------------|-------------|---------|----------|
            | 1 | ~/.claude/CLAUDE.md | Add line | Quick ref for new script X | Medium |
            | 2 | ~/.claude/skills/foo/SKILL.md | Add section | Pattern for handling Y | High |
          severity_levels:
            - level: High
              meaning: Will save significant time or prevent consequential errors in future sessions
            - level: Medium
              meaning: Will save a few tool calls or avoid minor confusion
            - level: Low
              meaning: Nice to have but marginal benefit
          expected: "A severity-ranked table presented to the user; no edits made until approval."
      checklist:
        - "Session audited across all three categories (code changes, recovered errors, inefficient operations)"
        - "Every candidate run through all four filters"
        - "Every survivor classified into exactly one target"
        - "Each target document read and its current state assessed"
        - "Severity-ranked table presented and user approval obtained before editing"
  anti_patterns:
    - id: document_rare_recoverable_errors
      name: Documenting rare, recoverable errors
      keywords: [one-off failure, transient error, self-correcting, rare error, context noise]
      why_it_seems_right: "The error happened and cost time, so writing it down feels like learning from it."
      why_it_is_wrong: "One-off failures that self-correct waste context space in every future session that loads the doc, while preventing nothing."
      alternative: "Apply the Recurrence and Consequence filters in step 2 -- keep only what is likely to recur AND cost more than 2-3 tool calls."
    - id: document_the_obvious
      name: Documenting correct-but-obvious information
      keywords: [obvious fact, derivable, no tool calls saved, filler documentation, restating the code]
      why_it_seems_right: "The statement is true and accurate, so adding it looks like an improvement to the docs."
      why_it_is_wrong: "If it doesn't save tool calls it is noise -- it dilutes the signal in the doc and costs context on every load."
      alternative: "Apply the Not derivable filter in step 2 -- drop anything findable in 1-2 obvious tool calls."
    - id: misplaced_error_avoidance
      name: Adding error avoidance to skills not involved in the error
      keywords: [wrong skill, out of the path, error avoidance, skill not loaded, gated behind trigger]
      why_it_seems_right: "The skill is topically related to the error, so it looks like a reasonable home for the warning."
      why_it_is_wrong: "The documentation must sit in the path of the work that triggers the error; a warning behind a skill that never loads in that situation is never read."
      alternative: "Use the classification rules in step 3 -- if a skill isn't required in the error situation, place the guidance in the CLAUDE.md that is ambient there instead."
    - id: duplicate_existing_docs
      name: Duplicating existing documentation
      keywords: [already documented, duplicate, ssot violation, copy instead of pointer, drift]
      why_it_seems_right: "Restating the fact in the target under review makes that document self-contained."
      why_it_is_wrong: "Two copies of one fact drift independently and break SSOT; the reader pays for the same content twice."
      alternative: "Apply the Not already documented filter in step 2; when the fact exists elsewhere, add a pointer, not a copy."
    - id: over_document_one_session
      name: Over-documenting a single session
      keywords: [too many updates, marginal recommendations, volume over value, session dump, long list]
      why_it_seems_right: "A long recommendation list looks like a thorough review of the session."
      why_it_is_wrong: "Marginal entries crowd out the valuable ones and inflate every future context load; 2-4 high-quality updates beat 10 marginal ones."
      alternative: "Filter hard in step 2 and rank by severity in step 5; present the small high-severity set."
```

## Integration Points

- **knowledge-encoding** (in plugins-kit:skills-kit) - For deeper analysis of where insights should live in the project structure
- **md-domain** (in plugins-kit:skills-kit) - If recommendations include authoring a new skill (`/md-domain author skill`) or a CLAUDE.md (`/md-domain author claude-md`)
- **content-authoring** (an md-domain reference, `plugins/skills-kit/skills/md-domain/references/authoring-patterns/content-authoring.md`) - For how a recommended doc update should be shaped
- **md-domain standards** (`plugins/skills-kit/skills/md-domain/references/standards/`) - SSOT for the per-artifact size and hygiene thresholds referenced by steps 3-4

## Scope Boundaries

See the `scope:` block in the contract above -- it is the load-bearing statement.
