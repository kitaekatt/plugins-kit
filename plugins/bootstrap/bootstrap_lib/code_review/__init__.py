"""Shared code-review primitives consumed by p4-kit and git-kit.

Pure stdlib; no Perforce or Git knowledge here. VCS adapters (e.g.
p4-kit/scripts/prepare_review.py, git-kit/scripts/prepare_review.py)
produce a raw diff text + a per-file action map, then hand off to the
helpers in this package for chunking, CLAUDE.md collection, submit-gate
matching, and bundle persistence.

The interface those front-halves implement is written down (as documentation,
not an enforced base class) in ``vcs_adapter.VcsAdapter``.
"""
