"""Host-side P4 CLI helpers. Stdlib-only.

CCP: changes to the P4 CLI invocation contract change here. Used together by
anything talking to P4 from a Claude script - hence the small module.
"""
import os
import math
import shutil
import subprocess
import sys
from collections import defaultdict


def find_p4():
    """Locate the p4 binary.

    Resolution order:
      1. Path recorded by bootstrap (tool_paths.json) — most authoritative.
      2. shutil.which('p4' / 'p4.exe') — covers fresh installs where
         bootstrap hasn't recorded a path yet.
      3. Standard Windows install locations — engine-bundled Pythons often
         have a sanitized PATH that doesn't see Perforce's install dir.
      4. Bare 'p4' — let subprocess error out informatively.

    See docs/planning/bootstrap/tool-resolution-redesign.md.
    """
    try:
        from bootstrap_lib import tool_paths
        recorded = tool_paths.resolve(tool_paths.canonical_data_dir(), 'p4')
        if recorded and os.path.isfile(recorded):
            return recorded
    except ImportError:
        pass
    candidates = [
        shutil.which('p4'),
        shutil.which('p4.exe'),
        r'C:\Program Files\Perforce\p4.exe',
        r'C:\Program Files (x86)\Perforce\p4.exe',
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return 'p4'


P4 = find_p4()

QUERY_TIMEOUT_ENV = 'UNREAL_KIT_P4_QUERY_TIMEOUT_S'
MUTATION_TIMEOUT_ENV = 'UNREAL_KIT_P4_MUTATION_TIMEOUT_S'
P4_TIMEOUT_RETURN_CODE = 124


class P4TimeoutConfigError(ValueError):
    """A configured P4 timeout is missing, non-finite, or not positive."""


class P4TimeoutError(SystemExit):
    """A P4 child timed out and its completion state is unknown."""

    def __init__(self, label, result):
        super().__init__(1)
        self.label = label
        self.result = result


class P4Result(tuple):
    """Three-value tuple-compatible P4 result with diagnostics."""

    def __new__(cls, returncode, stdout, stderr, *, timed_out=False, spawn_error=False):
        result = super().__new__(cls, (returncode, stdout, stderr))
        result.returncode = returncode
        result.stdout = stdout
        result.stderr = stderr
        result.timed_out = timed_out
        result.spawn_error = spawn_error
        return result


def _as_text(value):
    if value is None:
        return ''
    if isinstance(value, bytes):
        return value.decode(errors='replace')
    return str(value)


def _configured_timeout(name):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise P4TimeoutConfigError(
            f'{name} must be a finite positive number; got {raw!r}'
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise P4TimeoutConfigError(
            f'{name} must be a finite positive number; got {raw!r}'
        )
    return value


def _command_name(args):
    """Find the P4 verb after global options such as ``-x -``."""
    args = list(args)
    index = 0
    while index < len(args):
        token = str(args[index])
        if token == '-x':
            index += 2
            continue
        if token in ('-ztag', '-s', '-G', '-C', '-p', '-u', '-c', '-P', '-H', '-Q'):
            # These options either stand alone or consume their next token.
            index += 2 if token in ('-p', '-u', '-c', '-P', '-H', '-Q') else 1
            continue
        if token.startswith('-'):
            index += 1
            continue
        return token.lower()
    return ''


_QUERY_COMMANDS = frozenset({'info', 'where', 'opened', 'fstat', 'changes', 'dirs'})


def _timeout_for(args, timeout_s):
    if timeout_s is not None:
        try:
            value = float(timeout_s)
        except (TypeError, ValueError) as exc:
            raise P4TimeoutConfigError(
                f'timeout_s must be a finite positive number; got {timeout_s!r}'
            ) from exc
        if not math.isfinite(value) or value <= 0:
            raise P4TimeoutConfigError(
                f'timeout_s must be a finite positive number; got {timeout_s!r}'
            )
        return value
    command = _command_name(args)
    env_name = QUERY_TIMEOUT_ENV if command in _QUERY_COMMANDS else MUTATION_TIMEOUT_ENV
    return _configured_timeout(env_name)


def run_p4(args, stdin=None, timeout_s=None):
    """Run P4 once with the command's query or mutation budget.

    Unset environment settings retain the historical unbounded subprocess
    call. A timeout returns partial evidence and a nonzero result; it never
    retries a command whose completion is unknown.
    """
    args = list(args)
    timeout = _timeout_for(args, timeout_s)
    command = [P4] + args
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            input=stdin,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _as_text(getattr(exc, 'stdout', None) or getattr(exc, 'output', None))
        stderr = _as_text(getattr(exc, 'stderr', None))
        detail = f'P4 command timed out after {timeout:g}s'
        if stderr:
            stderr = f'{stderr}\n{detail}'
        else:
            stderr = detail
        return P4Result(
            P4_TIMEOUT_RETURN_CODE, stdout, stderr, timed_out=True
        )
    except OSError as exc:
        return P4Result(
            127, '', f'P4 command could not start: {exc}', spawn_error=True
        )
    return P4Result(result.returncode, result.stdout, result.stderr)


def run_p4_or_die(args, stdin=None, what=None, timeout_s=None):
    """Run a p4 command. Exits with a clear error on non-zero return."""
    result = run_p4(args, stdin=stdin, timeout_s=timeout_s)
    rc, out, err = result
    if rc != 0:
        label = what or f"p4 {' '.join(args)}"
        if getattr(result, 'timed_out', False):
            raise P4TimeoutError(label, result)
        sys.stderr.write(f"{label} failed (rc={rc}):\n{err}\n")
        sys.exit(1)
    return out


def get_workspace_mapping():
    """Return ((depot_root, local_root)) for the workspace's primary //... mapping.

    Both roots have trailing '/...' stripped and are forward-slashed."""
    out = run_p4_or_die(['where', '//...'], what='p4 where //...')
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith('-'):
            continue
        parts = line.split(' ')
        if len(parts) < 3:
            continue
        depot, _client, local = parts[0], parts[1], ' '.join(parts[2:])
        if depot.endswith('/...'):
            depot = depot[:-4]
        if local.endswith('\\...'):
            local = local[:-4]
        elif local.endswith('/...'):
            local = local[:-4]
        return depot.rstrip('/'), local.replace('\\', '/').rstrip('/')
    sys.exit("Could not parse `p4 where //...`")


def local_to_depot(local_path, depot_root, local_root):
    """Convert a local file path to its depot path. Returns None if not in workspace."""
    lp = local_path.replace('\\', '/').rstrip('/')
    lr = local_root.replace('\\', '/').rstrip('/')
    lp_fold = lp.casefold()
    lr_fold = lr.casefold()
    if lp_fold != lr_fold and not lp_fold.startswith(lr_fold + '/'):
        return None
    rel = lp[len(lr):]
    return depot_root.rstrip('/') + rel


def parse_opened(opened_output):
    """Parse `p4 opened -a` output. Returns dict keyed by lowercase depot path,
    each value a list of {user, client, change} dicts.

    Example line:
        //depot/main/foo.uasset#3 - edit change 12345 (binary+l) by alice@workspace
    """
    opened = defaultdict(list)
    for line in opened_output.splitlines():
        if ' - ' not in line or ' by ' not in line:
            continue
        depot_path = line.split('#', 1)[0].strip()
        if not depot_path.startswith('//'):
            continue
        change = 'default'
        if 'change ' in line:
            after = line.split('change ', 1)[1]
            tok = after.split()[0]
            if tok.isdigit():
                change = tok
        userclient = line.rsplit(' by ', 1)[1].strip()
        if '@' in userclient:
            user, client = userclient.split('@', 1)
        else:
            user, client = userclient, ''
        opened[depot_path.lower()].append({
            'user': user,
            'client': client,
            'change': change,
        })
    return opened


def get_opened_map():
    """Return the parsed `p4 opened -a` map for the whole workspace."""
    out = run_p4_or_die(['opened', '-a'], what='p4 opened -a')
    return parse_opened(out)


def create_pending_cl(description, client=None):
    """Create a new pending CL with the given description. Returns the CL number."""
    spec_lines = ["Change: new"]
    if client:
        spec_lines.append(f"Client: {client}")
    spec_lines.append("Description:")
    for line in description.splitlines():
        spec_lines.append("\t" + line)
    spec = "\n".join(spec_lines) + "\n"

    out = run_p4_or_die(['change', '-i'], stdin=spec, what='p4 change -i')
    for tok in out.split():
        if tok.isdigit():
            return tok
    sys.exit(f"Could not parse new CL number from: {out!r}")


def edit_files(cl_num, files, batch_size=200):
    """Open files for edit in the given CL, batching to avoid command-line length limits."""
    for i in range(0, len(files), batch_size):
        batch = files[i:i + batch_size]
        run_p4_or_die(['-x', '-', 'edit', '-c', cl_num], stdin='\n'.join(batch),
                      what=f'p4 edit batch {i // batch_size}')


def delete_files(cl_num, files, batch_size=200):
    """Open files for delete in the given CL, batching to avoid command-line length limits.
    `p4 delete` opens each file for delete and removes it from the workspace."""
    for i in range(0, len(files), batch_size):
        batch = files[i:i + batch_size]
        run_p4_or_die(['-x', '-', 'delete', '-c', cl_num], stdin='\n'.join(batch),
                      what=f'p4 delete batch {i // batch_size}')


def reopen_files(cl_num, files, batch_size=200):
    """Move already-opened files into the given CL via `p4 reopen -c`. Used
    when UE's source-control plugin auto-opens files in the default CL and
    we need to herd them into our pending CL."""
    for i in range(0, len(files), batch_size):
        batch = files[i:i + batch_size]
        run_p4_or_die(['-x', '-', 'reopen', '-c', cl_num], stdin='\n'.join(batch),
                      what=f'p4 reopen batch {i // batch_size}')


def get_p4_user():
    """Return the current P4 user. Prefers $P4USER env var; falls back to
    `p4 info` parsed for "User name: <name>". Returns the empty string only
    if every probe fails.

    On some Perforce servers `p4 -F %userName% info` exits 0 with empty
    stdout (the %userName% format variable is supported by `p4 user -o`
    but not by `p4 info`). We previously used that form and it silently
    skipped the existing-CL guard. The plain-text parse below is robust
    across server versions."""
    env_user = os.environ.get('P4USER', '').strip()
    if env_user:
        return env_user
    result = run_p4(['info'])
    rc, out, _err = result
    if getattr(result, 'timed_out', False):
        raise P4TimeoutError('p4 info', result)
    if rc != 0:
        return ''
    for line in out.splitlines():
        if line.startswith('User name:'):
            return line.split(':', 1)[1].strip()
    return ''


def get_opened_in_cl(cl_num):
    """Return a set of lowercase depot paths currently opened in the given CL."""
    out = run_p4_or_die(['opened', '-c', cl_num], what=f'p4 opened -c {cl_num}')
    depots = set()
    for line in out.splitlines():
        if ' - ' in line:
            depot = line.split('#', 1)[0].strip().lower()
            if depot.startswith('//'):
                depots.add(depot)
    return depots


def where_batch(local_paths):
    """Resolve a batch of local paths to depot paths via `p4 where`. Returns
    set of lowercase depot paths."""
    if not local_paths:
        return set()
    out = run_p4_or_die(['-x', '-', 'where'], stdin='\n'.join(local_paths), what='p4 where (batch)')
    depots = set()
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith('-'):
            continue
        parts = line.split(' ')
        if len(parts) >= 3 and parts[0].startswith('//'):
            depots.add(parts[0].lower())
    return depots


def _parse_tagged_where(output):
    """Parse one or more ``p4 -ztag where`` records.

    A tagged record keeps the depot path associated with the input file.  This
    matters when a client has overlapping mappings: a set of depot paths loses
    which mapping belonged to which candidate and can approve the wrong file.
    """
    records = []
    current = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        if line.startswith('... '):
            field_value = line[4:]
            field, sep, value = field_value.partition(' ')
            if not sep:
                continue
            if field == 'depotFile' and 'depotFile' in current:
                records.append(current)
                current = {}
            current[field] = value
            continue
        # Keep compatibility with a non-tagged fake or old p4 wrapper.  The
        # normal production path is tagged and never relies on this parser.
        parts = line.split(None, 2)
        if len(parts) >= 3 and parts[0].startswith('//'):
            records.append({'depotFile': parts[0], 'clientFile': parts[1], 'path': parts[2]})
    if current:
        records.append(current)
    return records


def where_records(local_paths):
    """Resolve each local path independently and retain every tagged mapping.

    The return value is a flat list of dictionaries.  Each dictionary has the
    tagged P4 fields plus ``input`` identifying the local path that was
    queried.  Multiple records for one input are deliberately retained as an
    ambiguity; callers must refuse them rather than choosing the first view.
    """
    records = []
    for local_path in local_paths:
        if not local_path:
            continue
        try:
            output = run_p4_or_die(
                ['-ztag', 'where', local_path],
                what=f'p4 where {local_path}',
            )
        except SystemExit:
            # An unmapped or temporarily unavailable view is evidence of an
            # incomplete classification, not permission to mutate the file.
            continue
        matches = _parse_tagged_where(output)
        for match in matches:
            record = dict(match)
            record['input'] = local_path
            records.append(record)
    return records
