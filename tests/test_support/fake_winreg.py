"""Small in-memory winreg substitute for cross-platform bootstrap tests."""

import types


class FakeWinreg(types.ModuleType):
    """In-memory registry module with the subset used by bootstrap checks."""

    HKEY_CURRENT_USER = object()
    HKEY_LOCAL_MACHINE = object()
    KEY_READ = 1
    KEY_WRITE = 2
    REG_SZ = 1
    REG_EXPAND_SZ = 2

    class _Key:
        def __init__(self, owner, root, subkey):
            self.owner = owner
            self.root = root
            self.subkey = subkey

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def __init__(self):
        super().__init__("winreg")
        self._values = {
            self.HKEY_CURRENT_USER: {"Environment": {}},
            self.HKEY_LOCAL_MACHINE: {},
        }
        self.store = {}
        self.writes = []
        self.open_error = None

    def OpenKey(self, root, subkey, reserved=0, access=0):
        if self.open_error is not None:
            raise self.open_error
        return self._Key(self, root, subkey)

    def QueryValueEx(self, key, name):
        if key.root is self.HKEY_CURRENT_USER and key.subkey == "Environment":
            if name not in self.store:
                raise FileNotFoundError(name)
            stored = self._values[key.root][key.subkey].get(name)
            value_type = stored[1] if isinstance(stored, tuple) else self.REG_SZ
            return self.store[name], value_type
        values = self._values.get(key.root, {}).get(key.subkey, {})
        if name not in values:
            raise FileNotFoundError(name)
        value, value_type = values[name]
        return value, value_type

    def SetValueEx(self, key, name, reserved, value_type, value):
        self._values.setdefault(key.root, {}).setdefault(key.subkey, {})[name] = (
            value, value_type
        )
        if key.root is self.HKEY_CURRENT_USER and key.subkey == "Environment":
            self.store[name] = value
        self.writes.append((name, value))

    def DeleteValue(self, key, name):
        values = self._values.get(key.root, {}).get(key.subkey, {})
        if name not in values:
            raise FileNotFoundError(name)
        del values[name]
        if key.root is self.HKEY_CURRENT_USER and key.subkey == "Environment":
            self.store.pop(name, None)

    def set_value(self, root, subkey, name, value, value_type=None):
        if value_type is None:
            value_type = self.REG_EXPAND_SZ
        self._values.setdefault(root, {}).setdefault(subkey, {})[name] = (
            value, value_type
        )
        if root is self.HKEY_CURRENT_USER and subkey == "Environment":
            self.store[name] = value
