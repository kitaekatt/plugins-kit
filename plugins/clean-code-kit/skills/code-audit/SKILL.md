---
_schema_version: 1
name: code-audit
author: christina
skill-type: domain-skill
description: Use when auditing a git repository's code at rest for duplication or complexity. Do NOT use for reviewing a change or PR, auditing markdown, or fixing code.
---

# Code Audit

Measuring what is wrong in a body of code at rest -- a whole git repository,
not a change -- by running metric tools over a snapshot of it and writing a
findings report. The audit is report-only: it never edits the target's code,
and every finding is a measurement for the user to weigh, not a fix to apply.

The YAML contract below is the load-bearing surface; match user phrasing
against the `keywords:` clusters to route to a capability or a lane reference.

```yaml
domain_skill:
  _schema_version: "1"
  identity: >-
    Measuring a git repository's code at rest with metric tools, one lane per
    tool, and writing a ranked findings report without editing the code.
  companions:
    siblings: []
    note: >-
      No sibling domains in this plugin. Two neighbours in other plugins share
      the word "audit" or "review" and differ by SUBJECT: code review
      (git-kit:git-code-review, p4-kit:p4-code-review) judges a CHANGE -- a
      diff, a branch, a changelist -- while this skill measures a codebase at
      rest with no diff to bound it; skills-kit:md-domain audits MARKDOWN
      documents and reads code only as a source for them. Route a diff or PR
      to code review and a document to md-domain.
  scope:
    covers:
      - running an audit over a target git repository, with every enabled lane or the lanes the user names
      - listing the lanes this version registers
      - reading and explaining a report the audit wrote
      - helping the user write the target's config file when it is missing
      - the duplication lane (tool dryer) and the complexity lane (tool crapper, ranked by cyclomatic complexity without coverage)
    excludes:
      - editing, refactoring or "fixing" the target's code -- deciding what to fix is the user's, and fixing is separate work after the audit
      - "reviewing a change, diff, branch or PR (code review: git-kit:git-code-review, p4-kit:p4-code-review)"
      - auditing or writing markdown documents (skills-kit:md-domain)
      - coverage-backed CRAP scores -- not available in this version; the config refuses coverage true
      - mutation testing -- not available in this version
      - dependency-direction analysis -- not available in this version
      - verifying or closing md-domain's potential-defect records (CLAUDE-potential-defects.md) -- not handled in this version
  orientation:
    summary: >-
      One command, `code_audit.py run`, audits one target: the git repository
      containing `--target`, which may be any directory inside it (default:
      the current directory). It reads the target's config at
      `.claude/clean-code-kit/config.yaml`, where every key is required and
      none has a default, copies a
      snapshot of the repository (committed HEAD, or the working tree) into a
      temporary directory, prunes it to the configured languages and excludes,
      runs each lane's tool on that copy, and writes `report.md` plus
      `findings.json` into a report directory under the target's
      `.local-data/`. The temporary copy is removed afterwards, and nothing
      else is written inside the target. A lane is one metric tool behind one
      interface; this version ships two lanes, duplication and complexity.
      Both tools load tree-sitter parsers from the plugin's parser cache,
      which a bootstrap pass fills; a run fetches them itself when that
      pass could not. A successful run prints one line per lane, and its
      last stdout line is
      `REPORT <absolute report directory>`; exit 1 means a tool failed
      (its stderr is shown verbatim) and exit 2 means a usage, config or
      environment refusal whose message names the file or path at fault.
    vocabulary:
      - term: target
        definition: >-
          The git repository being audited: the top level of the repository
          that contains the `--target` directory.
      - term: lane
        definition: >-
          One metric tool run behind a common interface, enabled per target in
          the config's `lanes:` block. This version registers `duplication`
          and `complexity`.
      - term: snapshot
        definition: >-
          The copy of the target the tools actually read. `head` holds the
          committed HEAD only; `working-tree` holds tracked and untracked,
          non-ignored files as they are on disk. The report records the mode,
          the HEAD sha and which paths were dirty (the config file excluded).
      - term: finding
        definition: >-
          One measured item in a lane's output -- a duplicate set or a ranked
          function -- carrying the tool's metrics and a rank. A measurement,
          not a verdict.
      - term: report directory
        definition: >-
          `<target>/.local-data/plugins-kit/clean-code-kit/reports/<UTC
          timestamp>-<first 8 chars of HEAD>/`, holding `report.md` and
          `findings.json`. It must be git-ignored in the target, and a run
          refuses (exit 2) when it already exists rather than overwrite
          it.
    behavioral_guardrails:
      - >-
        Announce before running: the target repository path, the snapshot mode
        (the config's `snapshot`, or `--snapshot` when given) and the lanes
        (the config's enabled lanes, or the `--lane` values when given). Read
        the target's config file to state them; do not run first and describe
        afterwards.
      - >-
        Never write into the target except the report directory, which the
        runner itself writes. During an audit do not create, edit or delete
        any file in the target. The one file this skill may help create there
        is the config file, and only after the user has confirmed its content.
      - >-
        A finding is a measurement, not a verdict. Present the numbers and the
        ranking; deciding what is worth fixing, and in what order, is the
        user's call.
      - >-
        Do not fix findings as part of an audit run. If the user wants a
        finding acted on, that is a separate request, made after the report is
        read.
      - >-
        When the config file is missing (exit 2 naming
        `.claude/clean-code-kit/config.yaml`), help the user write one from
        references/configuring.md: start from `code_audit.py init` output and adjust `source_roots`, `test_roots`,
        `languages` and `exclude` from what the repository actually contains,
        say what you proposed and why, and wait for confirmation. Do not guess
        the source roots silently, and do not run the audit on an unconfirmed
        config.
      - >-
        Complexity findings carry a file and a start line but no end line:
        crapper does not report one. Do not invent an end line.
      - >-
        On exit 1, relay the tool's stderr as printed. On exit 2, relay the
        message; it names the file or path at fault. Do not retry with
        different flags to get past either without telling the user why.
      - >-
        When exit 1 carries the line "parsers are fetched on first use and
        need network once", the parser cache is empty and the run had no
        network. Read the `tree_sitter_parsers` entry of
        `${CLAUDE_BOOTSTRAP_DATA_ROOT:-$HOME/.claude/plugins/data}/plugins-kit/clean-code-kit/deferred_requirements.json`
        when it exists and present its `agent_msg`, which names the fetch
        command; otherwise say that the next run with network fetches the
        parsers once. Ask before running the fetch command.
      - >-
        When the run refuses because the report directory is not git-ignored,
        relay the message (add `.local-data/` to `.git/info/exclude` or
        `.gitignore`). That edit is the user's to make or to approve; do not
        make it silently.
  index:
    references:
      - id: configuring
        path: references/configuring.md
        keywords: [config, config.yaml, .claude/clean-code-kit, schema, missing
                   config, unknown key, source_roots, test_roots, languages,
                   exclude, snapshot, head, working-tree, lanes block,
                   threshold, min_lines, group_identical, coverage, top,
                   required keys, no defaults, extensions, fnmatchcase,
                   report location, .local-data, git-ignore, check-ignore,
                   report directory exists, parser cache, tree-sitter,
                   deferred requirement, tree_sitter_parsers, test files
                   skipped, go, java, rust, clojure, exit codes, refusal
                   messages]
        summary: >-
          The target's config file: its path, every key and its meaning, the
          language-to-extension table and what the tools skip or measure
          beyond it, the two snapshot modes, where reports and the parser
          cache are written and why the report directory must be git-ignored,
          the exit codes and every refusal message. Read it when writing or
          fixing a config, or when a run exits 1 or 2.
      - id: lane-duplication
        path: references/lane-duplication.md
        keywords: [duplication, duplicate code, copy-paste, dryer, similarity,
                   score, copies, identical group, group_identical, threshold,
                   min_lines, min-nodes, languages read, gdscript, cpp, vendored copies,
                   exclude, report table, parser cache, follow-up task]
        summary: >-
          The duplication lane (dryer): what it measures, how to read its
          report table, its known limits (languages read: JavaScript, GDScript and C++ included, `.c++`/`.hxx` ignored,
          deliberately vendored copies, what dryer skips on its own, the
          parser cache) and what a sensible follow-up task looks like.
      - id: lane-complexity
        path: references/lane-complexity.md
        keywords: [complexity, cyclomatic complexity, crapper, crap score,
                   coverage, no coverage, ranking, top, namespace, symbol,
                   file and start line, no end line, gdscript, cpp, javascript as
                   typescript, every function in findings.json, report
                   table, parser cache, follow-up task]
        summary: >-
          The complexity lane (crapper without coverage): what it measures,
          how to read its ranked table, its known limits (no file location,
          no coverage-backed CRAP score, JavaScript scored as TypeScript, C++ and GDScript
          scored without coverage,
          what crapper skips on its own, the parser cache) and what a
          sensible follow-up task looks like.
  capabilities:
    - id: run_audit
      keywords: [run audit, audit this repo, code audit, measure code,
                 duplication, complexity, all lanes, one lane, snapshot,
                 working tree, head]
      description: >-
        Audit the target with every lane enabled in its config, or only the
        lanes named with `--lane` (repeatable; a named lane runs even when
        the config disables it). With no lane enabled and no `--lane` the
        run exits 2. `--snapshot` overrides the config's snapshot mode.
        Announce target, snapshot mode and lanes first (see
        behavioral_guardrails). On success stdout carries one line per lane
        (`<lane>: <status>, <n> findings, <seconds> s`) and its last line is
        `REPORT <absolute report directory>`.
      operation: >-
        "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLAUDE_PLUGIN_ROOT}/scripts/code_audit.py" run --target <dir> [--lane duplication] [--lane complexity] [--snapshot head|working-tree]
      tool: scripts/code_audit.py
      scope_axes: [lanes, snapshot]
      reference_section: configuring.md
    - id: propose_config
      keywords: [init, propose config, write config, missing config, source_roots guess]
      description: >-
        Print a proposed config.yaml for a repository from its `git ls-files`
        extension counts (each top-level directory is proposed as the deepest directory holding all its supported files; a top-level directory with a single supported file is listed, not rooted). It writes nothing; review it with the user and save
        it only after they confirm.
      operation: >-
        "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLAUDE_PLUGIN_ROOT}/scripts/code_audit.py" init --target <dir>
      tool: scripts/code_audit.py
      reference_section: configuring.md
    - id: list_lanes
      keywords: [list lanes, which lanes, available lanes, what can it
                 measure, lane names]
      description: >-
        Print the lane names this version registers, one per line. Lane names
        are the keys of the config's `lanes:` block and the values `--lane`
        accepts.
      operation: >-
        "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLAUDE_PLUGIN_ROOT}/scripts/code_audit.py" lanes
      tool: scripts/code_audit.py
    - id: read_report
      keywords: [read report, explain report, findings, report.md,
                 findings.json, what did it find, worst offenders, ranked]
      description: >-
        Read a report the audit wrote: `report.md` in the report directory
        gives a coverage table (tracked vs measured files per extension; measured and unread source languages listed, all other files one count), per-lane
        counts and a ranked table (the complexity table only the first `top`
        functions at or above `min_complexity`, plus a complexity distribution
        and the files with no scored function); `findings.json` holds every finding with
        exact values plus the run's provenance (target
        repo, HEAD, snapshot mode, dirty paths, each lane's tool commit, argv,
        exit, seconds and status). Explain columns from the lane's reference;
        do not re-rank or re-score.
      operation: >-
        Read <report directory>/report.md, then <report directory>/findings.json
        for exact values. The report directory is the path on the run's
        `REPORT` line.
      reference_section: lane-duplication.md / lane-complexity.md
  tools:
    - name: code_audit.py
      command: >-
        "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLAUDE_PLUGIN_ROOT}/scripts/code_audit.py" <run|init|lanes> ...
      description: >-
        The audit runner. It re-execs under the plugin's bootstrap-provisioned
        venv and refuses (exit 2) when an installed tool's commit differs from
        the commit pinned in the plugin's pyproject.toml ("installed tool does
        not match the pinned commit; run bootstrap"). Exit codes: 0 report
        written; 1 a tool failed; 2 usage, config or environment refusal.
```

```yaml
asset_dependencies:
  - path: ../../scripts/code_audit.py
    consumer: SKILL.md capabilities run_audit and list_lanes
    purpose: the audit runner every capability launches
  - path: ../../pyproject.toml
    consumer: scripts/code_audit.py
    purpose: pins the dryer and crapper commits the runner's provenance check compares against
    invariant: each tool requirement pins a 40-character commit sha
```

## When to invoke

- "Audit this repo", "find duplicated code", "what are the most complex
  functions" -- announce target, snapshot mode and lanes, then `run`.
- "Which lanes are there" -- `lanes`.
- "What did the audit find", "explain this report" -- read the report
  directory named on the run's `REPORT` line, with the lane's reference.
- A run that exits 2 naming the config file -- help write the config from
  `references/configuring.md`, confirm it with the user, then run.

Do NOT use to review a diff or PR, to audit markdown, or to change the code
the audit measured.
