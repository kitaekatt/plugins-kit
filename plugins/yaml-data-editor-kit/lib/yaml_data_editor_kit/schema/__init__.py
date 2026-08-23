"""The YAML schema dialect: project-independent type and shape declarations.

Two entry points:

- ``load_profile(paths)`` -- read ``type`` / ``view`` / ``source`` documents
  into a ``Profile``. Raises ``ProfileError`` when a declaration is malformed.
- ``validate_corpus(profile, root)`` -- check a data corpus against a loaded
  profile, returning ``Diagnostic`` objects that each name a file, a record and
  a field.
"""

from .adapter import adapt_shape
from .corpus import Corpus, Record, load_corpus, resolve_value_set
from .errors import ADVISORY, Diagnostic, ERROR, ProfileError, errors_only
from .loader import load_profile
from .merge import flatten_type, merge_values
from .model import (
    Adapter,
    Constraint,
    Extensible,
    FieldSpec,
    OpenSpec,
    Ordered,
    Profile,
    SourceSpec,
    TypeSpec,
    Variants,
    ViewEntry,
    ViewSpec,
)
from .validate import Validator, validate, validate_corpus

__all__ = [
    "ADVISORY",
    "Adapter",
    "Constraint",
    "Corpus",
    "Diagnostic",
    "ERROR",
    "Extensible",
    "FieldSpec",
    "OpenSpec",
    "Ordered",
    "Profile",
    "ProfileError",
    "Record",
    "SourceSpec",
    "TypeSpec",
    "Validator",
    "Variants",
    "ViewEntry",
    "ViewSpec",
    "adapt_shape",
    "errors_only",
    "flatten_type",
    "load_corpus",
    "load_profile",
    "merge_values",
    "resolve_value_set",
    "validate",
    "validate_corpus",
]
