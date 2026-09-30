"""The one binding skills-kit keeps to bootstrap_lib.skill_material.

`parse_frontmatter(mode="strict")` reaches the library only through this
module. Nothing on the audit path imports it: `markdown_heuristics` imports
it inside the strict branch, so importing `markdown_heuristics` (which every
audit caller does) imports neither this module nor `bootstrap_lib`.

Edge class (plugins/CLAUDE.md, "Optional use of another plugin"): REQUIRED
library, floor `SKILL_MATERIAL_BOOTSTRAP` in this plugin's bootstrap.json;
REFUSE at the call for the states a floor cannot exclude. A strict parse
that fell back to the lenient parser would report a degraded result as
strict, so an unusable library is `SkillMaterialUnavailable`, never a
lenient answer. Three states are told apart by `state`:

- "absent": `bootstrap_lib` is not importable in this interpreter;
- "too-old": `bootstrap_lib` imports, but the module is missing, its
  capability marker lacks the report schema this binding reads, or a call
  skills-kit makes does not bind;
- "no-pyyaml": the library is current and the running interpreter has no
  PyYAML, which the library needs to read a skill.

The consumer contract is
plugins/bootstrap/skills/plugin-dev/references/skill-material.md
("Probing for the module").
"""

from __future__ import annotations

import inspect

# The bootstrap version that first carries bootstrap_lib.skill_material with
# every call shape this binding makes. This plugin's own constant: a stale
# module cannot know the version that replaced it, so no message reads a
# version from the library.
SKILL_MATERIAL_BOOTSTRAP = "0.138.0"

_REQUIRED_REPORT_SCHEMA = "plugins-kit.skill-material-report/v1"

_ABSENT_MESSAGE = (
    "skills-kit's strict frontmatter mode needs bootstrap_lib, which is not "
    "importable in this interpreter. Install bootstrap: "
    "claude plugin install bootstrap@plugins-kit. If bootstrap is installed, "
    "run skills-kit through its launcher, scripts/skills_kit_tool.py, which "
    "re-executes under the skills-kit venv that links bootstrap_lib. "
    "Nothing was parsed."
)

_TOO_OLD_MESSAGE = (
    "the linked bootstrap_lib does not provide the skill-material calls "
    "skills-kit makes (bootstrap_lib.skill_material, report schema "
    f"{_REQUIRED_REPORT_SCHEMA}); skills-kit needs bootstrap >= "
    f"{SKILL_MATERIAL_BOOTSTRAP}. Update it: "
    "claude plugin update bootstrap@plugins-kit. A bootstrap_lib copy left "
    "behind by an uninstall reads the same way. Nothing was parsed."
)

_NO_PYYAML_MESSAGE = (
    "the interpreter running this call has no PyYAML, which "
    "bootstrap_lib.skill_material needs to read a skill. The skills-kit venv "
    "has it: run skills-kit through its launcher, scripts/skills_kit_tool.py. "
    "Nothing was parsed; the frontmatter was not judged invalid."
)


class SkillMaterialUnavailable(ImportError):
    """bootstrap_lib.skill_material cannot be used from this interpreter.

    `state` is "absent", "too-old" or "no-pyyaml". It says something about
    the environment, never about the text being parsed.
    """

    def __init__(self, message: str, *, state: str) -> None:
        super().__init__(message)
        self.state = state


def load_skill_material():
    """Return `bootstrap_lib.skill_material` after probing every call
    skills-kit makes, or raise `SkillMaterialUnavailable`.

    The import is lazy and static. Only `ModuleNotFoundError` for the named
    module is caught, so a syntax error in a half-synced copy propagates.
    """
    try:
        import bootstrap_lib  # noqa: F401
    except ModuleNotFoundError as exc:
        if exc.name != "bootstrap_lib":
            raise
        raise SkillMaterialUnavailable(_ABSENT_MESSAGE, state="absent") from exc
    try:
        import bootstrap_lib.skill_material as module
    except ModuleNotFoundError as exc:
        if exc.name != "bootstrap_lib.skill_material":
            raise
        raise SkillMaterialUnavailable(_TOO_OLD_MESSAGE, state="too-old") from exc
    supported = getattr(module, "SUPPORTED_REPORT_SCHEMAS", None)
    if (
        not isinstance(supported, (set, frozenset))
        or _REQUIRED_REPORT_SCHEMA not in supported
        or not isinstance(getattr(module, "SkillMaterialError", None), type)
        or not isinstance(getattr(module, "PyYamlUnavailableError", None), type)
    ):
        raise SkillMaterialUnavailable(_TOO_OLD_MESSAGE, state="too-old")
    strict = getattr(module, "parse_frontmatter_strict", None)
    from_json = getattr(getattr(module, "SkillSelection", None), "from_json", None)
    materialize = getattr(module, "materialize", None)
    to_json = getattr(getattr(module, "SkillMaterialReport", None), "to_json", None)
    try:
        inspect.signature(strict).bind("")  # parse_frontmatter_strict(content)
        inspect.signature(from_json).bind({})  # SkillSelection.from_json(mapping)
        inspect.signature(materialize).bind(object(), base_dir=None)
        inspect.signature(to_json).bind(object())  # report.to_json()
    except (TypeError, ValueError) as exc:
        raise SkillMaterialUnavailable(_TOO_OLD_MESSAGE, state="too-old") from exc
    return module


def no_pyyaml(exc: BaseException) -> SkillMaterialUnavailable:
    """The one mapping of the library's `PyYamlUnavailableError` to this
    binding's error. The caller raises the result `from exc`."""
    return SkillMaterialUnavailable(_NO_PYYAML_MESSAGE, state="no-pyyaml")
