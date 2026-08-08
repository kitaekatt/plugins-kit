#!/usr/bin/env python3
"""Block commits that change the orchestrate skill's DECISION half without a
corresponding change to the principles it is derived from.

The invariant
-------------
`plugins/awesome-kit/skills/orchestrate/defaults/orchestration.yaml` has two
halves. The DECISION half (keys listed in DECISION_KEYS) is DERIVED from the
design documents under `plugins/awesome-kit/skills/orchestrate/references/` --
tier-principles.md and lexicon.md. Authorship is ONE-WAY: change a principle,
THEN re-derive the data. Never edit the derived tree and back-fill a principle
to match it.

Nothing enforced that rule; it was a comment in the YAML header. This script
makes a violation visible at commit time, in two steps:

1. DRIFT. A fingerprint of the decision half is committed at
   plugins/awesome-kit/skills/orchestrate/references/decision-fingerprint.txt.
   If the YAML's decision half no longer matches it, the commit is blocked
   until the fingerprint is regenerated -- which is the moment the author is
   forced to notice they are changing derived data.

2. ONE-WAY AUTHORSHIP. Regenerating the fingerprint alone would make step 1
   a rubber stamp, so a staged diff that MOVES the baseline must also stage a
   change under plugins/awesome-kit/skills/orchestrate/references/ other than
   the fingerprint file itself. The gate keys on the fingerprint rather than on orchestration.yaml,
   so an edit to the machine half of that file -- which is derived from nothing
   -- never trips it.

What is compared, and against what
----------------------------------
A pre-commit check must judge what is BEING COMMITTED, not what happens to be
sitting in the working tree. So when anything is staged and we are inside a git
repo, step 1 reads BOTH the YAML and the baseline out of the INDEX
(`git show :<path>`) and compares those. Otherwise -- nothing staged, or no git
at all -- it falls back to the working tree, which is what a bare manual
invocation and `--update` want.

That distinction is load-bearing. Comparing working tree against working tree
let this sequence through: edit the decision half, run `--update` so the tree is
self-consistent again, then stage ONLY the YAML. The drift gate saw a tree that
agreed with itself, and the authorship gate never fired because the fingerprint
file was not in the index. Read from the index instead and the staged YAML
mismatches the unstaged baseline, which is the correct answer.

Git errors FAIL CLOSED. "Not a git repo" (no .git) is a legitimate, silent skip
of the authorship gate -- other workspaces here are Perforce, and git-scoped
checks degrade to advisory outside git -- but git being present and FAILING is
reported as a problem, because an un-evaluated gate must never be assumed
satisfied.

What this does NOT catch
------------------------
Honestly stated, because overselling it is how a weak check gets trusted like
a strong one:

- It catches "changed WITHOUT a principles change". It does NOT check that the
  data AGREES with the principles. A one-character edit to tier-principles.md
  satisfies it. Deriving the tree from the principles at build time would be
  the strong guarantee; that was considered and deferred, because the
  principles are deliberately prose-with-rationale so a human can audit them.
- The fingerprint is taken over the YAML SUBTREE, not the rendered output, so
  a change to `orchestration_guidance.py` that alters the rendered tree
  without touching the YAML is invisible here. That is a deliberate trade:
  the rendered output is MACHINE-DEPENDENT (the Codex ladder renders only
  where `codex` is on PATH, and the backend block disappears with it), so a
  render-derived fingerprint would fail spuriously on half the fleet. Hashing
  the subtree is machine-independent by construction.
- It says nothing about the MACHINE half (`backends`, `capacity`), which is
  not derived from anything and is expected to differ per machine.

Escape hatch:  PLUGINS_KIT_SKIP_BUMP_CHECK=1 git commit ...   (or --no-verify)

Enforced at pre-commit (chained from scripts/pre-commit-version-check.sh) as
well as under test, following scripts/check_bootstrap_dependency.py: this
repo's history shows suite-only invariants lose.

Usage:
    check_orchestration_drift.py            # check; exit 1 on violation
    check_orchestration_drift.py --update   # rewrite the fingerprint baseline
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, List, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

# Repo-relative, so every message and every stored path stays portable.
POLICY_REL = "plugins/awesome-kit/skills/orchestrate/defaults/orchestration.yaml"
PRINCIPLES_DIR_REL = "plugins/awesome-kit/skills/orchestrate/references"
FINGERPRINT_REL = f"{PRINCIPLES_DIR_REL}/decision-fingerprint.txt"

# The principles dir doubles as the orchestrate skill's general references/
# directory (codex-dispatch.md, configuration.md, why-delegate.md live there
# too, alongside the design docs the decision half is derived from). The
# one-way-authorship gate must key on the actual source documents, not on
# "anything under this directory" -- otherwise an edit to an unrelated
# reference would count as a principles change and silently weaken the gate.
PRINCIPLE_FILENAMES = ("tier-principles.md", "lexicon.md")
PRINCIPLE_FILES_REL = tuple(f"{PRINCIPLES_DIR_REL}/{name}" for name in PRINCIPLE_FILENAMES)

UPDATE_COMMAND = "uv run python scripts/check_orchestration_drift.py --update"

# The DECISION half: the keys derived from the principles. The MACHINE half
# (`backends`, `capacity`) and bookkeeping keys (`schema_version`,
# `default_backend`) are deliberately excluded -- they are what a given machine
# has, not what the principles say, and a machine-half edit must never trip
# this check.
DECISION_KEYS = (
    "resolution",
    "lexicon",
    "shape",
    "backend",
    "ladders",
    "agent_types",
    "effort",
    "announce",
)

_FINGERPRINT_PREFIX = "sha256:"

_HEADER = f"""\
# Fingerprint of the DECISION half of
# {POLICY_REL}
#
# GENERATED -- do not hand-edit. Regenerate with:
#   {UPDATE_COMMAND}
#
# Why it exists: the decision half is DERIVED from the principles in this
# directory, one-way (change a principle, then re-derive). This baseline is
# what scripts/check_orchestration_drift.py compares against so that a
# derived-data edit cannot pass unnoticed. See that script's header for what
# the check does NOT guarantee.
"""


# --------------------------------------------------------------------------
# Fingerprint
# --------------------------------------------------------------------------


def _canonical(value: Any) -> Any:
    """Recursively normalize a parsed-YAML value for hashing.

    String scalars are whitespace-folded because the decision half is written
    as YAML block scalars: re-wrapping a `>-` paragraph changes the bytes and
    changes nothing else (the renderer folds them anyway), and a check that
    fires on line re-wrapping would be routed around within a week.

    List ORDER is preserved -- the policy resolves by ordered elimination, so
    reordering rungs or gates is a real policy change.
    """
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    if isinstance(value, str):
        return " ".join(value.split())
    return value


def decision_half(policy: dict) -> dict:
    """The decision-half subtree, in DECISION_KEYS order. Absent keys are omitted."""
    return {key: policy[key] for key in DECISION_KEYS if key in policy}


def parse_policy(text: str, label: str) -> dict:
    """Parse policy YAML from text. `label` names the source in any error.

    Malformed YAML is reported as a ValueError naming `label` and the
    underlying parser message, rather than letting yaml.YAMLError escape as
    an uncaught traceback -- callers turn this into a printable problem
    block, same as every other failure mode in check().
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"{label}: could not be parsed as YAML:\n{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{label}: top level must be a mapping")
    return data


def load_policy(policy_path: Path, label: Optional[str] = None) -> dict:
    """Parse the policy at `policy_path`.

    `label` names the source in any error. Callers pass the repo-relative
    constant so a reported problem reads the same whether it came from the
    working tree or the index; the absolute path is only the fallback for a
    caller that has nothing better to say.
    """
    return parse_policy(
        policy_path.read_text(encoding="utf-8"), label or str(policy_path)
    )


def fingerprint(policy: dict) -> str:
    """Machine-independent fingerprint of the decision half."""
    payload = json.dumps(
        _canonical(decision_half(policy)),
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_baseline(text: Optional[str]) -> Optional[str]:
    """The stored hash inside a fingerprint file's text, or None when there is
    no text at all or it carries no hash line."""
    if text is None:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(_FINGERPRINT_PREFIX):
            return line[len(_FINGERPRINT_PREFIX):].strip()
        return line
    return None


def read_baseline(fingerprint_path: Path) -> Optional[str]:
    """The stored hash, or None when the file is absent or carries no hash."""
    if not fingerprint_path.is_file():
        return None
    return parse_baseline(fingerprint_path.read_text(encoding="utf-8"))


def write_baseline(fingerprint_path: Path, digest: str) -> None:
    fingerprint_path.parent.mkdir(parents=True, exist_ok=True)
    fingerprint_path.write_text(
        f"{_HEADER}\n{_FINGERPRINT_PREFIX}{digest}\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------
# Staged-diff gate
# --------------------------------------------------------------------------


GIT_FAILED_PROBLEM = (
    "git is present but did not answer `git diff --cached --name-only`.\n"
    "The one-way-authorship gate could NOT be evaluated, so it is reported\n"
    "rather than assumed satisfied. Fix the git failure and re-run, or bypass\n"
    "deliberately with PLUGINS_KIT_SKIP_BUMP_CHECK=1."
)


def is_git_repo(repo_root: Path) -> bool:
    """True when repo_root is (or is inside) a git working tree.

    Presence of `.git` is enough: it is a directory in a normal clone and a
    file in a worktree/submodule. Outside git the authorship gate simply does
    not apply -- other workspaces here are Perforce -- so it is skipped
    silently, and only the drift gate runs.
    """
    return (repo_root / ".git").exists()


def staged_paths(repo_root: Path) -> Optional[List[str]]:
    """Repo-relative paths in the index (forward slashes).

    Three distinguishable states, because a caller must not confuse "nothing
    is staged" with "git did not answer":

      []    nothing staged -- or not a git repo at all, where the gate does
            not apply and is skipped silently.
      [...] git answered; these paths are staged.
      None  git is present but FAILED (raised, or exited non-zero). The gate
            cannot be evaluated. Fails CLOSED: check() turns this into a
            problem instead of an empty, gate-disabling list.
    """
    if not is_git_repo(repo_root):
        return []
    try:
        proc = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout.decode("utf-8", "replace")
    return [line.strip().replace("\\", "/") for line in text.splitlines() if line.strip()]


def index_blob(repo_root: Path, rel_path: str) -> Optional[str]:
    """The content of `rel_path` AS STAGED, or None when there is no such entry.

    `git show :<path>` returns the index entry, which for an unmodified tracked
    file is still its HEAD content -- exactly what a commit would carry. None
    means the path has no index entry at all: staged for deletion, or untracked.
    Callers treat that as "missing", which fails closed; it never escapes as an
    exception and never reads as a pass.
    """
    try:
        proc = subprocess.run(
            ["git", "show", f":{rel_path}"],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def baseline_changed(paths: Iterable[str]) -> bool:
    """Did the change move the decision-half baseline?

    Deliberately keyed on the FINGERPRINT file, not on orchestration.yaml. The
    YAML also carries the MACHINE half (`backends`, `capacity`), which is edited
    routinely and is derived from nothing; gating on the YAML path would fire on
    every machine-half edit, and a check that cries wolf gets disabled. The
    baseline moves only when the decision half actually changed -- and a decision
    edit that does NOT move it is caught by the drift comparison instead.
    """
    return FINGERPRINT_REL in set(paths)


def touches_principles(paths: Iterable[str]) -> bool:
    """A change to one of the actual principle documents (PRINCIPLE_FILES_REL).

    Keyed on the specific source files rather than "anything under
    PRINCIPLES_DIR_REL": that directory is also the orchestrate skill's
    general references/ dir, so a directory-wide check would treat an edit to
    an unrelated reference (codex-dispatch.md, configuration.md,
    why-delegate.md) as a principles change and let it satisfy the gate. The
    fingerprint file is naturally excluded since it is not in this tuple --
    regenerating it must never count as a principles change on its own.
    """
    principle_paths = set(PRINCIPLE_FILES_REL)
    return any(p in principle_paths for p in paths)


# --------------------------------------------------------------------------
# The check
# --------------------------------------------------------------------------


def check(repo_root: Path, staged: Optional[List[str]] = None) -> List[str]:
    """Problems, as printable blocks. Empty list means the invariant holds.

    `staged` is the injection seam: pass a list to drive the gates directly,
    or leave it None to ask git. A git failure is reported, not swallowed.
    """
    policy_path = repo_root / POLICY_REL
    fingerprint_path = repo_root / FINGERPRINT_REL
    problems: List[str] = []

    if staged is None:
        staged = staged_paths(repo_root)
        if staged is None:
            problems.append(GIT_FAILED_PROBLEM)
            staged = []

    # Something is being committed: judge THAT, not the working tree. See the
    # module header -- comparing the tree against itself is what let a
    # partially-staged decision-half edit through.
    from_index = bool(staged) and is_git_repo(repo_root)

    if from_index:
        policy_text = index_blob(repo_root, POLICY_REL)
        if policy_text is None:
            problems.append(
                f"{POLICY_REL} has no entry in the index (staged for deletion, or\n"
                "untracked) -- the decision half of what is being committed cannot be\n"
                "fingerprinted. Restore or stage the file."
            )
            return problems
        try:
            current = fingerprint(parse_policy(policy_text, f"{POLICY_REL} (index)"))
        except ValueError as exc:
            problems.append(str(exc))
            return problems
        baseline = parse_baseline(index_blob(repo_root, FINGERPRINT_REL))
    else:
        if not policy_path.is_file():
            return [f"{POLICY_REL} is missing -- cannot fingerprint the decision half."]
        try:
            current = fingerprint(load_policy(policy_path, POLICY_REL))
        except ValueError as exc:
            problems.append(str(exc))
            return problems
        baseline = read_baseline(fingerprint_path)

    if baseline is None:
        problems.append(
            f"No decision-half baseline at {FINGERPRINT_REL}.\n"
            f"  Create it with:  {UPDATE_COMMAND}"
        )
    elif baseline != current:
        problems.append(
            "The DECISION half of the orchestration policy has changed, but its\n"
            f"baseline has not:\n"
            f"  policy:   {POLICY_REL}\n"
            f"  baseline: {FINGERPRINT_REL}\n"
            f"    expected {baseline}\n"
            f"    computed {current}\n"
            "\n"
            "That half is DERIVED from the principles in "
            f"{PRINCIPLES_DIR_REL}/, one-way:\n"
            "change a principle, THEN re-derive the data. If you did that, record it:\n"
            f"  {UPDATE_COMMAND}\n"
            "and stage the result together with the principles change."
            + (
                "\n\nBoth halves were read from the INDEX -- this is what the commit\n"
                "would carry. A regenerated baseline that is not staged does not\n"
                "count; stage it too."
                if from_index
                else ""
            )
        )

    if baseline_changed(staged) and not touches_principles(staged):
        problems.append(
            "The decision-half baseline moved with no staged change under\n"
            f"{PRINCIPLES_DIR_REL}/:\n"
            f"  {FINGERPRINT_REL}\n"
            "\n"
            "Authorship is ONE-WAY -- change a principle "
            f"({PRINCIPLES_DIR_REL}/tier-principles.md\n"
            "or lexicon.md), then re-derive. Editing the tree and back-filling a\n"
            "principle later is the failure this check exists to make visible.\n"
            f"(Regenerating {Path(FINGERPRINT_REL).name} does not count as a "
            "principles change.)"
        )

    return problems


def update(repo_root: Path) -> str:
    """Rewrite the baseline from the current policy. Returns the new digest."""
    digest = fingerprint(load_policy(repo_root / POLICY_REL, POLICY_REL))
    write_baseline(repo_root / FINGERPRINT_REL, digest)
    return digest


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--update",
        action="store_true",
        help="Rewrite the fingerprint baseline from the current policy",
    )
    args = parser.parse_args(argv)

    if args.update:
        digest = update(REPO_ROOT)
        print(f"{FINGERPRINT_REL}: {_FINGERPRINT_PREFIX}{digest}")
        print(
            "Stage it together with the principles change it records "
            f"({PRINCIPLES_DIR_REL}/)."
        )
        return 0

    if os.environ.get("PLUGINS_KIT_SKIP_BUMP_CHECK") == "1":
        return 0

    problems = check(REPO_ROOT)
    if not problems:
        return 0
    print("orchestration decision-half drift:", file=sys.stderr)
    for problem in problems:
        print("", file=sys.stderr)
        for line in problem.splitlines():
            print(f"  {line}", file=sys.stderr)
    print(
        "\n(Intentional dev commit? PLUGINS_KIT_SKIP_BUMP_CHECK=1 git commit ...)",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
