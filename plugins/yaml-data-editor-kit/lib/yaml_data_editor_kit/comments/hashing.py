'''Canonical hashing for one resolved corpus slice.'''

from __future__ import annotations

from hashlib import sha256
from typing import Any

import yaml

from yaml_data_editor_kit.schema.corpus import ABSENT

from .errors import EvaluationError


def canonical_bytes(value: Any) -> bytes:
    '''Serialize a YAML value after deterministic recursive normalization.'''
    normalized = _canonicalize(value)
    try:
        rendered = yaml.safe_dump(
            normalized,
            sort_keys=False,
            allow_unicode=True,
        )
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        raise EvaluationError(
            'anchored slice cannot be serialized as canonical YAML: {}'.format(exc)
        ) from exc
    return rendered.encode('utf-8')


def slice_hash(value: Any) -> str:
    '''Return the SHA-256 guard for one resolved anchor slice.'''
    return 'sha256:' + sha256(canonical_bytes(value)).hexdigest()


def _canonicalize(value: Any) -> Any:
    if value is ABSENT:
        return {'__absent__': True}
    if isinstance(value, dict):
        return {
            key: _canonicalize(item)
            for key, item in sorted(
                value.items(),
                key=lambda pair: (type(pair[0]).__name__, str(pair[0])),
            )
        }
    if isinstance(value, list):
        return [_canonicalize(item) for item in value]
    return value
