"""Tests for bootstrap lib/platform_detect.py."""

from unittest.mock import mock_open, patch

import pytest

from bootstrap_lib import platform_detect
from bootstrap_lib.platform_detect import UnsupportedPlatformError, detect_os


class TestDetectOs:
    def test_returns_known_os(self):
        result = detect_os()
        assert result in ("macos", "windows", "ubuntu")

    def test_macos(self):
        with patch("platform.system", return_value="Darwin"):
            assert detect_os() == "macos"

    def test_windows(self):
        with patch("platform.system", return_value="Windows"):
            assert detect_os() == "windows"


# --------------------------------------------------------------------------- #
# Non-Ubuntu Linux fail-fast (ratified 2026-07-02 behavior change).
# --------------------------------------------------------------------------- #

_UBUNTU_OS_RELEASE = (
    'NAME="Ubuntu"\nID=ubuntu\nVERSION_ID="22.04"\n'
    'PRETTY_NAME="Ubuntu 22.04.3 LTS"\n'
)
# Ubuntu-on-WSL reports ID=ubuntu; must keep resolving to "ubuntu".
_WSL_UBUNTU_OS_RELEASE = (
    'NAME="Ubuntu"\nID=ubuntu\nPRETTY_NAME="Ubuntu 22.04 LTS"\n'
    'VERSION="22.04.3 LTS (Jammy Jellyfish)"\n'
)
# apt-based derivative: ID differs but ID_LIKE=ubuntu -> non-regressing match.
_POP_OS_RELEASE = (
    'NAME="Pop!_OS"\nID=pop\nID_LIKE="ubuntu debian"\n'
    'PRETTY_NAME="Pop!_OS 22.04 LTS"\n'
)
_FEDORA_OS_RELEASE = (
    'NAME="Fedora Linux"\nID=fedora\nVERSION_ID=39\n'
    'PRETTY_NAME="Fedora Linux 39 (Workstation Edition)"\n'
)
_ARCH_OS_RELEASE = 'NAME="Arch Linux"\nID=arch\nPRETTY_NAME="Arch Linux"\n'


def _linux_with_os_release(content):
    """Context wiring: platform.system() -> Linux and open() -> content."""
    return (
        patch("platform.system", return_value="Linux"),
        patch("builtins.open", mock_open(read_data=content)),
    )


class TestDetectOsLinuxFailFast:
    def test_ubuntu_passes(self):
        sysp, openp = _linux_with_os_release(_UBUNTU_OS_RELEASE)
        with sysp, openp:
            assert detect_os() == "ubuntu"

    def test_wsl_ubuntu_passes(self):
        sysp, openp = _linux_with_os_release(_WSL_UBUNTU_OS_RELEASE)
        with sysp, openp:
            assert detect_os() == "ubuntu"

    def test_ubuntu_derivative_id_like_passes(self):
        # Non-regression: apt-based derivative declaring ID_LIKE=ubuntu still
        # maps to ubuntu (it received ubuntu commands before this change too).
        sysp, openp = _linux_with_os_release(_POP_OS_RELEASE)
        with sysp, openp:
            assert detect_os() == "ubuntu"

    def test_fedora_fails_descriptively(self):
        sysp, openp = _linux_with_os_release(_FEDORA_OS_RELEASE)
        with sysp, openp:
            with pytest.raises(UnsupportedPlatformError) as exc:
                detect_os()
        msg = str(exc.value)
        assert "Fedora Linux 39 (Workstation Edition)" in msg  # what was detected
        assert "Ubuntu" in msg                                 # why unsupported
        assert "platform_detect.py" in msg                     # what to do

    def test_arch_fails_descriptively(self):
        sysp, openp = _linux_with_os_release(_ARCH_OS_RELEASE)
        with sysp, openp:
            with pytest.raises(UnsupportedPlatformError) as exc:
                detect_os()
        assert "Arch Linux" in str(exc.value)

    def test_missing_os_release_fails_no_silent_ubuntu(self):
        # No readable /etc/os-release: cannot confirm Ubuntu -> fail fast rather
        # than silently defaulting to ubuntu (the old behavior).
        with patch("platform.system", return_value="Linux"), \
             patch("builtins.open", side_effect=FileNotFoundError):
            with pytest.raises(UnsupportedPlatformError) as exc:
                detect_os()
        assert "no readable /etc/os-release" in str(exc.value)


class TestDetectArch:
    def test_amd64_variants_normalize(self):
        for raw in ("x86_64", "AMD64", "amd64", "x64"):
            with patch("platform.machine", return_value=raw):
                assert platform_detect.detect_arch() == "amd64"

    def test_arm64_variants_normalize(self):
        for raw in ("arm64", "aarch64", "ARM64"):
            with patch("platform.machine", return_value=raw):
                assert platform_detect.detect_arch() == "arm64"

    def test_unknown_arch_returned_lowercased(self):
        with patch("platform.machine", return_value="riscv64"):
            assert platform_detect.detect_arch() == "riscv64"


class TestDetectOsArch:
    def test_combines_with_dash(self):
        with patch("bootstrap_lib.platform_detect.detect_os", return_value="macos"), \
             patch("bootstrap_lib.platform_detect.detect_arch", return_value="arm64"):
            assert platform_detect.detect_os_arch() == "macos-arm64"
