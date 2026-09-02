'''Persistent file-per-comment records for the editor/dispatcher seam.'''

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

import yaml

from yaml_data_editor_kit.schema import Corpus, Profile
from yaml_data_editor_kit.schema.errors import Diagnostic

from .address import Selector, parse_anchor, resolve_anchor
from .errors import SelectorError
from .hashing import slice_hash

_REQUIRED_FIELDS = ('id', 'anchor', 'text', 'state', 'created', 'guard')
_LEGAL_FIELDS = frozenset((*_REQUIRED_FIELDS, 'annotations', 'kind', 'ruling'))
INSTRUCTION = 'instruction'
QUESTION = 'question'
_KINDS = frozenset((INSTRUCTION, QUESTION))
_STATES = frozenset(('open', 'resolved'))
_GUARD_PATTERN = re.compile(r'^sha256:[0-9a-f]{64}$')


@dataclass(frozen=True)
class Comment:
    '''One guarded, singular unit of intent over a corpus slice.'''

    id: str
    anchor: Selector
    text: str
    state: str
    created: str
    guard: str
    annotations: dict[Any, Any]
    kind: str = INSTRUCTION
    ruling: str | None = None

    @classmethod
    def create(
        cls,
        profile: Profile,
        corpus: Corpus,
        *,
        id: str,
        anchor: str,
        text: str,
        created: str,
        annotations: Mapping[Any, Any] | None = None,
    ) -> Comment:
        '''Create an open comment with a guard over its resolved anchor slice.'''
        parsed = parse_anchor(anchor)
        resolved = resolve_anchor(parsed, profile, corpus)
        comment = cls(
            id=id,
            anchor=parsed,
            text=text,
            state='open',
            created=created,
            guard=slice_hash(resolved.slice_value),
            annotations=dict(annotations) if annotations is not None else {},
        )
        problems = _comment_diagnostics(comment, Path('<new comment>'))
        if problems:
            raise ValueError('; '.join(str(problem) for problem in problems))
        return comment

    @classmethod
    def create_question(
        cls,
        profile: Profile,
        corpus: Corpus,
        *,
        id: str,
        anchor: str,
        text: str,
        created: str,
        annotations: Mapping[Any, Any] | None = None,
    ) -> Comment:
        '''Create an open question with a guard over its resolved anchor slice.'''
        parsed = parse_anchor(anchor)
        resolved = resolve_anchor(parsed, profile, corpus)
        comment = cls(
            id=id,
            anchor=parsed,
            text=text,
            state='open',
            created=created,
            guard=slice_hash(resolved.slice_value),
            annotations=dict(annotations) if annotations is not None else {},
            kind=QUESTION,
        )
        problems = _comment_diagnostics(comment, Path('<new question>'))
        if problems:
            raise ValueError('; '.join(str(problem) for problem in problems))
        return comment


@dataclass
class CommentSet:
    '''Comments loaded from one store plus every malformed-file diagnostic.'''

    comments: list[Comment]
    diagnostics: list[Diagnostic]


class CommentStore:
    '''A directory containing one atomic YAML file per comment.'''

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @classmethod
    def init(cls, root: Path) -> CommentStore:
        '''Create an explicit store root or validate an existing store directory.'''
        store = cls(root)
        if store.root.exists():
            if not store.root.is_dir():
                raise NotADirectoryError(
                    'comment store root is not a directory: {}'.format(store.root)
                )
            entries = list(store.root.iterdir())
            unexpected = [
                path
                for path in entries
                if not path.is_file() or path.suffix.lower() != '.yaml'
            ]
            if unexpected:
                raise ValueError(
                    'refusing non-store directory {}: unexpected entry {}'.format(
                        store.root, unexpected[0].name
                    )
                )
            if entries:
                loaded = store.load()
                if loaded.diagnostics:
                    raise ValueError(
                        'refusing malformed comment store {}: {}'.format(
                            store.root, loaded.diagnostics[0]
                        )
                    )
            return store
        store.root.mkdir(parents=True)
        return store

    def load(self) -> CommentSet:
        '''Load every YAML comment, collecting every malformed-file diagnostic.'''
        self._require_root()
        comments: list[Comment] = []
        diagnostics: list[Diagnostic] = []
        for path in sorted(self.root.glob('*.yaml'), key=lambda item: item.name):
            comment, problems = _load_comment(path)
            diagnostics.extend(problems)
            if comment is not None:
                comments.append(comment)
        return CommentSet(comments=comments, diagnostics=diagnostics)

    def write(self, comment: Comment) -> Path:
        '''Atomically write one complete comment record.'''
        self._require_root()
        problems = _comment_diagnostics(comment, self.root / '<write>')
        if problems:
            raise ValueError('; '.join(str(problem) for problem in problems))
        target = self._path_for(comment.id)
        if target.exists():
            self._check_collision(target, comment.id)
        rendered = yaml.safe_dump(
            _comment_mapping(comment), sort_keys=False, allow_unicode=True
        )
        temporary = self.root / '.{}.{}.tmp'.format(comment.id, uuid4().hex)
        try:
            with temporary.open('x', encoding='utf-8', newline='\n') as stream:
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def resolve(self, comment: Comment) -> Comment:
        '''Mark one comment resolved and persist it without changing its guard.'''
        if comment.kind == QUESTION:
            raise ValueError('question comments must be resolved with rule()')
        resolved = replace(comment, state='resolved')
        self.write(resolved)
        return resolved

    def rule(self, question: Comment, ruling: str) -> Comment:
        '''Resolve one open question with a non-empty ruling.'''
        if question.kind != QUESTION:
            raise ValueError('rule() requires a question comment')
        if question.state != 'open':
            raise ValueError('rule() requires an open question')
        if not isinstance(ruling, str) or not ruling.strip():
            raise ValueError('rule() requires non-empty ruling text')
        resolved = replace(question, state='resolved', ruling=ruling)
        self.write(resolved)
        return resolved

    def _require_root(self) -> None:
        if not self.root.exists():
            raise FileNotFoundError(
                'comment store root does not exist: {}'.format(self.root)
            )
        if not self.root.is_dir():
            raise NotADirectoryError(
                'comment store root is not a directory: {}'.format(self.root)
            )

    def _path_for(self, comment_id: str) -> Path:
        if not _safe_comment_id(comment_id):
            raise ValueError(
                'comment id {!r} cannot be represented as one file under {}'.format(
                    comment_id, self.root
                )
            )
        return self.root / '{}.yaml'.format(comment_id)

    @staticmethod
    def _check_collision(target: Path, comment_id: str) -> None:
        try:
            raw = yaml.safe_load(target.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ValueError(
                'cannot inspect existing comment file {} before atomic '
                'replace: {}'.format(target, exc)
            ) from exc
        existing_id = raw.get('id') if isinstance(raw, dict) else None
        if existing_id != comment_id:
            raise ValueError(
                'refusing to overwrite {}: existing file declares id {!r}, '
                'not {!r}'.format(target, existing_id, comment_id)
            )


def _load_comment(path: Path) -> tuple[Comment | None, list[Diagnostic]]:
    try:
        raw = yaml.safe_load(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        return None, [_diagnostic(path, 'cannot read comment YAML: {}'.format(exc))]
    if not isinstance(raw, dict):
        return None, [_diagnostic(path, 'comment document must be a mapping')]

    problems: list[Diagnostic] = []
    for field_name in _REQUIRED_FIELDS:
        if field_name not in raw:
            problems.append(
                _diagnostic(
                    path,
                    'missing required field {!r}'.format(field_name),
                    field_name,
                )
            )
    for field_name in raw:
        if field_name not in _LEGAL_FIELDS:
            problems.append(
                _diagnostic(
                    path,
                    'unknown top-level key {!r}; legal keys: {}'.format(
                        field_name, _listed_fields()
                    ),
                    str(field_name),
                )
            )

    comment: Comment | None = None
    if all(field_name in raw for field_name in _REQUIRED_FIELDS):
        comment, field_problems = _comment_from_mapping(path, raw)
        problems.extend(field_problems)
    if problems:
        return None, problems
    return comment, []


def _comment_from_mapping(
    path: Path, raw: dict[Any, Any]
) -> tuple[Comment | None, list[Diagnostic]]:
    problems: list[Diagnostic] = []
    comment_id = raw['id']
    if not isinstance(comment_id, str) or not comment_id:
        problems.append(_diagnostic(path, 'field id must be non-empty text', 'id'))
    elif not _safe_comment_id(comment_id):
        problems.append(
            _diagnostic(
                path,
                'field id must name one file and cannot contain a path separator',
                'id',
            )
        )
    elif path.stem != comment_id:
        problems.append(
            _diagnostic(
                path,
                'filename stem {!r} does not match comment id {!r}'.format(
                    path.stem, comment_id
                ),
                'id',
            )
        )

    parsed_anchor: Selector | None = None
    anchor_text = raw['anchor']
    if not isinstance(anchor_text, str):
        problems.append(_diagnostic(path, 'field anchor must be text', 'anchor'))
    else:
        try:
            parsed_anchor = parse_anchor(anchor_text)
        except SelectorError as exc:
            problems.append(_diagnostic(path, str(exc), 'anchor'))

    if not isinstance(raw['text'], str):
        problems.append(_diagnostic(path, 'field text must be text', 'text'))
    kind = raw.get('kind', INSTRUCTION)
    if not isinstance(kind, str) or kind not in _KINDS:
        problems.append(
            _diagnostic(
                path,
                'field kind must be exactly instruction or question',
                'kind',
            )
        )
    if not isinstance(raw['state'], str) or raw['state'] not in _STATES:
        problems.append(
            _diagnostic(
                path,
                'field state must be exactly open or resolved',
                'state',
            )
        )
    if not isinstance(raw['created'], str):
        problems.append(_diagnostic(path, 'field created must be text', 'created'))
    if (
        not isinstance(raw['guard'], str)
        or _GUARD_PATTERN.fullmatch(raw['guard']) is None
    ):
        problems.append(
            _diagnostic(
                path,
                'field guard must be sha256: followed by 64 lowercase hex digits',
                'guard',
            )
        )

    annotations = raw.get('annotations', {})
    if not isinstance(annotations, dict):
        problems.append(
            _diagnostic(path, 'field annotations must be a mapping', 'annotations')
        )
    ruling = raw.get('ruling')
    if kind == INSTRUCTION and 'ruling' in raw:
        problems.append(
            _diagnostic(path, 'instruction comments cannot have a ruling', 'ruling')
        )
    elif kind == QUESTION:
        if not isinstance(raw['text'], str) or not raw['text'].strip():
            problems.append(
                _diagnostic(path, 'question text must contain a non-whitespace character', 'text')
            )
        if raw['state'] == 'open' and ruling is not None:
            problems.append(
                _diagnostic(path, 'open questions must have ruling: null', 'ruling')
            )
        elif raw['state'] == 'resolved' and (
            not isinstance(ruling, str) or not ruling.strip()
        ):
            problems.append(
                _diagnostic(
                    path,
                    'resolved questions must have non-empty ruling text',
                    'ruling',
                )
            )
    if problems or parsed_anchor is None or not isinstance(comment_id, str):
        return None, problems
    return (
        Comment(
            id=comment_id,
            anchor=parsed_anchor,
            text=raw['text'],
            state=raw['state'],
            created=raw['created'],
            guard=raw['guard'],
            annotations=annotations,
            kind=kind,
            ruling=ruling,
        ),
        [],
    )


def _comment_diagnostics(comment: Comment, path: Path) -> list[Diagnostic]:
    problems: list[Diagnostic] = []
    if not isinstance(comment.id, str) or not comment.id:
        problems.append(_diagnostic(path, 'field id must be non-empty text', 'id'))
    elif not _safe_comment_id(comment.id):
        problems.append(
            _diagnostic(
                path,
                'field id must name one file and cannot contain a path separator',
                'id',
            )
        )
    if not isinstance(comment.anchor, Selector) or not comment.anchor.is_anchor:
        problems.append(
            _diagnostic(path, 'field anchor must be a parsed singular anchor', 'anchor')
        )
    if not isinstance(comment.text, str):
        problems.append(_diagnostic(path, 'field text must be text', 'text'))
    if not isinstance(comment.kind, str) or comment.kind not in _KINDS:
        problems.append(
            _diagnostic(
                path,
                'field kind must be exactly instruction or question',
                'kind',
            )
        )
    if not isinstance(comment.state, str) or comment.state not in _STATES:
        problems.append(
            _diagnostic(path, 'field state must be exactly open or resolved', 'state')
        )
    if not isinstance(comment.created, str):
        problems.append(_diagnostic(path, 'field created must be text', 'created'))
    if (
        not isinstance(comment.guard, str)
        or _GUARD_PATTERN.fullmatch(comment.guard) is None
    ):
        problems.append(
            _diagnostic(
                path,
                'field guard must be sha256: followed by 64 lowercase hex digits',
                'guard',
            )
        )
    if not isinstance(comment.annotations, dict):
        problems.append(
            _diagnostic(path, 'field annotations must be a mapping', 'annotations')
        )
    if comment.kind == INSTRUCTION and comment.ruling is not None:
        problems.append(
            _diagnostic(path, 'instruction comments cannot have a ruling', 'ruling')
        )
    elif comment.kind == QUESTION:
        if not isinstance(comment.text, str) or not comment.text.strip():
            problems.append(
                _diagnostic(path, 'question text must contain a non-whitespace character', 'text')
            )
        if comment.state == 'open' and comment.ruling is not None:
            problems.append(
                _diagnostic(path, 'open questions must have ruling: null', 'ruling')
            )
        elif comment.state == 'resolved' and (
            not isinstance(comment.ruling, str) or not comment.ruling.strip()
        ):
            problems.append(
                _diagnostic(
                    path,
                    'resolved questions must have non-empty ruling text',
                    'ruling',
                )
            )
    return problems


def _comment_mapping(comment: Comment) -> dict[str, Any]:
    mapping = {
        'id': comment.id,
        'anchor': comment.anchor.canonical(),
        'text': comment.text,
        'state': comment.state,
        'created': comment.created,
        'guard': comment.guard,
        'kind': comment.kind,
        'annotations': comment.annotations,
    }
    if comment.kind == QUESTION:
        mapping['ruling'] = comment.ruling
    return mapping


def _diagnostic(path: Path, message: str, field: str | None = None) -> Diagnostic:
    return Diagnostic(message=message, file=str(path), field=field)


def _listed_fields() -> str:
    return '[{}]'.format(', '.join(sorted(_LEGAL_FIELDS)))


def _safe_comment_id(comment_id: str) -> bool:
    return (
        comment_id not in ('', '.', '..')
        and '/' not in comment_id
        and '\\' not in comment_id
        and '\x00' not in comment_id
    )
