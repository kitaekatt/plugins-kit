"""Phase 3 facade: apply fixups for the safe set in a fresh CL.

Runs inside Unreal as a commandlet. Reads SAFE_JSON env var. Optionally
reads CL_DESC_SUFFIX env var (e.g. '[Mix, Tool]') to append a project-specific
tag to the CL description.

Two modes:

  - Fix-up mode (default): redirectors with referencers. Loads each referencer
    in UE, force-saves to rewrite import tables, then deletes the redirector
    .uasset files. Used for the standard fix-up safe set.
  - Delete-only mode: redirectors with zero referencers (orphans). Skips all
    UE referencer load/save work — there are no referencers to rewrite.
    Auto-detected when no records have any referencers, or forced via
    `--mode=delete-only`. The orphan path is much faster (no commandlet asset
    loading) and avoids the file-lock dance entirely (UE never opens the
    redirector packages, so it never holds Windows file handles on them).

Flow (fix-up mode):

  1. Guard against duplicate "Fix up redirectors" pending CLs (auto-detects
     P4USER from `p4 info` if not in env).
  2. Create a fresh pending CL.
  3. `p4 edit` only the non-redirector REFERENCER files into the CL. The
     redirector .uasset files are not edited - they will be deleted instead.
  4. UE: load every referencer asset and force-save it. UE's Linker resolves
     redirectors at load time; force-saving rewrites the package's import
     table to reference the redirector's target directly, severing the link.
     Soft references that were not auto-resolved are also rewritten via
     `rename_referencing_soft_object_paths` after the packages are loaded.
  5. `p4 delete` the redirector .uasset files into the same CL. This opens
     them for delete and removes them from the workspace.
  6. Save a manifest.

Flow (delete-only mode): steps 1, 2, then `p4 delete` the redirector files
(plus their .umap siblings if present — level redirectors come in .uasset+.umap
pairs and both must land in the CL together). No UE referencer work.

UE5.x note: `unreal.AssetTools.fixup_referencers` does NOT exist in current
Python bindings. The load+save pattern in step 4 is the supported substitute.

SAFE_JSON resolution: the env var is resolved to an absolute path before
use. UE commandlets run from a different cwd than the user's shell, so
relative paths fail silently — always normalize to absolute first.
"""
import os
import sys

sys.path.insert(0, os.path.expanduser('~/.claude/plugins/data/plugins-kit/unreal-kit/lib'))
sys.path.insert(0, os.path.expanduser('~/.claude/plugins/data/plugins-kit/unreal-kit/github/unreal-pip'))
from bootstrap import ensure_dependencies
ensure_dependencies()

# Restore registry-canonical PATH before p4 subprocess fan-out (see
# unreal-kit/lib/path_repair.py). Defense-in-depth — even though UE
# commandlets rarely trip cmd.exe overflow, the cost is negligible.
from path_repair import repair_path
repair_path()

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'lib')))
import unreal
from p4cli import (
    create_pending_cl, delete_files, edit_files, get_p4_user, reopen_files,
    run_p4, run_p4_or_die,
)
from redirector_record import load_safe_set, save_apply_manifest


def fail(msg):
    sys.stderr.write(f"[apply_fixups] FAIL: {msg}\n")
    sys.exit(1)


SAFE_JSON = os.environ.get('SAFE_JSON')
CL_DESC_SUFFIX = os.environ.get('CL_DESC_SUFFIX', '').strip()
FORCE_NEW_CL = '--force-new-cl' in sys.argv

# Parse --mode={fixup,delete-only,auto}; default 'auto' picks based on input shape.
MODE = 'auto'
for arg in sys.argv[1:]:
    if arg.startswith('--mode='):
        MODE = arg.split('=', 1)[1].strip()
if MODE not in ('auto', 'fixup', 'delete-only'):
    fail(f"--mode must be one of: auto, fixup, delete-only (got {MODE!r})")

# Resolve SAFE_JSON to absolute. Commandlets run from <project>/Binaries/Win64
# (or similar), not the user's shell cwd, so a relative SAFE_JSON silently
# misses the file. Normalizing here once removes the foot-gun.
if SAFE_JSON:
    SAFE_JSON = os.path.abspath(SAFE_JSON)
if not SAFE_JSON or not os.path.isfile(SAFE_JSON):
    fail(f"SAFE_JSON env var not set or file missing: {SAFE_JSON!r}")

safe = load_safe_set(SAFE_JSON)
scope = safe.get('scope', '/Game')
records = safe.get('redirectors', [])
if not records:
    print("Nothing safe to fix - aborting.")
    sys.exit(0)

# Auto-detect mode from the safe-set shape: if no record has any referencers,
# this is an orphaned-only set and the delete-only path is correct.
def _has_any_referencers(rec):
    return bool(rec.get('referencer_files') or rec.get('referencer_pkgs')
                or rec.get('has_level_referencer'))

if MODE == 'auto':
    MODE = 'delete-only' if not any(_has_any_referencers(r) for r in records) else 'fixup'
print(f"[apply_fixups] mode: {MODE}")

# 1) Guard: refuse if a "Fix up redirectors" CL is already pending for this user.
if not FORCE_NEW_CL:
    p4_user = get_p4_user()
    if not p4_user:
        sys.stderr.write(
            "[apply_fixups] WARN: Could not resolve P4USER (env or `p4 info`). "
            "Skipping the existing-CL guard.\n"
        )
    else:
        existing = run_p4_or_die(['changes', '-s', 'pending', '-l', '-u', p4_user])
        for chunk in existing.split('Change '):
            if 'Fix up redirectors' in chunk:
                cl_num = chunk.split(' ', 1)[0].strip()
                fail(
                    f"Pending CL {cl_num} already starts with 'Fix up redirectors'. "
                    f"Submit/revert it first, or pass --force-new-cl."
                )

# 2) Build the file sets.
#    - redirector_files: every redirector .uasset (these will be DELETED).
#    - referencer_pkgs / referencer_files: union of all referencers,
#      MINUS any package that is itself a redirector in this safe set
#      (those will be deleted, not re-saved). Empty in delete-only mode.
redirector_pkgs = {r['pkg'] for r in records}
redirector_files = [r['file'] for r in records if r.get('file')]

# Always include .umap siblings of every redirector .uasset, in both modes.
# Level redirectors come in .uasset+.umap pairs and both files must land in
# the CL together — otherwise the depot keeps a dangling .umap pointing at a
# deleted .uasset (or vice versa). Two distinct shapes need this:
#   - Normal level redirectors: both .uasset and .umap exist on disk; UE deletes
#     both; we need both in the apply CL.
#   - .umap-native packages: discovery (UE asset registry) reports a .uasset
#     path but on disk only the .umap exists; UE delete_asset removes the .umap
#     and auto-opens it for delete in default; without pairing, the .umap is
#     stranded outside the apply CL while the .uasset path is a phantom.
# We add the sibling unconditionally if it exists on disk; cheap to check,
# prevents an easy-to-miss correctness bug in either mode.
def _umap_sibling(uasset_path):
    if not uasset_path or not uasset_path.lower().endswith('.uasset'):
        return None
    candidate = uasset_path[:-len('.uasset')] + '.umap'
    return candidate if os.path.isfile(candidate) else None


umap_companion_files = []
for f in list(redirector_files):
    sibling = _umap_sibling(f)
    if sibling:
        umap_companion_files.append(sibling)
if umap_companion_files:
    redirector_files = redirector_files + umap_companion_files
    print(f"Including {len(umap_companion_files)} .umap sibling(s) of level redirectors.")

referencer_pkgs = set()
referencer_files = set()
if MODE == 'fixup':
    for r in records:
        for ref_pkg in (r.get('referencer_pkgs') or []):
            if ref_pkg in redirector_pkgs:
                continue
            referencer_pkgs.add(ref_pkg)
        for ref_file in (r.get('referencer_files') or []):
            referencer_files.add(ref_file)
    # Drop referencer files that are themselves redirector files.
    referencer_files -= set(redirector_files)
referencer_files = sorted(referencer_files)

print(f"Redirectors to delete: {len(redirector_files)}")
if MODE == 'fixup':
    print(f"Non-redirector referencers to rewrite: {len(referencer_pkgs)} pkgs / "
          f"{len(referencer_files)} files")

# 3) Create the CL.
mode_label = 'orphans' if MODE == 'delete-only' else 'assets'
title = f"Fix up redirectors: {len(records)} {mode_label} in {scope}"
if CL_DESC_SUFFIX:
    title = f"{title} {CL_DESC_SUFFIX}"
if MODE == 'delete-only':
    description = (
        f"{title}\n\n"
        "Automated cleanup via /fix-up-redirectors (delete-only mode). "
        "Deletes orphaned redirector .uasset files (target missing, zero "
        "referencers) plus their .umap siblings."
    )
else:
    description = (
        f"{title}\n\n"
        "Automated cleanup via /fix-up-redirectors. Rewrites referencers to "
        "point at redirector targets and deletes the redirector .uasset files."
    )
cl_num = create_pending_cl(description, client=os.environ.get('P4CLIENT'))
print(f"Created CL {cl_num}")

# Initialize the bookkeeping that both modes' manifest needs.
loaded_referencers = []
load_failures = []
saved = 0
save_failures = []
ue_deleted = 0
ue_delete_failures = []
collections_reopened = []

# 4) Mode-specific work.
if MODE == 'fixup':
    # 4-pre) Suppress UE's per-collection auto-submit during this commandlet.
    #
    # UE's CollectionManager (Engine/Source/Developer/CollectionManager/Private/
    # CollectionContainer.cpp::HandleRedirectorsDeleted) listens for redirector
    # deletions, rewrites every affected .collection file (Content/Collections/
    # *.collection) in-place, and -- if UCollectionSettings::bAutoCommitOnSave
    # is true (the engine default) -- immediately submits each one as its own
    # one-file changelist via FCheckIn. Description shows up as "Collection
    # '<Name>' not modified" (Collection.cpp:1121) when the logical members are
    # unchanged but the on-disk paths got rewritten.
    #
    # In a typical fix-up run that touches collection members, this generates
    # dozens of stray one-file CLs that fragment review and pollute the change
    # history. The setting is editor-wide (not per-asset), so we flip the CDO
    # for the lifetime of this commandlet. UE exits, the value resets -- no
    # project .ini change, no impact on interactive editor sessions.
    #
    # The collection file STILL gets correctly rewritten and left open-for-edit
    # in P4; step 4d below moves it into the apply CL alongside the redirector
    # deletes so the whole batch lands as one reviewable changelist.
    try:
        unreal.get_default_object(unreal.CollectionSettings).set_editor_property(
            'b_auto_commit_on_save', False
        )
        print("Disabled UCollectionSettings.bAutoCommitOnSave for this commandlet.")
    except Exception as exc:
        print(f"[apply_fixups] WARN: could not flip bAutoCommitOnSave ({exc}); "
              f"UE may auto-submit collection files into stray one-file CLs.")

    # 4a) p4 edit only the non-redirector referencer files (so UE can re-save them).
    if referencer_files:
        edit_files(cl_num, referencer_files)
        print(f"Opened {len(referencer_files)} referencer file(s) for edit in CL {cl_num}.")

    # 4b) UE: load each referencer (resolves redirectors at link time), rewrite
    #     soft references via the supported API, then force-save so the package
    #     import table is rewritten to reference the target directly.
    asset_tools = unreal.AssetToolsHelpers.get_asset_tools()

    def _soft_object_path(pkg_path):
        asset_name = pkg_path.rsplit('/', 1)[-1]
        return unreal.SoftObjectPath(f"{pkg_path}.{asset_name}")

    for pkg in sorted(referencer_pkgs):
        asset = unreal.EditorAssetLibrary.load_asset(pkg)
        if asset is None:
            load_failures.append(pkg)
        else:
            loaded_referencers.append(pkg)
    print(f"Loaded {len(loaded_referencers)} referencer asset(s) "
          f"({len(load_failures)} failed to load)")

    # Soft-reference rewrite (only meaningful when there are loaded packages).
    redirector_map = {}
    for r in records:
        target = r.get('target_pkg')
        if target and r.get('target_exists'):
            redirector_map[_soft_object_path(r['pkg'])] = _soft_object_path(target)
    if loaded_referencers and redirector_map:
        pkg_names = unreal.Array(unreal.Name)
        for p in loaded_referencers:
            pkg_names.append(unreal.Name(p))
        asset_tools.rename_referencing_soft_object_paths(pkg_names, redirector_map)
        print("Rewrote soft references.")

    for pkg in loaded_referencers:
        asset = unreal.EditorAssetLibrary.load_asset(pkg)
        if asset is None or not unreal.EditorAssetLibrary.save_loaded_asset(asset, only_if_is_dirty=False):
            save_failures.append(pkg)
        else:
            saved += 1
    print(f"Force-saved {saved} referencer package(s) "
          f"({len(save_failures)} failures)")

    # 4c) Delete each redirector via UE first, releasing the Windows file
    #     handle so `p4 delete` can succeed. Without this step `p4 delete`'s
    #     internal unlink fights UE for the lock and the batch fails partway.
    for r in records:
        pkg = r['pkg']
        try:
            if unreal.EditorAssetLibrary.does_asset_exist(pkg):
                if unreal.EditorAssetLibrary.delete_asset(pkg):
                    ue_deleted += 1
                    continue
            ue_delete_failures.append(pkg)
        except Exception as exc:
            ue_delete_failures.append(f"{pkg}: {exc}")
    print(f"UE-deleted {ue_deleted} redirector asset(s) "
          f"({len(ue_delete_failures)} failures)")

    # Force a GC pass so any lingering handles on package objects are released
    # before P4 tries to record the delete.
    try:
        unreal.SystemLibrary.collect_garbage(0)
    except Exception:
        pass

    # 4d) Herd UE-auto-opened .collection files into the apply CL.
    #     With bAutoCommitOnSave flipped off above, UE still calls
    #     SourceControlProvider::CheckOut on each affected .collection (so the
    #     file is correctly rewritten on disk and opened-for-edit in P4) but
    #     skips FCheckIn. The default landing spot for UE-internal checkouts
    #     is the workspace's default CL. Sweep there and reopen into our apply
    #     CL so the rewrites travel with the redirector deletes as one atomic,
    #     reviewable changelist.
    rc, out, _err = run_p4(['opened', '-c', 'default', '//...Content/Collections/....collection'])
    if rc == 0:
        for line in out.splitlines():
            # Format: //depot/path/file.collection#N - edit default change (text) by user@client
            if ' - ' in line:
                depot_path = line.split('#', 1)[0]
                if depot_path.endswith('.collection'):
                    collections_reopened.append(depot_path)
    if collections_reopened:
        try:
            reopen_files(cl_num, collections_reopened)
            print(f"Reopened {len(collections_reopened)} UE-touched .collection file(s) into CL {cl_num}.")
        except SystemExit:
            sys.stderr.write("[apply_fixups] WARN: p4 reopen of .collection files failed; "
                             "they remain in default CL.\n")

# 5) Open redirector .uasset (and .umap sibling) files for delete in the CL.
#    In fixup mode UE already deleted them on disk + auto-opened them in the
#    default CL — we use `p4 reopen -c` to herd them into our pending CL, with
#    `p4 delete -c` as fallback. In delete-only mode UE never touched them, so
#    `p4 delete -c` does the whole job.
delete_failures = []
lock_failures = []  # files that hit "in use by another process" and need a retry pass

if redirector_files:
    rc, out, _err = run_p4(['-x', '-', 'fstat', '-T', 'depotFile,action,change'],
                           stdin='\n'.join(redirector_files))
    auto_opened = []
    not_opened = []
    cur_path = None
    cur_action = None
    if rc == 0:
        for line in out.splitlines():
            line = line.strip()
            if not line:
                if cur_path and cur_action == 'delete':
                    auto_opened.append(cur_path)
                elif cur_path and cur_action is None:
                    not_opened.append(cur_path)
                cur_path = cur_action = None
            elif line.startswith('... depotFile '):
                cur_path = line[len('... depotFile '):]
            elif line.startswith('... action '):
                cur_action = line[len('... action '):]
        if cur_path and cur_action == 'delete':
            auto_opened.append(cur_path)
        elif cur_path and cur_action is None:
            not_opened.append(cur_path)

    if auto_opened:
        try:
            reopen_files(cl_num, auto_opened)
            print(f"Reopened {len(auto_opened)} UE-auto-deleted redirector(s) into CL {cl_num}.")
        except SystemExit:
            delete_failures.extend(auto_opened)
            sys.stderr.write("[apply_fixups] WARN: p4 reopen batch failed; see error above.\n")
    if not_opened:
        # Per-file `p4 delete` so a single locked file doesn't sink the whole
        # batch. Files that hit "in use by another process" go to lock_failures
        # for the post-UE retry pass below.
        for path in not_opened:
            rc, _out, err = run_p4(['delete', '-c', cl_num, path])
            if rc == 0:
                continue
            err_lower = (err or '').lower()
            # Windows P4 emits "being used by another process" (the wording from
            # the OS error string), not "in use by another process". Cover both
            # phrasings plus the generic "access is denied" so the lock path is
            # entered whenever the OS denied us a handle to delete.
            locked_markers = (
                'being used by another process',
                'in use by another process',
                'access is denied',
            )
            if any(marker in err_lower for marker in locked_markers):
                lock_failures.append(path)
            else:
                delete_failures.append(path)
                sys.stderr.write(f"[apply_fixups] WARN: p4 delete failed for {path}: {err.strip()}\n")
        if not_opened:
            print(f"Attempted p4 delete on {len(not_opened)} file(s); "
                  f"{len(not_opened) - len(lock_failures) - len(delete_failures)} succeeded, "
                  f"{len(lock_failures)} locked (will retry via p4 delete post-exit).")

# 6) Lock-failure retry pass.
#    UE has been observed to keep Windows file handles open on referencer
#    packages even after `delete_asset` returns (and even after a GC). The
#    locked files can't be `p4 delete`d while UE holds them. Empirically the
#    files are STILL ON DISK when the commandlet exits -- UE held the handle
#    long enough that disk-delete never happened in-process -- so the post-
#    exit retry needs `p4 delete` (which marks the depot delete and removes
#    the local file in one step), NOT `p4 reconcile` (which would see an
#    unchanged file on disk and do nothing).
#    We can't wait for UE-exit inside the commandlet, so we attach a
#    delayed-delete file list that the skill phase runs after the commandlet
#    returns. If a future UE version actually finishes the on-disk delete
#    before exiting, `p4 delete` errors per-file with "file not found" and
#    the user can fall back to `p4 reconcile` against the same list.
retry_script = None
if lock_failures:
    # Drop a newline-separated file list next to the manifest. The skill's
    # Phase 4 tail pipes it to `p4 -x - delete -c <CL>` once the commandlet
    # exits and the locks release.
    retry_dir = os.path.join(str(unreal.Paths.project_dir()), 'Saved', 'PythonOutput')
    os.makedirs(retry_dir, exist_ok=True)
    retry_script = os.path.join(retry_dir, f'redirectors_lock_retry_{cl_num}.txt')
    with open(retry_script, 'w') as f:
        for path in lock_failures:
            f.write(path + '\n')
    print(f"[apply_fixups] {len(lock_failures)} file(s) locked by UE; "
          f"retry list at {retry_script}")
    print(f"[apply_fixups] Run after UE exits: "
          f"p4 -x - delete -c {cl_num} < {retry_script}")

# 7) Manifest.
manifest = {
    'cl': cl_num,
    'scope': scope,
    'mode': MODE,
    'redirectors_deleted': len(redirector_files) - len(delete_failures) - len(lock_failures),
    'referencers_saved': saved,
    'referencer_save_failures': save_failures,
    'redirector_delete_failures': delete_failures,
    'redirector_lock_failures': lock_failures,
    'load_failures': load_failures,
    'umap_companions_included': umap_companion_files,
    'collections_reopened': collections_reopened,
    'lock_retry_list': retry_script,
}
manifest_path = os.path.join(
    str(unreal.Paths.project_dir()), 'Saved', 'PythonOutput',
    f'redirectors_apply_{cl_num}.yaml',
)
save_apply_manifest(manifest_path, manifest)

print()
print(f"Done. CL {cl_num}: deleted {manifest['redirectors_deleted']} redirectors"
      + (f", saved {saved} referencers" if MODE == 'fixup' else "")
      + ".")
if lock_failures:
    print(f"  ({len(lock_failures)} file(s) locked by UE — retry via the "
          f"p4 delete command above after UE exits.)")
print(f"Manifest: {manifest_path}")
