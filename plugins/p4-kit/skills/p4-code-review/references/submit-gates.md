# Authoring submit gates

The CLAUDE.md-author-facing guide for writing submit-gate blocks. Submit gates are path-scoped pre-submit reminders authored in CLAUDE.md files; `p4-code-review` detects them deterministically (via `prepare_review.py`) and surfaces them verbatim at review time when at least one file in the CL falls within a gate's scope. This doc covers only how to author them; detection and rendering are described in the `submit_gates` block of the SKILL.md contract.

## Authoring format

Add this block to any CLAUDE.md (root, subdirectory, or both):

```
**Submit gate:** <imperative -- what the author must do>.
Applies to:
- <path prefix or glob>
- <path prefix or glob>

<optional rationale paragraph, rendered verbatim with the gate>
```

Scope path semantics:

- No glob characters (`*`, `?`, `[`): prefix match. `Foo/Bar/` matches every file under Foo/Bar/. `Foo/Bar` (no trailing slash) is equivalent and does NOT accidentally match `Foo/BarBaz/`.
- Contains glob characters: fnmatch-style glob, anchored to the workspace root. `*` matches anything including `/`; `?` matches one character.
- Case-insensitive on Windows, case-sensitive elsewhere.

Multiple gates per CLAUDE.md allowed; blocks must be separated by a blank line. Malformed blocks (missing `Applies to:`, empty scope list) are skipped with a one-line stderr warning -- never silently dropped.
