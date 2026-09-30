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

This module is also the `material` command of scripts/skills_kit_tool.py
(`main`, `build_parser`, the exit codes and `OUTCOMES`). The command prints
the selected skills as one text block, or the provenance report as JSON, and
maps each failure to one exit code; the three unavailable states above are
all exit 3.
"""

from __future__ import annotations

import argparse
import inspect
import json
import math
import sys

# The bootstrap version that first carries bootstrap_lib.skill_material with
# every call shape this binding makes. This plugin's own constant: a stale
# module cannot know the version that replaced it, so no message reads a
# version from the library.
SKILL_MATERIAL_BOOTSTRAP = "0.138.0"

_REQUIRED_REPORT_SCHEMA = "plugins-kit.skill-material-report/v1"

_ABSENT_MESSAGE = (
    "skills-kit's skill-material features (the strict frontmatter mode and "
    "the material command) need bootstrap_lib, which is not importable in "
    "this interpreter. Install bootstrap: "
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


# ---------------------------------------------------------------------------
# The command: skills_kit_tool.py material
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3
EXIT_OVER_BUDGET = 4

# The four outcomes of a RUN, keyed by exit code. Exit 2 is deliberately not
# a key: a malformed command line decides nothing about the skill, so it is
# not a verdict on it. md-domain's render lane declares these values as its
# verdicts.
OUTCOMES = {
    EXIT_OK: "RENDERED",
    EXIT_REFUSED: "REFUSED",
    EXIT_UNAVAILABLE: "UNAVAILABLE",
    EXIT_OVER_BUDGET: "OVER-BUDGET",
}

_PROG = "material"


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


class _AddSkill(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        refs = list(getattr(namespace, "refs", None) or [])
        refs.append(
            {"path": values, "level": "full", "declared_resources": True, "resources": []}
        )
        namespace.refs = refs


class _Modifier(argparse.Action):
    """A flag that changes the `--skill` given before it."""

    def __call__(self, parser, namespace, values, option_string=None):
        refs = getattr(namespace, "refs", None)
        if not refs:
            parser.error(f"{option_string} must come after the --skill it applies to")
        ref = refs[-1]
        if self.dest == "catalog":
            ref["level"] = "catalog"
        elif self.dest == "no_declared":
            ref["declared_resources"] = False
        else:
            ref["resources"] = [*ref["resources"], values]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            "Print the text of the named skills, and the reference files they "
            "declare, as one block for another model or agent. Reads files and "
            "writes nothing."
        ),
    )
    parser.add_argument(
        "--skill", action=_AddSkill, metavar="PATH",
        help="a skill directory or its SKILL.md; repeatable, order is kept",
    )
    parser.add_argument(
        "--catalog", action=_Modifier, nargs=0, dest="catalog", default=argparse.SUPPRESS,
        help="render the preceding --skill as name and description only",
    )
    parser.add_argument(
        "--no-declared", action=_Modifier, nargs=0, dest="no_declared",
        default=argparse.SUPPRESS,
        help="do not load the files the preceding --skill declares",
    )
    parser.add_argument(
        "--resource", action=_Modifier, dest="resource", metavar="REL",
        default=argparse.SUPPRESS,
        help="a further file of the preceding --skill, relative to its directory; repeatable",
    )
    parser.add_argument(
        "--budget", type=_positive_int, required=True, metavar="N",
        help="token budget for the whole block (chars/4 estimate); no default",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="print the provenance report as JSON instead of the text block",
    )
    parser.set_defaults(refs=[])
    return parser


def _is_json_native(value) -> bool:
    """A mapping whose every value, at every depth, is a str, int, finite
    float, bool, None, list, or dict with str keys. Tuples and other objects
    fail."""
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_native(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_json_native(v) for k, v in value.items())
    return False


def _write_stdout(text: str) -> None:
    """UTF-8 with LF line ends on every platform, so the bytes equal the digest's."""
    data = text.encode("utf-8")
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(text)
        return
    sys.stdout.flush()
    buffer.write(data)
    buffer.flush()


def _fail(message: str, code: int) -> int:
    print(f"{_PROG}: {message}", file=sys.stderr)
    return code


def main(argv=None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    if not args.refs:
        try:
            parser.error("at least one --skill is required")
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else EXIT_USAGE

    try:
        module = load_skill_material()
    except SkillMaterialUnavailable as exc:
        return _fail(str(exc), EXIT_UNAVAILABLE)

    mapping = {"skills": args.refs, "token_budget": args.budget}
    try:
        selection = module.SkillSelection.from_json(mapping)
        result = module.materialize(selection)
    except module.PyYamlUnavailableError as exc:
        return _fail(str(no_pyyaml(exc)), EXIT_UNAVAILABLE)
    except module.SkillMaterialBudgetExceeded as exc:
        return _fail(str(exc), EXIT_OVER_BUDGET)
    except module.SkillMaterialError as exc:
        return _fail(str(exc), EXIT_REFUSED)

    if not args.json:
        _write_stdout(result.text + "\n")
        return EXIT_OK

    try:
        document = result.report.to_json()
    except TypeError:
        return _fail(_TOO_OLD_MESSAGE, EXIT_UNAVAILABLE)
    if not isinstance(document, dict) or not _is_json_native(document):
        return _fail(_TOO_OLD_MESSAGE, EXIT_UNAVAILABLE)
    _write_stdout(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
