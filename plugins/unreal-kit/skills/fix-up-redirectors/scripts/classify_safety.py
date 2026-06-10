"""Phase 2 facade: classify each redirector as safe-to-fix or blocked.

Host-side. Reads phase-1 YAML, runs `p4 opened -a` once, and emits:
- safe-set JSON for phase 3
- report JSON for the skill prompt
"""
import argparse
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'lib')))
# Plugin-level lib (for bootstrap_guard/path_repair) — scripts/ -> skill/ -> skills/ -> unreal-kit/lib
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'lib')))

# Re-exec under the bootstrap-provisioned plugin venv (no-op when already
# there) so pyyaml resolves regardless of which interpreter launched the
# script — a bare `python` or `uv run` from a project root dies with
# ModuleNotFoundError: yaml otherwise.
from bootstrap_guard import reexec_under_plugin_venv
reexec_under_plugin_venv("unreal-kit")

from path_repair import repair_path
repair_path()

from p4cli import get_opened_map, get_workspace_mapping, local_to_depot
from redirector_record import load_discovery, save_safe_set, save_report


def _is_orphaned(record):
    """Orphaned redirector: target gone AND nothing references it.

    These are pure dead pointers — safe to delete with zero rewrite risk
    (no referencers means no .uasset to rewrite, just `p4 delete` the
    redirector file itself).
    """
    refs = record.get('referencer_files') or []
    ref_pkgs = record.get('referencer_pkgs') or []
    has_level = record.get('has_level_referencer', False)
    return not refs and not ref_pkgs and not has_level


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--discovery', required=True, help='Phase 1 YAML output')
    ap.add_argument('--out-safe', required=True,
                    help='Where to write the fix-up safe-set JSON (target_exists, no checkouts)')
    ap.add_argument('--out-orphaned', required=False, default=None,
                    help='Where to write the orphaned safe-set JSON '
                         '(target gone, zero referencers, no checkouts on the redirector itself). '
                         'Consumed by apply_fixups.py in --mode=delete-only.')
    ap.add_argument('--out-report', required=True, help='Where to write the report JSON')
    args = ap.parse_args()

    discovery = load_discovery(args.discovery)
    scope = discovery.get('scope', '/Game')
    redirectors = discovery.get('redirectors', [])

    depot_root, local_root = get_workspace_mapping()
    opened = get_opened_map()

    safe = []                  # fix-up safe set: target exists, all files unlocked
    blocked = []               # at least one file checked out by someone (us or others)
    orphaned_safe = []         # target gone, zero referencers, redirector unlocked -> delete-only
    orphaned_blocked = []      # target gone, zero referencers, but redirector itself locked
    referenced_broken = []     # target gone, has referencers -> manual cleanup needed
    non_writable = []
    safe_with_level = 0
    orphaned_with_level = 0    # always 0 by definition; tracked for symmetry
    blocked_by_user = Counter()
    blocked_changes_by_user = defaultdict(set)

    def _check_blockers(local_paths):
        """Return the list of blocker dicts for the given local paths.
        Mirrors the original inline logic; centralized so both code paths
        (fix-up and orphaned) charge the same tally.
        """
        blockers = []
        for local_path in local_paths:
            if not local_path:
                continue
            depot = local_to_depot(local_path, depot_root, local_root)
            if not depot:
                blockers.append({'file': local_path, 'reason': 'not_in_workspace'})
                continue
            for entry in opened.get(depot.lower(), []):
                blockers.append({
                    'file': local_path, 'depot': depot,
                    'user': entry['user'], 'change': entry['change'],
                })
        return blockers

    for r in redirectors:
        if not r.get('target_exists'):
            # Broken target — split into orphaned (zero referencers, safe to delete)
            # vs referenced-broken (has referencers, manual cleanup needed).
            if _is_orphaned(r):
                # Only the redirector's own file matters for lock-checking;
                # there are no referencers to consider.
                blockers = _check_blockers([r.get('file')])
                if blockers:
                    r['blockers'] = blockers
                    orphaned_blocked.append(r)
                    for b in blockers:
                        u = b.get('user')
                        if u:
                            blocked_by_user[u] += 1
                            blocked_changes_by_user[u].add(str(b.get('change', '')))
                else:
                    orphaned_safe.append(r)
            else:
                referenced_broken.append(r)
            continue

        if r.get('has_unresolvable_referencer'):
            non_writable.append(r)
            continue

        all_files = [r['file']] + list(r.get('referencer_files', []))
        blockers = _check_blockers(all_files)

        if blockers:
            r['blockers'] = blockers
            blocked.append(r)
            for b in blockers:
                u = b.get('user')
                if u:
                    blocked_by_user[u] += 1
                    blocked_changes_by_user[u].add(str(b.get('change', '')))
        else:
            safe.append(r)
            if r.get('has_level_referencer'):
                safe_with_level += 1

    report = {
        'scope': scope,
        'total': len(redirectors),
        'counts': {
            'safe': len(safe),
            'safe_touching_levels': safe_with_level,
            'blocked': len(blocked),
            # Split broken into the two distinct sub-buckets. `broken` retained
            # for backward compat = orphaned_safe + orphaned_blocked + referenced_broken.
            'broken': len(orphaned_safe) + len(orphaned_blocked) + len(referenced_broken),
            'orphaned_safe': len(orphaned_safe),
            'orphaned_blocked': len(orphaned_blocked),
            'referenced_broken': len(referenced_broken),
            'non_writable': len(non_writable),
        },
        'blocked_by_user': [
            {'user': user, 'count': count, 'changes': sorted(blocked_changes_by_user[user])}
            for user, count in blocked_by_user.most_common()
        ],
        'broken_samples': [r['pkg'] for r in referenced_broken[:20]],
        'orphaned_samples': [r['pkg'] for r in orphaned_safe[:20]],
    }

    save_safe_set(args.out_safe, scope, safe)
    if args.out_orphaned:
        save_safe_set(args.out_orphaned, scope, orphaned_safe)
    save_report(args.out_report, report)

    total_broken = len(orphaned_safe) + len(orphaned_blocked) + len(referenced_broken)
    print(f"Scope: {scope}")
    print(f"Total: {len(redirectors)}")
    print(f"  safe to fix:      {len(safe)}" + (f"  ({safe_with_level} touch levels)" if safe_with_level else ""))
    print(f"  blocked:          {len(blocked)}")
    for entry in report['blocked_by_user']:
        changes = ', '.join(f"CL {c}" if c.isdigit() else 'default CL' for c in entry['changes'])
        print(f"      @{entry['user']}: {entry['count']} blockers ({changes})")
    print(f"  broken target:    {total_broken}")
    print(f"      orphaned (safe to delete):     {len(orphaned_safe)}")
    print(f"      orphaned but redirector locked: {len(orphaned_blocked)}")
    print(f"      referenced-broken (manual):     {len(referenced_broken)}")
    print(f"  non-writable:     {len(non_writable)}")
    print()
    print(f"Wrote {args.out_safe}")
    if args.out_orphaned:
        print(f"Wrote {args.out_orphaned}")
    print(f"Wrote {args.out_report}")


if __name__ == '__main__':
    main()
