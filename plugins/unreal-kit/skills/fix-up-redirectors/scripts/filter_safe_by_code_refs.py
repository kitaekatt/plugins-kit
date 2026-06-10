"""Filter the phase-2 safe set against the code-references cache.

Host-side. Run this immediately before phase 4 (apply). If the code-refs
cache is missing or older than --max-age-hours, regenerate it first.

Any redirector whose package appears in the cache is dropped from the safe
set - we never fix a redirector that code still points at.
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

from code_refs import DEFAULT_EXTENSIONS, get_age_hours, load, save, scan
from redirector_record import load_safe_set, save_report, save_safe_set


def _maybe_regenerate(refs_path, root, max_age_hours, extensions):
    age = get_age_hours(refs_path)
    if age is None:
        print(f"Code-refs cache missing - scanning {root}...")
    elif age > max_age_hours:
        print(f"Code-refs cache is {age:.1f}h old (>{max_age_hours}h) - rescanning {root}...")
    else:
        print(f"Code-refs cache is {age:.1f}h old (fresh).")
        return
    refs, file_count, scanned_count, mounts = scan(root, extensions=extensions)
    save(refs_path, refs, root, file_count, scanned_count, extensions, mounts=mounts)
    print(f"Wrote {refs_path}: {len(refs)} references from {file_count} files.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--safe-in', required=True)
    ap.add_argument('--safe-out', required=True)
    ap.add_argument('--refs', default=os.path.join('.local-data', 'code_references.yaml'))
    ap.add_argument('--root', default=os.getcwd(),
                    help='Project root to scan when regenerating (default: cwd)')
    ap.add_argument('--max-age-hours', type=float, default=24.0)
    ap.add_argument('--report-out', default=None,
                    help='Optional JSON report of dropped redirectors')
    ap.add_argument('--extensions', default=','.join(DEFAULT_EXTENSIONS))
    args = ap.parse_args()

    extensions = tuple(e.strip() if e.strip().startswith('.') else '.' + e.strip()
                       for e in args.extensions.split(',') if e.strip())

    _maybe_regenerate(args.refs, args.root, args.max_age_hours, extensions)

    refs_doc = load(args.refs) or {}
    refs_set = set(refs_doc.get('references') or [])
    if not refs_set:
        # Defensive: if the scan found zero, something is probably wrong with
        # extensions/root. Don't silently auto-approve every redirector.
        sys.stderr.write(
            f"[filter_safe_by_code_refs] Refusing to proceed: code-refs cache "
            f"at {args.refs} has 0 references. Check --root and --extensions.\n")
        sys.exit(1)

    safe = load_safe_set(args.safe_in)
    scope = safe.get('scope', '/Game')
    records = safe.get('redirectors', [])

    kept = []
    dropped = []
    for r in records:
        pkg = r.get('pkg')
        if pkg and pkg in refs_set:
            r = dict(r)
            r['code_referenced'] = True
            dropped.append(r)
        else:
            kept.append(r)

    save_safe_set(args.safe_out, scope, kept)

    if args.report_out:
        save_report(args.report_out, {
            'scope': scope,
            'refs_path': args.refs,
            'refs_age_hours': get_age_hours(args.refs),
            'safe_in_count': len(records),
            'kept_count': len(kept),
            'dropped_count': len(dropped),
            'dropped_samples': [r['pkg'] for r in dropped[:20]],
        })

    print(f"Safe set: {len(records)} -> {len(kept)} after code-ref filter "
          f"({len(dropped)} dropped).")
    if dropped:
        print("Dropped (sample):")
        for r in dropped[:10]:
            print(f"  {r['pkg']}")


if __name__ == '__main__':
    main()
