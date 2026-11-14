"""VCS-neutral helpers shared by git-kit's and p4-kit's prepare_review.py.

Both front-halves carried identical copies of these: the subprocess-timeout
environment read and the argv split. Neither knows Git or Perforce; the caller
passes the one kit-specific value (its timeout variable).

Pure stdlib. Two copies stay outside this module on purpose: each kit's
snapshot_review.py is stdlib-only and imports no bootstrap_lib
(tests/p4-kit/test_p4_snapshot_review.py::test_the_script_is_stdlib_only), and
p4-kit's shared library p4kit_vcs is imported by consumer venvs that need not
have bootstrap_lib.
"""

from __future__ import annotations

import os


def env_timeout_s(env_var: str, default: float) -> float:
    """Read a subprocess timeout (seconds) from the environment variable `env_var`.

    Falls back to `default` when the variable is unset, empty, or holds a
    value `float()` rejects.
    """
    raw = os.environ.get(env_var)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def parse_review_args(args: list[str]) -> tuple[list[str], list[str], bool]:
    """Split prepare_review argv into (positionals, claim_globs, review_machine_emitted).

    `--claim <glob>` (repeatable) and `--claim=<glob>` collect claim patterns;
    `--review-machine-emitted` turns OFF machine-emitted-artifact exclusion so
    those files are chunked and reviewed like any other; everything else (a
    git range or ref, `--staged`, `--working`, a p4 CL number) is a positional.
    Raises ValueError on a `--claim` with no value.
    """
    positionals: list[str] = []
    claim_globs: list[str] = []
    review_machine_emitted = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--review-machine-emitted":
            review_machine_emitted = True
            i += 1
        elif a == "--claim":
            if i + 1 >= len(args):
                raise ValueError("--claim requires a glob argument")
            claim_globs.append(args[i + 1])
            i += 2
        elif a.startswith("--claim="):
            claim_globs.append(a[len("--claim="):])
            i += 1
        else:
            positionals.append(a)
            i += 1
    return positionals, claim_globs, review_machine_emitted
