"""The in-memory profile: the object model a loaded dialect profile becomes.

Nothing here knows a consuming project's vocabulary. A ``TypeSpec`` is whatever
a profile declared; this module only says what a declaration may contain.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any

# Scalar field types, and the compound ones. A field's ``kind`` is one of
# these, or the empty string for a field declared purely by ``shape_from`` or
# ``partial_of`` (both of which supply a shape instead of naming a type).
SCALAR_KINDS = ("string", "int", "float", "bool", "id", "text")
COMPOUND_KINDS = ("list", "map", "ref", "enum", "record")
FIELD_KINDS = SCALAR_KINDS + COMPOUND_KINDS

# A map key must have a declared legal set.
LEGAL_KEY_KINDS = ("id", "ref", "enum")

STORED_INT = "int"
STORED_STRING = "string"

VIEW_FORMS = ("table", "card", "summary")
SOURCE_LAYOUTS = ("rows", "file_per_record", "keyed_map", "single")
CONSTRAINT_KINDS = ("covers", "matches_files", "unique")


def split_path(path: str) -> list[str]:
    """Split a dotted path into its segments. The separator is '.' for both
    anchored paths (``<type>.<segment>[.<segment>]*``) and field paths
    (``<segment>[.<segment>]*``)."""
    return path.split(".")


@dataclass(frozen=True)
class Ordered:
    """``ordered:`` -- position in this list carries meaning."""

    significance: str


@dataclass(frozen=True)
class Adapter:
    """How to read one foreign schema-as-data language.

    Lives on the type that USES ``shape_from``, because it describes that
    corpus's own type language, not the dialect's.
    """

    type_key: str
    types: dict[str, str]
    cardinality_key: str | None = None


@dataclass
class FieldSpec:
    """One field declaration, at any depth."""

    name: str
    kind: str = ""

    # Annotations.
    required: bool = True
    unit: str | None = None
    meaning: str | None = None
    sentinel: dict[Any, str] = dataclass_field(default_factory=dict)
    derived: str | None = None
    provenance: str | None = None

    # Compound shape.
    of: "FieldSpec | None" = None
    key: "FieldSpec | None" = None
    value: "FieldSpec | None" = None
    fields: "dict[str, FieldSpec]" = dataclass_field(default_factory=dict)
    to: str | None = None

    # enum
    values: list[Any] | None = None
    value_labels: dict[Any, Any] | None = None
    values_from: str | None = None
    stored: str = STORED_STRING

    # key-dependent shape and named override layers
    shape_from: str | None = None
    partial_of: str | None = None
    routes: dict[str, str] = dataclass_field(default_factory=dict)

    # size and cardinality
    length: int | None = None
    min_length: int | None = None
    max_length: int | None = None
    min_chars: int | None = None
    max_chars: int | None = None
    minimum: float | None = None
    maximum: float | None = None

    ordered: Ordered | None = None

    @property
    def is_authored(self) -> bool:
        """A derived field is computed, not authored, so it is never demanded."""
        return self.derived is None

    @property
    def enum_members(self) -> list[Any]:
        """Inline enum members, whichever of the two inline forms was used."""
        if self.value_labels is not None:
            return list(self.value_labels.keys())
        return list(self.values or [])


@dataclass(frozen=True)
class Variants:
    """``variants:`` -- a record shape that depends on one of its own fields."""

    on: str
    when: dict[Any, dict[str, FieldSpec]]


@dataclass(frozen=True)
class Extensible:
    """``extensible:`` -- record-to-record inheritance within one type."""

    via: str
    abstract_flag: str | None = None


@dataclass(frozen=True)
class OpenSpec:
    """``open:`` -- prefixed ad hoc fields, the dialect's only escape hatch."""

    prefix: str
    type: FieldSpec


@dataclass(frozen=True)
class Constraint:
    """One obligation between types. ``why`` is required on every one."""

    kind: str
    why: str
    # covers
    from_path: str | None = None
    to_path: str | None = None
    both_ways: bool = False
    # matches_files / unique
    ids: str | None = None
    files: str | None = None
    scope: str | None = None


@dataclass
class TypeSpec:
    """One ``dialect: type/1`` document."""

    id: str
    title: str | None = None
    identified_by: str | None = None
    fields: dict[str, FieldSpec] = dataclass_field(default_factory=dict)
    variants: Variants | None = None
    extensible: Extensible | None = None
    open: OpenSpec | None = None
    constraints: list[Constraint] = dataclass_field(default_factory=list)
    adapter: Adapter | None = None
    document: Path | None = None

    def declared_fields(self) -> dict[str, FieldSpec]:
        """Every field name this type may carry, before variant selection.

        Includes the two fields ``extensible:`` declares -- it declares them,
        so a profile must not restate them under ``fields:``.
        """
        out: dict[str, FieldSpec] = dict(self.fields)
        if self.extensible is not None:
            out[self.extensible.via] = FieldSpec(
                name=self.extensible.via, kind="ref", to=self.id, required=False
            )
            if self.extensible.abstract_flag is not None:
                out[self.extensible.abstract_flag] = FieldSpec(
                    name=self.extensible.abstract_flag, kind="bool", required=False
                )
        return out

    def fields_for(self, discriminator_value: Any) -> dict[str, FieldSpec]:
        """The declared fields plus whatever the discriminator value adds."""
        out = self.declared_fields()
        if self.variants is not None:
            added = self.variants.when.get(discriminator_value)
            if added:
                out.update(added)
        return out

    def every_possible_field(self) -> dict[str, FieldSpec]:
        """The union over every variant -- what a view may legally name."""
        out = self.declared_fields()
        if self.variants is not None:
            for added in self.variants.when.values():
                out.update(added)
        return out


@dataclass(frozen=True)
class PathKeyStep:
    """One map-key step recorded while the loader walks a declared path."""

    segment: str
    map_path: str
    anchored_map_path: str
    key_spec: FieldSpec | None


@dataclass(frozen=True)
class PathWalk:
    """The result and map-key obligations of one declared path use."""

    type_id: str
    segments: tuple[str, ...]
    path: str
    anchored_path: str
    field: FieldSpec | None
    synthetic_identity: bool
    key_steps: tuple[PathKeyStep, ...]
    where: str
    document: Path | None
    value_set_owner: FieldSpec | None = None


@dataclass(frozen=True)
class ViewEntry:
    """One line of a ``view``'s ordered ``fields:`` list."""

    field: str | None = None
    computed: str | None = None
    label: str | None = None
    format: str | None = None
    link: bool = False
    group: str | None = None
    when: Any = None
    from_expr: str | None = None

    @property
    def name(self) -> str:
        return self.field or self.computed or ""


@dataclass
class ViewSpec:
    """One ``dialect: view/1`` document."""

    id: str
    of: str
    form: str
    entries: list[ViewEntry] = dataclass_field(default_factory=list)
    covers: str | None = None
    document: Path | None = None

    def field_names(self) -> list[str]:
        return [e.field for e in self.entries if e.field is not None]


@dataclass
class SourceSpec:
    """One ``dialect: source/1`` document."""

    of: str
    layout: str
    path: str
    key: str | None = None
    generated_by: str | None = None
    record_keys: list[str] | None = None
    record_keys_from: str | None = None
    metadata_keys: list[str] | None = None
    document: Path | None = None


@dataclass
class Profile:
    """A loaded profile: every type, view and source it declares."""

    types: dict[str, TypeSpec] = dataclass_field(default_factory=dict)
    views: dict[str, ViewSpec] = dataclass_field(default_factory=dict)
    sources: list[SourceSpec] = dataclass_field(default_factory=list)
    path_walks: list[PathWalk] = dataclass_field(default_factory=list)

    def sources_for(self, type_id: str) -> list[SourceSpec]:
        return [s for s in self.sources if s.of == type_id]
