"""Bootstrap script: contribute task-kit's cell to claude-ui-kit's statusline segment API.

Copies the synced ``statusline/segment.sh`` to ``<claude-ui-kit data dir>/segments/99-task.sh``. The ``99-`` prefix
sorts after every other segment, so the cell renders at the far right. Idempotent: no write when content is equal.
"""

from pathlib import Path

SEGMENT_NAME = "99-task.sh"
OWNER_MARKER = b"task-kit statusline cell for claude-ui-kit"
CONFIG_PATH = Path.home() / ".claude" / "task.local.yaml"


def cell_enabled(path: Path | None = None) -> bool:
    """``statusline.cell`` in ``task.local.yaml`` (bool, default true). Raises ValueError on a bad value."""
    path = Path(path) if path is not None else CONFIG_PATH
    if not path.is_file():
        return True
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return True
    if not isinstance(data, dict):
        raise ValueError(f"{path}: the file must be a mapping")
    section = data.get("statusline")
    if section is None:
        return True
    if not isinstance(section, dict):
        raise ValueError(f"{path}: statusline must be a mapping")
    unknown = sorted(str(k) for k in set(section) - {"cell"})
    if unknown:
        raise ValueError(f"{path}: unknown statusline keys {unknown}; known keys: ['cell']")
    cell = section.get("cell", True)
    if not isinstance(cell, bool):
        raise ValueError(f"{path}: statusline.cell must be true or false")
    return cell


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
    target = uikit / "segments" / SEGMENT_NAME
    try:
        enabled = cell_enabled()
    except Exception as exc:  # unreadable or invalid config: report, change nothing
        ctx.log(f"task-kit statusline: FAILED - bad statusline config: {exc}")
        return
    if not enabled:
        try:
            if target.is_file() and OWNER_MARKER in target.read_bytes():
                target.unlink()
                ctx.log(f"task-kit statusline: cell disabled; removed segment {target.as_posix()}")
            else:
                ctx.log_ok("task-kit statusline: cell disabled; no owned segment installed")
        except OSError as exc:
            ctx.log(f"task-kit statusline: FAILED - cannot remove segment: {exc}")
        return
    content = src.read_bytes()
    try:
        if target.is_file() and target.read_bytes() == content:
            ctx.log_ok("task-kit statusline: segment installed (no-op)")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        ctx.log(f"task-kit statusline: installed segment at {target.as_posix()}")
    except OSError as exc:
        ctx.log(f"task-kit statusline: FAILED - cannot write segment: {exc}")
