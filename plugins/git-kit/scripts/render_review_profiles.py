#!/usr/bin/env python3
"""Render the resolved code-review profile configuration for git-kit.

Thin plugin-venv entry point: re-execs under git-kit's bootstrap-provisioned
venv, then delegates to the shared resolver in
bootstrap_lib.code_review.review_profiles, which owns resolution, merging,
and rendering. This script exists only so the git-code-review skill can
invoke a stable per-plugin path rather than reaching into bootstrap's tree
directly.

Usage:
    render_review_profiles.py --project-root <path> [--home <path>]

See bootstrap_lib.code_review.review_profiles for the full CLI contract
(this module's main() is a direct pass-through).

Stdout: the resolved profile table (plus provenance). Non-zero exit on a
configuration error.
"""

import sys

# Plugins define their own bootstrap-provisioned venv and must run under it
# preferentially. A bare `python` or `uv run` invocation lands in a different
# environment with no shared-libs .pth, so re-exec under the provisioned venv
# before importing bootstrap_lib below -- a no-op when already there. The guard
# is the vendored, stdlib-only bootstrap_guard next to this script; importing it
# can never itself trip the missing-bootstrap_lib failure.
from bootstrap_guard import reexec_under_plugin_venv  # noqa: E402

reexec_under_plugin_venv("git-kit")

try:
    from bootstrap_lib.path_repair import repair_path  # noqa: E402
    from bootstrap_lib.code_review.review_profiles import main  # noqa: E402
except ImportError:
    # bootstrap_lib is absent -> the bootstrap plugin never provisioned this
    # plugin's venv. Convert the raw ModuleNotFoundError traceback into an
    # actionable "install/enable plugins-kit:bootstrap" message and exit.
    from bootstrap_guard import require_bootstrap

    require_bootstrap(
        "git-kit", feature="code review", missing="bootstrap_lib", force=True
    )

repair_path()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
