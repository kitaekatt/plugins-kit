"""Make a ``bootstrap_lib`` module the ``llm_kit`` module itself.

``bootstrap_lib.codex`` is an alias of ``llm_kit.adapters.codex_command`` in the
``katsoft-llm-kit`` distribution: ``sys.modules[<bootstrap_lib name>]`` is
replaced by the canonical module object, so identity is exact and private names
stay reachable. Nothing is copied or re-exported, and no behavior is kept here.
Command construction has one owner, and it lives in the library.

An interpreter that holds ``bootstrap_lib`` without a ``katsoft-llm-kit`` that
carries the module cannot resolve the alias, and there are two causes with
different remedies. The refusal names which one it is: the distribution is not
installed, or it is installed at a version before the module existed.

Standard library only. The bootstrap engine, hooks and scripts never import this
module or ``bootstrap_lib.codex`` (tests/bootstrap/test_standalone_closure.py);
the engine's own Codex question is answered by ``bootstrap_lib.codex_detection``.
"""
from __future__ import annotations

import importlib
import sys

LLM_KIT_DIST = "katsoft-llm-kit"
LLM_KIT_PACKAGE = "llm_kit"
LLM_KIT_REQUIREMENT = "katsoft-llm-kit>=1.3,<2"


def alias(name: str, target: str):
    """Replace ``sys.modules[name]`` by the module ``target`` and return it.

    ``name`` is the dotted ``bootstrap_lib`` module name (``__name__`` of the
    caller) and ``target`` the dotted ``llm_kit`` module it aliases. Only a
    ``ModuleNotFoundError`` for ``llm_kit`` or for ``target`` itself is turned
    into a refusal; any other import failure propagates unchanged, because a
    broken install must not read as an absent or old one.
    """
    try:
        module = importlib.import_module(target)
    except ModuleNotFoundError as exc:
        if exc.name == target:
            raise ModuleNotFoundError(
                f"{name} is an alias of {target}, and the installed {LLM_KIT_DIST} "
                f"({sys.executable}) predates that module. Upgrade it to "
                f"'{LLM_KIT_REQUIREMENT}': declare that requirement in the "
                "pyproject.toml of the plugin whose venv runs this code so "
                "bootstrap installs it.",
                name=target,
            ) from exc
        if exc.name == LLM_KIT_PACKAGE or (
            exc.name and target.startswith(exc.name + ".")
        ):
            raise ModuleNotFoundError(
                f"{name} is an alias of {target}, and the {LLM_KIT_DIST} "
                f"distribution is not installed in this interpreter ({sys.executable}). "
                f"This is a missing {LLM_KIT_DIST}, not a bootstrap_lib that is too old: "
                f"run the code under its plugin's bootstrap-provisioned venv, and "
                f"declare '{LLM_KIT_REQUIREMENT}' in that plugin's pyproject.toml so "
                "bootstrap installs it.",
                name=LLM_KIT_PACKAGE,
            ) from exc
        raise
    sys.modules[name] = module
    return module
