"""Load and validate the two declarations: secrets.json and manifest.json.

Two files, deliberately separate:

- ``~/.claude/secrets.json`` (private, tracked in claude-settings) says WHERE
  the repo is and WHICH machine gets WHICH profiles. Instance data.
- ``manifest.json`` (inside the secrets repo, beside the blobs) says what each
  entry IS and where it lands. It ships with the blobs so the mapping can
  never drift from the ciphertext it describes.

Neither ever contains a secret value.
"""

import json
import os
import re
import socket
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import SecretsError, cli_command

# Both spellings: ${VAR} and bare $VAR. The bare form matters because the
# natural way to write a fleet-wide root is "$DEVROOT/christina-norman", and a
# resolver that silently ignored it would materialize a secret into a directory
# literally named "$DEVROOT".
_VAR_PATTERN = re.compile(
    r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))"
)

# Variables may reference variables (KNOWLEDGE_BANK = "$DEVROOT/bank"), so
# expansion iterates to a fixpoint. Bounded, because a cycle must fail loudly
# rather than spin: a self-referential path is a manifest bug, not input to
# tolerate.
_MAX_EXPANSION_PASSES = 10

# Mirrors the engine's own host resolution: exact hostname first, then the
# domain-stripped short form. Keeping the rule identical is what lets
# secrets.json key on the SAME names as env.json's machines registry instead
# of inventing a second machine vocabulary.
def resolve_host() -> List[str]:
    """Candidate machine keys for this host, most specific first."""
    full = socket.gethostname()
    candidates = [full]
    short = full.split(".")[0]
    if short != full:
        candidates.append(short)
    return candidates


def _read_json(path: Path, what: str) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise SecretsError(f"cannot read {what} at {path}: {e}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SecretsError(
            f"{what} at {path} is not valid JSON: {e}",
            "Fix the syntax; secrets-kit will converge on the next pass.",
        )
    if not isinstance(data, dict):
        raise SecretsError(f"{what} at {path} must be a JSON object")
    return data


class Config:
    """The machine-facing half: repo URL, variables, and this host's profiles."""

    def __init__(self, path: Path, data: Dict[str, Any]) -> None:
        self.path = path
        self.repo: str = data.get("repo", "")
        self.vars: Dict[str, str] = dict(data.get("vars") or {})
        self.machines: Dict[str, Any] = dict(data.get("machines") or {})
        if not self.repo:
            raise SecretsError(
                f"{path} declares no 'repo'",
                "Add the fleet-secrets clone URL, e.g. "
                '"repo": "git@github.com:<account>/fleet-secrets.git".',
            )

    @classmethod
    def load(cls, path: Path) -> Optional["Config"]:
        """Return None when the file is absent -- that is a valid no-op state.

        A third party who installs this plugin without declaring anything must
        get silence, not an error: the service is inert until configured.
        """
        if not path.is_file():
            return None
        return cls(path, _read_json(path, "secrets.json"))

    def machine_key(self) -> Optional[str]:
        """This host's key in the machines block, or None if unlisted.

        Unlisted is NOT an error: subsetting by omission is how a machine opts
        out of holding secrets it has no business holding.
        """
        for candidate in resolve_host():
            if candidate in self.machines:
                return candidate
        return None

    def profiles_for(self, machine_key: str) -> List[str]:
        entry = self.machines.get(machine_key) or {}
        profiles = entry.get("profiles") or []
        if not isinstance(profiles, list):
            raise SecretsError(
                f"{self.path}: machines.{machine_key}.profiles must be a list"
            )
        return [str(p) for p in profiles]

    def vars_for(self, machine_key: str) -> Dict[str, str]:
        """Global vars overlaid with this machine's vars.

        Per-machine wins: the knowledge bank lives at a different absolute path
        on Windows than on the Mac, and that difference belongs in the machine
        block rather than in the shared manifest.
        """
        merged = dict(self.vars)
        entry = self.machines.get(machine_key) or {}
        merged.update(entry.get("vars") or {})
        return merged


class Entry:
    """One materializable secret: a blob, a destination, and a mode."""

    def __init__(self, name: str, data: Dict[str, Any]) -> None:
        self.name = name
        self.blob: str = data.get("blob", "")
        self.dest_spec: Any = data.get("dest", "")
        self.mode: int = _parse_mode(name, data.get("mode", "0600"))
        self.newline: Optional[str] = data.get("newline")
        self.doc: str = data.get("doc", "")
        if not self.blob:
            raise SecretsError(f"manifest entry '{name}' declares no 'blob'")
        if not self.dest_spec:
            raise SecretsError(f"manifest entry '{name}' declares no 'dest'")
        if self.newline not in (None, "lf"):
            raise SecretsError(
                f"manifest entry '{name}': newline must be 'lf' if present"
            )

    def dest(self, variables: Dict[str, str]) -> Path:
        """Resolve the destination path for this machine.

        ``dest`` may be a plain string or a per-OS object; the object form
        exists only for the rare path no single variable can express.
        """
        spec = self.dest_spec
        if isinstance(spec, dict):
            key = "windows" if os.name == "nt" else "default"
            raw = spec.get(key) or spec.get("default")
            if not raw:
                raise SecretsError(
                    f"manifest entry '{self.name}': dest object has no "
                    f"'{key}' or 'default' key"
                )
        else:
            raw = spec
        return Path(expand(str(raw), variables, where=f"entry '{self.name}'"))


def _parse_mode(name: str, raw: Any) -> int:
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw), 8)
    except ValueError:
        raise SecretsError(
            f"manifest entry '{name}': mode '{raw}' is not an octal string"
        )


def expand(value: str, variables: Dict[str, str], *, where: str) -> str:
    """Expand ``${VAR}`` / ``$VAR`` to a fixpoint, then ``~``.

    Variables resolve from secrets.json first and the process environment
    second, so a fleet-wide default can be declared once yet still be
    overridden by a machine that exports its own. An unresolvable variable is
    a hard error naming the entry -- silently materializing a secret to a path
    containing a literal ``$VAR`` would be worse than failing.

    Iterating to a fixpoint is what makes the natural declaration work:
    ``KNOWLEDGE_BANK = "$DEVROOT/christina-norman"`` referenced as
    ``${KNOWLEDGE_BANK}/secrets/x`` needs two passes, and a single-pass
    resolver would leave ``$DEVROOT`` in the path.
    """

    def _sub(match: "re.Match[str]") -> str:
        key = match.group(1) or match.group(2)
        if key in variables:
            return variables[key]
        env = os.environ.get(key)
        if env is not None:
            return env
        raise SecretsError(
            f"{where}: cannot resolve ${{{key}}}",
            f"Declare '{key}' in secrets.json vars (globally or under this "
            f"machine) or export it in the environment.",
        )

    current = value
    for _ in range(_MAX_EXPANSION_PASSES):
        expanded = _VAR_PATTERN.sub(_sub, current)
        if expanded == current:
            return os.path.expanduser(expanded)
        current = expanded

    raise SecretsError(
        f"{where}: variable expansion did not settle after "
        f"{_MAX_EXPANSION_PASSES} passes ('{value}')",
        "This usually means a variable refers to itself, directly or through "
        "another. Break the cycle in secrets.json vars.",
    )


class Manifest:
    """The repo-side declaration: recipient, profiles, and entries."""

    def __init__(self, path: Path, data: Dict[str, Any]) -> None:
        self.path = path
        self.version = data.get("version", 1)
        self.recipient: str = data.get("recipient", "")
        self.profiles: Dict[str, List[str]] = dict(data.get("profiles") or {})
        raw_entries = data.get("entries") or {}
        if not isinstance(raw_entries, dict):
            raise SecretsError(f"{path}: 'entries' must be an object")
        self.entries: Dict[str, Entry] = {
            name: Entry(name, value) for name, value in raw_entries.items()
        }
        if not self.recipient:
            raise SecretsError(
                f"{path} declares no 'recipient'",
                "The manifest must carry the age public key its blobs are "
                f"encrypted to. Re-run `{cli_command('init')}` if this repo "
                f"was never seeded.",
            )
        self._validate_profiles()

    def _validate_profiles(self) -> None:
        for profile, names in self.profiles.items():
            if not isinstance(names, list):
                raise SecretsError(
                    f"{self.path}: profile '{profile}' must be a list of "
                    f"entry names"
                )
            for name in names:
                if name not in self.entries:
                    raise SecretsError(
                        f"{self.path}: profile '{profile}' names unknown "
                        f"entry '{name}'",
                        "Either add the entry or drop it from the profile; a "
                        "dangling name would silently materialize nothing.",
                    )

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        return cls(path, _read_json(path, "manifest.json"))

    def select(self, profiles: List[str]) -> List[Entry]:
        """Entries this machine should hold: the UNION of its profiles.

        Unknown profile names are a hard error rather than an empty set --
        a typo that silently provisions nothing is the failure mode most
        likely to be mistaken for "it worked".
        """
        selected: Dict[str, Entry] = {}
        for profile in profiles:
            if profile not in self.profiles:
                raise SecretsError(
                    f"unknown profile '{profile}'",
                    f"secrets.json assigns it to this machine but "
                    f"{self.path} defines only: "
                    f"{', '.join(sorted(self.profiles)) or '(none)'}.",
                )
            for name in self.profiles[profile]:
                selected[name] = self.entries[name]
        return [selected[name] for name in sorted(selected)]

    def dump(self) -> str:
        return json.dumps(
            {
                "version": self.version,
                "recipient": self.recipient,
                "profiles": self.profiles,
                "entries": {
                    name: _entry_dict(entry)
                    for name, entry in sorted(self.entries.items())
                },
            },
            indent=2,
        ) + "\n"


def _entry_dict(entry: Entry) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "blob": entry.blob,
        "dest": entry.dest_spec,
        "mode": format(entry.mode, "04o"),
    }
    if entry.newline:
        out["newline"] = entry.newline
    if entry.doc:
        out["doc"] = entry.doc
    return out
