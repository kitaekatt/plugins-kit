"""Moved to ``task_system.plan_sections``, so the export reads it without importing the dashboard.

This name stays an alias of that module (the same module object), so every
dashboard import, private name and test monkeypatch keeps its meaning.
"""

from __future__ import annotations

import sys

from .. import plan_sections as _moved

sys.modules[__name__] = _moved
