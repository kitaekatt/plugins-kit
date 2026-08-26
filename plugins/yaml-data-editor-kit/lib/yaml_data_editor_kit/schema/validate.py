"""Validate a data corpus against a loaded profile.

Every finding is a ``Diagnostic`` naming the file, the record and the field.
That is the whole contract of this module: a validator that reports "type
error" without saying where is a validator nobody can act on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .adapter import adapt_shape
from .corpus import (
    ABSENT,
    check_path_key_steps,
    Corpus,
    load_corpus,
    Record,
    resolve_path_value,
    resolve_value_set,
)
from .errors import ADVISORY, Diagnostic, ERROR
from .loader import load_profile, resolve_field_path
from .merge import flatten_type
from .model import FieldSpec, Profile, split_path, STORED_INT, TypeSpec

_MAX_LISTED_VALUES = 12


def validate(profile_paths: Any, corpus_root: Path) -> list[Diagnostic]:
    """Load a profile and validate the corpus rooted at ``corpus_root``."""
    profile = load_profile(profile_paths)
    return validate_corpus(profile, corpus_root)


def validate_corpus(profile: Profile, corpus_root: Path) -> list[Diagnostic]:
    """Validate the corpus at ``corpus_root`` against an already-loaded profile."""
    corpus = load_corpus(profile, corpus_root)
    return Validator(profile, corpus).run()


class Validator:
    """One validation pass over one corpus."""

    def __init__(self, profile: Profile, corpus: Corpus) -> None:
        self.profile = profile
        self.corpus = corpus
        self.diagnostics: list[Diagnostic] = []
        self._value_sets: dict[str, list[Any]] = {}

    # -- entry point ------------------------------------------------------

    def run(self) -> list[Diagnostic]:
        check_path_key_steps(self.profile, self.corpus)
        for type_spec in self.profile.types.values():
            flattened: dict[str, dict[str, Any]] = {}
            if type_spec.value is None:
                flattened, merge_diagnostics = flatten_type(
                    type_spec, self.corpus
                )
                self.diagnostics.extend(merge_diagnostics)
            for record in self.corpus.of_type(type_spec.id):
                data = record.data
                if record.identity is not None and record.identity in flattened:
                    data = flattened[record.identity]
                self._check_record(type_spec, record, data)
            for constraint_index, _ in enumerate(type_spec.constraints):
                self._check_constraint(type_spec, constraint_index)
        self._check_views()
        # Corpus loading includes declared-path checking and owns that
        # diagnostic channel; prefix it to record-level validator findings.
        self.diagnostics = self.corpus.diagnostics + self.diagnostics
        return self.diagnostics

    # -- reporting --------------------------------------------------------

    def _report(
        self,
        message: str,
        record: Record,
        field: str | None = None,
        severity: str = ERROR,
    ) -> None:
        self.diagnostics.append(
            Diagnostic(
                message=message,
                file=record.file,
                record=record.label,
                field=field,
                severity=severity,
            )
        )

    # -- records ----------------------------------------------------------

    def _check_record(self, type_spec: TypeSpec, record: Record, data: Any) -> None:
        if type_spec.value is not None:
            if record.identity is None:
                if not isinstance(data, dict):
                    self._report(
                        'document metadata is not a mapping of fields',
                        record,
                    )
                    return
                self._check_shape(
                    data,
                    type_spec.fields,
                    type_spec,
                    record,
                    prefix='',
                    open_allowed=False,
                )
            else:
                self._check_value(
                    data,
                    type_spec.value,
                    type_spec,
                    record,
                    'value',
                )
            return
        if not isinstance(data, dict):
            self._report("is not a mapping of fields", record)
            return

        discriminator = None
        if type_spec.variants is not None:
            discriminator = data.get(type_spec.variants.on)
        fields = type_spec.fields_for(discriminator)

        self._check_shape(data, fields, type_spec, record, prefix="", open_allowed=True)

    def _check_shape(
        self,
        data: dict[str, Any],
        fields: dict[str, FieldSpec],
        owner: TypeSpec,
        record: Record,
        prefix: str,
        open_allowed: bool,
    ) -> None:
        """Check one mapping against a set of field declarations."""
        for key, value in data.items():
            name = str(key)
            path = prefix + name
            spec = fields.get(name)
            if spec is not None:
                if value is None:
                    continue
                self._check_value(value, spec, owner, record, path)
                continue
            if open_allowed and owner.open is not None and name.startswith(owner.open.prefix):
                self._report(
                    "is an ad hoc '{0}' field, permitted by 'open:' -- a growing set of "
                    "them is the signal that a real field is waiting to be "
                    "declared".format(owner.open.prefix),
                    record,
                    path,
                    severity=ADVISORY,
                )
                if value is not None:
                    self._check_value(value, owner.open.type, owner, record, path)
                continue
            self._report(
                "is not a field type '{0}' declares".format(owner.id), record, path
            )

        for name, spec in fields.items():
            if name in data:
                value = data[name]
                # An explicit `null` is PRESENT, not absent, exactly when the
                # field's sentinel set declares `null` as a member: the corpus
                # is asserting the sentinel's meaning, not staying silent. A
                # `null` with no such declaration stays ABSENT, same as today.
                if value is not None or None in spec.sentinel:
                    continue
            if spec.required and spec.is_authored:
                self._report(
                    "is required but absent", record, prefix + name
                )

    # -- values -----------------------------------------------------------

    def _check_value(
        self, value: Any, spec: FieldSpec, owner: TypeSpec, record: Record, path: str
    ) -> None:
        if spec.partial_of is not None:
            self._check_partial(value, spec, record, path)
            return
        if spec.shape_from is not None:
            self._report(
                "'shape_from:' is only meaningful as a map value, where the key names "
                "the record supplying the shape",
                record,
                path,
            )
            return

        kind = spec.kind
        if kind in ("string", "id", "text"):
            if not isinstance(value, str):
                self._wrong_type(value, kind, record, path)
                return
            self._check_chars(value, spec, record, path)
        elif kind == "int":
            if not _is_int(value):
                self._wrong_type(value, kind, record, path)
                return
            self._check_range(value, spec, record, path)
        elif kind == "float":
            if not _is_number(value):
                self._wrong_type(value, kind, record, path)
                return
            self._check_range(value, spec, record, path)
        elif kind == "bool":
            if not isinstance(value, bool):
                self._wrong_type(value, kind, record, path)
        elif kind == "enum":
            self._check_enum(value, spec, record, path)
        elif kind == "ref":
            self._check_ref(value, spec, record, path)
        elif kind == "list":
            self._check_list(value, spec, owner, record, path)
        elif kind == "map":
            self._check_map(value, spec, owner, record, path)
        elif kind == "record":
            if not isinstance(value, dict):
                self._wrong_type(value, "record", record, path)
                return
            self._check_shape(
                value, spec.fields, owner, record, prefix=path + ".", open_allowed=False
            )

    def _wrong_type(self, value: Any, kind: str, record: Record, path: str) -> None:
        self._report(
            "is declared '{0}' but holds {1!r}".format(kind, value), record, path
        )

    def _check_chars(self, value: str, spec: FieldSpec, record: Record, path: str) -> None:
        if spec.max_chars is not None and len(value) > spec.max_chars:
            self._report(
                "holds {0} characters, above the declared max_chars of {1}".format(
                    len(value), spec.max_chars
                ),
                record,
                path,
            )
        if spec.min_chars is not None and len(value) < spec.min_chars:
            self._report(
                "holds {0} characters, below the declared min_chars of {1}".format(
                    len(value), spec.min_chars
                ),
                record,
                path,
            )

    def _check_range(self, value: Any, spec: FieldSpec, record: Record, path: str) -> None:
        if value in spec.sentinel:
            return
        if spec.minimum is not None and value < spec.minimum:
            self._report(
                "holds {0!r}, below the declared min of {1}".format(value, spec.minimum),
                record,
                path,
            )
        if spec.maximum is not None and value > spec.maximum:
            self._report(
                "holds {0!r}, above the declared max of {1}".format(value, spec.maximum),
                record,
                path,
            )

    def _check_size(self, size: int, spec: FieldSpec, record: Record, path: str) -> None:
        if spec.length is not None and size != spec.length:
            self._report(
                "holds {0} entries, but exactly {1} are declared".format(size, spec.length),
                record,
                path,
            )
        if spec.min_length is not None and size < spec.min_length:
            self._report(
                "holds {0} entries, below the declared min_length of {1}".format(
                    size, spec.min_length
                ),
                record,
                path,
            )
        if spec.max_length is not None and size > spec.max_length:
            self._report(
                "holds {0} entries, above the declared max_length of {1}".format(
                    size, spec.max_length
                ),
                record,
                path,
            )

    def _check_list(
        self, value: Any, spec: FieldSpec, owner: TypeSpec, record: Record, path: str
    ) -> None:
        if not isinstance(value, list):
            self._wrong_type(value, "list", record, path)
            return
        self._check_size(len(value), spec, record, path)
        if spec.of is None:
            return
        for index, item in enumerate(value):
            self._check_value(item, spec.of, owner, record, "{0}[{1}]".format(path, index))

    def _check_map(
        self, value: Any, spec: FieldSpec, owner: TypeSpec, record: Record, path: str
    ) -> None:
        if not isinstance(value, dict):
            self._wrong_type(value, "map", record, path)
            return
        self._check_size(len(value), spec, record, path)
        if spec.total and spec.key is not None:
            members = self._legal_values(spec.key)
            if spec.key.kind == 'ref':
                set_label = 'declared set of ref type {0!r}'.format(
                    spec.key.to
                )
            elif spec.key.values_from is not None:
                set_label = 'declared set {0!r}'.format(
                    spec.key.values_from
                )
            else:
                set_label = 'declared inline enum set'
            for member in members:
                if member not in value:
                    self._report(
                        'map {0!r} is missing key {1!r} from {2}'.format(
                            path, member, set_label
                        ),
                        record,
                        path,
                    )
        if spec.key is None or spec.value is None:
            return
        for key, entry in value.items():
            key_path = "{0}.{1}".format(path, key)
            self._check_key(key, spec.key, record, key_path)
            if spec.value.shape_from is not None:
                self._check_shaped_value(key, entry, spec, owner, record, key_path)
            elif entry is not None:
                self._check_value(entry, spec.value, owner, record, key_path)

    def _check_key(self, key: Any, spec: FieldSpec, record: Record, path: str) -> None:
        if spec.kind == "id":
            if not isinstance(key, str):
                self._report(
                    "has key {0!r}, but the key is declared 'id'".format(key), record, path
                )
        elif spec.kind == "ref":
            self._check_ref(key, spec, record, path, subject="key")
        elif spec.kind == "enum":
            self._check_enum(key, spec, record, path, subject="key")

    def _check_shaped_value(
        self,
        key: Any,
        entry: Any,
        spec: FieldSpec,
        owner: TypeSpec,
        record: Record,
        path: str,
    ) -> None:
        """A map value whose shape comes from the record its own key names."""
        assert spec.value is not None and spec.value.shape_from is not None
        type_id, *field_path = spec.value.shape_from.split(".")
        source_record = self.corpus.find(type_id, key if isinstance(key, str) else str(key))
        if source_record is None:
            self._report(
                "takes its shape from '{0}', but no record of type '{1}' has that "
                "identity".format(spec.value.shape_from, type_id),
                record,
                path,
            )
            return
        if owner.adapter is None:
            self._report(
                "uses 'shape_from:' but type '{0}' declares no 'adapter:'".format(owner.id),
                record,
                path,
            )
            return
        target = self.profile.types.get(type_id)
        shape_value = resolve_path_value(target, source_record.data, field_path)
        if shape_value is ABSENT:
            shape_value = None
        fields, problems = adapt_shape(owner.adapter, shape_value)
        for problem in problems:
            self._report(
                "cannot take its shape from record '{0}' of type '{1}': {2}".format(
                    source_record.label, type_id, problem
                ),
                record,
                path,
            )
        if not isinstance(entry, dict):
            self._wrong_type(entry, "record", record, path)
            return
        self._check_shape(
            entry, fields, owner, record, prefix=path + ".", open_allowed=False
        )

    def _check_partial(
        self, value: Any, spec: FieldSpec, record: Record, path: str
    ) -> None:
        assert spec.partial_of is not None
        target = self.profile.types[spec.partial_of]
        if not isinstance(value, dict):
            self._wrong_type(value, "partial_of " + spec.partial_of, record, path)
            return
        available = target.every_possible_field()
        for key, entry in value.items():
            name = str(key)
            key_path = "{0}.{1}".format(path, name)
            if name in available:
                if entry is not None:
                    self._check_value(entry, available[name], target, record, key_path)
                continue
            route = spec.routes.get(name)
            if route is None:
                self._report(
                    "is neither a field of type '{0}' nor a declared route -- there is "
                    "no blanket 'allow anything else' flag, so a routed key must be "
                    "named".format(spec.partial_of),
                    record,
                    key_path,
                )
                continue
            routed_type_id = route.split(".", 1)[0]
            routed_type = self.profile.types[routed_type_id]
            routed_path = resolve_field_path(self.profile, route)
            routed_spec = routed_path.field
            if routed_spec is None:
                if not routed_path.synthetic_identity or routed_type.identified_by is None:
                    raise RuntimeError(
                        "resolved route '{0}' has neither a field nor a synthetic "
                        "identity".format(route)
                    )
                routed_spec = FieldSpec(name=routed_type.identified_by, kind="id")
            if entry is not None:
                self._check_value(entry, routed_spec, routed_type, record, key_path)

    # -- enum and ref -----------------------------------------------------

    def _legal_values(self, spec: FieldSpec) -> list[Any]:
        if spec.kind == 'ref' and spec.to is not None:
            return list(self.corpus.identities(spec.to))
        if spec.values_from is not None:
            if spec.values_from not in self._value_sets:
                self._value_sets[spec.values_from] = resolve_value_set(
                    self.profile, self.corpus, spec.values_from
                )
            return self._value_sets[spec.values_from]
        return spec.enum_members

    def _check_enum(
        self, value: Any, spec: FieldSpec, record: Record, path: str, subject: str = "holds"
    ) -> None:
        if spec.values_from is None and spec.stored == STORED_INT and not _is_int(value):
            self._report(
                "{0} {1!r}, but the enum is 'stored: int'".format(
                    "has key" if subject == "key" else "holds", value
                ),
                record,
                path,
            )
            return
        legal = self._legal_values(spec)
        if value in legal:
            return
        self._report(
            "{0} {1!r}, which is not one of the declared values ({2})".format(
                "has key" if subject == "key" else "holds", value, _listed(legal)
            ),
            record,
            path,
        )

    def _check_ref(
        self, value: Any, spec: FieldSpec, record: Record, path: str, subject: str = "holds"
    ) -> None:
        if spec.to is None:
            return
        identities = self.corpus.identities(spec.to)
        if str(value) in identities:
            return
        self._report(
            "{0} {1!r}, which names no record of type '{2}'".format(
                "has key" if subject == "key" else "holds", value, spec.to
            ),
            record,
            path,
        )

    # -- constraints ------------------------------------------------------

    def _locate(self, path: str, value: Any) -> tuple[str, str | None]:
        """The file and record a value of a ``<type>.<segment>*`` path came from."""
        type_id, *field_path = path.split(".")
        target = self.profile.types.get(type_id)
        for record in self.corpus.of_type(type_id):
            candidate = resolve_path_value(target, record.data, field_path)
            if candidate is ABSENT:
                candidate = record.identity
            if candidate == value or (isinstance(candidate, list) and value in candidate):
                return record.file, record.label
        return self._profile_file(type_id), None

    def _profile_file(self, type_id: str) -> str:
        type_spec = self.profile.types.get(type_id)
        if type_spec is not None and type_spec.document is not None:
            return type_spec.document.name
        return "<profile>"

    def _check_constraint(self, type_spec: TypeSpec, index: int) -> None:
        constraint = type_spec.constraints[index]
        if constraint.kind == "covers":
            assert constraint.from_path is not None and constraint.to_path is not None
            self._check_covers(constraint.from_path, constraint.to_path, constraint.why)
            if constraint.both_ways:
                self._check_covers(constraint.to_path, constraint.from_path, constraint.why)
        elif constraint.kind == "matches_files":
            assert constraint.ids is not None and constraint.files is not None
            self._check_matches_files(constraint.ids, constraint.files, constraint.why)
        elif constraint.kind == "unique":
            assert constraint.ids is not None
            self._check_unique(constraint.ids, constraint.scope, constraint.why)

    def _set(self, path: str) -> list[Any]:
        if path not in self._value_sets:
            self._value_sets[path] = resolve_value_set(self.profile, self.corpus, path)
        return self._value_sets[path]

    def _check_covers(self, from_path: str, to_path: str, why: str) -> None:
        counterparts = set(_hashable(v) for v in self._set(to_path))
        for value in self._set(from_path):
            if _hashable(value) in counterparts:
                continue
            file_name, record_label = self._locate(from_path, value)
            self.diagnostics.append(
                Diagnostic(
                    "'{0}' has no counterpart in '{1}': {2}".format(value, to_path, why),
                    file_name,
                    record=record_label,
                    field=from_path,
                )
            )

    def _check_matches_files(self, ids_path: str, glob: str, why: str) -> None:
        files = {p.stem: p for p in sorted(self.corpus.root.glob(glob)) if p.is_file()}
        declared = [str(v) for v in self._set(ids_path)]
        for value in declared:
            if value in files:
                continue
            file_name, record_label = self._locate(ids_path, value)
            self.diagnostics.append(
                Diagnostic(
                    "'{0}' matches no file under '{1}': {2}".format(value, glob, why),
                    file_name,
                    record=record_label,
                    field=ids_path,
                )
            )
        for stem, path in files.items():
            if stem in declared:
                continue
            self.diagnostics.append(
                Diagnostic(
                    "matches '{0}' but is named by no value of '{1}': {2}".format(
                        glob, ids_path, why
                    ),
                    _relative_name(path, self.corpus.root),
                    record=stem,
                    field=ids_path,
                )
            )

    def _check_unique(self, ids_path: str, scope: str | None, why: str) -> None:
        seen: set[Any] = set()
        duplicated: list[Any] = []
        for value in self._set(ids_path):
            marker = _hashable(value)
            if marker in seen and value not in duplicated:
                duplicated.append(value)
            seen.add(marker)
        where = " within {0}".format(scope) if scope else ""
        for value in duplicated:
            file_name, record_label = self._locate(ids_path, value)
            self.diagnostics.append(
                Diagnostic(
                    "'{0}' appears more than once in '{1}'{2}: {3}".format(
                        value, ids_path, where, why
                    ),
                    file_name,
                    record=record_label,
                    field=ids_path,
                )
            )

    # -- views ------------------------------------------------------------

    def _check_views(self) -> None:
        for view in self.profile.views.values():
            if view.covers is None:
                continue
            covered = self.profile.views[view.covers]
            shown = [split_path(name) for name in view.field_names()]
            for name in covered.field_names():
                candidate = split_path(name)
                if any(_is_ancestor_or_self(prefix, candidate) for prefix in shown):
                    continue
                self.diagnostics.append(
                    Diagnostic(
                        "declares 'covers: {0}' but does not show a field that view "
                        "shows".format(view.covers),
                        view.document.name if view.document is not None else "<profile>",
                        record=view.id,
                        field=name,
                    )
                )


def _is_ancestor_or_self(prefix: list[str], candidate: list[str]) -> bool:
    """``prefix`` covers ``candidate`` if it names the same path or an
    ancestor of it -- a view naming 'weapon_stats' covers one naming
    'weapon_stats.damage', because showing the map shows the key."""
    return len(prefix) <= len(candidate) and candidate[: len(prefix)] == prefix


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _hashable(value: Any) -> Any:
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value


def _listed(values: list[Any]) -> str:
    shown = [repr(v) for v in values[:_MAX_LISTED_VALUES]]
    if len(values) > _MAX_LISTED_VALUES:
        shown.append("...")
    return ", ".join(shown) if shown else "the set is empty"


def _relative_name(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name
