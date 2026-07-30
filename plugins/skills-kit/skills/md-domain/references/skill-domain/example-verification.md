# Verifying SKILL.md Examples

Every shell command shown verbatim in a `SKILL.md` must have been executed exactly as written, against a real environment, before the skill ships. Skills are read by both humans and Claude agents; both copy-paste examples literally. A broken example fails at runtime, not at review.

The cost of a broken example is silent: the failure happens later, in a session where the agent or user is mid-task, and the recovery cost is much higher than the upfront verification cost.

## Two failure modes that automated unit tests do not catch

These are real bugs that landed in shipped skills despite the underlying code being correct.

### 1. Argument-passing bugs in wrapper-script chains

A skill invoked an inner script via a wrapper:

```bash
"${CLAUDE_PLUGIN_ROOT}/skills/ue-python-api/bin/ue-runner.cmd" \
  "${CLAUDE_PLUGIN_ROOT}/skills/fix-up-redirectors/bin/apply_fixups.py" \
  --mode=delete-only
```

The wrapper (`ue-runner.cmd`) had its own `--mode {remote,commandlet}` argparse with no `parse_known_args` and no `--` separator support, so it consumed the trailing `--mode=delete-only` itself and rejected `delete-only` as an invalid choice. The inner script never received the flag.

Both the inner script's unit tests and the wrapper's unit tests passed in isolation. The composition broke.

**Verification rule:** every wrapper-chained example must be smoke-tested as a whole pipeline, not just by validating each stage in isolation. The interface between stages is part of what the example commits to.

### 2. Environment-resolution claims that depend on cwd

A skill documented `uv run python` as the canonical way to invoke its host-side scripts, with the explanation "it activates the right venv". The claim was correct when run from the plugin's own pyproject.toml directory, but the same skill explicitly instructed users to run from a *foreign* cwd (the user's project root) -- where there's no matching pyproject.toml and `uv run python` falls back to a bare interpreter, crashing with `ModuleNotFoundError`.

**Verification rule:** every Python-invocation example must be smoke-tested from the cwd the skill instructs users to be in. If the skill says "run from project root", run the example from project root before shipping.

## Pre-ship checklist

1. **Copy each example verbatim.** Do not adapt for testing. The literal text is what users and agents will use; verifying a "spiritually similar" command misses arg-passing bugs and quote-escaping mistakes.
2. **Match the documented cwd.** If the skill says "run from project root", `cd` there first.
3. **Use a fresh shell.** Inheriting the author's interactive shell state (an activated venv, exported env vars, an old PATH entry) hides reliance on environment configuration the user won't have.
4. **Watch the exit code.** `&& echo OK || echo FAIL` to make non-zero exits visible without scrolling.
5. **Skim stderr too.** Some failure modes are exit-0-with-empty-stdout (see `docs/p4-cli-gotchas.md` for an example) -- they fail silently unless the caller validates output content, not just rc.

## Related

- `docs/p4-cli-gotchas.md` -- catalog of silent failure modes when parsing `p4` CLI output, several of which only surfaced because someone ran an example verbatim.
- `plugins/unreal-kit/skills/ue-python-api/references/commandlet-safe-init.md` -- another class of "code passes in isolation, breaks in composition" bug.

Surfaced 2026-05-05 across two distinct unreal-kit fix-up-redirectors examples; both fixed in 0.9.3 / 0.9.4 respectively.
