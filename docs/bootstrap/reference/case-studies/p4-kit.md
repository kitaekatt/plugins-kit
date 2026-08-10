# Case Study: p4-kit

P4 multi-agent code review plugin. Notable for **consuming `bootstrap` two ways**: as a SessionStart manifest target (the usual case — tool checks, venv install, legacy cleanup), AND as a Python library dependency at runtime — `prepare_review.py` imports `bootstrap_lib.code_review` for diff chunking, CLAUDE.md ancestor walks, and submit-gate parsing. That second mode is the same dependency pattern update06 uses, but here it's a peer-to-peer share between sibling plugins rather than a chicken-and-egg bootstrap-of-bootstrap.

## Current Operations

### Automatable

| Category | Condition | Check Method | Remediation |
|----------|-----------|-------------|-------------|
| Tool | `p4` not installed | `command -v p4` | `brew install --cask perforce` on macOS; manual download on Windows/Ubuntu |
| Tool | `uv` not installed | `command -v uv` | Platform install command |
| Tool | `claude` not installed | `command -v claude` | Manual (Claude Code CLI) |
| Library/Data | Python venv missing or broken | Check dir → binary → `import bootstrap_lib.code_review.chunking; import bootstrap_lib.code_review.claude_mds` | `uv sync` from `pyproject.toml` (installs `bootstrap` as a git subdir dep) |
| Legacy cleanup | `<project>/.local-data/p4-kit/config.yaml` or `<project>/.claude/p4-kit.yaml` left over from pre-0.9.2 releases | `script` primitive | `scripts/cleanup_legacy_config.py:cleanup` deletes the file and prunes the now-empty parent dir; silent no-op when nothing is present |

### Manual

None. The plugin doesn't ask for P4PORT/P4USER — when a `p4` command run by `prepare_review.py` can't reach the server, the native p4 error surfaces verbatim and the user resolves it through standard p4 mechanisms (`p4 set`, `p4 login`, etc.).

## Manifest (`bootstrap.json`)

Declarative tool + venv checks plus the legacy cleanup script:

```json
{
  "tools": [
    { "name": "p4", "install": { "macos": "brew install --cask perforce", "windows": "manual", "ubuntu": "manual" } },
    { "name": "uv", "install": { "macos": "...", "windows": "...", "ubuntu": "..." } },
    { "name": "claude", "install": { "macos": "manual", "windows": "manual", "ubuntu": "manual" } }
  ],
  "venv": {
    "check_imports": ["bootstrap_lib.code_review.chunking", "bootstrap_lib.code_review.claude_mds"]
  },
  "script": {
    "path": "scripts/cleanup_legacy_config.py",
    "entry_point": "cleanup"
  }
}
```

The matching `pyproject.toml` declares the dependency that the venv check verifies:

```toml
[project]
name = "p4-kit"
version = "0.10.0"
requires-python = ">=3.12,!=3.14.*"
dependencies = [
    "bootstrap @ git+https://github.com/kitaekatt/plugins-kit.git#subdirectory=plugins/bootstrap",
]
```

The two halves are paired: `pyproject.toml` drives what `uv sync` installs; `check_imports` drives what the engine treats as "installed correctly." Either alone is a silent-failure trap.

## Library Usage

| Source | Operation | Primitive |
|--------|-----------|-----------|
| Manifest | Verify `p4`, `uv`, `claude` installed | `check_tool()` |
| Manifest | Create / repair plugin venv from `pyproject.toml` and verify `bootstrap_lib.code_review.*` imports | `venv_check` |
| Manifest | Remove legacy per-project p4-kit config files | `script` |
| Runtime (in `prepare_review.py`) | Repair `PATH` so `subprocess.run(["p4", ...])` sees the binary on Windows | `bootstrap_lib.path_repair.repair_path` |
| Runtime (in `prepare_review.py`) | Partition the p4 diff into ≤1 MB chunks at directory boundaries; write per-chunk `.diff` fragments and an index entry | `bootstrap_lib.code_review.chunking.partition_sections_into_chunks` + `write_chunks` |
| Runtime (in `prepare_review.py`) | Walk parents of each changed file collecting `CLAUDE.md`; parse `**Submit gate:**` blocks; match scope paths against the CL's files | `bootstrap_lib.code_review.claude_mds.collect_claude_mds` + `collect_submit_gates` |
| Runtime (in `prepare_review.py`) | Detect a machine-generated file from its content and hold it out of the chunks entirely -- the review target is the GENERATOR, not its output -- surfacing it under `machine_emitted_files` | `bootstrap_lib.code_review.machine_emitted.detect_machine_emitted` |

The bottom four runtime rows are the new shape from 0.10.0. Before that, the chunking and CLAUDE.md walks were inlined in `prepare_review.py`; lifting them into `bootstrap_lib` let `git-kit`'s sibling `git-code-review` skill reuse the same machinery without copying ~500 lines.

## Observations

- **Two roles for bootstrap.** The session-start role (tool checks + venv install + cleanup) and the runtime-library role (`from bootstrap_lib... import ...` inside `prepare_review.py`) operate on different timelines but share one source. Bumping `bootstrap`'s Python package version (in `pyproject.toml`) is what makes consumers' `uv sync` reinstall — the plugin-manifest version bump alone won't trigger that, just like the bootstrap-vs-update06 relationship.
- **VCS-neutral shared lib, vendor-specific adapter.** The shared primitives in `bootstrap_lib.code_review` take opaque `DiffSection {identifier, text}` records. `prepare_review.py`'s `_p4_diff_to_sections` is a ~10-line adapter that re-shapes the output of `split_diff_sections` (the p4-specific `==== //depot/path#rev ====` parser) into that neutral shape. The same pattern in `git-kit`'s `prepare_review.py` does the equivalent for `diff --git a/... b/...` headers.
- **Diff chunking is what made the venv reintroduction earn its keep.** Before chunking, `prepare_review.py` was stdlib-only and the plugin shipped without a venv. Chunking was added inline in 0.9.3 (still stdlib-only) to keep large CLs reviewable; in 0.10.0 the chunking code was lifted into `bootstrap_lib` for sharing with `git-kit`, which forced the venv reintroduction. The dependency growth was downstream of a real reuse, not premature.
- **Runtime fan-out matches diff size.** The `p4-code-review` skill launches `R × K` reviewer subagents in one parallel message — R = reviewers in the selected profile (2 for `data_only`, 3 for `code`), K = number of chunks. Each subagent does one `Read` of one chunk path. A 1.4 MB CL fans out to ~12-18 parallel agents instead of asking each reviewer to ingest the whole diff (which fails outright — the Read tool refuses files past some unpublished threshold).
- **Minimal persistent state beyond the venv.** No per-machine config file, no per-project config file. Review bundles are written to `~/.claude/plugins/data/plugins-kit/p4-kit/reviews/<CL>/` (chunks + a bundle.json index) and overwritten on every invocation; they're cached artifacts of one review run, not persistent state.
- **Vendored copy of `path_repair.py` retired.** Before 0.10.0, `plugins/p4-kit/lib/path_repair.py` was a hand-synced copy of `bootstrap_lib.path_repair`. The test `tests/bootstrap/test_path_repair.py::TestVendoredCopiesInSync::test_vendored_copies_match_canon` enforced byte-identical content. With the venv in place, `prepare_review.py` imports the canonical version directly and the vendored copy is gone; the test now only guards the `unreal-kit` copy.
- **Skill named `p4-code-review`, not `local-code-review`.** Renamed in 0.10.0 for parity with the new `git-code-review` skill. The old name persists in historical changelog/phase-label references (e.g. `Phase Y4 = local-code-review conversion`) but every present-tense reference in the codebase points at `p4-code-review`.
- **History.** Earlier iterations (pre-0.7.0) had a `code-review-research` git dep and a `run-review.py` orchestrator that called external LLMs (codex/gemini/openrouter). All removed in 0.7.0; the venv was removed at the same time once the script went stdlib-only. The `project_config` autodetect that wrote `<project>/.local-data/p4-kit/config.yaml` (and the pre-migration `<project>/.claude/p4-kit.yaml`) was removed in 0.9.2 — nothing ever consumed those files and they polluted every cwd Claude was launched in, including ephemeral eval tmp dirs. The venv came back in 0.10.0 for the shared-lib refactor.
