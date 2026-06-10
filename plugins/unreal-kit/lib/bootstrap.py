"""
UE Python dependency bootstrap.

Call ensure_dependencies() at the top of any script that needs external packages.
Reads requirements from lib/requirements.yaml and uses unreal_pip to install
any missing packages into UE's site-packages.

Usage in scripts:
    import sys, os
    sys.path.insert(0, os.path.expanduser('~/.claude/plugins/data/plugins-kit/unreal-kit/lib'))
    sys.path.insert(0, os.path.expanduser('~/.claude/plugins/data/plugins-kit/unreal-kit/github/unreal-pip'))
    from bootstrap import ensure_dependencies
    ensure_dependencies()

    import yaml  # now available
"""

import sys
from pathlib import Path

# lib/ directory where this file lives — synced to data dir by bootstrap
LIB_DIR = Path(__file__).resolve().parent
REQUIREMENTS_FILE = LIB_DIR / 'requirements.yaml'


def ensure_dependencies():
    """Install any missing packages listed in requirements.yaml."""
    import unreal

    # Ensure lib/ is on path so unreal_pip is importable
    lib_str = str(LIB_DIR)
    if lib_str not in sys.path:
        sys.path.insert(0, lib_str)

    # Read requirements (use a simple parser to avoid needing yaml before it's installed)
    packages = _read_requirements()
    if not packages:
        return

    # Check what's already installed. importlib.metadata is the stdlib
    # replacement for pkg_resources (removed from modern setuptools).
    try:
        from importlib import metadata as _metadata
        installed = {
            (dist.metadata["Name"] or "").lower()
            for dist in _metadata.distributions()
        }
        installed.discard("")
    except Exception:
        installed = set()

    missing = [p for p in packages if p.lower() not in installed]
    if not missing:
        unreal.log("All UE Python dependencies satisfied.")
        return

    unreal.log(f"Installing missing UE Python packages: {missing}")
    import unreal_pip
    unreal_pip.install(missing)

    # Refresh import machinery so newly installed packages are importable
    try:
        import importlib
        importlib.invalidate_caches()
    except Exception:
        pass

    unreal.log("Dependencies installed. You may need to restart the script if imports still fail.")


def _read_requirements():
    """Parse requirements.yaml without needing pyyaml (chicken-and-egg)."""
    if not REQUIREMENTS_FILE.exists():
        return []

    packages = []
    in_packages = False
    with open(REQUIREMENTS_FILE, 'r') as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith('#') or not stripped:
                continue
            if stripped == 'packages:':
                in_packages = True
                continue
            if in_packages and stripped.startswith('- '):
                packages.append(stripped[2:].strip())
            elif not stripped.startswith('-'):
                in_packages = False
    return packages
