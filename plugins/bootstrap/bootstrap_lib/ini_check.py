"""INI settings check and write for bootstrap manifests.

Reads/writes UE-style INI files. Logic adapted from
plugins/unreal-kit/skills/ue-python-api/lib/ue_ini.py.
"""

from pathlib import Path
from typing import NamedTuple, Optional


class IniCheckResult(NamedTuple):
    passed: bool
    file: str
    section: str
    key: str
    message: str


def check_ini_setting(
    file: str, section: str, key: str, expected_value: str
) -> IniCheckResult:
    """Check if an INI file has the expected value for a key.

    Args:
        file: Path to the INI file
        section: Section header including brackets, e.g. "[Section]"
        key: Setting key name
        expected_value: Expected value string

    Returns:
        IniCheckResult with pass/fail and descriptive message
    """
    ini_path = Path(file)

    if not ini_path.is_file():
        return IniCheckResult(
            passed=False, file=file, section=section, key=key,
            message="file does not exist",
        )

    in_section = False
    try:
        with open(ini_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("["):
                    in_section = stripped == section
                    continue
                if in_section and "=" in stripped:
                    k, _, v = stripped.partition("=")
                    if k.strip() == key:
                        if v.strip() == expected_value:
                            return IniCheckResult(
                                passed=True, file=file, section=section, key=key,
                                message=f"{key}={expected_value}",
                            )
                        return IniCheckResult(
                            passed=False, file=file, section=section, key=key,
                            message=f"expected {expected_value}, got {v.strip()}",
                        )
    except OSError as exc:
        return IniCheckResult(
            passed=False, file=file, section=section, key=key,
            message=f"failed to read file: {exc}",
        )

    return IniCheckResult(
        passed=False, file=file, section=section, key=key,
        message="key not found",
    )


def write_ini_setting(file: str, section: str, key: str, value: str) -> None:
    """Write a setting to an INI file, creating file/section if needed.

    Args:
        file: Path to the INI file
        section: Section header including brackets, e.g. "[Section]"
        key: Setting key name
        value: Value to write
    """
    ini_path = Path(file)
    lines: list[str] = []

    if ini_path.is_file():
        with open(ini_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

    # Try to find and update existing key in the target section
    in_section = False
    section_idx: Optional[int] = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            in_section = stripped == section
            if in_section:
                section_idx = i
            continue
        if in_section and "=" in stripped:
            k, _, _ = stripped.partition("=")
            if k.strip() == key:
                lines[i] = f"{key}={value}\n"
                ini_path.parent.mkdir(parents=True, exist_ok=True)
                with open(ini_path, "w", encoding="utf-8", errors="replace") as f:
                    f.writelines(lines)
                return

    # Key not found — append to section or create section
    if section_idx is not None:
        lines.insert(section_idx + 1, f"{key}={value}\n")
    else:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"{section}\n")
        lines.append(f"{key}={value}\n")

    ini_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ini_path, "w", encoding="utf-8", errors="replace") as f:
        f.writelines(lines)
