"""Reduce a safe-set JSON to one redirector per package directory.

Per-directory subset sampling is a low-blast-radius testing technique for
broad redirector purges: instead of committing to a multi-thousand-file
edit and discovering breakage at minute 90, you take exactly one
redirector per unique package directory, get a representative slice that
exercises every directory layout, and verify the broad purge works on the
small slice first.

In our reference run, 2839 safe redirectors collapsed to 241 unique
directories — a ~12x reduction in apply time with the same coverage of
directory-shape edge cases.

Input: a safe-set JSON in either shape produced by classify_safety.py
(fix-up safe set OR orphaned safe set). The output JSON keeps the same
shape so it can feed straight into apply_fixups.py.

Determinism: directories are sorted; within each directory the redirector
records are sorted by pkg name and the first is picked. Same input ->
same output.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'lib')))
# Plugin-level lib (for bootstrap_guard) — scripts/ -> skill/ -> skills/ -> unreal-kit/lib
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'lib')))

# Re-exec under the bootstrap-provisioned plugin venv (no-op when already
# there) so pyyaml resolves regardless of the invoking interpreter.
from bootstrap_guard import reexec_under_plugin_venv
reexec_under_plugin_venv("unreal-kit")

from redirector_record import load_safe_set, save_safe_set


def package_dir(pkg):
    """Strip trailing asset name from a package path.

    /Game/UI/Widgets/Inbox/WBP_X -> /Game/UI/Widgets/Inbox
    """
    return pkg.rsplit('/', 1)[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='in_path', required=True,
                    help='Input safe-set JSON (fix-up or orphaned shape).')
    ap.add_argument('--out', dest='out_path', required=True,
                    help='Output safe-set JSON (one redirector per package directory).')
    args = ap.parse_args()

    data = load_safe_set(args.in_path)
    scope = data.get('scope', '/Game')
    records = data.get('redirectors', [])

    # Group by package directory.
    by_dir = {}
    for rec in records:
        pkg = rec.get('pkg', '')
        if not pkg:
            continue
        by_dir.setdefault(package_dir(pkg), []).append(rec)

    # Pick deterministically: sort directory keys, sort records within each
    # group by pkg name, keep the first.
    picked = []
    for pkg_dir in sorted(by_dir.keys()):
        group = sorted(by_dir[pkg_dir], key=lambda r: r.get('pkg', ''))
        picked.append(group[0])

    save_safe_set(args.out_path, scope, picked)

    print(f"Input redirectors:       {len(records)}")
    print(f"Unique directories:      {len(by_dir)}")
    print(f"Picked one per dir:      {len(picked)}")
    print(f"Wrote {args.out_path}")


if __name__ == '__main__':
    main()
