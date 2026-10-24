"""Guard for the shipped agent guidance: plugins/llm-kit/CLAUDE.md and references/*.md.

The guidance is long and re-points a lot of names, so every name it states is
checked against the tree instead of trusted:

* every path (a backticked file path or a markdown link) resolves in the tree;
* every ``llm_kit.<module>[.<name>]`` resolves statically (the module file exists
  and, for a trailing name, the module or class defines it), and every
  ``from llm_kit... import`` in a python block resolves the same way;
* every ``llm-kit <group> <verb>`` in a code span or block resolves in the built
  CLI's argparse tree (``llm_kit.cli.<group>._parser``);
* no legacy plugin name, import package, CLI or plugin-root variable appears,
  except the persisted-state shapes design decision 7 keeps (data and config
  locations, event and envelope identities, the validator subset marker) and
  the explicit provenance lines;
* every env-var name it states appears in the plugin's source;
* the shipped-text rules the repo applies elsewhere: ASCII only, no temporal
  deixis, the plugin root only as ``${LLM_KIT_ROOT:?...}``, interpreter variables
  for commands.

Each checker takes the text (and a document location) so ``TestCheckersCanFail``
feeds it a planted violation and watches it report one; a checker that cannot
fail would let a stale name through silently.
"""

from __future__ import annotations

import ast
import importlib
import os
import re
from functools import lru_cache
from pathlib import Path

import pytest

from test_registrations import LEGACY, interpreter_violations

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "llm-kit"
import llm_kit
LIB = Path(llm_kit.__file__).resolve().parent.parent
REFERENCES = PLUGIN / "references"
CLAUDE_MD = PLUGIN / "CLAUDE.md"
README = PLUGIN / "README.md"

REQUIRED_REFERENCES = (
    "completion-seam-contract.md",
    "llm-access.md",
    "jobs.md",
    "pipeline-library.md",
)


def doc_files() -> list[Path]:
    return [CLAUDE_MD, *sorted(REFERENCES.glob("*.md"))]


def rel(path: Path) -> str:
    return path.relative_to(PLUGIN).as_posix()


# --- text structure ---------------------------------------------------------

FENCE = re.compile(r"^\s*(`{3,})\s*([A-Za-z0-9_+-]*)\s*$")


def split_text(text: str) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """(fenced blocks as (language, lines)), (prose paragraphs, joined onto one line)."""
    fences: list[tuple[str, list[str]]] = []
    prose_lines: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = FENCE.match(lines[i])
        if m:
            marker, lang = m.group(1), m.group(2)
            j = i + 1
            body: list[str] = []
            while j < len(lines) and not (lines[j].strip().startswith(marker) and lines[j].strip().strip("`") == ""):
                body.append(lines[j])
                j += 1
            fences.append((lang, body))
            prose_lines.append("")
            i = j + 1
            continue
        prose_lines.append(lines[i])
        i += 1
    paragraphs: list[str] = []
    current: list[str] = []
    for line in prose_lines:
        if line.strip() == "":
            if current:
                paragraphs.append(" ".join(part.strip() for part in current))
                current = []
        else:
            current.append(line)
    if current:
        paragraphs.append(" ".join(part.strip() for part in current))
    return fences, paragraphs


def code_spans(paragraphs: list[str]) -> list[str]:
    spans: list[str] = []
    for paragraph in paragraphs:
        spans.extend(re.findall(r"`([^`]+)`", paragraph))
    return spans


# --- the file index ----------------------------------------------------------

_SKIP_DIRS = {".venv", "node_modules", "__pycache__", ".git", "tmp", ".pytest_cache"}


@lru_cache(maxsize=1)
def file_index() -> tuple[frozenset[str], frozenset[str]]:
    """(all repo-relative posix paths, all basenames) under the directories a doc may name."""
    paths: set[str] = set()
    names: set[str] = set()
    for top in ("plugins", "docs", "scripts", "tests", ".claude-plugin"):
        base = REPO / top
        if not base.is_dir():
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for name in files:
                full = Path(root, name)
                paths.add(full.relative_to(REPO).as_posix())
                names.add(name)
    return frozenset(paths), frozenset(names)


# --- paths --------------------------------------------------------------------

PATH_ROOTS = {
    "lib", "scripts", "bin", "skills", "references", "agents", "workflows", "contract",
    "docs", "tests", "plugins", "packages", "bootstrap",
}
BARE_EXTS = {"py", "md", "sh", "js", "cmd"}
KNOWN_BARE_JSON = {"bootstrap.json", "public-surface.json", "plugin.json"}
_SKIP_START = ("~", "$", "/", "<", "-", "http", ".local-data", ".plugin-data", ".env", ".venv")
_SKIP_CHARS = set("*{}<>|=():,;\"' ")


def resolve_path(token: str, doc_dir: Path) -> bool:
    token = token.rstrip(".,;:")
    for base in (PLUGIN, REPO, doc_dir):
        if (base / token).exists():
            return True
    paths, names = file_index()
    suffix = "/" + token.lstrip("./")
    if any(p.endswith(suffix) for p in paths):
        return True
    if "/" not in token and token in names:
        return True
    return False


def path_tokens(spans: list[str]) -> list[str]:
    found = []
    for span in spans:
        token = span.strip()
        if not token or token.startswith(_SKIP_START) or any(c in _SKIP_CHARS for c in token):
            continue
        if "/" in token:
            if token.split("/")[0] in PATH_ROOTS or token.startswith(("./", "../")):
                found.append(token)
            continue
        ext = token.rsplit(".", 1)[-1] if "." in token else ""
        if ext in BARE_EXTS and re.fullmatch(r"[\w.-]+", token):
            found.append(token)
        elif token in KNOWN_BARE_JSON:
            found.append(token)
    return found


def link_targets(paragraphs: list[str]) -> list[str]:
    out = []
    for paragraph in paragraphs:
        for target in re.findall(r"\]\(([^)\s]+)\)", paragraph):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            out.append(target.split("#", 1)[0])
    return [t for t in out if t]


def path_problems(text: str, doc_dir: Path) -> list[str]:
    _fences, paragraphs = split_text(text)
    problems = []
    for token in path_tokens(code_spans(paragraphs)):
        if not resolve_path(token, doc_dir):
            problems.append(f"path does not resolve: {token}")
    for target in link_targets(paragraphs):
        if not (doc_dir / target).exists():
            problems.append(f"link does not resolve: {target}")
    return problems


# --- modules and symbols --------------------------------------------------------

DOTTED = re.compile(r"\bllm_kit(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


def module_file(parts: tuple[str, ...]) -> Path | None:
    base = LIB.joinpath(*parts)
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def _collect(statements: list[ast.stmt], into: set[str]) -> None:
    for node in statements:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            into.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        into.add(sub.id)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Name):
                into.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                into.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.If, ast.With)):
            _collect(node.body, into)
            if isinstance(node, ast.If):
                _collect(node.orelse, into)
        elif isinstance(node, ast.Try):
            _collect(node.body, into)
            for handler in node.handlers:
                _collect(handler.body, into)
            _collect(node.orelse, into)
            _collect(node.finalbody, into)


@lru_cache(maxsize=None)
def module_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def top_names(path: Path) -> frozenset[str]:
    names: set[str] = set()
    _collect(module_tree(path).body, names)
    return frozenset(names)


def class_members(path: Path, class_name: str) -> frozenset[str] | None:
    """Members of a class defined in the module, or None when it is not defined there."""
    for node in module_tree(path).body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            members: set[str] = set()
            _collect(node.body, members)
            return frozenset(members)
    return None


def symbol_problem(dotted: str) -> str | None:
    parts = tuple(dotted.split("."))
    for i in range(len(parts), 0, -1):
        mf = module_file(parts[:i])
        if mf is not None:
            break
    else:
        return f"no module for {dotted}"
    rest = parts[i:]
    if not rest:
        return None
    if rest[0] not in top_names(mf):
        return f"{'.'.join(parts[:i])} defines no {rest[0]!r} (named as {dotted})"
    if len(rest) > 1:
        members = class_members(mf, rest[0])
        if members is not None and rest[1] not in members:
            return f"{'.'.join(parts[:i])}.{rest[0]} has no member {rest[1]!r} (named as {dotted})"
    return None


def module_problems(text: str) -> list[str]:
    problems = []
    seen = set()
    for m in DOTTED.finditer(text):
        name = m.group(0)
        if name in seen:
            continue
        seen.add(name)
        problem = symbol_problem(name)
        if problem:
            problems.append(problem)
    fences, _ = split_text(text)
    for lang, body in fences:
        if lang != "python":
            continue
        source = "\n".join(body)
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            problems.append(f"python block does not parse: {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "llm_kit":
                parts = tuple(node.module.split("."))
                mf = module_file(parts)
                if mf is None:
                    problems.append(f"import of a missing module: {node.module}")
                    continue
                for alias in node.names:
                    if alias.name != "*" and alias.name not in top_names(mf) and module_file((*parts, alias.name)) is None:
                        problems.append(f"{node.module} defines no {alias.name!r} (python block import)")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "llm_kit" and module_file(tuple(alias.name.split("."))) is None:
                        problems.append(f"import of a missing module: {alias.name}")
    return problems


# --- the CLI ---------------------------------------------------------------------

_BARE_WORD = re.compile(r"[a-z][a-z0-9-]*(?:\|[a-z][a-z0-9-]*)*")
_CLI_AT = re.compile(r"(?<![A-Za-z0-9_.-])llm-kit(?=[ \t]+[a-z])")


@lru_cache(maxsize=1)
def cli_groups() -> dict:
    """group -> its argparse parser (None for the front door, which owns its own parser)."""
    cli = importlib.import_module("llm_kit.cli")
    parsers = {}
    for group in cli.GROUPS:
        if group == "frontdoor":
            parsers[group] = None
            continue
        parsers[group] = importlib.import_module(f"llm_kit.cli.{group}")._parser()
    return parsers


def _subchoices(parser) -> dict | None:
    for action in parser._actions:
        if action.__class__.__name__ == "_SubParsersAction":
            return action.choices
    return None


def cli_problem(command: str) -> str | None:
    """None when `command` (the text after a literal `llm-kit`) names a real group/verb path."""
    words = []
    for token in command.split():
        if _BARE_WORD.fullmatch(token):
            words.append(token)
        else:
            break
    if not words:
        return None
    groups = cli_groups()
    group = words[0]
    if group not in groups:
        return f"llm-kit has no group {group!r} (in `llm-kit {' '.join(words)}`)"
    parser = groups[group]
    for word in words[1:]:
        if parser is None:
            break
        choices = _subchoices(parser)
        if choices is None:
            break
        for alternative in word.split("|"):
            if alternative not in choices:
                return f"llm-kit {group} has no verb {alternative!r} (in `llm-kit {' '.join(words)}`)"
        parser = choices[word.split("|")[0]]
    return None


def cli_problems(text: str) -> list[str]:
    fences, paragraphs = split_text(text)
    chunks = [line for _lang, body in fences for line in body] + code_spans(paragraphs)
    problems = []
    for chunk in chunks:
        for m in _CLI_AT.finditer(chunk):
            problem = cli_problem(chunk[m.end():].strip())
            if problem:
                problems.append(problem)
    return problems


# --- legacy names --------------------------------------------------------------------

# Persisted-state shapes design decision 7 keeps (docs/reference/llm-kit-architecture.md,
# "Persisted-state compatibility"). Each is an exact shape, removed from a line before
# the legacy scan; anything else naming a legacy plugin is a finding.
PERSISTED_SHAPES = re.compile(
    r"plugins-kit/llm-scripting-kit/(?:\.env|usage-verdicts\.json|config\.yaml|frontdoor-access\.jsonl)"  # shared files only
    r"|\.local-data/llm-scripting-kit/(?:\.env|config\.yaml)"       # the superseded project location
    r"|config/llm-scripting-kit\.yaml"                              # the fleet layer file
    r"|llm-scripting-kit\.json-schema-subset/v1"                    # the frozen validator subset marker
    r"|plugins-kit/job-kit/runs\.sqlite3"                           # the default job ledger path
    r"|\bjob-kit:"                                                  # job event types: job-kit:run-created, ...
    r"|\bjob-kit\.interrupt-(?:request|resolution)/v1"              # job interrupt envelope literals
)

# (document relative to the plugin, substring of the one line it permits, reason).
# An entry that matches no line fails test_provenance_entries_are_not_stale.
PROVENANCE: tuple[tuple[str, str, str], ...] = (
    ("CLAUDE.md", "the `content-pipeline-kit`",
     "persisted identity: the pipeline execution event-extension owner is recorded as content-pipeline-kit"),
)


def legacy_problems(rel_path: str, text: str, provenance=PROVENANCE) -> list[str]:
    problems = []
    for n, line in enumerate(text.splitlines(), start=1):
        scrubbed = PERSISTED_SHAPES.sub("", line)
        if not LEGACY.search(scrubbed):
            continue
        if any(rel_path == p and anchor in line for p, anchor, _ in provenance):
            continue
        problems.append(f"legacy name at {rel_path}:{n}: {line.strip()[:100]}")
    return problems


# --- env vars -------------------------------------------------------------------------

ENV_PREFIXES = (
    "LLM_SCRIPTING_KIT_", "CONTENT_PIPELINE_", "JOB_KIT_", "OPENROUTER_", "CLAUDE_BOOTSTRAP_",
    "CLAUDE_CODE_", "LLM_KIT_",
)
# Exported by the bootstrap engine from the plugin name, not defined in this plugin's source.
BOOTSTRAP_EXPORTED = {"LLM_KIT_ROOT"}


@lru_cache(maxsize=1)
def plugin_source_text() -> str:
    chunks = []
    for base in (LIB, PLUGIN / "scripts", PLUGIN / "bin"):
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix in {".py", ".sh", ".cmd", ""} and "__pycache__" not in path.parts:
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    for name in ("custom_bootstrap.py", "bootstrap.json"):
        chunks.append((PLUGIN / name).read_text(encoding="utf-8"))
    return "\n".join(chunks)


def env_problems(text: str) -> list[str]:
    _fences, paragraphs = split_text(text)
    source = plugin_source_text()
    problems = []
    for span in code_spans(paragraphs):
        token = span.strip()
        if re.fullmatch(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+", token) and token.startswith(ENV_PREFIXES):
            if token not in BOOTSTRAP_EXPORTED and token not in source:
                problems.append(f"env var not found in the plugin source: {token}")
    return problems


def bootstrap_export_problems() -> list[str]:
    from bootstrap_lib.env_var_check import plugin_root_env_var_name

    return [] if plugin_root_env_var_name("llm-kit") == "LLM_KIT_ROOT" else ["LLM_KIT_ROOT is not the exported root name"]


# --- shipped-text rules -------------------------------------------------------------------

BOX_DRAWING = range(0x2500, 0x2580)
TEMPORAL = re.compile(
    r"\b(?:recent|recently|newly|just (?:shipped|added|landed|released)|no longer|used to|"
    r"as of now|these days|at the moment|nowadays|(?:is|are|was|were) now|"
    r"now (?:ships|supports|uses|lives|carries|requires))\b",
    re.I,
)


def ascii_problems(text: str) -> list[str]:
    return [
        f"non-ASCII character U+{ord(ch):04X} at line {text[:i].count(chr(10)) + 1}"
        for i, ch in enumerate(text)
        if ord(ch) > 127 and ord(ch) not in BOX_DRAWING
    ][:5]


def temporal_problems(text: str) -> list[str]:
    fences, paragraphs = split_text(text)
    return [f"temporal deixis: {m.group(0)!r} in {p[:80]!r}" for p in paragraphs for m in TEMPORAL.finditer(p)]


def root_variable_problems(text: str) -> list[str]:
    problems = []
    if "CLAUDE_PLUGIN_ROOT" in text or "CLAUDE_SKILL_DIR" in text:
        problems.append("a non-skill document names a harness plugin variable")
    for m in re.finditer(r"\$\{LLM_KIT_ROOT([^}]*)\}", text):
        if not m.group(1).startswith(":?"):
            problems.append(f"plugin root without a :? guard: {m.group(0)}")
    return problems


def interpreter_problems(rel_path: str, text: str) -> list[str]:
    """The repo's interpreter rule, applied to commands (fenced lines and spans with a space)."""
    fences, paragraphs = split_text(text)
    lines = [line for lang, body in fences if lang not in {"yaml", "json", "python", "text"} for line in body]
    lines += [span for span in code_spans(paragraphs) if " " in span.strip()]
    return [f"interpreter rule: {line.strip()[:100]}" for line in lines if interpreter_violations(rel_path, line + "\n")]


ALL_CHECKS = {
    "paths": lambda path, text: path_problems(text, path.parent),
    "modules": lambda path, text: module_problems(text),
    "cli": lambda path, text: cli_problems(text),
    "legacy": lambda path, text: legacy_problems(rel(path), text),
    "env": lambda path, text: env_problems(text),
    "ascii": lambda path, text: ascii_problems(text),
    "temporal": lambda path, text: temporal_problems(text),
    "root": lambda path, text: root_variable_problems(text),
    "interpreter": lambda path, text: interpreter_problems(rel(path), text),
}


# --- the tests --------------------------------------------------------------------------------

def test_the_documents_exist() -> None:
    assert CLAUDE_MD.is_file()
    names = {p.name for p in REFERENCES.glob("*.md")}
    assert set(REQUIRED_REFERENCES) <= names, set(REQUIRED_REFERENCES) - names


@pytest.mark.parametrize("check", sorted(ALL_CHECKS))
@pytest.mark.parametrize("path", doc_files(), ids=rel)
def test_document_obeys(check: str, path: Path) -> None:
    problems = ALL_CHECKS[check](path, path.read_text(encoding="utf-8"))
    assert not problems, "\n".join(problems)


def test_the_root_variable_name_is_what_bootstrap_exports() -> None:
    assert not bootstrap_export_problems()


def test_the_entry_documents_point_at_every_reference() -> None:
    readme = README.read_text(encoding="utf-8")
    claude = CLAUDE_MD.read_text(encoding="utf-8")
    for name in REQUIRED_REFERENCES:
        assert f"references/{name}" in readme, f"README.md does not point at references/{name}"
        assert f"references/{name}" in claude, f"CLAUDE.md does not point at references/{name}"


def test_the_seam_contract_is_the_carried_document() -> None:
    text = (REFERENCES / "completion-seam-contract.md").read_text(encoding="utf-8")
    assert text.startswith("# The completion seam's per-call contract")
    assert "llm_kit.completion.requirements.match_capabilities" in text
    assert "llm_kit.schema.json_schema" in text


def test_provenance_entries_are_not_stale() -> None:
    for rel_path, anchor, _reason in PROVENANCE:
        text = (PLUGIN / rel_path).read_text(encoding="utf-8")
        assert any(anchor in line for line in text.splitlines()), (rel_path, anchor)


def test_every_cli_group_is_documented_somewhere() -> None:
    corpus = "\n".join(p.read_text(encoding="utf-8") for p in doc_files())
    for group in cli_groups():
        assert f"llm-kit {group}" in corpus, group


# --- every checker, shown to fail ----------------------------------------------------------------

class TestCheckersCanFail:
    def test_a_missing_path_is_reported(self) -> None:
        assert path_problems("see `lib/llm_kit/no_such_module.py`", PLUGIN)
        assert path_problems("see `references/no-such-doc.md`", PLUGIN)
        assert path_problems("see [x](no-such-doc.md)", REFERENCES)
        assert path_problems("run `no_such_script.py`", PLUGIN)
        assert not module_problems("`llm_kit.completion.halt`")
        assert not path_problems("see [y](jobs.md)", REFERENCES)
        assert not path_problems("see `README.md` and `~/.claude/x.py`", PLUGIN)

    def test_a_missing_module_or_symbol_is_reported(self) -> None:
        assert module_problems("`llm_kit.no_such_package.thing`")
        assert module_problems("`llm_kit.dispatch.declaration.no_such_function`")
        assert module_problems("`llm_kit.completion.halt.Backpressure.no_such_member`")
        assert module_problems("```python\nfrom llm_kit.completion.halt import NoSuchName\n```")
        assert module_problems("```python\nimport llm_kit.no_such\n```")
        assert not module_problems("`llm_kit.completion.halt.Backpressure.retry_after_s` and `llm_kit.pipeline.*`")
        assert not module_problems("```python\nfrom llm_kit.completion.halt import HaltError\n```")

    def test_a_missing_cli_verb_is_reported(self) -> None:
        assert cli_problems("run `llm-kit status`")
        assert cli_problems("run `llm-kit models no-such-verb`")
        assert cli_problems("```\nllm-kit local swapper no-such-verb --port 1\n```")
        assert cli_problems("run `llm-kit account set-key|no-such`")
        assert not cli_problems("run `llm-kit models endpoints --verify` or `llm-kit frontdoor --check`")
        assert not cli_problems("```\nprintf x | llm-kit completion complete --models a\n```")
        assert not cli_problems("`llm-kit local acceptance swapper|frontdoor` and the `llm-kit` command")

    def test_a_legacy_name_is_reported_and_persisted_state_is_not(self) -> None:
        assert legacy_problems("CLAUDE.md", "run `llm-scripting-kit status` or import llm_scripting_kit\n")
        assert legacy_problems("CLAUDE.md", "the content-pipeline-kit plugin\n")
        assert legacy_problems("CLAUDE.md", "`job-kit status` shows the run\n")
        assert legacy_problems("CLAUDE.md", "~/.claude/plugins/data/plugins-kit/llm-scripting-kit-extra/venv\n")
        assert legacy_problems("CLAUDE.md", "the llm-scripting-kit data directory\n")
        # bootstrap's own state files live in llm-kit's data directory, not the legacy one
        assert legacy_problems("CLAUDE.md", "`plugins-kit/llm-scripting-kit/deferred_requirements.json`\n")
        assert legacy_problems("CLAUDE.md", "`plugins-kit/llm-scripting-kit/last_validated.sha256`\n")
        assert not legacy_problems("CLAUDE.md", "`~/.claude/plugins/data/plugins-kit/llm-scripting-kit/.env`\n")
        assert not legacy_problems("CLAUDE.md", "`~/.claude/config/llm-scripting-kit.yaml` and `job-kit:run-created`\n")
        assert not legacy_problems("CLAUDE.md", "marker `llm-scripting-kit.json-schema-subset/v1`\n")
        # provenance is per document and per line
        assert not legacy_problems("CLAUDE.md", "the `content-pipeline-kit` owner\n")
        assert legacy_problems("references/jobs.md", "the `content-pipeline-kit` owner\n")

    def test_an_unknown_env_var_is_reported(self) -> None:
        assert env_problems("set `LLM_SCRIPTING_KIT_NO_SUCH_VARIABLE`")
        assert env_problems("set `JOB_KIT_NO_SUCH_VARIABLE`")
        assert not env_problems("set `JOB_KIT_BACKPRESSURE_CAP_S` and `LLM_KIT_ROOT`")

    def test_the_shipped_text_rules_are_enforced(self) -> None:
        assert ascii_problems("an em dash \u2014 here")
        assert not ascii_problems("a tree \u251c\u2500 drawn")
        assert temporal_problems("the thing was recently added")
        assert temporal_problems("the file is now generated")
        assert temporal_problems("the flag was removed, it no longer applies")
        assert not temporal_problems("usable right now, and it works")
        assert root_variable_problems("run ${CLAUDE_PLUGIN_ROOT}/x")
        assert root_variable_problems('"${LLM_KIT_ROOT}/x"')
        assert not root_variable_problems('"${LLM_KIT_ROOT:?run bootstrap run}/x"')
        assert interpreter_problems("CLAUDE.md", "```\npython scripts/x.py\n```\n")
        assert interpreter_problems("CLAUDE.md", "run `uv run python -m x`\n")
        assert not interpreter_problems("CLAUDE.md", "the script `scripts/x.py` reports\n")

    def test_the_fence_splitter_separates_code_from_prose(self) -> None:
        fences, paragraphs = split_text("intro `a`\n\n```bash\nllm-kit models probe\n```\n\nafter `b`\n")
        assert fences == [("bash", ["llm-kit models probe"])]
        assert paragraphs == ["intro `a`", "after `b`"]
