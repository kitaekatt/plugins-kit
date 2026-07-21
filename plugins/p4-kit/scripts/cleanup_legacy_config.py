#!/usr/bin/env python3
"""Remove legacy per-project p4-kit config files.

Earlier p4-kit releases wrote `<project>/.local-data/p4-kit/config.yaml` (and
the pre-migration `<project>/.claude/p4-kit.yaml`) on every session as part of
a `project_config` autodetect. Nothing actually consumed the file -- p4 itself
resolves P4PORT/P4USER from its own registry/P4CONFIG cascade -- so the writes
were dead weight that also polluted ephemeral tmp dirs Claude was launched in.

This script removes those files (and prunes the empty `.local-data/p4-kit/`
parent) from the current project dir. It is a no-op when nothing is present,
so it can stay in place for several releases until the long tail of legacy
files has been swept up; once the field is clean it can be deleted along with
its entry in bootstrap.json.
"""

from pathlib import Path


LEGACY_PATHS = (
    Path(".local-data") / "p4-kit" / "config.yaml",
    Path(".claude") / "p4-kit.yaml",
)


def cleanup(ctx) -> None:
    project_dir = getattr(ctx, "project_dir", None)
    if not project_dir:
        return

    root = Path(project_dir)
    removed = []

    for rel in LEGACY_PATHS:
        target = root / rel
        if target.is_file():
            try:
                target.unlink()
                removed.append(rel.as_posix())
            except OSError as e:
                ctx.log(f"legacy config: WARNING failed to remove {target}: {e}")
                continue

            parent = target.parent
            while parent != root and parent.is_dir():
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent

    if removed:
        ctx.log(f"legacy config: removed {', '.join(removed)} from {root.as_posix()}")
