"""Host-side code-reference scanner and cache I/O.

Scans project source for references to UE content paths (e.g. `/Game/Foo/Bar`)
and emits only the ones that actually point at real assets:

  1. The first segment must be a real UE mount point (`/Game`, `/Engine`, or
     a discovered plugin), found by walking the project for `.uproject` /
     `.uplugin` files.
  2. The path must resolve to an existing `.uasset` or `.umap` on disk under
     that mount's `Content/` directory.

The candidate regex is intentionally permissive; the mount + on-disk filter
is what gives the cache its signal-to-noise. Without that filter the cache
fills with false positives (test fixtures, `/Script/...` class paths,
include paths, doc-comment URLs, etc.).

Stdlib + pyyaml only.
"""
import datetime as _dt
import os
import re

import yaml


def _utc_now() -> "_dt.datetime":
    """Timezone-aware UTC now (datetime.utcnow() is deprecated since 3.12)."""
    return _dt.datetime.now(_dt.timezone.utc)


# Candidate finder: anything `/CapitalSegment/...`-shaped. Narrowed
# downstream by mount point and on-disk existence.
_PATH_RE = re.compile(r'/[A-Z][A-Za-z0-9_]*(?:/[A-Za-z0-9_.\-]+)+')

DEFAULT_EXTENSIONS = (
    '.cpp', '.h', '.hpp', '.c', '.cc', '.cxx', '.inl',
    '.cs', '.py', '.ini',
    '.uplugin', '.uproject',
)

DEFAULT_EXCLUDE_DIRS = frozenset({
    '.git', '.svn', '.hg',
    'Intermediate', 'Binaries', 'Saved', 'DerivedDataCache', 'Build',
    'node_modules', '.venv', 'venv', '__pycache__',
    'tmp', '.local-data', '.pytest_cache',
    '.vs', '.vscode', '.idea',
})

ASSET_EXTENSIONS = ('.uasset', '.umap')

# Skip very large files - generated dumps, lockfiles, blobs.
_MAX_FILE_BYTES = 5 * 1024 * 1024


def _normalize(path):
    """Strip `.AssetName` / `.AssetName_C` suffix so `/Game/Foo/Bar.Bar`
    collapses to `/Game/Foo/Bar`."""
    last_slash = path.rfind('/')
    if last_slash < 0:
        return path
    asset = path[last_slash + 1:]
    if '.' in asset:
        asset = asset.split('.', 1)[0]
    asset = asset.rstrip('-_.')
    if not asset:
        return path[:last_slash]
    return path[:last_slash + 1] + asset


def _is_binaryish(sample):
    return b'\x00' in sample


def _iter_source_files(root, extensions, exclude_dirs):
    ext_set = {e.lower() for e in extensions}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext in ext_set:
                yield os.path.join(dirpath, name)


def discover_mount_points(root, exclude_dirs=DEFAULT_EXCLUDE_DIRS):
    """Find real UE content mount points under `root`.

    Returns a dict mapping mount name (`/Game`, `/Engine`, `/<PluginName>`)
    to the absolute path of its `Content/` directory.

    Sources:
      - The shallowest `*.uproject` with a `Content/` sibling -> `/Game`.
        Engine sub-tools (UnrealLightmass etc.) ship their own `.uproject`
        files; we ignore those by preferring the one closest to `root`.
      - `<root>/Engine/Content` -> `/Engine`, if present
      - Each `*.uplugin` -> `/<plugin-stem>` (Content/ alongside the .uplugin)
    """
    mounts = {}
    game_uproject_depth = None

    abs_root = os.path.abspath(root)

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        for name in filenames:
            lower = name.lower()
            if lower.endswith('.uproject'):
                content_dir = os.path.join(dirpath, 'Content')
                if not os.path.isdir(content_dir):
                    continue
                rel = os.path.relpath(dirpath, abs_root)
                depth = 0 if rel in ('.', '') else rel.count(os.sep) + 1
                if game_uproject_depth is None or depth < game_uproject_depth:
                    game_uproject_depth = depth
                    mounts['/Game'] = os.path.abspath(content_dir)
            elif lower.endswith('.uplugin'):
                stem = os.path.splitext(name)[0]
                content_dir = os.path.join(dirpath, 'Content')
                if os.path.isdir(content_dir):
                    mounts['/' + stem] = os.path.abspath(content_dir)

    engine_content = os.path.join(root, 'Engine', 'Content')
    if os.path.isdir(engine_content):
        mounts.setdefault('/Engine', os.path.abspath(engine_content))

    return mounts


def _resolve_to_disk(pkg_path, mounts):
    """Return the on-disk asset path for `pkg_path`, or None if it doesn't
    resolve under any known mount as a real `.uasset` / `.umap`.

    `pkg_path` is a normalized package path like `/Game/UI/WBP_Foo`.
    """
    slash = pkg_path.find('/', 1)
    if slash < 0:
        return None
    mount = pkg_path[:slash]
    rel = pkg_path[slash + 1:]
    content_dir = mounts.get(mount)
    if not content_dir:
        return None
    rel_native = rel.replace('/', os.sep)
    for ext in ASSET_EXTENSIONS:
        candidate = os.path.join(content_dir, rel_native + ext)
        if os.path.isfile(candidate):
            return candidate
    return None


def scan(root, extensions=DEFAULT_EXTENSIONS, exclude_dirs=DEFAULT_EXCLUDE_DIRS,
         mounts=None, verify_on_disk=True):
    """Walk `root` and return `(refs_set, file_count, scanned_count, mounts)`.

    `refs_set` contains normalized package paths under known mounts. With
    `verify_on_disk=True` (default), each path also resolves to an existing
    `.uasset` / `.umap` -- so a hit always corresponds to a real asset.

    `mounts` is the mount map used (auto-discovered when None).
    """
    if mounts is None:
        mounts = discover_mount_points(root, exclude_dirs=exclude_dirs)

    candidates = set()
    scanned_count = 0
    file_count = 0
    for path in _iter_source_files(root, extensions, exclude_dirs):
        scanned_count += 1
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size > _MAX_FILE_BYTES:
            continue
        try:
            with open(path, 'rb') as f:
                head = f.read(8192)
                if _is_binaryish(head):
                    continue
                rest = f.read()
        except OSError:
            continue
        try:
            text = (head + rest).decode('utf-8', errors='ignore')
        except Exception:
            continue
        file_count += 1
        for match in _PATH_RE.findall(text):
            normalized = _normalize(match)
            slash = normalized.find('/', 1)
            if slash < 0:
                continue
            if normalized[:slash] not in mounts:
                continue
            candidates.add(normalized)

    if verify_on_disk:
        refs = {pkg for pkg in candidates if _resolve_to_disk(pkg, mounts) is not None}
    else:
        refs = candidates

    return refs, file_count, scanned_count, mounts


def save(path, refs, root, file_count, scanned_count, extensions,
         mounts=None, verify_on_disk=True):
    """Write the cache YAML."""
    out = {
        'generated_at': _utc_now().replace(microsecond=0).isoformat().replace('+00:00', '') + 'Z',
        'root': os.path.abspath(root).replace('\\', '/'),
        'extensions': list(extensions),
        'mounts': sorted(mounts.keys()) if mounts else [],
        'verify_on_disk': bool(verify_on_disk),
        'file_count': file_count,
        'scanned_count': scanned_count,
        'reference_count': len(refs),
        'references': sorted(refs),
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w') as f:
        yaml.dump(out, f, default_flow_style=False, sort_keys=False)


def load(path):
    with open(path, 'r') as f:
        return yaml.safe_load(f)


def get_age_hours(path):
    """Return age of the cache in hours based on its `generated_at` field.
    Falls back to filesystem mtime if the field is missing/unparseable.
    Returns None if the file doesn't exist."""
    if not os.path.isfile(path):
        return None
    try:
        data = load(path)
        ts = data.get('generated_at') if isinstance(data, dict) else None
        if ts:
            generated = _dt.datetime.fromisoformat(ts.rstrip('Z'))
            if generated.tzinfo is None:
                generated = generated.replace(tzinfo=_dt.timezone.utc)
            delta = _utc_now() - generated
            return delta.total_seconds() / 3600.0
    except Exception:
        pass
    try:
        mtime = os.path.getmtime(path)
        delta = _utc_now().timestamp() - mtime
        return delta / 3600.0
    except OSError:
        return None


def is_stale(path, max_age_hours):
    age = get_age_hours(path)
    return age is None or age > max_age_hours
