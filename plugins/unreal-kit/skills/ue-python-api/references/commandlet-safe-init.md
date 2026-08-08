# Commandlet-Safe Project Init Scripts

When UE runs in commandlet mode (no Editor UI), it still loads `Content/Python/init_unreal.py`. Project init scripts that touch Editor-UI APIs without guarding for commandlet mode print Python tracebacks every commandlet run -- noise that obscures legitimate output and makes commandlet logs harder to read.

In one observed `/fix-up-redirectors` run, three init-script bugs combined to produce **29 LogPython errors per commandlet startup**, hiding behind the legitimate UE output and only visible when the operator looked carefully.

## Three patterns to apply

### 1. Gate UI-touching init code behind `not is_trying_to_run_commandlet()`

`init_unreal.py` is loaded unconditionally on every UE startup, including commandlets. If it calls a function that builds menus, sub-menus, toolbars, or any other UI surface, gate the call:

```python
import unreal

if not unreal.EditorItemConfigStatics.is_trying_to_run_commandlet():
    from editor_tools.init_sc_tools import init_sc_tools
    init_sc_tools()
```

This is the most surgical fix because it skips the work entirely in commandlet mode rather than relying on every called function to be commandlet-safe individually.

### 2. Defensively guard `find_menu()` returns

`unreal.ToolMenus.get().find_menu('LevelEditor.MainMenu')` returns `None` in commandlet mode (and during early init phases before the UI is up). Calling methods on the result blows up with `AttributeError: 'NoneType' object has no attribute 'add_sub_menu'`. Defensive pattern:

```python
def populate_main_menu():
    main_menu = unreal.ToolMenus.get().find_menu('LevelEditor.MainMenu')
    if main_menu is None:
        return  # No main menu (commandlet, headless, or pre-UI init)
    # ... build sub-menus
```

This is belt-and-braces with the not-commandlet gate at the call site -- both are good practice. Either alone catches most cases; both together catch all of them, including any case where the function is called from a context the original author didn't anticipate.

### 3. Catch `ImportError` separately for optional debug-only deps

`pydevd` (PyCharm remote debug attach) is often not installed in the Python that ships with UE -- so an `import pydevd` in a debug-attach helper raises `ModuleNotFoundError`. A naked `try/except Exception` around the entire debug-attach block catches the import failure but typically prints the traceback for diagnostic reasons -- which is the wrong default for an *optional* dependency that's expected to be missing in most environments. Split the imports:

```python
def attach_to_debugger(host, port):
    try:
        import pydevd
    except ImportError:
        return  # pydevd is optional; skip silently rather than spamming a traceback

    try:
        pydevd.settrace(...)
    except Exception:
        import traceback
        traceback.print_exc()
```

The pattern: silent skip on import failure (a routine, expected condition), traceback only on actual operational failure.

## Verification

Run a UE commandlet (any one -- the discovery commandlet from `fix-up-redirectors` is fast at ~13 seconds) and grep the output for `Error|Traceback|attach_debugger|init_sc_tools|main_menu|pydevd`. After applying all three patterns, the only remaining `Error` matches should be unrelated UE-internal noise (e.g. `LogWindows: Failed to load 'aqProf.dll'` and similar conditional-DLL loads).

Surfaced 2026-05-05 in a consuming UE project and fixed there. The same patterns apply to any UE project that ships project-side Python init scripts.
