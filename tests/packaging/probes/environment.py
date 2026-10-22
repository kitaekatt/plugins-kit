"""Closure probe: what the interpreter this script runs under can see.

Run by tests/packaging/test_wheel_closure.py with the venv's own interpreter in
isolated mode (-I), so no PYTHONPATH, user site or script directory leaks in.
Prints one JSON object. It asserts nothing; the test judges the report.
"""
import importlib.metadata as md
import importlib.util
import json
import site
import sys
from pathlib import Path

report = {
    "prefix": sys.prefix,
    "sys_path": list(sys.path),
    "bootstrap_lib_origin": None,
    "pth": {},
    "distributions": {},
    "origins": {},
    "markers": {},
    "errors": {},
}

spec = importlib.util.find_spec("bootstrap_lib")
if spec is not None:
    report["bootstrap_lib_origin"] = str(spec.origin or list(spec.submodule_search_locations or []))

for entry in sys.path:
    path = Path(entry)
    if path.is_dir():
        for pth in path.glob("*.pth"):
            report["pth"][str(pth)] = pth.read_text(encoding="utf-8").splitlines()

for dist in md.distributions():
    report["distributions"][dist.metadata["Name"].lower().replace("_", "-")] = dist.version

for name in ("llm_kit", "katsoft_contracts"):
    try:
        module = __import__(name)
        report["origins"][name] = module.__file__
    except BaseException as exc:  # noqa: BLE001 - the report names it
        report["errors"][name] = f"{type(exc).__name__}: {exc}"

try:
    from llm_kit import _deployment, _runtime
    report["markers"]["llm_kit"] = {k: getattr(_deployment, k, None)
                                    for k in ("MODE", "DIST", "VERSION", "TAG", "COMMIT")}
    report["markers"]["llm_kit_runtime_mode"] = _runtime.mode()
except BaseException as exc:  # noqa: BLE001
    report["errors"]["llm_kit_marker"] = f"{type(exc).__name__}: {exc}"

try:
    from katsoft_contracts import _release
    report["markers"]["katsoft_contracts"] = {k: getattr(_release, k, None)
                                              for k in ("MODE", "DIST", "VERSION", "TAG", "COMMIT")}
except BaseException as exc:  # noqa: BLE001
    report["errors"]["contracts_marker"] = f"{type(exc).__name__}: {exc}"

report["site_packages"] = site.getsitepackages()
print(json.dumps(report))
