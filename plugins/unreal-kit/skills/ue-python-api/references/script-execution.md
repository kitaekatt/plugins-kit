# Script Execution Modes

## 0. Host Terminal Runner

Run the host-side runner through the bootstrap-selected interpreter. The
expression keeps project environments preferred and emits a version diagnosis
when the bootstrap variables are missing:

```
"${BOOTSTRAP_PROJECT_PYTHON:-${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}}" "${CLAUDE_PLUGIN_ROOT}/skills/ue-python-api/scripts/ue_runner.py" <script>.py
```

The `py` examples below are Unreal Editor's embedded-console syntax. They run
inside the Editor and are separate from the host launch command above.

## 1. Output Log Console

The most common way to run scripts interactively.

1. Open: Window → Developer Tools → Output Log
2. Switch the dropdown from "Cmd" to **"Python"**
3. Type commands directly, or run a file:

```
py "C:/path/to/script.py"
```

**Notes:**
- Paths must use forward slashes or escaped backslashes
- The `unreal` module is auto-imported
- Console history persists during the editor session

## 2. Execute Python Command (Editor Menu)

File → Execute Python Script — opens a file browser to select a `.py` file.

## 3. Startup Scripts

Place `.py` files in `<Project>/Content/Python/` — they run automatically on editor startup.

**Use case:** Setting up persistent editor utilities, registering custom menus, or auto-loading development tools.

**Subfolders:** Scripts in `Content/Python/init_unreal.py` specifically are auto-run.

## 4. Commandline Execution (Headless)

Run scripts without opening the full editor UI:

```
UnrealEditor-Cmd.exe "C:/path/to/project.uproject" -ExecutePythonScript="C:/path/to/script.py"
```

The host runner exposes the same operation through `run_ue_script` and its
CLI. Use `--commandlet-timeout <seconds>` to bound only the headless
commandlet; the remote editor transport keeps its own contract. The Python
API argument is `commandlet_timeout_s=None`. The value must be finite and
positive. When it is unset, the legacy unbounded behavior remains available,
but it is not a hang guarantee. Unattended jobs must provide an explicit
budget.

A commandlet timeout terminates and reaps the child through the subprocess
boundary, retains partial stdout and stderr, returns a nonzero result with
completion marked unknown, and does not retry the script. A commandlet may
have applied partial asset changes, so callers must inspect the retained
diagnosis before deciding what to do next.

**Use case:** CI/CD, batch processing, automated asset audits — and the default mode when the terminal runner detects no running Editor.

**What works in commandlet mode:**
- Asset loading and saving (`EditorAssetLibrary.load_asset`, `save_loaded_asset`)
- Asset listing and searching (`EditorAssetLibrary.list_assets`)
- Asset registry queries and filtering (`AssetRegistryHelpers.get_asset_registry()`)
- Property reading and writing (`get_editor_property`, `set_editor_property`)
- Reference graph traversal (dependencies, referencers)
- DataTable queries
- Blueprint inspection
- Asset renaming, deleting, duplicating

**What requires an open Editor (does NOT work in commandlet mode):**
- `EditorUtilityLibrary.get_selected_assets()` / `get_selected_actors()` — needs an active selection
- `EditorLevelLibrary.get_all_level_actors()` — needs an open level viewport
- `ScopedSlowTask.make_dialog()` — no UI to show progress bar (the task itself still works, just no dialog)
- Editor Utility Widgets
- PIE (Play in Editor)
- Any operation that reads from or writes to the active viewport

## 5. Editor Utility Widgets (Python + UI)

Create Python-backed editor tabs with Slate/UMG widgets:

```python
# Register a simple editor utility
import unreal

@unreal.uclass()
class MyEditorUtility(unreal.EditorUtilityObject):
    pass
```

More commonly done via Blueprint Editor Utility Widgets that call Python functions.

## 6. Init Scripts and Site Packages

Configure in Editor Preferences → Plugins → Python:
- **Additional Paths**: Add directories to Python's `sys.path`
- **Startup Scripts**: List of scripts to run on editor startup

## Progress Feedback for Long Operations

```python
import unreal

total = 1000
with unreal.ScopedSlowTask(total, "Processing assets...") as slow_task:
    slow_task.make_dialog(True)  # Show progress bar with cancel button
    for i in range(total):
        if slow_task.should_cancel():
            break
        slow_task.enter_progress_frame(1, f"Processing {i}/{total}")
        # ... do work ...
```

## Logging

```python
unreal.log("Info message")          # Normal log
unreal.log_warning("Warning!")      # Yellow warning
unreal.log_error("Error!")          # Red error
```

## Error Handling

```python
try:
    asset = unreal.EditorAssetLibrary.load_asset('/Game/Bad/Path')
    if asset is None:
        unreal.log_error("Asset not found")
except Exception as e:
    unreal.log_error(f"Exception: {e}")
```

## Batch Operations Pattern

```python
import unreal, json, os

def batch_process(asset_paths, processor_fn):
    """Process multiple assets with progress bar and error collection."""
    results = {'success': [], 'errors': []}
    total = len(asset_paths)

    with unreal.ScopedSlowTask(total, "Batch processing...") as task:
        task.make_dialog(True)
        for i, path in enumerate(asset_paths):
            if task.should_cancel():
                unreal.log_warning(f"Cancelled at {i}/{total}")
                break
            task.enter_progress_frame(1, f"{i+1}/{total}: {path.split('/')[-1]}")
            try:
                result = processor_fn(path)
                results['success'].append({'path': path, 'result': result})
            except Exception as e:
                results['errors'].append({'path': path, 'error': str(e)})
                unreal.log_warning(f"Error on {path}: {e}")

    unreal.log(f"Done: {len(results['success'])} success, {len(results['errors'])} errors")
    return results
```

## Writing Output Files

For large results, write to the project's Saved directory:

```python
import json, os

def write_output(data, filename):
    """Write JSON output to <Project>/Saved/PythonOutput/."""
    out_dir = os.path.join(unreal.Paths.project_dir(), 'Saved', 'PythonOutput')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, filename)
    with open(out_path, 'w') as f:
        json.dump(data, f, indent=2, default=str)
    unreal.log(f"Output: {out_path}")
    return out_path
```

## Importing External Python Packages

The editor's Python is an embedded interpreter. To use external packages:

1. Find the editor's Python: `import sys; print(sys.executable)`
2. Install with pip: `<UE_Python_exe> -m pip install <package>`
3. Or add to Additional Paths in Editor Preferences

**Caution:** Installing packages into the engine's Python can cause conflicts.
Prefer writing to `<Project>/Saved/PythonOutput/` as JSON and processing externally.
