# llm-kit cost classes and per-machine consent (S1) -- plan

Status: superseded by [consent-design.md](../shared-lib-packaging/consent-design.md). The class names `free`, `usage` and `spend` in the body below were renamed to `local`, `subscription` and `metered`.

Plan revision: 2 by opus 2026-10-21 after a codex/sol cross-check (verdict UNSOUND on revision 1); source design docs/planning/task-kit-dashboard/databench-integration-review.md.


I changed no files. **[E]** means I checked it (file:line, or a command run on this machine). **[H]** means it is a hypothesis.

### Premises
- **[E] Import layering.** `dispatch` may import only `{completion, registry, adapters}`. `adapters` may import only `{completion, registry}`. `registry` imports nothing (`docs/reference/llm-kit-architecture.md:107-124`). The classifier therefore goes in `registry`, the auth probes in `adapters`, and the filter in `dispatch`.
- **[E] Front-door paid rule.** `is_paid` is `billing_mode != "unmetered"` (`frontdoor/acceptance.py:90-92`). A front door's tiers are transport entries whose `routing.group` equals the group (`acceptance.py:101-105`, `model_endpoints.py:45-51`). `discover_model_entries` returns a dict of `EndpointEntry` (`registry/models.py:206`).
- **[E] Claude's diverting variables** are listed at `claude_bg.py:177-197`, and a variable counts only when set and non-empty.
- **[E] Claude auth state is readable.** On this machine (Claude Code 2.1.307), `claude auth status` prints JSON with `authMethod: "claude.ai"`, `apiProvider: "firstParty"` and `subscriptionType: "max"`.
- **[H] Claude API-key login.** A Console API-key login reports a different `authMethod`. Fail-closed handling covers this case either way.
- **[E] Codex inherits the whole environment.** The codex adapter passes no `env` (`adapters/codex_backend.py:405-420`), and the runner uses `env=None` (`adapters/claude_runner.py:118-129`).
- **[E] The codex binary knows about API-key auth.** The 0.162.0 binary contains the strings `auth.json`, `OPENAI_API_KEY`, `CODEX_API_KEY`, `auth_mode`, `forced_login_method` and `model_provider`.
- **[E] Codex auth on this machine.** `~/.codex/auth.json` has the keys `{auth_mode: str, OPENAI_API_KEY: null, tokens{...}, last_refresh}` with `auth_mode == "chatgpt"`, and `codex login status` prints "Logged in using ChatGPT". I printed no secret values.
- **[H] Codex API-key precedence.** I have not shown how much priority `CODEX_API_KEY`/`OPENAI_API_KEY` in the environment get over a ChatGPT login. Either one being present is therefore treated as metered.
- **[E] Codex config.** `config.toml` is read even outside text-only mode, because `ignore_user_config` is passed only when `text_only` is set (`codex_backend.py:699-718`).
- **[E] Factory and legacy paths.** The factory lower-cases the harness name (`dispatch/factory.py:97-109`). The legacy wrapper reaches `describe` with `merged is None` (`declaration.py:612-625`).
- **[E] Paths are resolved per call.** The home directory is looked up on each call, not at import (`model_endpoints.py:111-118`). New state goes under the llm-kit namespace, because the legacy namespace is kept only for state that already exists (`registry/constants.py:26-44`).
- **[E]** `run` calls `describe` again on every attempt (`declaration.py:977-983`). Requirements are checked after routability (`:600-655`) and probing comes after that (`:667-675`). The Python floor is 3.12, so `tomllib` is available (`pyproject.toml:5`).

### Interfaces

**`registry/cost.py`** (new, pure, layer 0):
```python
FREE, USAGE, SPEND = "free", "usage", "spend"; COST_CLASSES = (FREE, USAGE, SPEND)
SUBSCRIPTION, METERED, UNKNOWN = "subscription", "metered", "unknown"
BILLING_DIVERTING_VARS: tuple[str, ...]          # == claude_bg's 8 names
@dataclass(frozen=True) class HarnessAuth: state: str; reason: str
@dataclass(frozen=True) class CostVerdict: cost_class: str; reason: str
def is_unmetered(entry: EndpointEntry) -> bool
def normalize_allow(allow: Iterable[str] | None) -> frozenset[str] | None  # ValueError on unknown
def classify(entry: EndpointEntry | None, *, entries: Mapping[str, EndpointEntry] | None = None,
             harness_auth: Mapping[str, HarnessAuth] | None = None) -> CostVerdict
def cost_class(entry, **kw) -> str
```
Rules, applied in order:
1. `entry is None` -> spend, reason "no registry entry: billing undeclared".
2. `kind == "harness"` with `h = (entry.harness or "").lower()`:
   - If `h` is `claude` or `codex` and `harness_auth[h].state == SUBSCRIPTION`, the entry is usage.
   - Otherwise (including a missing key) it is spend, with the reason taken from the auth record.
   - opencode or any other harness is spend.
3. `kind == "transport"` with `frontdoor: true`: tiers are the transport entries in `entries` whose `routing.group == entry.model`. The entry is free only if that set is non-empty and every tier is unmetered. Otherwise it is spend, with the reason "no tiers declared for group G" or "tier T metered". The entry's own `billing` does not count.
4. Any other transport is free if it is unmetered, and spend otherwise.
5. Any other kind is spend.

**`adapters/harness_auth.py`** (new; does I/O; never raises):
```python
def claude_auth(env: Mapping[str, str] | None = None, *, runner=subprocess.run, timeout_s: float = 10) -> HarnessAuth
def codex_auth(env: Mapping[str, str] | None = None, *, codex_home: Path | None = None) -> HarnessAuth
def probe_harness_auth(harnesses: Iterable[str], env=None) -> dict[str, HarnessAuth]
```
- **`claude_auth`:** if any diverting variable is set and non-empty, the result is METERED and names the variable. Otherwise it runs `claude auth status`. `authMethod == "claude.ai"` with `apiProvider == "firstParty"` gives SUBSCRIPTION. Any other value gives METERED. A launch failure, timeout or parse failure gives UNKNOWN.
- **`codex_auth`:** a non-empty `CODEX_API_KEY` or `OPENAI_API_KEY` in the environment gives METERED. The auth file is `$CODEX_HOME/auth.json`, or `~/.codex/auth.json` when `CODEX_HOME` is unset. `auth_mode == "chatgpt"` with a null or empty `OPENAI_API_KEY` gives SUBSCRIPTION, but only if `config.toml` (read with tomllib) selects no `model_provider` other than `openai`, at top level or in the active `profile`, and does not set `forced_login_method = "api"`. A missing or unreadable file gives UNKNOWN. Anything else gives METERED. Secrets are never logged, and reasons name fields, not values.

**`registry/consent.py`** (new):
```python
def default_consent_path() -> Path   # Path.home()/".claude/plugins/data/plugins-kit/llm-kit/consent.json", per call
@dataclass(frozen=True) class Consent:
    usage: bool; spend: bool; source: str  # file|absent|unreadable
    path: Path; note: str
    @property allowed -> frozenset[str]
def load_consent(path: Path | None = None) -> Consent      # catches OSError/decode/schema -> free-only + note
def save_consent(*, usage: bool, spend: bool, path: Path | None = None) -> Path  # errors propagate
```
- File format: `{"schema":1,"usage":bool,"spend":bool,"updated_at":iso}`. Only real booleans are accepted.
- Save is atomic: write a tmp file in the same directory, then `Path.replace`.
- Callers that opt in must print `Consent.note` themselves. The floor does not carry it.

**`dispatch/declaration.py` changes:**
- Add `DISPOSITION_SWITCHED_OFF = "switched-off"`. `Disposition` gains `cost_class: Optional[str] = None`, which `to_json` emits only when it is not None.
- `describe(..., allow=None, harness_auth=None)` and `run(..., allow=None)`:
  - `run` normalizes `allow` once through `normalize_allow` and passes the frozenset on every attempt.
  - When `allow` is None, `describe` does no classification and no auth probing.
  - Otherwise `describe` probes auth lazily, once, only for the claude/codex harnesses it meets, unless the caller supplies `harness_auth`. `run` probes once and reuses the result across attempts.
- **Where the check sits:** after the unroutable checks and before the requirements matcher. It calls `classify(merged, entries=entries, harness_auth=...)`. When the class is not allowed, the entry gets `settle(SWITCHED_OFF, f"{cls} not allowed: {reason}")` with its `cost_class` set.
- `Ranking` gains `switched_off: tuple[Disposition, ...] = field(default=(), repr=False)`.
  - When the tuple is non-empty, `render()` adds a line `Switched off: <id> (<class>), ...` and `to_json()` adds a `"switched_off"` key.
  - When it is empty, the output is byte-identical to today.
  - The docstrings must state that switched-off entries are shown, not hidden.
- `NoUsableRoutingTarget` gains a `switched_off_ids` property. Its message is unchanged apart from the per-entry detail.

**`frontdoor/acceptance.py`:** `is_paid(entry)` becomes `not is_unmetered(entry)`.

**CLI:**
- `cli/consent.py`: `llm-kit consent show [--json]` and `consent set [--usage on|off] [--spend on|off]` (partial update; exit 2 if neither flag is given).
- `cli/__init__.py`: add the `"consent"` group and its branch in `_run_group`.
- `cli/models.py`: `models describe --allow free,usage`.

### Units

| Unit | Files owned | Order |
|---|---|---|
| U0 scaffold | stubs with final signatures and constants in `registry/cost.py`, `registry/consent.py`, `adapters/harness_auth.py` (bodies raise `NotImplementedError`) | first, small |
| U1 classifier | `registry/cost.py`, `frontdoor/acceptance.py`, `tests/llm-kit/test_cost_class.py` | after U0 |
| U1b auth probes | `adapters/harness_auth.py`, `tests/llm-kit/test_harness_auth.py` | after U0 |
| U2 consent | `registry/consent.py`, `tests/llm-kit/test_consent.py` | after U0 |
| U3 filter | `dispatch/declaration.py`, `cli/models.py`, `tests/llm-kit/test_allow_filter.py` | after U0; tests inject `harness_auth` and stub `classify` until U1 lands; final run after U1 |
| U4 CLI | `cli/consent.py`, `cli/__init__.py`, `tests/llm-kit/test_cli_consent.py`, `tests/llm-kit/test_cli_groups.py` (pins `GROUPS` at :105) | after U0; final run after U2 |
| U5 release | `plugin.json`, `pyproject.toml`, `plugins/llm-kit/CLAUDE.md`, `README.md` | last |

After U0, units U1, U1b, U2, U3 and U4 can run in parallel, and no two own the same file. There are no new import edges (adapters->registry, dispatch->adapters and registry, frontdoor->registry, cli->all are already allowed).

### Checks, each shown to fail by a stated revert
- **C1** `test_cost_class::test_harness_requires_subscription`: claude or codex with METERED or UNKNOWN auth, or with no auth key, is spend. Revert: default a harness to usage.
- **C2** `::test_harness_name_case`: `"Codex"` with SUBSCRIPTION is usage. Revert: drop `.lower()`.
- **C3** `::test_frontdoor_all_tiers` (entry declared unmetered, one tier with no billing) and `::test_frontdoor_no_tiers_is_spend`. Revert: use the entry's own billing, or default to free.
- **C4** `::test_none_entry_and_unknown_kind_spend`. Revert: classify `None` as a transport.
- **C5** `::test_divert_vars_match_claude_bg`. Revert: add a name to one of the two tuples.
- **C6** `::test_is_paid_delegates`: spy on `is_unmetered`. Revert: inline the rule again.
- **C7** `test_harness_auth::test_claude_divert_var`, `::test_claude_empty_var_ignored`, `::test_claude_status_console_is_metered`, `::test_claude_status_failure_unknown` (injected runner). Revert: skip the env check, use `name in env`, or default to SUBSCRIPTION.
- **C8** `::test_codex_chatgpt_is_subscription`, `::test_codex_env_key_metered`, `::test_codex_auth_mode_apikey_metered`, `::test_codex_provider_override_metered`, `::test_codex_missing_auth_unknown`, `::test_codex_home_honored` (fixtures in a tmp `CODEX_HOME`). Each one reverts its own clause.
- **C9** `::test_reasons_hold_no_secret` (fixture token string never appears in a reason). Revert: include the value in the reason.
- **C10** `test_consent::test_absent_free_only_with_note`, `::test_non_bool_fails_closed`, `::test_decode_error_free_only`. Revert: default to True, use `bool(value)`, or let the exception escape.
- **C11** `::test_path_resolved_per_call` (HOME changed after import). Revert: a module-level constant.
- **C12** `::test_save_atomic`: spy on `Path.replace` and assert a single call to the target; inject a failure before the replace and assert the old content survives. Revert: write straight to the target.
- **C13** `::test_save_error_propagates`. Revert: swallow `OSError`.
- **C14** `test_allow_filter::test_switched_off_shown`: exact render line and JSON containing `cost_class`. Revert: leave the entry out of `switched_off`.
- **C15** `::test_allow_none_byte_identical`: exact `render()`, `to_json()`, floor `to_json()` and `str(exc)` against literals captured before the change, with spies proving that neither `classify` nor any probe is called. Revert: always classify, or always emit the keys.
- **C16** `::test_run_generator_allow_survives_retry`: a generator `allow`, the first attempt halts with a quota error, and the second `describe` still filters. Revert: drop the normalization.
- **C17** `::test_run_floors_all_switched_off` (no backend call). Revert: have `run` drop `allow`.
- **C18** `::test_switched_off_not_probed`: `check_many` spy. Revert: move the check after probing.
- **C19** `test_cli_consent::test_set_partial_preserves_other`, `::test_show_json_absent`. Revert: write unspecified flags as False.
- **C20** existing `test_import_graph.py`. Revert: import `claude_bg` or `adapters` from `registry/cost.py`; it must go red.

### Repo rules
- **Opinion razor:** the only new seam is the consent file, and you ruled on it. Its free-only default applies only to callers that pass `allow`.
- **Write path:** one new user-scoped path, from `default_consent_path()`. It is not derived from the script's location.
- **Shipped model families:** S1 changes no shipped declaration. An opted-in caller running an all-harness declaration with no consent hits the floor, and the caller prints `note`.
- **Consumers in S2 and S3:** probe the `allow` keyword (`inspect.signature`) and `llm_kit.registry.consent.load_consent`, and give different messages for "absent" and "too old" (plugins/CLAUDE.md:430-446).

### Version bump and publishing
- llm-kit goes **0.3.2 -> 0.4.0** in `plugin.json` and `pyproject.toml`. `requires_bootstrap` stays at 0.157.0, because no new `bootstrap_lib` call is added.
- Docs: a "Cost classes and consent" section in llm-kit's CLAUDE.md and README.
- Publish with `uv run python scripts/publish.py` (pre-authorized, CLAUDE.md:5).
- Smoke-test with `claudx`: `llm-kit consent show`, `consent set --usage on`, and `models describe <ids> --allow free,usage`. The last one exercises the real `claude auth status` and codex auth probes.

### Critical files for implementation
- /Users/christina/Dev/plugins-kit/plugins/llm-kit/lib/llm_kit/dispatch/declaration.py
- /Users/christina/Dev/plugins-kit/plugins/llm-kit/lib/llm_kit/registry/model_endpoints.py
- /Users/christina/Dev/plugins-kit/plugins/llm-kit/lib/llm_kit/adapters/codex_backend.py
- /Users/christina/Dev/plugins-kit/plugins/llm-kit/lib/llm_kit/pipeline/execution/drivers/claude_bg.py
- /Users/christina/Dev/plugins-kit/plugins/llm-kit/lib/llm_kit/frontdoor/acceptance.py
- /Users/christina/Dev/plugins-kit/plugins/llm-kit/lib/llm_kit/cli/__init__.py

## Amendment (revision 2a): codex auth through `codex doctor --json`

This section REPLACES the codex premises, the `codex_auth` interface and checks C8-C9 above (codex/sol re-check: findings 1-7 resolved; this fixes its one new blocker, the effective configuration stack).

### Amendment summary

`codex_auth` no longer reads Codex's files itself. It now asks Codex for its own resolved configuration by running `codex doctor --json` (Codex 0.162.0 on this machine) in the same working directory and environment as the run.

- **What doctor reports:** its `config.load` check states its scope as "invocation config, including cloud-managed policy". It also reports the effective `model provider` and the `cwd`, plus the stored auth mode and whether an API key is stored.
- **Layers I found in the codex binary:** mdm, system, user, project (`.codex/config.toml`), session flags, cloud requirements, cloud-managed config, and legacy managed config from a file or MDM. The binary also names `/etc/codex/config.toml`, `/etc/codex/requirements.toml` and `/etc/codex/managed_config.toml`.
- **Why I stopped reading files:** cloud and MDM layers cannot be read from disk, so a file reader cannot cover every provider-affecting layer.
- **The rule:** an auth environment variable gives METERED before doctor runs. Otherwise the result is SUBSCRIPTION only when doctor's report positively shows the `openai` provider, ChatGPT auth and no stored API key. Any other provider or auth mode is METERED. Anything else is UNKNOWN, which counts as spend: doctor missing, a non-zero exit, a timeout, an unknown schema, a missing field, or a check whose status is not ok.
- **The managed/system-provider test you asked for** is C8c below.

This replaces the codex parts of the Premises, the `codex_auth` part of the `adapters/harness_auth.py` interface, and C8--C9 of the checks. Nothing else changes.

---

### Premises (replaces the codex bullets)

- **[E] Codex inherits the whole environment.** The codex adapter passes no `env` (`adapters/codex_backend.py:405-420`), and the runner uses `env=None` (`adapters/claude_runner.py:118-129`).
- **[E] The production argv sets no provider or profile.** It sets `-C <root>`, a model, an effort, and in text-only mode only `features.*=false` plus `model_catalog_json` (`codex_backend.py:699-725`, `adapters/capabilities.py:543-553`).
  - `ignore_user_config` is passed only in text-only mode.
  - **[H]** It drops only the user layer. System, managed and cloud layers still apply.
- **[E] Config layers in the binary.** `strings` on the native binary (`.../codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex`, 0.162.0) shows the tagged enum `ConfigLayerSource`, with the variants `mdm`, `system`, `user`, project (`dotCodexFolder`, `.codex/config.toml`), `sessionFlags`, `cloudRequirements`, `cloudManagedConfig`, `legacyManagedConfigFile` and `legacyManagedConfigMdm`. It also names `/etc/codex/config.toml`, `/etc/codex/requirements.toml`, `/etc/codex/managed_config.toml` and the preferences domain `com.openai.codex`.
- **[E] Auth modes in the binary:** `apiKey`, `chatgpt` and `amazonBedrock`.
- **[E] Auth environment variables in the binary:** `CODEX_API_KEY`, `OPENAI_API_KEY` and `CODEX_ACCESS_TOKEN`, next to `auth.json`.
- **[E] State of this machine.**
  - `/etc/codex` does not exist.
  - The `com.openai.codex` defaults domain exists. I saw only desktop-app Sparkle keys there.
  - `CODEX_HOME` is unset, and `~/.codex` is a symlink to `~/Dev/codex-settings`.
  - `auth.json` has the keys `{auth_mode, OPENAI_API_KEY: null, tokens{id_token, access_token, refresh_token, account_id}, last_refresh}` with `auth_mode == "chatgpt"`.
  - I printed no secret values.
- **[E] Codex can dump its effective configuration.** `codex doctor --json` ("Emit a redacted machine-readable report"; it accepts `-c`) returns JSON with `schemaVersion: 1` and `codexVersion`. Its `checks` include:
  - `config.load`: `status`, plus `details` with `"model provider"`, `"configuration scope"` and `"cwd"`. Here these read `"openai"`, `"invocation config, including cloud-managed policy"` and the process cwd.
  - `auth.credentials`: `status`, plus `details` with `"stored auth mode"` (`"chatgpt"`), `"stored API key"` (`"false"`) and `"stored ChatGPT tokens"` (`"true"`).
  - It took about 4 s here, and `codex login status` agrees ("Logged in using ChatGPT").
- **[E]** The `app-server` protocol has a `config/read` method that returns `layers` (`ConfigReadResponse`). It needs a daemon round-trip, so I do not use it.
- **[H] Doctor may write.** Doctor may make network checks (`network.provider_reachability`) and write logs under `CODEX_HOME`. It changes no configuration.
- **[H] `CODEX_ACCESS_TOKEN`** may carry a ChatGPT token rather than an API key. Its presence is still treated as not proven subscription.

### `adapters/harness_auth.py`: the `codex_auth` part (replaces the earlier spec)

```python
CODEX_AUTH_ENV = ("CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN")
DOCTOR_SCHEMA = 1
def codex_auth(env: Mapping[str, str] | None = None, *, cwd: Path,
               runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
               timeout_s: float = 30.0) -> HarnessAuth
def probe_harness_auth(harnesses: Iterable[str], env: Mapping[str, str] | None = None,
                       *, cwd: Path) -> dict[str, HarnessAuth]
```

The rule is evaluated in order and never raises.

1. **Environment first.** If `CODEX_API_KEY` or `OPENAI_API_KEY` is set and non-empty, the result is METERED. If only `CODEX_ACCESS_TOKEN` is set, the result is UNKNOWN. The reason names the variable, and the runner is not called.
2. **Run doctor.** Run `codex doctor --json` through `runner`, with `cwd=cwd`, the same `env` (default `os.environ`), and `timeout_s`. The run itself passes no `-c` provider or profile override, so doctor gets none either. **If the backend ever adds a `-c` key starting with `model_provider`, `model_providers` or `profile`, it must be forwarded here too. Until then, `codex_auth` assumes no such key.**
3. **Parse the report.** The result is UNKNOWN on a missing executable, a timeout, a non-zero exit, a JSON decode error, `schemaVersion != 1`, or any missing field among those below.
4. **Read the checks.** Let `cl = checks["config.load"]` and `ac = checks["auth.credentials"]`.
   - If `cl.status` or `ac.status` is not `"ok"`, the result is UNKNOWN. The configuration did not load cleanly, so a layer may not have been applied.
   - If `cl.details["model provider"]` is not exactly `"openai"`, the result is METERED, with the reason "effective provider <name>". This covers a provider chosen by any layer: system, project, MDM, cloud-managed or requirements.
   - If `ac.details["stored auth mode"]` is not exactly `"chatgpt"`, or `ac.details["stored API key"]` is not exactly `"false"`, the result is METERED.
   - Otherwise the result is SUBSCRIPTION, with the reason "doctor: provider openai, chatgpt auth (codex <version>)".
5. **Never log secrets.** Reasons quote only field names and the provider or auth-mode value, never a token. The report is already redacted.

**Caching and callers:**
- Results are cached per process, keyed by `(cwd, the values of CODEX_AUTH_ENV, CODEX_HOME)`.
- `describe` passes `cwd=Path(project_root or Path.cwd()).resolve()`.
- `run` passes `request.workspace or project_root or Path.cwd()`, resolved. This matches the backend's `-C` root default (`codex_backend.py:366-373`). `run` probes once per call.
- No file-reading fallback: if doctor is missing or older than this schema, the result is UNKNOWN, which counts as spend.

### Checks (replace C8 and C9)

All of these use an injected `runner` with canned doctor JSON, plus a tmp `CODEX_HOME`.

- **C8a** `test_harness_auth::test_codex_env_key_metered_without_doctor`: `OPENAI_API_KEY=x` gives METERED, and the runner spy is never called. `CODEX_ACCESS_TOKEN=x` gives UNKNOWN. An empty value falls through to doctor. **Revert:** drop the environment step, or use `name in env`.
- **C8b** `::test_codex_doctor_openai_chatgpt_is_subscription`. **Revert:** default to UNKNOWN; this test then goes red, which shows the positive path is real.
- **C8c** `::test_codex_managed_provider_metered` (the managed/system-provider test): the tmp `CODEX_HOME/config.toml` says `model_provider = "openai"` and `auth.json` says chatgpt, but the doctor fixture reports `"model provider": "azure"`, as a `/etc/codex/managed_config.toml` or cloud-managed layer would produce. The expected result is METERED. **Revert:** take the provider from `CODEX_HOME/config.toml` instead of doctor.
- **C8d** `::test_codex_doctor_apikey_auth_metered`: covers `"stored auth mode": "apiKey"`, and separately `"stored API key": "true"`. **Revert:** check only one of the two fields.
- **C8e** `::test_codex_doctor_failure_unknown`, parametrized over: `FileNotFoundError`, `TimeoutExpired`, exit code 1, non-JSON output, `schemaVersion: 2`, a missing `"model provider"`, and `config.load.status == "warn"`. Each must give UNKNOWN. **Revert:** treat a missing field as `openai`, or drop the status gate.
- **C8f** `::test_codex_doctor_uses_run_cwd_and_env`: a runner spy asserts `cwd` equals the run's workspace and `env` is the same mapping. A `run()`-level variant asserts that one probe is made per `run`. **Revert:** call the runner with `cwd=None`.
- **C8g** `::test_codex_cache_key_includes_cwd`: two different `cwd` values cause two probes. **Revert:** cache by `env` alone.
- **C9** `::test_reasons_hold_no_secret`: a fixture token string placed in the `auth.json` and doctor fixtures never appears in any reason. **Revert:** include raw `details` in the reason.
- **C9-live** (optional, behind the existing live marker; not part of CI): on a machine logged in to ChatGPT, the real `codex doctor --json` gives SUBSCRIPTION. On this machine the inputs it reads are already confirmed (provider `openai`, `chatgpt`, stored API key `false`).

### Critical files for implementation
- /Users/christina/Dev/plugins-kit/plugins/llm-kit/lib/llm_kit/adapters/codex_backend.py
- /Users/christina/Dev/plugins-kit/plugins/llm-kit/lib/llm_kit/adapters/capabilities.py
- /Users/christina/Dev/plugins-kit/plugins/llm-kit/lib/llm_kit/adapters/claude_runner.py
- /Users/christina/Dev/plugins-kit/plugins/llm-kit/lib/llm_kit/dispatch/declaration.py
