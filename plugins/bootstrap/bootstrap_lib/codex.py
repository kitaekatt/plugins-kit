"""Module alias of ``llm_kit.adapters.codex_command``.

``sys.modules[__name__]`` becomes the canonical module, so
``bootstrap_lib.codex is llm_kit.adapters.codex_command`` and every name,
private ones included, resolves on it. The source of Codex detection and
command construction is
packages/katsoft-llm-kit/src/llm_kit/adapters/codex_command.py. See
``bootstrap_lib._llm_kit_alias`` for the refusal when the distribution is absent
or too old. Bootstrap's own Codex availability question is answered by
``bootstrap_lib.codex_detection``, which never reaches this alias.
"""
from bootstrap_lib._llm_kit_alias import alias as _alias

_alias(__name__, "llm_kit.adapters.codex_command")
