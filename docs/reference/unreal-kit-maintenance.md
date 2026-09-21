# unreal-kit maintenance

The files under `plugins/unreal-kit/lib` are shipped runtime files. They do
not load repository guidance. The local `path_repair.py` and
`bootstrap_guard.py` files are vendored copies of the bootstrap provider's
runtime helpers so Unreal-side scripts can run without importing the provider
package from the active environment.

When a vendored helper changes, compare it with its canonical bootstrap copy
and update both in the same change. The bootstrap vendoring tests discover
these filenames and enforce byte identity. Consumer documentation belongs in
the plugin skills and README; repository maintenance rules belong here or in
the root and plugins guidance.

The redirector code-reference filter is intentionally heuristic. If a
regression exposes a missed channel, maintainers should identify the file type,
path shape, mount source, or registry blind spot; update the scanner while
preserving mount and on-disk validation; invalidate the project-local cache;
rerun the filter; and document the resulting coverage. Consumer guidance only
lists the coverage and the supported `--extensions` option. It does not tell a
plugin user to edit scanner internals.
