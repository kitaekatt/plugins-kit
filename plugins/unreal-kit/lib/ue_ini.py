"""
UE .ini file read/write utilities.

Used by ue_runner.py. Pure stdlib, no external dependencies.
"""

import os
import tempfile
from pathlib import Path


def read_ini_bool(ini_path: Path, section: str, key: str) -> bool | None:
    """Read a boolean value from a UE .ini file. Returns None if not found."""
    if not ini_path.is_file():
        return None
    in_section = False
    with open(ini_path, "r") as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("["):
                in_section = stripped == section
                continue
            if in_section and "=" in stripped:
                k, _, v = stripped.partition("=")
                if k.strip() == key:
                    return v.strip().lower() in ("true", "1")
    return None


def write_ini_setting(ini_path: Path, section: str, key: str, value: str):
    """Write a setting while preserving unrelated sections and old bytes on failure."""
    ini_path = Path(ini_path)
    lines = []
    if ini_path.is_file():
        with open(ini_path, "r") as f:
            lines = f.readlines()

    # Try to find and update existing key in the target section
    in_section = False
    section_idx = None
    next_section_idx = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            in_section = stripped == section
            if in_section:
                section_idx = i
            elif section_idx is not None and next_section_idx == len(lines):
                next_section_idx = i
            continue
        if in_section and "=" in stripped:
            k, _, _ = stripped.partition("=")
            if k.strip() == key:
                if line.endswith("\r\n"):
                    newline = "\r\n"
                elif line.endswith("\n"):
                    newline = "\n"
                else:
                    newline = ""
                prefix, _, old_value = line.partition("=")
                suffix = ""
                for marker in (";", "#"):
                    marker_index = old_value.find(marker)
                    if marker_index >= 0 and (marker_index == 0 or old_value[marker_index - 1].isspace()):
                        suffix = old_value[marker_index:].rstrip("\r\n")
                        break
                leading = old_value[: len(old_value) - len(old_value.lstrip())]
                lines[i] = f"{prefix}={leading}{value}"
                if suffix:
                    lines[i] += f" {suffix}"
                lines[i] += newline
                return _write_ini_lines(ini_path, lines)

    # Key not found -- append to the end of the target section, before the next
    # section. This leaves comments and all unrelated sections in place.
    if section_idx is not None:
        insert_at = next_section_idx
        if insert_at and not lines[insert_at - 1].endswith(("\n", "\r")):
            lines[insert_at - 1] += "\n"
        lines.insert(insert_at, f"{key}={value}\n")
    else:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        if lines:
            lines.append("\n")
        lines.append(f"{section}\n")
        lines.append(f"{key}={value}\n")
    return _write_ini_lines(ini_path, lines)


def _write_ini_lines(ini_path: Path, lines: list[str]) -> None:
    ini_path.parent.mkdir(parents=True, exist_ok=True)
    candidate: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=ini_path.parent,
            prefix=f".{ini_path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            candidate = stream.name
            stream.write("".join(lines))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(candidate, ini_path)
        candidate = None
    finally:
        if candidate:
            try:
                os.unlink(candidate)
            except OSError:
                pass
