"""OS and architecture detection for bootstrap operations."""

import platform
import sys
from typing import Optional, Tuple


class UnsupportedPlatformError(Exception):
    """The current platform cannot be mapped to a supported target.

    Today the sole trigger is a Linux host that is not Ubuntu. Bootstrap's
    Linux install commands are Ubuntu/apt-specific, so silently treating a
    non-Ubuntu distro as Ubuntu (the old behavior) would run the wrong
    commands. Fail fast with a descriptive error instead. This is a
    deliberate, user-ratified (2026-07-02) behavior change; see the
    bootstrap-env-refactor software-management-strategy (failure policy).
    """


def _read_os_release() -> Tuple[Optional[str], str]:
    """Read ``/etc/os-release`` for Linux distribution detection.

    Returns ``(lowercased_content_or_None, human_label)``. ``human_label``
    prefers ``PRETTY_NAME``, falls back to ``ID``, else "" -- it exists only
    to make the unsupported-distro error descriptive. ``None`` content means
    the file is missing or unreadable (no silent Ubuntu default).
    """
    try:
        with open("/etc/os-release") as f:
            raw = f.read()
    except (FileNotFoundError, PermissionError, OSError):
        return None, ""
    pretty = ""
    distro_id = ""
    for line in raw.splitlines():
        if line.startswith("PRETTY_NAME="):
            pretty = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("ID="):
            distro_id = line.split("=", 1)[1].strip().strip('"')
    return raw.lower(), (pretty or distro_id)


def detect_os() -> str:
    """Detect the current operating system.

    Returns one of: "macos", "windows", "ubuntu".

    Raises :class:`UnsupportedPlatformError` for a Linux host that is not
    Ubuntu. Ubuntu proper AND apt-based Ubuntu derivatives that declare
    "ubuntu" in ``/etc/os-release`` (ID or ID_LIKE) still resolve to "ubuntu"
    -- this includes Ubuntu-on-WSL, which reports ``ID=ubuntu`` and must keep
    working. A genuinely different distribution (fedora, arch, alpine, ...) or
    a host with no readable ``/etc/os-release`` fails fast rather than
    receiving Ubuntu/apt install commands that would be wrong for it.
    """
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    elif system == "windows":
        return "windows"
    elif system == "linux":
        content, label = _read_os_release()
        # Preserve the prior (non-regressing) positive match: any os-release
        # mentioning "ubuntu" anywhere -- ID=ubuntu (incl. WSL) or ID_LIKE=ubuntu
        # on apt-based derivatives -- is treated as ubuntu.
        if content is not None and "ubuntu" in content:
            return "ubuntu"
        detected = label or "unknown (no readable /etc/os-release)"
        raise UnsupportedPlatformError(
            f"Unsupported Linux distribution: detected {detected}. Bootstrap "
            "supports only Ubuntu among Linux distributions -- its Linux "
            "install commands are Ubuntu/apt-specific and would be wrong here. "
            "Run bootstrap on Ubuntu (native or WSL), or add first-class "
            "support for this distribution to bootstrap_lib/platform_detect.py."
        )
    else:
        return system


def detect_arch() -> str:
    """Detect the current CPU architecture.

    Returns one of: "amd64" (x86_64), "arm64" (aarch64), or platform.machine()
    lowercased for less-common values. Normalizes Intel/Apple/Linux naming
    differences so download-recipe keys can be a single canonical token.
    """
    m = platform.machine().lower()
    if m in ("amd64", "x86_64", "x64"):
        return "amd64"
    if m in ("arm64", "aarch64"):
        return "arm64"
    return m


def detect_os_arch() -> str:
    """Convenience: 'macos-arm64', 'windows-amd64', etc."""
    return f"{detect_os()}-{detect_arch()}"
