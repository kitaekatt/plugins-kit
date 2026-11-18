"""Bootstrap script: contribute task-kit's cell to claude-ui-kit's statusline segment API.

Copies the synced ``statusline/segment.sh`` to ``<claude-ui-kit data dir>/segments/99-task.sh``. The ``99-`` prefix
sorts after every other segment, so the cell renders at the far right. Idempotent: no write when content is equal.
"""

from pathlib import Path

SEGMENT_NAME = "99-task.sh"


def install(ctx) -> None:
    data_dir = Path(ctx.data_dir)
    src = data_dir / "statusline" / "segment.sh"
    uikit = data_dir.parent / "claude-ui-kit"
    if not uikit.is_dir():
        ctx.log_ok("task-kit statusline: claude-ui-kit data dir absent; segment not installed")
        return
    if not src.is_file():
        ctx.log(f"task-kit statusline: FAILED - segment not synced at {src.as_posix()}")
        return
    content = src.read_bytes()
    target = uikit / "segments" / SEGMENT_NAME
    try:
        if target.is_file() and target.read_bytes() == content:
            ctx.log_ok("task-kit statusline: segment installed (no-op)")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        ctx.log(f"task-kit statusline: installed segment at {target.as_posix()}")
    except OSError as exc:
        ctx.log(f"task-kit statusline: FAILED - cannot write segment: {exc}")
