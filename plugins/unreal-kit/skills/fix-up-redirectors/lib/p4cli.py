"""Host-side P4 CLI helpers. Stdlib-only.

CCP: changes to the P4 CLI invocation contract change here. Used together by
anything talking to P4 from a Claude script - hence the small module.
"""
import os
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


def run_p4(args, stdin=None):
    """Run a p4 command. Returns (rc, stdout, stderr); does not raise."""
    result = subprocess.run([P4] + list(args), capture_output=True, text=True, input=stdin, check=False)
    return result.returncode, result.stdout, result.stderr


def run_p4_or_die(args, stdin=None, what=None):
    """Run a p4 command. Exits with a clear error on non-zero return."""
    rc, out, err = run_p4(args, stdin=stdin)
    if rc != 0:
        label = what or f"p4 {' '.join(args)}"
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
    rc, out, _err = run_p4(['info'])
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
