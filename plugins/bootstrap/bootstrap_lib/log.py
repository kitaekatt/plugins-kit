"""File-based logging for bootstrap operations."""

import os
from datetime import datetime, timezone
from typing import List, Optional


LOG_FILENAME = "bootstrap.log"
MAX_LOG_LINES = 500


def write_log_block(
    data_dir: str,
    header_label: str,
    entries: List[str],
    start_time: Optional[datetime] = None,
) -> None:
    """Write a header + timestamped log entries + footer as an atomic block.

    Only call this when entries is non-empty. Writes a section header
    followed by the timestamped entries, then trims the log if needed.
    When start_time is provided, the header uses that timestamp and a
    footer with elapsed time is appended.

    Args:
        data_dir: Directory containing the log file
        header_label: Label for the section header (e.g. "Shell", "Engine")
        entries: List of log messages to append (must be non-empty)
        start_time: When bootstrap started (UTC). Used for header timestamp
            and to compute elapsed time for the footer.
    """
    if not entries:
        return

    os.makedirs(data_dir, exist_ok=True)
    log_file = os.path.join(data_dir, LOG_FILENAME)

    if start_time is not None:
        timestamp = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [f"--- {header_label} {timestamp} ---\n"]
    for entry in entries:
        lines.append(f"{entry}\n")
    if start_time is not None:
        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
        lines.append(f"--- {header_label} done in {elapsed:.1f}s ---\n")

    # One write() call in append mode: on POSIX, O_APPEND makes a single
    # write land contiguously even when a concurrent session appends too,
    # so blocks don't interleave line-by-line (B20).
    with open(log_file, "a") as f:
        f.write("".join(lines))

    _trim_log(log_file)


def _trim_log(log_file: str) -> None:
    """Keep roughly the last MAX_LOG_LINES lines, trimming at a block boundary.

    The cut point advances to the next block header (``--- label ... ---``)
    so a trim never decapitates a block — a headless block has no timestamp
    and would be mis-attributed by the display reader (B20). Note the
    read-modify-write here can still drop a block another session appends
    between the read and the write; the trim only runs after this session's
    own append, so the loss window is small and the cost is a missing
    historical block, never a corrupt current one.
    """
    try:
        with open(log_file, "r") as f:
            all_lines = f.readlines()
        if len(all_lines) <= MAX_LOG_LINES:
            return
        cut = len(all_lines) - MAX_LOG_LINES
        # Advance the cut to the next block header so the kept tail starts
        # clean. Worst case (no header found) keeps the plain tail as before.
        for i in range(cut, len(all_lines)):
            line = all_lines[i]
            # Headers, not footers ("--- label done in X.Xs ---"): the tail
            # must start at a timestamped header for the display reader.
            if line.startswith("--- ") and " done in " not in line:
                cut = i
                break
        with open(log_file, "w") as f:
            f.writelines(all_lines[cut:])
    except (FileNotFoundError, PermissionError):
        pass
