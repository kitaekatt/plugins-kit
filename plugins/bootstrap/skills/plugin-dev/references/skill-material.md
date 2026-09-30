# Skill material

Skill material is the text of chosen skills, cut to a token budget the caller
states, with a record of every file that was read. A caller names skills by
path and gets one text block to place in a prompt it controls, and a report
that says exactly what went into the block. The module reads files and
returns values; it writes nothing and delivers nothing to any model.

The audience is a plugin author who wants skill text in a prompt, and a
reviewer checking one. The implementation is `bootstrap_lib.skill_material`,
first carried by bootstrap 0.138.0; this file specifies the rules it enforces.

## What the module does not do

- It does not find skills. The caller names each skill by path: a skill
  directory, or the `SKILL.md` inside it. No name is searched for and no root
  restricts the path.
- It does not load anything on demand. Everything in the block is read once,
  inside one `materialize` call, before the caller sends anything.
- It does not run a script, follow a link outside the skill directory, or
  send the block to a model. Where the block goes in the prompt is the
  caller's choice.

## Selection

```python
SkillSelection(
    skills=(
        SkillRef("skills/alpha"),                          # level "full"
        SkillRef("skills/beta", level="catalog"),
        SkillRef("skills/gamma", declared_resources=False,
                 resources=("references/one.md",)),
    ),
    token_budget=8000,      # required, an int greater than 0, no default
    format_version="1",     # optional; "1" is the only registered format
)
```

`SkillRef` has four fields:

| Field | Meaning |
| --- | --- |
| `path` | a non-empty `str`: a skill directory or its `SKILL.md`. A relative path resolves against `base_dir` (default: the process working directory). Absolute paths, `..` and links are accepted. |
| `level` | `"full"` (default) renders name, description, instructions and resources. `"catalog"` renders name and description only. |
| `declared_resources` | `True` (default) loads the resources the skill itself declares (see "Declared resources"). `False` loads only the files in `resources`. A skill named only by `catalog` refs renders no resource whatever this says; on a `catalog` ref that shares its skill with a `full` ref, the flag counts (see "Duplicates"). |
| `resources` | a tuple of further files, as POSIX paths relative to the skill directory. Refused on a `catalog` ref. |

Construction checks types and grammar only and opens no file. Each check
raises `SkillMaterialError`:

- `path` is a non-empty `str`;
- `level` is one of the two words; `declared_resources` is a `bool`;
- `resources` is a tuple of `str`, each passing the resource-path grammar;
- `skills` is a non-empty tuple of `SkillRef`;
- `token_budget` is an `int` greater than 0 (a `bool` is refused);
- `format_version` is a `str`.

`SkillSelection.to_json()` and `SkillSelection.from_json(mapping)` convert to
and from a JSON document with the same keys (`skills`, `token_budget`,
`format_version`, and per skill `path`, `level`, `declared_resources`,
`resources`). `from_json` refuses an unknown key at either level and a
missing `skills`, `token_budget` or `path`.

## The strict reader

`parse_frontmatter_strict(content)` returns `(fields, raw_block, body)` for
decoded text that opens with a frontmatter block: a `---` line, YAML, and a
closing `---` line (`FRONTMATTER_RE`). It raises `FrontmatterError` when
there is no block, the block is not valid YAML, or it is not a mapping. It
never returns empty fields for a block it could not read; a reader that
degrades would let a broken skill pass as an empty one.

`read_skill(path, *, base_dir=None)` reads one skill with the same rules and
returns a `SkillDocument` (`name`, `description`, `body`, `source`, `sha256`,
`bytes`, `declared`, `unparsed_yaml_blocks`). A skill is refused when:

- the file is not strict UTF-8 (a leading byte-order mark is removed first);
- `name` is missing, not a string, empty, or outside
  `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`;
- `description` is missing, not a string, or empty;
- `path` does not exist, is a directory with no `SKILL.md`, or is a file not
  named exactly `SKILL.md`.

A refusal from `materialize` names the ref index (`skills[2]: ...`) of the ref
at fault.

## Declared resources

A `full` ref loads the files the skill declares. A skill declares a file by a
record with a `path` in a `references` list, inside a fenced YAML block of its
`SKILL.md` body:

```yaml
references:
  - id: details
    path: references/details.md
  - id: examples
    path: references/examples/     # a directory: every regular file beneath it
```

Rules, in the order the module applies them:

1. **Fences.** A block opens at a line that is exactly three backticks and
   `yaml` or `yml` (trailing spaces or tabs allowed) and closes at the next
   line that is exactly three backticks. Blocks are read in the order they
   appear.
2. **Parsing.** Each block is read with `yaml.safe_load`. A block that never
   closes, that is not valid YAML, or that holds more than one YAML document
   declares nothing and is counted in `unparsed_yaml_blocks`. It is not a
   refusal, and the count is in the report, so the omission is recorded.
3. **The walk.** Within one parsed block, the walk visits mapping entries in
   the block's own order and list items by index. At an entry whose key is
   exactly `references` and whose value is a list, the list's own declaring
   items are recorded first, in index order, and only then is the list
   searched for anything nested in its items. An item declares a path when it
   is a mapping whose `path` is a string; a prose string or a record without
   `path` declares nothing. A key other than `references` declares nothing.
4. **Aliases and cycles.** A container reached a second time through a YAML
   alias, a merge key or a cycle is not entered again, so a shared
   `references` list is recorded once and a recursive block ends the walk.
5. **Repeats.** A path written twice is declared once, at its first position.
   `declared` in the report is the paths as written.

Each declared path is then resolved against the skill directory of the ref
that declared it:

- grammar: non-empty; no backslash; no leading `/`; no drive (`X:`); no empty
  segment; no `.` or `..` segment; every segment matches `[A-Za-z0-9._-]+`.
  One trailing `/` is allowed on a declared path, and only there;
- the resolved file must lie inside the resolved skill directory, links
  followed. A link that leads out is refused;
- a regular file is one resource. A directory expands to every regular file
  beneath it, recursively, in sorted POSIX-path order, each checked the same
  way;
- a path that fails any of these is refused. The message names the skill, the
  declared path and the remedy: set `declared_resources=False` and name the
  files wanted in `resources`. A skill that declares a file it does not ship
  is never silently shortened.

Files in `resources` follow, in the caller's order, under the same grammar
except that each must be a regular file with no trailing `/`. A file the
declaration already supplied is rendered once and recorded as a suppression.
With `declared_resources=False` the declared paths are listed in the report as
written and are neither resolved nor checked.

Files under a skill's `references/` directory that no record declares are not
loaded unless the caller names them.

## Budget

`estimate_tokens(text)` is `ceil(len(text) / 4)` over Unicode code points; the
report calls it `"chars/4"`. It is an estimate and can undercount code-heavy
or CJK text. The budget is a runaway guard, not a cost cap or a check against
a context window.

- **Before a read.** A UTF-8 code point is at most 4 bytes, so
  `ceil(size_bytes / 16)` is a lower bound on a file's estimate. A resource,
  or the `SKILL.md` of any ref at either level, whose lower bound exceeds
  `token_budget` is refused from its size alone and is not read. The check is
  per file and applies to a `catalog` ref too, although a `catalog` ref
  renders no body: a `SKILL.md` larger than `16 * token_budget` bytes is
  refused even at level `"catalog"`, where the only fix is a larger budget.
  Both refusals raise `SkillMaterialBudgetExceeded` and differ only in the
  remedy that ends the message. A resource, or a `full` ref's `SKILL.md`,
  ends with `To fit: give a skill level "catalog", or set
  declared_resources=False on it and name a shorter list in resources.` A
  `catalog` ref's `SKILL.md` ends with `To fit: raise token_budget. The ref
  is already at level "catalog", and a SKILL.md is checked before it is read
  at either level.`
- **After rendering.** The estimate of the whole block is compared with
  `token_budget`. Over budget raises `SkillMaterialBudgetExceeded`; the
  message lists each skill's name, level and estimate, each resource's path and
  estimate, the total and the budget, and the two ways to fit: give a skill
  level `"catalog"`, or set `declared_resources=False` and name a shorter list.
- Nothing is truncated and nothing partial is returned. There is no automatic
  fallback to a lower level.

A skill that declares many files can be large: a `full` ref to a skill with
substantial `references` can estimate at tens of thousands of tokens. The
refusal is the designed answer, and its message says how to narrow the ref.

## Duplicates

Identity is the frontmatter `name`.

| Case | Result |
| --- | --- |
| the same resolved `SKILL.md` named twice | one skill, at the first ref's position; `full` if either ref is `full`; `declared_resources` is true if either ref sets it, a `catalog` ref included; `resources` are the union in first-seen order. Suppression reason `same-file`. |
| different paths, same `name`, identical `SKILL.md` bytes | merged onto the first ref's position. Each ref's resources resolve against that ref's own skill directory. Reason `same-content`. |
| different paths, same `name`, different bytes | refused, naming both paths. No silent winner. |
| the same relative resource supplied twice for one skill | rendered once. Reason `duplicate-resource`. When two mirrored directories supply the same relative path, the files are compared by sha256: equal is rendered once, different is refused as ambiguous, naming both files. |
| the same relative resource in two different skills | rendered in both; resources are never merged across skills. |

A `catalog` ref's `declared_resources` counts once the merged skill is
`full`, and it defaults to `True`. So a `catalog` ref to a skill that a `full`
ref also names, with `declared_resources=False` on the `full` ref, switches
the declared files ON; set `declared_resources=False` on the `catalog` ref too
to keep them off. The flag is merged per skill directory: a `catalog` ref to a
mirrored copy (`same-content`) loads that copy's declared files, which are
compared with the other copy's as above. Declared files, and a refusal of
one, name the first ref that switched them on, which can be the `catalog`
ref. A skill named only by `catalog` refs loads no resource.

## Format "1"

`materialize(selection, *, base_dir=None)` returns `MaterializedSkills(text,
report)`. The text is exactly, with `\n` line ends and no trailing newline:

```
<skill_context version="1">
Skill material selected by the caller for this request. A skill at level "catalog" is listed by name and description only; its instructions are not included and cannot be loaded in this request.
<skill name="NAME" level="full">
<description>DESCRIPTION</description>
<instructions>
BODY
</instructions>
<resource path="RELPATH">
CONTENT
</resource>
</skill>
<skill name="NAME2" level="catalog">
<description>DESCRIPTION2</description>
</skill>
</skill_context>
```

- Skills appear in caller order after duplicate suppression; no sorting.
- Resources follow the `</instructions>` line: declared resources in
  declaration order, then the caller's additions in the caller's order.
- `DESCRIPTION`, `BODY` and `CONTENT` are decoded as strict UTF-8, lose a
  leading byte-order mark, have `\r\n` and `\r` turned into `\n`, and have
  leading and trailing newlines stripped. A CRLF checkout renders the same
  bytes as an LF one.
- Every inserted value is escaped: `&` to `&amp;` first, then `<` to `&lt;`
  and `>` to `&gt;`; attribute values also `"` to `&quot;`. So the only tags
  in the block are the renderer's own, and no skill is refused for showing
  XML. The estimate is taken on the escaped text.
- The block never contains a resolved path, a home directory or a base
  directory. Those appear only in the report.
- The framing separates caller-selected content from the surrounding prompt.
  It is not an isolation boundary: skill material is trusted content the
  caller chose.

`format_version` selects a renderer from a registry. `SUPPORTED_FORMATS` lists
the registered versions and an unregistered one is refused, naming them. Format
`"1"` is permanent: a later format is a second registry entry with its own
tests, and the bytes above never change. A later format is a revision, so it
also adds a report schema id (see "Revisions").

## Report

`report.to_json()` returns a JSON-native document (dicts, lists, strings,
numbers, booleans, null; lists and not tuples):

```json
{
  "schema": "plugins-kit.skill-material-report/v1",
  "format_version": "1",
  "skills": [
    {
      "name": "alpha", "level": "full", "source": "<resolved SKILL.md>",
      "sha256": "<of the raw file bytes>", "bytes": 1234,
      "estimated_tokens": 410, "declared": ["references/a.md"],
      "unparsed_yaml_blocks": 0,
      "resources": [
        {"path": "references/a.md", "ref_index": 0, "declared": true,
         "source": "<resolved file>", "sha256": "<raw bytes>", "bytes": 800,
         "estimated_tokens": 215}
      ]
    }
  ],
  "suppressed": [
    {"ref_index": 1, "name": "alpha", "reason": "same-file",
     "kept_index": 0, "detail": ""}
  ],
  "estimated_tokens": 640,
  "token_budget": 8000,
  "token_estimate": "chars/4",
  "digest": "<sha256 of the rendered text, UTF-8>"
}
```

- Hashes are of the raw file bytes, before decoding or normalization.
- `skills[].estimated_tokens` and `resources[].estimated_tokens` are the
  estimates of that skill's or resource's rendered element.
- `declared` lists what the skill declares, as written, whether or not it was
  loaded; `resources` lists what was rendered, and `declared` on a resource
  says whether the skill's own declaration supplied it.
- `digest` is known before anything is sent. It names one snapshot: the files
  can change after `materialize` returns.
- The schema id is frozen. A later revision of the document adds a schema id to
  `SUPPORTED_REPORT_SCHEMAS`; it never edits the first.

## Errors

| Class | Base | Meaning |
| --- | --- | --- |
| `SkillMaterialError` | `ValueError` | the input is refused: a selection, a skill, or a resource |
| `FrontmatterError` | `SkillMaterialError` | frontmatter is missing, invalid or not a mapping |
| `SkillMaterialBudgetExceeded` | `SkillMaterialError` | the block does not fit `token_budget` |
| `SkillMaterialUnavailableError` | `RuntimeError` | the environment cannot read skills |
| `PyYamlUnavailableError` | `SkillMaterialUnavailableError` | PyYAML cannot be imported |

`PyYamlUnavailableError` is deliberately not a `SkillMaterialError`: a caller
that catches refused input does not catch a missing package by accident, and
an interpreter problem is never reported as a bad skill.

## PyYAML

Importing the module never fails for a missing package: the module imports
only the standard library at module top and imports `yaml` inside the
functions that parse. The first parse in an interpreter without PyYAML raises
`PyYamlUnavailableError`, whose message names PyYAML; it never degrades to a
lenient parse.

PyYAML is present where the running environment declares it. A plugin's own
virtual environment has it when that plugin's `pyproject.toml` lists `pyyaml`.
The bare bootstrap standalone interpreter carries only path links and no
third-party packages, so it has no PyYAML. A plugin that links `bootstrap_lib`
and calls this module declares `pyyaml` itself; linking `bootstrap_lib` does
not provide it.

## API

| Name | Purpose |
| --- | --- |
| `REPORT_SCHEMA_V1`, `SUPPORTED_REPORT_SCHEMAS` | the frozen report schema id, `"plugins-kit.skill-material-report/v1"`, and the set of supported ids (the capability marker; it only grows) |
| `SUPPORTED_FORMATS` | the registered format versions; `{"1"}` |
| `LEVEL_CATALOG`, `LEVEL_FULL` | `"catalog"` and `"full"` |
| `TOKEN_ESTIMATE` | `"chars/4"`, reported in every report |
| `FRONTMATTER_RE` | the frontmatter pattern the strict reader uses |
| `SkillMaterialError`, `FrontmatterError`, `SkillMaterialBudgetExceeded` | refused input (see "Errors") |
| `SkillMaterialUnavailableError`, `PyYamlUnavailableError` | an environment that cannot read skills |
| `parse_frontmatter_strict(content)` | `(fields, raw_block, body)`, or a refusal; never degrades |
| `SkillDocument`, `read_skill(path, *, base_dir=None)` | read one skill strictly |
| `SkillRef`, `SkillSelection` | the caller's selection; `SkillSelection` has `to_json()` and `from_json(mapping)` |
| `ResourceProvenance`, `SkillProvenance`, `SuppressedRef`, `SkillMaterialReport` | the report's parts; `SkillMaterialReport.to_json()` is its document |
| `MaterializedSkills`, `materialize(selection, *, base_dir=None)` | the text and report for a selection |
| `estimate_tokens(text)` | `ceil(len(text) / 4)` |

## Probing for the module

A shared-lib link pins no version, and a long-running process keeps the copy
it imported. A plugin that calls this module probes for it and holds two
constants of its own: the report schema it reads and the bootstrap version
that carries it. `SUPPORTED_REPORT_SCHEMAS` is the one capability marker.
Every revision adds a schema id to it: a revision of the report, of the call
shape that produces it, or a new format version, since the report records
`format_version`. A module that passes the probe for your schema therefore
accepts every format that schema covers, and the probe does not read
`SUPPORTED_FORMATS`. The probe distinguishes three states:

1. `import bootstrap_lib` raises `ModuleNotFoundError`: absent. Tell the user
   to run `claude plugin install bootstrap@plugins-kit`.
2. Too old or stale, when any of these holds:
   - `bootstrap_lib.skill_material` does not import (catch
     `ModuleNotFoundError` for that module name only, so a syntax error in a
     half-synced copy still surfaces);
   - `SUPPORTED_REPORT_SCHEMAS` is missing, or your schema is not in it;
   - `SkillMaterialError` or `PyYamlUnavailableError` is missing;
   - `inspect.signature(<call>).bind(<the exact arguments you pass>)` raises
     `TypeError`, for every call you make.

   Tell the user to run `claude plugin update bootstrap@plugins-kit`, naming
   the version from YOUR constant.
3. Otherwise: usable. A `PyYamlUnavailableError` at the first parse is a
   fourth, separate diagnosis: the module is current and the interpreter has
   no PyYAML. Report that, not a stale bootstrap.

Never read the version for a message from the module: a stale module cannot
know the version that replaced it.

```python
import inspect

REQUIRED_REPORT_SCHEMA = "plugins-kit.skill-material-report/v1"
SKILL_MATERIAL_BOOTSTRAP = "0.138.0"


def _skill_material():
    try:
        import bootstrap_lib  # noqa: F401
    except ModuleNotFoundError as exc:
        raise MySupportError(
            "skill material needs bootstrap: "
            "claude plugin install bootstrap@plugins-kit"
        ) from exc
    too_old = (
        f"skill material needs bootstrap >= {SKILL_MATERIAL_BOOTSTRAP}: "
        "claude plugin update bootstrap@plugins-kit"
    )
    try:
        import bootstrap_lib.skill_material as module
    except ModuleNotFoundError as exc:
        if exc.name != "bootstrap_lib.skill_material":
            raise
        raise MySupportError(too_old) from exc
    supported = getattr(module, "SUPPORTED_REPORT_SCHEMAS", None)
    if (
        not isinstance(supported, (set, frozenset))
        or REQUIRED_REPORT_SCHEMA not in supported
        or not isinstance(getattr(module, "SkillMaterialError", None), type)
        or not isinstance(getattr(module, "PyYamlUnavailableError", None), type)
    ):
        raise MySupportError(too_old)
    try:
        inspect.signature(module.SkillSelection.from_json).bind({})
        inspect.signature(module.materialize).bind(object(), base_dir=None)
        inspect.signature(module.SkillMaterialReport.to_json).bind(object())
    except (AttributeError, TypeError, ValueError) as exc:
        raise MySupportError(too_old) from exc
    return module
```

The probe sees the class, not the object `materialize` returns, so also guard
the call where it is made: a `TypeError` from `report.to_json()`, or a result
that is not a mapping of JSON-native values at every depth, is the too-old
state.

Choose the edge class (REQUIRED, REFUSE, or DEGRADE) for your plugin with
[optional-plugin-dependencies](optional-plugin-dependencies.md). A prompt
built without the block you asked for would be read as if the skills had been
supplied, so a plugin that can work without skill material REFUSES the request
that needs it, and a plugin that cannot is REQUIRED. A plugin that calls the
module declares `"shared_lib_imports": ["bootstrap_lib"]` in its
`bootstrap.json`, and a REQUIRED edge sets `requires_bootstrap` to the version
that carries the call shapes it uses.

## Revisions

Format `"1"`, the report schema id and the rules above are frozen. A later
rule enters only through a later revision:

1. a new report schema id added to `SUPPORTED_REPORT_SCHEMAS` beside the
   first, and, when the revision is a new format, that format version
   registered in `SUPPORTED_FORMATS` beside `"1"` in the same release;
2. the old entry and its rules unchanged;
3. a keyword selector on the affected call that defaults to the old
   revision, so a caller that passes no selector is unaffected.

This is the procedure [execution-events](execution-events.md) sets for its
schemas ("Revisions"). A process that holds a module without a later revision
finds it missing at its own probe, before it sends anything.
