"""Clean-venv closure of the two built wheels (spec unit U4).

A fresh venv gets ONLY katsoft-contracts and katsoft-llm-kit (the wheels built
here, or the PyPI releases under ``--wheel-source pypi``) plus their declared
third-party dependencies. There is no bootstrap_lib, no plugin tree, no .pth
link and no PYTHONPATH, and HOME is a scratch directory. In that venv the tests
run the library surface the databench index-pages runner uses (derived from its
source; see probes/databench_surface.py) and the CLI verbs it shells out to
(llm-kit completion|models|jobs), through both the console script and
``python -m llm_kit.cli``, against a local stub OpenAI-compatible endpoint.

Opt-in (marker ``wheel_closure``); see conftest.py. Each assertion is shown to
go red by the ``test_closure_*_is_detected`` tests, which break the closure on
purpose and require the same judging functions to report it.

    uv run --extra dev pytest tests/packaging --wheel-source local -v
"""
from __future__ import annotations

import json
import sys
import textwrap

import pytest

import closure_support as support

pytestmark = pytest.mark.wheel_closure

VIAS = ("script", "module")


# --- the install is closed --------------------------------------------------

def _environment(venv, tmp_path):
    return support.run_probe(venv, "environment.py", home=tmp_path / "home", cwd=tmp_path)


def test_install_is_closed(closure_venv, expected_commits, tmp_path):
    report = _environment(closure_venv, tmp_path)
    assert support.environment_problems(report, expected_commits=expected_commits) == []


def test_markers_name_package_mode_and_the_build_commit(closure_venv, expected_commits, tmp_path):
    report = _environment(closure_venv, tmp_path)
    for label, dist in (("llm_kit", support.KIT_DIST), ("katsoft_contracts", support.CONTRACTS_DIST)):
        marker = report["markers"][label]
        assert marker["MODE"] == "package"
        assert marker["COMMIT"] == expected_commits[dist]
        assert marker["VERSION"] == support.project_version(dist)
    assert report["markers"]["llm_kit_runtime_mode"] == "package"


# --- databench's in-process library surface ---------------------------------

def _surface(venv, tmp_path):
    return support.run_probe(venv, "databench_surface.py", str(tmp_path / "scratch"),
                             json.dumps(list(support.KIT_MODULES)),
                             home=tmp_path / "nonexistent-home", cwd=tmp_path)


def test_databench_in_process_surface(closure_venv, tmp_path):
    assert support.surface_problems(_surface(closure_venv, tmp_path)) == []


def test_databench_in_process_surface_needs_no_extras(base_venv, tmp_path):
    """The five modules and the execution-event contract are standard-library only."""
    report = support.run_probe(base_venv, "environment.py", home=tmp_path / "home", cwd=tmp_path)
    assert not {"openai", "pyyaml"} & set(report["distributions"])
    assert support.surface_problems(_surface(base_venv, tmp_path)) == []


def test_databench_surface_covers_databench_source():
    root = support.databench_root() / "src" / "databench"
    if not root.is_dir():
        pytest.skip(f"databench source not at {root} (set DEVROOT)")
    sources = [(root / name).read_text(encoding="utf-8") for name in support.DATABENCH_FILES]
    assert support.uncovered_references(support.databench_references(sources)) == []


def test_drift_check_names_an_unexercised_module():
    source = "import importlib\nm = importlib.import_module('llm_kit.pipeline.runtime.platform')\n"
    assert support.uncovered_references(support.databench_references([source])) == [
        "llm_kit.pipeline.runtime.platform"]
    source = "from bootstrap_lib import interrupt_contract\n"
    assert support.uncovered_references(support.databench_references([source])) == [
        "bootstrap_lib.interrupt_contract"]


# --- the CLI verbs databench shells out to ----------------------------------

@pytest.fixture()
def cli(closure_venv, stub_endpoint, tmp_path):
    """Run `llm-kit` under the closure venv against the stub endpoint."""
    home = tmp_path / "home"
    home.mkdir()
    registry = tmp_path / "registry.yaml"
    registry.write_text(stub_endpoint.registry(), encoding="utf-8")
    env = {"OPENROUTER_API_KEY": "fake-key", "MODEL_ENDPOINTS_REGISTRY": str(registry)}
    stub_endpoint.requests.clear()

    def run(via, argv, stdin=None):
        return support.run_cli(closure_venv, via, argv, home=home, cwd=tmp_path, env=env, stdin=stdin)

    run.tmp = tmp_path
    return run


def _json(proc):
    assert "Traceback" not in proc.stderr, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("via", VIAS)
def test_jobs_version(cli, via):
    proc = cli(via, ["jobs", "--version"])
    assert proc.returncode == 0 and proc.stdout.startswith("llm-kit jobs "), (proc.stdout, proc.stderr)


@pytest.mark.parametrize("via", VIAS)
def test_completion_request_schema(cli, via):
    proc = cli(via, ["completion", "request-schema"])
    assert proc.returncode == 0, proc.stderr
    assert {"protocol", "keys", "options"} <= set(_json(proc))


@pytest.mark.parametrize("via", VIAS)
def test_completion_repair_json(cli, via):
    schema = cli.tmp / "schema.json"
    schema.write_text(json.dumps({"type": "object", "properties": {"n": {"type": "integer"}},
                                  "required": ["n"]}), encoding="utf-8")
    proc = cli(via, ["completion", "repair-json", "--schema-file", str(schema)],
               stdin='```json\n{"n": 1}\n```')
    assert proc.returncode == 0, proc.stderr
    out = _json(proc)
    assert out["status"] == "repaired" and json.loads(out["text"]) == {"n": 1}


@pytest.mark.parametrize("via", VIAS)
def test_models_endpoints(cli, via):
    proc = cli(via, ["models", "endpoints"])
    assert proc.returncode == 0, proc.stderr
    entry = _json(proc)["endpoints"]["local"]
    assert entry["kind"] == "transport" and entry["model"] == "m-local"


@pytest.mark.parametrize("via", VIAS)
def test_models_describe_reaches_the_stub(cli, stub_endpoint, via):
    requirements = cli.tmp / "requirements.json"
    requirements.write_text("{}", encoding="utf-8")
    proc = cli(via, ["models", "describe", "--caller", "process", "--requirements",
                     str(requirements), "--json", "local"])
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    entry = _json(proc)["rendered_entries"][0]
    assert (entry["id"], entry["reachability"], entry["usable"]) == ("local", "reachable", True)
    assert any(method == "GET" for method, *_ in stub_endpoint.requests)


@pytest.mark.parametrize("via", VIAS)
def test_models_resolve(cli, via):
    proc = cli(via, ["models", "resolve", "--models", "local"])
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    out = _json(proc)
    assert (out["endpoint"], out["backend"], out["model"]) == ("local", "openrouter", "m-local")


@pytest.mark.parametrize("via", VIAS)
def test_completion_complete_calls_the_stub_with_the_key(cli, stub_endpoint, via):
    proc = cli(via, ["completion", "complete", "--models", "local", "--prompt", "hi", "--format", "json"])
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    response = _json(proc)["response"]
    assert response["status"] == "completed" and response["text"] == stub_endpoint.ANSWER
    posts = [r for r in stub_endpoint.requests if r[0] == "POST"]
    assert posts and posts[-1][1].endswith("/chat/completions")
    assert posts[-1][2] == "Bearer fake-key"


_INTERRUPTING = textwrap.dedent(
    r"""
    import json, os, sys
    resolution = os.environ.get("JOB_KIT_INTERRUPT_RESOLUTION")
    if resolution is None:
        request = {"schema": "job-kit.interrupt-request/v1", "kind": "approval",
                   "request_schema": {"type": "object", "required": ["approved"],
                                      "properties": {"approved": {"const": True}}},
                   "payload": {"action": "push release tag"}}
        with open(os.environ["JOB_KIT_INTERRUPT_REQUEST"], "w", encoding="utf-8") as handle:
            json.dump(request, handle)
    else:
        with open(sys.argv[1], "a", encoding="utf-8") as handle:
            handle.write(open(resolution, encoding="utf-8").read() + "\n")
    """
)


@pytest.mark.parametrize("via", VIAS)
def test_jobs_run_resolve_resume_events(cli, closure_venv, via):
    """run -> job waits on an interrupt (exit 4) -> resolve -> resume (exit 0) -> events."""
    work = cli.tmp / "work"
    work.mkdir()
    contract = cli.tmp / "contract.py"
    contract.write_text(_INTERRUPTING, encoding="utf-8")
    log = cli.tmp / "continuations.jsonl"
    jobs = cli.tmp / "jobs.json"
    jobs.write_text(json.dumps({"max_parallel": 1, "jobs": [{
        "id": "release", "prompt": {"user": "release"}, "models": ["local"], "directory": str(work),
        "contract": {"command": [str(support.venv_python(closure_venv)), str(contract), str(log)],
                     "directory": str(work)}}]}), encoding="utf-8")
    store = str(cli.tmp / "ledger" / "runs.sqlite3")

    first = cli(via, ["jobs", "run", str(jobs), "--store", store, "--run-id", "r1", "--timeout", "60"])
    assert first.returncode == 4, (first.stdout, first.stderr)
    assert [j["state"] for j in _json(first)["jobs"]] == ["waiting"]

    resolved = cli(via, ["jobs", "resolve", "r1", "1", "--store", store, "--input", '{"approved": true}'])
    assert resolved.returncode == 0, (resolved.stdout, resolved.stderr)

    second = cli(via, ["jobs", "resume", "r1", "--store", store, "--timeout", "60"])
    assert second.returncode == 0, (second.stdout, second.stderr)
    assert [j["state"] for j in _json(second)["jobs"]] == ["accepted"]
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[0])

    events = cli(via, ["jobs", "events", "r1", "--store", store])
    assert events.returncode == 0, events.stderr
    stream = [json.loads(line) for line in events.stdout.splitlines()]
    assert stream and all(e["schema"].startswith("plugins-kit.execution-event/") for e in stream)
    assert any(e["event"].startswith("interrupt") for e in stream), [e["event"] for e in stream]


# --- each assertion goes red when the closure is broken ----------------------

def _kit_variant(tmp_path, wheels, name, mutate):
    return support.rewrite_wheel(wheels["kit"], tmp_path / name, mutate)


def test_closure_catches_a_wheel_that_imports_bootstrap_lib(local_wheels, tmp_path):
    """Inject `import bootstrap_lib` into an in-process module the surface loads."""
    member = "llm_kit/storage/atomic_json.py"

    def mutate(members):
        members[member] = members[member] + b"\nimport bootstrap_lib\n"

    broken = _kit_variant(tmp_path, local_wheels, "inject", mutate)
    venv = support.make_venv(tmp_path / "v", [str(local_wheels["contracts"]), str(broken)])
    problems = support.surface_problems(_surface(venv, tmp_path))
    assert any("blocked import: bootstrap_lib" in p for p in problems), problems


def test_closure_catches_a_missing_contracts_distribution(local_wheels, tmp_path):
    """Install the kit without katsoft-contracts: the contract imports must fail by name."""
    venv = support.make_venv(tmp_path / "v", [str(local_wheels["kit"])], no_deps=True)
    problems = support.surface_problems(_surface(venv, tmp_path))
    assert any("katsoft_contracts" in p for p in problems), problems
    report = _environment(venv, tmp_path)
    assert any(f"distribution {support.CONTRACTS_DIST} is not installed" in p
               for p in support.environment_problems(report, expected_commits={
                   support.KIT_DIST: local_wheels["commit"],
                   support.CONTRACTS_DIST: local_wheels["commit"]}))


def test_closure_catches_a_linked_plugin_tree(local_wheels, tmp_path):
    """A .pth link to the plugin tree makes bootstrap_lib reachable: the environment check names it."""
    venv = support.make_venv(tmp_path / "v", support.local_requirements(local_wheels, extras=""))
    site = next((venv / "lib").glob("python*/site-packages")) if sys.platform != "win32" \
        else venv / "Lib" / "site-packages"
    (site / "plugin_link.pth").write_text(str(support.REPO / "plugins" / "bootstrap") + "\n", encoding="utf-8")
    problems = support.environment_problems(_environment(venv, tmp_path), expected_commits={
        support.KIT_DIST: local_wheels["commit"], support.CONTRACTS_DIST: local_wheels["commit"]})
    assert any("bootstrap_lib is importable" in p for p in problems), problems
    assert any(".pth links present" in p for p in problems), problems
    assert any("reaches the repository" in p for p in problems), problems


def test_closure_catches_a_commit_that_is_not_the_build_commit(closure_venv, tmp_path):
    wrong = "f" * 40
    problems = support.environment_problems(
        _environment(closure_venv, tmp_path),
        expected_commits={support.KIT_DIST: wrong, support.CONTRACTS_DIST: wrong})
    assert sum("!= build commit" in p for p in problems) == 2, problems


def _mutated(report, edit):
    import copy
    changed = copy.deepcopy(report)
    edit(changed)
    return changed


@pytest.mark.parametrize("label,edit,expect", [
    ("legacy marker", lambda r: r["markers"]["llm_kit"].update(MODE="legacy"), "MODE is 'legacy'"),
    ("wrong dist", lambda r: r["markers"]["katsoft_contracts"].update(DIST="x"), "DIST is 'x'"),
    ("stale version", lambda r: r["markers"]["llm_kit"].update(VERSION="0.0.1"), "VERSION '0.0.1'"),
    ("legacy runtime", lambda r: r["markers"].update(llm_kit_runtime_mode="legacy"), "runtime mode is 'legacy'"),
    ("llm_kit outside site-packages",
     lambda r: r["origins"].update(llm_kit="/elsewhere/llm_kit/__init__.py"), "llm_kit does not load"),
    ("plugin distribution", lambda r: r["distributions"].update({"llm-kit": "0.4.0"}), "unexpected first-party"),
    ("probe error", lambda r: r["errors"].update(llm_kit="ImportError: x"), "llm_kit failed"),
])
def test_environment_check_reports_each_defect(closure_venv, expected_commits, tmp_path, label, edit, expect):
    report = _mutated(_environment(closure_venv, tmp_path), edit)
    problems = support.environment_problems(report, expected_commits=expected_commits)
    assert any(expect in p for p in problems), (label, problems)


def test_surface_check_reports_a_loaded_bootstrap_lib():
    assert support.surface_problems({"_bootstrap_lib_modules": ["bootstrap_lib"]}) == [
        "bootstrap_lib was imported: ['bootstrap_lib']"]
    assert support.surface_problems({"x": {"ok": True}, "_bootstrap_lib_modules": []}) == []


def test_closure_catches_a_legacy_marker(local_wheels, tmp_path):
    """A kit wheel whose marker still says legacy is not a package-mode install."""
    def mutate(members):
        members["llm_kit/_deployment.py"] = members["llm_kit/_deployment.py"].replace(
            b'MODE = "package"', b'MODE = "legacy"')

    broken = _kit_variant(tmp_path, local_wheels, "legacy", mutate)
    venv = support.make_venv(tmp_path / "v", [str(local_wheels["contracts"]), str(broken)])
    problems = support.environment_problems(_environment(venv, tmp_path), expected_commits={
        support.KIT_DIST: local_wheels["commit"], support.CONTRACTS_DIST: local_wheels["commit"]})
    assert any("legacy" in p or "llm_kit_marker failed" in p for p in problems), problems
