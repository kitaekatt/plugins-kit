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
---

## Skill Purpose

Evaluate the current session's work and recommend documentation updates that will make future sessions more efficient. This skill translates session experience into persistent improvements across CLAUDE.md files, skills, and reference documents.

Run at end of session before committing/pushing, when there is meaningful work to capture.

## When to Use

- Scenario: End of session with code changes, new scripts, or modified interfaces
- Scenario: Session involved errors that cost significant time to recover from
- Scenario: Exploration was required to locate information that could be documented
- Scenario: User asks to review what documentation should change after a task

## Process

### Phase 1: Session Audit

Review the session for three categories of documentable events:

**Code changes** - new or modified interfaces, scripts, libraries, configurations
- Check: `git diff --stat` and `git log --oneline` for this session's commits
- Look for: new files, renamed files, changed APIs, new scripts, modified configs

**Recovered errors** - tool errors, incorrect actions, misunderstood requests
- Review conversation for corrections, retries, wrong assumptions
- Focus on errors caused by missing context (not typos or transient failures)

**Inefficient operations** - expensive exploration to find information
- Review conversation for repeated searches, multi-step lookups for simple facts
- Look for: grepping across many files to find something that could be documented with a path

### Phase 2: Filter

Apply these filters to each candidate. Drop items that fail:

| Filter | Keep if... |
|--------|-----------|
| Recurrence | Likely to happen again in future sessions |
| Consequence | Cost more than 2-3 tool calls to resolve |
| Not already documented | Information isn't already in CLAUDE.md, skills, or docs |
| Not derivable | Can't be found in 1-2 obvious tool calls (e.g., `git log`, reading a file header) |

### Phase 3: Classify

Place each surviving candidate into a documentation target:

| Target | When appropriate | Size guideline |
|--------|-----------------|----------------|
| Root `~/.claude/CLAUDE.md` | Universally needed for navigation or avoiding common mistakes | Keep under 200 lines total |
| Directory `CLAUDE.md` | Specific to files in that directory/subdirectory | Keep under 60 lines |
| Skill `SKILL.md` | Within an existing skill's domain | Keep under 200 lines |
| Skill reference doc | Sub-domain detail not always needed | No hard limit; loaded conditionally |

**Classification rules:**
- If it helps locate information quickly across the project -> root CLAUDE.md
- If it helps understand files in a specific directory -> directory CLAUDE.md
- If it's domain expertise relevant when a skill is active -> skill or skill reference
- If a skill isn't required in the error situation, don't add error avoidance to that skill

### Phase 4: Evaluate Integration

For each candidate, check the current state of the target document:

1. Read the target document
2. Assess current size against guidelines
3. Determine if the addition is worth the context cost
4. If the target is near its size guideline, consider whether existing content can be condensed or whether a different target is better

### Phase 5: Present Recommendations

Output a table of recommended changes:

```
| # | Target file | Change type | Summary | Severity |
|---|------------|-------------|---------|----------|
| 1 | ~/.claude/CLAUDE.md | Add line | Quick ref for new script X | Medium |
| 2 | ~/.claude/skills/foo/SKILL.md | Add section | Pattern for handling Y | High |
```

**Severity levels:**
- **High** - Will save significant time or prevent consequential errors in future sessions
- **Medium** - Will save a few tool calls or avoid minor confusion
- **Low** - Nice to have but marginal benefit

Wait for user approval before making changes. User may accept all, select specific items, or modify recommendations.

## Anti-Patterns

- **Documenting rare, recoverable errors** - one-off failures that self-correct waste context space
- **Documenting correct-but-obvious information** - if it doesn't save tool calls, it's noise
- **Adding error avoidance to skills not involved in the error** - the documentation must be in the path of the work that triggers the error
- **Duplicating existing documentation** - if it's already documented somewhere, add a pointer, not a copy
- **Over-documenting a single session** - 2-4 high-quality updates beat 10 marginal ones

## Integration Points

- **knowledge-encoding** (in plugins-kit:skills-kit) - For deeper analysis of where insights should live in the project structure
- **md-domain** (in plugins-kit:skills-kit) - If recommendations include authoring a new skill (`/md-domain author skill`) or a CLAUDE.md (`/md-domain author claude-md`)
- **content-authoring** (an md-domain reference) - For how a recommended doc update should be shaped

## Scope Boundaries

**IN SCOPE:**
- Reviewing session work for documentation candidates
- Recommending updates to CLAUDE.md files, skills, and reference docs
- Making approved documentation changes

**OUT OF SCOPE:**
- Creating new skills (hand off to md-domain's author skill lane)
- Deep placement design across the load graph (hand off to md-domain's cohesion-principles.md / knowledge-encoding)
- Memory system updates (separate from documentation)
